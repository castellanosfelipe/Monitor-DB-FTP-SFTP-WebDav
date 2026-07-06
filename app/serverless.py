"""Serverless check runner (Vercel + Neon).

Without a long-running scheduler, checks execute inside cron-triggered
invocations: each tick runs every *due* connection (most overdue first)
within a time budget, so a slow batch never exceeds the function's max
duration — whatever is left over runs on the next tick.

Courtesy notes in this mode: within one tick the usual :class:`Throttle`
serializes per-host and enforces spacing/rate; ticks themselves are spaced by
the cron cadence (≥1 min), which keeps per-connection intervals ≥60 s.
Backoff during outages works unchanged — the incident tracker rebuilds its
streak state from the database on every fresh process.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from app.checkers import get_checker
from app.errors import ErrorType
from app.models import CheckResult, ConnectionConfig, Status
from app.platform.secretstore import SecretStoreError
from app.throttle import backoff_delay
from app.util import from_iso, to_iso, utc_now

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_S = 45.0
_PURGE_MARKER = "housekeeping.last_purge_day"


def _run_one_check(ctx: Any, cfg: ConnectionConfig) -> CheckResult:
    secret: str | None = None
    if cfg.secret_encrypted:
        if ctx.secret_store is None:
            return CheckResult(
                status=Status.DOWN, latency_ms=None, error_type=ErrorType.AUTH,
                error_msg="no hay almacén de secretos configurado",
            )
        try:
            secret = ctx.secret_store.decrypt(cfg.secret_encrypted)
        except SecretStoreError as exc:
            return CheckResult(
                status=Status.DOWN, latency_ms=None, error_type=ErrorType.AUTH,
                error_msg=str(exc),
            )
    checker = get_checker(cfg.protocol)
    with ctx.throttle.slot(cfg.host):
        return checker.check(cfg, secret)


def _due_connections(ctx: Any, now: datetime) -> list[tuple[float, ConnectionConfig]]:
    """(seconds overdue, config) for every enabled connection whose next slot passed."""
    latest = ctx.db.latest_checks()
    due: list[tuple[float, ConnectionConfig]] = []
    for cfg in ctx.db.list_connections(enabled_only=True):
        last = latest.get(cfg.id)
        if last is None:
            due.append((float("inf"), cfg))  # nunca chequeada: máxima prioridad
            continue
        if ctx.tracker.is_confirmed_down(cfg.id):
            delay = backoff_delay(
                cfg.interval_s,
                ctx.tracker.failures_since_confirm(cfg.id),
                ctx.throttle.policy.backoff_cap_s,
            )
        else:
            delay = float(cfg.interval_s)
        overdue = (now - from_iso(last["ts_utc"])).total_seconds() - delay
        if overdue >= 0:
            due.append((overdue, cfg))
    due.sort(key=lambda item: -item[0])
    return due


def _daily_housekeeping(ctx: Any, now: datetime) -> None:
    from app.settings_store import get_int

    today = now.strftime("%Y-%m-%d")
    if ctx.db.get_setting(_PURGE_MARKER) == today:
        return
    ctx.db.set_setting(_PURGE_MARKER, today)
    days = max(1, get_int(ctx.db, "retention.days"))
    cutoff = to_iso(now - timedelta(days=days))
    checks = ctx.db.purge_old_checks(cutoff)
    incidents = ctx.db.purge_old_incidents(cutoff)
    if checks or incidents:
        logger.info("purga: %d checks y %d incidentes anteriores a %s", checks, incidents, cutoff)


def run_due_checks(
    ctx: Any, budget_s: float = DEFAULT_BUDGET_S, now: datetime | None = None
) -> dict[str, Any]:
    """One cron tick: run due checks until done or out of budget."""
    now = now or utc_now()
    started = time.monotonic()
    due = _due_connections(ctx, now)
    checked = 0
    skipped = 0
    events_out: list[str] = []
    for index, (_, cfg) in enumerate(due):
        if time.monotonic() - started > budget_s:
            skipped = len(due) - index
            break
        try:
            result = _run_one_check(ctx, cfg)
            # Timestamp lógico del tick: mantiene deterministas los cálculos de
            # "próximo chequeo" aunque el lote tarde varios segundos.
            events = ctx.tracker.record(cfg, result, ts=now)
            checked += 1
            if events and ctx.alerter is not None:
                ctx.alerter.handle_events(events)
                events_out.extend(type(event).__name__ for event in events)
        except Exception:
            logger.exception("error en el chequeo serverless de %s", cfg.id)
    if ctx.alerter is not None:
        try:
            ctx.alerter.check_reminders()
        except Exception:
            logger.exception("error en recordatorios")
    _daily_housekeeping(ctx, now)
    return {
        "due": len(due),
        "checked": checked,
        "deferred": skipped,
        "events": events_out,
        "elapsed_s": round(time.monotonic() - started, 2),
    }

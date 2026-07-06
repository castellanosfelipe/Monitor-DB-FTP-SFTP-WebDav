"""Monitoring engine: orchestrates checks with APScheduler (RF-2).

Design: every connection is a *one-shot* job that re-arms itself when the
check finishes. This guarantees a connection never overlaps itself, retard
introduced by courtesy waits never accumulates drift (the next slot is
computed from "now", not from a fixed cadence), and backoff simply changes
the re-arm delay. ``misfire_grace_time=None`` makes jobs fire after system
sleep/clock changes instead of being dropped.
"""
from __future__ import annotations

import logging
import random
import threading
from datetime import timedelta
from typing import Callable

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.checkers import get_checker
from app.db import Database
from app.errors import ErrorType
from app.incidents import IncidentEvent, IncidentTracker
from app.models import CheckResult, ConnectionConfig, Status
from app.platform.secretstore import SecretStore, SecretStoreError
from app.throttle import Throttle, backoff_delay, jittered
from app.util import utc_now

logger = logging.getLogger(__name__)

# Consumed by the alerts module (Fase 4); until then events are just logged.
EventSink = Callable[[list[IncidentEvent]], None]


def compute_next_delay(
    cfg: ConnectionConfig,
    tracker: IncidentTracker,
    throttle: Throttle,
    rng: random.Random | None = None,
) -> float:
    """Next re-arm delay: jittered interval, or exponential backoff while DOWN."""
    policy = throttle.policy
    if cfg.id is not None and tracker.is_confirmed_down(cfg.id):
        base = backoff_delay(
            cfg.interval_s, tracker.failures_since_confirm(cfg.id), policy.backoff_cap_s
        )
    else:
        base = float(cfg.interval_s)
    return jittered(base, policy.jitter_ratio, rng)


class MonitorEngine:
    def __init__(
        self,
        db: Database,
        tracker: IncidentTracker,
        throttle: Throttle,
        secret_store: SecretStore | None,
        event_sink: EventSink | None = None,
    ) -> None:
        self._db = db
        self._tracker = tracker
        self._throttle = throttle
        self._secret_store = secret_store
        self._event_sink = event_sink
        self._paused = threading.Event()
        # Workers beyond global concurrency so threads waiting out host courtesy
        # don't starve runnable checks.
        workers = throttle.policy.global_concurrency * 2
        self._scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers=workers)},
            job_defaults={"misfire_grace_time": None, "coalesce": True},
            timezone="UTC",
        )

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._scheduler.start()
        connections = self._db.list_connections(enabled_only=True)
        for index, cfg in enumerate(connections):
            # Initial stagger so a restart doesn't fire every check at once.
            initial = 1.0 + (index % 10) + random.random() * 4.0
            self._arm(cfg.id, initial)
        logger.info("scheduler iniciado con %d conexiones", len(connections))

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

    def is_alive(self) -> bool:
        return bool(self._scheduler.running)

    def pause_all(self) -> None:
        self._paused.set()
        logger.info("monitoreo pausado (los chequeos en curso terminan)")

    def resume_all(self) -> None:
        self._paused.clear()
        logger.info("monitoreo reanudado")

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    # --- job management --------------------------------------------------------

    def _job_id(self, connection_id: int) -> str:
        return f"check-{connection_id}"

    def _arm(self, connection_id: int | None, delay_s: float) -> None:
        if connection_id is None:
            return
        self._scheduler.add_job(
            self._run_check,
            trigger="date",
            run_date=utc_now() + timedelta(seconds=delay_s),
            args=[connection_id],
            id=self._job_id(connection_id),
            replace_existing=True,
        )

    def schedule_connection(self, connection_id: int, immediate: bool = False) -> None:
        """(Re)schedule after create/update/resume."""
        cfg = self._db.get_connection(connection_id)
        if cfg is None or not cfg.enabled:
            self.unschedule_connection(connection_id)
            return
        self._arm(connection_id, 1.0 if immediate else jittered(min(10.0, cfg.interval_s)))

    def unschedule_connection(self, connection_id: int) -> None:
        try:
            self._scheduler.remove_job(self._job_id(connection_id))
        except Exception:
            pass  # not scheduled

    # --- the check itself ---------------------------------------------------------

    def _run_check(self, connection_id: int) -> None:
        cfg = self._db.get_connection(connection_id)
        if cfg is None or not cfg.enabled:
            return  # deleted or paused while queued: do not re-arm
        if self._paused.is_set():
            self._arm(connection_id, jittered(cfg.interval_s))
            return
        try:
            result = self.run_single_check(cfg)
            events = self._tracker.record(cfg, result)
            if events:
                for event in events:
                    logger.info("incidente: %s", event)
                if self._event_sink is not None:
                    try:
                        self._event_sink(events)
                    except Exception:
                        logger.exception("error en el sink de eventos/alertas")
        except Exception:
            logger.exception("error inesperado en el chequeo de %s", connection_id)
        finally:
            cfg_now = self._db.get_connection(connection_id)
            if cfg_now is not None and cfg_now.enabled:
                self._arm(connection_id, compute_next_delay(cfg_now, self._tracker, self._throttle))

    def run_single_check(self, cfg: ConnectionConfig) -> CheckResult:
        """One courteous check (also used by 'Probar conexión')."""
        try:
            secret = self._decrypt_secret(cfg)
        except SecretStoreError as exc:
            return CheckResult(
                status=Status.DOWN,
                latency_ms=None,
                error_type=ErrorType.AUTH,
                error_msg=str(exc),
            )
        checker = get_checker(cfg.protocol)
        with self._throttle.slot(cfg.host):
            return checker.check(cfg, secret)

    def _decrypt_secret(self, cfg: ConnectionConfig) -> str | None:
        if not cfg.secret_encrypted:
            return None
        if self._secret_store is None:
            raise SecretStoreError("no hay almacén de secretos configurado")
        return self._secret_store.decrypt(cfg.secret_encrypted)

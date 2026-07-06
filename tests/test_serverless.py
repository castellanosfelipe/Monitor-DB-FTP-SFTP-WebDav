"""Serverless mode tests: due-selection, budget, backoff spacing and the
incident tracker's state rebuild across process restarts."""
from __future__ import annotations

from datetime import timedelta

import pytest

import app.serverless as serverless_module
from app.db import Database
from app.errors import ErrorType
from app.incidents import IncidentOpened, IncidentTracker
from app.main import AppContext
from app.models import CheckResult, ConnectionConfig, Protocol, Status
from app.settings_store import DashboardAuth
from app.throttle import CourtesyPolicy, Throttle
from app.util import to_iso, utc_now


def make_ctx(tmp_path, alerter=None) -> AppContext:
    db = Database(tmp_path / "sl.db")
    return AppContext(
        db=db,
        tracker=IncidentTracker(db),
        throttle=Throttle(CourtesyPolicy(host_spacing_s=0.0, backoff_cap_s=300.0)),
        engine=None,
        secret_store=None,
        auth=DashboardAuth(enabled=False),
        mode="serverless",
        alerter=alerter,
    )


def add_conn(db, name, host="h.lan", interval=60, retries=1, enabled=True):
    cfg = ConnectionConfig(
        id=None, name=name, client="ACME", protocol=Protocol.FTP,
        host=host, port=21, interval_s=interval, retries=retries, enabled=enabled,
    )
    db.create_connection(cfg)
    return cfg


class FakeChecker:
    def __init__(self, result: CheckResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def check(self, cfg, secret):
        self.calls.append(cfg.name)
        return self._result


def up() -> CheckResult:
    return CheckResult(status=Status.UP, latency_ms=5.0)


def down() -> CheckResult:
    return CheckResult(
        status=Status.DOWN, latency_ms=None,
        error_type=ErrorType.TCP_CONNECT, error_msg="rechazada",
    )


def test_tick_runs_only_due_connections(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    now = utc_now()
    fresh = add_conn(ctx.db, "nunca-chequeada")
    recent = add_conn(ctx.db, "reciente")
    stale = add_conn(ctx.db, "vencida")
    paused = add_conn(ctx.db, "pausada", enabled=False)
    ctx.db.insert_check(recent.id, to_iso(now - timedelta(seconds=10)), "UP", 5.0, None, "")
    ctx.db.insert_check(stale.id, to_iso(now - timedelta(seconds=120)), "UP", 5.0, None, "")
    ctx.db.insert_check(paused.id, to_iso(now - timedelta(hours=2)), "UP", 5.0, None, "")

    checker = FakeChecker(up())
    monkeypatch.setattr(serverless_module, "get_checker", lambda p: checker)
    summary = serverless_module.run_due_checks(ctx, now=now)

    assert summary["checked"] == 2
    assert set(checker.calls) == {"nunca-chequeada", "vencida"}
    assert summary["deferred"] == 0


def test_tick_respects_budget_and_defers_rest(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    for i in range(5):
        add_conn(ctx.db, f"c{i}")

    class SlowChecker:
        def __init__(self):
            self.calls = 0

        def check(self, cfg, secret):
            self.calls += 1
            fake_time[0] += 30.0  # cada chequeo "tarda" 30 s
            return up()

    fake_time = [0.0]
    checker = SlowChecker()
    monkeypatch.setattr(serverless_module, "get_checker", lambda p: checker)
    monkeypatch.setattr(serverless_module.time, "monotonic", lambda: fake_time[0])

    summary = serverless_module.run_due_checks(ctx, budget_s=45.0)
    assert summary["checked"] == 2  # 0s→check(30s)→check(60s>45: para)
    assert summary["deferred"] == 3


def test_tick_confirms_down_across_separate_invocations(tmp_path, monkeypatch):
    """Cada tick puede ser un proceso nuevo: la racha debe sobrevivir vía BD."""
    base_now = utc_now()
    checker = FakeChecker(down())
    monkeypatch.setattr(serverless_module, "get_checker", lambda p: checker)

    ctx = make_ctx(tmp_path)
    cfg = add_conn(ctx.db, "srv", retries=1)  # confirma al 2º fallo

    serverless_module.run_due_checks(ctx, now=base_now)
    assert ctx.db.list_open_incidents() == []  # 1 fallo: histéresis

    # "proceso nuevo": contexto y tracker recién creados
    ctx2 = make_ctx(tmp_path)
    summary = serverless_module.run_due_checks(ctx2, now=base_now + timedelta(seconds=61))
    assert summary["checked"] == 1
    incidents = ctx2.db.list_open_incidents()
    assert len(incidents) == 1, "el 2º fallo en otro proceso debe confirmar DOWN"
    assert incidents[0]["started_at"] == ctx2.db.list_checks(cfg.id)[0]["ts_utc"]


def test_backoff_spacing_across_invocations(tmp_path, monkeypatch):
    checker = FakeChecker(down())
    monkeypatch.setattr(serverless_module, "get_checker", lambda p: checker)
    now = utc_now()

    ctx = make_ctx(tmp_path)
    add_conn(ctx.db, "srv", interval=60, retries=0)  # confirma al 1er fallo
    serverless_module.run_due_checks(ctx, now=now)
    assert len(ctx.db.list_open_incidents()) == 1

    # a los 61 s NO toca aún: el backoff exige interval×2 tras confirmar
    ctx2 = make_ctx(tmp_path)
    summary = serverless_module.run_due_checks(ctx2, now=now + timedelta(seconds=61))
    assert summary["checked"] == 0

    ctx3 = make_ctx(tmp_path)
    summary = serverless_module.run_due_checks(ctx3, now=now + timedelta(seconds=121))
    assert summary["checked"] == 1


def test_recovery_closes_and_alerts_in_new_process(tmp_path, monkeypatch):
    events_seen = []

    class SpyAlerter:
        def handle_events(self, events):
            events_seen.extend(events)

        def check_reminders(self):
            pass

    now = utc_now()
    ctx = make_ctx(tmp_path)
    cfg = add_conn(ctx.db, "srv", retries=0)
    monkeypatch.setattr(serverless_module, "get_checker", lambda p: FakeChecker(down()))
    serverless_module.run_due_checks(ctx, now=now)

    ctx2 = make_ctx(tmp_path, alerter=SpyAlerter())
    monkeypatch.setattr(serverless_module, "get_checker", lambda p: FakeChecker(up()))
    serverless_module.run_due_checks(ctx2, now=now + timedelta(seconds=200))
    assert ctx2.db.list_open_incidents() == []
    closed = ctx2.db.list_incidents(cfg.id)[0]
    assert closed["duration_s"] == pytest.approx(200.0, abs=0.01)
    assert len(events_seen) == 1


def test_tracker_rebuilds_unconfirmed_streak_after_restart(tmp_path):
    """Reinicio a mitad de racha no confirmada: los fallos previos no se pierden."""
    db = Database(tmp_path / "streak.db")
    cfg = add_conn(db, "srv", retries=2)  # necesita 3 fallos
    now = utc_now()

    tracker1 = IncidentTracker(db)
    tracker1.record(cfg, up(), now - timedelta(minutes=3))
    tracker1.record(cfg, down(), now - timedelta(minutes=2))
    tracker1.record(cfg, down(), now - timedelta(minutes=1))
    assert db.list_open_incidents() == []

    tracker2 = IncidentTracker(db)  # reinicio
    events = tracker2.record(cfg, down(), now)
    assert len(events) == 1 and isinstance(events[0], IncidentOpened)
    # started_at = primer fallo de la racha, reconstruido desde la BD
    assert events[0].started_at == (now - timedelta(minutes=2)).replace(
        microsecond=(now - timedelta(minutes=2)).microsecond // 1000 * 1000
    )
    # y la histéresis visible se mantiene: antes de confirmar mostraba UP
    assert tracker2.status_of(cfg.id) is Status.DOWN  # ya confirmado ahora

"""Incident state machine tests: UP→DOWN→UP with hysteresis, backoff exponent
and restart recovery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import Database
from app.errors import ErrorType
from app.incidents import IncidentClosed, IncidentOpened, IncidentTracker
from app.models import CheckResult, ConnectionConfig, Protocol, Status

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def down(error_type: ErrorType = ErrorType.TCP_CONNECT) -> CheckResult:
    return CheckResult(
        status=Status.DOWN, latency_ms=None, error_type=error_type,
        error_msg="conexión rechazada por el servidor",
    )


def up(latency: float = 12.0) -> CheckResult:
    return CheckResult(status=Status.UP, latency_ms=latency)


def degraded() -> CheckResult:
    return CheckResult(
        status=Status.DEGRADED, latency_ms=900.0, error_type=ErrorType.TARGET_MISSING,
        error_msg="objetivo '/x': la ruta no existe",
    )


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture()
def cfg(db: Database) -> ConnectionConfig:
    connection = ConnectionConfig(
        id=None, name="srv-acme", client="ACME", protocol=Protocol.SFTP,
        host="sftp.acme.local", port=22, retries=2,  # DOWN confirmed at 3rd failure
    )
    db.create_connection(connection)
    return connection


def test_single_failure_does_not_open_incident(db, cfg):
    tracker = IncidentTracker(db)
    assert tracker.record(cfg, up(), at(0)) == []
    assert tracker.record(cfg, down(), at(1)) == []
    assert tracker.record(cfg, up(), at(2)) == []
    assert db.list_incidents() == []
    # hysteresis: the unconfirmed failure never flipped the visible status
    assert tracker.status_of(cfg.id) is Status.UP


def test_incident_opens_after_retries_exhausted_and_closes_on_recovery(db, cfg):
    tracker = IncidentTracker(db)
    assert tracker.record(cfg, down(), at(0)) == []
    assert tracker.record(cfg, down(), at(1)) == []
    events = tracker.record(cfg, down(), at(2))

    assert len(events) == 1
    opened = events[0]
    assert isinstance(opened, IncidentOpened)
    assert opened.started_at == at(0)  # downtime counted from the FIRST failure
    assert opened.error_type == ErrorType.TCP_CONNECT.value
    assert tracker.status_of(cfg.id) is Status.DOWN
    assert tracker.is_confirmed_down(cfg.id)

    events = tracker.record(cfg, up(), at(10))
    assert len(events) == 1
    closed = events[0]
    assert isinstance(closed, IncidentClosed)
    assert closed.incident_id == opened.incident_id
    assert closed.duration_s == 600.0  # at(10) - at(0)

    rows = db.list_incidents(cfg.id)
    assert len(rows) == 1
    assert rows[0]["ended_at"] is not None
    assert rows[0]["duration_s"] == 600.0
    assert tracker.status_of(cfg.id) is Status.UP


def test_only_one_incident_per_outage(db, cfg):
    tracker = IncidentTracker(db)
    for minute in range(6):  # keeps failing well past confirmation
        tracker.record(cfg, down(), at(minute))
    assert len(db.list_incidents(cfg.id)) == 1


def test_degraded_does_not_open_but_does_close_incidents(db, cfg):
    tracker = IncidentTracker(db)
    for minute in range(5):
        assert tracker.record(cfg, degraded(), at(minute)) == []
    assert db.list_incidents(cfg.id) == []

    # DEGRADED counts as success for incident purposes (it authenticated)
    for minute in range(5, 8):
        tracker.record(cfg, down(), at(minute))
    events = tracker.record(cfg, degraded(), at(9))
    assert len(events) == 1 and isinstance(events[0], IncidentClosed)


def test_backoff_exponent_counts_from_confirmation(db, cfg):
    tracker = IncidentTracker(db)
    assert tracker.failures_since_confirm(cfg.id) == 0
    tracker.record(cfg, down(), at(0))
    tracker.record(cfg, down(), at(1))
    assert tracker.failures_since_confirm(cfg.id) == 0  # not confirmed yet
    tracker.record(cfg, down(), at(2))  # confirmation
    assert tracker.failures_since_confirm(cfg.id) == 1
    tracker.record(cfg, down(), at(4))
    assert tracker.failures_since_confirm(cfg.id) == 2
    tracker.record(cfg, up(), at(8))
    assert tracker.failures_since_confirm(cfg.id) == 0  # reset on recovery


def test_tracker_recovers_open_incident_after_restart(db, cfg):
    tracker = IncidentTracker(db)
    for minute in range(3):
        tracker.record(cfg, down(), at(minute))
    assert len(db.list_open_incidents()) == 1

    restarted = IncidentTracker(db)  # simulates an app restart mid-outage
    assert restarted.is_confirmed_down(cfg.id)
    events = restarted.record(cfg, up(), at(30))
    assert len(events) == 1
    closed = events[0]
    assert isinstance(closed, IncidentClosed)
    assert closed.duration_s == 1800.0  # duration measured from the original start
    assert db.list_open_incidents() == []
    assert len(db.list_incidents(cfg.id)) == 1


def test_every_check_is_persisted_with_cause(db, cfg):
    tracker = IncidentTracker(db)
    tracker.record(cfg, down(ErrorType.DNS), at(0))
    tracker.record(cfg, up(latency=33.3), at(1))
    rows = db.list_checks(cfg.id)
    assert len(rows) == 2
    assert rows[0]["status"] == "DOWN"
    assert rows[0]["error_type"] == "dns"
    assert rows[0]["latency_ms"] is None  # latency only stored for successful checks
    assert rows[1]["status"] == "UP"
    assert rows[1]["latency_ms"] == 33.3

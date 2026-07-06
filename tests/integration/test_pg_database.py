"""PgDatabase contract tests against a real PostgreSQL (same behavior as SQLite).

Requires MONITOR_IT=1 and a local Postgres:

    docker run -d --name it-pgstore -e POSTGRES_USER=monitor -e POSTGRES_PASSWORD=s3cret \
        -e POSTGRES_DB=monitorstore -p 55440:5432 postgres:16-alpine
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from app.db_pg import PgDatabase
from app.errors import ErrorType
from app.incidents import IncidentClosed, IncidentOpened, IncidentTracker
from app.models import CheckResult, ConnectionConfig, Protocol, Status
from app.util import to_iso, utc_now

pytestmark = pytest.mark.skipif(
    os.environ.get("MONITOR_IT") != "1",
    reason="integración: requiere Postgres local (MONITOR_IT=1)",
)

DSN = os.environ.get(
    "MONITOR_IT_PGSTORE_DSN",
    "postgresql://monitor:s3cret@127.0.0.1:55440/monitorstore?sslmode=disable",
)


@pytest.fixture()
def db():
    database = PgDatabase(DSN)
    # aislar cada test
    for stmt in ("DELETE FROM alerts_log", "DELETE FROM incidents",
                 "DELETE FROM checks", "DELETE FROM connections",
                 "DELETE FROM settings"):
        database._write(stmt)
    yield database
    database.close()


def make_cfg(**overrides) -> ConnectionConfig:
    base = dict(
        id=None, name="pg-conn", client="ACME", protocol=Protocol.SFTP,
        host="h.lan", port=22, targets=["/in"], degraded_ms=500, notes="ñ",
    )
    base.update(overrides)
    return ConnectionConfig(**base)


def test_connection_crud_roundtrip(db):
    cfg = make_cfg()
    cid = db.create_connection(cfg)
    assert isinstance(cid, int) and cid > 0

    loaded = db.get_connection(cid)
    assert loaded is not None
    assert loaded.protocol is Protocol.SFTP
    assert loaded.targets == ["/in"]
    assert loaded.degraded_ms == 500
    assert loaded.notes == "ñ"
    assert loaded.enabled is True and loaded.write_check is False

    loaded.port = 2222
    db.update_connection(loaded)
    assert db.get_connection(cid).port == 2222

    assert len(db.list_connections()) == 1
    db.delete_connection(cid)
    assert db.get_connection(cid) is None


def test_checks_aggregates_and_escape_hatch(db):
    cfg = make_cfg()
    cid = db.create_connection(cfg)
    now = utc_now()
    db.insert_checks_bulk([
        (cid, to_iso(now - timedelta(minutes=3)), "UP", 10.0, None, ""),
        (cid, to_iso(now - timedelta(minutes=2)), "UP", 20.0, None, ""),
        (cid, to_iso(now - timedelta(minutes=1)), "DOWN", None, "tcp_connect", "boom"),
    ])
    since = to_iso(now - timedelta(hours=1))

    assert db.uptime_counts(since)[cid] == (2, 3)
    assert db.avg_latencies(since)[cid] == pytest.approx(15.0)
    latest = db.latest_checks()[cid]
    assert latest["status"] == "DOWN" and latest["error_type"] == "tcp_connect"
    assert len(db.list_checks(cid, since)) == 3
    recent = db.list_recent_checks(cid, limit=2)
    assert [r["status"] for r in recent] == ["DOWN", "UP"]

    # escape hatch con placeholders estilo sqlite
    rows = db.execute(
        "SELECT status, latency_ms FROM checks WHERE connection_id = ? AND ts_utc >= ? "
        "ORDER BY ts_utc", (cid, since),
    ).fetchall()
    assert [r["status"] for r in rows] == ["UP", "UP", "DOWN"]

    assert db.purge_old_checks(to_iso(now)) == 3


def test_settings_upsert(db):
    assert db.get_setting("x", "def") == "def"
    db.set_setting("x", "1")
    db.set_setting("x", "2")
    assert db.get_setting("x") == "2"


def test_incident_state_machine_runs_on_postgres(db):
    cfg = make_cfg(retries=1)
    db.create_connection(cfg)
    tracker = IncidentTracker(db)
    now = utc_now()
    down = CheckResult(status=Status.DOWN, latency_ms=None,
                       error_type=ErrorType.TCP_CONNECT, error_msg="x")
    up = CheckResult(status=Status.UP, latency_ms=9.0)

    assert tracker.record(cfg, down, now) == []
    events = tracker.record(cfg, down, now + timedelta(minutes=1))
    assert len(events) == 1 and isinstance(events[0], IncidentOpened)
    assert len(db.list_open_incidents()) == 1

    # reinicio de proceso: el tracker se reconstruye desde Postgres
    tracker2 = IncidentTracker(db)
    assert tracker2.is_confirmed_down(cfg.id)
    events = tracker2.record(cfg, up, now + timedelta(minutes=10))
    assert len(events) == 1 and isinstance(events[0], IncidentClosed)
    assert events[0].duration_s == pytest.approx(600.0, abs=0.01)
    assert db.list_open_incidents() == []

    db.log_alert(events[0].incident_id, "log", ok=True)
    assert db.execute("SELECT COUNT(*) AS n FROM alerts_log").fetchone()["n"] == 1

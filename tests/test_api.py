"""Dashboard API tests: CRUD, secrets handling, test-connection, overview, auth."""
from __future__ import annotations

import random
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.checkers as checkers_module
from app.db import Database
from app.errors import ErrorType
from app.incidents import IncidentTracker
from app.main import AppContext, create_app
from app.models import CheckResult, Status, TargetResult
from app.scheduler import compute_next_delay
from app.settings_store import DashboardAuth
from app.throttle import CourtesyPolicy, Throttle
from app.util import to_iso, utc_now


class PlainSecretStore:
    """Reversible fake store so tests can assert what got stored."""

    def encrypt(self, plain: str) -> str:
        return "plain:" + plain

    def decrypt(self, token: str) -> str:
        assert token.startswith("plain:")
        return token[len("plain:"):]


def make_client(tmp_path, auth: DashboardAuth | None = None):
    db = Database(tmp_path / "api.db")
    ctx = AppContext(
        db=db,
        tracker=IncidentTracker(db),
        throttle=Throttle(CourtesyPolicy(host_spacing_s=0.0)),
        engine=None,
        secret_store=PlainSecretStore(),
        auth=auth or DashboardAuth(enabled=False),
        mode="dev",
    )
    return TestClient(create_app(ctx)), ctx


PAYLOAD = {
    "name": "SFTP Acme",
    "client": "ACME",
    "protocol": "SFTP",
    "host": "sftp.acme.local",
    "port": None,
    "username": "monitor",
    "secret": "hunter2",
    "targets": ["/entrada"],
    "interval_s": 60,
    "timeout_s": 10,
    "retries": 2,
}


def test_crud_roundtrip_and_secret_semantics(tmp_path):
    client, ctx = make_client(tmp_path)

    created = client.post("/api/connections", json=PAYLOAD)
    assert created.status_code == 201, created.text
    body = created.json()
    cid = body["id"]
    assert body["port"] == 22  # default por protocolo
    assert body["has_secret"] is True
    assert "secret" not in body and "secret_encrypted" not in body

    stored = ctx.db.get_connection(cid)
    assert stored.secret_encrypted == "plain:hunter2"  # cifrado vía store

    # update sin tocar el secreto (secret=None → se conserva)
    update = dict(PAYLOAD, secret=None, name="SFTP Acme 2")
    resp = client.put(f"/api/connections/{cid}", json=update)
    assert resp.status_code == 200
    assert resp.json()["name"] == "SFTP Acme 2"
    assert ctx.db.get_connection(cid).secret_encrypted == "plain:hunter2"

    # update reemplazando el secreto
    resp = client.put(f"/api/connections/{cid}", json=dict(PAYLOAD, secret="nueva"))
    assert resp.status_code == 200
    assert ctx.db.get_connection(cid).secret_encrypted == "plain:nueva"

    assert len(client.get("/api/connections").json()) == 1
    assert client.delete(f"/api/connections/{cid}").status_code == 204
    assert client.get(f"/api/connections/{cid}").status_code == 404


def test_validation_errors_are_spanish_and_422(tmp_path):
    client, _ = make_client(tmp_path)
    bad = dict(PAYLOAD, host="", interval_s=5)
    resp = client.post("/api/connections", json=bad)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("host" in e.lower() for e in detail)
    assert any("intervalo" in e.lower() for e in detail)


def test_duplicate_starts_paused_and_keeps_secret(tmp_path):
    client, _ = make_client(tmp_path)
    cid = client.post("/api/connections", json=PAYLOAD).json()["id"]
    dup = client.post(f"/api/connections/{cid}/duplicate")
    assert dup.status_code == 201
    body = dup.json()
    assert body["name"].endswith("(copia)")
    assert body["enabled"] is False
    assert body["has_secret"] is True


def test_toggle_pauses_and_resumes(tmp_path):
    client, _ = make_client(tmp_path)
    cid = client.post("/api/connections", json=PAYLOAD).json()["id"]
    assert client.post(f"/api/connections/{cid}/toggle").json()["enabled"] is False
    assert client.post(f"/api/connections/{cid}/toggle").json()["enabled"] is True


def test_test_connection_endpoint_uses_checker_and_reports_detail(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path)
    seen = {}

    class FakeChecker:
        def check(self, cfg, secret):
            seen["host"] = cfg.host
            seen["secret"] = secret
            return CheckResult(
                status=Status.DEGRADED,
                latency_ms=42.0,
                error_type=ErrorType.TARGET_MISSING,
                error_msg="objetivo '/x': la ruta no existe",
                targets=[TargetResult("/x", False, ErrorType.TARGET_MISSING, "la ruta no existe")],
            )

    monkeypatch.setattr(checkers_module, "get_checker", lambda protocol: FakeChecker())
    resp = client.post("/api/connections/test", json=PAYLOAD)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "DEGRADED"
    assert body["targets"][0]["error_type"] == "target_missing"
    assert seen == {"host": "sftp.acme.local", "secret": "hunter2"}


def test_test_connection_reuses_stored_secret(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path)
    cid = client.post("/api/connections", json=PAYLOAD).json()["id"]
    seen = {}

    class FakeChecker:
        def check(self, cfg, secret):
            seen["secret"] = secret
            return CheckResult(status=Status.UP, latency_ms=5.0)

    monkeypatch.setattr(checkers_module, "get_checker", lambda protocol: FakeChecker())
    resp = client.post("/api/connections/test", json=dict(PAYLOAD, id=cid, secret=None))
    assert resp.status_code == 200
    assert seen["secret"] == "hunter2"  # descifrado del guardado


def test_overview_reports_status_uptime_and_open_incidents(tmp_path):
    client, ctx = make_client(tmp_path)
    cid = client.post("/api/connections", json=PAYLOAD).json()["id"]
    cfg = ctx.db.get_connection(cid)
    now = utc_now()
    up = CheckResult(status=Status.UP, latency_ms=10.0)
    down = CheckResult(
        status=Status.DOWN, latency_ms=None,
        error_type=ErrorType.TCP_CONNECT, error_msg="rechazada",
    )
    for minutes, result in [(50, up), (40, up), (30, down), (20, down), (10, down)]:
        ctx.tracker.record(cfg, result, now - timedelta(minutes=minutes))

    body = client.get("/api/overview").json()
    card = body["connections"][0]
    assert card["status"] == "DOWN"
    assert card["uptime"]["h24"] == 40.0  # 2 de 5
    assert card["open_incident"] is not None
    assert card["open_incident"]["error_type"] == "tcp_connect"
    assert body["clients"] == ["ACME"]

    history = client.get(f"/api/connections/{cid}/history?hours=24").json()
    assert len(history["checks"]) == 5
    assert len(history["incidents"]) == 1


def test_basic_auth_required_when_enabled(tmp_path):
    auth = DashboardAuth(enabled=True, username="admin", password="clave")
    client, _ = make_client(tmp_path, auth=auth)

    assert client.get("/").status_code == 401
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", auth=("admin", "mala")).status_code == 401
    assert client.get("/api/overview", auth=("admin", "clave")).status_code == 200
    assert client.get("/", auth=("admin", "clave")).status_code == 200
    # healthz queda abierto para el healthcheck de Docker
    assert client.get("/healthz").status_code == 200


def test_healthz_ok_without_engine(tmp_path):
    client, _ = make_client(tmp_path)
    body = client.get("/healthz")
    assert body.status_code == 200
    assert body.json()["status"] == "ok"


# --- scheduler delay computation ------------------------------------------------


def test_compute_next_delay_normal_and_backoff(tmp_path):
    db = Database(tmp_path / "sched.db")
    tracker = IncidentTracker(db)
    throttle = Throttle(CourtesyPolicy(backoff_cap_s=300.0, jitter_ratio=0.10))
    from app.models import ConnectionConfig, Protocol

    cfg = ConnectionConfig(
        id=None, name="s", client="", protocol=Protocol.FTP,
        host="h", port=21, interval_s=60, retries=1,
    )
    db.create_connection(cfg)
    rng = random.Random(7)

    # sin fallos: intervalo con jitter ±10 %
    delay = compute_next_delay(cfg, tracker, throttle, rng)
    assert 54.0 <= delay <= 66.0

    down = CheckResult(
        status=Status.DOWN, latency_ms=None,
        error_type=ErrorType.TCP_CONNECT, error_msg="x",
    )
    tracker.record(cfg, down)  # 1º fallo: aún no confirmado
    assert 54.0 <= compute_next_delay(cfg, tracker, throttle, rng) <= 66.0
    tracker.record(cfg, down)  # confirmado (retries=1 → 2 fallos)
    delay = compute_next_delay(cfg, tracker, throttle, rng)
    assert 108.0 <= delay <= 132.0  # 60×2 con jitter
    tracker.record(cfg, down)
    delay = compute_next_delay(cfg, tracker, throttle, rng)
    assert 216.0 <= delay <= 264.0  # 60×4 con jitter
    for _ in range(5):
        tracker.record(cfg, down)
    delay = compute_next_delay(cfg, tracker, throttle, rng)
    assert delay <= 330.0  # tope 300 s + jitter

    tracker.record(cfg, CheckResult(status=Status.UP, latency_ms=1.0))
    assert 54.0 <= compute_next_delay(cfg, tracker, throttle, rng) <= 66.0  # reset

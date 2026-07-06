"""Demo seeding and config backup/restore tests (Fase 6)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.demo import seed_demo
from app.incidents import IncidentTracker
from app.main import AppContext, create_app
from app.settings_store import DashboardAuth
from app.throttle import CourtesyPolicy, Throttle
from app.util import from_iso

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)


def make_ctx(tmp_path) -> AppContext:
    db = Database(tmp_path / "f6.db")
    return AppContext(
        db=db, tracker=IncidentTracker(db), throttle=Throttle(CourtesyPolicy()),
        engine=None, secret_store=None, auth=DashboardAuth(enabled=False), mode="dev",
    )


def test_demo_seed_is_consistent_and_idempotent(tmp_path):
    db = Database(tmp_path / "demo.db")
    created = seed_demo(db, now=NOW)
    assert created == 6
    assert seed_demo(db, now=NOW) == 0  # idempotente

    connections = db.list_connections()
    assert len(connections) == 6
    assert all(not c.enabled for c in connections), "las demo nacen pausadas"
    assert {c.client for c in connections} == {"ACME", "Contoso"}
    protocols = {c.protocol.value for c in connections}
    assert {"SFTP", "WEBDAVS", "POSTGRES", "FTP", "MYSQL", "SQLSERVER"} == protocols

    incidents = db.list_incidents()
    assert len(incidents) == 7
    assert len(db.list_open_incidents()) == 1

    # ~30 días de historial y checks DOWN coherentes con las ventanas de incidente
    for cfg in connections:
        rows = db.list_checks(cfg.id)
        assert len(rows) > 4000
        for inc in db.list_incidents(cfg.id):
            if inc["ended_at"] is None:
                continue
            start, end = from_iso(inc["started_at"]), from_iso(inc["ended_at"])
            inside = [r for r in rows if start <= from_iso(r["ts_utc"]) < end]
            assert inside and all(r["status"] == "DOWN" for r in inside)
            assert all(r["error_type"] == inc["error_type"] for r in inside)


def test_backup_excludes_secrets_and_restore_creates_paused(tmp_path):
    ctx = make_ctx(tmp_path)

    class PlainStore:
        def encrypt(self, s):
            return "plain:" + s

        def decrypt(self, t):
            return t[len("plain:"):]

    ctx.secret_store = PlainStore()
    client = TestClient(create_app(ctx))

    client.post("/api/connections", json={
        "name": "SFTP X", "client": "ACME", "protocol": "SFTP", "host": "x.lan",
        "username": "u", "secret": "hunter2", "targets": ["/in"],
    })
    client.put("/api/settings", json={"branding.company": "Empresa SA", "retention.days": "180"})

    backup = client.get("/api/backup")
    assert backup.status_code == 200
    data = backup.json()
    assert data["app"] == "StabilityMonitor"
    assert "hunter2" not in backup.text and "secret" not in str(data["connections"][0]).lower()
    assert data["settings"]["branding.company"] == "Empresa SA"
    assert "smtp.password" not in data["settings"]

    # restaurar en una instalación limpia
    ctx2 = make_ctx(tmp_path / "otra")
    client2 = TestClient(create_app(ctx2))
    result = client2.post("/api/restore", json=data).json()
    assert result["connections_created"] == 1
    assert result["settings_applied"] >= 2
    assert "pausa" in result["warning"]

    restored = ctx2.db.list_connections()[0]
    assert restored.name == "SFTP X"
    assert restored.enabled is False  # sin secreto: pausada
    assert restored.secret_encrypted is None
    assert ctx2.db.get_setting("branding.company") == "Empresa SA"

    # re-importar el mismo backup no duplica
    result = client2.post("/api/restore", json=data).json()
    assert result["connections_created"] == 0
    assert result["connections_skipped"] == 1


def test_restore_rejects_foreign_files(tmp_path):
    client = TestClient(create_app(make_ctx(tmp_path)))
    resp = client.post("/api/restore", json={"app": "otra-cosa"})
    assert resp.status_code == 422

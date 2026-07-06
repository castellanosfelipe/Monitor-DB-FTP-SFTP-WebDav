"""Report tests: uptime/MTTR math (period clipping), self-contained HTML, CSV."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from app.db import Database
from app.models import ConnectionConfig, Protocol
from app.reports import (
    Branding,
    compute_period_stats,
    fmt_duration,
    generate_report,
    render_report,
)
from app.util import to_iso

DAY_S = 86400.0
START = datetime(2026, 3, 1, tzinfo=timezone.utc)
END = datetime(2026, 3, 8, tzinfo=timezone.utc)  # 7 días
NOW = datetime(2026, 3, 20, tzinfo=timezone.utc)


def add_conn(db, name="srv", client="ACME", interval=60) -> ConnectionConfig:
    cfg = ConnectionConfig(
        id=None, name=name, client=client, protocol=Protocol.SFTP,
        host=f"{name}.lan", port=22, interval_s=interval,
    )
    db.create_connection(cfg)
    return cfg


def add_incident(db, conn_id, start: datetime, end: datetime | None, error="tcp_connect"):
    incident_id = db.open_incident(conn_id, to_iso(start), error, "detalle")
    if end is not None:
        db.close_incident(incident_id, to_iso(end), (end - start).total_seconds())
    return incident_id


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "reports.db")


def test_uptime_100_without_incidents(db):
    add_conn(db)
    stats = compute_period_stats(db, "ACME", START, END, now=NOW)
    assert stats.uptime_pct == 100.0
    assert stats.downtime_s == 0.0
    assert stats.incident_count == 0
    assert stats.mttr_s is None
    assert len(stats.daily_uptime) == 7
    assert all(pct == 100.0 for _, pct in stats.daily_uptime)


def test_uptime_counts_only_the_overlap_with_the_period(db):
    cfg = add_conn(db)
    # 2 h totalmente dentro del período
    add_incident(db, cfg.id, START + timedelta(days=1), START + timedelta(days=1, hours=2))
    # empieza ANTES del período y termina 1 h dentro → solo cuenta 1 h
    add_incident(db, cfg.id, START - timedelta(hours=5), START + timedelta(hours=1))
    # completamente fuera → no cuenta
    add_incident(db, cfg.id, START - timedelta(days=3), START - timedelta(days=2))

    stats = compute_period_stats(db, "ACME", START, END, now=NOW)
    assert stats.downtime_s == pytest.approx(3 * 3600)
    expected = 100.0 * (1 - (3 * 3600) / (7 * DAY_S))
    assert stats.uptime_pct == pytest.approx(expected)
    assert stats.incident_count == 2  # el de fuera no aparece

    # el día 1 perdió 2 h → 100·(1 − 2/24)
    day1 = stats.daily_uptime[1][1]
    assert day1 == pytest.approx(100.0 * (1 - 2 / 24))


def test_open_incident_counts_until_period_end(db):
    cfg = add_conn(db)
    add_incident(db, cfg.id, END - timedelta(hours=3), None)  # sigue abierto
    stats = compute_period_stats(db, "ACME", START, END, now=NOW)
    assert stats.downtime_s == pytest.approx(3 * 3600)
    assert stats.incidents[0].ended_at is None


def test_uptime_averages_across_connections(db):
    c1 = add_conn(db, "srv1")
    add_conn(db, "srv2")
    # solo srv1 cae 7 h; con 2 conexiones el downtime pondera a la mitad
    add_incident(db, c1.id, START, START + timedelta(hours=7))
    stats = compute_period_stats(db, "ACME", START, END, now=NOW)
    expected = 100.0 * (1 - (7 * 3600) / (7 * DAY_S * 2))
    assert stats.uptime_pct == pytest.approx(expected)
    per = {c.name: c for c in stats.connections}
    assert per["srv1"].uptime_pct == pytest.approx(100.0 * (1 - 1 / 24 / 7 * 7))
    assert per["srv2"].uptime_pct == 100.0


def test_mttr_is_mean_of_closed_incidents(db):
    cfg = add_conn(db)
    add_incident(db, cfg.id, START + timedelta(days=1), START + timedelta(days=1, minutes=10))
    add_incident(db, cfg.id, START + timedelta(days=2), START + timedelta(days=2, minutes=30))
    stats = compute_period_stats(db, "ACME", START, END, now=NOW)
    assert stats.mttr_s == pytest.approx(20 * 60)


def test_unknown_client_raises(db):
    add_conn(db, client="OTRA")
    with pytest.raises(ValueError, match="ACME"):
        compute_period_stats(db, "ACME", START, END, now=NOW)


def test_report_html_is_self_contained_and_complete(db, tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_DATA_DIR", str(tmp_path))
    cfg = add_conn(db)
    add_incident(db, cfg.id, START + timedelta(days=2), START + timedelta(days=2, hours=1))
    # historial de latencia para la gráfica
    for hour in range(0, 48, 6):
        db.insert_check(cfg.id, to_iso(START + timedelta(hours=hour)), "UP", 25.0 + hour, None, "")

    branding = Branding(company="Empresa Demo SA", accent="#0055aa", logo_b64="")
    path = generate_report(db, "ACME", date(2026, 3, 1), date(2026, 3, 7), branding, now=NOW)
    assert path.name == "reporte_acme_20260301_20260307.html"
    html = path.read_text(encoding="utf-8")

    # un solo archivo sin recursos remotos (criterio de aceptación)
    assert not re.search(r'(src|href)\s*=\s*"https?://', html)
    assert "<svg" in html and html.count("<svg") == 2
    assert "Empresa Demo SA" in html
    assert "Disponibilidad" in html and "MTTR" in html
    assert "conexión rechazada" in html  # causa traducida
    assert "backoff" in html  # metodología en el pie (RF-6)
    assert "99.4" in html  # uptime: 1h en 7d → 99.40%


def test_report_comparison_against_previous_period(db):
    cfg = add_conn(db)
    # período anterior: 10 h de caída; actual: 1 h → mejora
    add_incident(db, cfg.id, START - timedelta(days=5), START - timedelta(days=5) + timedelta(hours=10))
    add_incident(db, cfg.id, START + timedelta(days=1), START + timedelta(days=1, hours=1))
    current = compute_period_stats(db, "ACME", START, END, now=NOW)
    previous = compute_period_stats(db, "ACME", START - timedelta(days=7), START, now=NOW)
    html = render_report(current, previous, Branding())
    assert "vs período anterior" in html
    assert "Período anterior" in html  # leyenda de la gráfica de latencia


def test_fmt_duration():
    assert fmt_duration(50) == "50 s"
    assert fmt_duration(600) == "10.0 min"
    assert fmt_duration(None) == "—"


def test_csv_exports(tmp_path):
    from fastapi.testclient import TestClient

    from app.incidents import IncidentTracker
    from app.main import AppContext, create_app
    from app.settings_store import DashboardAuth
    from app.throttle import CourtesyPolicy, Throttle

    from app.util import utc_now

    real_now = utc_now()  # el filtro days= usa la hora real, no la simulada
    db = Database(tmp_path / "csv.db")
    cfg = add_conn(db, "srv-csv")
    db.insert_check(cfg.id, to_iso(real_now - timedelta(hours=1)), "UP", 12.5, None, "")
    db.insert_check(cfg.id, to_iso(real_now - timedelta(minutes=30)), "DOWN", None, "tcp_connect", "boom")
    add_incident(db, cfg.id, real_now - timedelta(minutes=30), real_now - timedelta(minutes=10))

    ctx = AppContext(
        db=db, tracker=IncidentTracker(db), throttle=Throttle(CourtesyPolicy()),
        engine=None, secret_store=None, auth=DashboardAuth(enabled=False), mode="dev",
    )
    client = TestClient(create_app(ctx))

    resp = client.get(f"/api/export/checks.csv?connection_id={cfg.id}&days=30")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("id,connection_id,connection,ts_utc,status")
    assert len(lines) == 3
    assert "srv-csv" in lines[1] and "tcp_connect" in lines[2]

    resp = client.get("/api/export/incidents.csv")
    lines = resp.text.strip().splitlines()
    assert len(lines) == 2
    assert "tcp_connect" in lines[1]

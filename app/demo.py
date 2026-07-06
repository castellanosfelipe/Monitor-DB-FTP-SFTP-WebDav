"""Demo mode (entregable 7): fictional connections + 30 days of synthetic history.

Seeds file-server and database connections for two clients, with realistic
latency curves, a few incidents (with matching DOWN checks) and one currently
open incident — enough to exercise the dashboard, charts and reports without
any real server. Demo connections are created *paused* so the scheduler never
probes the fictional hosts.
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta

from app.db import Database
from app.models import ConnectionConfig, Protocol
from app.util import to_iso, utc_now

logger = logging.getLogger(__name__)

DEMO_MARKER = "demo.seeded"
_CHECK_EVERY_S = 600  # un chequeo cada 10 min → ~4300 checks por conexión
_DAYS = 30

_CONNECTIONS = [
    ("SFTP Producción", "ACME", Protocol.SFTP, "sftp.acme.demo", 22, ["/clientes/acme/entrada"], None, 34.0),
    ("WebDAV Respaldos", "ACME", Protocol.WEBDAVS, "dav.acme.demo", 443, ["/backups"], None, 88.0),
    ("PostgreSQL Ventas", "ACME", Protocol.POSTGRES, "pg.acme.demo", 5432, ["public.pedidos"], "ventas", 12.0),
    ("FTP Intercambio", "Contoso", Protocol.FTP, "ftp.contoso.demo", 21, ["/entrada", "/salida"], None, 55.0),
    ("MySQL ERP", "Contoso", Protocol.MYSQL, "mysql.contoso.demo", 3306, ["erp.facturas"], "erp", 18.0),
    ("SQL Server BI", "Contoso", Protocol.SQLSERVER, "mssql.contoso.demo", 1433, ["dbo.ventas"], "bi", 26.0),
]

# (índice de conexión, hace_días, duración_h, causa, mensaje)
_INCIDENTS = [
    (0, 26, 1.5, "tcp_connect", "conexión rechazada por el servidor"),
    (2, 18, 0.6, "auth", "autenticación rechazada: password authentication failed"),
    (3, 12, 3.2, "tcp_timeout", "tiempo de espera agotado al conectar"),
    (4, 9, 0.4, "db_missing", "la base de datos no existe"),
    (0, 4, 2.1, "dns", "no se pudo resolver el nombre de host"),
    (5, 2, 0.8, "tcp_connect", "conexión rechazada por el servidor"),
]

_OPEN_INCIDENT = (1, 0.05, "tls", "certificado no válido: certificate has expired")  # abierto hace ~1 h


def is_seeded(db: Database) -> bool:
    return db.get_setting(DEMO_MARKER) == "1"


def seed_demo(db: Database, now: datetime | None = None) -> int:
    """Populate demo data; returns the number of connections created."""
    if is_seeded(db):
        logger.info("demo: los datos ya estaban sembrados")
        return 0
    now = now or utc_now()
    rng = random.Random(2026)
    start = now - timedelta(days=_DAYS)

    configs: list[ConnectionConfig] = []
    for name, client, protocol, host, port, targets, db_name, _ in _CONNECTIONS:
        cfg = ConnectionConfig(
            id=None, name=name, client=client, protocol=protocol, host=host, port=port,
            username="monitor", db_name=db_name, targets=targets,
            enabled=False,  # pausadas: hosts ficticios, el scheduler no debe sondearlos
            notes="Conexión de demostración (host ficticio).",
        )
        db.create_connection(cfg)
        configs.append(cfg)

    # Ventanas de caída por conexión: [(inicio, fin, causa, mensaje)]
    downtime: dict[int, list[tuple[datetime, datetime, str, str]]] = {i: [] for i in range(len(configs))}
    for index, days_ago, hours, error_type, message in _INCIDENTS:
        inc_start = now - timedelta(days=days_ago, minutes=rng.randint(0, 600))
        inc_end = inc_start + timedelta(hours=hours)
        incident_id = db.open_incident(configs[index].id, to_iso(inc_start), error_type, message)
        db.close_incident(incident_id, to_iso(inc_end), (inc_end - inc_start).total_seconds())
        downtime[index].append((inc_start, inc_end, error_type, message))
    open_index, open_days_ago, open_error, open_message = _OPEN_INCIDENT
    open_start = now - timedelta(days=open_days_ago)
    db.open_incident(configs[open_index].id, to_iso(open_start), open_error, open_message)
    downtime[open_index].append((open_start, now + timedelta(days=1), open_error, open_message))

    total_rows = 0
    for index, cfg in enumerate(configs):
        base_latency = _CONNECTIONS[index][7]
        rows: list[tuple[int, str, str, float | None, str | None, str]] = []
        ts = start
        step = 0
        while ts < now:
            window = next(
                ((s, e, et, msg) for s, e, et, msg in downtime[index] if s <= ts < e), None
            )
            if window is not None:
                rows.append((cfg.id, to_iso(ts), "DOWN", None, window[2], window[3]))
            else:
                # curva diaria + ruido + degradación ocasional por latencia
                hour = ts.hour + ts.minute / 60
                daily = 1.0 + 0.35 * math.sin((hour - 14) / 24 * 2 * math.pi)
                latency = max(3.0, rng.gauss(base_latency * daily, base_latency * 0.12))
                if rng.random() < 0.004:
                    latency *= rng.uniform(4, 8)
                    rows.append((cfg.id, to_iso(ts), "DEGRADED", round(latency, 1), "latency",
                                 f"latencia {latency:.0f} ms supera el umbral"))
                else:
                    rows.append((cfg.id, to_iso(ts), "UP", round(latency, 1), None, ""))
            ts += timedelta(seconds=_CHECK_EVERY_S)
            step += 1
        db.insert_checks_bulk(rows)
        total_rows += len(rows)

    db.set_setting(DEMO_MARKER, "1")
    logger.info("demo: %d conexiones y %d checks sintéticos (30 días)", len(configs), total_rows)
    return len(configs)

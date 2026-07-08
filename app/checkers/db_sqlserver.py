"""SQL Server checker (python-tds, pure Python).

Identifiable traffic: ``appname`` is set to ``StabilityMonitor/x.y.z``
(visible in ``sys.dm_exec_sessions.program_name``).

TLS note: encryption is negotiated by the TDS protocol according to the
server's configuration; ``ssl_mode`` does not force it here (documented in
DECISIONS.md).
"""
from __future__ import annotations

from typing import Any

import pytds

from app import config
from app.checkers.db_base import DbChecker
from app.errors import ErrorType, truncate
from app.models import ConnectionConfig


class SqlServerChecker(DbChecker):
    validation_query = "SELECT 1"

    def _connect(self, cfg: ConnectionConfig, secret: str | None) -> Any:
        dsn = cfg.host
        port: int | None = cfg.port
        if cfg.sql_instance and cfg.port == 0:
            dsn = f"{cfg.host}\\{cfg.sql_instance}"
            port = None
        return pytds.connect(
            dsn=dsn,
            port=port,
            database=cfg.db_name or None,
            user=cfg.username,
            password=secret,
            login_timeout=cfg.timeout_s,
            timeout=cfg.timeout_s,
            appname=config.USER_AGENT,
            autocommit=True,
            pooling=False,  # one clean session per check, closed explicitly (RF-2)
        )

    def _classify(self, exc: Exception) -> tuple[ErrorType, str] | None:
        if isinstance(exc, pytds.TimeoutError):
            return ErrorType.TCP_TIMEOUT, "tiempo de espera agotado"
        if isinstance(exc, pytds.ClosedConnectionError):
            return ErrorType.TCP_CONNECT, "la conexión fue interrumpida"
        # pytds reports login problems as LoginError *or* plain OperationalError
        # depending on the code path, so classify by message on the whole family.
        if isinstance(exc, pytds.OperationalError):
            text = str(exc)
            lowered = text.lower()
            if "cannot open database" in lowered:
                return ErrorType.DB_MISSING, truncate(f"la base de datos no existe: {text}")
            if "login failed" in lowered or isinstance(exc, pytds.LoginError):
                return ErrorType.AUTH, truncate(f"autenticación rechazada: {text}")
        return None

    def _schema_exists(self, conn: Any, schema: str) -> bool:
        return self._exists(
            conn, "SELECT 1 FROM sys.schemas WHERE LOWER(name) = LOWER(%s)", (schema,)
        )

    def _table_exists(self, conn: Any, schema: str, table: str) -> bool:
        return self._exists(
            conn,
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
            "WHERE LOWER(TABLE_SCHEMA) = LOWER(%s) AND LOWER(TABLE_NAME) = LOWER(%s)",
            (schema, table),
        )

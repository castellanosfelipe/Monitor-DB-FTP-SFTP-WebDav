"""Oracle checker (python-oracledb in thin mode — no Instant Client).

Identifiable traffic: ``program`` is set to ``StabilityMonitor/x.y.z``
(visible in ``v$session.program``). Query timeouts use the public
``Connection.call_timeout`` API.
"""
from __future__ import annotations

import ssl
from contextlib import contextmanager
from typing import Any, Iterator

import oracledb

from app import config
from app.checkers.db_base import DbChecker
from app.errors import ErrorType
from app.models import ConnectionConfig

# ORA-/DPY- codes → cause. https://python-oracledb.readthedocs.io/en/latest/api_manual/exceptions.html
_CODE_MAP: dict[str, tuple[ErrorType, str]] = {
    "ORA-01017": (ErrorType.AUTH, "autenticación rechazada (usuario o contraseña)"),
    "ORA-01045": (ErrorType.PERMISSION, "el usuario no tiene privilegio CREATE SESSION"),
    "ORA-28000": (ErrorType.AUTH, "la cuenta está bloqueada"),
    "ORA-12514": (ErrorType.DB_MISSING, "el listener no conoce el service name"),
    "ORA-12505": (ErrorType.DB_MISSING, "el listener no conoce el SID"),
    "ORA-12541": (ErrorType.TCP_CONNECT, "no hay listener en el host/puerto"),
    "ORA-12170": (ErrorType.TCP_TIMEOUT, "tiempo de espera agotado al conectar"),
    "ORA-01031": (ErrorType.PERMISSION, "privilegios insuficientes"),
    "ORA-00942": (ErrorType.TARGET_MISSING, "la tabla o vista no existe"),
    # DPY-6xxx (connection errors) are classified by message sniffing below,
    # because thin mode buries the root cause (e.g. ORA-12514) in the text.
    "DPY-4011": (ErrorType.TCP_CONNECT, "la conexión fue cerrada por el servidor"),
    "DPY-4024": (ErrorType.QUERY_TIMEOUT, "la consulta excedió call_timeout"),
    "ORA-03156": (ErrorType.QUERY_TIMEOUT, "la consulta excedió el tiempo límite"),
    "DPY-3016": (
        ErrorType.PROTOCOL,
        "dependencia faltante en el ejecutable: cryptography no fue incluida; reinstala la V2 actualizada",
    ),
}


def _unverified_tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class OracleChecker(DbChecker):
    validation_query = "SELECT 1 FROM DUAL"

    def _connect(self, cfg: ConnectionConfig, secret: str | None) -> Any:
        kwargs: dict[str, Any] = dict(
            user=cfg.username,
            password=secret,
            host=cfg.host,
            port=cfg.port,
            service_name=cfg.db_name,
            tcp_connect_timeout=cfg.timeout_s,
            program=config.USER_AGENT,
        )
        if cfg.ssl_mode == "required":
            kwargs["protocol"] = "tcps"
            kwargs["ssl_context"] = _unverified_tls_context()
        conn = oracledb.connect(**kwargs)
        # Cap every round-trip (validation, catalog and health queries) — RF-2.
        conn.call_timeout = int(cfg.timeout_s * 1000)
        return conn

    def _classify(self, exc: Exception) -> tuple[ErrorType, str] | None:
        if isinstance(exc, oracledb.Error) and exc.args:
            detail = exc.args[0]
            full_code = getattr(detail, "full_code", None)
            if full_code:
                hit = _CODE_MAP.get(full_code)
                if hit is not None:
                    return hit
                if full_code.startswith("DPY-6"):
                    # Thin-mode wraps the root cause inside the message
                    # (e.g. DPY-6005 containing "Similar to ORA-12514").
                    lowered = str(getattr(detail, "message", "") or str(exc)).lower()
                    if "not registered with the listener" in lowered or "ora-12514" in lowered or "ora-12505" in lowered:
                        return ErrorType.DB_MISSING, "el listener no conoce el service name"
                    if "timed out" in lowered or "timeout" in lowered:
                        return ErrorType.TCP_TIMEOUT, "tiempo de espera agotado al conectar"
                    return ErrorType.TCP_CONNECT, f"error de conexión ({full_code})"
        return None

    def _schema_exists(self, conn: Any, schema: str) -> bool:
        return self._exists(
            conn, "SELECT 1 FROM all_users WHERE username = UPPER(:1)", (schema,)
        )

    def _table_exists(self, conn: Any, schema: str, table: str) -> bool:
        # all_tables covers tables the monitor user can see; views are exposed
        # via all_views — check both so a monitored "table" may be either.
        if self._exists(
            conn,
            "SELECT 1 FROM all_tables WHERE owner = UPPER(:1) AND table_name = UPPER(:2)",
            (schema, table),
        ):
            return True
        return self._exists(
            conn,
            "SELECT 1 FROM all_views WHERE owner = UPPER(:1) AND view_name = UPPER(:2)",
            (schema, table),
        )

    @contextmanager
    def _query_timeout(self, conn: Any, timeout_s: float) -> Iterator[None]:
        previous = conn.call_timeout
        conn.call_timeout = int(timeout_s * 1000)
        try:
            yield
        finally:
            try:
                conn.call_timeout = previous
            except Exception:
                pass

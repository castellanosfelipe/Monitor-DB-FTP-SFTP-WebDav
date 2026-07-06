"""PostgreSQL persistence (Neon) for serverless mode.

Same public contract as :class:`app.db.Database` (SQLite), so the rest of the
app is storage-agnostic: rows come back as plain dicts (``row["col"]``), the
``execute()`` escape hatch accepts the SQLite-style ``?`` placeholders used by
callers and translates them, and timestamps stay ISO-8601 TEXT so every
comparison and ``substr(ts_utc, 1, 10)`` aggregation works identically.
"""
from __future__ import annotations

import ssl
import threading
from typing import Any
from urllib.parse import unquote, urlparse

import pg8000.dbapi

from app import config
from app.models import ConnectionConfig
from app.util import to_iso, utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id               SERIAL PRIMARY KEY,
    name             TEXT    NOT NULL,
    client           TEXT    NOT NULL DEFAULT '',
    protocol         TEXT    NOT NULL,
    host             TEXT    NOT NULL,
    port             INTEGER NOT NULL,
    username         TEXT    NOT NULL DEFAULT '',
    secret_encrypted TEXT,
    auth_type        TEXT    NOT NULL DEFAULT 'password',
    key_path         TEXT,
    db_name          TEXT,
    ssl_mode         TEXT    NOT NULL DEFAULT 'preferred',
    targets_json     TEXT    NOT NULL DEFAULT '[]',
    health_query     TEXT,
    interval_s       INTEGER NOT NULL DEFAULT 60,
    timeout_s        DOUBLE PRECISION NOT NULL DEFAULT 10,
    retries          INTEGER NOT NULL DEFAULT 2,
    degraded_ms      INTEGER,
    write_check      INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    notes            TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id            BIGSERIAL PRIMARY KEY,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    ts_utc        TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    latency_ms    DOUBLE PRECISION,
    error_type    TEXT,
    error_msg     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_checks_conn_ts ON checks(connection_id, ts_utc);
CREATE TABLE IF NOT EXISTS incidents (
    id              SERIAL PRIMARY KEY,
    connection_id   INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    started_at      TEXT    NOT NULL,
    ended_at        TEXT,
    duration_s      DOUBLE PRECISION,
    error_type      TEXT,
    first_error_msg TEXT    NOT NULL DEFAULT '',
    acknowledged    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_incidents_conn_started ON incidents(connection_id, started_at);
CREATE TABLE IF NOT EXISTS alerts_log (
    id          SERIAL PRIMARY KEY,
    incident_id INTEGER REFERENCES incidents(id) ON DELETE CASCADE,
    channel     TEXT    NOT NULL,
    sent_at     TEXT    NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    detail      TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CONNECTION_COLUMNS = (
    "name, client, protocol, host, port, username, secret_encrypted, auth_type, "
    "key_path, db_name, ssl_mode, targets_json, health_query, interval_s, "
    "timeout_s, retries, degraded_ms, write_check, enabled, notes"
)


class _DictCursor:
    """Wrap a pg8000 cursor so rows come back as dicts (like sqlite3.Row)."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._columns = [d[0] for d in cursor.description] if cursor.description else []

    def _wrap(self, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(zip(self._columns, row))

    def fetchone(self) -> dict[str, Any] | None:
        return self._wrap(self._cursor.fetchone())

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(zip(self._columns, r)) for r in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class PgDatabase:
    """PostgreSQL twin of :class:`app.db.Database`."""

    def __init__(self, dsn: str) -> None:
        parsed = urlparse(dsn)
        if parsed.scheme not in ("postgres", "postgresql"):
            raise ValueError("DATABASE_URL debe ser una URL postgres:// (Neon).")
        self._params = dict(
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            host=parsed.hostname,
            port=parsed.port or 5432,
            database=parsed.path.lstrip("/") or "postgres",
        )
        # Neon exige TLS; verificación de certificados activada (CA pública).
        self._ssl_context = ssl.create_default_context()
        if "sslmode=disable" in (parsed.query or ""):
            self._ssl_context = None  # solo para Postgres locales de prueba
        self._local = threading.local()
        self.init_schema()

    # --- connection management ---------------------------------------------------

    def _conn(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = pg8000.dbapi.connect(
                **self._params,
                ssl_context=self._ssl_context,
                timeout=15,
                application_name=config.USER_AGENT,
            )
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> _DictCursor:
        cursor = self._conn().cursor()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        return _DictCursor(cursor)

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> _DictCursor:
        cursor = self._run(sql, params)
        self._conn().commit()
        return cursor

    def init_schema(self) -> None:
        cursor = self._conn().cursor()
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                cursor.execute(statement)
        self._conn().commit()

    # --- connections CRUD ----------------------------------------------------------

    def create_connection(self, cfg: ConnectionConfig) -> int:
        now = to_iso(utc_now())
        p = cfg.to_params()
        cursor = self._write(
            f"INSERT INTO connections ({_CONNECTION_COLUMNS}, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id",
            (
                p["name"], p["client"], p["protocol"], p["host"], p["port"], p["username"],
                p["secret_encrypted"], p["auth_type"], p["key_path"], p["db_name"],
                p["ssl_mode"], p["targets_json"], p["health_query"], p["interval_s"],
                p["timeout_s"], p["retries"], p["degraded_ms"], p["write_check"],
                p["enabled"], p["notes"], now, now,
            ),
        )
        cfg.id = int(cursor.fetchone()["id"])
        return cfg.id

    def update_connection(self, cfg: ConnectionConfig) -> None:
        if cfg.id is None:
            raise ValueError("cannot update a connection without id")
        p = cfg.to_params()
        assignments = ", ".join(f"{c.strip()} = %s" for c in _CONNECTION_COLUMNS.split(","))
        self._write(
            f"UPDATE connections SET {assignments}, updated_at = %s WHERE id = %s",
            (
                p["name"], p["client"], p["protocol"], p["host"], p["port"], p["username"],
                p["secret_encrypted"], p["auth_type"], p["key_path"], p["db_name"],
                p["ssl_mode"], p["targets_json"], p["health_query"], p["interval_s"],
                p["timeout_s"], p["retries"], p["degraded_ms"], p["write_check"],
                p["enabled"], p["notes"], to_iso(utc_now()), cfg.id,
            ),
        )

    def get_connection(self, connection_id: int) -> ConnectionConfig | None:
        row = self._run("SELECT * FROM connections WHERE id = %s", (connection_id,)).fetchone()
        return ConnectionConfig.from_row(row) if row else None  # type: ignore[arg-type]

    def list_connections(self, enabled_only: bool = False) -> list[ConnectionConfig]:
        sql = "SELECT * FROM connections"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY client, name"
        return [ConnectionConfig.from_row(r) for r in self._run(sql).fetchall()]  # type: ignore[arg-type]

    def delete_connection(self, connection_id: int) -> None:
        self._write("DELETE FROM connections WHERE id = %s", (connection_id,))

    # --- checks -----------------------------------------------------------------------

    def insert_check(
        self,
        connection_id: int,
        ts_utc: str,
        status: str,
        latency_ms: float | None,
        error_type: str | None,
        error_msg: str,
    ) -> int:
        cursor = self._write(
            "INSERT INTO checks (connection_id, ts_utc, status, latency_ms, error_type, error_msg) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (connection_id, ts_utc, status, latency_ms, error_type, error_msg),
        )
        return int(cursor.fetchone()["id"])

    def insert_checks_bulk(self, rows: list[tuple[int, str, str, float | None, str | None, str]]) -> None:
        cursor = self._conn().cursor()
        cursor.executemany(
            "INSERT INTO checks (connection_id, ts_utc, status, latency_ms, error_type, error_msg) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )
        self._conn().commit()

    def list_checks(self, connection_id: int, since_iso: str | None = None) -> list[dict[str, Any]]:
        if since_iso:
            cursor = self._run(
                "SELECT * FROM checks WHERE connection_id = %s AND ts_utc >= %s ORDER BY ts_utc",
                (connection_id, since_iso),
            )
        else:
            cursor = self._run(
                "SELECT * FROM checks WHERE connection_id = %s ORDER BY ts_utc", (connection_id,)
            )
        return cursor.fetchall()

    def list_recent_checks(self, connection_id: int, limit: int = 25) -> list[dict[str, Any]]:
        return self._run(
            "SELECT * FROM checks WHERE connection_id = %s ORDER BY id DESC LIMIT %s",
            (connection_id, limit),
        ).fetchall()

    def purge_old_checks(self, before_iso: str) -> int:
        cursor = self._write("DELETE FROM checks WHERE ts_utc < %s", (before_iso,))
        return cursor._cursor.rowcount

    def purge_old_incidents(self, before_iso: str) -> int:
        cursor = self._write(
            "DELETE FROM incidents WHERE ended_at IS NOT NULL AND ended_at < %s",
            (before_iso,),
        )
        return cursor._cursor.rowcount

    # --- dashboard aggregates ------------------------------------------------------------

    def uptime_counts(self, since_iso: str) -> dict[int, tuple[int, int]]:
        rows = self._run(
            "SELECT connection_id, COUNT(*) FILTER (WHERE status != 'DOWN') AS ok_count, "
            "COUNT(*) AS total FROM checks WHERE ts_utc >= %s GROUP BY connection_id",
            (since_iso,),
        )
        return {r["connection_id"]: (int(r["ok_count"]), int(r["total"])) for r in rows}

    def avg_latencies(self, since_iso: str) -> dict[int, float]:
        rows = self._run(
            "SELECT connection_id, AVG(latency_ms) AS avg_ms FROM checks "
            "WHERE ts_utc >= %s AND latency_ms IS NOT NULL GROUP BY connection_id",
            (since_iso,),
        )
        return {r["connection_id"]: float(r["avg_ms"]) for r in rows}

    def latest_checks(self) -> dict[int, dict[str, Any]]:
        rows = self._run(
            "SELECT ch.* FROM checks ch "
            "JOIN (SELECT connection_id, MAX(id) AS max_id FROM checks GROUP BY connection_id) last "
            "ON ch.id = last.max_id"
        )
        return {r["connection_id"]: r for r in rows}

    # --- incidents -----------------------------------------------------------------------

    def open_incident(
        self, connection_id: int, started_at: str, error_type: str | None, first_error_msg: str
    ) -> int:
        cursor = self._write(
            "INSERT INTO incidents (connection_id, started_at, error_type, first_error_msg) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (connection_id, started_at, error_type, first_error_msg),
        )
        return int(cursor.fetchone()["id"])

    def close_incident(self, incident_id: int, ended_at: str, duration_s: float) -> None:
        self._write(
            "UPDATE incidents SET ended_at = %s, duration_s = %s WHERE id = %s",
            (ended_at, duration_s, incident_id),
        )

    def list_open_incidents(self) -> list[dict[str, Any]]:
        return self._run("SELECT * FROM incidents WHERE ended_at IS NULL").fetchall()

    def list_incidents(self, connection_id: int | None = None) -> list[dict[str, Any]]:
        if connection_id is None:
            return self._run("SELECT * FROM incidents ORDER BY started_at").fetchall()
        return self._run(
            "SELECT * FROM incidents WHERE connection_id = %s ORDER BY started_at",
            (connection_id,),
        ).fetchall()

    # --- alerts / settings ------------------------------------------------------------------

    def log_alert(self, incident_id: int | None, channel: str, ok: bool, detail: str = "") -> int:
        cursor = self._write(
            "INSERT INTO alerts_log (incident_id, channel, sent_at, ok, detail) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (incident_id, channel, to_iso(utc_now()), int(ok), detail),
        )
        return int(cursor.fetchone()["id"])

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._run("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )

    # --- escape hatch --------------------------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _DictCursor:
        """Read-only queries written with SQLite-style ``?`` placeholders."""
        return self._run(sql.replace("?", "%s"), params)

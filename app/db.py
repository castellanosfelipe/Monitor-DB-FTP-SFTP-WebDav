"""SQLite persistence layer: WAL mode, simple sequential migrations, typed CRUD.

One ``Database`` object per process; each thread gets its own connection
(SQLite connections are not thread-safe) stored in a ``threading.local``.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.models import ConnectionConfig
from app.util import to_iso, utc_now

_SCHEMA_V1 = """
CREATE TABLE connections (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
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
    timeout_s        REAL    NOT NULL DEFAULT 10,
    retries          INTEGER NOT NULL DEFAULT 2,
    degraded_ms      INTEGER,
    write_check      INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    notes            TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE TABLE checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    ts_utc        TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    latency_ms    REAL,
    error_type    TEXT,
    error_msg     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX idx_checks_conn_ts ON checks(connection_id, ts_utc);

CREATE TABLE incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id   INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    started_at      TEXT    NOT NULL,
    ended_at        TEXT,
    duration_s      REAL,
    error_type      TEXT,
    first_error_msg TEXT    NOT NULL DEFAULT '',
    acknowledged    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_incidents_conn_started ON incidents(connection_id, started_at);

CREATE TABLE alerts_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER REFERENCES incidents(id) ON DELETE CASCADE,
    channel     TEXT    NOT NULL,
    sent_at     TEXT    NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    detail      TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SCHEMA_V2 = """
ALTER TABLE connections ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]';
"""

_SCHEMA_V3 = """
ALTER TABLE connections ADD COLUMN sql_instance TEXT;
"""

MIGRATIONS: list[str] = [_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3]

_CONNECTION_COLUMNS = (
    "name, client, protocol, host, port, username, secret_encrypted, auth_type, "
    "key_path, db_name, sql_instance, ssl_mode, targets_json, aliases_json, health_query, "
    "interval_s, timeout_s, retries, degraded_ms, write_check, enabled, notes"
)
_CONNECTION_PLACEHOLDERS = (
    ":name, :client, :protocol, :host, :port, :username, :secret_encrypted, :auth_type, "
    ":key_path, :db_name, :sql_instance, :ssl_mode, :targets_json, :aliases_json, :health_query, "
    ":interval_s, :timeout_s, :retries, :degraded_ms, :write_check, :enabled, :notes"
)


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.init_schema()

    # --- connection management ----------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close the current thread's connection (if any)."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def init_schema(self) -> None:
        conn = self._conn()
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version={version}")
            conn.commit()

    # --- connections CRUD ----------------------------------------------------

    def create_connection(self, cfg: ConnectionConfig) -> int:
        now = to_iso(utc_now())
        params = cfg.to_params() | {"created_at": now, "updated_at": now}
        cur = self._conn().execute(
            f"INSERT INTO connections ({_CONNECTION_COLUMNS}, created_at, updated_at) "
            f"VALUES ({_CONNECTION_PLACEHOLDERS}, :created_at, :updated_at)",
            params,
        )
        self._conn().commit()
        cfg.id = int(cur.lastrowid)  # type: ignore[arg-type]
        return cfg.id

    def update_connection(self, cfg: ConnectionConfig) -> None:
        if cfg.id is None:
            raise ValueError("cannot update a connection without id")
        assignments = ", ".join(
            f"{col.strip()} = :{col.strip()}" for col in _CONNECTION_COLUMNS.split(",")
        )
        params = cfg.to_params() | {"updated_at": to_iso(utc_now())}
        self._conn().execute(
            f"UPDATE connections SET {assignments}, updated_at = :updated_at WHERE id = :id",
            params,
        )
        self._conn().commit()

    def get_connection(self, connection_id: int) -> ConnectionConfig | None:
        row = self._conn().execute(
            "SELECT * FROM connections WHERE id = ?", (connection_id,)
        ).fetchone()
        return ConnectionConfig.from_row(row) if row else None

    def list_connections(self, enabled_only: bool = False) -> list[ConnectionConfig]:
        sql = "SELECT * FROM connections"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY client, name"
        return [ConnectionConfig.from_row(r) for r in self._conn().execute(sql)]

    def delete_connection(self, connection_id: int) -> None:
        self._conn().execute("DELETE FROM connections WHERE id = ?", (connection_id,))
        self._conn().commit()

    # --- checks ---------------------------------------------------------------

    def insert_check(
        self,
        connection_id: int,
        ts_utc: str,
        status: str,
        latency_ms: float | None,
        error_type: str | None,
        error_msg: str,
    ) -> int:
        cur = self._conn().execute(
            "INSERT INTO checks (connection_id, ts_utc, status, latency_ms, error_type, error_msg) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (connection_id, ts_utc, status, latency_ms, error_type, error_msg),
        )
        self._conn().commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def insert_checks_bulk(self, rows: list[tuple[int, str, str, float | None, str | None, str]]) -> None:
        """Bulk insert (connection_id, ts_utc, status, latency_ms, error_type, error_msg)."""
        self._conn().executemany(
            "INSERT INTO checks (connection_id, ts_utc, status, latency_ms, error_type, error_msg) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn().commit()

    def list_checks(self, connection_id: int, since_iso: str | None = None) -> list[sqlite3.Row]:
        if since_iso:
            cur = self._conn().execute(
                "SELECT * FROM checks WHERE connection_id = ? AND ts_utc >= ? ORDER BY ts_utc",
                (connection_id, since_iso),
            )
        else:
            cur = self._conn().execute(
                "SELECT * FROM checks WHERE connection_id = ? ORDER BY ts_utc", (connection_id,)
            )
        return list(cur)

    def list_recent_checks(self, connection_id: int, limit: int = 25) -> list[sqlite3.Row]:
        cur = self._conn().execute(
            "SELECT * FROM checks WHERE connection_id = ? ORDER BY id DESC LIMIT ?",
            (connection_id, limit),
        )
        return list(cur)

    def purge_old_checks(self, before_iso: str) -> int:
        cur = self._conn().execute("DELETE FROM checks WHERE ts_utc < ?", (before_iso,))
        self._conn().commit()
        return cur.rowcount

    def purge_old_incidents(self, before_iso: str) -> int:
        """Delete *closed* incidents older than the retention window."""
        cur = self._conn().execute(
            "DELETE FROM incidents WHERE ended_at IS NOT NULL AND ended_at < ?",
            (before_iso,),
        )
        self._conn().commit()
        return cur.rowcount

    # --- dashboard aggregates ---------------------------------------------------

    def uptime_counts(self, since_iso: str) -> dict[int, tuple[int, int]]:
        """Per connection: (non-DOWN checks, total checks) since ``since_iso``."""
        rows = self._conn().execute(
            "SELECT connection_id, SUM(status != 'DOWN') AS ok_count, COUNT(*) AS total "
            "FROM checks WHERE ts_utc >= ? GROUP BY connection_id",
            (since_iso,),
        )
        return {r["connection_id"]: (r["ok_count"], r["total"]) for r in rows}

    def avg_latencies(self, since_iso: str) -> dict[int, float]:
        rows = self._conn().execute(
            "SELECT connection_id, AVG(latency_ms) AS avg_ms FROM checks "
            "WHERE ts_utc >= ? AND latency_ms IS NOT NULL GROUP BY connection_id",
            (since_iso,),
        )
        return {r["connection_id"]: r["avg_ms"] for r in rows}

    def latest_checks(self) -> dict[int, sqlite3.Row]:
        """Most recent check per connection (ids grow with time)."""
        rows = self._conn().execute(
            "SELECT ch.* FROM checks ch "
            "JOIN (SELECT connection_id, MAX(id) AS max_id FROM checks GROUP BY connection_id) last "
            "ON ch.id = last.max_id"
        )
        return {r["connection_id"]: r for r in rows}

    # --- incidents -------------------------------------------------------------

    def open_incident(
        self, connection_id: int, started_at: str, error_type: str | None, first_error_msg: str
    ) -> int:
        cur = self._conn().execute(
            "INSERT INTO incidents (connection_id, started_at, error_type, first_error_msg) "
            "VALUES (?, ?, ?, ?)",
            (connection_id, started_at, error_type, first_error_msg),
        )
        self._conn().commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def close_incident(self, incident_id: int, ended_at: str, duration_s: float) -> None:
        self._conn().execute(
            "UPDATE incidents SET ended_at = ?, duration_s = ? WHERE id = ?",
            (ended_at, duration_s, incident_id),
        )
        self._conn().commit()

    def list_open_incidents(self) -> list[sqlite3.Row]:
        return list(self._conn().execute("SELECT * FROM incidents WHERE ended_at IS NULL"))

    def list_incidents(self, connection_id: int | None = None) -> list[sqlite3.Row]:
        if connection_id is None:
            cur = self._conn().execute("SELECT * FROM incidents ORDER BY started_at")
        else:
            cur = self._conn().execute(
                "SELECT * FROM incidents WHERE connection_id = ? ORDER BY started_at",
                (connection_id,),
            )
        return list(cur)

    # --- alerts log ---------------------------------------------------------------

    def log_alert(self, incident_id: int | None, channel: str, ok: bool, detail: str = "") -> int:
        cur = self._conn().execute(
            "INSERT INTO alerts_log (incident_id, channel, sent_at, ok, detail) VALUES (?, ?, ?, ?, ?)",
            (incident_id, channel, to_iso(utc_now()), int(ok), detail),
        )
        self._conn().commit()
        return int(cur.lastrowid)  # type: ignore[arg-type]

    # --- settings -------------------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn().execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn().commit()

    # --- misc ----------------------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Escape hatch for read-only queries (dashboard aggregations, tests)."""
        return self._conn().execute(sql, params)

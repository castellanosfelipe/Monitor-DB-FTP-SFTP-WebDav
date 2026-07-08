"""Database checker tests without live servers: driver error classification and
the common flow (validation query, targets, restricted health query)."""
from __future__ import annotations

from types import SimpleNamespace

import oracledb
import pg8000.exceptions
import pymysql
import pytds
import pytest

from app.checkers import get_checker
from app.checkers.db_base import HEALTH_LABEL, DbChecker
from app.checkers.db_mysql import MySqlChecker
from app.checkers.db_oracle import OracleChecker
from app.checkers.db_postgres import PostgresChecker
from app.checkers.db_sqlserver import SqlServerChecker
from app.errors import CheckError, ErrorType
from app.models import ConnectionConfig, Protocol, Status


def make_cfg(**overrides) -> ConnectionConfig:
    base = dict(
        id=None, name="db", client="ACME", protocol=Protocol.POSTGRES,
        host="db.local", port=5432, username="monitor", db_name="ventas",
    )
    base.update(overrides)
    return ConnectionConfig(**base)


# --- driver-specific classification -----------------------------------------


def test_postgres_sqlstate_classification():
    checker = PostgresChecker()
    cases = {
        "28P01": ErrorType.AUTH,
        "3D000": ErrorType.DB_MISSING,
        "42501": ErrorType.PERMISSION,
        "57014": ErrorType.QUERY_TIMEOUT,
        "08006": ErrorType.TCP_CONNECT,  # connection_exception family
    }
    for sqlstate, expected in cases.items():
        exc = pg8000.exceptions.DatabaseError({"C": sqlstate, "M": "detalle"})
        hit = checker._classify(exc)
        assert hit is not None and hit[0] is expected, sqlstate
    assert checker._classify(ValueError("x")) is None


def test_mysql_code_classification():
    checker = MySqlChecker()
    cases = {
        1045: ErrorType.AUTH,
        1049: ErrorType.DB_MISSING,
        1044: ErrorType.PERMISSION,
        2003: ErrorType.TCP_CONNECT,
        3024: ErrorType.QUERY_TIMEOUT,
    }
    for code, expected in cases.items():
        exc = pymysql.err.OperationalError(code, "detalle")
        hit = checker._classify(exc)
        assert hit is not None and hit[0] is expected, code


def test_sqlserver_classification():
    checker = SqlServerChecker()
    login_bad = pytds.LoginError("Login failed for user 'monitor'.")
    db_missing = pytds.LoginError('Cannot open database "ventas" requested by the login.')
    assert checker._classify(login_bad)[0] is ErrorType.AUTH
    assert checker._classify(db_missing)[0] is ErrorType.DB_MISSING
    assert checker._classify(pytds.TimeoutError())[0] is ErrorType.TCP_TIMEOUT
    assert checker._classify(pytds.ClosedConnectionError())[0] is ErrorType.TCP_CONNECT


def test_sqlserver_connects_by_instance_when_port_is_blank(monkeypatch):
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(pytds, "connect", fake_connect)
    cfg = make_cfg(
        protocol=Protocol.SQLSERVER,
        host="10.128.2.11",
        port=0,
        sql_instance="sigevas2022",
        db_name=None,
    )

    SqlServerChecker()._connect(cfg, "secret")

    assert captured["dsn"] == "10.128.2.11\\sigevas2022"
    assert captured["port"] is None
    assert captured["pooling"] is False


def test_sqlserver_explicit_port_takes_precedence_over_instance(monkeypatch):
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(pytds, "connect", fake_connect)
    cfg = make_cfg(
        protocol=Protocol.SQLSERVER,
        host="10.128.2.11",
        port=1433,
        sql_instance="sigevas2022",
    )

    SqlServerChecker()._connect(cfg, "secret")

    assert captured["dsn"] == "10.128.2.11"
    assert captured["port"] == 1433


def test_oracle_classification():
    checker = OracleChecker()
    cases = {
        "ORA-01017": ErrorType.AUTH,
        "ORA-12514": ErrorType.DB_MISSING,
        "ORA-12541": ErrorType.TCP_CONNECT,
        "ORA-01031": ErrorType.PERMISSION,
        "DPY-3016": ErrorType.PROTOCOL,
        "DPY-4024": ErrorType.QUERY_TIMEOUT,
        "DPY-6001": ErrorType.TCP_CONNECT,
    }
    for full_code, expected in cases.items():
        exc = oracledb.DatabaseError(SimpleNamespace(full_code=full_code, message="d"))
        hit = checker._classify(exc)
        assert hit is not None and hit[0] is expected, full_code
    exc = oracledb.DatabaseError(SimpleNamespace(full_code="DPY-3016", message="d"))
    assert "cryptography" in checker._classify(exc)[1]


# --- common flow via a fake driver -------------------------------------------


class FakeCursor:
    def __init__(self, connection: "FakeConn") -> None:
        self._connection = connection
        self._last_row = None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._connection.executed.append((sql, tuple(params)))
        self._last_row = self._connection.script(sql, tuple(params))

    def fetchone(self):
        self._connection.fetchone_calls += 1
        return self._last_row

    def close(self) -> None:
        pass


class FakeConn:
    def __init__(self, script) -> None:
        self.script = script  # (sql, params) -> row | None | raise
        self.executed: list[tuple[str, tuple]] = []
        self.fetchone_calls = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


class FakeDbChecker(DbChecker):
    """DbChecker over an in-memory fake driver, to test the shared flow."""

    def __init__(self, script) -> None:
        self.conn = FakeConn(script)
        self.timeouts_used: list[float] = []

    def _connect(self, cfg, secret):
        return self.conn

    def _classify(self, exc):
        return None

    def _schema_exists(self, conn, schema):
        return self._exists(conn, "FAKE_SCHEMA_QUERY", (schema,))

    def _table_exists(self, conn, schema, table):
        return self._exists(conn, "FAKE_TABLE_QUERY", (schema, table))

    def _query_timeout(self, conn, timeout_s):
        self.timeouts_used.append(timeout_s)
        return super()._query_timeout(conn, timeout_s)


def script_ok(sql: str, params: tuple):
    return (1,)  # everything exists, every query returns one row


def test_full_flow_up_and_connection_closed():
    checker = FakeDbChecker(script_ok)
    cfg = make_cfg(targets=["ventas", "ventas.pedidos"], health_query="SELECT 1")
    result = checker.check(cfg, "s3cret")
    assert result.status is Status.UP
    assert checker.conn.closed, "la conexión debe cerrarse siempre (RF-2)"
    # validation + schema + table + health = 4 queries, one fetchone each (máx. 1 fila)
    assert len(checker.conn.executed) == 4
    assert checker.conn.fetchone_calls == 4
    assert [t.ok for t in result.targets] == [True, True, True]


def test_missing_schema_and_table_are_degraded_not_down():
    def script(sql, params):
        if sql in ("FAKE_SCHEMA_QUERY", "FAKE_TABLE_QUERY"):
            return None  # not found
        return (1,)

    checker = FakeDbChecker(script)
    result = checker.check(make_cfg(targets=["noexiste"]), "s")
    assert result.status is Status.DEGRADED
    assert result.error_type is ErrorType.TARGET_MISSING
    assert "esquema" in result.error_msg

    checker = FakeDbChecker(script)
    result = checker.check(make_cfg(targets=["ventas.fantasma"]), "s")
    assert result.status is Status.DEGRADED
    assert result.error_type is ErrorType.TARGET_MISSING
    assert "tabla" in result.error_msg


def test_health_query_revalidated_at_execution():
    checker = FakeDbChecker(script_ok)
    cfg = make_cfg(health_query="DROP TABLE ventas")  # bypassed the save-time check
    result = checker.check(cfg, "s")
    assert result.status is Status.DEGRADED
    health = [t for t in result.targets if t.target == HEALTH_LABEL][0]
    assert not health.ok
    assert "rechazada" in health.message
    # the forbidden statement must never reach the driver
    assert all("DROP" not in sql for sql, _ in checker.conn.executed)


def test_health_query_timeout_capped_at_5s():
    checker = FakeDbChecker(script_ok)
    checker.check(make_cfg(health_query="SELECT 1", timeout_s=30.0), "s")
    assert checker.timeouts_used == [5.0]

    checker = FakeDbChecker(script_ok)
    checker.check(make_cfg(health_query="SELECT 1", timeout_s=3.0), "s")
    assert checker.timeouts_used == [3.0]  # never above cfg.timeout_s either


def test_health_query_timeout_reported_as_query_timeout():
    def script(sql, params):
        if sql.startswith("SELECT pesada"):
            raise TimeoutError("timed out")
        return (1,)

    checker = FakeDbChecker(script)
    result = checker.check(make_cfg(health_query="SELECT pesada FROM t"), "s")
    assert result.status is Status.DEGRADED
    health = result.targets[-1]
    assert health.error_type is ErrorType.QUERY_TIMEOUT


def test_connection_failure_is_down_and_classified():
    class RefusingChecker(FakeDbChecker):
        def _connect(self, cfg, secret):
            raise ConnectionRefusedError("refused")

    result = RefusingChecker(script_ok).check(make_cfg(), "s")
    assert result.status is Status.DOWN
    assert result.error_type is ErrorType.TCP_CONNECT


def test_validation_query_failure_closes_connection():
    def script(sql, params):
        if sql == "SELECT 1":
            raise CheckError(ErrorType.AUTH, "autenticación rechazada")
        return (1,)

    checker = FakeDbChecker(script)
    result = checker.check(make_cfg(), "s")
    assert result.status is Status.DOWN
    assert result.error_type is ErrorType.AUTH
    assert checker.conn.closed


def test_registry_returns_db_checkers():
    assert isinstance(get_checker(Protocol.POSTGRES), PostgresChecker)
    assert isinstance(get_checker(Protocol.MYSQL), MySqlChecker)
    assert isinstance(get_checker(Protocol.MARIADB), MySqlChecker)
    assert isinstance(get_checker(Protocol.SQLSERVER), SqlServerChecker)
    assert isinstance(get_checker(Protocol.ORACLE), OracleChecker)

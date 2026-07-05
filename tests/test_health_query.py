"""Health query validation: only trivial SELECTs may ever reach a monitored DB."""
from __future__ import annotations

import pytest

from app.models import validate_health_query


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "select 1",
        "  SELECT 1  ",
        "SELECT 1;",
        "SELECT 1 FROM DUAL",
        "SELECT now()",
        "SELECT count(*) FROM ventas.pedidos WHERE fecha > current_date - 1",
        # words that merely *contain* forbidden keywords are fine
        "SELECT * FROM updates",
        "SELECT delete_flag FROM t",
        "SELECT created_at FROM t",
    ],
)
def test_valid_queries_accepted(query: str):
    assert validate_health_query(query) is None


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "DELETE FROM ventas",
        "UPDATE t SET x = 1",
        "INSERT INTO t VALUES (1)",
        "DROP TABLE t",
        "TRUNCATE TABLE t",
        "WITH x AS (SELECT 1) SELECT * FROM x",  # must start with SELECT (spec-literal)
        "SELECT 1; DROP TABLE t",  # multi-statement
        "SELECT * INTO backup FROM t",  # SELECT INTO writes
        "SELECT * FROM t FOR UPDATE",  # takes locks
        "EXEC sp_who",
        "CALL procedure()",
        "SELECT " + "x" * 3000,  # too long
        "select pg_sleep(10); select 1",
    ],
)
def test_invalid_queries_rejected(query: str):
    assert validate_health_query(query) is not None

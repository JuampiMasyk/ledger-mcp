import sqlite3

import pytest

from ledger_mcp.db import QueryError, run_readonly_query, validate_select


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE t (id INTEGER, v TEXT)")
    c.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"row{i}") for i in range(300)])
    return c


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "drop table t",
        "SELECT 1; DELETE FROM t",
        "PRAGMA table_info(t)",
        "WITH x AS (SELECT 1) DELETE FROM t",
        "UPDATE t SET v = 'x'",
        "",
    ],
)
def test_rejects_non_select(sql):
    with pytest.raises(QueryError):
        validate_select(sql)


def test_allows_select_and_cte():
    assert validate_select("SELECT * FROM t;") == "SELECT * FROM t"
    assert validate_select("WITH a AS (SELECT 1) SELECT * FROM a")


def test_row_cap_and_truncation(conn):
    out = run_readonly_query(conn, "SELECT * FROM t", limit=10)
    assert len(out["rows"]) == 10 and out["truncated"] is True
    assert out["columns"] == ["id", "v"]


def test_limit_is_clamped(conn):
    out = run_readonly_query(conn, "SELECT * FROM t", limit=10_000)
    assert len(out["rows"]) == 200


def test_sql_errors_become_query_errors(conn):
    with pytest.raises(QueryError):
        run_readonly_query(conn, "SELECT nope FROM t")

"""SQLite access layer.

The server only ever reads from the database. Every connection is opened in
read-only mode, and free-form SQL goes through `run_readonly_query`, which
rejects anything that is not a single SELECT statement.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(os.environ.get("LEDGER_DB", "data/ledger.db"))
MAX_ROWS = 200

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)


class QueryError(ValueError):
    """Raised when a query is rejected or fails."""


def connect(db_path: Path | str = DEFAULT_DB_PATH, *, read_only: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    if read_only:
        if not path.exists():
            raise FileNotFoundError(f"Database not found at {path}. Run `python -m ledger_mcp.seed` first.")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def validate_select(sql: str) -> str:
    """Return a cleaned SQL string or raise QueryError if it is not a safe SELECT."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise QueryError("Empty query.")
    if ";" in cleaned:
        raise QueryError("Only a single statement is allowed.")
    first = cleaned.split(None, 1)[0].lower()
    if first not in {"select", "with"}:
        raise QueryError("Only SELECT queries are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise QueryError("Query contains a forbidden keyword.")
    return cleaned


def run_readonly_query(conn: sqlite3.Connection, sql: str, limit: int = MAX_ROWS) -> dict[str, Any]:
    cleaned = validate_select(sql)
    limit = max(1, min(limit, MAX_ROWS))
    try:
        cur = conn.execute(f"SELECT * FROM ({cleaned}) LIMIT {limit + 1}")
    except sqlite3.Error as exc:  # surface DB errors as tool errors, not crashes
        raise QueryError(str(exc)) from exc
    rows = cur.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    columns = [d[0] for d in cur.description] if cur.description else []
    return {"columns": columns, "rows": [list(r) for r in rows], "truncated": truncated}


def describe_schema(conn: sqlite3.Connection) -> str:
    tables = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return "\n\n".join(f"-- {t['name']}\n{t['sql']};" for t in tables)

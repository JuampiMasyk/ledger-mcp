"""Ledger MCP server.

Exposes a read-only accounts-payable ledger (vendors, invoices, payments) to
LLM agents through the Model Context Protocol:

Tools
    run_sql            read-only SELECT with a row cap
    vendor_balances    open balance per vendor
    reconcile_ledger   match invoices to payments and list exceptions
    explain_exception  pull the raw rows behind one exception

Resources
    ledger://schema    table definitions, so the agent can write correct SQL

Prompts
    month_end_review   a guided workflow for an AP month-end close
"""

from __future__ import annotations

import argparse
import os
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .db import DEFAULT_DB_PATH, QueryError, connect, describe_schema, rows_to_dicts, run_readonly_query
from .reconcile import reconcile

mcp = FastMCP(
    "ledger",
    instructions=(
        "Read-only accounts-payable ledger. Read ledger://schema before writing SQL. "
        "Use reconcile_ledger for matching questions instead of re-implementing it in SQL."
    ),
)


def _db_path() -> Path:
    return Path(os.environ.get("LEDGER_DB", str(DEFAULT_DB_PATH)))


def _load(conn) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    invoices = rows_to_dicts(conn.execute("SELECT * FROM invoices ORDER BY id").fetchall())
    payments = rows_to_dicts(conn.execute("SELECT * FROM payments ORDER BY id").fetchall())
    return invoices, payments


@mcp.resource("ledger://schema", mime_type="text/plain")
def schema() -> str:
    """SQL definitions of every table in the ledger."""
    with closing(connect(_db_path())) as conn:
        return describe_schema(conn)


@mcp.tool()
def run_sql(query: str, limit: int = 50) -> dict[str, Any]:
    """Run a read-only SELECT (or WITH ... SELECT) against the ledger.

    Returns columns, rows and whether the result was truncated. Write
    statements, multiple statements and PRAGMAs are rejected.
    """
    with closing(connect(_db_path())) as conn:
        try:
            return run_readonly_query(conn, query, limit)
        except QueryError as exc:
            return {"error": str(exc)}


@mcp.tool()
def vendor_balances(currency: Literal["USD", "ARS"] | None = None) -> list[dict[str, Any]]:
    """Billed, paid and open balance per vendor, optionally filtered by currency.

    Payments are counted in the vendor's own currency only, so a payment sent
    in the wrong currency shows up as an open balance (and as an exception in
    reconcile_ledger).
    """
    sql = """
        SELECT v.id AS vendor_id, v.name, v.currency,
               ROUND(COALESCE(i.billed, 0), 2) AS billed,
               ROUND(COALESCE(p.paid, 0), 2) AS paid,
               ROUND(COALESCE(i.billed, 0) - COALESCE(p.paid, 0), 2) AS open_balance
        FROM vendors v
        LEFT JOIN (SELECT vendor_id, SUM(amount) AS billed FROM invoices GROUP BY vendor_id) i
               ON i.vendor_id = v.id
        LEFT JOIN (SELECT p.vendor_id, SUM(p.amount) AS paid
                   FROM payments p JOIN vendors v2 ON v2.id = p.vendor_id AND v2.currency = p.currency
                   GROUP BY p.vendor_id) p
               ON p.vendor_id = v.id
        WHERE (:currency IS NULL OR v.currency = :currency)
        ORDER BY open_balance DESC
    """
    with closing(connect(_db_path())) as conn:
        return rows_to_dicts(conn.execute(sql, {"currency": currency}).fetchall())


@mcp.tool()
def reconcile_ledger(
    as_of: str = "2026-10-01",
    amount_tolerance: float = 0.01,
    date_window_days: int = 45,
    include_matches: bool = False,
) -> dict[str, Any]:
    """Match invoices to payments and list every exception.

    Exception kinds: underpaid, overpaid, duplicate_payment, currency_mismatch,
    overdue_unpaid, unmatched_payment, ambiguous_payment.

    Args:
        as_of: ISO date used to decide which unpaid invoices are overdue.
        amount_tolerance: max difference (same currency) still treated as equal.
        date_window_days: how far after issue date a reference-less payment can land.
        include_matches: also return every matched invoice (large output).
    """
    with closing(connect(_db_path())) as conn:
        invoices, payments = _load(conn)
    report = reconcile(
        invoices,
        payments,
        as_of=as_of,
        amount_tolerance=amount_tolerance,
        date_window_days=date_window_days,
    ).to_dict()
    if not include_matches:
        report["fuzzy_matches"] = [m for m in report["matches"] if m["rule"] == "fuzzy"]
        del report["matches"]
    return report


@mcp.tool()
def explain_exception(invoice_id: int | None = None, payment_id: int | None = None) -> dict[str, Any]:
    """Return the raw invoice and payment rows behind an exception, plus the vendor."""
    if invoice_id is None and payment_id is None:
        return {"error": "Pass invoice_id, payment_id or both."}
    out: dict[str, Any] = {}
    with closing(connect(_db_path())) as conn:
        if invoice_id is not None:
            inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            out["invoice"] = dict(inv) if inv else None
            if inv:
                out["payments_referencing_invoice"] = rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM payments WHERE UPPER(reference) LIKE '%' || UPPER(?) || '%'",
                        (inv["number"],),
                    ).fetchall()
                )
        if payment_id is not None:
            pay = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            out["payment"] = dict(pay) if pay else None
        vendor_id = (out.get("invoice") or out.get("payment") or {}).get("vendor_id")
        if vendor_id:
            out["vendor"] = dict(conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone())
    return out


@mcp.prompt()
def month_end_review(as_of: str = "2026-10-01") -> str:
    """Guided accounts-payable month-end review."""
    return (
        f"You are closing accounts payable as of {as_of}.\n"
        "1. Read ledger://schema.\n"
        f"2. Call reconcile_ledger(as_of='{as_of}').\n"
        "3. For every exception, call explain_exception and decide the next action "
        "(chase vendor, request refund, fix currency, escalate).\n"
        "4. Call vendor_balances and flag vendors whose open balance is driven by exceptions.\n"
        "5. Finish with a table: exception, amount at risk, owner, next action. "
        "Do not invent rows that the tools did not return."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ledger MCP server.")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

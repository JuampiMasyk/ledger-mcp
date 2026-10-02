"""Protocol-level tests: call the server through an in-memory MCP client."""

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from ledger_mcp import seed, server


@pytest.fixture(autouse=True)
def ledger_db(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    seed.write(db, tmp_path / "golden.json")
    monkeypatch.setenv("LEDGER_DB", str(db))
    return db


def payload(result):
    if result.structuredContent:
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


async def test_lists_tools_resource_and_prompt():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"run_sql", "vendor_balances", "reconcile_ledger", "explain_exception"} <= tools
        resources = [str(r.uri) for r in (await client.list_resources()).resources]
        assert "ledger://schema" in resources
        prompts = [p.name for p in (await client.list_prompts()).prompts]
        assert "month_end_review" in prompts


async def test_schema_resource_describes_tables():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        res = await client.read_resource("ledger://schema")
        text = res.contents[0].text
        assert "CREATE TABLE invoices" in text and "CREATE TABLE payments" in text


async def test_run_sql_rejects_writes_over_protocol():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        out = payload(await client.call_tool("run_sql", {"query": "DELETE FROM invoices"}))
        assert "error" in out


async def test_reconcile_summary_matches_planted_anomalies():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        out = payload(await client.call_tool("reconcile_ledger", {}))
        assert out["summary"]["exceptions"] == {
            "ambiguous_payment": 1,
            "currency_mismatch": 2,
            "duplicate_payment": 2,
            "overdue_unpaid": 4,
            "underpaid": 3,
            "unmatched_payment": 3,
        }


async def test_explain_exception_returns_vendor():
    async with create_connected_server_and_client_session(server.mcp._mcp_server) as client:
        out = payload(await client.call_tool("explain_exception", {"invoice_id": 1}))
        assert out["invoice"]["id"] == 1 and out["vendor"]["id"] == out["invoice"]["vendor_id"]

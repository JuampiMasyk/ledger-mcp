"""End-to-end eval suite that talks to the server over MCP, like an agent would.

It checks three things against ground truth written by the seed script
(evals/golden.json), not against the code under test:

1. Reconciliation quality: precision and recall per exception kind.
2. Safety: a set of hostile SQL inputs must all be rejected.
3. Contract: the advertised tools, resource and prompt exist and are documented.

    python evals/run_evals.py            # prints a report, exits 1 if a gate fails
    python evals/run_evals.py --json     # machine-readable output for CI
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ledger_mcp.server import mcp  # noqa: E402

GOLDEN = json.loads((ROOT / "evals" / "golden.json").read_text())

HOSTILE_SQL = [
    "DELETE FROM invoices",
    "DROP TABLE payments",
    "SELECT 1; DELETE FROM invoices",
    "UPDATE invoices SET amount = 0",
    "PRAGMA table_info(invoices)",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "WITH x AS (SELECT 1) DELETE FROM invoices",
    "insert into vendors values (99,'x','AR','USD')",
]

EXPECTED_TOOLS = {"run_sql", "vendor_balances", "reconcile_ledger", "explain_exception"}

GATES = {"min_recall": 1.0, "min_precision": 1.0, "min_safety": 1.0}


def _payload(result) -> dict:
    if getattr(result, "structuredContent", None):
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


async def evaluate() -> dict:
    out: dict = {"reconciliation": {}, "safety": {}, "contract": {}}
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        # 1. Reconciliation quality.
        report = _payload(await client.call_tool("reconcile_ledger", {"as_of": GOLDEN["as_of"]}))
        invoices = {
            row[0]: row[1]
            for row in _payload(
                await client.call_tool("run_sql", {"query": "SELECT id, number FROM invoices", "limit": 200})
            )["rows"]
        }
        predicted: dict[str, set[str]] = {}
        for exc in report["exceptions"]:
            key = (
                invoices.get(exc["invoice_id"])
                if exc["kind"] not in {"unmatched_payment", "ambiguous_payment"}
                else str(exc["payment_id"])
            )
            predicted.setdefault(exc["kind"], set()).add(key)
        predicted["fuzzy_matched"] = {m["invoice_number"] for m in report["fuzzy_matches"]}

        for kind, expected in GOLDEN["expected"].items():
            exp, got = set(expected), predicted.get(kind, set())
            tp = len(exp & got)
            out["reconciliation"][kind] = {
                "expected": len(exp),
                "predicted": len(got),
                "precision": round(tp / len(got), 3) if got else (1.0 if not exp else 0.0),
                "recall": round(tp / len(exp), 3) if exp else 1.0,
                "missed": sorted(exp - got),
                "spurious": sorted(got - exp),
            }
        extra = set(predicted) - set(GOLDEN["expected"])
        out["reconciliation"]["_unexpected_kinds"] = sorted(extra)

        # 2. Safety.
        rejected = []
        for sql in HOSTILE_SQL:
            res = _payload(await client.call_tool("run_sql", {"query": sql}))
            rejected.append({"query": sql, "rejected": "error" in res})
        count = _payload(await client.call_tool("run_sql", {"query": "SELECT COUNT(*) FROM invoices"}))
        out["safety"] = {
            "rejection_rate": round(sum(r["rejected"] for r in rejected) / len(rejected), 3),
            "cases": rejected,
            "data_intact": count["rows"][0][0] == len(invoices),
        }

        # 3. Contract.
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        prompts = (await client.list_prompts()).prompts
        out["contract"] = {
            "missing_tools": sorted(EXPECTED_TOOLS - {t.name for t in tools}),
            "undocumented_tools": sorted(t.name for t in tools if not (t.description or "").strip()),
            "has_schema_resource": any(str(r.uri) == "ledger://schema" for r in resources),
            "has_month_end_prompt": any(p.name == "month_end_review" for p in prompts),
        }
    return out


def gates(results: dict) -> list[str]:
    failures = []
    for kind, m in results["reconciliation"].items():
        if kind.startswith("_"):
            continue
        if m["recall"] < GATES["min_recall"]:
            failures.append(f"{kind}: recall {m['recall']} (missed {m['missed']})")
        if m["precision"] < GATES["min_precision"]:
            failures.append(f"{kind}: precision {m['precision']} (spurious {m['spurious']})")
    if results["reconciliation"]["_unexpected_kinds"]:
        failures.append(f"unexpected exception kinds: {results['reconciliation']['_unexpected_kinds']}")
    if results["safety"]["rejection_rate"] < GATES["min_safety"]:
        failures.append(f"safety: only {results['safety']['rejection_rate']:.0%} of hostile queries rejected")
    if not results["safety"]["data_intact"]:
        failures.append("safety: data changed during the run")
    c = results["contract"]
    if (
        c["missing_tools"]
        or c["undocumented_tools"]
        or not c["has_schema_resource"]
        or not c["has_month_end_prompt"]
    ):
        failures.append(f"contract: {c}")
    return failures


def print_report(results: dict, failures: list[str]) -> None:
    print(f"{'kind':<20}{'exp':>5}{'pred':>6}{'precision':>11}{'recall':>8}")
    for kind, m in results["reconciliation"].items():
        if kind.startswith("_"):
            continue
        print(f"{kind:<20}{m['expected']:>5}{m['predicted']:>6}{m['precision']:>11.2f}{m['recall']:>8.2f}")
    s = results["safety"]
    print(f"\nhostile SQL rejected: {s['rejection_rate']:.0%}   data intact: {s['data_intact']}")
    print(f"contract: {results['contract']}")
    print("\nPASS" if not failures else "\nFAIL\n- " + "\n- ".join(failures))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.INFO)
    results = asyncio.run(evaluate())
    failures = gates(results)
    if args.json:
        print(json.dumps({"results": results, "failures": failures}, indent=2))
    else:
        print_report(results, failures)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()

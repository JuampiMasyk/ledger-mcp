# ledger-mcp

An [MCP](https://modelcontextprotocol.io) server that lets LLM agents (Claude, ChatGPT, Cursor, any MCP client) query and reconcile an accounts-payable ledger, plus an eval suite that measures how well the server does the job.

It ships with a synthetic ledger (8 vendors, 131 invoices, 125 payments) where real-world problems are planted on purpose: underpayments, duplicate payments, payments in the wrong currency, overdue invoices, payments without a reference, and a payment that could belong to two invoices. The eval suite checks the server against that ground truth.

## Why

Reconciling invoices against payments is a classic job for an agent: lots of rows, fuzzy references, and a few exceptions that matter. Letting a model write arbitrary SQL against financial data is risky, so this server gives it:

- **Safe access**: the database is opened read-only, and free-form SQL is limited to a single `SELECT` with a row cap.
- **Deterministic tools for the hard parts**: matching logic lives in tested Python, not in the model's head.
- **Measurable quality**: precision and recall per exception type, a hostile-SQL safety check, and a contract check, all runnable in CI.

## What it exposes

| Type | Name | What it does |
|------|------|--------------|
| Tool | `run_sql` | Read-only `SELECT` / `WITH` query, max 200 rows, flags truncation |
| Tool | `vendor_balances` | Billed, paid and open balance per vendor |
| Tool | `reconcile_ledger` | Matches invoices to payments and lists exceptions by type |
| Tool | `explain_exception` | Raw invoice, payment and vendor rows behind an exception |
| Resource | `ledger://schema` | Table definitions, so the agent writes valid SQL |
| Prompt | `month_end_review` | Step-by-step month-end close workflow |

### Matching rules

1. **Reference match**: the payment reference contains the invoice number (case-insensitive).
2. **Fuzzy match**: same vendor and currency, amount within tolerance, paid within N days of issue, and exactly one candidate invoice. If more than one invoice fits, the payment is flagged as `ambiguous_payment` instead of guessed.

Exception types: `underpaid`, `overpaid`, `duplicate_payment`, `currency_mismatch`, `overdue_unpaid`, `unmatched_payment`, `ambiguous_payment`.

## Quick start

```bash
pip install -e ".[dev]"
ledger-seed                      # creates data/ledger.db and evals/golden.json
ledger-mcp                       # stdio transport, for Claude Desktop / Cursor
ledger-mcp --transport streamable-http --port 8000   # HTTP transport
```

Inspect it interactively with the MCP Inspector:

```bash
mcp dev src/ledger_mcp/server.py
```

### Use it from Claude Desktop

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ledger": {
      "command": "ledger-mcp",
      "env": { "LEDGER_DB": "/absolute/path/to/data/ledger.db" }
    }
  }
}
```

Then ask: *"Run the month-end review and tell me which vendors I need to contact."*

### Docker

```bash
docker build -t ledger-mcp .
docker run -p 8000:8000 ledger-mcp   # streamable HTTP on :8000/mcp
```

## Tests and evals

```bash
pytest -q                  # unit tests + protocol tests through an in-memory MCP client
python evals/run_evals.py  # golden-set eval, exits 1 if a quality gate fails
```

Sample eval output:

```
kind                  exp  pred  precision  recall
underpaid               3     3       1.00    1.00
duplicate_payment       2     2       1.00    1.00
currency_mismatch       2     2       1.00    1.00
overdue_unpaid          4     4       1.00    1.00
unmatched_payment       3     3       1.00    1.00
ambiguous_payment       1     1       1.00    1.00
fuzzy_matched           2     2       1.00    1.00

hostile SQL rejected: 100%   data intact: True
PASS
```

The golden file is written by the seed script when it plants each anomaly, so the eval never grades the code against its own output. CI runs lint, tests, evals and a Docker build on every push.

## Project layout

```
src/ledger_mcp/
  server.py      MCP tools, resource and prompt (FastMCP)
  reconcile.py   matching logic, pure functions
  db.py          read-only SQLite access and SQL guard
  seed.py        synthetic data with planted anomalies + golden file
tests/           unit and protocol tests
evals/           end-to-end eval suite over MCP
```

## Ideas for next steps

- Migrate to MCP Python SDK 2.x (`MCPServer`), currently pinned to 1.x.
- Postgres backend behind the same tools.
- An agent-level eval: run a model through `month_end_review` and score its final table against the golden file.
- Multi-currency matching with FX rates.

## License

MIT

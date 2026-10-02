"""Generate a deterministic synthetic ledger with planted anomalies.

The planted anomalies are written to `evals/golden.json`, so the eval suite can
check the server against ground truth that does not come from the code under
test.

    python -m ledger_mcp.seed            # writes data/ledger.db and evals/golden.json
    python -m ledger_mcp.seed --db x.db  # custom path
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from .db import connect

AS_OF = "2026-10-01"

SCHEMA = """
CREATE TABLE vendors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    currency TEXT NOT NULL
);
CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    number TEXT NOT NULL UNIQUE,
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL
);
CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
    reference TEXT,
    paid_date TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    method TEXT NOT NULL
);
"""

VENDORS = [
    ("Andes Cloud Hosting", "AR", "USD"),
    ("Patagonia Print Co.", "AR", "ARS"),
    ("Northwind Logistics", "US", "USD"),
    ("Rio Data Labs", "BR", "USD"),
    ("Pampa Office Supply", "AR", "ARS"),
    ("Atlas Legal LLP", "US", "USD"),
    ("Cordillera Marketing", "CL", "USD"),
    ("Delta Payroll Services", "US", "USD"),
]
METHODS = ["wire", "ach", "card"]


def build(seed: int = 7) -> tuple[list[tuple], list[tuple], list[tuple], dict]:
    rng = random.Random(seed)
    invoices: list[dict] = []
    payments: list[dict] = []
    golden: dict[str, list[str]] = {
        "underpaid": [],
        "duplicate_payment": [],
        "currency_mismatch": [],
        "overdue_unpaid": [],
        "unmatched_payment": [],
        "ambiguous_payment": [],
        "fuzzy_matched": [],
    }
    start = date(2026, 6, 1)

    def add_invoice(vendor_id: int, amount: float, issue: date, due_days: int = 30) -> dict:
        inv = {
            "id": len(invoices) + 1,
            "vendor_id": vendor_id,
            "number": f"INV-{2026}{len(invoices) + 1:04d}",
            "issue_date": issue.isoformat(),
            "due_date": (issue + timedelta(days=due_days)).isoformat(),
            "amount": round(amount, 2),
            "currency": VENDORS[vendor_id - 1][2],
        }
        invoices.append(inv)
        return inv

    def add_payment(
        vendor_id: int, amount: float, paid: date, reference: str | None, currency: str | None = None
    ) -> dict:
        pay = {
            "id": len(payments) + 1,
            "vendor_id": vendor_id,
            "reference": reference,
            "paid_date": paid.isoformat(),
            "amount": round(amount, 2),
            "currency": currency or VENDORS[vendor_id - 1][2],
            "method": rng.choice(METHODS),
        }
        payments.append(pay)
        return pay

    # Clean history: invoices paid in full with a reference.
    for _ in range(110):
        v = rng.randint(1, len(VENDORS))
        issue = start + timedelta(days=rng.randint(0, 75))
        inv = add_invoice(v, rng.uniform(150, 9000), issue)
        add_payment(v, inv["amount"], issue + timedelta(days=rng.randint(5, 28)), f"Payment {inv['number']}")

    # Planted: underpaid (3).
    for _ in range(3):
        v = rng.randint(1, len(VENDORS))
        issue = start + timedelta(days=rng.randint(0, 60))
        inv = add_invoice(v, rng.uniform(1000, 5000), issue)
        add_payment(
            v,
            inv["amount"] - rng.choice([50, 125.5, 300]),
            issue + timedelta(days=10),
            f"{inv['number']} partial",
        )
        golden["underpaid"].append(inv["number"])

    # Planted: duplicate payments (2).
    for _ in range(2):
        v = rng.randint(1, len(VENDORS))
        issue = start + timedelta(days=rng.randint(0, 60))
        inv = add_invoice(v, rng.uniform(500, 4000), issue)
        add_payment(v, inv["amount"], issue + timedelta(days=7), f"Payment {inv['number']}")
        add_payment(v, inv["amount"], issue + timedelta(days=9), f"Payment {inv['number']} retry")
        golden["duplicate_payment"].append(inv["number"])

    # Planted: currency mismatch (2), on ARS vendors paid in USD.
    for v in (2, 5):
        issue = start + timedelta(days=rng.randint(0, 60))
        inv = add_invoice(v, rng.uniform(200000, 900000), issue, due_days=200)
        add_payment(
            v, inv["amount"] / 1000, issue + timedelta(days=12), f"Payment {inv['number']}", currency="USD"
        )
        golden["currency_mismatch"].append(inv["number"])

    # Planted: overdue with no payment (4).
    for _ in range(4):
        v = rng.randint(1, len(VENDORS))
        issue = start + timedelta(days=rng.randint(0, 40))
        inv = add_invoice(v, rng.uniform(300, 6000), issue, due_days=30)
        golden["overdue_unpaid"].append(inv["number"])

    # Planted: fuzzy matches, payment without reference but exact amount (2).
    for v in (3, 6):
        issue = date(2026, 9, 1) + timedelta(days=rng.randint(0, 10))
        inv = add_invoice(v, rng.uniform(1000, 3000) + 0.37, issue)
        add_payment(v, inv["amount"], issue + timedelta(days=6), None)
        golden["fuzzy_matched"].append(inv["number"])

    # Planted: ambiguous payment, two open invoices with the same amount (1).
    issue = date(2026, 9, 15)
    a = add_invoice(4, 1499.0, issue, due_days=60)
    b = add_invoice(4, 1499.0, issue + timedelta(days=2), due_days=60)
    pay = add_payment(4, 1499.0, issue + timedelta(days=8), "Monthly retainer")
    golden["ambiguous_payment"].append(str(pay["id"]))
    _ = (a, b)

    # Planted: unmatched payments, no reference and no invoice with that amount (3).
    for v in (1, 7, 8):
        pay = add_payment(v, 777.77 + v, date(2026, 9, 20), None)
        golden["unmatched_payment"].append(str(pay["id"]))

    # Open invoices not yet due: not exceptions.
    for _ in range(6):
        v = rng.randint(1, len(VENDORS))
        add_invoice(
            v, rng.uniform(300, 5000), date(2026, 9, 20) + timedelta(days=rng.randint(0, 8)), due_days=30
        )

    vendor_rows = [(i + 1, *v) for i, v in enumerate(VENDORS)]
    inv_rows = [tuple(i.values()) for i in invoices]
    pay_rows = [tuple(p.values()) for p in payments]
    return vendor_rows, inv_rows, pay_rows, {"as_of": AS_OF, "expected": golden}


def write(db_path: Path, golden_path: Path, seed: int = 7) -> None:
    if db_path.exists():
        db_path.unlink()
    vendors, invoices, payments, golden = build(seed)
    conn = connect(db_path, read_only=False)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO vendors VALUES (?,?,?,?)", vendors)
    conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?,?)", invoices)
    conn.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?,?)", payments)
    conn.commit()
    conn.close()
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    golden_path.write_text(json.dumps(golden, indent=2) + "\n")
    print(f"Wrote {len(invoices)} invoices and {len(payments)} payments to {db_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/ledger.db")
    parser.add_argument("--golden", default="evals/golden.json")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    write(Path(args.db), Path(args.golden), args.seed)


if __name__ == "__main__":
    main()

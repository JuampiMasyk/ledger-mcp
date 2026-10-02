"""Invoice vs payment reconciliation.

Pure functions over plain dicts so the logic is easy to unit test without a
database or an MCP client.

Matching rules, in order:
1. Reference match: the payment reference contains the invoice number.
2. Fuzzy match: same vendor, same currency, amount within `amount_tolerance`,
   paid within `date_window_days` of the invoice issue date, and the invoice is
   still unmatched.
Anything left over is reported as an exception with a reason code.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


@dataclass
class Match:
    invoice_id: int
    invoice_number: str
    payment_ids: list[int]
    rule: str  # "reference" or "fuzzy"
    invoice_amount: float
    paid_amount: float
    status: str  # "paid", "underpaid", "overpaid"


@dataclass
class Exception_:
    kind: str
    detail: str
    invoice_id: int | None = None
    payment_id: int | None = None


@dataclass
class ReconciliationReport:
    matches: list[Match] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for e in self.exceptions:
            counts[e.kind] += 1
        return {
            "matched_invoices": len(self.matches),
            "exceptions": dict(sorted(counts.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "matches": [asdict(m) for m in self.matches],
            "exceptions": [asdict(e) for e in self.exceptions],
        }


def _d(value: str) -> date:
    return date.fromisoformat(value)


def reconcile(
    invoices: list[dict[str, Any]],
    payments: list[dict[str, Any]],
    *,
    as_of: str,
    amount_tolerance: float = 0.01,
    date_window_days: int = 45,
) -> ReconciliationReport:
    report = ReconciliationReport()
    by_number = {inv["number"]: inv for inv in invoices}
    paid: dict[int, list[dict[str, Any]]] = defaultdict(list)
    used_payments: set[int] = set()

    # Rule 1: reference match. A payment can reference only one invoice.
    for pay in payments:
        ref = (pay.get("reference") or "").upper()
        for number, inv in by_number.items():
            if number.upper() in ref:
                if pay["currency"] != inv["currency"]:
                    report.exceptions.append(
                        Exception_(
                            "currency_mismatch",
                            f"Payment {pay['id']} in {pay['currency']} references "
                            f"{number} billed in {inv['currency']}",
                            invoice_id=inv["id"],
                            payment_id=pay["id"],
                        )
                    )
                    used_payments.add(pay["id"])
                    break
                paid[inv["id"]].append(pay | {"_rule": "reference"})
                used_payments.add(pay["id"])
                break

    # Rule 2: fuzzy match for payments without a usable reference.
    for pay in payments:
        if pay["id"] in used_payments:
            continue
        candidates = [
            inv
            for inv in invoices
            if inv["id"] not in paid
            and inv["vendor_id"] == pay["vendor_id"]
            and inv["currency"] == pay["currency"]
            and abs(inv["amount"] - pay["amount"]) <= amount_tolerance
            and 0 <= (_d(pay["paid_date"]) - _d(inv["issue_date"])).days <= date_window_days
        ]
        if len(candidates) == 1:
            paid[candidates[0]["id"]].append(pay | {"_rule": "fuzzy"})
            used_payments.add(pay["id"])
        elif len(candidates) > 1:
            report.exceptions.append(
                Exception_(
                    "ambiguous_payment",
                    f"Payment {pay['id']} could match {len(candidates)} invoices: "
                    + ", ".join(c["number"] for c in candidates),
                    payment_id=pay["id"],
                )
            )
            used_payments.add(pay["id"])

    # Build matches and amount exceptions.
    for inv in invoices:
        pays = paid.get(inv["id"])
        if not pays:
            continue
        total = round(sum(p["amount"] for p in pays), 2)
        if len(pays) > 1 and any(
            p["amount"] == q["amount"] and p["id"] != q["id"] for p in pays for q in pays
        ):
            report.exceptions.append(
                Exception_(
                    "duplicate_payment",
                    f"Invoice {inv['number']} received {len(pays)} payments of the same amount",
                    invoice_id=inv["id"],
                    payment_id=pays[-1]["id"],
                )
            )
        diff = round(total - inv["amount"], 2)
        if abs(diff) <= amount_tolerance:
            status = "paid"
        elif diff < 0:
            status = "underpaid"
            report.exceptions.append(
                Exception_(
                    "underpaid",
                    f"Invoice {inv['number']} short by {abs(diff):.2f} {inv['currency']}",
                    invoice_id=inv["id"],
                )
            )
        else:
            status = "overpaid"
            if not any(
                e.kind == "duplicate_payment" and e.invoice_id == inv["id"] for e in report.exceptions
            ):
                report.exceptions.append(
                    Exception_(
                        "overpaid",
                        f"Invoice {inv['number']} overpaid by {diff:.2f} {inv['currency']}",
                        invoice_id=inv["id"],
                    )
                )
        report.matches.append(
            Match(
                invoice_id=inv["id"],
                invoice_number=inv["number"],
                payment_ids=[p["id"] for p in pays],
                rule=pays[0]["_rule"],
                invoice_amount=inv["amount"],
                paid_amount=total,
                status=status,
            )
        )

    # Leftovers.
    today = _d(as_of)
    for inv in invoices:
        if inv["id"] not in paid and _d(inv["due_date"]) < today:
            report.exceptions.append(
                Exception_(
                    "overdue_unpaid",
                    f"Invoice {inv['number']} was due {inv['due_date']} and has no payment",
                    invoice_id=inv["id"],
                )
            )
    for pay in payments:
        if pay["id"] not in used_payments:
            report.exceptions.append(
                Exception_(
                    "unmatched_payment",
                    f"Payment {pay['id']} ({pay['amount']:.2f} {pay['currency']}) matches no invoice",
                    payment_id=pay["id"],
                )
            )

    return report

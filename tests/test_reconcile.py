from ledger_mcp.reconcile import reconcile

AS_OF = "2026-10-01"


def inv(id_, number, amount, *, vendor=1, currency="USD", issue="2026-08-01", due="2026-08-31"):
    return {
        "id": id_,
        "vendor_id": vendor,
        "number": number,
        "issue_date": issue,
        "due_date": due,
        "amount": amount,
        "currency": currency,
    }


def pay(id_, amount, *, ref=None, vendor=1, currency="USD", paid="2026-08-10"):
    return {
        "id": id_,
        "vendor_id": vendor,
        "reference": ref,
        "paid_date": paid,
        "amount": amount,
        "currency": currency,
    }


def kinds(report):
    return sorted(e.kind for e in report.exceptions)


def test_reference_match_paid_in_full():
    r = reconcile([inv(1, "INV-1", 100.0)], [pay(1, 100.0, ref="Payment INV-1")], as_of=AS_OF)
    assert r.exceptions == []
    assert r.matches[0].status == "paid" and r.matches[0].rule == "reference"


def test_reference_match_is_case_insensitive():
    r = reconcile([inv(1, "INV-1", 100.0)], [pay(1, 100.0, ref="payment inv-1")], as_of=AS_OF)
    assert r.matches and r.matches[0].rule == "reference"


def test_underpaid_is_flagged():
    r = reconcile([inv(1, "INV-1", 100.0)], [pay(1, 60.0, ref="INV-1")], as_of=AS_OF)
    assert kinds(r) == ["underpaid"]
    assert r.matches[0].status == "underpaid"


def test_duplicate_payment_reported_once_not_also_as_overpaid():
    r = reconcile(
        [inv(1, "INV-1", 100.0)],
        [pay(1, 100.0, ref="INV-1"), pay(2, 100.0, ref="INV-1 retry")],
        as_of=AS_OF,
    )
    assert kinds(r) == ["duplicate_payment"]


def test_currency_mismatch_does_not_count_as_payment():
    r = reconcile([inv(1, "INV-1", 1000.0, currency="ARS")], [pay(1, 1.0, ref="INV-1")], as_of=AS_OF)
    assert "currency_mismatch" in kinds(r)
    assert r.matches == []


def test_fuzzy_match_without_reference():
    r = reconcile([inv(1, "INV-1", 123.45)], [pay(1, 123.45)], as_of=AS_OF)
    assert r.exceptions == []
    assert r.matches[0].rule == "fuzzy"


def test_fuzzy_match_respects_vendor_and_date_window():
    invoices = [inv(1, "INV-1", 50.0, vendor=1)]
    other_vendor = reconcile(invoices, [pay(1, 50.0, vendor=2)], as_of=AS_OF)
    too_late = reconcile(invoices, [pay(1, 50.0, paid="2026-12-01")], as_of=AS_OF)
    assert "unmatched_payment" in kinds(other_vendor)
    assert "unmatched_payment" in kinds(too_late)


def test_ambiguous_payment_is_not_guessed():
    r = reconcile(
        [inv(1, "INV-1", 99.0, due="2026-12-01"), inv(2, "INV-2", 99.0, due="2026-12-01")],
        [pay(1, 99.0)],
        as_of=AS_OF,
    )
    assert kinds(r) == ["ambiguous_payment"]
    assert r.matches == []


def test_overdue_only_when_past_due_date():
    r = reconcile(
        [inv(1, "INV-1", 10.0, due="2026-09-01"), inv(2, "INV-2", 10.0, due="2026-10-15")],
        [],
        as_of=AS_OF,
    )
    assert [e.invoice_id for e in r.exceptions if e.kind == "overdue_unpaid"] == [1]


def test_amount_tolerance():
    r = reconcile([inv(1, "INV-1", 100.0)], [pay(1, 99.995, ref="INV-1")], as_of=AS_OF, amount_tolerance=0.01)
    assert r.exceptions == []

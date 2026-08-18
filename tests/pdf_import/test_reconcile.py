from pdf_import.reconcile import check_balance, check_date_range, reconcile_or_unavailable


def test_check_balance_ok_when_sums_match():
    transactions = [{"amount": -100.0}, {"amount": 250.0}, {"amount": -30.5}]
    result = check_balance(transactions, opening=1000.0, closing=1119.5)
    assert result["ok"] is True
    assert result["discrepancy"] == 0.0


def test_check_balance_flags_mismatch_with_discrepancy():
    transactions = [{"amount": -100.0}, {"amount": 250.0}]
    result = check_balance(transactions, opening=1000.0, closing=2000.0)
    assert result["ok"] is False
    assert result["expected_closing"] == 1150.0
    assert result["actual_sum"] == 150.0
    assert result["discrepancy"] == 850.0


def test_check_balance_tolerates_float_rounding():
    # 0.1 + 0.2 != 0.3 exactly in floating point; must not false-flag.
    transactions = [{"amount": 0.1}, {"amount": 0.2}]
    result = check_balance(transactions, opening=0.0, closing=0.3)
    assert result["ok"] is True


def test_check_date_range_flags_dates_outside_tolerance():
    transactions = [
        {"date": "2026-07-01"},   # in range
        {"date": "2026-06-27"},   # 3 days before start, still within tolerance
        {"date": "2026-06-26"},   # 4 days before start, out of tolerance
        {"date": "2026-08-05"},   # well after period end
    ]
    flagged = check_date_range(
        transactions, period_start="2026-06-30", period_end="2026-07-31", tolerance_days=3
    )
    assert flagged == [2, 3]


def test_check_date_range_empty_when_all_within_period():
    transactions = [{"date": "2026-07-15"}, {"date": "2026-07-20"}]
    flagged = check_date_range(
        transactions, period_start="2026-07-01", period_end="2026-07-31", tolerance_days=3
    )
    assert flagged == []


def test_reconcile_or_unavailable_returns_ok_status_when_balances_match():
    transactions = [{"amount": -100.0}, {"amount": 250.0}]
    result = reconcile_or_unavailable(transactions, opening_balance=1000.0, closing_balance=1150.0)
    assert result["status"] == "ok"
    assert result["discrepancy"] == 0.0


def test_reconcile_or_unavailable_returns_failed_status_on_mismatch():
    transactions = [{"amount": -100.0}, {"amount": 250.0}]
    result = reconcile_or_unavailable(transactions, opening_balance=1000.0, closing_balance=2000.0)
    assert result["status"] == "failed"
    assert result["discrepancy"] == 850.0


def test_reconcile_or_unavailable_when_opening_balance_unknown():
    # Some statements (e.g. a card with no printed "previous balance")
    # genuinely can't be reconciled -- this must be distinct from "failed",
    # not silently treated as a mismatch against an assumed-zero opening.
    transactions = [{"amount": -100.0}]
    result = reconcile_or_unavailable(transactions, opening_balance=None, closing_balance=500.0)
    assert result["status"] == "unavailable"
    assert "discrepancy" not in result


def test_reconcile_or_unavailable_when_closing_balance_unknown():
    transactions = [{"amount": -100.0}]
    result = reconcile_or_unavailable(transactions, opening_balance=500.0, closing_balance=None)
    assert result["status"] == "unavailable"

"""Balance reconciliation and date-range validation for parsed statements.

Credit-card adapters normalize previous_balance/total_dues into the same
opening_balance/closing_balance keys during meta-extraction (see
adapter_engine), so this module stays statement-type-agnostic: the
sign-flip already happened at the amount-convention level, and
opening + sum(amounts) == closing holds uniformly.
"""
from datetime import date, timedelta

_EPSILON = 0.01


def check_balance(transactions: list[dict], opening: float, closing: float) -> dict:
    actual_sum = sum(t["amount"] for t in transactions)
    expected_closing = opening + actual_sum
    discrepancy = round(closing - expected_closing, 2)
    return {
        "ok": abs(discrepancy) <= _EPSILON,
        "expected_closing": round(expected_closing, 2),
        "actual_sum": round(actual_sum, 2),
        "discrepancy": abs(discrepancy) if abs(discrepancy) > _EPSILON else 0.0,
    }


def reconcile_or_unavailable(
    transactions: list[dict], opening_balance: float | None, closing_balance: float | None
) -> dict:
    """Wraps check_balance with a third state: "unavailable" when either
    balance is unknown (e.g. a statement that never prints a previous
    balance figure). Distinct from "failed" -- there's no mismatch to
    report, just nothing to compare."""
    if opening_balance is None or closing_balance is None:
        return {"status": "unavailable"}
    result = check_balance(transactions, opening_balance, closing_balance)
    return {"status": "ok" if result["ok"] else "failed", **result}


def check_date_range(
    transactions: list[dict],
    period_start: str,
    period_end: str,
    tolerance_days: int = 3,
) -> list[int]:
    start = date.fromisoformat(period_start) - timedelta(days=tolerance_days)
    end = date.fromisoformat(period_end) + timedelta(days=tolerance_days)
    flagged = []
    for index, txn in enumerate(transactions):
        txn_date = date.fromisoformat(txn["date"])
        if txn_date < start or txn_date > end:
            flagged.append(index)
    return flagged

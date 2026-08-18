"""Generic adapter interpreter.

Applies one adapter JSON config (see pdf_import.schema) to a raw docling
table plus the statement's free text. This is the single generic engine --
it contains zero bank-specific branches; all bank-specific behavior comes
from the config it's given.
"""
import re
from datetime import datetime

_CURRENCY_STRIP_RE = re.compile(r"[₹,\s]|Rs\.?|INR|(?<![A-Za-z])C(?=\s*\d)")
_PAREN_NEGATIVE_RE = re.compile(r"^\((.+)\)$")
_DATE_SEPARATOR_CLEANUP_RE = re.compile(r"\s*\|\s*")
_WHITESPACE_RE = re.compile(r"\s+")
# Finds a proper decimal-formatted currency amount (always has '.XX' in this
# data) with its own immediately-preceding sign, if any. Reward-point counts
# are bare integers with no decimal point, so they never match this pattern
# even when merged into the same cell as the amount (e.g. "+ 45 C 1,453.98"
# -- the '+' there belongs to the points, not the amount, and is correctly
# NOT captured since digit characters block \D from reaching across it).
_SIGNED_DECIMAL_AMOUNT_RE = re.compile(r"([+-])?\s*\D{0,6}?([\d,]+\.\d{2})")


def apply_adapter(table: list[list[str]], statement_text: str, config: dict) -> dict:
    column_map = config["column_map"]
    decimal_style = config["decimal_style"]
    skip_patterns = [re.compile(p, re.IGNORECASE) for p in config["skip_row_patterns"]]

    transactions = []
    unparsed_rows = []

    for row_index, row in enumerate(table):
        if _is_skip_row(row, skip_patterns):
            continue

        date_cell = _cell(row, column_map.get("date"))

        if (
            not date_cell
            and config["wrap_merge"] == "empty_date_row_appends_to_previous_description"
            and transactions
        ):
            continuation = _cell(row, column_map.get("description"))
            if continuation:
                transactions[-1]["description"] = (
                    f"{transactions[-1]['description']} {continuation}".strip()
                )
            continue

        try:
            date_iso = _parse_date(date_cell, config["date_format"])
            amount = _parse_row_amount(
                row,
                column_map,
                config["amount_convention"],
                decimal_style,
                config.get("credit_description_patterns"),
            )
            if amount is None:
                raise ValueError("no amount found in row")
        except (ValueError, TypeError):
            unparsed_rows.append({
                "raw_row": row,
                "row_index": row_index,
                "reason": "couldn't parse this row",
            })
            continue

        time_cell = _cell(row, column_map.get("time")) or None
        balance_cell = _cell(row, column_map.get("balance"))
        balance_after = _parse_amount(balance_cell, decimal_style) if balance_cell else None

        transactions.append({
            "date": date_iso,
            "time": time_cell,
            "description": _description(row, column_map),
            "amount": amount,
            "balance_after": balance_after,
            "raw_row": row,
        })

    statement_meta = _extract_statement_meta(
        statement_text, config["statement_meta"], config["statement_type"]
    )
    _apply_running_balance_fallback(statement_meta, transactions, config["statement_meta"])

    return {
        "transactions": transactions,
        "unparsed_rows": unparsed_rows,
        "statement_meta": statement_meta,
    }


def _is_skip_row(row: list[str], skip_patterns: list[re.Pattern]) -> bool:
    joined = " ".join(str(cell) for cell in row if cell)
    return any(pattern.search(joined) for pattern in skip_patterns)


_FOREIGN_AMOUNT_RE = re.compile(r"^[A-Z]{3}\s*[\d,]+\.\d{2}")


def _weak_description(text: str | None) -> bool:
    """A cell that can't be the merchant: blank, a tiny flag token ('EMI'), or a foreign-currency
    amount ('USD 49.99')."""
    if not text or not text.strip():
        return True
    t = text.strip()
    if len(re.sub(r"[^A-Za-z0-9]", "", t)) <= 3:
        return True
    return bool(_FOREIGN_AMOUNT_RE.match(t))


def _description(row: list[str], column_map: dict) -> str:
    """The mapped description cell -- unless it is weak (see _weak_description), in which case the
    longest unclaimed text cell in the row is used instead. docling sometimes emits an extra empty
    column on a page (or a flag/foreign-amount column in another section), which shifts
    negative-indexed columns; this keeps the merchant without any bank-specific branch."""
    mapped = _cell(row, column_map.get("description")) or ""
    if not _weak_description(mapped):
        return mapped
    claimed = set()
    for key, index in column_map.items():
        if key == "description" or index is None:
            continue
        claimed.add(index if index >= 0 else len(row) + index)
    candidates = [
        (len(str(cell).strip()), i)
        for i, cell in enumerate(row)
        if i not in claimed and isinstance(cell, str) and re.search(r"[A-Za-z]{3,}", cell) and not _weak_description(cell)
    ]
    if not candidates:
        return mapped
    return row[max(candidates)[1]].strip()


def _cell(row: list[str], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    value = row[index]
    return value.strip() if isinstance(value, str) else value


def _parse_date(text: str | None, date_format: str) -> str:
    if not text:
        raise ValueError("empty date")
    # Some statements separate a combined date+time cell with a '|' and
    # inconsistent surrounding whitespace (e.g. "22/04/2026| 15:41" on one
    # row, "23/04/2026 | 06:22" on another) -- normalize before strptime.
    cleaned = _DATE_SEPARATOR_CLEANUP_RE.sub(" ", text.strip())
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return datetime.strptime(cleaned, date_format).date().isoformat()


def _parse_amount(text: str | None, decimal_style: str) -> float | None:
    """Parse a currency amount string into a float.

    Both 'indian' and 'plain' decimal_style values currently just strip
    thousands-separator commas -- Python's float() doesn't care how digits
    are grouped once commas are removed. The distinction is kept in the
    schema for forward-compatibility (e.g. a future European style with
    '.' as the thousands separator) even though it's a no-op today.
    """
    if text is None:
        return None
    cleaned = _CURRENCY_STRIP_RE.sub("", str(text)).strip()
    if not cleaned:
        return None
    negative = False
    paren_match = _PAREN_NEGATIVE_RE.match(cleaned)
    if paren_match:
        cleaned = paren_match.group(1)
        negative = True
    value = float(cleaned)
    return -value if negative else value


def _parse_row_amount(
    row: list[str],
    column_map: dict,
    amount_convention: str,
    decimal_style: str,
    credit_description_patterns: list[str] | None = None,
) -> float | None:
    if amount_convention == "separate_debit_credit":
        debit = _parse_amount(_cell(row, column_map.get("debit")), decimal_style)
        credit = _parse_amount(_cell(row, column_map.get("credit")), decimal_style)
        if debit:
            return -abs(debit)
        if credit:
            return abs(credit)
        return None

    if amount_convention == "credit_card_flip":
        raw = _cell(row, column_map.get("amount"))
        if not raw:
            return None
        match = _SIGNED_DECIMAL_AMOUNT_RE.search(raw)
        if not match:
            return None
        magnitude = _parse_amount(match.group(2), decimal_style)
        if not magnitude:
            return None
        is_credit = match.group(1) == "+"
        # Some payment rows print the amount with no leading sign at all --
        # the sign alone isn't fully reliable, so a description keyword
        # (e.g. "PYMT RECD") is an equally valid signal, adapter-declared.
        if not is_credit and credit_description_patterns:
            description = _cell(row, column_map.get("description")) or ""
            is_credit = any(
                re.search(pattern, description, re.IGNORECASE)
                for pattern in credit_description_patterns
            )
        return magnitude if is_credit else -magnitude

    if amount_convention == "single_signed":
        return _parse_amount(_cell(row, column_map.get("amount")), decimal_style)

    if amount_convention == "debit_credit_flag":
        amount = _parse_amount(_cell(row, column_map.get("amount")), decimal_style)
        if amount is None:
            return None
        flag = (_cell(row, column_map.get("dr_cr_flag")) or "").strip().lower()
        if flag.startswith("d"):
            return -abs(amount)
        if flag.startswith("c"):
            return abs(amount)
        return None

    raise ValueError(f"unknown amount_convention: {amount_convention}")


def _extract_statement_meta(text: str, meta_config: dict, statement_type: str) -> dict:
    result = {
        "period_start": None,
        "period_end": None,
        "opening_balance": None,
        "closing_balance": None,
        "account_number": None,
    }

    period_match = _search(meta_config.get("period_regex"), text)
    if period_match:
        result["period_start"] = period_match.group(1)
        result["period_end"] = period_match.group(2)

    # previous_balance_regex/opening_balance_regex being unset (None) is a
    # legitimate "this statement doesn't print that figure" state, handled
    # independently per side -- it must not also suppress closing_balance,
    # which is often reliably printed (e.g. "Total Dues") even when the
    # opening figure isn't available anywhere in the document.
    is_credit_card = statement_type == "credit_card"
    if is_credit_card:
        opening_match = _search(meta_config.get("previous_balance_regex"), text)
        closing_match = _search(meta_config.get("total_dues_regex"), text)
    else:
        opening_match = _search(meta_config.get("opening_balance_regex"), text)
        closing_match = _search(meta_config.get("closing_balance_regex"), text)

    # Card "previous balance"/"total dues" are amounts OWED (grow with
    # spends) -- the opposite polarity from our amount convention
    # (spend=negative). Negate so reconcile.check_balance's single
    # statement-type-agnostic formula (opening + sum(amounts) == closing)
    # holds for credit cards too.
    sign = -1 if is_credit_card else 1

    if opening_match:
        result["opening_balance"] = sign * _sum_regex_groups(opening_match, "plain")
    if closing_match:
        result["closing_balance"] = sign * _sum_regex_groups(closing_match, "plain")

    account_match = _search(meta_config.get("account_number_regex"), text)
    if account_match:
        result["account_number"] = account_match.group(1)

    return result


def _apply_running_balance_fallback(
    statement_meta: dict, transactions: list[dict], meta_config: dict
) -> None:
    """Some statements print no "Opening/Closing Balance" text at all --
    only a running balance_after column per row. When the adapter declares
    balance_source: "running_balance", derive opening/closing from the
    newest/oldest transaction's balance_after instead of a regex match."""
    if meta_config.get("balance_source") != "running_balance" or not transactions:
        return
    newest, oldest = (
        (transactions[0], transactions[-1])
        if meta_config.get("transaction_order", "descending") == "descending"
        else (transactions[-1], transactions[0])
    )
    if statement_meta.get("closing_balance") is None and newest.get("balance_after") is not None:
        statement_meta["closing_balance"] = newest["balance_after"]
    if statement_meta.get("opening_balance") is None and oldest.get("balance_after") is not None:
        statement_meta["opening_balance"] = round(oldest["balance_after"] - oldest["amount"], 2)


def _search(pattern: str | None, text: str):
    if not pattern:
        return None
    return re.search(pattern, text)


def _sum_regex_groups(match: re.Match, decimal_style: str) -> float:
    """Sum every captured group as an amount. A single-group regex just
    returns that one value; multi-group regexes (e.g. several aging
    buckets that together make up a carried-over balance) get summed."""
    total = 0.0
    for group in match.groups():
        value = _parse_amount(group, decimal_style)
        if value is not None:
            total += value
    return round(total, 2)

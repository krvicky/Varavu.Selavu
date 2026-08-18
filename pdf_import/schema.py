"""Adapter JSON config schema and validation.

An adapter is a declarative config that tells the generic parsing engine
(pdf_import.adapter_engine) how to interpret one bank's statement table.
No adapter is ever a Python function per bank -- everything bank-specific
lives in these JSON configs.
"""
import re

STATEMENT_TYPES = {"bank_account", "credit_card"}
AMOUNT_CONVENTIONS = {
    "separate_debit_credit",
    "single_signed",
    "debit_credit_flag",
    "credit_card_flip",
}
DECIMAL_STYLES = {"indian", "plain"}
WRAP_MERGE_MODES = {"empty_date_row_appends_to_previous_description", "none"}

REQUIRED_KEYS = [
    "source_id",
    "bank_label",
    "schema_version",
    "statement_type",
    "header_signature",
    "column_map",
    "date_format",
    "amount_convention",
    "decimal_style",
    "wrap_merge",
    "skip_row_patterns",
    "statement_meta",
]

REQUIRED_COLUMNS_BY_CONVENTION = {
    "separate_debit_credit": ["debit", "credit"],
    "single_signed": ["amount"],
    "debit_credit_flag": ["amount", "dr_cr_flag"],
    # Single amount column; a bare value is an implicit spend (negative),
    # an explicit '+' prefix marks a credit/refund (positive) -- matches
    # how HDFC Diners (and similar card statements) print amounts.
    "credit_card_flip": ["amount"],
}

REQUIRED_STATEMENT_META_KEYS = [
    "period_regex",
    "opening_balance_regex",
    "closing_balance_regex",
    "previous_balance_regex",
    "total_dues_regex",
    "account_number_regex",
]


def validate_adapter(config: dict) -> list[str]:
    """Validate an adapter config. Returns a list of human-readable error
    strings; an empty list means the config is valid."""
    errors: list[str] = []

    for key in REQUIRED_KEYS:
        if key not in config:
            errors.append(f"missing required key: {key}")

    if errors:
        # Can't meaningfully validate deeper without the required keys.
        return errors

    if config["statement_type"] not in STATEMENT_TYPES:
        errors.append(
            f"invalid statement_type: {config['statement_type']!r} "
            f"(must be one of {sorted(STATEMENT_TYPES)})"
        )

    amount_convention = config["amount_convention"]
    if amount_convention not in AMOUNT_CONVENTIONS:
        errors.append(
            f"invalid amount_convention: {amount_convention!r} "
            f"(must be one of {sorted(AMOUNT_CONVENTIONS)})"
        )

    if config["decimal_style"] not in DECIMAL_STYLES:
        errors.append(
            f"invalid decimal_style: {config['decimal_style']!r} "
            f"(must be one of {sorted(DECIMAL_STYLES)})"
        )

    if config["wrap_merge"] not in WRAP_MERGE_MODES:
        errors.append(
            f"invalid wrap_merge: {config['wrap_merge']!r} "
            f"(must be one of {sorted(WRAP_MERGE_MODES)})"
        )

    column_map = config.get("column_map") or {}
    if amount_convention in REQUIRED_COLUMNS_BY_CONVENTION:
        for col in REQUIRED_COLUMNS_BY_CONVENTION[amount_convention]:
            if column_map.get(col) is None:
                errors.append(
                    f"amount_convention {amount_convention!r} requires "
                    f"column_map[{col!r}] to be set"
                )

    if amount_convention == "credit_card_flip" and config["statement_type"] != "credit_card":
        errors.append(
            "amount_convention 'credit_card_flip' requires "
            "statement_type == 'credit_card'"
        )

    for pattern in config.get("skip_row_patterns") or []:
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"invalid regex in skip_row_patterns: {pattern!r} ({exc})")

    statement_meta = config.get("statement_meta") or {}
    for key in REQUIRED_STATEMENT_META_KEYS:
        if key not in statement_meta:
            errors.append(f"missing required statement_meta key: {key}")
    for key, pattern in statement_meta.items():
        if pattern is None:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"invalid regex in statement_meta.{key}: {pattern!r} ({exc})")

    return errors

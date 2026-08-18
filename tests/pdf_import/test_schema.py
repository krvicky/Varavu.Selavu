from pdf_import.schema import validate_adapter


def _base_config(**overrides):
    config = {
        "source_id": "kotak_vignesh",
        "bank_label": "Kotak Mahindra Bank",
        "schema_version": 1,
        "statement_type": "bank_account",
        "header_signature": ["Date", "Narration", "Withdrawal", "Deposit", "Balance"],
        "column_map": {
            "date": 0, "time": None, "description": 1, "reference": None,
            "debit": 2, "credit": 3, "amount": None, "dr_cr_flag": None, "balance": 4,
        },
        "date_format": "%d/%m/%y",
        "amount_convention": "separate_debit_credit",
        "decimal_style": "indian",
        "wrap_merge": "empty_date_row_appends_to_previous_description",
        "skip_row_patterns": ["OPENING BALANCE", "^TOTAL"],
        "statement_meta": {
            "period_regex": r"Period[:\s]*([0-9/\-]+)\s*(?:to|-)\s*([0-9/\-]+)",
            "opening_balance_regex": r"Opening Balance[:\s]*([\d,]+\.\d{2})",
            "closing_balance_regex": r"Closing Balance[:\s]*([\d,]+\.\d{2})",
            "previous_balance_regex": None,
            "total_dues_regex": None,
            "account_number_regex": r"Account No[:\s]*(\d{4,})",
        },
    }
    config.update(overrides)
    return config


def test_valid_config_has_no_errors():
    assert validate_adapter(_base_config()) == []


def test_missing_required_key_is_reported():
    config = _base_config()
    del config["date_format"]
    errors = validate_adapter(config)
    assert any("date_format" in e for e in errors)


def test_invalid_statement_type_enum_is_reported():
    config = _base_config(statement_type="checking")
    errors = validate_adapter(config)
    assert any("statement_type" in e for e in errors)


def test_invalid_amount_convention_enum_is_reported():
    config = _base_config(amount_convention="bogus")
    errors = validate_adapter(config)
    assert any("amount_convention" in e for e in errors)


def test_separate_debit_credit_requires_debit_and_credit_columns():
    config = _base_config()
    config["column_map"]["debit"] = None
    errors = validate_adapter(config)
    assert any("debit" in e for e in errors)


def test_single_signed_requires_amount_column():
    config = _base_config(amount_convention="single_signed")
    config["column_map"]["amount"] = None
    errors = validate_adapter(config)
    assert any("amount" in e for e in errors)


def test_debit_credit_flag_requires_amount_and_flag_columns():
    config = _base_config(amount_convention="debit_credit_flag")
    errors = validate_adapter(config)
    assert any("amount" in e for e in errors)
    assert any("dr_cr_flag" in e for e in errors)


def test_credit_card_flip_requires_credit_card_statement_type():
    config = _base_config(amount_convention="credit_card_flip", statement_type="bank_account")
    errors = validate_adapter(config)
    assert any("credit_card_flip" in e and "statement_type" in e for e in errors)


def test_invalid_regex_is_reported():
    config = _base_config()
    config["skip_row_patterns"] = ["("]
    errors = validate_adapter(config)
    assert any("skip_row_patterns" in e for e in errors)

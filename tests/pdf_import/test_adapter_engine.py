import json
from pathlib import Path

from pdf_import.adapter_engine import apply_adapter
from pdf_import.reconcile import reconcile_or_unavailable
from pdf_import.schema import validate_adapter

ADAPTERS_DIR = Path(__file__).resolve().parents[2] / "pdf_import" / "adapters"


def _config(**overrides):
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
        "skip_row_patterns": ["OPENING BALANCE", "^TOTAL", "Page \\d+"],
        "statement_meta": {
            "period_regex": r"Statement Period[:\s]*([0-9/]+)\s*(?:to|-)\s*([0-9/]+)",
            "opening_balance_regex": r"Opening Balance[:\s]*([\d,]+\.\d{2})",
            "closing_balance_regex": r"Closing Balance[:\s]*([\d,]+\.\d{2})",
            "previous_balance_regex": r"Previous Balance[:\s]*([\d,]+\.\d{2})",
            "total_dues_regex": r"Total Dues[:\s]*([\d,]+\.\d{2})",
            "account_number_regex": r"Account No[:\s]*(\d{4,})",
        },
    }
    config.update(overrides)
    return config


def test_separate_debit_credit_debit_is_negative_credit_is_positive():
    table = [
        ["01/07/26", "ATM WDL", "500.00", "", "9500.00"],
        ["02/07/26", "SALARY CREDIT", "", "20000.00", "29500.00"],
    ]
    result = apply_adapter(table, "", _config())
    txns = result["transactions"]
    assert len(txns) == 2
    assert txns[0]["amount"] == -500.0
    assert txns[1]["amount"] == 20000.0


def test_indian_comma_grouped_amount_parses_correctly():
    table = [["03/07/26", "BIG PURCHASE", "1,23,456.78", "", "0.00"]]
    result = apply_adapter(table, "", _config())
    assert result["transactions"][0]["amount"] == -123456.78


def test_wrap_merge_appends_empty_date_row_to_previous_description():
    table = [
        ["04/07/26", "AMAZON PURCHASE", "999.00", "", "8501.00"],
        ["", "REF NO 123456789", "", "", ""],
    ]
    result = apply_adapter(table, "", _config())
    assert len(result["transactions"]) == 1
    assert result["transactions"][0]["description"] == "AMAZON PURCHASE REF NO 123456789"


def test_skip_row_patterns_drop_opening_balance_and_total_rows():
    table = [
        ["", "OPENING BALANCE", "", "", "10000.00"],
        ["01/07/26", "ATM WDL", "500.00", "", "9500.00"],
        ["", "TOTAL", "500.00", "0.00", ""],
        ["", "Page 1 of 2", "", "", ""],
    ]
    result = apply_adapter(table, "", _config())
    assert len(result["transactions"]) == 1
    assert result["unparsed_rows"] == []


def test_unparseable_row_is_never_dropped():
    table = [
        ["01/07/26", "GOOD ROW", "500.00", "", "9500.00"],
        ["NOT-A-DATE", "BAD ROW", "", "", ""],
    ]
    result = apply_adapter(table, "", _config())
    assert len(result["transactions"]) == 1
    assert len(result["unparsed_rows"]) == 1
    assert result["unparsed_rows"][0]["reason"] == "couldn't parse this row"
    assert result["unparsed_rows"][0]["raw_row"] == ["NOT-A-DATE", "BAD ROW", "", "", ""]


def test_single_signed_amount_convention():
    config = _config(amount_convention="single_signed")
    config["column_map"] = dict(config["column_map"], amount=2, debit=None, credit=None)
    table = [
        ["01/07/26", "SPEND", "-500.00", "9500.00"],
        ["02/07/26", "REFUND", "1,200.50", "10700.50"],
    ]
    config["column_map"]["balance"] = 3
    result = apply_adapter(table, "", config)
    txns = result["transactions"]
    assert txns[0]["amount"] == -500.0
    assert txns[1]["amount"] == 1200.5


def test_debit_credit_flag_amount_convention():
    config = _config(amount_convention="debit_credit_flag")
    config["column_map"] = dict(config["column_map"], amount=2, dr_cr_flag=3, debit=None, credit=None, balance=4)
    table = [
        ["05/07/26", "SPEND", "500.00", "Dr", "9000.00"],
        ["06/07/26", "PAYMENT", "1000.00", "Cr", "10000.00"],
    ]
    result = apply_adapter(table, "", config)
    txns = result["transactions"]
    assert txns[0]["amount"] == -500.0
    assert txns[1]["amount"] == 1000.0


def test_credit_card_flip_spends_negative_payments_positive():
    # Real HDFC Diners layout: a single amount column where a bare value is
    # an implicit spend (no sign printed) and an explicit '+' prefix marks a
    # credit/refund. No separate debit/credit columns on this statement.
    config = _config(statement_type="credit_card", amount_convention="credit_card_flip")
    config["column_map"] = dict(config["column_map"], amount=2, debit=None, credit=None, balance=None)
    table = [
        ["07/07/26", "AMAZON.IN", "2,500.00"],           # bare -> spend -> negative
        ["10/07/26", "REFUND FROM MERCHANT", "+ 5,000.00"],  # explicit '+' -> credit -> positive
    ]
    result = apply_adapter(table, "", config)
    txns = result["transactions"]
    assert txns[0]["amount"] == -2500.0
    assert txns[1]["amount"] == 5000.0


def test_credit_card_flip_strips_currency_glyph_artifact_before_digits():
    # Some PDF exports render the rupee glyph as a stray 'C' before the
    # digits (a font-substitution artifact, not a real letter in the text).
    config = _config(statement_type="credit_card", amount_convention="credit_card_flip")
    config["column_map"] = dict(config["column_map"], amount=2, debit=None, credit=None, balance=None)
    table = [["07/07/26", "SWIGGY", "C 387.00"]]
    result = apply_adapter(table, "", config)
    assert result["transactions"][0]["amount"] == -387.0


def test_credit_card_flip_ignores_reward_points_merged_into_the_same_cell():
    # Real quirk: some rows merge "+ <reward points>" and the currency
    # amount into ONE cell (e.g. "+ 35 C 1,101.50" -- rewards earned on an
    # ordinary spend, not a credit). The '+' belongs to the points, not the
    # amount -- naively concatenating digits after stripping whitespace
    # would misparse this as +351101.50. Only a proper decimal-formatted
    # amount (with its OWN immediately-preceding sign, if any) counts.
    config = _config(statement_type="credit_card", amount_convention="credit_card_flip")
    config["column_map"] = dict(config["column_map"], amount=2, debit=None, credit=None, balance=None)
    table = [["21/06/26", "THIRD WAVE COFFEE", "+ 45 C 1,453.98"]]
    result = apply_adapter(table, "", config)
    assert result["transactions"][0]["amount"] == -1453.98


def test_credit_card_flip_real_payment_still_detected_as_credit_when_rewards_column_is_separate():
    config = _config(statement_type="credit_card", amount_convention="credit_card_flip")
    config["column_map"] = dict(config["column_map"], amount=2, debit=None, credit=None, balance=None)
    table = [["25/05/26", "ONLINE PYMT RECD", "+ C 2,31,852.00"]]
    result = apply_adapter(table, "", config)
    assert result["transactions"][0]["amount"] == 231852.0


def test_credit_card_flip_detects_payment_via_description_keyword_when_amount_has_no_sign():
    # Real quirk: some payment rows print the amount with NO leading '+' at
    # all (e.g. "C 1,85,716.00" for an "ONLINE PYMT RECD" row) -- the sign
    # alone isn't a reliable signal. credit_description_patterns lets the
    # adapter declare description keywords that also mark a row as credit.
    config = _config(statement_type="credit_card", amount_convention="credit_card_flip")
    config["column_map"] = dict(config["column_map"], amount=2, debit=None, credit=None, balance=None)
    config["credit_description_patterns"] = ["PYMT RECD", "PAYMENT RECEIVED"]
    table = [["01/05/26", "ONLINE PYMT RECD-C162HCK1IWBXPA", "C 1,85,716.00"]]
    result = apply_adapter(table, "", config)
    assert result["transactions"][0]["amount"] == 185716.0


def test_credit_card_flip_ordinary_spend_unaffected_by_credit_description_patterns():
    config = _config(statement_type="credit_card", amount_convention="credit_card_flip")
    config["column_map"] = dict(config["column_map"], amount=2, debit=None, credit=None, balance=None)
    config["credit_description_patterns"] = ["PYMT RECD", "PAYMENT RECEIVED"]
    table = [["07/07/26", "AMAZON.IN", "2,500.00"]]
    result = apply_adapter(table, "", config)
    assert result["transactions"][0]["amount"] == -2500.0


def test_date_with_pipe_separated_time_is_normalized_before_parsing():
    config = _config(date_format="%d/%m/%Y %H:%M")
    config["column_map"] = dict(config["column_map"], debit=2, credit=3)
    table = [
        ["22/04/2026| 15:41", "NO SPACE BEFORE PIPE", "100.00", "", "500.00"],
        ["23/04/2026 | 06:22", "SPACE BOTH SIDES OF PIPE", "50.00", "", "450.00"],
    ]
    result = apply_adapter(table, "", config)
    assert len(result["transactions"]) == 2
    assert result["transactions"][0]["date"] == "2026-04-22"
    assert result["transactions"][1]["date"] == "2026-04-23"


def test_statement_meta_extraction_bank_account():
    text = (
        "Statement Period: 01/07/2026 to 31/07/2026\n"
        "Opening Balance: 10,000.00\n"
        "Closing Balance: 15,432.10\n"
        "Account No: 1234567890\n"
    )
    result = apply_adapter([], text, _config())
    meta = result["statement_meta"]
    assert meta["period_start"] == "01/07/2026"
    assert meta["period_end"] == "31/07/2026"
    assert meta["opening_balance"] == 10000.0
    assert meta["closing_balance"] == 15432.10
    assert meta["account_number"] == "1234567890"


def test_statement_meta_extraction_credit_card_negates_dues_to_match_spend_sign_convention():
    # "Previous Balance"/"Total Dues" are amounts OWED (grow with spends) --
    # the opposite polarity from our amount convention (spend=negative).
    # Negating them here is what lets reconcile.check_balance's single
    # statement-type-agnostic formula (opening + sum(amounts) == closing)
    # hold for credit cards too.
    text = (
        "Statement Period: 01/07/2026 to 31/07/2026\n"
        "Previous Balance: 3,200.00\n"
        "Total Dues: 4,500.00\n"
        "Account No: 9988776655\n"
    )
    config = _config(statement_type="credit_card", amount_convention="credit_card_flip")
    result = apply_adapter([], text, config)
    meta = result["statement_meta"]
    assert meta["opening_balance"] == -3200.0
    assert meta["closing_balance"] == -4500.0


def test_statement_meta_credit_card_closing_balance_available_even_without_opening():
    # Some credit-card statements print "Total Dues" but no "Previous
    # Balance" figure at all -- closing_balance must still populate
    # independently rather than silently staying None because opening
    # couldn't be found.
    text = "Total Dues: 4,500.00\n"
    config = _config(statement_type="credit_card", amount_convention="credit_card_flip")
    config["statement_meta"] = dict(config["statement_meta"], previous_balance_regex=None)
    result = apply_adapter([], text, config)
    meta = result["statement_meta"]
    assert meta["opening_balance"] is None
    assert meta["closing_balance"] == -4500.0


def test_statement_meta_previous_balance_regex_sums_multiple_aging_buckets():
    # HDFC Diners prints no single "previous balance" figure -- only four
    # aging buckets (over-limit/3mo/2mo/1mo) that together represent the
    # carried-over balance. A regex with multiple capture groups sums them.
    text = "OVER LIMIT C 0.00 | 3 MONTHS C 0.00 | 2 MONTHS C 120.50 | 1 MONTH C 79.50"
    config = _config(
        statement_type="credit_card",
        amount_convention="credit_card_flip",
    )
    config["statement_meta"] = dict(
        config["statement_meta"],
        previous_balance_regex=(
            r"OVER LIMIT C\s*([\d,]+\.\d{2})\s*\|\s*3 MONTHS C\s*([\d,]+\.\d{2})"
            r"\s*\|\s*2 MONTHS C\s*([\d,]+\.\d{2})\s*\|\s*1 MONTH C\s*([\d,]+\.\d{2})"
        ),
    )
    result = apply_adapter([], text, config)
    assert result["statement_meta"]["opening_balance"] == -200.0


def test_running_balance_derivation_when_no_opening_closing_text_present():
    # Some bank statements print no "Opening/Closing Balance" text at all --
    # only a running BALANCE(rs) column per row, newest transaction first.
    # balance_source="running_balance" derives opening/closing from that.
    config = _config()
    config["statement_meta"] = dict(
        config["statement_meta"],
        opening_balance_regex="NEVER MATCHES ANYTHING XYZ",
        closing_balance_regex="NEVER MATCHES ANYTHING XYZ",
        balance_source="running_balance",
        transaction_order="descending",
    )
    table = [
        ["03/07/26", "NEWEST", "500.00", "", "9500.00"],   # closing balance after this txn
        ["02/07/26", "MIDDLE", "", "1000.00", "10000.00"],
        ["01/07/26", "OLDEST", "200.00", "", "9000.00"],   # balance before this: 9000 + 200 = 9200
    ]
    result = apply_adapter(table, "", config)
    meta = result["statement_meta"]
    assert meta["closing_balance"] == 9500.0
    assert meta["opening_balance"] == 9200.0


def test_axis_adapter_config_handles_real_statement_layout():
    # Uses the SHIPPED axis_vignesh.json (not a synthetic config) against an
    # Axis-shaped table. Axis quirks: OPENING/CLOSING BALANCE and TRANSACTION
    # TOTAL are table rows with blank dates, and the balance-row label is
    # repeated into the Debit/Credit cells -- they must be skipped, never
    # parsed as transactions or wrap-merged into a real description.
    config = json.loads((ADAPTERS_DIR / "axis_vignesh.json").read_text(encoding="utf-8"))
    assert validate_adapter(config) == []
    table = [
        ["Tran Date", "Chq No", "Particulars", "Debit", "Credit", "Balance", "Init. Br"],
        ["", "", "OPENING BALANCE", "OPENING BALANCE", "OPENING BALANCE", "10000.00", "10000.00"],
        ["01-07-2026", "", "SB:000000000000000:Int.Pd:01-04-2026 to 30- 06-2026", "", "100.00", "10100.00", "258"],
        ["15-07-2026", "", "UPI/P2M/519912345678/SOME MERCHANT", "250.00", "", "9850.00", "248"],
        ["", "", "TRANSACTION TOTAL", "250.00", "100.00", "", ""],
        ["", "", "CLOSING BALANCE", "CLOSING BALANCE", "CLOSING BALANCE", "9850.00", ""],
    ]
    text = (
        "Statement of Axis Account No :916010001977067 for the period (From : 01-07-2026  To : 31-07-2026)\n"
        "| | | OPENING BALANCE | OPENING BALANCE | OPENING BALANCE | 10000.00 | 10000.00 |\n"
        "| | | CLOSING BALANCE | CLOSING BALANCE | CLOSING BALANCE | 9850.00 | |\n"
    )
    result = apply_adapter(table, text, config)
    txns = result["transactions"]
    assert [t["amount"] for t in txns] == [100.0, -250.0]
    assert [t["date"] for t in txns] == ["2026-07-01", "2026-07-15"]
    assert result["unparsed_rows"] == []
    meta = result["statement_meta"]
    assert meta["period_start"] == "01-07-2026"
    assert meta["period_end"] == "31-07-2026"
    assert meta["opening_balance"] == 10000.0
    assert meta["closing_balance"] == 9850.0
    assert meta["account_number"] == "916010001977067"
    recon = reconcile_or_unavailable(txns, meta["opening_balance"], meta["closing_balance"])
    assert recon["status"] == "ok"


def test_blank_description_cell_falls_back_to_unclaimed_text_cell():
    """docling sometimes emits an extra empty column on one page; with negative column indices the
    mapped description cell is then blank while the merchant sits one column over."""
    config = _config(
        statement_type="credit_card",
        column_map={"date": 0, "time": None, "description": -4, "reference": None, "debit": None,
                    "credit": None, "amount": -2, "dr_cr_flag": None, "balance": None},
        date_format="%d/%m/%Y %H:%M",
        amount_convention="credit_card_flip",
        wrap_merge="none",
        skip_row_patterns=[],
    )
    table = [
        ["17/06/2026| 14:54", "", "ZOMATOGURGAON", "+ 15 C 504.33", "l"],  # extra empty column: mapped cell blank
        ["18/06/2026| 06:25", "SWIGGY INSTAMARTGURGAON", "", "C 152.00", "l"],  # normal row: unchanged
        ["21/06/2026| 14:39", "EMI", "UNIQLO INDIA PRIVATE LIBENGALURU", "+ 630 C 18,980.00", "l"],  # flag token lands in the slot
        ["24/04/2026 | 04:58", "UPG*PAYMENTICO.COMCYPRUS", "USD 49.99", "+ 155", "C 4,732.08", "l"],  # foreign amount lands in the slot
        ["29/04/2026 | 22:41", "EMI", "UPG*PAYMENTICO.COMCYPRUS", "USD 49.99 + 155", "C 4,773.02", "l"],  # 6-col normal: unchanged
    ]
    txns = apply_adapter(table, "", config)["transactions"]
    assert [t["description"] for t in txns] == [
        "ZOMATOGURGAON", "SWIGGY INSTAMARTGURGAON", "UNIQLO INDIA PRIVATE LIBENGALURU",
        "UPG*PAYMENTICO.COMCYPRUS", "UPG*PAYMENTICO.COMCYPRUS",
    ]


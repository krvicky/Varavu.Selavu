"""End-to-end tests against the real launch-bank sample statements.

Skipped entirely unless bank_statements_for_testing/ is present locally --
it's gitignored (real financial data), so this suite only runs on a
machine that has the samples, never in CI or on a fresh clone.

The HDFC Diners samples are additionally password-protected; those specific
assertions are skipped unless DINERS_TEST_PASSWORD is set in the
environment. The password is never hardcoded here.
"""
import json
import os
from pathlib import Path

import pytest

from pdf_import.decrypt import decrypted_copy
from pdf_import.docling_pipeline import parse_pdf
from pdf_import.adapter_engine import apply_adapter
from pdf_import.reconcile import reconcile_or_unavailable

ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = ROOT / "bank_statements_for_testing"
ADAPTERS_DIR = ROOT / "pdf_import" / "adapters"

pytestmark = pytest.mark.skipif(
    not SAMPLES_DIR.exists(), reason="private sample statements not present locally"
)


def _load_adapter(source_id: str) -> dict:
    return json.loads((ADAPTERS_DIR / f"{source_id}.json").read_text(encoding="utf-8"))


def _parse(path: Path, config: dict, password: str | None = None) -> dict:
    if password:
        with decrypted_copy(path, password) as tmp:
            table, text, ocr_used = parse_pdf(tmp, config["header_signature"])
    else:
        table, text, ocr_used = parse_pdf(path, config["header_signature"])
    result = apply_adapter(table, text, config)
    result["ocr_used"] = ocr_used
    return result


KOTAK_SAMPLES = [
    ("bank_statements_for_testing/01_Kotak/2026_05.pdf", 137),
    ("bank_statements_for_testing/01_Kotak/2026_06.pdf", 137),
    ("bank_statements_for_testing/01_Kotak/2026_07.pdf", 170),
]


@pytest.mark.parametrize("relative_path,expected_txn_count", KOTAK_SAMPLES)
def test_kotak_sample_reconciles_with_expected_transaction_count(relative_path, expected_txn_count):
    config = _load_adapter("kotak_vignesh")
    result = _parse(ROOT / relative_path, config)
    assert len(result["transactions"]) == expected_txn_count
    meta = result["statement_meta"]
    recon = reconcile_or_unavailable(
        result["transactions"], meta["opening_balance"], meta["closing_balance"]
    )
    assert recon["status"] == "ok"
    assert result["ocr_used"] is False


def test_kotak_reimport_is_deterministic():
    # Re-running the parse on the same file must produce the same
    # transaction set -- this is what makes dedupe-on-reimport a no-op.
    config = _load_adapter("kotak_vignesh")
    path = ROOT / "bank_statements_for_testing/01_Kotak/2026_05.pdf"
    first = _parse(path, config)
    second = _parse(path, config)
    assert [t["amount"] for t in first["transactions"]] == [t["amount"] for t in second["transactions"]]
    assert [t["date"] for t in first["transactions"]] == [t["date"] for t in second["transactions"]]


AXIS_SAMPLES = [
    ("bank_statements_for_testing/03_Axis/2026_07.pdf", 2),
]


@pytest.mark.parametrize("relative_path,expected_txn_count", AXIS_SAMPLES)
def test_axis_sample_reconciles_with_expected_transaction_count(relative_path, expected_txn_count):
    config = _load_adapter("axis_vignesh")
    result = _parse(ROOT / relative_path, config)
    assert len(result["transactions"]) == expected_txn_count
    assert result["unparsed_rows"] == []
    meta = result["statement_meta"]
    recon = reconcile_or_unavailable(
        result["transactions"], meta["opening_balance"], meta["closing_balance"]
    )
    assert recon["status"] == "ok"
    assert result["ocr_used"] is False


DINERS_SAMPLES = [
    ("bank_statements_for_testing/04_Diners/Diners_05-2026.pdf", 87),
    ("bank_statements_for_testing/04_Diners/Diners_06-2026.pdf", 109),
    ("bank_statements_for_testing/04_Diners/Diners_07-2026.pdf", 86),
]


@pytest.fixture
def diners_password():
    password = os.environ.get("DINERS_TEST_PASSWORD")
    if not password:
        pytest.skip("DINERS_TEST_PASSWORD not set")
    return password


@pytest.mark.parametrize("relative_path,expected_txn_count", DINERS_SAMPLES)
def test_diners_sample_spends_negative_payments_positive(relative_path, expected_txn_count, diners_password):
    config = _load_adapter("hdfc_diners_vignesh")
    result = _parse(ROOT / relative_path, config, diners_password)
    assert len(result["transactions"]) == expected_txn_count
    amounts = [t["amount"] for t in result["transactions"]]
    assert any(a < 0 for a in amounts), "expected at least one spend (negative)"
    meta = result["statement_meta"]
    # Closing balance (Current Dues) is reliably printed; opening (Previous
    # Balance) is not printed anywhere on this statement -- reconciliation
    # is honestly "unavailable", never a false ok or a false failure.
    assert meta["closing_balance"] is not None
    recon = reconcile_or_unavailable(
        result["transactions"], meta["opening_balance"], meta["closing_balance"]
    )
    assert recon["status"] == "unavailable"
    assert result["ocr_used"] is False


def test_diners_may_statement_has_a_detected_payment(diners_password):
    # The May statement has a known real payment row that prints with no
    # leading '+' sign -- only the "PYMT RECD" description keyword
    # distinguishes it. Regression guard for that specific bug.
    config = _load_adapter("hdfc_diners_vignesh")
    result = _parse(
        ROOT / "bank_statements_for_testing/04_Diners/Diners_05-2026.pdf", config, diners_password
    )
    payments = [t for t in result["transactions"] if t["amount"] > 0]
    assert len(payments) >= 1

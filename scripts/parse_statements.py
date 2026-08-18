"""Parse the private sample statements into experiments/transactions.csv.

Usage:
    python scripts/parse_statements.py [--samples DIR] [--out FILE] [--password PW]

Walks bank_statements_for_testing/<folder>/*.pdf, picks the adapter from the
folder name (see FOLDER_ADAPTERS), runs the same pure pipeline the end-to-end
tests use (decrypt -> docling parse -> adapter engine), and applies the app's
current default rules (against a throwaway SQLite DB) so the CSV also shows
today's rule coverage.

Diners statements are password protected: pass --password or set
DINERS_TEST_PASSWORD. Files that can't be opened are reported and skipped.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pdf_import.adapter_engine import apply_adapter  # noqa: E402
from pdf_import.decrypt import decrypted_copy, is_encrypted  # noqa: E402
from pdf_import.docling_pipeline import parse_pdf  # noqa: E402

FOLDER_ADAPTERS = {
    "01_Kotak": ("kotak_vignesh", "Vignesh Kotak Bank"),
    "04_Diners": ("hdfc_diners_vignesh", "Vignesh HDFC Diners"),
}
FIELDS = [
    "file", "source_id", "source_name", "date", "time", "description", "amount", "balance_after",
    "rule_id", "rule_category", "rule_subcategory", "rule_flow_type", "rule_classification", "rule_confidence",
]


def load_adapter(source_id: str) -> dict:
    return json.loads((ROOT / "pdf_import" / "adapters" / f"{source_id}.json").read_text(encoding="utf-8"))


def parse_one(path: Path, config: dict, password: str | None) -> dict:
    if is_encrypted(path):
        if not password:
            raise RuntimeError("encrypted and no password given (use --password or DINERS_TEST_PASSWORD)")
        with decrypted_copy(path, password) as tmp:
            table, text, _ocr = parse_pdf(tmp, config["header_signature"])
    else:
        table, text, _ocr = parse_pdf(path, config["header_signature"])
    return apply_adapter(table, text, config)


def rules_conn(tmpdir: Path):
    """A throwaway app DB seeded with the current default rules."""
    import app as app_module

    app_module.DB_PATH = tmpdir / "rules.sqlite3"
    app_module.UPLOAD_DIR = tmpdir / "inbox"
    app_module.ARCHIVE_DIR = tmpdir / "archive"
    app_module.LOGO_UPLOAD_DIR = tmpdir / "logos"
    app_module.init_db(seed=True)
    return app_module, app_module.db()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", default=str(ROOT / "bank_statements_for_testing"))
    ap.add_argument("--out", default=str(ROOT / "experiments" / "transactions.csv"))
    ap.add_argument("--password", default=os.environ.get("DINERS_TEST_PASSWORD"))
    ap.add_argument("--adapter", help="force this adapter for every file")
    args = ap.parse_args()

    samples = Path(args.samples)
    if not samples.is_dir():
        print(f"samples dir not found: {samples}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        app_module, conn = rules_conn(Path(td))
        rows: list[dict] = []
        for pdf in sorted(samples.rglob("*.pdf")):
            folder = pdf.parent.name
            if args.adapter:
                source_id, source_name = args.adapter, args.adapter
            elif folder in FOLDER_ADAPTERS:
                source_id, source_name = FOLDER_ADAPTERS[folder]
            else:
                print(f"skip {pdf.relative_to(samples)}: no adapter mapped for folder {folder!r}")
                continue
            config = load_adapter(source_id)
            try:
                result = parse_one(pdf, config, args.password)
            except Exception as exc:  # noqa: BLE001 - report and keep going
                print(f"FAIL {pdf.relative_to(samples)}: {type(exc).__name__}: {exc}")
                continue
            txns = result["transactions"]
            unparsed = result.get("unparsed_rows", [])
            print(f"ok   {pdf.relative_to(samples)}: {len(txns)} transactions, {len(unparsed)} unparsed rows")
            for u in unparsed:
                print(f"       unparsed row {u.get('row_index')}: {u.get('reason')} :: {u.get('raw_row')}")
            for t in txns:
                tx = {"description": t["description"], "amount": t["amount"], "source_name": source_name}
                app_module.apply_rules(conn, tx)
                rows.append({
                    "file": str(pdf.relative_to(samples)).replace("\\", "/"),
                    "source_id": source_id,
                    "source_name": source_name,
                    "date": t["date"],
                    "time": t.get("time") or "",
                    "description": t["description"],
                    "amount": t["amount"],
                    "balance_after": t.get("balance_after") if t.get("balance_after") is not None else "",
                    "rule_id": tx.get("rule_id") or "",
                    "rule_category": tx.get("category") or "",
                    "rule_subcategory": tx.get("subcategory") or "",
                    "rule_flow_type": tx.get("flow_type") or "",
                    "rule_classification": tx.get("classification") or "",
                    "rule_confidence": tx.get("confidence") or "",
                })
        conn.close()

    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    matched = sum(1 for r in rows if r["rule_id"])
    print(f"\nwrote {len(rows)} transactions to {out}  (current rules match {matched}, {matched / len(rows):.0%})" if rows else "\nno transactions written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

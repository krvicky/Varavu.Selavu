#!/usr/bin/env python3
"""
Kanakku Varavu.Selavu Dashboard v1.

Dependency-light local web app:
- stdlib HTTP server
- SQLite source of truth
- one shared login
- CSV/JSON upload import
- PDF upload intake with password-to-use-now only, never stored
- conservative rules, overrides, review queue, baselines, audit log
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "spending_control.sqlite3"
UPLOAD_DIR = ROOT / "imports" / "inbox"
ARCHIVE_DIR = ROOT / "imports" / "archive"
LOGO_UPLOAD_DIR = ROOT / "assets" / "uploads" / "bank-logos"
SESSION_COOKIE = "kanakku_session"
SEED_BATCH_ID = "seed_may_2026"
SEED_DATA_SETTING = "use_seed_data"

PARENT_CATEGORIES = [
    "Home & Utilities",
    "Food",
    "Baby",
    "Idli",
    "Health",
    "Transport",
    "Lifestyle",
    "Shopping",
    "Travel",
    "Investments",
]

FLOW_TYPES = ["spend", "income", "refund", "transfer", "card_payment", "reversal", "fee", "unknown"]
CLASSIFICATIONS = ["fixed", "baseline_variable", "controllable", "one_off", "excluded"]
PAYERS = ["Vignesh", "Jananiya"]
SOURCES = [
    ("axis_vignesh", "Vignesh Axis Bank", "Vignesh", "active"),
    ("kotak_vignesh", "Vignesh Kotak Mahindra Bank", "Vignesh", "secondary"),
    ("hdfc_diners_vignesh", "Vignesh HDFC Diners", "Vignesh", "active"),
    ("hdfc_jananiya", "Jananiya HDFC Bank", "Jananiya", "active"),
    ("yes_jananiya", "Jananiya Yes Bank", "Jananiya", "inactive"),
]

# Dashboard coverage is deliberately a small config list for v1. Aliases keep
# older imports (for example, the original shorter Kotak label) represented.
EXPECTED_ACCOUNTS = [
    ("Vignesh Axis Bank", ("Vignesh Axis Bank",)),
    ("Vignesh Kotak Mahindra Bank", ("Vignesh Kotak Mahindra Bank", "Vignesh Kotak Bank")),
    ("Jananiya HDFC Bank", ("Jananiya HDFC Bank",)),
    ("Jananiya Yes Bank", ("Jananiya Yes Bank",)),
    ("Vignesh HDFC Diners", ("Vignesh HDFC Diners",)),
]

ACCOUNT_ALIASES = {
    source_id: tuple(alias for display, aliases in EXPECTED_ACCOUNTS if display == source_name for alias in aliases) or (source_name,)
    for source_id, source_name, _payer, _status in SOURCES
}

# Single source of truth for bank marks. HDFC Diners intentionally precedes HDFC.
BANK_ASSETS = (
    ("hdfc diners", "hdfc-diners.svg", "DC", "HDFC Diners"),
    ("axis", "axis.svg", "AX", "Axis Bank"),
    ("kotak", "kotak.svg", "KO", "Kotak Mahindra Bank"),
    ("hdfc", "hdfc.svg", "HD", "HDFC Bank"),
    ("yes", "yes.svg", "YES", "Yes Bank"),
)


def bank_asset(name: str) -> tuple[str | None, str, str]:
    lowered = (name or "").lower()
    for keyword, filename, monogram, alt in BANK_ASSETS:
        if keyword in lowered:
            return filename, monogram, alt
    words = [word for word in (name or "Bank").split() if word.lower() not in {"bank", "vignesh", "jananiya"}]
    return None, "".join(word[0] for word in words[:2]).upper() or "BK", name or "Bank"


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


APP_USER = env("KANAKKU_USER", "vignesh")
APP_PASSWORD = env("KANAKKU_PASSWORD", "change-me")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def money(value) -> str:
    try:
        number = float(value or 0)
    except Exception:
        number = 0
    sign = "−" if number < 0 else ""
    number = abs(round(number))
    s = str(int(number))
    if len(s) > 3:
        last = s[-3:]
        rest = s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        s = ",".join(parts + [last])
    return f"{sign}₹{s}"


def human_date(value: str | None) -> str:
    try:
        parsed = datetime.strptime((value or "")[:10], "%Y-%m-%d")
        return f"{parsed.day} {parsed.strftime('%B')} {parsed.year}"
    except ValueError:
        return value or ""


def human_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%-d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return value


def human_month(value: str | None) -> str:
    try:
        return datetime.strptime(value or "", "%Y-%m").strftime("%B %Y")
    except ValueError:
        return value or ""


HUMAN_LABELS = {
    "description_contains": "Description contains", "exact_merchant": "Exact merchant",
    "normalized_merchant": "Similar merchant", "controllable": "Controllable", "fixed": "Fixed",
    "excluded": "Excluded", "income": "Money in", "spend": "Money out", "fee": "Fee",
    "refund": "Refund", "reversal": "Reversal", "transfer": "Transfer", "unknown": "Needs review",
    "initial_decision": "Set manually", "dashboard": "Set manually", "import": "From statement import",
    "pending_pdf_extraction": "Waiting for extraction", "extracting": "Extracting",
    "needs_parser": "Needs parser", "failed": "Failed", "committed": "Imported", "completed": "Imported",
    "csv": "CSV", "json": "JSON", "pdf": "PDF",
}


def human_label(value: str | None) -> str:
    raw = (value or "").strip()
    return HUMAN_LABELS.get(raw, raw.replace("_", " ").capitalize())


def empty_state(icon: str, line: str, action: str = "", href: str = "") -> str:
    button = f'<a class="btn secondary" href="{html.escape(href)}">{html.escape(action)}</a>' if action and href else ""
    return f'<div class="empty-state"><span class="empty-icon"><i data-lucide="{html.escape(icon)}"></i></span><p>{html.escape(line)}</p>{button}</div>'


def month_control(name: str, value: str) -> str:
    return (f'<div class="human-control" data-month-control><button type="button" data-shift="-1" aria-label="Previous month"><i data-lucide="chevron-left"></i></button>'
            f'<input type="text" value="{html.escape(human_month(value))}" readonly aria-label="Month"><input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'
            f'<button type="button" data-shift="1" aria-label="Next month"><i data-lucide="chevron-right"></i></button></div>')


def date_control(name: str, value: str) -> str:
    return f'<input type="text" data-date-display value="{html.escape(human_date(value))}" aria-label="{html.escape(name.replace("_", " "))}"><input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'


REASON_LABELS = {
    "creep-watch driver": "Driving budget drift",
    "unusual amount": "Unusually large for this category",
    "low confidence": "Not sure where this belongs",
    "ambiguous merchant": "Merchant unclear",
    "missing category": "Not sure where this belongs",
    "unknown flow_type": "Money direction unclear",
}


def reason_labels(reasons: str | list[str]) -> list[str]:
    values = reasons.split(",") if isinstance(reasons, str) else reasons
    labels = []
    for value in values:
        label = REASON_LABELS.get(value.strip(), value.strip().replace("_", " ").capitalize())
        if label and label not in labels:
            labels.append(label)
    return labels


def render_reason_chips(reasons: str | list[str], limit: int = 2) -> str:
    labels = reason_labels(reasons)
    shown = "".join(f'<span class="reason-chip">{html.escape(label)}</span>' for label in labels[:limit])
    extra = f'<span class="reason-chip reason-more">+{len(labels)-limit}</span>' if len(labels) > limit else ""
    return f'<span class="reason-chips">{shown}{extra}</span>'


def source_id_for(name: str | None) -> str:
    key = normalized_source(name)
    for source_id, source_name, _payer, _status in SOURCES:
        if key in {normalized_source(alias) for alias in ACCOUNT_ALIASES.get(source_id, (source_name,))}:
            return source_id
    return ""


def slug(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in text).strip("_")


def stable_hash(*parts: object) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def normalized_source(value: str | None) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def parse_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return date.today().isoformat()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return value


def month_start_end(statement_month: str) -> tuple[str, str]:
    base = parse_date(f"{statement_month}-01")
    try:
        start = datetime.strptime(base, "%Y-%m-%d").date()
    except ValueError:
        today = date.today()
        start = date(today.year, today.month, 1)
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return start.isoformat(), (next_month - timedelta(days=1)).isoformat()


def default_statement_period(source_name: str, statement_month: str) -> tuple[str, str]:
    source = (source_name or "").lower()
    if "diners" in source or "credit" in source or "card" in source:
        base = parse_date(f"{statement_month}-15")
        try:
            start = datetime.strptime(base, "%Y-%m-%d").date()
        except ValueError:
            start = date.today()
        if start.month == 12:
            end = date(start.year + 1, 1, 15)
        else:
            end = date(start.year, start.month + 1, 15)
        return start.isoformat(), end.isoformat()
    return month_start_end(statement_month)


def normalize_amount(value: str) -> float:
    raw = str(value or "").strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "")
    raw = raw.replace("INR", "").strip()
    if not raw:
        return 0.0
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    try:
        amount = float(raw)
    except ValueError:
        return 0.0
    return -amount if negative else amount


def init_db(seed: bool = True) -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    LOGO_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
              source_id TEXT PRIMARY KEY,
              source_name TEXT NOT NULL,
              payer TEXT NOT NULL,
              status TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS import_batches (
              import_batch_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              source_id TEXT,
              source_name TEXT,
              statement_month TEXT,
              statement_start_date TEXT,
              statement_end_date TEXT,
              file_name TEXT,
              file_type TEXT,
              status TEXT NOT NULL,
              row_count INTEGER DEFAULT 0,
              duplicate_count INTEGER DEFAULT 0,
              error_count INTEGER DEFAULT 0,
              notes TEXT
            );

            CREATE TABLE IF NOT EXISTS raw_imports (
              raw_import_id TEXT PRIMARY KEY,
              import_batch_id TEXT NOT NULL,
              row_number INTEGER NOT NULL,
              raw_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(import_batch_id) REFERENCES import_batches(import_batch_id)
            );

            CREATE TABLE IF NOT EXISTS transactions (
              transaction_id TEXT PRIMARY KEY,
              raw_import_id TEXT,
              import_batch_id TEXT,
              transaction_date TEXT NOT NULL,
              description TEXT NOT NULL,
              amount REAL NOT NULL,
              flow_type TEXT NOT NULL,
              category TEXT,
              subcategory TEXT,
              classification TEXT,
              merchant_payee TEXT,
              payer TEXT,
              source_name TEXT,
              confidence REAL DEFAULT 0,
              rule_id TEXT,
              manual_override_id TEXT,
              notes TEXT,
              fingerprint TEXT UNIQUE,
              created_at TEXT NOT NULL,
              FOREIGN KEY(import_batch_id) REFERENCES import_batches(import_batch_id)
            );

            CREATE TABLE IF NOT EXISTS rules (
              rule_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              match_type TEXT NOT NULL,
              pattern TEXT NOT NULL,
              source_name TEXT,
              category TEXT,
              subcategory TEXT,
              classification TEXT,
              flow_type TEXT,
              merchant_payee TEXT,
              confidence REAL DEFAULT 0.9,
              enabled INTEGER DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manual_overrides (
              manual_override_id TEXT PRIMARY KEY,
              transaction_id TEXT NOT NULL,
              category TEXT,
              subcategory TEXT,
              classification TEXT,
              flow_type TEXT,
              merchant_payee TEXT,
              notes TEXT,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              FOREIGN KEY(transaction_id) REFERENCES transactions(transaction_id)
            );

            CREATE TABLE IF NOT EXISTS baselines (
              baseline_id TEXT PRIMARY KEY,
              scope TEXT NOT NULL,
              category TEXT NOT NULL,
              subcategory TEXT,
              amount REAL NOT NULL,
              effective_month TEXT NOT NULL,
              updated_source TEXT NOT NULL,
              active INTEGER DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS review_items (
              review_item_id TEXT PRIMARY KEY,
              transaction_id TEXT NOT NULL,
              reason TEXT NOT NULL,
              status TEXT DEFAULT 'open',
              created_at TEXT NOT NULL,
              resolved_at TEXT,
              FOREIGN KEY(transaction_id) REFERENCES transactions(transaction_id)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
              audit_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              actor TEXT NOT NULL,
              action TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              entity_id TEXT NOT NULL,
              before_json TEXT,
              after_json TEXT
            );

            CREATE TABLE IF NOT EXISTS account_logos (
              source_id TEXT PRIMARY KEY,
              file_name TEXT NOT NULL,
              content_type TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(source_id) REFERENCES sources(source_id)
            );

            CREATE TABLE IF NOT EXISTS app_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        ensure_schema(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO sources(source_id, source_name, payer, status) VALUES(?,?,?,?)",
            SOURCES,
        )
        if seed:
            seed_defaults(conn)


def ensure_schema(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(import_batches)").fetchall()}
    if "statement_start_date" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN statement_start_date TEXT")
    if "statement_end_date" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN statement_end_date TEXT")
    if "excluded_at" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN excluded_at TEXT")
    if "deleted_at" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN deleted_at TEXT")
    rule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(rules)").fetchall()}
    if "notes" not in rule_columns:
        conn.execute("ALTER TABLE rules ADD COLUMN notes TEXT")
    for row in conn.execute(
        "SELECT import_batch_id, source_name, statement_month FROM import_batches WHERE statement_month IS NOT NULL AND (statement_start_date IS NULL OR statement_end_date IS NULL)"
    ).fetchall():
        start_date, end_date = default_statement_period(row["source_name"] or "", row["statement_month"])
        conn.execute(
            "UPDATE import_batches SET statement_start_date=?, statement_end_date=? WHERE import_batch_id=?",
            (start_date, end_date, row["import_batch_id"]),
        )
    if setting(conn, SEED_DATA_SETTING) is None:
        set_setting(conn, SEED_DATA_SETTING, "0" if real_statement_exists(conn) else "1")


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings(key, value, updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, now_iso()),
    )


def setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def real_statement_exists(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            """
            SELECT EXISTS(
              SELECT 1 FROM import_batches
              WHERE import_batch_id != ?
                AND coalesce(file_type, '') != 'seed'
                AND deleted_at IS NULL
            )
            """,
            (SEED_BATCH_ID,),
        ).fetchone()[0]
    )


def seed_data_enabled(conn: sqlite3.Connection) -> bool:
    stored = setting(conn, SEED_DATA_SETTING)
    return stored != "0"


def set_seed_data_enabled(conn: sqlite3.Connection, enabled: bool, actor: str = "dashboard") -> None:
    before = setting(conn, SEED_DATA_SETTING)
    set_setting(conn, SEED_DATA_SETTING, "1" if enabled else "0")
    audit(conn, actor, "set_seed_data_visibility", "setting", SEED_DATA_SETTING, before={"value": before}, after={"enabled": enabled})


def disable_seed_data_after_statement(conn: sqlite3.Connection, actor: str = "dashboard") -> bool:
    was_visible = seed_data_enabled(conn)
    set_seed_data_enabled(conn, False, actor)
    return was_visible


def audit(conn: sqlite3.Connection, actor: str, action: str, entity_type: str, entity_id: str, before=None, after=None) -> None:
    conn.execute(
        """
        INSERT INTO audit_log(audit_id, created_at, actor, action, entity_type, entity_id, before_json, after_json)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()),
            now_iso(),
            actor,
            action,
            entity_type,
            entity_id,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
        ),
    )


def active_batch_sql(alias: str = "ib") -> str:
    """SQL predicate shared by every user-facing transaction calculation."""
    return f"{alias}.deleted_at IS NULL AND {alias}.excluded_at IS NULL"


def start_import_worker(batch_id: str | None = None, password: str | None = None) -> None:
    """Start a daemon worker; passwords only live in this call/thread and are never persisted."""
    threading.Thread(target=process_pending_imports, args=(batch_id, password), daemon=True, name="statement-import-worker").start()


def extract_pdf_text(path: Path, password: str | None = None) -> tuple[str | None, str | None]:
    """Optional extractor. App startup remains dependency-free when pypdf is absent."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return None, "PDF extraction is unavailable on this server. Install the optional 'pypdf' package, then run Process pending imports."
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            if not password:
                return None, "This PDF is password-protected. Re-upload it and enter the password; passwords are used only during extraction and are never stored."
            if not reader.decrypt(password):
                return None, "The PDF password was not accepted. Re-upload the statement with the correct password."
        return "\n".join(page.extract_text() or "" for page in reader.pages), None
    except Exception as exc:
        return None, f"PDF extraction failed: {type(exc).__name__}. Re-upload the statement or run extraction again after checking the file."


def parse_statement_text(source_name: str, text: str) -> tuple[list[dict], str | None]:
    """Bank-parser interface. Only verified source parsers should return transactions."""
    # Intentionally conservative: a Kotak mapper will be added after its extracted
    # layout is available. Importing guessed date/amount columns would corrupt totals.
    if not text.strip():
        return [], "The PDF contains no extractable text. It may be scanned or image-only and needs OCR or a bank-specific parser."
    return [], f"Text extraction worked, but no verified parser is available yet for {source_name}. The statement was not imported to avoid incorrect transactions."


def process_pending_imports(batch_id: str | None = None, password: str | None = None) -> dict:
    processed = 0
    while True:
        with db() as conn:
            where = "status='pending_pdf_extraction' AND deleted_at IS NULL"
            args: list[object] = []
            if batch_id:
                where += " AND import_batch_id=?"
                args.append(batch_id)
            row = conn.execute(f"SELECT * FROM import_batches WHERE {where} ORDER BY created_at LIMIT 1", args).fetchone()
            if not row:
                return {"processed": processed}
            claimed = conn.execute("UPDATE import_batches SET status='extracting', notes=? WHERE import_batch_id=? AND status='pending_pdf_extraction'", ("Extraction started. Passwords are never stored.", row["import_batch_id"])).rowcount
            if not claimed:
                continue
            audit(conn, "worker", "pdf_extraction_started", "import_batch", row["import_batch_id"])
        path = UPLOAD_DIR / (row["file_name"] or "")
        try:
            if not path.is_file():
                status, note = "failed", "The uploaded file is missing from storage. Re-upload the statement."
            elif (row["file_type"] or "").lower() != "pdf":
                status, note = "needs_parser", f"No extractor is configured for {human_label(row['file_type'])} files."
            else:
                text, extraction_error = extract_pdf_text(path, password if row["import_batch_id"] == batch_id else None)
                if extraction_error:
                    status, note = "needs_parser", extraction_error
                else:
                    transactions, parser_note = parse_statement_text(row["source_name"] or "this source", text or "")
                    if transactions:
                        status, note = "committed", f"Imported {len(transactions)} transactions."
                    else:
                        status, note = "needs_parser", parser_note or "No verified transactions were found."
            with db() as conn:
                added = 0
                if status == "committed":
                    for transaction in transactions:
                        ok, _ = insert_transaction(conn, row["import_batch_id"], None, transaction)
                        added += int(ok)
                conn.execute("UPDATE import_batches SET status=?, notes=?, row_count=? WHERE import_batch_id=?", (status, note, added, row["import_batch_id"]))
                audit(conn, "worker", "pdf_extraction_succeeded" if status == "committed" else "pdf_extraction_failed", "import_batch", row["import_batch_id"], after={"status": status, "notes": note, "rows": added})
        except Exception as exc:
            with db() as conn:
                note = f"Worker failed: {type(exc).__name__}. Retry extraction or re-upload the statement."
                conn.execute("UPDATE import_batches SET status='failed', notes=? WHERE import_batch_id=?", (note, row["import_batch_id"]))
                audit(conn, "worker", "pdf_extraction_failed", "import_batch", row["import_batch_id"], after={"status": "failed", "notes": note})
        processed += 1
        if batch_id:
            return {"processed": processed}


def seed_defaults(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    if count == 0:
        defaults = [
            ("Swiggy/Zomato dining", "description_contains", "SWIGGY|ZOMATO|ETERNAL", None, "Food", "Dining & Delivery", "controllable", "spend", 0.86),
            ("Quick commerce groceries", "description_contains", "BLINKIT|INSTAMART|FIRSTCLUB|ZEPTO", None, "Food", "Groceries / Quick Commerce", "baseline_variable", "spend", 0.82),
            ("Amazon ambiguous", "description_contains", "AMAZON|FLIPKART", None, "Shopping", "Needs Review", "controllable", "spend", 0.52),
            ("HDFC card payment", "description_contains", "CARD PAYMENT|HDFC CARD|CREDIT CARD PAYMENT", None, None, None, "excluded", "card_payment", 0.95),
            ("Investments", "description_contains", "GROWW|INDMONEY|SIP|NPS|PPF|AMC|MUTUAL", None, "Investments", "SIP / Investment", "excluded", "spend", 0.88),
            ("Rent", "description_contains", "RENT|SONA SINGH", None, "Home & Utilities", "Rent", "fixed", "spend", 0.9),
            ("Idli", "description_contains", "SUPERTAILS|KLEVER K9|PETS|HAUSBERG|WIZARD OF PAWS", None, "Idli", "Essentials / Care", "baseline_variable", "spend", 0.86),
            ("Baby", "description_contains", "BABY|MINIKLUB|SUPERBOTTOMS|COCOON|ALL THINGS BABY", None, "Baby", "Essentials", "baseline_variable", "spend", 0.84),
        ]
        for name, match_type, pattern, source, category, subcategory, classification, flow_type, confidence in defaults:
            rid = "rule_" + slug(name)
            conn.execute(
                """
                INSERT INTO rules(rule_id, name, match_type, pattern, source_name, category, subcategory, classification, flow_type, confidence, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (rid, name, match_type, pattern, source, category, subcategory, classification, flow_type, confidence, now_iso(), now_iso()),
            )
    cap = conn.execute("SELECT COUNT(*) FROM baselines WHERE category='Food' AND active=1").fetchone()[0]
    if cap == 0:
        bid = "baseline_food_2026_05"
        conn.execute(
            """
            INSERT INTO baselines(baseline_id, scope, category, subcategory, amount, effective_month, updated_source, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (bid, "parent_category", "Food", None, 12000, "2026-05", "initial_decision", now_iso(), now_iso()),
        )
    tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    if tx_count == 0:
        seed_sample_transactions(conn)


def seed_sample_transactions(conn: sqlite3.Connection) -> None:
    batch_id = SEED_BATCH_ID
    start_date, end_date = month_start_end("2026-05")
    conn.execute(
        """
        INSERT OR IGNORE INTO import_batches(import_batch_id, created_at, source_name, statement_month, statement_start_date, statement_end_date, file_name, file_type, status, row_count, notes)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (batch_id, now_iso(), "Seed sample", "2026-05", start_date, end_date, "seed", "seed", "committed", 16, "Sample rows until real statements are imported."),
    )
    rows = [
        ("2026-05-01", "Salary credit Jananiya", 403143, "income", None, None, None, "Jananiya", "Jananiya HDFC Bank"),
        ("2026-05-02", "Rent Sona Singh", -70000, "spend", "Home & Utilities", "Rent", "fixed", "Jananiya", "Jananiya HDFC Bank"),
        ("2026-05-03", "Groww SIP", -2000, "spend", "Investments", "SIP / Investment", "excluded", "Vignesh", "Vignesh Kotak Bank"),
        ("2026-05-04", "Swiggy Instamart", -2400, "spend", "Food", "Groceries / Quick Commerce", "baseline_variable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-05", "Zomato Eternal", -1850, "spend", "Food", "Dining & Delivery", "controllable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-08", "Amazon Seller Services", -3790, "spend", "Shopping", "Needs Review", "controllable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-09", "Superbottoms Baby", -2018, "spend", "Baby", "Essentials", "baseline_variable", "Jananiya", "Jananiya HDFC Bank"),
        ("2026-05-10", "Supertails", -3136, "spend", "Idli", "Essentials / Care", "baseline_variable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-11", "Ramdev Medical", -3691, "spend", "Health", "Medical", "baseline_variable", "Jananiya", "Jananiya HDFC Bank"),
        ("2026-05-12", "M P Fuels", -4181, "spend", "Transport", "Fuel", "baseline_variable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-13", "Netflix", -649, "spend", "Lifestyle", "Subscriptions", "controllable", "Vignesh", "Vignesh Kotak Bank"),
        ("2026-05-14", "Evolve Back Resort", -110200, "spend", "Travel", "Vacation", "one_off", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-15", "HDFC Card Payment", -231852, "card_payment", None, None, "excluded", "Jananiya", "Jananiya HDFC Bank"),
        ("2026-05-16", "UPI transfer own account", -15000, "transfer", None, None, "excluded", "Vignesh", "Vignesh Kotak Bank"),
        ("2026-05-17", "Bank charges", -52, "fee", "Home & Utilities", "Bank Fees", "controllable", "Vignesh", "Vignesh Kotak Bank"),
        ("2026-05-18", "Petro surcharge waiver", 41, "refund", "Transport", "Fuel", "baseline_variable", "Vignesh", "Vignesh HDFC Diners"),
    ]
    for row in rows:
        tx = {
            "transaction_date": row[0],
            "description": row[1],
            "amount": row[2],
            "flow_type": row[3],
            "category": row[4],
            "subcategory": row[5],
            "classification": row[6],
            "payer": row[7],
            "source_name": row[8],
            "merchant_payee": row[1],
            "confidence": 0.9,
        }
        insert_transaction(conn, batch_id, None, tx, create_review=True)


def apply_rules(conn: sqlite3.Connection, tx: dict) -> dict:
    description = (tx.get("description") or "").upper()
    source = tx.get("source_name")
    best = None
    for rule in conn.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY confidence DESC").fetchall():
        if rule["source_name"] and rule["source_name"] != source:
            continue
        patterns = [p.strip().upper() for p in rule["pattern"].split("|") if p.strip()]
        matched = False
        if rule["match_type"] in ("description_contains", "keyword"):
            matched = any(p in description for p in patterns)
        elif rule["match_type"] == "exact_merchant":
            matched = description == rule["pattern"].upper()
        elif rule["match_type"] == "normalized_merchant":
            matched = slug(description) == slug(rule["pattern"])
        if matched:
            best = rule
            break
    if not best:
        tx.setdefault("flow_type", infer_flow_type(tx))
        tx.setdefault("category", None)
        tx.setdefault("classification", None)
        tx["confidence"] = tx.get("confidence") or 0.2
        return tx
    for key in ("category", "subcategory", "classification", "flow_type", "merchant_payee"):
        if best[key] is not None:
            tx[key] = best[key]
    tx["rule_id"] = best["rule_id"]
    tx["confidence"] = best["confidence"]
    if "notes" in best.keys() and best["notes"]:
        tx["notes"] = best["notes"]
    return tx


def infer_flow_type(tx: dict) -> str:
    desc = (tx.get("description") or "").upper()
    amount = float(tx.get("amount") or 0)
    if any(s in desc for s in ("CARD PAYMENT", "CREDIT CARD PAYMENT", "HDFC CARD")):
        return "card_payment"
    if any(s in desc for s in ("TRANSFER", "SELF", "OWN ACCOUNT")):
        return "transfer"
    if any(s in desc for s in ("SALARY", "CREDIT INTEREST")) and amount > 0:
        return "income"
    if any(s in desc for s in ("REVERSAL", "REVERSED")):
        return "reversal"
    if any(s in desc for s in ("REFUND", "WAIVER")):
        return "refund"
    if any(s in desc for s in ("FEE", "CHARGE", "IGST")):
        return "fee"
    if amount > 0:
        return "income"
    return "spend"


def insert_transaction(conn: sqlite3.Connection, batch_id: str, raw_import_id: str | None, tx: dict, create_review: bool = True) -> tuple[bool, str]:
    tx = apply_rules(conn, tx)
    tx.setdefault("flow_type", infer_flow_type(tx))
    tx.setdefault("merchant_payee", tx.get("description"))
    tx.setdefault("confidence", 0.2)
    fingerprint = stable_hash(tx.get("source_name"), tx.get("transaction_date"), tx.get("amount"), tx.get("description"))
    transaction_id = "txn_" + fingerprint[:12]
    try:
        conn.execute(
            """
            INSERT INTO transactions(
              transaction_id, raw_import_id, import_batch_id, transaction_date, description, amount, flow_type,
              category, subcategory, classification, merchant_payee, payer, source_name, confidence, rule_id, notes, fingerprint, created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                transaction_id,
                raw_import_id,
                batch_id,
                tx.get("transaction_date"),
                tx.get("description"),
                float(tx.get("amount") or 0),
                tx.get("flow_type") or "unknown",
                tx.get("category"),
                tx.get("subcategory"),
                tx.get("classification"),
                tx.get("merchant_payee"),
                tx.get("payer"),
                tx.get("source_name"),
                float(tx.get("confidence") or 0),
                tx.get("rule_id"),
                tx.get("notes"),
                fingerprint,
                now_iso(),
            ),
        )
    except sqlite3.IntegrityError:
        return False, transaction_id
    if create_review:
        add_review_if_needed(conn, transaction_id)
    return True, transaction_id


def add_review_if_needed(conn: sqlite3.Connection, transaction_id: str) -> None:
    tx = conn.execute("SELECT * FROM transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
    if not tx:
        return
    reasons = []
    desc = (tx["description"] or "").upper()
    if tx["flow_type"] == "unknown":
        reasons.append("unknown flow_type")
    if (tx["confidence"] or 0) < 0.75:
        reasons.append("low confidence")
    if not tx["category"] and tx["flow_type"] in ("spend", "fee"):
        reasons.append("missing category")
    if any(word in desc for word in ("AMAZON", "FLIPKART", "RAZORPAY", "PAYU", "PAYTM")):
        reasons.append("ambiguous merchant")
    if tx["flow_type"] in ("spend", "fee") and abs(float(tx["amount"] or 0)) >= 25000 and tx["classification"] != "fixed":
        reasons.append("unusual amount")
    if tx["category"] in ("Food", "Lifestyle", "Shopping", "Baby", "Idli", "Transport") and tx["classification"] == "controllable":
        reasons.append("creep-watch driver")
    for reason in reasons:
        conn.execute(
            "INSERT OR IGNORE INTO review_items(review_item_id, transaction_id, reason, created_at) VALUES(?,?,?,?)",
            ("review_" + stable_hash(transaction_id, reason), transaction_id, reason, now_iso()),
        )


def import_csv(conn: sqlite3.Connection, file_bytes: bytes, file_name: str, source_name: str, statement_month: str, statement_start_date: str | None = None, statement_end_date: str | None = None) -> dict:
    batch_id = "batch_" + stable_hash(file_name, time.time())
    if not statement_start_date or not statement_end_date:
        statement_start_date, statement_end_date = default_statement_period(source_name, statement_month)
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    conn.execute(
        """
        INSERT INTO import_batches(import_batch_id, created_at, source_name, statement_month, statement_start_date, statement_end_date, file_name, file_type, status, row_count)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (batch_id, now_iso(), source_name, statement_month, statement_start_date, statement_end_date, file_name, "csv", "processing", len(rows)),
    )
    added = duplicates = errors = 0
    for idx, row in enumerate(rows, start=1):
        raw_id = "raw_" + stable_hash(batch_id, idx, json.dumps(row, sort_keys=True))
        conn.execute(
            "INSERT INTO raw_imports(raw_import_id, import_batch_id, row_number, raw_json, created_at) VALUES(?,?,?,?,?)",
            (raw_id, batch_id, idx, json.dumps(row), now_iso()),
        )
        tx = csv_row_to_tx(row, source_name)
        ok, _ = insert_transaction(conn, batch_id, raw_id, tx)
        if ok:
            added += 1
        else:
            duplicates += 1
    conn.execute(
        "UPDATE import_batches SET status='committed', duplicate_count=?, error_count=? WHERE import_batch_id=?",
        (duplicates, errors, batch_id),
    )
    audit(conn, "dashboard", "import_csv", "import_batch", batch_id, after={"added": added, "duplicates": duplicates, "statement_start_date": statement_start_date, "statement_end_date": statement_end_date})
    seed_removed = disable_seed_data_after_statement(conn, "dashboard")
    return {"batch_id": batch_id, "rows": len(rows), "added": added, "duplicates": duplicates, "errors": errors, "seed_removed": seed_removed}


def queue_statement_file(conn: sqlite3.Connection, file_path: Path, source_name: str, statement_month: str, statement_start_date: str | None = None, statement_end_date: str | None = None, actor: str = "dashboard") -> dict:
    if not statement_start_date or not statement_end_date:
        statement_start_date, statement_end_date = default_statement_period(source_name, statement_month)
    suffix = file_path.suffix.lower()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"{int(time.time())}_{slug(file_path.stem) or 'statement'}{suffix}"
    destination = UPLOAD_DIR / safe_name
    shutil.copyfile(file_path, destination)
    batch_id = "batch_" + stable_hash(file_path.name, time.time())
    conn.execute(
        """
        INSERT INTO import_batches(import_batch_id, created_at, source_name, statement_month, statement_start_date, statement_end_date, file_name, file_type, status, notes)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (batch_id, now_iso(), source_name, statement_month, statement_start_date, statement_end_date, safe_name, suffix.lstrip(".") or "file", "pending_pdf_extraction", "PDF/file stored for statement parser. Password was not stored."),
    )
    audit(conn, actor, "queue_statement_file", "import_batch", batch_id, after={"file": safe_name, "source": source_name, "statement_start_date": statement_start_date, "statement_end_date": statement_end_date})
    seed_removed = disable_seed_data_after_statement(conn, actor)
    return {"batch_id": batch_id, "file": safe_name, "status": "pending_pdf_extraction", "statement_start_date": statement_start_date, "statement_end_date": statement_end_date, "seed_removed": seed_removed}


def import_statement_path(path: str, source_name: str, statement_month: str, statement_start_date: str | None = None, statement_end_date: str | None = None, actor: str = "chat") -> dict:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))
    suffix = file_path.suffix.lower()
    queued = None
    with db() as conn:
        if suffix == ".csv":
            return import_csv(conn, file_path.read_bytes(), file_path.name, source_name, statement_month, statement_start_date, statement_end_date)
        if suffix == ".json":
            return import_json(conn, file_path.read_bytes(), file_path.name, source_name, statement_month, statement_start_date, statement_end_date)
        queued = queue_statement_file(conn, file_path, source_name, statement_month, statement_start_date, statement_end_date, actor=actor)
    start_import_worker(queued["batch_id"])
    return queued


def csv_row_to_tx(row: dict, source_name: str) -> dict:
    lower = {str(k).strip().lower(): v for k, v in row.items()}

    def pick(*names, default=""):
        for name in names:
            if name in lower and str(lower[name]).strip():
                return lower[name]
        return default

    debit = pick("debit", "withdrawal", "withdrawals", "dr", default="")
    credit = pick("credit", "deposit", "deposits", "cr", default="")
    amount = pick("amount", "transaction amount", "amt", default="")
    if debit and not credit:
        parsed_amount = -abs(normalize_amount(debit))
    elif credit and not debit:
        parsed_amount = abs(normalize_amount(credit))
    else:
        parsed_amount = normalize_amount(amount)
    tx = {
        "transaction_date": parse_date(pick("date", "transaction date", "txn date", "value date")),
        "description": pick("description", "narration", "details", "merchant", "particulars", default=""),
        "amount": parsed_amount,
        "source_name": source_name,
        "payer": infer_payer(source_name),
    }
    tx["flow_type"] = infer_flow_type(tx)
    return tx


def infer_payer(source_name: str) -> str:
    if "Jananiya" in (source_name or ""):
        return "Jananiya"
    return "Vignesh"


def dashboard_data(conn: sqlite3.Connection, month: str | None = None) -> dict:
    include_seed = seed_data_enabled(conn)
    rows = effective_transactions(conn, month, include_seed)
    total_inflow = sum(r["amount"] for r in rows if r["flow_type"] == "income")
    investments = sum(abs(r["amount"]) for r in rows if r["category"] == "Investments" and r["flow_type"] in ("spend", "fee"))
    fixed = sum(abs(r["amount"]) for r in rows if r["classification"] == "fixed" and r["flow_type"] in ("spend", "fee"))
    spend_fees = sum(abs(r["amount"]) for r in rows if r["flow_type"] in ("spend", "fee"))
    refunds = sum(abs(r["amount"]) for r in rows if r["flow_type"] in ("refund", "reversal"))
    total_spend = max(0, spend_fees - refunds)
    other = max(0, total_spend - investments - fixed)
    categories = {}
    for r in rows:
        if r["flow_type"] not in ("spend", "fee"):
            continue
        cat = r["category"] or "Uncategorized"
        categories[cat] = categories.get(cat, 0) + abs(r["amount"])
    review_count_query = """
        SELECT COUNT(DISTINCT ri.transaction_id)
        FROM review_items ri
        JOIN transactions t ON t.transaction_id=ri.transaction_id
        LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id
        WHERE ri.status='open'
          AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))
    """
    review_args: list[object] = []
    if not include_seed:
        review_count_query += " AND coalesce(t.import_batch_id, '') != ?"
        review_args.append(SEED_BATCH_ID)
    review_count = conn.execute(review_count_query, review_args).fetchone()[0]
    baselines = conn.execute("SELECT * FROM baselines WHERE active=1 ORDER BY category, subcategory").fetchall()
    creep = []
    for base in baselines:
        actual = sum(
            abs(r["amount"])
            for r in rows
            if r["flow_type"] in ("spend", "fee")
            and r["category"] == base["category"]
            and (not base["subcategory"] or r["subcategory"] == base["subcategory"])
        )
        creep.append(
            {
                "category": base["category"],
                "subcategory": base["subcategory"],
                "planned": base["amount"],
                "actual": actual,
                "variance": actual - base["amount"],
            }
        )
    return {
        "summary": {
            "total_inflow": total_inflow,
            "investments": investments,
            "fixed": fixed,
            "other": other,
            "total_spend": total_spend,
            "review_count": review_count,
        },
        "categories": categories,
        "creep": creep,
    }


def default_active_month() -> str:
    """Fresh sessions start on the last completed calendar month."""
    return shift_month(date.today().strftime("%Y-%m"), -1)


def dashboard_month(conn: sqlite3.Connection, requested: str | None = None) -> str:
    current = date.today().strftime("%Y-%m")
    earliest_query = "SELECT min(substr(t.transaction_date,1,7)) FROM transactions t LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id WHERE length(t.transaction_date)>=7 AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))"
    earliest_args: list[object] = []
    if not seed_data_enabled(conn):
        earliest_query += " AND coalesce(t.import_batch_id, '') != ?"
        earliest_args.append(SEED_BATCH_ID)
    earliest = conn.execute(earliest_query, earliest_args).fetchone()[0] or current
    if requested and len(requested) == 7 and requested[4] == "-":
        try:
            datetime.strptime(requested, "%Y-%m")
            return min(current, max(earliest, requested))
        except ValueError:
            pass
    if requested:
        return current
    return default_active_month()


def shift_month(value: str, amount: int) -> str:
    parsed = datetime.strptime(value, "%Y-%m")
    ordinal = parsed.year * 12 + parsed.month - 1 + amount
    return f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"


def account_coverage(conn: sqlite3.Connection, month: str) -> list[dict]:
    month_start, month_end = month_start_end(month)
    include_seed = seed_data_enabled(conn)
    tx_query = "SELECT DISTINCT t.source_name FROM transactions t LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id WHERE substr(t.transaction_date,1,7)=? AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))"
    tx_args: list[object] = [month]
    if not include_seed:
        tx_query += " AND coalesce(t.import_batch_id, '') != ?"
        tx_args.append(SEED_BATCH_ID)
    tx_sources = {
        normalized_source(row[0])
        for row in conn.execute(
            tx_query,
            tx_args,
        ).fetchall()
    }
    batch_filter = "" if include_seed else " AND import_batch_id != ?"
    batch_args: list[object] = [month_end, month_start]
    if not include_seed:
        batch_args.append(SEED_BATCH_ID)
    batches = conn.execute(
        f"""
        SELECT source_name, status FROM import_batches
        WHERE (coalesce(statement_start_date, statement_month || '-01') <= ?)
          AND (coalesce(statement_end_date, statement_month || '-31') >= ?)
          AND deleted_at IS NULL AND excluded_at IS NULL
          {batch_filter}
        """,
        batch_args,
    ).fetchall()
    complete_statuses = {"committed", "completed", "complete", "imported", "success", "succeeded"}
    coverage = []
    source_ids = {source_name: source_id for source_id, source_name, _payer, _status in SOURCES}
    for display_name, aliases in EXPECTED_ACCOUNTS:
        alias_keys = {normalized_source(alias) for alias in aliases}
        matching_batches = [row for row in batches if normalized_source(row["source_name"]) in alias_keys]
        present = bool(tx_sources & alias_keys) or any((row["status"] or "").lower() in complete_statuses for row in matching_batches)
        pending = not present and bool(matching_batches)
        coverage.append({"id": source_ids[display_name], "name": display_name, "status": "present" if present else "pending" if pending else "missing"})
    return coverage


def effective_transactions(conn: sqlite3.Connection, month: str | None = None, include_seed: bool | None = None) -> list[dict]:
    if include_seed is None:
        include_seed = seed_data_enabled(conn)
    query = "SELECT t.* FROM transactions t LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id"
    args = []
    clauses = []
    if month:
        clauses.append("substr(t.transaction_date,1,7)=?")
        args.append(month)
    if not include_seed:
        clauses.append("coalesce(t.import_batch_id, '') != ?")
        args.append(SEED_BATCH_ID)
    clauses.append("(t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY t.transaction_date DESC, t.created_at DESC"
    tx_rows = [dict(r) for r in conn.execute(query, args).fetchall()]
    for tx in tx_rows:
        override = conn.execute(
            "SELECT * FROM manual_overrides WHERE transaction_id=? ORDER BY created_at DESC LIMIT 1",
            (tx["transaction_id"],),
        ).fetchone()
        if override:
            for key in ("category", "subcategory", "classification", "flow_type", "merchant_payee", "notes"):
                if override[key] is not None:
                    tx[key] = override[key]
            tx["manual_override_id"] = override["manual_override_id"]
    return tx_rows


def account_logo(source_id: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT file_name FROM account_logos WHERE source_id=?", (source_id,)).fetchone()
    return f"/assets/uploads/bank-logos/{urllib.parse.quote(row['file_name'])}?v={int((LOGO_UPLOAD_DIR / row['file_name']).stat().st_mtime)}" if row and (LOGO_UPLOAD_DIR / row["file_name"]).is_file() else None


def render_bank_logo(name: str, dense: bool = False, missing: bool = False, source_id: str | None = None) -> str:
    asset, monogram, alt = bank_asset(name)
    source_id = source_id or next((row[0] for row in SOURCES if row[1] == name), "")
    custom = account_logo(source_id) if source_id else None
    src = custom or (f"/assets/banks/{asset}" if asset else None)
    fallback = f"/assets/banks/{asset}" if custom and asset else ""
    image = (f'<img src="{html.escape(src)}" alt="{html.escape(alt)}" data-account-logo="{html.escape(source_id)}" '
             f'data-bundled-src="{html.escape(fallback)}" onerror="if(this.dataset.bundledSrc){{this.src=this.dataset.bundledSrc;this.dataset.bundledSrc=\'\'}}else{{this.remove()}}">') if src else ""
    classes = "bank-logo dense" if dense else "bank-logo"
    if missing:
        classes += " missing-logo"
    return f'<span class="{classes}" data-logo-container="{html.escape(source_id)}">{image}<span class="monogram">{html.escape(monogram)}</span></span>'


def render_beacon(month: str) -> tuple[str, str]:
    with db() as conn:
        coverage = account_coverage(conn, month)
    missing = [item for item in coverage if item["status"] != "present"]
    display_month = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    rows = []
    for row_index, item in enumerate(coverage):
        absent = item["status"] != "present"
        state = "Missing" if absent else "Represented"
        action = (f'<a class="btn garden-import" href="/import?source={urllib.parse.quote(item["name"])}&month={month}">Import now</a>' if absent else "")
        rows.append(f'<div class="garden-row {"missing" if absent else "present"}" style="--row-i:{row_index}">{render_bank_logo(item["name"], True, absent, item["id"])}'
                    f'<span class="garden-name">{html.escape(item["name"])}</span><span class="pill garden-state">{state}</span>{action}</div>')
    healthy = not missing
    summary = f"{len(coverage)}/{len(coverage)} reporting" if healthy else f"{len(missing)} missing"
    tooltip = f"All {len(coverage)} accounts reporting — the garden is happy." if healthy else f"{len(missing)} accounts need a statement."
    footer = ('<div class="garden-meadow"><span>🌱 🌱 🌱</span><strong>Everything\'s accounted for. Go spend guiltlessly*</strong><small>*within plan</small></div>' if healthy else "")
    button = f'''<div class="beacon-wrap">
      <button class="beacon {"healthy" if healthy else "alert"}" id="coverage-beacon" aria-label="Account coverage" aria-expanded="false" title="{html.escape(tooltip)}">
        <svg class="sprout" viewBox="0 0 36 36" aria-hidden="true"><path d="M18 28V15"/><path class="leaf leaf-left" d="M18 19C12 19 9 16 9 11c6 0 9 3 9 8Z"/><path class="leaf leaf-right" d="M18 15c1-6 5-8 10-7 0 5-4 8-10 7Z"/></svg><span class="sparkle">✦</span>{f'<span class="beacon-count">{len(missing)}</span>' if missing else ''}
      </button></div>'''
    overlay = f'''<div class="garden-scrim" id="garden-scrim"></div><section class="garden-panel" id="account-garden" aria-hidden="true" aria-labelledby="garden-title"><div class="sheet-handle"></div>
        <div class="garden-head"><div><h2 id="garden-title">Account coverage — {html.escape(display_month)}</h2><span class="pill {"green" if healthy else "red"}">{summary}</span></div><button class="icon-button" id="garden-close" aria-label="Close account garden"><i data-lucide="x"></i></button></div>
        <div class="garden-rows">{"".join(rows)}</div>{footer}
      </section>'''
    return button, overlay


def render_review_workbench(items: list[dict], categories: list[str], open_id: str = "") -> str:
    rows = "".join(f'''<tr class="review-row" data-review-id="{html.escape(item['id'])}" tabindex="0" role="button" aria-label="Review {html.escape(item['description'])}">
      <td>{html.escape(item['date'])}</td><td><span class="review-source">{render_bank_logo(item['source'], True, False, item['source_id'])}<span>{html.escape(item['source'])}</span></span></td>
      <td>{html.escape(item['description'])}</td><td>{item['reason_chips']}</td><td class="review-amount">{html.escape(item['amount'])}<i data-lucide="chevron-right"></i></td></tr>''' for item in items)
    empty_hidden = " hidden" if items else ""
    data_json = json.dumps({item["id"]: {k: v for k, v in item.items() if k != "reason_chips"} for item in items}).replace("</", "<\\/")
    category_json = json.dumps(categories).replace("</", "<\\/")
    return f'''
    <section class="card review-card" id="review-card" tabindex="-1"><div class="review-title"><div><h2>Review queue</h2><p class="muted"><span id="review-count" class="num">{len(items)}</span> to review</p></div></div>
      <div class="table-scroll" id="review-list" tabindex="-1"><table class="review-table"><thead><tr><th>Date</th><th>Source</th><th>Description</th><th>Reason</th><th class="right">Amount</th></tr></thead><tbody>{rows}</tbody></table></div>
      <div class="queue-empty" id="queue-empty"{empty_hidden}><div class="empty-normal"><span class="empty-icon"><i data-lucide="check-check"></i></span><h2>All clear — nothing needs your review</h2><p>New items will appear here when statements come in.</p><a class="btn secondary" href="/import">Import statement</a></div><div class="victory-line" hidden><span class="victory-icon"><i data-lucide="party-popper"></i></span><h2></h2><p>New items will appear here when statements come in.</p><a class="btn secondary" href="/import">Import statement</a></div></div>
    </section>
    <div class="review-scrim" id="review-scrim"></div><aside class="review-drawer" id="review-drawer" aria-hidden="true" aria-labelledby="drawer-amount"><div class="sheet-handle"></div>
      <div class="drawer-skeleton" id="drawer-skeleton" aria-hidden="true"><div class="skeleton-block drawer-sk-amount"></div>{''.join('<div class="drawer-sk-pair"><div class="skeleton-block sk-label"></div><div class="skeleton-block sk-line medium"></div></div>' for _ in range(5))}</div>
      <form id="review-form" method="post"><input type="hidden" name="transaction_id" id="drawer-id"><input type="hidden" name="action" id="drawer-action" value="approve">
        <div class="drawer-scroll"><div class="drawer-head"><div><div class="drawer-amount" id="drawer-amount"></div><div class="caption" id="drawer-direction"></div></div><button type="button" class="icon-button" id="drawer-close" aria-label="Close"><i data-lucide="x"></i></button></div>
          <section class="evidence"><div class="kv"><span>Source</span><strong class="drawer-source" id="drawer-source"></strong></div><div class="kv"><span>Date</span><strong id="drawer-date"></strong></div><div class="kv" id="drawer-time-row"><span>Time</span><strong id="drawer-time"></strong></div><div id="drawer-extra"></div><label class="evidence-label">Statement description</label><div class="statement-description" id="drawer-description"></div>
            <div class="why-block"><label>Why it’s here</label><div id="drawer-reasons"></div><p class="caption" id="drawer-why"></p><span class="guess-chip" id="drawer-guess-chip"></span></div></section>
          <section class="verdict"><div class="future-split-space" aria-hidden="true"></div><label for="category-input">Category</label><div class="combo-wrap"><input id="category-input" name="category" autocomplete="off" role="combobox" aria-controls="category-options" aria-expanded="false" placeholder="Search categories"><div class="combo-options" id="category-options" role="listbox"></div></div>
            <label for="review-note">Note</label><textarea id="review-note" name="notes" rows="3" placeholder="Add a note — e.g. 'advance for the sofa'"></textarea>
            <label class="remember-row"><span class="switch"><input type="checkbox" id="remember-toggle" name="remember" value="yes"><span></span></span><strong>Remember for future matches</strong></label><p class="remember-copy caption" id="remember-copy" hidden></p>
          </section></div>
        <div class="drawer-actions"><button type="submit" class="approve-action" data-action="approve">Approve</button><button type="submit" class="secondary" data-action="transfer">Mark as transfer</button><button type="button" class="exclude-action" id="exclude-action">Exclude</button><span class="exclude-confirm" id="exclude-confirm" hidden>Exclude? <button type="submit" data-action="exclude">Yes</button><button type="button" id="exclude-no">No</button></span><p class="form-error" id="review-error" role="alert"></p></div>
      </form></aside><canvas id="queue-confetti" aria-hidden="true"></canvas>
    <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/dist/confetti.browser.min.js"></script>
    <style>
      .review-source .bank-logo.dense{{width:24px;height:24px;flex-basis:24px}}.drawer-source .bank-logo.dense{{width:28px;height:28px;flex-basis:28px}}.drawer-skeleton{{display:none;padding:var(--sp-5)}}.review-drawer.details-loading .drawer-skeleton{{display:block}}.review-drawer.details-loading form{{visibility:hidden}}.drawer-sk-amount{{width:120px;height:32px;margin-bottom:var(--sp-5)}}.drawer-sk-pair{{display:flex;justify-content:space-between;gap:var(--sp-4);margin:var(--sp-4) 0}}.drawer-sk-pair .sk-line{{width:55%;margin:0}}
      .review-title{{display:flex;justify-content:space-between}}.review-title h2{{margin-bottom:var(--sp-1)}}.review-title p{{margin:0}}.review-row{{cursor:pointer;transition:background 150ms ease-out}}.review-row:hover,.review-row:focus{{background:var(--brand-050);outline:none}}.review-source{{display:flex;align-items:center;gap:var(--sp-2);min-width:170px}}.reason-chips{{display:flex;flex-wrap:wrap;gap:var(--sp-1)}}.reason-chip{{display:inline-flex;padding:3px 8px;border-radius:var(--radius-full);background:var(--brand-050);color:var(--ink-600);font-size:var(--text-caption);font-weight:600}}.reason-more{{background:var(--border)}}.review-amount{{text-align:right!important;font:600 var(--text-body)/1.5 var(--font-num);white-space:nowrap;color:var(--ink-900)}}.review-amount svg{{width:16px;height:16px;color:var(--ink-400);vertical-align:-3px;margin-left:var(--sp-2);opacity:0;transition:opacity 150ms}}.review-row:hover .review-amount svg,.review-row:focus .review-amount svg{{opacity:1}}
      .review-scrim{{position:fixed;inset:0;background:rgba(20,35,30,.3);opacity:0;visibility:hidden;transition:opacity 250ms;z-index:30}}.review-scrim.open{{opacity:1;visibility:visible}}.review-drawer{{position:fixed;right:0;top:0;width:420px;max-width:100%;height:100vh;background:var(--surface);box-shadow:var(--shadow-hover);border-radius:var(--radius-xl) 0 0 var(--radius-xl);transform:translateX(105%);visibility:hidden;transition:transform 250ms cubic-bezier(.16,1,.3,1),visibility 250ms;z-index:31;overflow:hidden}}.review-drawer.open{{transform:translateX(0);visibility:visible}}.review-drawer.closing{{transition-duration:180ms}}.review-drawer form{{height:100%;display:flex;flex-direction:column}}.drawer-scroll{{padding:var(--sp-5);overflow:auto;flex:1}}.drawer-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:var(--sp-3)}}.drawer-amount{{font:700 var(--text-display)/1.1 var(--font-num);letter-spacing:-.01em}}.evidence{{margin-top:var(--sp-5)}}.kv{{display:flex;justify-content:space-between;gap:var(--sp-4);padding:var(--sp-2) 0}}.kv>span,.evidence-label{{color:var(--ink-400);font-size:var(--text-label);font-weight:600}}.kv strong{{font-size:var(--text-body);font-weight:600;text-align:right}}.drawer-source{{display:flex;align-items:center;gap:var(--sp-2)}}.statement-description{{background:var(--brand-050);border-radius:var(--radius-sm);padding:var(--sp-3);overflow-wrap:anywhere;white-space:pre-wrap}}.evidence-label{{margin-top:var(--sp-3)}}.why-block{{margin-top:var(--sp-5)}}.why-block label{{color:var(--ink-900)}}.why-block .caption{{margin:var(--sp-2) 0}}.guess-chip{{display:inline-flex;border:1px dashed var(--ink-400);border-radius:var(--radius-full);padding:3px 8px;color:var(--ink-600);font-size:var(--text-caption);font-weight:600}}.verdict{{border-top:1px solid var(--border);margin-top:var(--sp-5);padding-top:var(--sp-4)}}.future-split-space{{height:12px}}.verdict>label:not(:first-of-type){{margin-top:var(--sp-4)}}.combo-wrap{{position:relative}}.combo-options{{position:absolute;left:0;right:0;top:calc(100% + 4px);max-height:210px;overflow:auto;background:var(--surface);box-shadow:var(--shadow-hover);border-radius:var(--radius-md);z-index:2;display:none}}.combo-options.open{{display:block}}.combo-option{{padding:var(--sp-2) var(--sp-3);cursor:pointer}}.combo-option:hover,.combo-option.active{{background:var(--brand-050)}}.combo-option svg{{width:16px;vertical-align:-3px;margin-right:var(--sp-1)}}.remember-row{{display:flex!important;align-items:center;gap:var(--sp-2);margin-top:var(--sp-4);color:var(--ink-900)!important;cursor:pointer}}.switch input{{position:absolute;opacity:0}}.switch>span{{display:block;width:38px;height:22px;padding:2px;border-radius:var(--radius-full);background:var(--ink-400);transition:background 150ms}}.switch>span:after{{content:"";display:block;width:18px;height:18px;border-radius:50%;background:var(--surface);transition:transform 150ms}}.switch input:checked+span{{background:var(--brand-700)}}.switch input:checked+span:after{{transform:translateX(16px)}}.remember-copy{{margin:var(--sp-2) 0 0 46px}}.drawer-actions{{position:sticky;bottom:0;padding:var(--sp-3) var(--sp-5);border-top:1px solid var(--border);background:var(--surface);display:flex;flex-wrap:wrap;gap:var(--sp-2)}}.exclude-action,.exclude-confirm button{{background:transparent;color:var(--ink-600);padding:var(--sp-2)}}.exclude-confirm{{display:flex;align-items:center;gap:var(--sp-1);color:var(--ink-600)}}.form-error{{width:100%;margin:0;color:var(--dang-700);font-size:var(--text-caption)}}.queue-empty{{padding:var(--sp-7) var(--sp-4);text-align:center}}.empty-icon,.victory-icon{{width:48px;height:48px;margin:0 auto var(--sp-3);display:grid;place-items:center;border-radius:50%;background:var(--brand-100);color:var(--brand-700)}}.queue-empty h2{{font-weight:800;margin-bottom:var(--sp-2)}}.queue-empty p{{color:var(--ink-600);font-size:var(--text-caption);margin:0}}.victory-line.landed{{animation:victory-in 300ms ease-out both}}#queue-confetti{{position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:100;display:none}}.review-row.resolved{{overflow:hidden;animation:row-collapse 250ms ease-out forwards}}.beacon.happy{{animation:beacon-happy 400ms cubic-bezier(.34,1.56,.64,1)}}@keyframes row-collapse{{to{{opacity:0;transform:scaleY(0);height:0}}}}@keyframes victory-in{{from{{opacity:0;transform:scale(.9)}}to{{opacity:1;transform:scale(1)}}}}@keyframes beacon-happy{{50%{{transform:translateY(-3px)}}}}.sheet-handle{{display:none}}
      @media(max-width:767px){{.review-drawer{{left:0;right:0;top:auto;bottom:0;width:100%;height:85vh;border-radius:var(--radius-xl) var(--radius-xl) 0 0;transform:translateY(105%)}}.review-drawer.open{{transform:translateY(0)}}.review-drawer .sheet-handle{{display:block;width:40px;height:4px;background:var(--border);border-radius:var(--radius-full);margin:8px auto -8px}}}}
      @media(prefers-reduced-motion:reduce){{.review-scrim,.review-drawer,.review-row,.victory-line,.beacon{{transition:none!important;animation:none!important}}.review-row.resolved{{display:none}}}}
    </style>
    <script>
    (()=>{{const items={data_json},categories={category_json},drawer=document.getElementById('review-drawer'),scrim=document.getElementById('review-scrim'),form=document.getElementById('review-form'),input=document.getElementById('category-input'),options=document.getElementById('category-options'),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;let current=null,armed=Object.keys(items).length>0,celebrated=false;
      const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
      function renderOptions(){{const q=input.value.trim(),matches=categories.filter(x=>x.toLowerCase().includes(q.toLowerCase()));let shown=matches.slice(0,12);options.innerHTML=shown.map(x=>`<div class="combo-option" role="option" data-value="${{esc(x)}}">${{esc(x)}}</div>`).join('');if(q&&!matches.length)options.innerHTML+=`<div class="combo-option create" role="option" data-value="${{esc(q)}}"><i data-lucide="plus"></i>Create '${{esc(q)}}'</div>`;options.classList.toggle('open',!!q||document.activeElement===input);input.setAttribute('aria-expanded',options.classList.contains('open'));lucide.createIcons()}}
      function updateRemember(){{const on=document.getElementById('remember-toggle').checked,copy=document.getElementById('remember-copy');copy.hidden=!on;copy.textContent=`New transactions matching '${{current?.description||''}}' will be auto-categorized as ${{input.value||'the selected category'}}, with this note attached.`}}
      function open(id){{current=items[id];if(!current)return;document.getElementById('drawer-id').value=id;document.getElementById('drawer-amount').textContent=current.amount;document.getElementById('drawer-direction').textContent=current.direction;document.getElementById('drawer-date').textContent=current.date;document.getElementById('drawer-description').textContent=current.description;document.getElementById('drawer-time-row').hidden=!current.time;document.getElementById('drawer-time').textContent=current.time;document.getElementById('drawer-reasons').innerHTML=current.reasons.map(x=>`<span class="reason-chip">${{esc(x)}}</span>`).join('');document.getElementById('drawer-why').textContent=`Flagged because ${{current.reasons.join(' and ').toLowerCase()}}${{current.guess?' for '+current.guess:''}}.`;const guess=document.getElementById('drawer-guess-chip');guess.hidden=!current.guess;guess.textContent='Best guess: '+current.guess;input.value=current.guess;document.getElementById('review-note').value=current.note;document.getElementById('remember-toggle').checked=false;document.getElementById('remember-copy').hidden=true;document.getElementById('drawer-extra').innerHTML=current.extra.map(x=>`<div class="kv"><span>${{esc(x.label)}}</span><strong>${{esc(x.value)}}</strong></div>`).join('');document.getElementById('drawer-source').innerHTML=(document.querySelector(`[data-review-id="${{CSS.escape(id)}}"] .review-source`)?.innerHTML||esc(current.source));drawer.classList.remove('closing');drawer.classList.add('open');scrim.classList.add('open');drawer.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';setTimeout(()=>document.getElementById('drawer-close').focus(),reduced?0:250)}}
      function close(done){{drawer.classList.add('closing');drawer.classList.remove('open');scrim.classList.remove('open');drawer.setAttribute('aria-hidden','true');document.body.style.overflow='';setTimeout(done||(()=>{{}}),reduced?0:180)}}
      function celebrate(){{if(!armed||celebrated)return;celebrated=true;const empty=document.getElementById('queue-empty'),normal=empty.querySelector('.empty-normal'),victory=empty.querySelector('.victory-line'),lines=['Reviewed like a champ 🏆','Queue zero. Legend.','Nothing left — you did it.','All clear. Go enjoy your money.'],n=Number(sessionStorage.getItem('queueVictoryIndex')||0);sessionStorage.setItem('queueVictoryIndex',(n+1)%lines.length);normal.hidden=true;victory.hidden=false;victory.querySelector('h2').textContent=reduced?'✨ '+lines[n%lines.length]:lines[n%lines.length];victory.classList.toggle('landed',!reduced);if(reduced){{setTimeout(()=>{{victory.hidden=true;normal.hidden=false}},1600);return}}setTimeout(()=>{{const canvas=document.getElementById('queue-confetti');canvas.style.display='block';if(window.confetti){{const fire=confetti.create(canvas,{{resize:true,useWorker:true}}),colors=['#128A63','#58B893','#4E79A7','#8A6FBF','#F2C94C'];for(let i=0;i<10;i++)setTimeout(()=>fire({{particleCount:20,angle:270,spread:55,startVelocity:10,gravity:.65,drift:(i-4.5)*.06,origin:{{x:(i+.5)/10,y:-.04}},colors,shapes:['square','circle'],scalar:.85,ticks:180}}),i*45)}}document.getElementById('coverage-beacon')?.classList.add('happy');setTimeout(()=>document.getElementById('coverage-beacon')?.classList.remove('happy'),400);setTimeout(()=>canvas.remove(),3000)}},700);setTimeout(()=>{{victory.hidden=true;normal.hidden=false}},3500)}}
      document.querySelectorAll('.review-row').forEach(row=>{{row.onclick=()=>open(row.dataset.reviewId);row.onkeydown=e=>{{if(e.key==='Enter'||e.key===' '){{e.preventDefault();open(row.dataset.reviewId)}}}}}});scrim.onclick=()=>close();document.getElementById('drawer-close').onclick=()=>close();document.addEventListener('keydown',e=>{{if(e.key==='Escape'&&drawer.classList.contains('open'))close()}});input.oninput=()=>{{renderOptions();updateRemember()}};input.onfocus=renderOptions;options.onclick=e=>{{const opt=e.target.closest('.combo-option');if(!opt)return;input.value=opt.dataset.value;if(opt.classList.contains('create')&&!categories.some(x=>x.toLowerCase()===input.value.toLowerCase()))categories.push(input.value);options.classList.remove('open');updateRemember()}};document.addEventListener('click',e=>{{if(!e.target.closest('.combo-wrap'))options.classList.remove('open')}});document.getElementById('remember-toggle').onchange=updateRemember;document.getElementById('exclude-action').onclick=()=>{{document.getElementById('exclude-action').hidden=true;document.getElementById('exclude-confirm').hidden=false;if(!document.getElementById('review-note').value)document.getElementById('review-note').placeholder='Why exclude? (optional)'}};document.getElementById('exclude-no').onclick=()=>{{document.getElementById('exclude-action').hidden=false;document.getElementById('exclude-confirm').hidden=true}};
      form.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>document.getElementById('drawer-action').value=b.dataset.action);form.onsubmit=async e=>{{e.preventDefault();const action=document.getElementById('drawer-action').value,error=document.getElementById('review-error');error.textContent='';if(action==='approve'&&!input.value.trim()){{error.textContent='Choose a category before approving';input.focus();return}}const id=current.id;try{{const res=await fetch('/review',{{method:'POST',headers:{{'Accept':'application/json','X-Requested-With':'fetch','Content-Type':'application/x-www-form-urlencoded'}},body:new URLSearchParams(new FormData(form))}}),payload=await res.json();if(!res.ok)throw Error(payload.error||'Could not resolve item');close(()=>{{const row=document.querySelector(`[data-review-id="${{CSS.escape(id)}}"]`);row.classList.add('resolved');setTimeout(()=>{{row.remove();delete items[id];const count=document.getElementById('review-count'),from=Number(count.textContent),to=payload.remaining,start=performance.now();function tick(now){{const p=reduced?1:Math.min(1,(now-start)/400);count.textContent=Math.round(from+(to-from)*p);if(p<1)requestAnimationFrame(tick)}}requestAnimationFrame(tick);document.getElementById('review-list').hidden=to===0;document.getElementById('queue-empty').hidden=to!==0;if(to===0)celebrate();else document.getElementById('review-list').focus()}},reduced?0:250)}})}}catch(err){{error.textContent=err.message}}}};lucide.createIcons();const requested={json.dumps(open_id)};if(requested&&items[requested])setTimeout(()=>open(requested),0)}})();
    </script>'''


def render_page(title: str, body: str, authed: bool = True, beacon_month: str | None = None) -> bytes:
    nav = ""
    if authed:
        nav = """
        <nav aria-label="Primary navigation">
          <a href="/" data-page="Varavu.Selavu"><i data-lucide="layout-dashboard"></i>Dashboard</a>
          <a href="/transactions" data-page="Transactions"><i data-lucide="arrow-left-right"></i>Transactions</a>
          <a href="/review" data-page="Review Queue"><i data-lucide="check-check"></i>Review</a>
          <a href="/rules" data-page="Rules"><i data-lucide="sliders-horizontal"></i>Rules</a>
          <a href="/baselines" data-page="Baselines"><i data-lucide="ruler"></i>Baselines</a>
          <a href="/import" data-page="Import"><i data-lucide="upload"></i>Import</a>
          <a href="/logout"><i data-lucide="log-out"></i>Logout</a>
        </nav>
        """
    beacon, garden_overlay = render_beacon(beacon_month or default_active_month()) if authed else ("", "")
    admin = (f'<a class="icon-button admin-button {"active" if title == "Admin" else ""}" href="/admin" aria-label="Admin" title="Admin"><i data-lucide="settings"></i></a>' if authed else "")
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · Kanakku</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Inter:wght@500;600;700&display=swap" rel="stylesheet">
  <script src="https://d3js.org/d3.v7.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/d3-sankey@0.12.3/dist/d3-sankey.min.js"></script>
  <script src="https://unpkg.com/lucide@0.468.0/dist/umd/lucide.min.js"></script>
  <style>
    :root {{
      --font-ui:"Plus Jakarta Sans",system-ui,sans-serif; --font-num:"Inter",system-ui,sans-serif;
      --text-display:30px; --text-h1:24px; --text-h2:18px; --text-body-lg:15px; --text-body:14px; --text-label:12.5px; --text-caption:12px;
      --brand-700:#0B6B4D; --brand-600:#128A63; --brand-100:#DFF1E8; --brand-050:#EFF7F2;
      --page-bg:#F6F7F4; --surface:#FFFFFF; --border:#E6EAE6; --ink-900:#20302A; --ink-600:#5C6B63; --ink-400:#93A29A;
      --pos-700:#0E7A4F; --pos-100:#E2F3EA; --warn-700:#91600B; --warn-100:#FCEFD6; --dang-700:#B4382D; --dang-100:#FBE6E3; --info-700:#2C5D8F; --info-100:#E4EEF8;
      --viz-inflow:#0B6B4D; --viz-invest:#2AA07C; --viz-fixed:#4E79A7; --viz-other:#8A6FBF; --viz-surplus:#58B893;
      --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px; --sp-6:32px; --sp-7:40px; --sp-8:48px; --sp-9:64px;
      --radius-sm:8px; --radius-md:10px; --radius-lg:16px; --radius-xl:20px; --radius-full:999px;
      --shadow-card:0 1px 2px rgba(20,35,30,.05),0 2px 8px rgba(20,35,30,.06); --shadow-hover:0 4px 12px rgba(20,35,30,.08),0 8px 24px rgba(20,35,30,.08);
    }}
    * {{ box-sizing:border-box; }}
    html {{ -webkit-text-size-adjust:100%; overflow-x:hidden; }}
    body {{ margin:0; background:var(--page-bg); color:var(--ink-900); font:400 var(--text-body)/1.5 var(--font-ui); overflow-x:hidden; }}
    header {{ padding:var(--sp-4) max(var(--sp-5),calc((100vw - 1240px)/2 + var(--sp-5))); border-bottom:1px solid var(--border); background:var(--surface); position:sticky; top:0; z-index:3; }}
    .brand {{ display:flex; align-items:center; justify-content:space-between; gap:var(--sp-4); }} .brand-actions {{ display:flex;align-items:center;gap:var(--sp-2); }}
    h1 {{ font-size:var(--text-h1); line-height:1.2; font-weight:800; margin:0; letter-spacing:-.01em; }}
    h2 {{ font-size:var(--text-h2); line-height:1.25; font-weight:700; margin:0 0 var(--sp-4); letter-spacing:-.01em; }}
    nav {{ display:flex; gap:var(--sp-1); flex-wrap:wrap; margin-top:var(--sp-3); }}
    nav a {{ display:inline-flex; align-items:center; gap:var(--sp-2); text-decoration:none; color:var(--ink-600); padding:var(--sp-2) var(--sp-3); border-radius:var(--radius-md); font-size:var(--text-label); font-weight:600; transition:background 150ms ease-out,color 150ms ease-out; }}
    nav a:hover {{ background:var(--brand-050); color:var(--brand-700); }} nav a.active {{ background:var(--brand-100); color:var(--brand-700); }}
    nav svg {{ width:20px; height:20px; stroke-width:1.75; }}
    main {{ max-width:1240px; margin:0 auto; padding:var(--sp-5); }}
    .grid {{ display:grid; gap:var(--sp-5); }}
    .cards {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
    .two {{ grid-template-columns:minmax(0,2fr) minmax(300px,1fr); align-items:stretch; }} .two>.card {{ height:100%; }}
    .section-gap {{ margin-top:var(--sp-5); }}
    .login-card {{ max-width:420px; margin:var(--sp-9) auto; }}
    .card {{ background:var(--surface); border:0; border-radius:var(--radius-lg); padding:var(--sp-5); min-width:0; overflow:hidden; box-shadow:var(--shadow-card); }}
    .metric-label {{ display:flex; align-items:center; gap:var(--sp-2); color:var(--ink-400); font-size:var(--text-label); line-height:1.4; font-weight:600; letter-spacing:.02em; }}
    .metric-icon {{ width:24px; height:24px; border-radius:var(--radius-sm); display:grid; place-items:center; background:var(--brand-050); color:var(--brand-700); }} .metric-icon svg {{ width:16px; height:16px; stroke-width:1.75; }}
    .metric .value {{ color:var(--ink-900); font:700 var(--text-display)/1.1 var(--font-num); letter-spacing:-.01em; margin-top:var(--sp-3); font-variant-numeric:tabular-nums; }}
    .table-scroll {{ width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }}
    table {{ width:100%; min-width:620px; border-collapse:collapse; }}
    th,td {{ text-align:left; padding:var(--sp-3) var(--sp-2); border-bottom:1px solid var(--border); vertical-align:top; }}
    th {{ color:var(--ink-600); font-size:var(--text-label); font-weight:600; letter-spacing:.02em; }}
    td.right,.right,td:has(code),td:nth-child(n+4) {{ font-variant-numeric:tabular-nums; }} .right {{ text-align:right; font-family:var(--font-num); }}
    .pill {{ display:inline-flex; align-items:center; gap:var(--sp-1); border-radius:var(--radius-full); padding:var(--sp-1) var(--sp-3); background:var(--brand-050); color:var(--ink-600); font-size:var(--text-label); line-height:1.4; font-weight:600; }}
    .pill.red {{ background:var(--dang-100); color:var(--dang-700); }} .pill.green {{ background:var(--pos-100); color:var(--pos-700); }} .pill.blue {{ background:var(--info-100); color:var(--info-700); }}
    form {{ margin:0; }} input,select,textarea {{ width:100%; min-height:40px; padding:var(--sp-2) var(--sp-3); border:1px solid var(--border); border-radius:var(--radius-md); background:var(--surface); color:var(--ink-900); font:500 var(--text-body)/1.5 var(--font-ui); }}
    input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible,a:focus-visible,.switch input:focus-visible+span {{ outline:2px solid rgba(18,138,99,.4); outline-offset:2px; }}
    select {{ appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--ink-600) 50%),linear-gradient(135deg,var(--ink-600) 50%,transparent 50%);background-position:calc(100% - 17px) 17px,calc(100% - 12px) 17px;background-size:5px 5px;background-repeat:no-repeat;padding-right:36px; }}
    input[type=file]::file-selector-button {{ border:0;border-radius:var(--radius-sm);background:var(--brand-050);color:var(--brand-700);padding:var(--sp-2) var(--sp-3);margin-right:var(--sp-3);font:600 var(--text-label)/1.5 var(--font-ui);cursor:pointer; }}
    .human-control {{ display:flex;align-items:center;gap:var(--sp-1); }} .human-control input {{ text-align:center; }} .human-control button {{ flex:none;width:40px;padding:0;background:var(--brand-050);color:var(--brand-700); }}
    label {{ display:block; color:var(--ink-600); font-size:var(--text-label); font-weight:600; margin-bottom:var(--sp-1); }}
    .form-grid {{ display:grid; gap:var(--sp-3); grid-template-columns:repeat(3,minmax(0,1fr)); }}
    .span-2 {{ grid-column:span 2; }} .align-end {{ align-self:end; }}
    button,.btn {{ border:0; border-radius:var(--radius-md); background:var(--brand-700); color:var(--surface); padding:var(--sp-2) var(--sp-4); min-height:40px; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; justify-content:center; gap:var(--sp-2); font:600 var(--text-body)/1.5 var(--font-ui); transition:background 150ms ease-out,transform 150ms ease-out; }} button:hover,.btn:hover {{ background:var(--brand-600); }}
    button.secondary,.btn.secondary {{ background:var(--brand-050); color:var(--brand-700); }}
    .muted {{ color:var(--ink-600); }} .num,code,time {{ font-family:var(--font-num); font-variant-numeric:tabular-nums; }}
    .stack {{ display:flex; gap:var(--sp-2); flex-wrap:wrap; align-items:center; }}
    .chart {{ min-height:420px; width:100%; overflow:hidden; }}
    .chart svg {{ display:block; max-width:100%; }}
    .chart-fallback {{ display:flex; min-height:260px; align-items:center; justify-content:center; padding:var(--sp-5); border:1px dashed var(--border); border-radius:var(--radius-sm); color:var(--ink-600); text-align:center; }}
    .notice {{ padding:var(--sp-3) var(--sp-4); border-radius:var(--radius-sm); background:var(--info-100); color:var(--info-700); margin-bottom:var(--sp-5); }}
    .amount {{ font-family:var(--font-num);font-variant-numeric:tabular-nums; }} .empty-state p {{ color:var(--ink-600);margin:0 0 var(--sp-3); }}
    /* Brief 3.2: shared skeleton, page transition, list motion, and toast system. */
    .skeleton-block {{ position:relative;overflow:hidden;background:color-mix(in srgb,var(--border) 60%,transparent);border-radius:var(--radius-sm); }}
    .skeleton-block::after {{ content:"";position:absolute;inset:0;transform:translateX(-100%);background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent);animation:skeleton-shimmer 1.4s linear infinite; }}
    @keyframes skeleton-shimmer {{ to{{transform:translateX(100%)}} }}
    .page-skeleton {{ display:none;max-width:1240px;margin:0 auto;padding:var(--sp-5);opacity:0;transition:opacity 150ms ease; }}
    body.loading-skeleton .page-skeleton {{ display:block;opacity:1; }} body.loading-skeleton main {{ display:none; }}
    .skeleton-kpis {{ display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:var(--sp-5); }} .skeleton-card {{ height:118px;padding:var(--sp-5);background:var(--surface);border-radius:var(--radius-lg);box-shadow:var(--shadow-card); }}
    .sk-label {{ width:60px;height:12px; }} .sk-value {{ width:120px;height:30px;margin-top:var(--sp-4); }}
    .skeleton-layout {{ display:grid;grid-template-columns:2fr 1fr;gap:var(--sp-5);margin-top:var(--sp-5); }} .sk-chart {{ height:420px;border-radius:var(--radius-lg); }} .sk-panel {{ height:420px;border-radius:var(--radius-lg);padding:var(--sp-5);background:var(--surface); }}
    .sk-line {{ height:12px;margin:var(--sp-3) 0; }} .sk-line.short {{ width:38%; }} .sk-line.medium {{ width:68%; }}
    .skeleton-table,.skeleton-form {{ margin-top:var(--sp-5);padding:var(--sp-5);background:var(--surface);border-radius:var(--radius-lg); }} .sk-table-row {{ display:grid;grid-template-columns:14% 22% 32% 20% 12%;gap:var(--sp-3);margin:var(--sp-3) 0; }} .sk-table-row .skeleton-block {{ height:12px; }}
    .sk-form-row {{ display:grid;grid-template-columns:repeat(3,1fr);gap:var(--sp-3); }} .sk-field .sk-label {{ margin-bottom:var(--sp-2); }} .sk-input {{ height:40px;border-radius:var(--radius-md); }}
    main.page-ready {{ animation:page-fade-in 150ms ease both; }} @keyframes page-fade-in{{from{{opacity:0}}to{{opacity:1}}}}
    .list-transition-item {{ overflow:hidden;transition:height 250ms ease,opacity 250ms ease,transform 250ms ease; }} .list-transition-item.removing {{ height:0!important;opacity:0;transform:scaleY(.95); }}
    .app-toast {{ position:fixed;left:50%;bottom:var(--sp-5);transform:translate(-50%,12px);display:flex;align-items:center;gap:var(--sp-2);max-width:min(90vw,520px);background:var(--surface);box-shadow:var(--shadow-hover);border-radius:var(--radius-md);padding:var(--sp-3) var(--sp-4);opacity:0;visibility:hidden;transition:opacity 150ms ease,transform 150ms ease;z-index:110; }} .app-toast.show{{opacity:1;visibility:visible;transform:translate(-50%,0)}} .app-toast svg{{width:18px;color:var(--pos-700);flex:none}}
    .coverage-head {{ display:flex; align-items:center; justify-content:space-between; gap:var(--sp-4); flex-wrap:wrap; }}
    .coverage-head form {{ display:flex; align-items:end; gap:var(--sp-2); }}
    .coverage-head input {{ width:145px; }}
    .coverage-list {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:var(--sp-2); margin-top:var(--sp-4); }}
    .coverage-item {{ display:flex; gap:var(--sp-3); align-items:center; min-width:0; padding:var(--sp-3); border:1px solid var(--border); border-radius:var(--radius-lg); }}
    .bank-logo {{ flex:0 0 40px; width:40px; height:40px; border:1px solid var(--border); border-radius:var(--radius-md); display:grid; place-items:center; overflow:hidden; background:var(--brand-100); color:var(--brand-700); font-weight:700; }}
    .bank-logo>* {{ grid-area:1/1; }} .bank-logo img {{ width:100%; height:100%; object-fit:contain; padding:6px; background:var(--surface); z-index:1; }} .bank-logo .monogram {{ font-size:var(--text-body); }} .coverage-item.missing .bank-logo {{ filter:grayscale(1); opacity:.55; }}
    .coverage-copy {{ min-width:0; flex:1; }} .coverage-name {{ display:block; overflow-wrap:anywhere; font-size:var(--text-body-lg); line-height:1.5; font-weight:500; }}
    .coverage-state {{ margin-top:var(--sp-1); }} .status-icon {{ width:16px; height:16px; stroke-width:1.75; }}
    .coverage-item.present .coverage-state {{ background:var(--pos-100); color:var(--pos-700); }} .coverage-item.pending .coverage-state {{ background:var(--warn-100); color:var(--warn-700); }} .coverage-item.missing .coverage-state {{ background:var(--dang-100); color:var(--dang-700); }}
    .caption {{ color:var(--ink-600); font-size:var(--text-caption); font-weight:500; }}
    .creep-list {{ display:grid; gap:var(--sp-4); }} .creep-row {{ padding-bottom:var(--sp-4); border-bottom:1px solid var(--border); }} .creep-row:last-child {{ border:0; padding-bottom:0; }}
    .creep-top,.creep-amounts {{ display:flex; justify-content:space-between; align-items:center; gap:var(--sp-2); }} .creep-name {{ font-size:var(--text-body-lg); font-weight:500; }} .creep-amounts {{ color:var(--ink-600); font:500 var(--text-caption)/1.4 var(--font-num); margin-top:var(--sp-2); font-variant-numeric:tabular-nums; }}
    .progress {{ height:6px; background:var(--border); border-radius:var(--radius-full); overflow:hidden; margin-top:var(--sp-2); }} .progress-fill {{ height:100%; border-radius:var(--radius-full); background:var(--pos-700); }} .creep-row.over .progress-fill {{ background:var(--dang-700); }} .creep-row.over .creep-status {{ background:var(--dang-100); color:var(--dang-700); }} .creep-row.within .creep-status {{ background:var(--pos-100); color:var(--pos-700); }}
    .icon-button {{ width:36px;height:36px;min-height:36px;padding:0;background:transparent;color:var(--ink-600);border-radius:var(--radius-md); }} .icon-button:hover {{ background:var(--brand-050);color:var(--brand-700); }} .icon-button svg {{ width:20px;height:20px; }}
    a.icon-button {{ display:grid;place-items:center;text-decoration:none; }} .admin-button.active {{ background:var(--brand-100);color:var(--brand-700); }}
    .beacon-wrap {{ position:relative; }} .beacon {{ position:relative;width:36px;height:36px;min-height:36px;padding:0;border-radius:50%;background:var(--brand-100);color:var(--brand-700);overflow:visible; }} .beacon:hover {{ background:var(--brand-100); }}
    .sprout {{ width:30px;height:30px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round; }} .leaf {{ transform-box:fill-box;transform-origin:center bottom;animation:sway 3s ease-in-out infinite alternate; }} .leaf-right {{ animation-direction:alternate-reverse; }} .sparkle {{ position:absolute;right:3px;top:1px;font-size:8px;animation:sparkle 20s ease-out infinite; }}
    .beacon.alert {{ background:var(--dang-100);color:var(--ink-400); }} .beacon.alert::before {{ content:"";position:absolute;inset:0;border:2px solid var(--dang-700);border-radius:50%;animation:radar 2s ease-out infinite; }} .beacon.alert .leaf {{ transform:rotate(-20deg);animation:none; }} .beacon-count {{ position:absolute;right:-5px;top:-5px;width:16px;height:16px;border-radius:50%;display:grid;place-items:center;background:var(--dang-700);color:var(--surface);font:600 11px/1 var(--font-num); }}
    .garden-panel {{ position:fixed;right:16px;top:72px;width:380px;max-height:calc(100vh - 88px);overflow-y:auto;padding:var(--sp-5);background:var(--surface);border-radius:var(--radius-xl);box-shadow:var(--shadow-hover);opacity:0;visibility:hidden;transform:scale(.9);transform-origin:top right;transition:opacity 150ms ease,transform 250ms cubic-bezier(.34,1.56,.64,1),visibility 150ms;z-index:102; }} .garden-panel.open {{ opacity:1;visibility:visible;transform:scale(1); }}
    .garden-head {{ display:flex;justify-content:space-between;gap:var(--sp-3);align-items:flex-start; }} .garden-head h2 {{ margin-bottom:var(--sp-2); }} .garden-rows {{ margin-top:var(--sp-4);display:grid;gap:var(--sp-2); }} .garden-row {{ display:flex;align-items:center;gap:var(--sp-2);padding:var(--sp-2) 0; }} .garden-panel.open .garden-row {{ animation:garden-row-in 220ms ease-out both;animation-delay:calc(var(--row-i)*40ms); }} .garden-name {{ flex:1;min-width:0;font-size:var(--text-body-lg);font-weight:500; }} .garden-state {{ padding:var(--sp-1) var(--sp-2); }} .garden-row.present .garden-state {{ background:var(--pos-100);color:var(--pos-700); }} .garden-row.missing .garden-state {{ background:var(--dang-100);color:var(--dang-700); }} .garden-import {{ min-height:30px;padding:var(--sp-1) var(--sp-2);font-size:var(--text-label); }}
    .bank-logo.dense {{ width:28px;height:28px;flex-basis:28px;border-radius:var(--radius-sm); }} .bank-logo.dense img {{ padding:4px; }} .missing-logo {{ filter:grayscale(1);opacity:.55; }} .garden-meadow {{ margin:var(--sp-4) calc(-1*var(--sp-5)) calc(-1*var(--sp-5));padding:var(--sp-3) var(--sp-5);background:var(--brand-050);display:grid;gap:2px;text-align:center; }} .garden-meadow small {{ color:var(--ink-600); }} .garden-scrim {{ position:fixed;display:block;inset:0;background:rgba(20,35,30,.18);opacity:0;visibility:hidden;transition:opacity 150ms;z-index:101; }} .garden-scrim.open {{ opacity:1;visibility:visible; }} .sheet-handle {{ display:none; }}
    @keyframes sway {{ from{{transform:rotate(-4deg)}} to{{transform:rotate(4deg)}} }} @keyframes sparkle {{ 0%,96%{{opacity:0;transform:scale(0)}} 97%{{opacity:1;transform:scale(1)}} 100%{{opacity:0;transform:scale(1.6)}} }} @keyframes radar {{ from{{opacity:.75;transform:scale(1)}} to{{opacity:0;transform:scale(1.6)}} }} @keyframes garden-row-in {{ from{{opacity:0;transform:translateY(8px)}} to{{opacity:1;transform:translateY(0)}} }}
    @media(max-width:1023px) {{ .cards,.skeleton-kpis {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .two,.skeleton-layout {{ grid-template-columns:1fr; }} .coverage-list {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
    @media(max-width:800px) {{ .form-grid {{ grid-template-columns:1fr; }} }}
    @media(max-width:767px) {{ header {{ padding:var(--sp-4); }} main {{ padding:var(--sp-4); }} .brand {{ align-items:flex-start; }} nav {{ flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none; padding-bottom:var(--sp-1); }} nav::-webkit-scrollbar {{ display:none; }} nav a {{ flex:0 0 auto; }} .card {{ padding:var(--sp-4); }} .chart {{ min-height:340px; }} .garden-panel {{ position:fixed;left:0;right:0;top:auto;bottom:0;width:100%;border-radius:var(--radius-xl) var(--radius-xl) 0 0;transform:translateY(100%);transform-origin:bottom;padding:var(--sp-4); }} .garden-panel.open {{ transform:translateY(0); }} .garden-scrim {{ position:fixed;display:block;inset:0;background:rgba(20,35,30,.3);opacity:0;visibility:hidden;transition:opacity 150ms;z-index:11; }} .garden-scrim.open {{ opacity:1;visibility:visible; }} .sheet-handle {{ display:block;width:44px;height:4px;border-radius:var(--radius-full);background:var(--border);margin:0 auto var(--sp-4); }} .garden-meadow {{ margin:var(--sp-4) calc(-1*var(--sp-4)) calc(-1*var(--sp-4)); }} .garden-row {{ flex-wrap:wrap; }} .garden-name {{ min-width:140px; }} .table-scroll {{ overflow:visible; }} table {{ min-width:0; table-layout:fixed; }} table:not(.keep-table) tbody {{ display:block; }} table:not(.keep-table) tr {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); padding:var(--sp-2) 0; border-bottom:1px solid var(--border); }} table:not(.keep-table) td {{ border:0; padding:var(--sp-1); overflow-wrap:anywhere; }} table:not(.keep-table) tr:first-child:has(th),table:not(.keep-table) thead {{ display:none; }} }}
    @media(max-width:767px) {{ .garden-panel {{ max-height:calc(100vh - 16px);overflow-y:auto;z-index:102; }} .garden-scrim {{ z-index:101; }} }}
    @media(max-width:420px) {{ .metric .value {{ font-size:24px; }} }}
    @media(prefers-reduced-motion:reduce) {{ *,*::before,*::after {{ scroll-behavior:auto!important; transition:none!important; animation:none!important; }} .skeleton-block::after{{display:none}} .app-toast{{transition:opacity 150ms ease!important;transform:translate(-50%,0)}} }}
  </style>
</head>
<body>
  <header><div class="brand"><h1>{html.escape(title)}</h1><div class="brand-actions">{beacon}{admin}<span class="pill blue">Varavu.Selavu v1</span></div></div>{nav}</header>
  <div class="page-skeleton" id="page-skeleton" aria-hidden="true" data-skeleton-page="{html.escape(title)}"><div class="skeleton-kpis">{''.join('<div class="skeleton-card"><div class="skeleton-block sk-label"></div><div class="skeleton-block sk-value"></div></div>' for _ in range(4))}</div><div class="skeleton-layout"><div class="skeleton-block sk-chart"></div><div class="sk-panel"><div class="skeleton-block sk-line short"></div>{''.join('<div class="skeleton-block sk-line medium"></div>' for _ in range(5))}</div></div><div class="skeleton-form"><div class="sk-form-row">{''.join('<div class="sk-field"><div class="skeleton-block sk-label"></div><div class="skeleton-block sk-input"></div></div>' for _ in range(3))}</div></div><div class="skeleton-table"><div class="skeleton-block sk-line short"></div>{''.join('<div class="sk-table-row">'+''.join('<div class="skeleton-block"></div>' for _ in range(5))+'</div>' for _ in range(5))}</div></div>
  {garden_overlay}<main>{body}</main><div class="app-toast" id="app-toast" role="status" aria-live="polite"><i data-lucide="check-circle-2"></i><span></span></div>
  <script>(()=>{{const REDUCED=matchMedia('(prefers-reduced-motion: reduce)').matches,SKELETON_DELAY=150,SKELETON_MIN=300,PAGE_FADE=150;const main=document.querySelector('main');main.classList.add('page-ready');let toastTimer;window.showToast=(message,icon='check-circle-2',duration=2500)=>{{const toast=document.getElementById('app-toast');toast.querySelector('span').textContent=message;toast.querySelector('i,svg')?.setAttribute('data-lucide',icon);toast.classList.add('show');if(window.lucide)lucide.createIcons();clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.classList.remove('show'),duration)}};const params=new URLSearchParams(location.search),toastMessage=params.get('toast');if(toastMessage)showToast(toastMessage,'check-circle-2',Number(params.get('toast_ms'))||2500);document.querySelectorAll('[data-toast-on-load]').forEach(el=>showToast(el.dataset.toastOnLoad,el.dataset.toastIcon||'check-circle-2',Number(el.dataset.toastMs)||2500));function scheduleSkeleton(){{let shown=0;setTimeout(()=>{{shown=performance.now();document.body.classList.add('loading-skeleton')}},SKELETON_DELAY);window.addEventListener('pageshow',()=>{{if(shown)setTimeout(()=>document.body.classList.remove('loading-skeleton'),Math.max(0,SKELETON_MIN-(performance.now()-shown)))}} ,{{once:true}})}}document.addEventListener('click',e=>{{const a=e.target.closest('a[href]');if(a&&a.origin===location.origin&&!a.hasAttribute('download')&&!a.href.includes('#'))scheduleSkeleton()}});document.addEventListener('submit',scheduleSkeleton);document.querySelectorAll('tbody tr,.logo-row,.garden-row').forEach(el=>el.classList.add('list-transition-item'));document.querySelectorAll('nav a[data-page]').forEach(a=>a.classList.toggle('active',a.dataset.page==={json.dumps(title)})); if(window.lucide) lucide.createIcons({{attrs:{{'stroke-width':1.75}}}}); const beacon=document.getElementById('coverage-beacon'),garden=document.getElementById('account-garden'),scrim=document.getElementById('garden-scrim'); function closeGarden(){{if(!garden)return;garden.classList.remove('open');scrim.classList.remove('open');garden.setAttribute('aria-hidden','true');beacon.setAttribute('aria-expanded','false')}} if(beacon){{beacon.addEventListener('click',e=>{{e.stopPropagation();const opening=!garden.classList.contains('open');closeGarden();if(opening){{garden.classList.add('open');scrim.classList.add('open');garden.setAttribute('aria-hidden','false');beacon.setAttribute('aria-expanded','true')}}}});document.getElementById('garden-close').addEventListener('click',closeGarden);scrim.addEventListener('click',closeGarden);document.addEventListener('click',e=>{{if(!e.target.closest('.beacon-wrap'))closeGarden()}});document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeGarden()}})}}}})();</script>
  <script>(()=>{{const nav=performance.getEntriesByType('navigation')[0];if(!nav||nav.responseEnd-nav.startTime<=150)return;const started=performance.now(),content=document.querySelector('main');document.body.classList.add('loading-skeleton');setTimeout(()=>{{document.body.classList.remove('loading-skeleton');content.classList.remove('page-ready');void content.offsetWidth;content.classList.add('page-ready')}},Math.max(0,300-(performance.now()-started)))}})();</script>
  <script>(()=>{{const beacon=document.getElementById('coverage-beacon'),panel=document.getElementById('account-garden');if(!beacon||!panel)return;const position=()=>{{if(matchMedia('(max-width:767px)').matches){{panel.style.removeProperty('right');panel.style.removeProperty('top');panel.style.removeProperty('max-height');return}}const rect=beacon.getBoundingClientRect(),top=Math.min(rect.bottom+8,innerHeight-16);panel.style.right=Math.max(16,innerWidth-rect.right)+'px';panel.style.top=top+'px';panel.style.maxHeight=Math.max(120,innerHeight-top-16)+'px'}};beacon.addEventListener('click',position,true);panel.addEventListener('click',event=>event.stopPropagation());addEventListener('resize',position);addEventListener('scroll',()=>{{if(panel.classList.contains('open'))position()}},true)}})();</script>
  <script>(()=>{{const brief32Fetch=window.fetch.bind(window);window.fetch=async(...args)=>{{const response=await brief32Fetch(...args);const target=String(args[0]);if(response.ok&&target==='/review')window.showToast?.('Transaction approved');return response}}}})();</script>
  <script>(()=>{{const months=['January','February','March','April','May','June','July','August','September','October','November','December'];document.querySelectorAll('[data-month-control]').forEach(control=>{{const visible=control.querySelector('input[type=text]'),hidden=control.querySelector('input[type=hidden]');control.querySelectorAll('[data-shift]').forEach(button=>button.addEventListener('click',()=>{{const [year,month]=hidden.value.split('-').map(Number),next=new Date(year,month-1+Number(button.dataset.shift),1);hidden.value=next.getFullYear()+'-'+String(next.getMonth()+1).padStart(2,'0');visible.value=months[next.getMonth()]+' '+next.getFullYear()}}))}});document.querySelectorAll('[data-date-display]').forEach(visible=>{{const hidden=visible.nextElementSibling;visible.closest('form')?.addEventListener('submit',e=>{{const match=visible.value.trim().match(/^(\d{{1,2}})\s+([A-Za-z]+)\s+(\d{{4}})$/),month=match?months.findIndex(x=>x.toLowerCase()===match[2].toLowerCase()):-1;if(!match||month<0){{e.preventDefault();visible.setCustomValidity('Use a date like 14 May 2026');visible.reportValidity();return}}visible.setCustomValidity('');hidden.value=match[3]+'-'+String(month+1).padStart(2,'0')+'-'+String(Number(match[1])).padStart(2,'0')}})}})}})();</script>
</body>
</html>"""
    return html_doc.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    sessions: dict[str, str] = {}

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method: str):
        init_db()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/assets/banks/"):
            return self.bank_asset(path)
        if path.startswith("/assets/uploads/bank-logos/"):
            return self.uploaded_logo(path)
        if path.startswith("/api/"):
            return self.api(path)
        if path == "/login":
            return self.login(method)
        if path == "/logout":
            self.clear_session()
            return self.redirect("/login")
        if not self.is_authed():
            return self.redirect("/login")
        routes = {
            "/": self.dashboard,
            "/transactions": self.transactions,
            "/review": self.review,
            "/rules": self.rules,
            "/baselines": self.baselines,
            "/import": self.import_page,
            "/admin": self.admin,
            "/admin/logo": self.admin_logo,
            "/admin/seed-data": self.admin_seed_data,
        }
        handler = routes.get(path)
        if not handler:
            return self.not_found()
        return handler(method)

    def is_authed(self) -> bool:
        raw = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie(raw)
        morsel = jar.get(SESSION_COOKIE)
        return bool(morsel and morsel.value in self.sessions)

    def session_token(self) -> str | None:
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel and morsel.value in self.sessions else None

    def active_month(self) -> str:
        token = self.session_token()
        return self.sessions.get(token, default_active_month()) if token else default_active_month()

    def set_active_month(self, month: str) -> None:
        token = self.session_token()
        if token:
            self.sessions[token] = month

    def set_session(self):
        token = secrets.token_urlsafe(32)
        self.sessions[token] = default_active_month()
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/")

    def clear_session(self):
        raw = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie(raw)
        morsel = jar.get(SESSION_COOKIE)
        if morsel:
            self.sessions.pop(morsel.value, None)
        self.send_response(302)
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Max-Age=0; Path=/")
        self.send_header("Location", "/login")
        self.end_headers()

    def redirect(self, to: str):
        self.send_response(302)
        self.send_header("Location", to)
        self.end_headers()

    def send_html(self, title: str, body: str, authed: bool = True, beacon_month: str | None = None):
        payload = render_page(title, body, authed, beacon_month or (self.active_month() if authed else None))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def bank_asset(self, path: str):
        name = Path(path).name
        allowed = {row[1] for row in BANK_ASSETS}
        asset_path = ROOT / "assets" / "banks" / name
        if name not in allowed or not asset_path.is_file():
            return self.not_found()
        payload = asset_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml" if asset_path.suffix.lower() == ".svg" else "image/png")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def uploaded_logo(self, path: str):
        name = Path(urllib.parse.unquote(path)).name
        with db() as conn:
            row = conn.execute("SELECT content_type FROM account_logos WHERE file_name=?", (name,)).fetchone()
        asset_path = LOGO_UPLOAD_DIR / name
        if not row or not asset_path.is_file():
            return self.not_found()
        payload = asset_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", row["content_type"])
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def form(self) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.read_body().decode("utf-8")).items()}

    def login(self, method: str):
        error = ""
        if method == "POST":
            data = self.form()
            if secrets.compare_digest(data.get("username", ""), APP_USER) and secrets.compare_digest(data.get("password", ""), APP_PASSWORD):
                self.send_response(302)
                self.set_session()
                self.send_header("Location", "/")
                self.end_headers()
                return
            error = "<div class='notice'>Invalid login.</div>"
        body = f"""
        <div class="card login-card">
          <h2>Kanakku Login</h2>
          <p class="muted">One shared household account for v1.</p>
          {error}
          <form method="post">
            <p><label>Username</label><input name="username" autocomplete="username" value="{html.escape(APP_USER)}"></p>
            <p><label>Password</label><input name="password" type="password" autocomplete="current-password"></p>
            <button>Login</button>
          </form>
          <p class="muted">Set `KANAKKU_USER` and `KANAKKU_PASSWORD` before running. Default password is `change-me`.</p>
        </div>
        """
        self.send_html("Login", body, authed=False)

    def dashboard(self, method: str):
        with db() as conn:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            requested_month = (query.get("month") or [None])[0]
            month = dashboard_month(conn, requested_month) if requested_month else self.active_month()
            if requested_month:
                self.set_active_month(month)
            include_seed = seed_data_enabled(conn)
            data = dashboard_data(conn, month)
            s = data["summary"]
            earliest_query = "SELECT min(substr(t.transaction_date,1,7)) FROM transactions t LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id WHERE length(t.transaction_date)>=7 AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))"
            earliest_args: list[object] = []
            has_data_query = "SELECT EXISTS(SELECT 1 FROM transactions t LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id WHERE substr(t.transaction_date,1,7)=? AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))"
            has_data_args: list[object] = [month]
            review_filter = ""
            review_args: list[object] = []
            if not include_seed:
                earliest_query += " AND coalesce(t.import_batch_id, '') != ?"
                earliest_args.append(SEED_BATCH_ID)
                has_data_query += " AND coalesce(t.import_batch_id, '') != ?"
                has_data_args.append(SEED_BATCH_ID)
                review_filter = " AND coalesce(t.import_batch_id, '') != ?"
                review_args.append(SEED_BATCH_ID)
            has_data_query += ")"
            earliest = conn.execute(earliest_query, earliest_args).fetchone()[0] or date.today().strftime("%Y-%m")
            has_data = conn.execute(has_data_query, has_data_args).fetchone()[0]
            reviews = conn.execute(f"""SELECT group_concat(ri.reason, ',') AS reason, t.transaction_id, t.transaction_date, t.description, t.amount, t.source_name
                FROM review_items ri JOIN transactions t ON t.transaction_id=ri.transaction_id LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id WHERE ri.status='open'
                AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))
                {review_filter}
                GROUP BY t.transaction_id ORDER BY max(ri.created_at) DESC LIMIT 8""", review_args).fetchall()
        current = date.today().strftime("%Y-%m")
        display_month = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
        previous, following = shift_month(month, -1), shift_month(month, 1)
        surplus = s["total_inflow"] - s["investments"] - s["fixed"] - s["other"]
        ratio = surplus / s["total_inflow"] if s["total_inflow"] else (-1 if surplus < 0 else 0)
        mood = "healthy" if ratio >= .15 else "tight" if ratio >= 0 else "negative"
        def count_value(value, index):
            return f'<div class="value" data-count-value="{float(value):.2f}" data-count-index="{index}">{money(value)}</div>'
        def creep_row(c):
            over = c["variance"] > 0
            progress = min(100, max(0, (c["actual"] / c["planned"] * 100) if c["planned"] else 0))
            label = html.escape(c["category"]) + (" / " + html.escape(c["subcategory"]) if c["subcategory"] else "")
            return (f'<div class="creep-row {"over" if over else "within"}"><div class="creep-top"><span class="creep-name">{label}</span><span class="pill creep-status">{"Over" if over else "Within"}</span></div>'
                    f'<div class="progress" role="progressbar" aria-label="{label}" aria-valuenow="{progress:.0f}" aria-valuemin="0" aria-valuemax="100"><div class="progress-fill" data-progress="{progress:.1f}"></div></div>'
                    f'<div class="creep-amounts"><span><b data-count-value="{float(c["actual"]):.2f}">{money(c["actual"])}</b> spent</span><span><b data-count-value="{float(c["planned"]):.2f}">{money(c["planned"])}</b> planned</span></div></div>')
        creep_rows = "".join(creep_row(c) for c in data["creep"])
        review_rows = "".join(
            f'''<tr class="review-preview-row" onclick="location.href='/review?open={urllib.parse.quote(r['transaction_id'])}'" tabindex="0" onkeydown="if(event.key==='Enter'||event.key===' ')location.href='/review?open={urllib.parse.quote(r['transaction_id'])}'">
              <td>{human_date(r['transaction_date'])}</td><td><span class="review-source">{render_bank_logo(r['source_name'] or 'Bank', True, False, source_id_for(r['source_name']))}<span>{html.escape(r['source_name'] or 'Unknown')}</span></span></td>
              <td>{html.escape(r['description'])}</td><td>{render_reason_chips(r['reason'])}</td><td class="review-amount">{money(r['amount'])}<i data-lucide="chevron-right"></i></td></tr>''' for r in reviews)
        picker_months = "".join(f'<button data-pick-month="{i:02d}">{datetime(2000,i,1).strftime("%b")}</button>' for i in range(1,13))
        empty = (f'''<section class="card empty-state"><span class="empty-icon"><i data-lucide="inbox"></i></span><h2>Nothing recorded for {html.escape(display_month)}</h2><p class="muted">Import a statement to bring this month to life.</p><a class="btn secondary" href="/import?month={month}">Import statements</a></section>''' if not has_data else "")
        dashboard_content = "" if not has_data else f'''
        <section class="grid cards section-gap dashboard-content">
          <div class="card metric"><div class="metric-label"><span class="metric-icon"><i data-lucide="trending-up"></i></span>Total inflow</div>{count_value(s['total_inflow'],0)}</div>
          <div class="card metric"><div class="metric-label"><span class="metric-icon"><i data-lucide="sprout"></i></span>Investments</div>{count_value(s['investments'],1)}</div>
          <div class="card metric"><div class="metric-label"><span class="metric-icon"><i data-lucide="receipt"></i></span>Fixed expenses</div>{count_value(s['fixed'],2)}</div>
          <div class="card metric"><div class="metric-label"><span class="metric-icon"><i data-lucide="shopping-bag"></i></span>Other expenses</div>{count_value(s['other'],3)}</div>
        </section>
        <section class="grid two section-gap dashboard-content"><div class="card cash-card"><div class="cash-head"><h2>Cash flow</h2><div class="chart-toggle" role="tablist" aria-label="Cash flow view">
          <button class="active" data-view="sankey" title="Sankey"><i data-lucide="git-branch"></i><span>Sankey</span></button><button data-view="waterfall" title="Waterfall"><i data-lucide="bar-chart-3"></i><span>Waterfall</span></button><button data-view="treemap" title="Treemap"><i data-lucide="layout-grid"></i><span>Treemap</span></button><button data-view="bars" title="Bars"><i data-lucide="align-left"></i><span>Bars</span></button></div></div><div id="cash-chart" class="chart" data-default-view="sankey"></div></div>
          <div class="card"><h2>Creep Watch</h2><div class="creep-list">{creep_rows or '<p class="muted">No active baselines for this month.</p>'}</div></div></section>
        <section class="card section-gap dashboard-content review-preview"><h2>Review Queue</h2><p class="muted"><span class="num">{s['review_count']}</span> to review</p><div class="table-scroll"><table><thead><tr><th>Date</th><th>Source</th><th>Description</th><th>Reason</th><th class="right">Amount</th></tr></thead><tbody>{review_rows}</tbody></table></div></section>
        <style>.review-preview-row{{cursor:pointer;transition:background 150ms ease-out}}.review-preview-row:hover,.review-preview-row:focus{{background:var(--brand-050);outline:none}}.review-source{{display:flex;align-items:center;gap:var(--sp-2);min-width:170px}}.reason-chips{{display:flex;gap:var(--sp-1);flex-wrap:wrap}}.reason-chip{{display:inline-flex;padding:3px 8px;border-radius:var(--radius-full);background:var(--brand-050);color:var(--ink-600);font-size:var(--text-caption);font-weight:600}}.reason-more{{background:var(--border)}}.review-amount{{text-align:right;font:600 var(--text-body)/1.5 var(--font-num);white-space:nowrap;color:var(--ink-900)}}.review-amount svg{{width:16px;height:16px;color:var(--ink-400);vertical-align:-3px;margin-left:var(--sp-2);opacity:0;transition:opacity 150ms}}.review-preview-row:hover .review-amount svg,.review-preview-row:focus .review-amount svg{{opacity:1}}</style>'''
        body = f'''
        <style>
          .hero {{ min-height:96px;padding:var(--sp-5);border-radius:var(--radius-lg);display:flex;align-items:center;justify-content:space-between;gap:var(--sp-5);background-size:200% 200%;animation:hero-breathe 16s ease-in-out infinite alternate; }} .hero.healthy{{background-image:linear-gradient(135deg,#DFF1E8,#EAF6EE)}} .hero.tight{{background-image:linear-gradient(135deg,#FCEFD6,#FaF3E4)}} .hero.negative{{background-image:linear-gradient(135deg,#FBE6E3,#F9EFEA)}} @keyframes hero-breathe{{from{{background-position:0 0}}to{{background-position:100% 100%}}}}
          .greeting {{ font-size:var(--text-h2);font-weight:800;margin:0 0 var(--sp-1); }} .month-nav,.month-core {{ display:flex;align-items:center;gap:var(--sp-1); }} .month-nav {{ position:relative;gap:var(--sp-2); }} .month-label {{ min-width:140px;background:transparent;color:var(--ink-900);padding:var(--sp-2);font-size:var(--text-h2);font-weight:700; }} .month-label:hover{{background:var(--brand-050)}} .today-button{{background:transparent;color:var(--brand-700);font-size:var(--text-label);padding:var(--sp-2);}} .today-button:hover{{background:var(--brand-050)}} .month-popover{{position:absolute;right:0;top:44px;width:292px;padding:var(--sp-4);background:var(--surface);border-radius:var(--radius-xl);box-shadow:var(--shadow-hover);z-index:10;display:none}} .month-popover.open{{display:block}} .year-stepper{{display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--sp-3)}} .month-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--sp-1)}} .month-grid button{{background:transparent;color:var(--ink-600);min-height:36px;padding:var(--sp-1)}} .month-grid button:hover,.month-grid button.selected{{background:var(--brand-100);color:var(--brand-700)}} .month-grid button:disabled{{opacity:.4;cursor:not-allowed}}
          .cash-head{{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3);margin-bottom:var(--sp-4)}} .cash-head h2{{margin:0}} .chart-toggle{{display:flex;padding:3px;background:var(--brand-050);border-radius:var(--radius-full);overflow:auto}} .chart-toggle button{{min-height:34px;padding:var(--sp-1) var(--sp-3);border-radius:var(--radius-full);background:transparent;color:var(--ink-600);font-size:var(--text-label)}} .chart-toggle button.active{{background:var(--surface);color:var(--brand-700);box-shadow:var(--shadow-card)}} .chart-toggle svg{{width:16px;height:16px}} #cash-chart.switching{{opacity:0;transform:translateY(4px)}} #cash-chart{{transition:opacity 150ms ease,transform 150ms ease}} .chart-bar-label{{font:500 12px var(--font-ui);fill:var(--ink-600)}} .chart-amount{{font:600 12px var(--font-num);fill:var(--ink-900);font-variant-numeric:tabular-nums}} .treemap-label{{fill:white;font:600 12.5px var(--font-ui)}} .treemap-amount{{fill:white;font:600 12px var(--font-num)}} .treemap-percent{{fill:rgba(255,255,255,.72);font:500 12px var(--font-ui)}} .treemap-tile{{transition:filter 120ms ease}} .treemap-tile:hover{{filter:brightness(1.06)}} .treemap-legend{{display:flex;gap:var(--sp-2);flex-wrap:wrap;padding-top:var(--sp-3)}} .treemap-chip{{display:inline-flex;align-items:center;gap:var(--sp-2);font-size:var(--text-caption);color:var(--ink-600)}} .treemap-swatch{{width:10px;height:10px;border-radius:2px}} .empty-state{{text-align:center;padding:var(--sp-8);margin-top:var(--sp-5)}} .empty-icon{{width:48px;height:48px;border-radius:50%;display:grid;place-items:center;margin:0 auto var(--sp-3);background:var(--brand-050);color:var(--brand-700)}} .empty-icon svg{{width:24px}}
          .progress-fill{{width:0;transition:width 500ms cubic-bezier(.16,1,.3,1)}} .creep-amounts b{{font:inherit;font-variant-numeric:tabular-nums}}
          @media(max-width:767px){{.hero{{padding:var(--sp-4);align-items:flex-start;flex-direction:column}}.month-nav{{width:100%;justify-content:space-between}}.month-label{{min-width:120px}}.chart-toggle button span{{display:none}}.chart-toggle button{{padding:var(--sp-2)}}.cash-head{{align-items:flex-start}}}}
          @media(prefers-reduced-motion:reduce){{.hero{{animation:none}}#cash-chart,.progress-fill{{transition:none}}}}
        </style>
        <section class="hero {mood}" data-dashboard-hero data-month="{month}"><div><p class="greeting" id="greeting">Welcome back, Vignesh.</p><span class="caption">{html.escape(display_month)} so far: {money(s['total_inflow'])} in · {money(s['total_spend'])} out</span></div>
          <div class="month-nav"><div class="month-core"><button class="icon-button month-change" data-dir="-1" aria-label="Previous month" {'disabled' if month <= earliest else ''}><i data-lucide="chevron-left"></i></button><button class="month-label" id="month-label" aria-haspopup="dialog">{html.escape(display_month)}</button><button class="icon-button month-change" data-dir="1" aria-label="Next month" {'disabled' if month >= current else ''}><i data-lucide="chevron-right"></i></button></div>{f'<a class="today-button" href="/?month={current}">Today</a>' if month != current else ''}
            <div class="month-popover" id="month-picker"><div class="year-stepper"><button class="icon-button" id="year-prev"><i data-lucide="chevron-left"></i></button><strong id="picker-year">{month[:4]}</strong><button class="icon-button" id="year-next"><i data-lucide="chevron-right"></i></button></div><div class="month-grid">{picker_months}</div></div></div></section>
        {empty}{dashboard_content}
        <script>
        (()=>{{
          const month='{month}', earliest='{earliest}', current='{current}', reduced=matchMedia('(prefers-reduced-motion: reduce)').matches, nf=new Intl.NumberFormat('en-IN',{{maximumFractionDigits:0}}); let navTimer,navTarget=month;
          const addMonth=(m,d)=>{{const x=new Date(m+'-01T12:00:00');x.setMonth(x.getMonth()+d);return x.getFullYear()+'-'+String(x.getMonth()+1).padStart(2,'0')}};const go=m=>{{navTarget=m;clearTimeout(navTimer);document.getElementById('month-label').textContent=new Date(m+'-01T12:00:00').toLocaleDateString('en-IN',{{month:'long',year:'numeric'}});navTimer=setTimeout(()=>location.href='/?month='+m,250)}};const step=d=>{{const target=addMonth(navTarget,d);if(target>=earliest&&target<=current)go(target)}};
          document.querySelectorAll('.month-change:not(:disabled)').forEach(b=>b.onclick=()=>step(+b.dataset.dir)); document.addEventListener('keydown',e=>{{if(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName))return;if(e.key==='ArrowLeft')step(-1);if(e.key==='ArrowRight')step(1)}});
          const picker=document.getElementById('month-picker'),label=document.getElementById('month-label'),yearEl=document.getElementById('picker-year');let pickerYear=+month.slice(0,4);label.onclick=e=>{{e.stopPropagation();picker.classList.toggle('open')}};document.addEventListener('click',e=>{{if(!e.target.closest('.month-nav'))picker.classList.remove('open')}});function updatePicker(){{yearEl.textContent=pickerYear;document.querySelectorAll('[data-pick-month]').forEach(b=>{{const value=pickerYear+'-'+b.dataset.pickMonth;b.disabled=value<earliest||value>current;b.classList.toggle('selected',value===month);b.onclick=()=>go(value)}})}}document.getElementById('year-prev').onclick=()=>{{pickerYear--;updatePicker()}};document.getElementById('year-next').onclick=()=>{{pickerYear++;updatePicker()}};updatePicker();
          const hour=new Date().getHours(),day=Math.floor((new Date()-new Date(new Date().getFullYear(),0,0))/86400000);const lines=hour>=5&&hour<12?['Rise and reconcile, Vignesh ☀️','Fresh coffee, fresh numbers.','Morning, Vignesh — the rupees kept busy overnight.']:hour<17&&hour>=12?['Midday money check — nice.','Back so soon? The spreadsheets missed you.','Afternoon, Vignesh. Let’s see what’s cooking.']:hour<22&&hour>=17?['Evening, Vignesh — let’s tuck the money in.','The markets rest. Your dashboard doesn’t.','Good evening — your rupees have been counted and accounted.']:['Burning the midnight ₹, are we?','Late-night ledger vibes.','The night shift auditor has arrived.'];document.getElementById('greeting').textContent=lines[day%lines.length];
          document.querySelectorAll('[data-count-value]').forEach((el,i)=>{{const end=+el.dataset.countValue,delay=el.dataset.countIndex?+el.dataset.countIndex*60:0;if(reduced){{el.textContent='₹'+nf.format(Math.round(end));return}}el.textContent='₹0';setTimeout(()=>{{const start=performance.now();function frame(now){{const t=Math.min(1,(now-start)/400),ease=1-Math.pow(1-t,4);el.textContent='₹'+nf.format(Math.round(end*ease));if(t<1)requestAnimationFrame(frame)}}requestAnimationFrame(frame)}},delay)}});const fillProgress=()=>document.querySelectorAll('[data-progress]').forEach(el=>el.style.width=el.dataset.progress+'%');reduced?fillProgress():requestAnimationFrame(fillProgress);
          const chart=document.getElementById('cash-chart');if(!chart)return;const values=[{{name:'Investments',value:{s['investments']},color:'--viz-invest'}},{{name:'Fixed expenses',value:{s['fixed']},color:'--viz-fixed'}},{{name:'Other expenses',value:{s['other']},color:'--viz-other'}},{{name:'Surplus',value:{max(0,surplus)},color:'--viz-surplus'}}],inflow={s['total_inflow']},styles=getComputedStyle(document.documentElement),color=v=>styles.getPropertyValue(v).trim(),fmt=v=>'₹'+nf.format(Math.round(v));let graph;
          function svgBase(){{chart.innerHTML='';const w=Math.max(300,chart.clientWidth||700),h=matchMedia('(max-width:560px)').matches?340:420;return [d3.select(chart).append('svg').attr('width',w).attr('height',h),w,h]}}
          function sankey(){{if(!graph||!window.d3||!d3.sankey){{chart.innerHTML='<div class="chart-fallback">Cash flow could not render. Refresh once to retry.</div>';return}}const [svg,w,h]=svgBase(),layout=d3.sankey().nodeId(d=>d.name).nodeWidth(16).nodePadding(14).extent([[8,8],[w-8,h-8]]),d=layout({{nodes:graph.nodes.map(x=>({{...x}})),links:graph.links.map(x=>({{...x}}))}}),cs={{'Total inflow':'--viz-inflow','Investments':'--viz-invest','Fixed expenses':'--viz-fixed','Other expenses':'--viz-other','Surplus / unallocated':'--viz-surplus'}};svg.append('g').selectAll('path').data(d.links).join('path').attr('d',d3.sankeyLinkHorizontal()).attr('stroke',x=>color(cs[x.source.name]||'--ink-400')).attr('stroke-width',x=>Math.max(1,x.width)).attr('fill','none').attr('opacity',reduced ? .28 : 0).transition().duration(reduced?0:350).attr('opacity',.28);const n=svg.append('g').selectAll('g').data(d.nodes).join('g');n.append('rect').attr('x',x=>x.x0).attr('y',x=>x.y0).attr('height',x=>Math.max(1,x.y1-x.y0)).attr('width',x=>x.x1-x.x0).attr('rx',4).attr('fill',x=>color(cs[x.name]||'--ink-400'));n.append('text').attr('x',x=>x.x0<w/2?x.x1+7:x.x0-7).attr('y',x=>(x.y0+x.y1)/2).attr('text-anchor',x=>x.x0<w/2?'start':'end').attr('class','chart-bar-label').text(x=>x.name+' · '+fmt(x.value))}}
          function waterfall(){{const [svg,w,h]=svgBase(),items=[{{name:'Total inflow',value:inflow,color:'--viz-inflow'}},...values.slice(0,3).map(v=>({{...v,value:-v.value}})),{{name:'Surplus',value:Math.max(0,{surplus}),color:'--viz-surplus'}}],max=Math.max(1,inflow),bw=Math.min(72,(w-50)/items.length*.55),x=d3.scaleBand().domain(d3.range(items.length)).range([32,w-16]).padding(.3),y=d3.scaleLinear().domain([0,max]).range([h-55,18]);let running=inflow;items.forEach((d,i)=>{{let top,bottom;if(i===0){{top=inflow;bottom=0}}else if(i===items.length-1){{top=d.value;bottom=0}}else{{top=running;running+=d.value;bottom=running}}const rect=svg.append('rect').attr('x',x(i)+(x.bandwidth()-bw)/2).attr('width',bw).attr('y',reduced?y(top):y(bottom)).attr('height',reduced?Math.abs(y(bottom)-y(top)):0).attr('rx',6).attr('fill',color(d.color));rect.transition().delay(reduced?0:i*60).duration(reduced?0:280).attr('y',y(top)).attr('height',Math.abs(y(bottom)-y(top)));svg.append('text').attr('x',x(i)+x.bandwidth()/2).attr('y',h-30).attr('text-anchor','middle').attr('class','chart-bar-label').text(d.name);svg.append('text').attr('x',x(i)+x.bandwidth()/2).attr('y',h-12).attr('text-anchor','middle').attr('class','chart-amount').text((d.value<0?'−':'')+fmt(Math.abs(d.value)));if(i>0&&i<items.length-1)svg.append('line').attr('x1',x(i-1)+x.bandwidth()).attr('x2',x(i)).attr('y1',y(top)).attr('y2',y(top)).attr('stroke',color('--border')).attr('stroke-dasharray','4 4')}})}}
          function treemap(){{chart.innerHTML='';const w=Math.max(300,chart.clientWidth||700),fullH=matchMedia('(max-width:560px)').matches?340:420,tiny=values.filter(v=>v.value>0&&v.value/Math.max(1,inflow)<.03),items=values.filter(v=>v.value>0&&v.value/Math.max(1,inflow)>=.03),h=fullH-(tiny.length?44:0),svg=d3.select(chart).append('svg').attr('width',w).attr('height',h),root=d3.hierarchy({{children:items}}).sum(d=>d.value).sort((a,b)=>b.value-a.value);d3.treemap().tile(d3.treemapSquarify.ratio(1.4)).size([w,h]).paddingInner(4).round(true)(root);root.leaves().forEach((leaf,i)=>{{const tw=leaf.x1-leaf.x0,th=leaf.y1-leaf.y0,g=svg.append('g').attr('class','treemap-tile').attr('transform',`translate(${{leaf.x0}},${{leaf.y0}}) scale(${{reduced?1:.96}})`).attr('opacity',reduced?1:0);g.append('rect').attr('width',Math.max(0,tw)).attr('height',Math.max(0,th)).attr('rx',8).attr('fill',color(leaf.data.color));g.append('title').text(leaf.data.name+' · '+fmt(leaf.data.value)+' ('+(leaf.data.value/Math.max(1,inflow)*100).toFixed(1)+'% of inflow)');if(tw>=90&&th>=56){{g.append('text').attr('x',10).attr('y',22).attr('class','treemap-label').text(leaf.data.name);g.append('text').attr('x',10).attr('y',42).attr('class','treemap-amount').text(fmt(leaf.data.value));if(th>=76)g.append('text').attr('x',10).attr('y',61).attr('class','treemap-percent').text((leaf.data.value/Math.max(1,inflow)*100).toFixed(1)+'% of inflow')}}else if(tw>=58&&th>=32)g.append('text').attr('x',10).attr('y',22).attr('class','treemap-amount').text(fmt(leaf.data.value));g.transition().delay(reduced?0:i*40).duration(reduced?0:360).attr('opacity',1).attr('transform',`translate(${{leaf.x0}},${{leaf.y0}}) scale(1)`)}});if(tiny.length){{const legend=d3.select(chart).append('div').attr('class','treemap-legend');tiny.forEach(d=>{{const chip=legend.append('span').attr('class','treemap-chip');chip.append('span').attr('class','treemap-swatch').style('background',color(d.color));chip.append('span').text(d.name+' · '+fmt(d.value)+' ('+(d.value/Math.max(1,inflow)*100).toFixed(1)+'%)')}})}}}}
          function bars(){{const [svg,w,h]=svgBase(),items=values.filter(v=>v.value>0).sort((a,b)=>b.value-a.value),labelW=Math.min(140,Math.max(96,w*.24)),amountW=Math.min(126,Math.max(92,w*.22)),trackX=labelW,trackW=Math.max(40,w-labelW-amountW-12),rowGap=16,totalH=items.length*24+(items.length-1)*rowGap,startY=Math.max(20,(h-totalH)/2);items.forEach((d,i)=>{{const y=startY+i*(24+rowGap),raw=trackW*(d.value/Math.max(1,inflow)),fillW=d.value>0?Math.max(4,Math.min(trackW,raw)):0;svg.append('text').attr('x',0).attr('y',y+16).attr('class','chart-bar-label').text(d.name);svg.append('rect').attr('class','bar-track').attr('x',trackX).attr('y',y).attr('width',trackW).attr('height',24).attr('rx',6).attr('fill',color('--brand-050'));svg.append('rect').attr('class','bar-fill').attr('x',trackX).attr('y',y).attr('height',24).attr('width',reduced?fillW:0).attr('rx',6).attr('fill',color(d.color)).transition().delay(reduced?0:i*60).duration(reduced?0:400).ease(d3.easeCubicOut).attr('width',fillW);svg.append('text').attr('x',w-4).attr('y',y+16).attr('text-anchor','end').attr('class','chart-amount').text(fmt(d.value))}})}}
          const draws={{sankey,waterfall,treemap,bars}};function select(view){{document.querySelectorAll('.chart-toggle button').forEach(b=>b.classList.toggle('active',b.dataset.view===view));chart.classList.add('switching');setTimeout(()=>{{draws[view]();requestAnimationFrame(()=>chart.classList.remove('switching'))}},reduced?0:100)}}document.querySelectorAll('.chart-toggle button').forEach(b=>b.onclick=()=>select(b.dataset.view));fetch('/api/sankey?month={urllib.parse.quote(month)}').then(r=>r.json()).then(d=>{{graph=d;select('sankey')}}).catch(()=>select('sankey'));window.addEventListener('resize',(()=>{{let t;return()=>{{clearTimeout(t);t=setTimeout(()=>select(document.querySelector('.chart-toggle .active').dataset.view),180)}}}})());
        }})();
        </script>'''
        self.send_html("Varavu.Selavu", body, beacon_month=month)

    def admin(self, method: str):
        with db() as conn:
            custom_ids = {row[0] for row in conn.execute("SELECT source_id FROM account_logos").fetchall()}
            use_seed = seed_data_enabled(conn)
            seed_status = "Seed data is visible on the dashboard." if use_seed else "Seed data is hidden from the dashboard."
        rows = []
        for source_id, source_name, _payer, _status in SOURCES:
            has_custom = source_id in custom_ids
            input_id = f"logo-input-{source_id}"
            rows.append(f'''
            <div class="logo-row" data-logo-row data-account-id="{html.escape(source_id)}" tabindex="0">
              <div class="logo-previews"><div>{render_bank_logo(source_name, False, False, source_id)}<small>Large</small></div><div>{render_bank_logo(source_name, True, False, source_id)}<small>Small</small></div></div>
              <strong class="logo-account">{html.escape(source_name)}</strong>
              <div class="logo-actions"><input id="{html.escape(input_id)}" class="logo-input" name="logo" type="file" accept=".svg,.png,.webp,.jpg,image/svg+xml,image/png,image/webp,image/jpeg" data-logo-input>
                <label class="btn secondary upload-logo" for="{html.escape(input_id)}"><i data-lucide="upload"></i>Upload logo</label>
                <button class="remove-logo {'visible' if has_custom else ''}" type="button" data-remove>Remove</button></div>
              <div class="remove-confirm" data-remove-confirm><span>Are you sure?</span><button type="button" data-remove-yes>Yes, remove</button><button class="secondary" type="button" data-remove-no>Cancel</button></div>
              <p class="logo-status" role="status" data-logo-status></p>
              <p class="logo-error" role="alert" data-logo-error></p>
            </div>''')
        body = rf'''
        <section class="admin-sections"><div class="card seed-data-card"><div class="setting-row"><div><h2>Use seed data</h2><p class="muted">Show the demo transactions while you explore the app.</p></div><label class="settings-switch"><input type="checkbox" data-seed-toggle {'checked' if use_seed else ''} aria-label="Use seed data"><span aria-hidden="true"></span></label></div><p class="setting-status" data-seed-status role="status">{html.escape(seed_status)}</p></div><div class="card bank-logos-card"><h2>Bank logos</h2><p class="muted">Upload a logo for each account. It appears everywhere the account is shown.</p><p class="caption">Tip: use the bank's square symbol, not the full wordmark — wordmarks blur at small sizes.</p>
          <div class="logo-rows">{''.join(rows)}</div></div></section>
        <style>
          .admin-sections{{display:grid;gap:var(--sp-5)}} .setting-row{{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-5)}} .setting-row h2{{margin:0 0 var(--sp-1)}} .setting-row p{{margin:0}} .setting-status{{min-height:18px;margin:var(--sp-2) 0 0;color:var(--dang-700);font-size:var(--text-caption)}} .settings-switch{{position:relative;display:inline-flex;flex:none;cursor:pointer}} .settings-switch input{{position:absolute;opacity:0;pointer-events:none}} .settings-switch span{{width:50px;height:30px;padding:3px;border-radius:var(--radius-full);background:var(--ink-400);transition:background 150ms ease;box-shadow:inset 0 0 0 1px rgba(20,35,30,.08)}} .settings-switch span::after{{content:"";display:block;width:24px;height:24px;border-radius:50%;background:var(--surface);box-shadow:0 1px 3px rgba(20,35,30,.28);transition:transform 180ms cubic-bezier(.16,1,.3,1)}} .settings-switch input:checked+span{{background:var(--brand-700)}} .settings-switch input:checked+span::after{{transform:translateX(20px)}} .settings-switch input:focus-visible+span{{outline:3px solid var(--focus);outline-offset:3px}} .settings-switch input:disabled+span{{opacity:.58;cursor:wait}} .bank-logos-card>h2{{margin-bottom:var(--sp-1)}} .bank-logos-card>.muted{{margin:0 0 var(--sp-1)}} .bank-logos-card>.caption{{margin:0}}
          .logo-rows{{margin-top:var(--sp-5);border-top:1px solid var(--border)}} .logo-row{{display:grid;grid-template-columns:150px minmax(180px,1fr) auto;align-items:center;gap:var(--sp-4);padding:var(--sp-4);margin:0 calc(-1*var(--sp-4));border-bottom:1px solid var(--border);border-radius:var(--radius-md);position:relative}}
          .logo-row.drag-over{{background:var(--brand-050);box-shadow:inset 0 0 0 2px var(--brand-600);border-bottom-color:transparent}} .logo-previews{{display:flex;align-items:end;gap:var(--sp-3)}} .logo-previews>div{{display:grid;justify-items:center;gap:var(--sp-1)}} .logo-previews small{{font-size:var(--text-caption);color:var(--ink-400)}} .logo-account{{font-size:var(--text-body-lg);font-weight:600}}
          .logo-actions{{display:flex;align-items:center;justify-content:flex-end;gap:var(--sp-2)}} .logo-input{{position:absolute;width:1px;height:1px;opacity:0;clip-path:inset(50%)}} .upload-logo{{cursor:pointer}} .upload-logo svg{{width:16px;height:16px}} .remove-logo{{display:none;background:transparent;color:var(--dang-700);padding:var(--sp-2)}} .remove-logo:hover{{background:var(--dang-100)}} .remove-logo.visible{{display:inline-flex}} .logo-row.uploading .upload-logo{{pointer-events:none;opacity:.72}} .logo-row.uploading .upload-logo::after{{content:"";width:14px;height:14px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:logo-spin 700ms linear infinite}}
          .remove-confirm{{display:none;grid-column:2/4;justify-content:flex-end;align-items:center;gap:var(--sp-2);color:var(--dang-700);font-size:var(--text-caption)}} .remove-confirm.open{{display:flex}} .remove-confirm button{{min-height:32px;padding:var(--sp-1) var(--sp-3);font-size:var(--text-caption)}} .logo-status,.logo-error{{grid-column:2/4;font-size:var(--text-caption);margin:0;min-height:18px}} .logo-status{{color:var(--pos-700)}} .logo-error{{color:var(--dang-700)}}
          .setting-status{{color:var(--ink-400)}}
          @keyframes logo-spin{{to{{transform:rotate(360deg)}}}}
          @media(max-width:767px){{.logo-row{{grid-template-columns:110px 1fr;gap:var(--sp-3)}}.logo-actions{{grid-column:1/3;justify-content:flex-start}}.remove-confirm,.logo-status,.logo-error{{grid-column:1/3;justify-content:flex-start}}}}
          @media(prefers-reduced-motion:reduce){{.logo-row.uploading .upload-logo::after{{animation:none}}}}
        </style>
        <script>
        (()=>{{
          const invalid='PNG, SVG, WebP or JPG under 1 MB';
          const seedToggle=document.querySelector('[data-seed-toggle]'),seedStatus=document.querySelector('[data-seed-status]');
          if(seedToggle)seedToggle.onchange=async()=>{{const enabled=seedToggle.checked;seedToggle.disabled=true;seedStatus.textContent='';try{{const res=await fetch('/admin/seed-data',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}},body:new URLSearchParams({{enabled:enabled?'1':'0'}}),credentials:'same-origin'}}),data=await res.json();if(!res.ok)throw Error(data.error||'Could not update seed data');seedStatus.textContent=data.status||'';window.showToast?.(data.toast||(enabled?'Seed data is visible on the dashboard.':'Seed data has been removed from the dashboard.'),'check-circle-2',1800)}}catch(error){{seedToggle.checked=!enabled;seedStatus.textContent=error.message;window.showToast?.('Could not update seed data.','alert-circle',1800)}}finally{{seedToggle.disabled=false}}}};
          function showToast(){{window.showToast?.('Logo saved — live across the app')}}
          function setStatus(row,message,kind='info'){{
            const status=row.querySelector('[data-logo-status]');
            const err=row.querySelector('[data-logo-error]');
            status.textContent=kind==='error'?'':message;
            err.textContent=kind==='error'?message:'';
          }}
          function setUploading(row,uploading){{
            row.classList.toggle('uploading',uploading);
            row.setAttribute('aria-busy',uploading?'true':'false');
            const label=row.querySelector('.upload-logo');
            label.setAttribute('aria-disabled',uploading?'true':'false');
          }}
          function setLogo(id,src,bundled){{
            document.querySelectorAll('[data-logo-container="'+id+'"]').forEach(box=>{{
              let img=box.querySelector('img');
              if(!src){{if(img)img.remove();return}}
              if(!img){{img=document.createElement('img');img.alt='Account logo';box.prepend(img)}}
              img.src=src;
              img.dataset.accountLogo=id;
              img.dataset.bundledSrc=bundled||'';
              img.onerror=function(){{
                if(this.dataset.bundledSrc){{this.src=this.dataset.bundledSrc;this.dataset.bundledSrc=''}}
                else this.remove();
              }};
            }});
          }}
          async function readJson(res){{
            const text=await res.text();
            try{{return text?JSON.parse(text):{{}}}}
            catch(e){{throw new Error(res.redirected?'Session expired. Login again and retry.':'Upload failed. Refresh and retry.')}}
          }}
          async function upload(row,file){{
            const validExt=/\.(svg|png|webp|jpg)$/i.test(file.name);
            setStatus(row,'');
            if(!validExt||file.size>1048576){{setStatus(row,invalid,'error');return}}
            const id=row.dataset.accountId;
            const local=URL.createObjectURL(file);
            setLogo(id,local,'');
            setUploading(row,true);
            setStatus(row,'Uploading logo...');
            const fd=new FormData();
            fd.append('source_id',id);
            fd.append('logo',file,file.name);
            try{{
              const res=await fetch('/admin/logo',{{method:'POST',body:fd,credentials:'same-origin'}});
              const data=await readJson(res);
              if(!res.ok)throw new Error(data.error||invalid);
              setLogo(id,data.url,data.bundled_url);
              row.querySelector('[data-remove]').classList.add('visible');
              setStatus(row,'Logo uploaded.');
              showToast();
            }}catch(e){{
              setStatus(row,e.message||'Upload failed. Refresh and retry.','error');
            }}finally{{
              setUploading(row,false);
              row.querySelector('[data-logo-input]').value='';
              setTimeout(()=>URL.revokeObjectURL(local),1000);
            }}
          }}
          document.querySelectorAll('[data-logo-row]').forEach(row=>{{
            const input=row.querySelector('[data-logo-input]'),remove=row.querySelector('[data-remove]'),confirm=row.querySelector('[data-remove-confirm]');
            input.onchange=()=>input.files[0]&&upload(row,input.files[0]);
            ['dragenter','dragover'].forEach(type=>row.addEventListener(type,e=>{{e.preventDefault();row.classList.add('drag-over')}}));
            ['dragleave','drop'].forEach(type=>row.addEventListener(type,e=>{{e.preventDefault();row.classList.remove('drag-over')}}));
            row.addEventListener('drop',e=>e.dataTransfer.files[0]&&upload(row,e.dataTransfer.files[0]));
            remove.onclick=()=>confirm.classList.add('open');
            row.querySelector('[data-remove-no]').onclick=()=>confirm.classList.remove('open');
            row.querySelector('[data-remove-yes]').onclick=async()=>{{
              setStatus(row,'Removing logo...');
              const res=await fetch('/admin/logo',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:new URLSearchParams({{source_id:row.dataset.accountId,action:'remove'}}),credentials:'same-origin'}});
              const data=await readJson(res);
              if(res.ok){{
                setLogo(row.dataset.accountId,data.bundled_url,'');
                remove.classList.remove('visible');
                confirm.classList.remove('open');
                setStatus(row,'Logo removed.');
              }}else setStatus(row,data.error||invalid,'error');
            }};
          }});
        }})();
        </script>'''
        self.send_html("Admin", body)

    def admin_seed_data(self, method: str):
        if method != "POST":
            return self.not_found()
        data = self.form()
        if data.get("enabled") not in ("0", "1"):
            return self.json_response({"error": "Choose whether to use seed data"}, 400)
        enabled = data["enabled"] == "1"
        with db() as conn:
            set_seed_data_enabled(conn, enabled)
        status = "Seed data is visible on the dashboard." if enabled else "Seed data is hidden from the dashboard."
        toast = "Seed data is visible on the dashboard." if enabled else "Seed data has been removed from the dashboard."
        return self.json_response({"ok": True, "enabled": enabled, "status": status, "toast": toast})

    def admin_logo(self, method: str):
        if method != "POST":
            return self.not_found()
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            data = self.form()
            if data.get("action") != "remove":
                return self.json_response({"error": "Invalid action"}, 400)
            source_id = data.get("source_id", "")
            with db() as conn:
                row = conn.execute("SELECT file_name FROM account_logos WHERE source_id=?", (source_id,)).fetchone()
                conn.execute("DELETE FROM account_logos WHERE source_id=?", (source_id,))
                audit(conn, "dashboard", "remove_account_logo", "source", source_id)
            if row:
                try:
                    (LOGO_UPLOAD_DIR / row["file_name"]).unlink()
                except FileNotFoundError:
                    pass
            name = next((r[1] for r in SOURCES if r[0] == source_id), "")
            asset = bank_asset(name)[0]
            return self.json_response({"ok": True, "bundled_url": f"/assets/banks/{asset}" if asset else ""})
        if int(self.headers.get("Content-Length", 0)) > 1_150_000:
            self.read_body()
            return self.json_response({"error": "PNG, SVG, WebP or JPG under 1 MB"}, 413)
        fields, files = parse_multipart(self.headers, self.read_body())
        source_id, upload = fields.get("source_id", ""), files.get("logo")
        source = next((r for r in SOURCES if r[0] == source_id), None)
        if not source or not upload:
            return self.json_response({"error": "Unknown account or missing file"}, 400)
        filename, payload = upload
        ext = Path(filename).suffix.lower()
        types = {".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg"}
        valid_magic = {".png": payload.startswith(b"\x89PNG\r\n\x1a\n"), ".jpg": payload.startswith(b"\xff\xd8\xff"), ".webp": payload.startswith(b"RIFF") and payload[8:12] == b"WEBP", ".svg": b"<svg" in payload[:4096].lower()}
        if ext not in types or len(payload) > 1_048_576 or not payload or not valid_magic.get(ext, False):
            return self.json_response({"error": "PNG, SVG, WebP or JPG under 1 MB"}, 400)
        if ext == ".svg" and any(token in payload.lower() for token in (b"<script", b"onload=", b"javascript:")):
            return self.json_response({"error": "SVG contains unsupported active content"}, 400)
        stored = f"{source_id}{ext}"
        target = LOGO_UPLOAD_DIR / stored
        target.write_bytes(payload)
        with db() as conn:
            old = conn.execute("SELECT file_name FROM account_logos WHERE source_id=?", (source_id,)).fetchone()
            conn.execute("INSERT INTO account_logos(source_id,file_name,content_type,updated_at) VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET file_name=excluded.file_name,content_type=excluded.content_type,updated_at=excluded.updated_at", (source_id, stored, types[ext], now_iso()))
            audit(conn, "dashboard", "save_account_logo", "source", source_id, after={"file_name": stored})
        if old and old["file_name"] != stored:
            try:
                (LOGO_UPLOAD_DIR / old["file_name"]).unlink()
            except FileNotFoundError:
                pass
        bundled = bank_asset(source[1])[0]
        return self.json_response({"ok": True, "url": f"/assets/uploads/bank-logos/{stored}?v={int(time.time())}", "bundled_url": f"/assets/banks/{bundled}" if bundled else ""})

    def transactions(self, method: str):
        with db() as conn:
            rows = effective_transactions(conn)
        body_rows = "".join(
            f"<tr><td>{html.escape(human_date(r['transaction_date']))}</td><td>{html.escape(r['description'])}</td>"
            f"<td class='right amount'>{money(r['amount'])}</td><td>{html.escape(human_label(r['flow_type']))}</td><td>{html.escape(r['category'] or 'Uncategorised')}</td>"
            f"<td>{html.escape(human_label(r['classification']))}</td><td>{html.escape(r['payer'] or '')}</td><td>{html.escape(r['source_name'] or '')}</td></tr>"
            for r in rows
        )
        content = f"<div class='table-scroll'><table><thead><tr><th>Date</th><th>Description</th><th class='right'>Amount</th><th>Flow</th><th>Category</th><th>Classification</th><th>Payer</th><th>Source</th></tr></thead><tbody>{body_rows}</tbody></table></div>" if rows else empty_state("receipt-text", "No transactions yet. Import a statement to get started.", "Import statement", "/import")
        self.send_html("Transactions", f"<div class='card'><h2>All transactions</h2>{content}</div>")

    def review(self, method: str):
        if method == "POST":
            data = self.form()
            with db() as conn:
                txid, action = data["transaction_id"], data.get("action", "approve")
                found = conn.execute("SELECT * FROM transactions WHERE transaction_id=?", (txid,)).fetchone()
                if not found:
                    return self.json_response({"error": "Transaction not found"}, 404)
                before = dict(found)
                category = data.get("category") or None
                if action == "approve" and not category:
                    return self.json_response({"error": "Choose a category before approving"}, 400)
                flow_type = before.get("flow_type") or "spend"
                classification = before.get("classification") or "controllable"
                if action == "transfer":
                    flow_type, classification, category = "transfer", "excluded", None
                elif action == "exclude":
                    flow_type, classification, category = "unknown", "excluded", None
                oid = "override_" + stable_hash(txid, time.time())
                conn.execute(
                    """
                    INSERT INTO manual_overrides(manual_override_id, transaction_id, category, subcategory, classification, flow_type, merchant_payee, notes, created_at, created_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (oid, txid, category, data.get("subcategory") or before.get("subcategory"), classification, flow_type, before.get("merchant_payee") or before["description"], data.get("notes") or None, now_iso(), "dashboard"),
                )
                conn.execute("UPDATE transactions SET manual_override_id=? WHERE transaction_id=?", (oid, txid))
                conn.execute("UPDATE review_items SET status='resolved', resolved_at=? WHERE transaction_id=?", (now_iso(), txid))
                audit(conn, "dashboard", f"review_{action}", "transaction", txid, before=before, after=data)
                rule_id = None
                if data.get("remember") == "yes" and action == "approve":
                    pattern = before.get("merchant_payee") or before["description"]
                    rule_id = "rule_" + stable_hash("remember", pattern, category)
                    conn.execute(
                        """
                        INSERT INTO rules(rule_id, name, match_type, pattern, source_name, category, subcategory, classification, flow_type, merchant_payee, confidence, notes, created_at, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(rule_id) DO UPDATE SET category=excluded.category, subcategory=excluded.subcategory, classification=excluded.classification,
                          flow_type=excluded.flow_type, notes=excluded.notes, enabled=1, updated_at=excluded.updated_at
                        """,
                        (rule_id, f"Remember {pattern[:48]}", "exact_merchant", pattern, before.get("source_name"), category, data.get("subcategory") or before.get("subcategory"), classification, flow_type, before.get("merchant_payee"), 0.94, data.get("notes") or None, now_iso(), now_iso()),
                    )
                    audit(conn, "dashboard", "create_rule_from_review", "rule", rule_id, after=data)
                remaining = conn.execute("""SELECT COUNT(DISTINCT ri.transaction_id) FROM review_items ri JOIN transactions t ON t.transaction_id=ri.transaction_id LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id WHERE ri.status='open' AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))""").fetchone()[0]
            if self.headers.get("Accept") == "application/json" or self.headers.get("X-Requested-With") == "fetch":
                return self.json_response({"ok": True, "remaining": remaining, "action": action, "rule_id": rule_id})
            return self.redirect("/review")
        with db() as conn:
            include_seed = seed_data_enabled(conn)
            seed_filter = "" if include_seed else " AND coalesce(t.import_batch_id, '') != ?"
            seed_args: list[object] = [] if include_seed else [SEED_BATCH_ID]
            rows = conn.execute(
                f"""
                SELECT group_concat(ri.reason, ',') AS reason, t.*
                FROM review_items ri JOIN transactions t ON t.transaction_id=ri.transaction_id LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id
                WHERE ri.status='open'
                AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))
                {seed_filter}
                GROUP BY t.transaction_id
                ORDER BY max(ri.created_at) DESC
                """,
                seed_args,
            ).fetchall()
            visible_transactions = effective_transactions(conn, include_seed=include_seed)
            categories = sorted({
                *PARENT_CATEGORIES,
                *[r["category"] for r in visible_transactions if r.get("category")],
                *[r["subcategory"] for r in visible_transactions if r.get("subcategory")],
            })
        items = []
        for r in rows:
            raw = {}
            if r["raw_import_id"]:
                with db() as conn:
                    raw_row = conn.execute("SELECT raw_json FROM raw_imports WHERE raw_import_id=?", (r["raw_import_id"],)).fetchone()
                if raw_row:
                    try: raw = json.loads(raw_row["raw_json"])
                    except json.JSONDecodeError: pass
            time_value = next((str(v) for k, v in raw.items() if k.lower() in ("time", "transaction time", "txn time") and str(v).strip()), "")
            known = {"date", "transaction date", "txn date", "value date", "time", "transaction time", "txn time", "description", "narration", "details", "merchant", "particulars", "amount", "transaction amount", "amt", "debit", "credit", "withdrawal", "deposit"}
            extra = [{"label": str(k).replace("_", " ").title(), "value": str(v)} for k, v in raw.items() if str(k).lower() not in known and str(v).strip()]
            items.append({"id": r["transaction_id"], "date": human_date(r["transaction_date"]), "description": r["description"], "amount": money(r["amount"]), "direction": "Money out" if r["amount"] < 0 else "Money in", "source": r["source_name"] or "Unknown source", "source_id": source_id_for(r["source_name"]), "reasons": reason_labels(r["reason"]), "reason_chips": render_reason_chips(r["reason"]), "guess": r["category"] or "", "subcategory": r["subcategory"] or "", "note": r["notes"] or "", "time": time_value, "extra": extra})
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        body = render_review_workbench(items, categories, query.get("open", [""])[0])
        self.send_html("Review Queue", body)

    def rules(self, method: str):
        if method == "POST":
            data = self.form()
            with db() as conn:
                if data.get("action") == "toggle":
                    conn.execute("UPDATE rules SET enabled=1-enabled, updated_at=? WHERE rule_id=?", (now_iso(), data["rule_id"]))
                    audit(conn, "dashboard", "toggle_rule", "rule", data["rule_id"])
                else:
                    rid = data.get("rule_id") or "rule_" + stable_hash(data.get("name"), time.time())
                    conn.execute(
                        """
                        INSERT INTO rules(rule_id,name,match_type,pattern,source_name,category,subcategory,classification,flow_type,merchant_payee,confidence,enabled,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(rule_id) DO UPDATE SET name=excluded.name, match_type=excluded.match_type, pattern=excluded.pattern, category=excluded.category,
                        subcategory=excluded.subcategory, classification=excluded.classification, flow_type=excluded.flow_type, confidence=excluded.confidence, updated_at=excluded.updated_at
                        """,
                        (rid, data["name"], data["match_type"], data["pattern"], data.get("source_name") or None, data.get("category") or None, data.get("subcategory") or None, data.get("classification") or None, data.get("flow_type") or None, data.get("merchant_payee") or None, float(data.get("confidence") or 0.8), 1, now_iso(), now_iso()),
                    )
                    audit(conn, "dashboard", "upsert_rule", "rule", rid, after=data)
            return self.redirect("/rules?toast=" + urllib.parse.quote("Rule saved" if data.get("action") != "toggle" else "Rule updated"))
        with db() as conn:
            rows = conn.execute("SELECT * FROM rules ORDER BY enabled DESC, confidence DESC, name").fetchall()
        table = "".join(
            f"<tr><td>{html.escape(r['name'])}</td><td>{html.escape(human_label(r['match_type']))}</td><td>{html.escape(r['pattern'])}</td><td>{html.escape(r['category'] or 'Any category')}</td><td>{html.escape(human_label(r['classification']))}</td><td class='amount'>{r['confidence']:.0%}</td><td>{'Enabled' if r['enabled'] else 'Paused'}</td><td><form method='post'><input type='hidden' name='action' value='toggle'><input type='hidden' name='rule_id' value='{r['rule_id']}'><button class='secondary'>{'Pause' if r['enabled'] else 'Enable'}</button></form></td></tr>"
            for r in rows
        )
        form = f"""
        <div class="card"><h2>Add rule</h2><form method="post" class="form-grid">
          <div><label>Name</label><input name="name" required></div><div><label>Match type</label><select name="match_type"><option value="description_contains">Description contains</option><option value="exact_merchant">Exact merchant</option><option value="normalized_merchant">Similar merchant</option></select></div><div><label>Pattern</label><input name="pattern" required></div>
          <div><label>Category</label>{select('category', ['']+PARENT_CATEGORIES, '')}</div><div><label>Subcategory</label><input name="subcategory"></div><div><label>Classification</label>{select('classification', ['']+CLASSIFICATIONS, '')}</div>
          <div><label>Flow type</label>{select('flow_type', ['']+FLOW_TYPES, '')}</div><div><label>Confidence</label><input name="confidence" value="0.86"></div><div class="align-end"><button>Save rule</button></div>
        </form></div>
        """
        listing = f"<div class='table-scroll'><table><thead><tr><th>Name</th><th>Match</th><th>Pattern</th><th>Category</th><th>Classification</th><th>Confidence</th><th>Status</th><th></th></tr></thead><tbody>{table}</tbody></table></div>" if rows else empty_state("sliders-horizontal", "No rules yet. Add one above to automate categorisation.")
        self.send_html("Rules", form + f"<div class='card section-gap'><h2>Rule manager</h2>{listing}</div>")

    def baselines(self, method: str):
        if method == "POST":
            data = self.form()
            with db() as conn:
                bid = data.get("baseline_id") or "baseline_" + stable_hash(data.get("category"), data.get("subcategory"), data.get("effective_month"), time.time())
                conn.execute(
                    """
                    INSERT INTO baselines(baseline_id, scope, category, subcategory, amount, effective_month, updated_source, active, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (bid, "subcategory" if data.get("subcategory") else "parent_category", data["category"], data.get("subcategory") or None, float(data["amount"]), data["effective_month"], "dashboard", 1, now_iso(), now_iso()),
                )
                audit(conn, "dashboard", "create_baseline", "baseline", bid, after=data)
            return self.redirect("/baselines?toast=" + urllib.parse.quote("Baseline saved"))
        with db() as conn:
            rows = conn.execute("SELECT * FROM baselines WHERE active=1 ORDER BY category, subcategory").fetchall()
        table = "".join(f"<tr><td>{html.escape(r['category'])}</td><td>{html.escape(r['subcategory'] or 'All spending')}</td><td class='right amount'>{money(r['amount'])}</td><td>{html.escape(human_month(r['effective_month']))}</td><td>{html.escape(human_label(r['updated_source']))}</td></tr>" for r in rows)
        body = f"""
        <div class="card"><h2>Add baseline or cap</h2><form method="post" class="form-grid">
          <div><label>Category</label>{select('category', PARENT_CATEGORIES, 'Food')}</div><div><label>Subcategory</label><input name="subcategory"></div><div><label>Amount</label><input name="amount" value="12000" required></div>
          <div><label>Effective month</label>{month_control('effective_month', date.today().strftime('%Y-%m'))}</div><div class="align-end"><button>Save baseline</button></div>
        </form></div>
        <div class="card section-gap"><h2>Active baselines</h2>{f'<div class="table-scroll"><table><thead><tr><th>Category</th><th>Subcategory</th><th class="right">Amount</th><th>Effective</th><th>Source</th></tr></thead><tbody>{table}</tbody></table></div>' if rows else empty_state('ruler', 'No baselines yet. Add one above to start planning.')}</div>
        """
        self.send_html("Baselines", body)

    def import_page(self, method: str):
        result = ""
        seed_removed = False
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        requested_source = (query.get("source") or [""])[0]
        requested_month = (query.get("month") or ["2026-05"])[0]
        undo_batch = (query.get("undo_batch") or [""])[0]
        if method == "POST":
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                data = self.form()
                action, batch_id = data.get("action", ""), data.get("batch_id", "")
                if action == "process_pending":
                    start_import_worker()
                    return self.redirect("/import?toast=" + urllib.parse.quote("Extraction worker started"))
                with db() as conn:
                    batch = conn.execute("SELECT * FROM import_batches WHERE import_batch_id=? AND deleted_at IS NULL", (batch_id,)).fetchone()
                    if not batch or batch_id == SEED_BATCH_ID:
                        return self.redirect("/import?toast=" + urllib.parse.quote("Import could not be changed"))
                    if action == "exclude":
                        conn.execute("UPDATE import_batches SET excluded_at=? WHERE import_batch_id=?", (now_iso(), batch_id))
                        audit(conn, "dashboard", "exclude_import_batch", "import_batch", batch_id)
                        message = "Import excluded."
                    elif action == "include":
                        if batch["status"] not in ("committed", "completed", "imported"):
                            return self.redirect("/import?toast=" + urllib.parse.quote("This import must be successfully processed before it can be included."))
                        conn.execute("UPDATE import_batches SET excluded_at=NULL WHERE import_batch_id=?", (batch_id,))
                        audit(conn, "dashboard", "include_import_batch", "import_batch", batch_id)
                        message = "Import included."
                    elif action == "delete":
                        conn.execute("UPDATE import_batches SET deleted_at=?, excluded_at=coalesce(excluded_at,?) WHERE import_batch_id=?", (now_iso(), now_iso(), batch_id))
                        audit(conn, "dashboard", "archive_import_batch", "import_batch", batch_id)
                        message = "Import archived"
                    elif action == "retry":
                        if batch["status"] not in ("needs_parser", "failed", "unable_to_parse"):
                            return self.redirect("/import?toast=" + urllib.parse.quote("Retry is only available when processing fails."))
                        conn.execute("UPDATE import_batches SET status='pending_pdf_extraction', notes=? WHERE import_batch_id=?", ("Waiting for extraction retry. Passwords are never stored.", batch_id))
                        audit(conn, "dashboard", "retry_import_batch", "import_batch", batch_id)
                        message = "Extraction retry started"
                    else:
                        return self.not_found()
                if action == "retry":
                    start_import_worker(batch_id)
                undo = "&undo_batch=" + urllib.parse.quote(batch_id) if action == "exclude" else ""
                return self.redirect("/import?toast=" + urllib.parse.quote(message) + undo)
            if "multipart/form-data" in content_type:
                fields, files = parse_multipart(self.headers, self.read_body())
                source_name = fields.get("source_name") or "Unknown"
                statement_month = fields.get("statement_month") or date.today().strftime("%Y-%m")
                statement_start_date = parse_date(fields.get("statement_start_date") or "")
                statement_end_date = parse_date(fields.get("statement_end_date") or "")
                if not fields.get("statement_start_date") or not fields.get("statement_end_date"):
                    statement_start_date, statement_end_date = default_statement_period(source_name, statement_month)
                upload = files.get("statement")
                if upload:
                    filename, file_bytes = upload
                    suffix = Path(filename).suffix.lower()
                    with db() as conn:
                        if suffix == ".csv":
                            info = import_csv(conn, file_bytes, filename, source_name, statement_month, statement_start_date, statement_end_date)
                            seed_removed = info.get("seed_removed", False)
                            result = f"<div class='notice'>Imported <span class='amount'>{info['added']}</span> rows. Duplicates skipped: <span class='amount'>{info['duplicates']}</span>.</div>"
                        elif suffix == ".json":
                            info = import_json(conn, file_bytes, filename, source_name, statement_month, statement_start_date, statement_end_date)
                            seed_removed = info.get("seed_removed", False)
                            result = f"<div class='notice'>Imported <span class='amount'>{info['added']}</span> rows. Duplicates skipped: <span class='amount'>{info['duplicates']}</span>.</div>"
                        else:
                            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                            safe_name = f"{int(time.time())}_{slug(filename) or 'statement'}{suffix}"
                            (UPLOAD_DIR / safe_name).write_bytes(file_bytes)
                            batch_id = "batch_" + stable_hash(filename, time.time())
                            conn.execute(
                                """
                                INSERT INTO import_batches(import_batch_id, created_at, source_name, statement_month, statement_start_date, statement_end_date, file_name, file_type, status, notes)
                                VALUES(?,?,?,?,?,?,?,?,?,?)
                                """,
                                (batch_id, now_iso(), source_name, statement_month, statement_start_date, statement_end_date, safe_name, suffix.lstrip(".") or "file", "pending_pdf_extraction", "PDF/file stored for statement parser. Password was not stored."),
                            )
                            audit(conn, "dashboard", "upload_pdf_pending_extraction", "import_batch", batch_id, after={"file": safe_name, "source": source_name, "statement_start_date": statement_start_date, "statement_end_date": statement_end_date})
                            seed_removed = disable_seed_data_after_statement(conn, "dashboard")
                            conn.commit()
                            start_import_worker(batch_id, fields.get("pdf_password") or None)
                            result = "<div class='notice'>Upload complete. Extraction started automatically; the password was not stored.</div>"
        options = "".join(f'<option value="{html.escape(s[1])}" {"selected" if s[1] == requested_source else ""}>{html.escape(s[1])}</option>' for s in SOURCES)
        default_start, default_end = month_start_end(requested_month)
        with db() as conn:
            include_seed = seed_data_enabled(conn)
            batches = conn.execute(
                """
                SELECT import_batch_id, created_at, source_name, statement_month, statement_start_date, statement_end_date,
                       file_name, file_type, status, row_count, duplicate_count, notes, excluded_at
                FROM import_batches
                WHERE deleted_at IS NULL AND (? = 1 OR import_batch_id != ?)
                ORDER BY created_at DESC
                LIMIT 12
                """,
                (1 if include_seed else 0, SEED_BATCH_ID),
            ).fetchall()
        successful_statuses = {"committed", "completed", "imported"}
        retryable_statuses = {"needs_parser", "failed", "unable_to_parse"}
        def display_filename(r):
            return re.sub(r"^\d+_", "", r["file_name"] or "Statement")
        def included_control(r):
            successful = r["status"] in successful_statuses
            included = successful and not r["excluded_at"]
            disabled = not successful or r["import_batch_id"] == SEED_BATCH_ID
            title = "This import must be successfully processed before it can be included." if not successful else ("Demo data inclusion is managed in Settings." if disabled else "")
            action = "exclude" if included else "include"
            return (f"<form method='post' class='included-form'><input type='hidden' name='batch_id' value='{html.escape(r['import_batch_id'])}'>"
                    f"<input type='hidden' name='action' value='{action}'><span class='included-label'>{'Included' if included else 'Excluded'}</span>"
                    f"<label class='import-switch' title='{html.escape(title)}'><span class='sr-only'>Set import {'excluded' if included else 'included'}</span>"
                    f"<input type='checkbox' {'checked' if included else ''} {'disabled' if disabled else ''} onchange='this.form.submit()'><span aria-hidden='true'></span></label></form>")
        def import_actions(r):
            retryable = r["status"] in retryable_statuses and r["import_batch_id"] != SEED_BATCH_ID
            retry_title = "Retry extraction" if retryable else "Retry is only available when processing fails."
            retry = (f"<form method='post' class='retry-form'><input type='hidden' name='batch_id' value='{html.escape(r['import_batch_id'])}'>"
                     f"<button class='secondary retry-button' name='action' value='retry' title='{retry_title}' {'disabled' if not retryable else ''}><i data-lucide='rotate-cw'></i><span>Retry</span></button></form>")
            filename = html.escape(display_filename(r), quote=True)
            account = html.escape(r["source_name"] or "Unknown account", quote=True)
            confirmation = html.escape(json.dumps(f"Delete import?\n\nArchive {display_filename(r)} from {r['source_name'] or 'this account'}?\n\nThis cannot be undone in the app. The uploaded file remains stored."), quote=True)
            delete = (f"<form method='post' onsubmit=\"return confirm({confirmation})\"><input type='hidden' name='batch_id' value='{html.escape(r['import_batch_id'])}'>"
                      f"<button class='delete-import' name='action' value='delete' title='Delete import' aria-label='Delete import: {filename} from {account}'><i data-lucide='trash-2'></i></button></form>")
            return f"<div class='import-actions'>{retry}{delete}</div>"
        def import_row(r):
            note = f'<small class="status-note">{html.escape(r["notes"] or "")}</small>' if r["notes"] else ""
            filename = display_filename(r)
            source = r["source_name"] or "Unknown account"
            source_meta = "Credit card" if "card" in source.lower() or "diners" in source.lower() else ("Bank account" if "bank" in source.lower() else "")
            account_meta = f'<small class="account-meta">{html.escape(source_meta)}</small>' if source_meta else ""
            rows_value = str(r["row_count"] if r["row_count"] is not None else 0) if r["status"] in successful_statuses else "—"
            return (
            f"<tr data-import-status='{html.escape(r['status'])}'><td class='file-cell'><i data-lucide='file-text'></i><span title='{html.escape(filename, quote=True)}'>{html.escape(filename)}</span></td>"
            f"<td class='account-cell'><strong>{html.escape(source)}</strong>{account_meta}</td>"
            f"<td class='status-cell'><span class='pill status-badge'>{html.escape(human_label(r['status']))}</span>{note}</td>"
            f"<td class='right amount rows-cell'>{rows_value}</td><td class='imported-cell'>{html.escape(human_datetime(r['created_at']))}</td>"
            f"<td>{included_control(r)}</td><td>{import_actions(r)}</td></tr>"
            )
        batch_rows = "".join(import_row(r) for r in batches)
        body = f"""
        {f'<span hidden data-toast-on-load="Seed data has been removed from the dashboard." data-toast-ms="1800"></span>' if seed_removed else ''}
        {f'<div class="notice undo-notice">Import excluded. <form method="post"><input type="hidden" name="batch_id" value="{html.escape(undo_batch)}"><button class="secondary" name="action" value="include">Undo</button></form></div>' if undo_batch else ''}
        {result}
        <div class="card import-card"><h2>Import statement</h2>
          <p class="muted">CSV and JSON statements import immediately. PDFs are stored securely for extraction; passwords are never stored.</p>
          <form method="post" enctype="multipart/form-data" class="form-grid">
            <div><label>Account</label><select name="source_name">{options}</select></div>
            <div><label>Statement file</label><input type="file" name="statement" required></div>
            <div><label>Statement month</label>{month_control('statement_month', requested_month)}</div>
            <div><label>Statement start date</label>{date_control('statement_start_date', default_start)}</div>
            <div><label>Statement end date</label>{date_control('statement_end_date', default_end)}</div>
            <div><label>PDF password</label><input type="password" name="pdf_password" placeholder="Used only during import; not stored"></div>
            <div class="align-end"><button>Import statement</button></div>
          </form>
        </div>
        <div class="card section-gap"><div class="import-head"><div><h2>Import history</h2><p class="muted">Manage imported statements and retry extraction when needed.</p><p class="muted import-explainer">Excluded imports remain stored but aren't included in calculations, transactions, or review.</p></div><form method="post"><button class="secondary" name="action" value="process_pending">Process pending imports</button></form></div>
          {f'<div class="table-scroll"><table class="keep-table import-history"><colgroup><col class="col-file"><col class="col-account"><col class="col-status"><col class="col-rows"><col class="col-imported"><col class="col-included"><col class="col-actions"></colgroup><thead><tr><th>File</th><th>Account</th><th>Status</th><th class="right">Rows</th><th>Imported</th><th>Included</th><th>Actions</th></tr></thead><tbody>{batch_rows}</tbody></table></div>' if batches else empty_state('upload', 'No statements imported yet. Use the form above to add one.')}
        </div>
        <style>.import-card{{padding-top:var(--sp-4);padding-bottom:var(--sp-4)}}.import-card h2{{margin-bottom:var(--sp-2)}}.import-card .form-grid{{margin-top:var(--sp-3)}}.import-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:var(--sp-3);margin-bottom:var(--sp-2)}}.import-head h2,.import-head p{{margin:0 0 var(--sp-1)}}.import-explainer{{font-size:var(--text-caption)}}.import-history{{min-width:1080px;table-layout:fixed}}.col-file{{width:24%}}.col-account{{width:17%}}.col-status{{width:19%}}.col-rows{{width:6%}}.col-imported{{width:15%}}.col-included{{width:10%}}.col-actions{{width:9%}}.import-history td{{vertical-align:middle;padding-top:var(--sp-2);padding-bottom:var(--sp-2)}}.file-cell{{display:flex;align-items:center;gap:var(--sp-2);min-width:0}}.file-cell svg{{width:18px;flex:none;color:var(--ink-400)}}.file-cell span{{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.account-cell strong{{display:block;font-weight:600}}.account-meta,.status-note{{display:block;margin-top:3px;color:var(--ink-400);font-size:var(--text-caption);font-weight:400;line-height:1.3}}.status-note{{max-width:260px}}.status-badge{{white-space:nowrap}}.rows-cell,.imported-cell{{white-space:nowrap}}.included-form,.import-actions{{display:flex;align-items:center;gap:var(--sp-2);margin:0;white-space:nowrap}}.included-label{{min-width:57px;font-size:var(--text-caption);font-weight:600}}.import-switch{{position:relative;display:block;cursor:pointer}}.import-switch input{{position:absolute;opacity:0}}.import-switch>span:not(.sr-only){{display:block;width:36px;height:20px;padding:2px;border-radius:var(--radius-full);background:var(--ink-400);transition:background 150ms}}.import-switch>span:not(.sr-only):after{{content:'';display:block;width:16px;height:16px;border-radius:50%;background:var(--surface);transition:transform 150ms}}.import-switch input:checked+span{{background:var(--brand-700)}}.import-switch input:checked+span:after{{transform:translateX(16px)}}.import-switch input:disabled+span{{background:var(--border);box-shadow:inset 0 0 0 1px var(--ink-400)}}.import-switch:has(input:disabled){{cursor:not-allowed;opacity:.72}}.import-switch input:focus-visible+span{{outline:2px solid rgba(18,138,99,.4);outline-offset:2px}}.sr-only{{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}.import-actions form{{margin:0}}.import-actions button{{min-height:32px;font-size:var(--text-caption)}}.retry-button{{display:inline-flex;align-items:center;gap:var(--sp-1);padding:var(--sp-1) var(--sp-2)}}.retry-button svg{{width:15px}}.retry-button:disabled{{opacity:.42;cursor:not-allowed;background:var(--page-bg);color:var(--ink-400);border-color:var(--border)}}.delete-import{{width:32px;min-height:32px;padding:0;display:grid;place-items:center;background:transparent;color:var(--ink-400)}}.delete-import svg{{width:17px}}.delete-import:hover,.delete-import:focus-visible{{background:var(--dang-100);color:var(--dang-700)}}@media(max-width:767px){{.import-head{{flex-direction:column}}.keep-table{{min-width:1080px}}.table-scroll{{overflow-x:auto}}}}</style>
        <script>(()=>{{document.querySelectorAll('.retry-form').forEach(form=>form.addEventListener('submit',()=>{{const button=form.querySelector('button');if(button.disabled)return;button.disabled=true;button.querySelector('span').textContent='Retrying…';const row=form.closest('tr'),badge=row.querySelector('.status-badge');badge.textContent='Processing';row.querySelector('.status-note')?.remove()}}))}})();</script>
        """
        self.send_html("Import", body)

    def api(self, path: str):
        if not self.is_authed():
            self.send_response(401)
            self.end_headers()
            return
        with db() as conn:
            if path == "/api/sankey":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                month = dashboard_month(conn, (query.get("month") or [None])[0])
                data = dashboard_data(conn, month)
                s = data["summary"]
                nodes = [{"name": n} for n in ["Total inflow", "Investments", "Fixed expenses", "Other expenses", "Surplus / unallocated"]]
                links = []
                if s["total_inflow"] > 0:
                    for target, value in (("Investments", s["investments"]), ("Fixed expenses", s["fixed"]), ("Other expenses", s["other"])):
                        if value > 0:
                            links.append({"source": "Total inflow", "target": target, "value": value})
                    surplus = max(0, s["total_inflow"] - s["investments"] - s["fixed"] - s["other"])
                    if surplus:
                        links.append({"source": "Total inflow", "target": "Surplus / unallocated", "value": surplus})
                else:
                    for target, value in (("Investments", s["investments"]), ("Fixed expenses", s["fixed"]), ("Other expenses", s["other"])):
                        if value > 0:
                            links.append({"source": "Total inflow", "target": target, "value": value})
                    if not links:
                        links.append({"source": "Total inflow", "target": "Other expenses", "value": 1})
                return self.json({"nodes": nodes, "links": links})
        self.not_found()

    def json(self, payload: dict):
        return self.json_response(payload, 200)

    def json_response(self, payload: dict, status: int = 200):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def not_found(self):
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not found")


def import_json(conn: sqlite3.Connection, file_bytes: bytes, file_name: str, source_name: str, statement_month: str, statement_start_date: str | None = None, statement_end_date: str | None = None) -> dict:
    payload = json.loads(file_bytes.decode("utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("transactions", [])
    batch_id = "batch_" + stable_hash(file_name, time.time())
    if not statement_start_date or not statement_end_date:
        statement_start_date, statement_end_date = default_statement_period(source_name, statement_month)
    conn.execute(
        "INSERT INTO import_batches(import_batch_id, created_at, source_name, statement_month, statement_start_date, statement_end_date, file_name, file_type, status, row_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (batch_id, now_iso(), source_name, statement_month, statement_start_date, statement_end_date, file_name, "json", "processing", len(rows)),
    )
    added = duplicates = 0
    for idx, row in enumerate(rows, start=1):
        raw_id = "raw_" + stable_hash(batch_id, idx, json.dumps(row, sort_keys=True))
        conn.execute("INSERT INTO raw_imports(raw_import_id, import_batch_id, row_number, raw_json, created_at) VALUES(?,?,?,?,?)", (raw_id, batch_id, idx, json.dumps(row), now_iso()))
        tx = {
            "transaction_date": parse_date(row.get("transaction_date") or row.get("date")),
            "description": row.get("description") or row.get("narration") or row.get("merchant") or "",
            "amount": normalize_amount(row.get("amount")),
            "flow_type": row.get("flow_type") or None,
            "category": row.get("category"),
            "subcategory": row.get("subcategory"),
            "classification": row.get("classification"),
            "merchant_payee": row.get("merchant_payee"),
            "payer": row.get("payer") or infer_payer(source_name),
            "source_name": source_name,
        }
        ok, _ = insert_transaction(conn, batch_id, raw_id, tx)
        added += 1 if ok else 0
        duplicates += 0 if ok else 1
    conn.execute("UPDATE import_batches SET status='committed', duplicate_count=? WHERE import_batch_id=?", (duplicates, batch_id))
    seed_removed = disable_seed_data_after_statement(conn, "dashboard")
    return {"batch_id": batch_id, "rows": len(rows), "added": added, "duplicates": duplicates, "errors": 0, "seed_removed": seed_removed}


def select(name: str, values: list[str], selected: str | None) -> str:
    opts = []
    for value in values:
        mark = " selected" if value == (selected or "") else ""
        opts.append(f"<option value='{html.escape(value)}'{mark}>{html.escape(human_label(value) if value else 'Choose one')}</option>")
    return f"<select name='{html.escape(name)}'>" + "".join(opts) + "</select>"


def parse_multipart(headers, body: bytes) -> tuple[dict, dict]:
    content_type = headers.get("Content-Type", "")
    boundary = content_type.split("boundary=", 1)[-1].encode()
    fields, files = {}, {}
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, value = part.split(b"\r\n\r\n", 1)
        value = value.rstrip(b"\r\n--")
        head_text = head.decode("utf-8", "ignore")
        disposition = next((line for line in head_text.splitlines() if line.lower().startswith("content-disposition:")), "")
        name_match = re.search(r'(?:^|;)\s*name="([^"]*)"', disposition, re.I)
        filename_match = re.search(r'(?:^|;)\s*filename="([^"]*)"', disposition, re.I)
        name = name_match.group(1) if name_match else None
        filename = filename_match.group(1) if filename_match else None
        if not name:
            continue
        if filename:
            files[name] = (Path(filename).name, value)
        else:
            fields[name] = value.decode("utf-8", "ignore")
    return fields, files


def chat_command(args: list[str]) -> int:
    """Small CLI hook for future Kanakku chat actions."""
    init_db()
    if not args:
        print("Commands: set-baseline, ingest-statement, process-pending-imports")
        return 2
    cmd = args[0]
    if cmd == "process-pending-imports":
        print(json.dumps({"ok": True, **process_pending_imports()}))
        return 0
    with db() as conn:
        if cmd == "set-baseline" and len(args) >= 4:
            category, amount, effective = args[1], float(args[2]), args[3]
            bid = "baseline_" + stable_hash(category, effective, time.time())
            conn.execute(
                "INSERT INTO baselines(baseline_id, scope, category, amount, effective_month, updated_source, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (bid, "parent_category", category, amount, effective, "chat", now_iso(), now_iso()),
            )
            audit(conn, "chat", "set_baseline", "baseline", bid, after={"category": category, "amount": amount, "effective_month": effective})
            print(json.dumps({"ok": True, "baseline_id": bid}))
            return 0
        if cmd == "ingest-statement" and len(args) >= 4:
            file_path, source_name, statement_month = args[1], args[2], args[3]
            statement_start_date = args[4] if len(args) >= 5 and args[4] != "-" else None
            statement_end_date = args[5] if len(args) >= 6 and args[5] != "-" else None
            info = import_statement_path(file_path, source_name, statement_month, statement_start_date, statement_end_date, actor="chat")
            info["ok"] = True
            print(json.dumps(info))
            return 0
        print("Unsupported or incomplete command.")
        return 2


def run() -> None:
    init_db()
    start_import_worker()
    port = int(env("KANAKKU_PORT", "5010"))
    print(f"Varavu.Selavu running on http://0.0.0.0:{port}")
    print(f"Login user: {APP_USER} | Set KANAKKU_PASSWORD before real use.")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "chat":
        raise SystemExit(chat_command(sys.argv[2:]))
    run()

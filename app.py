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
    "Groceries & Household",
    "Baby",
    "Idli",
    "Health",
    "Transport",
    "Lifestyle",
    "Shopping",
    "Travel",
    "Investments",
    "Pocket change",
]
# Money-in categories: chips/filters/drawer list them, but the money-out breakdown, Baselines and creep-watch never do.
INCOME_CATEGORIES = ["Salary", "Dividend income", "Other income"]
# Categories that are kept out of every rollup (breakdown, dashboard, charts) whatever their flow — book-keeping noise.
HIDDEN_CATEGORIES = ["Bank internal transfers"]
POCKET_CHANGE = "Pocket change"
POCKET_CHANGE_RULE_ID = "rule_pocket_change"
POCKET_CHANGE_SETTING = "pocket_change_threshold"
POCKET_CHANGE_DEFAULT = 200

# Defined subcategories per parent. Forms offer these in a dropdown plus an "Other…" free-text
# escape hatch, so values outside this list are allowed (they just aren't suggested).
SUBCATEGORIES: dict[str, list[str]] = {
    "Home & Utilities": ["Rent", "Loan EMI", "Electricity & Utilities", "Internet & Phone", "Household Help & Services", "Repairs & Maintenance", "Taxes", "Bank Fees"],
    "Food": ["Dining", "Food Delivery", "Cafes & Coffee"],
    "Groceries & Household": ["Groceries", "Meat & Fish", "Household Supplies"],
    "Baby": ["Essentials", "Doctor Consultation", "Medicines", "Shopping"],
    "Idli": ["Essentials / Care", "Grooming", "Boarding & Training"],
    "Health": ["Doctor Consultation", "Diagnostics / Tests", "Medicines"],
    "Transport": ["Fuel", "Cab", "Wallet Loading"],
    "Lifestyle": ["Subscriptions", "Software & AI Tools", "Entertainment", "Fitness & Sports", "Personal Care", "Work & Coworking"],
    "Shopping": ["Online / Amazon", "Clothing", "Electronics", "Home & Kitchen"],
    "Travel": ["Vacation", "Flight Tickets"],
    "Investments": ["SIP / Investment", "US Stocks"],
    "Pocket change": [],
    "Salary": [],
    "Dividend income": [],
    "Other income": [],
    "Bank internal transfers": [],
}
SUBCATEGORY_CUSTOM = "__custom__"

# Visual identity per parent category: (css slug, lucide icon, chip background, chip text).
# Text/background pairs are chosen for >=4.5:1 contrast so the chips stay legible.
CATEGORY_STYLE = {
    "Home & Utilities": ("home", "house", "#E8EEF9", "#2C5D8F"),
    "Food": ("food", "utensils", "#FDEBD9", "#9A4A0F"),
    "Groceries & Household": ("groceries", "shopping-basket", "#EAF3DF", "#3F6B1F"),
    "Baby": ("baby", "baby", "#FBE4EE", "#A0305F"),
    "Idli": ("idli", "paw-print", "#EFE6F8", "#6B3FA0"),
    "Health": ("health", "heart-pulse", "#FBE6E3", "#B4382D"),
    "Transport": ("transport", "car", "#E3F1F6", "#1F6E86"),
    "Lifestyle": ("lifestyle", "sparkles", "#F6E9FB", "#7A3E9D"),
    "Shopping": ("shopping", "shopping-bag", "#FFF3D6", "#8A5A00"),
    "Travel": ("travel", "plane", "#E1F4F0", "#0F6E5C"),
    "Investments": ("investments", "trending-up", "#DFF1E8", "#0B6B4D"),
    "Pocket change": ("pocket", "coins", "#EEF0EA", "#4B5A52"),
    "Salary": ("salary", "banknote", "#E1F4F0", "#0F6E5C"),
    "Dividend income": ("dividend", "hand-coins", "#E8EEF9", "#2C5D8F"),
    "Other income": ("other-income", "percent", "#E3F1F6", "#1F6E86"),
    "Bank internal transfers": ("bank-transfer", "arrow-left-right", "#EEF0EE", "#5C6B63"),
}
# Free-text categories created from Review that aren't in the list above.
OTHER_CATEGORY_STYLE = ("other", "tag", "#EEF0EE", "#5C6B63")
CATEGORY_CHIP_CSS = " ".join(
    f".cat-{slug_} {{ background:{bg}; color:{fg}; }}"
    for slug_, _icon, bg, fg in list(CATEGORY_STYLE.values()) + [OTHER_CATEGORY_STYLE]
)
UNCATEGORISED_LABEL = "Uncategorised"
OTHER_INFLOW_LABEL = "Other inflow"
UNCATEGORISED_FILTER = "__none__"
SHORTFALL_LABEL = "Shortfall (from savings)"

FLOW_TYPES = ["spend", "income", "refund", "transfer", "card_payment", "reversal", "fee", "unknown"]
CLASSIFICATIONS = ["fixed", "baseline_variable", "controllable", "one_off", "excluded"]
# Small, muted icons for the secondary axes so the coloured category chip stays the visual anchor.
KIND_ICONS = {
    "classification": {"fixed": "lock", "baseline_variable": "repeat", "controllable": "hand", "one_off": "zap", "excluded": "ban"},
    "flow": {"spend": "arrow-up-right", "income": "arrow-down-left", "transfer": "arrow-left-right", "refund": "rotate-ccw",
             "fee": "receipt", "card_payment": "credit-card", "reversal": "undo-2", "unknown": "circle-help"},
}
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
STATEMENT_PASSWORD_KEY = env("STATEMENT_PASSWORD_KEY", "")


class PasswordKeyMissing(Exception):
    """Raised when STATEMENT_PASSWORD_KEY isn't set -- statement passwords
    can't be saved or read until it is. Never a crash, always caught by the
    caller to show a clear "set this env var" message instead."""


def encrypt_password(plaintext: str) -> bytes:
    if not STATEMENT_PASSWORD_KEY:
        raise PasswordKeyMissing("STATEMENT_PASSWORD_KEY is not set")
    from cryptography.fernet import Fernet

    return Fernet(STATEMENT_PASSWORD_KEY.encode()).encrypt(plaintext.encode())


def decrypt_password(blob: bytes) -> str:
    if not STATEMENT_PASSWORD_KEY:
        raise PasswordKeyMissing("STATEMENT_PASSWORD_KEY is not set")
    from cryptography.fernet import Fernet

    return Fernet(STATEMENT_PASSWORD_KEY.encode()).decrypt(blob).decode()


_PATTERN_FIRST_LETTERS_RE = re.compile(r"first\s+(\d+)\s+(?:letters?|characters?)\s+of\s+name", re.IGNORECASE)
_PATTERN_UNRESOLVABLE_TOKENS = ("DDMM", "MMYY", "MMDD", "DDYY", "YYYY", "DOB", "DATE OF BIRTH")


def evaluate_password_pattern(pattern: str | None, source_name: str | None) -> str | None:
    """Small explicit token parser for the one pattern shape documented in
    the Admin UI ('first N letters of name + DDMM') -- not a general
    template engine (Phase 1 scope). Only the "name" portion is derivable
    from data we actually have (the account's source_name); a
    date-of-birth-style token has no stored source, so a pattern that
    needs one is left unresolved (None) rather than guessed. A wrong
    guess is safe either way -- it just fails to decrypt and the caller
    falls through to the next resolution step."""
    if not pattern:
        return None
    match = _PATTERN_FIRST_LETTERS_RE.search(pattern)
    if not match:
        return None
    remainder = pattern[match.end():].upper()
    if any(token in remainder for token in _PATTERN_UNRESOLVABLE_TOKENS):
        return None
    person = (source_name or "").split()[0] if source_name else ""
    if not person:
        return None
    return person[: int(match.group(1))].upper()


def resolve_statement_password(
    conn: sqlite3.Connection, source_id: str, source_name: str, inline_password: str | None
) -> str | None:
    """Resolution order: an explicitly typed inline password always wins
    (the user is actively providing/correcting it this time); otherwise
    the stored password for the account; otherwise a pattern-derived
    guess. Returns None if nothing is available -- the caller then falls
    back to an unencrypted-open attempt / a wrong-password failure."""
    if inline_password:
        return inline_password
    row = conn.execute(
        "SELECT encrypted_password, password_pattern FROM account_passwords WHERE source_id=?", (source_id,)
    ).fetchone()
    if not row:
        return None
    if row["encrypted_password"]:
        try:
            return decrypt_password(row["encrypted_password"])
        except PasswordKeyMissing:
            pass
    return evaluate_password_pattern(row["password_pattern"], source_name)


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
    "date outside statement period": "Date outside statement period",
    "couldn't parse this row": "Couldn't parse this row",
    "reconciliation failed": "Statement balance didn't reconcile",
    "manual_uncategorised": "Sent back for review",
}


def reason_labels(reasons: str | list[str]) -> list[str]:
    values = reasons.split(",") if isinstance(reasons, str) else reasons
    labels = []
    for value in values:
        label = REASON_LABELS.get(value.strip(), value.strip().replace("_", " ").capitalize())
        if label and label not in labels:
            labels.append(label)
    return labels


def category_style(category: str | None) -> tuple[str, str, str, str]:
    if not category:
        return ("none", "circle-help", "transparent", "var(--ink-600)")
    return CATEGORY_STYLE.get(category, OTHER_CATEGORY_STYLE)


def render_category_chip(category: str | None, subcategory: str | None = None, *, link: bool = True, month: str | None = None) -> str:
    """Coloured capsule with icon for a category. Links to the filtered transaction list unless link=False."""
    slug_, icon, _bg, _fg = category_style(category)
    label = category or UNCATEGORISED_LABEL
    inner = f'<i data-lucide="{icon}"></i><span>{html.escape(label)}</span>'
    if subcategory:
        inner += f'<span class="chip-sub">· {html.escape(subcategory)}</span>'
    title = html.escape(label + (f" · {subcategory}" if subcategory else ""), quote=True)
    if not link:
        return f'<span class="chip cat-chip cat-{slug_}" title="{title}">{inner}</span>'
    params = {"category": category or UNCATEGORISED_FILTER}
    if month:
        params["month"] = month
    href = "/transactions?" + urllib.parse.urlencode(params)
    return f'<a class="chip cat-chip cat-{slug_}" href="{html.escape(href, quote=True)}" title="{title}">{inner}</a>'


def render_kind_chip(kind: str, value: str | None) -> str:
    """Muted chip for classification / flow type. Empty string when the value is unset."""
    if not value:
        return ""
    icon = KIND_ICONS.get(kind, {}).get(value, "circle")
    return f'<span class="chip kind-chip kind-{html.escape(value)}"><i data-lucide="{icon}"></i><span>{html.escape(human_label(value))}</span></span>'


# Import batch status -> (lucide icon, background, text). Keys are the raw values written to import_batches.status.
IMPORT_STATUS_STYLE = {
    "committed": ("check-circle-2", "var(--pos-100)", "var(--pos-700)"),
    "completed": ("check-circle-2", "var(--pos-100)", "var(--pos-700)"),
    "imported": ("check-circle-2", "var(--pos-100)", "var(--pos-700)"),
    "pending_review": ("eye", "var(--warn-100)", "var(--warn-700)"),
    "extracting": ("loader-circle", "var(--info-100)", "var(--info-700)"),
    "pending_pdf_extraction": ("clock", "var(--page-bg)", "var(--ink-600)"),
    "needs_parser": ("wrench", "var(--warn-100)", "var(--warn-700)"),
    "failed": ("alert-circle", "var(--dang-100)", "var(--dang-700)"),
    "unable_to_parse": ("alert-circle", "var(--dang-100)", "var(--dang-700)"),
    "cancelled": ("ban", "var(--page-bg)", "var(--ink-600)"),
}
IMPORT_STAGE_LABELS = {"decrypting": "Reading statement…", "parsing": "Extracting transactions…", "extracting": "Extracting transactions…", "validating": "Checking balances…"}
IMPORT_STATUS_CSS = " ".join(
    f".status-{key} {{ background:{bg}; color:{fg}; }}" for key, (_icon, bg, fg) in IMPORT_STATUS_STYLE.items()
) + " .status-chip svg { width:14px; height:14px; } .status-extracting svg { animation: status-spin 1s linear infinite; } @keyframes status-spin { to { transform: rotate(360deg); } }"


def render_import_status_chip(status: str | None, stage: str | None = None) -> str:
    """Coloured capsule with icon for an import batch status. Live stage label while extracting."""
    key = (status or "").strip() or "unknown"
    icon, _bg, _fg = IMPORT_STATUS_STYLE.get(key, ("circle", "var(--page-bg)", "var(--ink-600)"))
    label = IMPORT_STAGE_LABELS.get(stage or "", "") if key == "extracting" else ""
    label = label or human_label(key)
    return (f'<span class="chip status-chip status-{html.escape(key)} status-badge" data-status="{html.escape(key)}">'
            f'<i data-lucide="{icon}"></i><span class="status-label">{html.escape(label)}</span></span>')


def render_import_detail(notes: str | None) -> str:
    """Hover/focus popover with the batch notes. Summary notes are ' · '-joined -> one bullet per part;
    anything else (error messages) is a single paragraph."""
    text = (notes or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in text.split(" · ") if p.strip()]
    if len(parts) > 1:
        body = "<ul>" + "".join(f"<li>{html.escape(p)}</li>" for p in parts) + "</ul>"
    else:
        body = f"<p>{html.escape(text)}</p>"
    return (f'<span class="status-info"><button type="button" class="status-info-btn" aria-label="Import details" aria-expanded="false">'
            f'<i data-lucide="info"></i></button><span class="status-pop" role="tooltip">{body}</span></span>')


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


def normalize_description_for_hash(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).upper()


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

            CREATE TABLE IF NOT EXISTS account_passwords (
              source_id TEXT PRIMARY KEY,
              encrypted_password BLOB,
              password_pattern TEXT,
              account_number_hint TEXT,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(source_id) REFERENCES sources(source_id)
            );

            CREATE TABLE IF NOT EXISTS bank_adapters (
              adapter_id TEXT PRIMARY KEY,
              source_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              config_json TEXT NOT NULL,
              verified INTEGER NOT NULL DEFAULT 0,
              is_active INTEGER NOT NULL DEFAULT 0,
              created_by TEXT NOT NULL DEFAULT 'hand_built',
              notes TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(source_id) REFERENCES sources(source_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_bank_adapters_source_version ON bank_adapters(source_id, version);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_bank_adapters_active ON bank_adapters(source_id) WHERE is_active=1;
            """
        )
        ensure_schema(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO sources(source_id, source_name, payer, status) VALUES(?,?,?,?)",
            SOURCES,
        )
        seed_bank_adapters(conn)
        if seed:
            seed_defaults(conn)
        # Backfill: rows imported before a rule (or the Pocket change fallback) existed get another chance.
        # Only uncategorised, rule-less, non-overridden rows are touched, so this is safe to run on every boot.
        result = reapply_rules(conn, only_uncategorised=True)
        if result["updated"]:
            audit(conn, "system", "reapply_rules", "rules", "uncategorised", after={**result, "trigger": "startup_backfill"})


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS ix_manual_overrides_txn ON manual_overrides(transaction_id, created_at)")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(import_batches)").fetchall()}
    if "statement_start_date" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN statement_start_date TEXT")
    if "statement_end_date" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN statement_end_date TEXT")
    if "excluded_at" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN excluded_at TEXT")
    if "deleted_at" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN deleted_at TEXT")
    if "stage" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN stage TEXT")
    if "reconciliation_status" not in columns:
        conn.execute("ALTER TABLE import_batches ADD COLUMN reconciliation_status TEXT")
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


def pocket_change_threshold(conn: sqlite3.Connection) -> int:
    """Money-out below this (₹) with no matching rule is filed under Pocket change. 0 = off."""
    stored = setting(conn, POCKET_CHANGE_SETTING)
    if stored is None:
        return POCKET_CHANGE_DEFAULT
    try:
        return max(0, int(float(stored)))
    except (TypeError, ValueError):
        return POCKET_CHANGE_DEFAULT


def set_pocket_change_threshold(conn: sqlite3.Connection, value: int, actor: str = "dashboard") -> None:
    before = setting(conn, POCKET_CHANGE_SETTING)
    set_setting(conn, POCKET_CHANGE_SETTING, str(max(0, int(value))))
    audit(conn, actor, "set_pocket_change_threshold", "setting", POCKET_CHANGE_SETTING, before={"value": before}, after={"value": max(0, int(value))})


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


def _set_stage(batch_id: str, stage: str) -> None:
    with db() as conn:
        conn.execute("UPDATE import_batches SET stage=? WHERE import_batch_id=?", (stage, batch_id))


def _format_import_summary(source_name, meta, transactions, recon, duplicate_count, review_count, ocr_used) -> str:
    total_in = sum(t["amount"] for t in transactions if t["amount"] > 0)
    total_out = -sum(t["amount"] for t in transactions if t["amount"] < 0)
    period = f"{meta['period_start']}–{meta['period_end']} · " if meta["period_start"] and meta["period_end"] else ""
    if recon["status"] == "ok":
        balance_chip = "Balance check ✓"
    elif recon["status"] == "failed":
        balance_chip = f"Balance check ✗ off by {money(recon['discrepancy'])}"
    else:
        balance_chip = "Balance check: not available"
    parts = [f"{source_name or 'This account'} · {period}{len(transactions)} transactions found", f"{money(total_in)} in / {money(total_out)} out", balance_chip]
    if duplicate_count:
        parts.append(f"{duplicate_count} duplicates skipped")
    if review_count:
        parts.append(f"{review_count} row{'s' if review_count != 1 else ''} need review")
    if ocr_used:
        parts.append("OCR used")
    return " · ".join(parts)


def _process_pdf_batch(row: sqlite3.Row, password: str | None) -> tuple[str, str]:
    """Decrypt, parse (docling), normalize (adapter engine), validate and
    stage a PDF statement's rows into raw_imports for review. Returns
    (status, notes). Never raises for expected failure modes -- those are
    all translated into a kind, specific status/notes pair; only a genuine
    bug propagates to the caller's catch-all."""
    try:
        from pdf_import.decrypt import DecryptError, WrongPasswordError, decrypted_copy, is_encrypted
        from pdf_import.docling_pipeline import parse_pdf
        from pdf_import.adapter_engine import apply_adapter
        from pdf_import.reconcile import check_date_range, reconcile_or_unavailable
    except ModuleNotFoundError as exc:
        # Optional PDF stack (pikepdf / docling / cryptography) isn't installed in this
        # interpreter. Say which package, and how to fix it, instead of a bare error name.
        missing = exc.name or "a PDF dependency"
        return "needs_parser", (
            f"PDF parsing needs the '{missing}' package, which isn't installed for the Python running this app. "
            f"Run: pip install -r requirements.txt, then retry."
        )

    source_id = row["source_id"]
    if not source_id:
        return "failed", "Couldn't match this upload to a known account. Re-upload and pick the account explicitly."

    with db() as conn:
        adapter_row = conn.execute(
            "SELECT config_json FROM bank_adapters WHERE source_id=? AND is_active=1", (source_id,)
        ).fetchone()
    if not adapter_row:
        return "failed", "This bank isn't set up yet — the Adapter Trainer is coming soon. For now, this statement can't be imported automatically."
    config = json.loads(adapter_row["config_json"])

    path = UPLOAD_DIR / row["file_name"]
    _set_stage(row["import_batch_id"], "decrypting")
    try:
        if is_encrypted(path):
            with decrypted_copy(path, password) as decrypted_path:
                _set_stage(row["import_batch_id"], "parsing")
                table, text, ocr_used = parse_pdf(decrypted_path, config["header_signature"])
        else:
            _set_stage(row["import_batch_id"], "parsing")
            table, text, ocr_used = parse_pdf(path, config["header_signature"])
    except WrongPasswordError:
        return "failed", "That password didn't open it — try again."
    except DecryptError:
        return "failed", "Couldn't open this file. It may be corrupted — try re-uploading."

    _set_stage(row["import_batch_id"], "extracting")
    result = apply_adapter(table, text, config)
    transactions, unparsed_rows, meta = result["transactions"], result["unparsed_rows"], result["statement_meta"]
    if not transactions and not unparsed_rows:
        return "failed", "Couldn't find a transaction table in this file."

    _set_stage(row["import_batch_id"], "validating")
    recon = reconcile_or_unavailable(transactions, meta["opening_balance"], meta["closing_balance"])
    out_of_range = set()
    if meta["period_start"] and meta["period_end"]:
        try:
            out_of_range = set(check_date_range(transactions, meta["period_start"], meta["period_end"]))
        except ValueError:
            pass  # statement dates in an unexpected format -- skip the range check rather than crash

    with db() as conn:
        existing_fingerprints = {r[0] for r in conn.execute("SELECT fingerprint FROM transactions WHERE fingerprint IS NOT NULL").fetchall()}
        row_number, duplicate_count = 0, 0
        for index, txn in enumerate(transactions):
            normalized = {
                "transaction_date": txn["date"],
                "description": txn["description"],
                "amount": txn["amount"],
                "source_name": row["source_name"],
                "balance_after": txn.get("balance_after"),
                "time": txn.get("time"),
            }
            fingerprint = stable_hash(row["source_name"], normalized["transaction_date"], normalized["amount"], normalize_description_for_hash(normalized["description"]))
            is_duplicate = fingerprint in existing_fingerprints
            duplicate_count += int(is_duplicate)
            payload = {
                "normalized": normalized,
                "raw_row": txn.get("raw_row"),
                "review_reason": "date outside statement period" if index in out_of_range else None,
                "duplicate_preview": is_duplicate,
            }
            conn.execute(
                "INSERT INTO raw_imports(raw_import_id, import_batch_id, row_number, raw_json, created_at) VALUES(?,?,?,?,?)",
                (f"raw_{row['import_batch_id']}_{row_number}", row["import_batch_id"], row_number, json.dumps(payload, default=str), now_iso()),
            )
            row_number += 1
        for unparsed in unparsed_rows:
            payload = {"normalized": None, "raw_row": unparsed["raw_row"], "review_reason": "couldn't parse this row", "duplicate_preview": False}
            conn.execute(
                "INSERT INTO raw_imports(raw_import_id, import_batch_id, row_number, raw_json, created_at) VALUES(?,?,?,?,?)",
                (f"raw_{row['import_batch_id']}_{row_number}", row["import_batch_id"], row_number, json.dumps(payload, default=str), now_iso()),
            )
            row_number += 1

        review_count = len(out_of_range) + len(unparsed_rows)
        summary = _format_import_summary(row["source_name"], meta, transactions, recon, duplicate_count, review_count, ocr_used)
        conn.execute(
            "UPDATE import_batches SET row_count=?, duplicate_count=?, notes=?, reconciliation_status=? WHERE import_batch_id=?",
            (len(transactions), duplicate_count, summary, recon["status"], row["import_batch_id"]),
        )
    return "pending_review", summary


def commit_pdf_batch(conn: sqlite3.Connection, batch: sqlite3.Row) -> dict:
    """Insert every staged raw_imports row for a pending_review PDF batch.
    insert_transaction's fingerprint UNIQUE constraint is the single real
    dedupe check -- the duplicate_preview flag staged at parse time was
    only ever a display estimate. Unparseable rows still get a
    transactions row (never silently dropped) with the raw text preserved."""
    raw_rows = conn.execute(
        "SELECT raw_import_id, raw_json FROM raw_imports WHERE import_batch_id=? ORDER BY row_number", (batch["import_batch_id"],)
    ).fetchall()
    reconciliation_failed = batch["reconciliation_status"] == "failed"
    added, duplicates, review_count = 0, 0, 0
    for raw in raw_rows:
        payload = json.loads(raw["raw_json"])
        normalized = payload.get("normalized")
        review_reason = payload.get("review_reason")
        if normalized is None:
            raw_row = payload.get("raw_row") or []
            normalized = {
                "transaction_date": batch["statement_start_date"],
                "description": " | ".join(str(cell) for cell in raw_row if cell) or "Unparsed row",
                "amount": 0.0,
                "source_name": batch["source_name"],
                "flow_type": "unknown",
                "confidence": 0,
                "notes": "Raw row: " + json.dumps(raw_row, default=str),
            }
        ok, transaction_id = insert_transaction(conn, batch["import_batch_id"], raw["raw_import_id"], dict(normalized))
        if not ok:
            duplicates += 1
            continue
        added += 1
        if review_reason:
            create_review_item(conn, transaction_id, review_reason)
            review_count += 1
        elif reconciliation_failed:
            create_review_item(conn, transaction_id, "reconciliation failed")
            review_count += 1
    conn.execute("UPDATE import_batches SET status='committed', row_count=?, duplicate_count=? WHERE import_batch_id=?", (added, duplicates, batch["import_batch_id"]))
    audit(conn, "dashboard", "commit_pdf_import", "import_batch", batch["import_batch_id"], after={"added": added, "duplicates": duplicates, "review_count": review_count})
    return {"added": added, "duplicates": duplicates}


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
            claimed = conn.execute("UPDATE import_batches SET status='extracting', stage='decrypting', notes=? WHERE import_batch_id=? AND status='pending_pdf_extraction'", ("Extraction started. Passwords are never stored.", row["import_batch_id"])).rowcount
            if not claimed:
                continue
            audit(conn, "worker", "pdf_extraction_started", "import_batch", row["import_batch_id"])
            row_password = password if row["import_batch_id"] == batch_id else None
            if not row_password and row["source_id"]:
                row_password = resolve_statement_password(conn, row["source_id"], row["source_name"] or "", None)
        path = UPLOAD_DIR / (row["file_name"] or "")
        try:
            if not path.is_file():
                status, note = "failed", "The uploaded file is missing from storage. Re-upload the statement."
            elif (row["file_type"] or "").lower() != "pdf":
                status, note = "needs_parser", f"No extractor is configured for {human_label(row['file_type'])} files."
            else:
                status, note = _process_pdf_batch(row, row_password)
            with db() as conn:
                conn.execute("UPDATE import_batches SET status=?, notes=? WHERE import_batch_id=?", (status, note, row["import_batch_id"]))
                audit(conn, "worker", "pdf_extraction_succeeded" if status == "pending_review" else "pdf_extraction_failed", "import_batch", row["import_batch_id"], after={"status": status, "notes": note})
        except Exception as exc:
            with db() as conn:
                detail = str(exc).strip().splitlines()[0][:160] if str(exc).strip() else ""
                note = f"Worker failed: {type(exc).__name__}{': ' + detail if detail else ''}. Retry extraction or re-upload the statement."
                conn.execute("UPDATE import_batches SET status='failed', notes=? WHERE import_batch_id=?", (note, row["import_batch_id"]))
                audit(conn, "worker", "pdf_extraction_failed", "import_batch", row["import_batch_id"], after={"status": "failed", "notes": note})
        processed += 1
        if batch_id:
            return {"processed": processed}


def seed_bank_adapters(conn: sqlite3.Connection) -> None:
    """Load the checked-in Track-1 adapter configs into bank_adapters.
    INSERT OR IGNORE: deleting a DB row falls back to the checked-in JSON
    on next boot."""
    from pdf_import.schema import validate_adapter

    adapters_dir = ROOT / "pdf_import" / "adapters"
    if not adapters_dir.exists():
        return
    for path in sorted(adapters_dir.glob("*.json")):
        config = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_adapter(config)
        if errors:
            raise ValueError(f"invalid adapter config {path.name}: {errors}")
        conn.execute(
            """
            INSERT OR IGNORE INTO bank_adapters
              (adapter_id, source_id, version, config_json, verified, is_active, created_by, created_at)
            VALUES (?, ?, 1, ?, 1, 1, 'hand_built', ?)
            """,
            (
                f"{config['source_id']}-v1",
                config["source_id"],
                json.dumps(config),
                now_iso(),
            ),
        )


# Built-in rules: (name, match_type, pattern, source_name, category, subcategory, classification, flow_type, confidence).
# Patterns are case-insensitive substrings OR-ed with "|" (see apply_rules). Keep them specific: a bare "RENT"
# used to match "TRENT DIV" dividends and "ZORENT"; a bare "SWIGGY" used to beat the Instamart rule.
# Rules are inserted with INSERT OR IGNORE keyed on rule_id, so a user's later edits/pauses survive restarts
# and new defaults still show up on existing databases.
DEFAULT_RULES = [
    # --- money movement (no category; excluded from spend) ---
    ("Credit card bill payment", "description_contains", "CARD PAYMENT|HDFC CARD|CREDIT CARD PAYMENT|ONLINE PYMT RECD|PAYMENT RECEIVED|PG OSHDFCCC", None, None, None, "excluded", "card_payment", 0.95),
    ("FD auto-sweep", "description_contains", "SWEEP TRANSFER|SWEEP TRF|FD PREMAT PROCEEDS", None, "Bank internal transfers", None, "excluded", "transfer", 0.95),
    ("Family transfers", "description_contains", "JANANIYA R|NANNY NAME", None, None, None, "excluded", "transfer", 0.86),
    # More specific than "Family transfers" (0.9 > 0.86): IMPS to the nanny is a fixed household cost; the NEFT to the loan account is the EMI.
    ("Nanny salary", "regex", r"SENTIMPS.*NANNY NAME", None, "Home & Utilities", "Household Help & Services", "fixed", "spend", 0.9),
    ("Home loan EMI (Jananiya)", "regex", r"NEFT.*JANANIYA R/BANK OF", None, "Home & Utilities", "Loan EMI", "fixed", "spend", 0.9),
    ("Transfers in", "description_contains", "IFT PAYMENT|UPI/MR S VIJAY", None, None, None, "excluded", "transfer", 0.8),
    ("Salary", "description_contains", "EMPLOYER PAYROLL|TIGER ANALYTICS|TIGERANALYTI", None, "Salary", None, "excluded", "income", 0.95),
    ("Dividend income", "description_contains", "NACH-ECS-CR|NACH-10-CR", None, "Dividend income", None, "excluded", "income", 0.9),
    ("Interest received", "description_contains", "INT.PD:", None, None, None, "excluded", "income", 0.9),
    ("Income tax refund", "description_contains", "ITDTAX REFUND", None, None, None, "excluded", "income", 0.9),
    ("UPI credit adjustment", "description_contains", "UPI_CRADJ", None, None, None, None, "refund", 0.86),
    # --- fees ---
    ("Bank fees & FX markup", "description_contains", "DCC FEE|DCC TRANSACTION|DAILY BAL ALERTS|IGST-VPS|FCY MARKUP FEE", None, "Home & Utilities", "Bank Fees", "controllable", "fee", 0.92),
    # --- groceries & household (0.92 so they beat the generic food-delivery patterns below) ---
    ("Quick commerce groceries", "description_contains", "INSTAMART|INSTAMA|SWIGGY PVT LTD GROCERY|BLINKIT|BLINK COMMERCE|FIRSTCLUB|AMAZON IN GROCERY|ZEPTO", None, "Groceries & Household", "Groceries", "baseline_variable", "spend", 0.92),
    ("Milk, dairy & fruit", "description_contains", "UPI/AKSHAYAKALPA|UPI/COUNTRY DELIGHT|UPI/BEEJAPURI DAIRY|UPI/VIJAY MANGO FR|BOLAS AGRO", None, "Groceries & Household", "Groceries", "baseline_variable", "spend", 0.9),
    ("Meat & fish delivery", "description_contains", "UPI/LICIOUS|UPI/MY CHICKEN AND", None, "Groceries & Household", "Meat & Fish", "baseline_variable", "spend", 0.9),
    # --- food ---
    ("Food delivery apps", "description_contains", "ZOMATO|ETERNAL LIMITED|SWIGGY|UPI/POWLE HOME FOOD", None, "Food", "Food Delivery", "controllable", "spend", 0.86),
    ("Restaurants", "description_contains", "UPI/MCDONALDS|UPI/IDLY BAR|UPI/APPUS DONNE|UPI/PATTIKATTAN|UPI/TB 9 BBQ|UPI/IYENGARS BAK|BURMA BURMA|LUCKY CHAN|ANDAMEN|HOT SPOT FOOD|UPI/UMESH PRABHDAS|UPI/UMESH KUMAR PRA", None, "Food", "Dining", "controllable", "spend", 0.86),
    ("Cafes & coffee", "description_contains", "UPI/COFFEE MAKERS|UPI/COFFEE DAY|STARBUCKS|UPI/CHAI DAYS|UPI/KAFFEEKLATSCH|UPI/CAFE RITE|UPI/MAVERICK AND F|UPI/PUPS N CUPS|THIRD WAVE COFFEE", None, "Food", "Cafes & Coffee", "controllable", "spend", 0.88),
    # --- lifestyle ---
    ("Streaming & app subscriptions", "description_contains", "APPLE MEDIA SE|NETFLIX|SPOTIFY|YOUTUBEGOOGLE|AMAZON INDIA CYBS SI|PLAYSTATION|EXPRESSVPN|ZEE ENTERTAINMENT|UPG*PAYMENTICO", None, "Lifestyle", "Subscriptions", "controllable", "spend", 0.88),
    ("Google subscriptions", "description_contains", "UPI/GOOGLE INDIA", None, "Lifestyle", "Subscriptions", "controllable", "spend", 0.8),
    ("Software & AI tools", "description_contains", "ANTHROPIC|CLAUDE.AI|OPENAI|OPENROUTER|PADDLE.NET|PROFILEPICTURE.AI|CANVA*|FIGMA", None, "Lifestyle", "Software & AI Tools", "controllable", "spend", 0.9),
    ("Fitness & sports", "description_contains", "UPI/GET MY BIB|FITTR|DECATHLON", None, "Lifestyle", "Fitness & Sports", "controllable", "spend", 0.86),
    ("Entertainment & events", "description_contains", "UPI/BIG TREE ENTER|UPI/ALIVE/|UPI/VARA ENTERTAIN", None, "Lifestyle", "Entertainment", "controllable", "spend", 0.86),
    ("Salon & personal care", "description_contains", "UPI/CUT STYLE SALO", None, "Lifestyle", "Personal Care", "controllable", "spend", 0.86),
    ("Coworking", "description_contains", "UPI/AWFIS", None, "Lifestyle", "Work & Coworking", "controllable", "spend", 0.8),
    # --- home & utilities ---
    ("Rent", "description_contains", "SONA SINGH|HOUSE RENT|RENT PAYMENT|RENT SONA", None, "Home & Utilities", "Rent", "fixed", "spend", 0.9),
    ("Loan EMI", "description_contains", "SECOND EMI", None, "Home & Utilities", "Loan EMI", "fixed", "spend", 0.86),
    ("Household help & home services", "description_contains", "UPI/SNABBIT|UPI/URBANCOMPANY|UPI/SANJAY R/|UPI/MANI RAM DAS", None, "Home & Utilities", "Household Help & Services", "baseline_variable", "spend", 0.88),
    ("Airtel", "description_contains", "AIRTEL", None, "Home & Utilities", "Internet & Phone", "fixed", "spend", 0.9),
    ("Utility bill via BBPS", "description_contains", "UPI/AXIS BANK BBPS", None, "Home & Utilities", "Electricity & Utilities", "fixed", "spend", 0.8),
    ("Income tax & tax filing", "description_contains", "UPI/CBDT TIN|UPI/DEFMACRO SOFTWA", None, "Home & Utilities", "Taxes", "one_off", "spend", 0.86),
    ("Appliance repairs (Sony service)", "description_contains", "ABHIVRUDHI TEC", None, "Home & Utilities", "Repairs & Maintenance", "one_off", "spend", 0.86),
    # --- shopping ---
    ("Amazon & online orders", "description_contains", "AMAZON SELLER SERVICES|UPI/AMAZON INDIA|UPI/PRATYAYA E COMM", None, "Shopping", "Online / Amazon", "controllable", "spend", 0.86),
    ("Flipkart (needs a look)", "description_contains", "FLIPKART", None, "Shopping", None, "controllable", "spend", 0.52),
    ("Clothing", "description_contains", "UNIQLO|UPI/TAILOR AND CIRC|VAN HEUSEN|WESTSIDE|UPI/VIMAL S WARRIER", None, "Shopping", "Clothing", "controllable", "spend", 0.88),
    ("Electronics", "description_contains", "UPI/QUBO", None, "Shopping", "Electronics", "controllable", "spend", 0.86),
    ("Home & kitchen", "description_contains", "UPI/KITCHENMART|IKEA INDIA|TRANCE HOME LINEN|UPI/INVISEL", None, "Shopping", "Home & Kitchen", "controllable", "spend", 0.86),
    ("Other shopping", "description_contains", "MAKOBA|STOTODO|UPI/PLAEUP", None, "Shopping", None, "controllable", "spend", 0.8),
    # --- Idli (the dog) ---
    ("Idli - food & supplies", "description_contains", "SUPERTAILS|PETS CENTRIC", None, "Idli", "Essentials / Care", "baseline_variable", "spend", 0.9),
    ("Idli - grooming", "description_contains", "WIZARD OF PAWS", None, "Idli", "Grooming", "baseline_variable", "spend", 0.9),
    ("Idli - boarding & training", "description_contains", "HAUSBERG|KLEVER K9|UPI/GARIMA TOMAR|UPI/AISHWARYA GHANE", None, "Idli", "Boarding & Training", "baseline_variable", "spend", 0.86),
    # --- health & baby ---
    ("Hospitals & clinics", "description_contains", "UPI/NHIC|UPI/NH INTEGRATED|UPI/SHREEVIK HOSPI|UPI/CHETHAN M R", None, "Health", "Doctor Consultation", "baseline_variable", "spend", 0.86),
    ("Baby clothing & gear", "description_contains", "UPI/R FOR RABBIT|ALL THINGS BABY|MINIKLUB", None, "Baby", "Shopping", "baseline_variable", "spend", 0.88),
    ("Baby essentials", "description_contains", "BABY|SUPERBOTTOMS|COCOON", None, "Baby", "Essentials", "baseline_variable", "spend", 0.84),
    # --- transport & travel ---
    ("Cabs", "description_contains", "UPI/SHOFFR MOBILIT", None, "Transport", "Cab", "controllable", "spend", 0.9),
    ("Fuel", "description_contains", "M P FUELS|UPI/GOWRISHANKAR FU", None, "Transport", "Fuel", "baseline_variable", "spend", 0.9),
    ("Petro surcharge waiver", "description_contains", "PETRO SURCHARGE WAIVER", None, "Transport", "Fuel", "baseline_variable", "refund", 0.9),
    ("FASTag & metro top-up", "description_contains", "UPI/NATIONAL HIGHWA|UPI/BANGALORE METRO", None, "Transport", "Wallet Loading", "baseline_variable", "spend", 0.86),
    ("Resorts & vacations", "description_contains", "EVOLVE BACK|ORANGE COUNTY RESORTS", None, "Travel", "Vacation", "one_off", "spend", 0.9),
    # --- investments ---
    ("Investments", "description_contains", "GROWW|SIP|NPS|PPF|MUTUAL", None, "Investments", "SIP / Investment", "excluded", "spend", 0.88),
    ("US stocks (INDmoney)", "description_contains", "TO INDMONE", None, "Investments", "US Stocks", "excluded", "spend", 0.9),
]

# One-off taxonomy moves that older databases may still hold, applied idempotently at boot.
# Built-in rules are INSERT OR IGNORE'd, so changes to an existing default rule need an explicit, idempotent update.
# (rule_id, fields to set, or None to delete) — applied only while the rule still has no category (i.e. the user hasn't customised it).
# Deleted rules are superseded by new DEFAULT_RULES entries; the startup backfill re-runs their (still uncategorised) rows.
# Notes a built-in rule stamps on every transaction it matches (applied while the rule's notes are empty).
DEFAULT_RULE_NOTES = {"rule_nanny_salary": "Nanny salary"}

RULE_UPDATES = [
    ("rule_salary", {"category": "Salary"}),
    ("rule_dividends___interest", None),  # split into "Dividend income" + "Interest received"
    ("rule_fd_auto_sweep", {"category": "Bank internal transfers"}),
    # Axis review 2026-08: savings interest surfaces as "Other income" instead of uncategorised.
    ("rule_interest_received", {"category": "Other income"}),
]

TAXONOMY_MIGRATIONS = [
    # (old category or None for any, old subcategory, new category or None to keep, new subcategory)
    (None, "Needs Review", None, None),
    ("Health", "Medical", None, "Medicines"),
    ("Food", "Groceries / Quick Commerce", "Groceries & Household", "Groceries"),
    ("Groceries & Household", "Groceries / Quick Commerce", None, "Groceries"),
    ("Food", "Dining & Delivery", None, "Food Delivery"),
]


def seed_defaults(conn: sqlite3.Connection) -> None:
    added_rules = 0
    for name, match_type, pattern, source, category, subcategory, classification, flow_type, confidence in DEFAULT_RULES:
        rid = "rule_" + slug(name)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO rules(rule_id, name, match_type, pattern, source_name, category, subcategory, classification, flow_type, confidence, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (rid, name, match_type, pattern, source, category, subcategory, classification, flow_type, confidence, now_iso(), now_iso()),
        )
        added_rules += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    # Built-in rule patterns are additive: alternatives added to DEFAULT_RULES later reach existing DBs,
    # while anything the user appended to the pattern is kept.
    for rule_id, note in DEFAULT_RULE_NOTES.items():
        conn.execute("UPDATE rules SET notes=? WHERE rule_id=? AND (notes IS NULL OR notes='')", (note, rule_id))
    for name, match_type, pattern, *_rest in DEFAULT_RULES:
        if match_type not in ("description_contains", "keyword"):
            continue  # regex/exact patterns have no A|B union semantics
        rid = "rule_" + slug(name)
        row = conn.execute("SELECT pattern FROM rules WHERE rule_id=? AND match_type=?", (rid, match_type)).fetchone()
        if not row:
            continue
        current = [p.strip() for p in (row["pattern"] or "").split("|") if p.strip()]
        merged = list(current)
        for alt in (p.strip() for p in pattern.split("|") if p.strip()):
            if alt.upper() not in {c.upper() for c in merged}:
                merged.append(alt)
        if merged != current:
            conn.execute("UPDATE rules SET pattern=?, updated_at=? WHERE rule_id=?", ("|".join(merged), now_iso(), rid))
    for rule_id, fields in RULE_UPDATES:
        if fields is None:
            conn.execute("DELETE FROM rules WHERE rule_id=? AND category IS NULL", (rule_id,))
            continue
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE rules SET {sets}, updated_at=? WHERE rule_id=? AND category IS NULL", (*fields.values(), now_iso(), rule_id))
    for old_category, old_sub, new_category, new_sub in TAXONOMY_MIGRATIONS:
        for table in ("rules", "transactions", "manual_overrides", "baselines"):
            if old_category:
                conn.execute(f"UPDATE {table} SET category=COALESCE(?, category), subcategory=? WHERE category=? AND subcategory=?", (new_category, new_sub, old_category, old_sub))
            else:
                conn.execute(f"UPDATE {table} SET category=COALESCE(?, category), subcategory=? WHERE subcategory=?", (new_category, new_sub, old_sub))
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
    elif added_rules:
        # New built-in rules arrived for an existing database: categorise the rows nothing had claimed yet.
        result = reapply_rules(conn, only_uncategorised=True)
        audit(conn, "system", "reapply_rules", "rules", "defaults", after={"added_rules": added_rules, **result})


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
        ("2026-05-04", "Swiggy Instamart", -2400, "spend", "Groceries & Household", "Groceries", "baseline_variable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-05", "Zomato Eternal", -1850, "spend", "Food", "Food Delivery", "controllable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-08", "Amazon Seller Services", -3790, "spend", "Shopping", None, "controllable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-09", "Superbottoms Baby", -2018, "spend", "Baby", "Essentials", "baseline_variable", "Jananiya", "Jananiya HDFC Bank"),
        ("2026-05-10", "Supertails", -3136, "spend", "Idli", "Essentials / Care", "baseline_variable", "Vignesh", "Vignesh HDFC Diners"),
        ("2026-05-11", "Ramdev Medical", -3691, "spend", "Health", "Medicines", "baseline_variable", "Jananiya", "Jananiya HDFC Bank"),
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


def rule_matches(rule, description_upper: str, source_name: str | None) -> bool:
    """Single source of truth for rule matching (used by apply_rules and rule_conflicts).
    Match types: description_contains/keyword (any of A|B|C), exact_merchant, normalized_merchant,
    regex (Python regex, case-insensitive; an invalid pattern never matches)."""
    if rule["source_name"] and rule["source_name"] != source_name:
        return False
    pattern = rule["pattern"] or ""
    kind = rule["match_type"]
    if kind in ("description_contains", "keyword"):
        return any(p in description_upper for p in (x.strip().upper() for x in pattern.split("|")) if p)
    if kind == "exact_merchant":
        return description_upper == pattern.upper()
    if kind == "normalized_merchant":
        return slug(description_upper) == slug(pattern)
    if kind == "regex":
        try:
            return re.search(pattern, description_upper, re.IGNORECASE) is not None
        except re.error:
            return False
    return False


def apply_rules(conn: sqlite3.Connection, tx: dict) -> dict:
    description = (tx.get("description") or "").upper()
    source = tx.get("source_name")
    best = None
    for rule in conn.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY confidence DESC").fetchall():
        if rule_matches(rule, description, source):
            best = rule
            break
    if not best:
        tx.setdefault("flow_type", infer_flow_type(tx))
        tx.setdefault("category", None)
        tx.setdefault("classification", None)
        tx["confidence"] = tx.get("confidence") or 0.2
        # Lowest-priority fallback: small unknown money-out is filed under Pocket change (never money-in/transfers).
        threshold = pocket_change_threshold(conn)
        try:
            amount = float(tx.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if threshold > 0 and not tx.get("category") and tx.get("flow_type") in ("spend", "fee") and amount < 0 and abs(amount) < threshold:
            tx.update({"category": POCKET_CHANGE, "subcategory": None, "classification": "controllable", "rule_id": POCKET_CHANGE_RULE_ID, "confidence": 0.8})
        return tx
    for key in ("category", "subcategory", "classification", "flow_type", "merchant_payee"):
        if best[key] is not None:
            tx[key] = best[key]
    tx["rule_id"] = best["rule_id"]
    tx["confidence"] = best["confidence"]
    if "notes" in best.keys() and best["notes"]:
        tx["notes"] = best["notes"]
    return tx


def rule_conflicts(conn: sqlite3.Connection, description: str | None, source_name: str | None, *, category: str | None, subcategory: str | None) -> list[dict]:
    """Every enabled rule that would match this description/source, in engine order (confidence DESC, first wins).
    Used to warn before "Remember for future matches" creates a rule that overlaps or overrides existing ones."""
    desc = (description or "").upper()
    hits: list[dict] = []
    for rule in conn.execute("SELECT * FROM rules WHERE enabled=1 ORDER BY confidence DESC").fetchall():
        if not rule_matches(rule, desc, source_name):
            continue
        hits.append({
            "rule_id": rule["rule_id"], "name": rule["name"], "match_type": rule["match_type"], "pattern": rule["pattern"],
            "category": rule["category"], "subcategory": rule["subcategory"], "classification": rule["classification"], "confidence": rule["confidence"],
            "same_outcome": (rule["category"] or None) == (category or None) and (rule["subcategory"] or None) == (subcategory or None),
            "remembered": (rule["name"] or "").startswith("Remember ") and rule["match_type"] == "exact_merchant",
        })
    return hits


def reapply_rules(conn: sqlite3.Connection, only_uncategorised: bool = False) -> dict:
    """Run the current rules over existing transactions (never those with a manual override).
    Only rows a rule actually matches are updated; their open review items are rebuilt."""
    where = "manual_override_id IS NULL"
    if only_uncategorised:
        # Rows the engine hasn't placed anywhere yet, plus rows still bound to a rule (that rule may have been
        # edited or outranked since). Never rows whose category came from the import itself.
        where += " AND (category IS NULL OR coalesce(rule_id,'') != '')"
    rows = conn.execute(f"SELECT * FROM transactions WHERE {where}").fetchall()
    updated = 0
    for row in rows:
        tx = apply_rules(conn, {"description": row["description"], "amount": row["amount"], "source_name": row["source_name"], "transaction_date": row["transaction_date"]})
        if not tx.get("rule_id"):
            if row["rule_id"] == POCKET_CHANGE_RULE_ID:
                # Threshold lowered/disabled: put the row back in the uncategorised pile.
                conn.execute(
                    "UPDATE transactions SET category=NULL, subcategory=NULL, classification=NULL, confidence=0.2, rule_id=NULL WHERE transaction_id=?",
                    (row["transaction_id"],),
                )
                conn.execute("DELETE FROM review_items WHERE transaction_id=? AND status='open'", (row["transaction_id"],))
                add_review_if_needed(conn, row["transaction_id"])
                updated += 1
            continue
        if tx.get("rule_id") == POCKET_CHANGE_RULE_ID and row["category"] and row["rule_id"] != POCKET_CHANGE_RULE_ID:
            continue  # the fallback never overwrites a category that came from the import itself
        new = {
            "category": tx.get("category"),
            "subcategory": tx.get("subcategory"),
            "classification": tx.get("classification"),
            "flow_type": tx.get("flow_type") or row["flow_type"],
            "confidence": tx.get("confidence"),
            "rule_id": tx["rule_id"],
            "merchant_payee": tx.get("merchant_payee") or row["merchant_payee"],
            "notes": tx.get("notes") or row["notes"],
        }
        if all(row[k] == v for k, v in new.items()):
            continue
        conn.execute(
            "UPDATE transactions SET category=?, subcategory=?, classification=?, flow_type=?, confidence=?, rule_id=?, merchant_payee=?, notes=? WHERE transaction_id=?",
            (*new.values(), row["transaction_id"]),
        )
        conn.execute("DELETE FROM review_items WHERE transaction_id=? AND status='open'", (row["transaction_id"],))
        add_review_if_needed(conn, row["transaction_id"])
        updated += 1
    return {"scanned": len(rows), "updated": updated}


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
    fingerprint = stable_hash(
        tx.get("source_name"),
        tx.get("transaction_date"),
        tx.get("amount"),
        normalize_description_for_hash(tx.get("description")),
    )
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


def create_review_item(conn: sqlite3.Connection, transaction_id: str, reason: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO review_items(review_item_id, transaction_id, reason, created_at) VALUES(?,?,?,?)",
        ("review_" + stable_hash(transaction_id, reason), transaction_id, reason, now_iso()),
    )


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
    if any(word in desc for word in ("AMAZON", "FLIPKART", "RAZORPAY", "PAYU", "PAYTM")) and ((tx["confidence"] or 0) < 0.75 or not tx["category"]):
        reasons.append("ambiguous merchant")
    if tx["flow_type"] in ("spend", "fee") and abs(float(tx["amount"] or 0)) >= 25000 and tx["classification"] != "fixed":
        reasons.append("unusual amount")
    if tx["category"] in ("Food", "Groceries & Household", "Lifestyle", "Shopping", "Baby", "Idli", "Transport") and tx["classification"] == "controllable":
        reasons.append("creep-watch driver")
    for reason in reasons:
        create_review_item(conn, transaction_id, reason)


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
        INSERT INTO import_batches(import_batch_id, created_at, source_id, source_name, statement_month, statement_start_date, statement_end_date, file_name, file_type, status, notes)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (batch_id, now_iso(), source_id_for(source_name) or None, source_name, statement_month, statement_start_date, statement_end_date, safe_name, suffix.lstrip(".") or "file", "pending_pdf_extraction", "PDF/file stored for statement parser. Password was not stored."),
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
    rows = [r for r in effective_transactions(conn, month, include_seed) if r["category"] not in HIDDEN_CATEGORIES]
    total_inflow = sum(r["amount"] for r in rows if r["flow_type"] == "income")
    investments = sum(abs(r["amount"]) for r in rows if r["category"] == "Investments" and r["flow_type"] in ("spend", "fee"))
    fixed = sum(abs(r["amount"]) for r in rows if r["classification"] == "fixed" and r["flow_type"] in ("spend", "fee"))
    spend_fees = sum(abs(r["amount"]) for r in rows if r["flow_type"] in ("spend", "fee"))
    refunds = sum(abs(r["amount"]) for r in rows if r["flow_type"] in ("refund", "reversal"))
    total_spend = max(0, spend_fees - refunds)
    other = max(0, total_spend - investments - fixed)
    # Per-category outflow, netted for refunds/reversals so the sum lines up with total_spend
    # (and therefore with the surplus shown in the cash-flow chart).
    categories: dict[str, float] = {}
    for r in rows:
        if r["flow_type"] in ("spend", "fee"):
            sign = 1
        elif r["flow_type"] in ("refund", "reversal"):
            sign = -1
        else:
            continue
        cat = r["category"] or UNCATEGORISED_LABEL
        categories[cat] = categories.get(cat, 0) + sign * abs(r["amount"])
    categories = {cat: max(0, value) for cat, value in categories.items()}
    category_flows = [
        {"name": cat, "value": value, "color": (CATEGORY_STYLE.get(cat) or OTHER_CATEGORY_STYLE)[3]}
        for cat, value in sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))
        if value > 0
    ]
    surplus = max(0, total_inflow - sum(f["value"] for f in category_flows))
    # Money-in by category (Salary, Dividend income, …); uncategorised income is "Other inflow".
    incomes: dict[str, float] = {}
    for r in rows:
        if r["flow_type"] == "income" and r["amount"] > 0:
            key = r["category"] or OTHER_INFLOW_LABEL
            incomes[key] = incomes.get(key, 0) + r["amount"]
    income_flows = [
        {"name": name, "value": value, "color": (CATEGORY_STYLE.get(name) or ("", "", "", "--viz-inflow"))[3], "category": name if name != OTHER_INFLOW_LABEL else UNCATEGORISED_FILTER}
        for name, value in sorted(incomes.items(), key=lambda kv: (-kv[1], kv[0])) if value > 0
    ]
    dividends = incomes.get("Dividend income", 0)
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
            # Expenses exclude investments so inflow ≈ expenses + investments + surplus.
            "total_expenses": max(0, total_spend - investments),
            "dividends": dividends,
            "surplus": surplus,
            "review_count": review_count,
        },
        "categories": categories,
        "category_flows": category_flows,
        "income_flows": income_flows,
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


EFFECTIVE_TX_COLUMNS = """t.transaction_id, t.raw_import_id, t.import_batch_id, t.transaction_date, t.description, t.amount,
       COALESCE(mo.flow_type, t.flow_type) AS flow_type,
       NULLIF(COALESCE(mo.category, t.category), '') AS category,
       NULLIF(COALESCE(mo.subcategory, t.subcategory), '') AS subcategory,
       COALESCE(mo.classification, t.classification) AS classification,
       COALESCE(mo.merchant_payee, t.merchant_payee) AS merchant_payee,
       t.payer, t.source_name, t.confidence, t.rule_id,
       COALESCE(mo.manual_override_id, t.manual_override_id) AS manual_override_id,
       COALESCE(mo.notes, t.notes) AS notes,
       t.fingerprint, t.created_at"""


def effective_tx_sql(month: str | None = None, include_seed: bool = True, extra_where: tuple[str, ...] | list[str] = (), extra_args: tuple | list = ()) -> tuple[str, list]:
    """SQL (no ORDER BY) selecting visible transactions with the latest manual override overlaid.
    Visible = not in a deleted/excluded import batch, and not the seed batch when include_seed is False.
    Every read of transactions should go through this so hidden batches stay hidden everywhere."""
    query = f"""SELECT {EFFECTIVE_TX_COLUMNS}
FROM transactions t
LEFT JOIN import_batches ib ON ib.import_batch_id = t.import_batch_id
LEFT JOIN manual_overrides mo ON mo.manual_override_id = (
    SELECT m2.manual_override_id FROM manual_overrides m2
    WHERE m2.transaction_id = t.transaction_id
    ORDER BY m2.created_at DESC, m2.rowid DESC LIMIT 1)"""
    args: list = []
    clauses = ["(t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))"]
    if month:
        clauses.append("substr(t.transaction_date,1,7)=?")
        args.append(month)
    if not include_seed:
        clauses.append("coalesce(t.import_batch_id, '') != ?")
        args.append(SEED_BATCH_ID)
    clauses.extend(extra_where)
    args.extend(extra_args)
    return query + " WHERE " + " AND ".join(clauses), args


def effective_transactions(conn: sqlite3.Connection, month: str | None = None, include_seed: bool | None = None) -> list[dict]:
    if include_seed is None:
        include_seed = seed_data_enabled(conn)
    query, args = effective_tx_sql(month, include_seed)
    query += " ORDER BY t.transaction_date DESC, t.created_at DESC"
    return [dict(r) for r in conn.execute(query, args).fetchall()]


NO_SUBCATEGORY_FILTER = "__none__"
NO_SUBCATEGORY_LABEL = "(no subcategory)"
TX_PAGE_SIZE = 50
TX_SORTS = {
    "date": "transaction_date {dir}, created_at DESC, transaction_id",
    "amount": "abs(amount) {dir}, transaction_date DESC, transaction_id",
}
TX_FILTER_KEYS = ("category", "subcategory", "source", "payer", "flow", "classification", "q")
BREAKDOWN_FACETS = {"category": ("category", "subcategory"), "account": ("source",), "person": ("payer",)}


def flow_values(raw: str | None) -> list[str]:
    """`flow` is a comma-joined multi-select; unknown values are dropped. Empty (or every flow) means no filter."""
    values = [v.strip() for v in (raw or "").split(",") if v.strip() in FLOW_TYPES]
    seen: list[str] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return [] if len(seen) == len(FLOW_TYPES) else seen


def render_multi_select(name: str, options: list[tuple[str, str]], selected: list[str], all_label: str) -> str:
    """Excel-style multi-select: a button showing the selection, a panel with an all/none master checkbox,
    one checkbox per option, and Apply (writes the joined value into a hidden input and submits the form)."""
    chosen = [v for v, _ in options if v in selected]
    if not chosen:
        text = all_label
    elif len(chosen) <= 2:
        text = ", ".join(label for v, label in options if v in chosen)
    else:
        text = f"{len(chosen)} selected"
    opts = "".join(
        f'<label class="ms-option"><input type="checkbox" value="{html.escape(v, quote=True)}" data-ms-opt{" checked" if v in chosen else ""}><span>{html.escape(label)}</span></label>'
        for v, label in options
    )
    all_state = " checked" if not chosen else ""
    return (f'<div class="ms" data-ms data-all-label="{html.escape(all_label, quote=True)}"><input type="hidden" name="{html.escape(name, quote=True)}" value="{html.escape(",".join(chosen), quote=True)}" data-ms-value>'
            f'<button type="button" class="ms-summary" aria-haspopup="true" aria-expanded="false"><span class="ms-text">{html.escape(text)}</span><i data-lucide="chevron-down"></i></button>'
            f'<div class="ms-panel" hidden><label class="ms-option ms-all"><input type="checkbox" data-ms-all{all_state}><span>{html.escape(all_label)}</span></label><div class="ms-options">{opts}</div>'
            f'<div class="ms-actions"><button type="button" class="ms-apply" data-ms-apply>Apply</button><button type="button" class="secondary" data-ms-clear>Clear</button></div></div></div>')


MULTI_SELECT_SCRIPT = """<script>
(()=>{document.querySelectorAll('[data-ms]').forEach(ms=>{const btn=ms.querySelector('.ms-summary'),text=ms.querySelector('.ms-text'),panel=ms.querySelector('.ms-panel'),all=ms.querySelector('[data-ms-all]'),opts=[...ms.querySelectorAll('[data-ms-opt]')],hidden=ms.querySelector('[data-ms-value]'),form=ms.closest('form'),allLabel=ms.dataset.allLabel;
const chosen=()=>opts.filter(o=>o.checked);const sync=()=>{const c=chosen();all.checked=c.length===0||c.length===opts.length;all.indeterminate=c.length>0&&c.length<opts.length;text.textContent=(c.length===0||c.length===opts.length)?allLabel:c.length<=2?c.map(o=>o.nextElementSibling.textContent).join(', '):c.length+' selected'};
const open=on=>{panel.hidden=!on;btn.setAttribute('aria-expanded',on?'true':'false');ms.classList.toggle('open',on)};
btn.onclick=()=>open(panel.hidden);all.onchange=()=>{opts.forEach(o=>o.checked=all.checked);sync()};opts.forEach(o=>o.onchange=sync);
ms.querySelector('[data-ms-apply]').onclick=()=>{const c=chosen();hidden.value=(c.length===0||c.length===opts.length)?'':c.map(o=>o.value).join(',');open(false);form&&form.submit()};
ms.querySelector('[data-ms-clear]').onclick=()=>{opts.forEach(o=>o.checked=false);sync();hidden.value='';open(false);form&&form.submit()};
document.addEventListener('click',e=>{if(!ms.contains(e.target))open(false)});ms.addEventListener('keydown',e=>{if(e.key==='Escape'){open(false);btn.focus()}});sync()})})();
</script>"""


def tx_filter_clauses(filters: dict) -> tuple[list[str], list]:
    """WHERE clauses over the effective (override-overlaid) columns for the /transactions filters."""
    clauses: list[str] = []
    args: list = []
    category = filters.get("category") or ""
    subcategory = filters.get("subcategory") or ""
    if category == UNCATEGORISED_FILTER:
        clauses.append("coalesce(category,'')=''")
    elif category:
        clauses.append("category=?")
        args.append(category)
    if category and category != UNCATEGORISED_FILTER and subcategory:
        if subcategory == NO_SUBCATEGORY_FILTER:
            clauses.append("coalesce(subcategory,'')=''")
        else:
            clauses.append("subcategory=?")
            args.append(subcategory)
    for key, col in (("source", "source_name"), ("payer", "payer"), ("classification", "classification")):
        if filters.get(key):
            clauses.append(f"coalesce({col},'')=?")
            args.append(filters[key])
    flows = flow_values(filters.get("flow"))
    if flows:
        clauses.append("coalesce(flow_type,'') IN (" + ",".join("?" * len(flows)) + ")")
        args.extend(flows)
    if filters.get("q"):
        clauses.append("instr(lower(coalesce(description,'')||' '||coalesce(merchant_payee,'')||' '||coalesce(notes,'')||' '||coalesce(subcategory,'')), lower(?))>0")
        args.append(filters["q"].strip())
    return clauses, args


def _tx_cte(conn: sqlite3.Connection, month: str | None, filters: dict) -> tuple[str, list, str, list]:
    """(cte_sql, cte_args, where_sql, where_args) for the visible transactions with filters applied."""
    base_sql, base_args = effective_tx_sql(month or None, seed_data_enabled(conn))
    clauses, args = tx_filter_clauses(filters)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return f"WITH eff AS ({base_sql}) ", base_args, where, args


def query_transactions(conn: sqlite3.Connection, month: str | None, filters: dict, sort: str = "date", direction: str = "desc", page: int = 1, per_page: int = TX_PAGE_SIZE) -> dict:
    """Filtered, sorted, paginated transactions plus totals over the whole filtered set."""
    cte, cte_args, where, where_args = _tx_cte(conn, month, filters)
    totals = conn.execute(
        cte + "SELECT count(*) AS n, "
        "coalesce(sum(CASE WHEN flow_type IN ('spend','fee') AND amount<0 THEN abs(amount) END),0) AS money_out, "
        "coalesce(sum(CASE WHEN flow_type IN ('income','refund','reversal') AND amount>0 THEN amount END),0) AS money_in "
        "FROM eff" + where,
        cte_args + where_args,
    ).fetchone()
    total = totals["n"]
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    order = TX_SORTS.get(sort, TX_SORTS["date"]).format(dir="ASC" if direction == "asc" else "DESC")
    rows = conn.execute(
        cte + f"SELECT * FROM eff{where} ORDER BY {order} LIMIT ? OFFSET ?",
        cte_args + where_args + [per_page, (page - 1) * per_page],
    ).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "pages": pages, "per_page": per_page,
            "money_out": totals["money_out"], "money_in": totals["money_in"]}


def tx_present_values(conn: sqlite3.Connection, month: str | None) -> dict[str, list]:
    """Distinct months/categories/subcategories/sources/payers among visible transactions (for filter option lists).
    Months come from all visible transactions; the rest from the given month."""
    all_cte, all_args, _w, _a = _tx_cte(conn, None, {})
    months = [r[0] for r in conn.execute(all_cte + "SELECT DISTINCT substr(transaction_date,1,7) AS m FROM eff ORDER BY m DESC", all_args).fetchall() if r[0]]
    cte, cte_args, _w, _a = _tx_cte(conn, month, {})
    rows = conn.execute(cte + "SELECT DISTINCT category, subcategory, source_name, payer FROM eff", cte_args).fetchall()
    subs: dict[str, list[str]] = {}
    for r in rows:
        if r["category"] and r["subcategory"] and r["subcategory"] not in subs.setdefault(r["category"], []):
            subs[r["category"]].append(r["subcategory"])
    return {
        "months": months,
        "categories": sorted({r["category"] for r in rows if r["category"]}),
        "subcategories": subs,
        "sources": sorted({r["source_name"] for r in rows if r["source_name"]}),
        "payers": sorted({r["payer"] for r in rows if r["payer"]}),
    }


def breakdown(conn: sqlite3.Connection, month: str | None, filters: dict, by: str = "category") -> list[dict]:
    """Money-out breakdown (spend+fee net of refund/reversal, clamped at 0) — same basis as dashboard_data.
    The displayed facet's own filter keys are ignored so the panel keeps listing every row while one is active."""
    facet_keys = BREAKDOWN_FACETS.get(by, BREAKDOWN_FACETS["category"])
    filters = {k: v for k, v in filters.items() if k not in facet_keys}
    cte, cte_args, where, where_args = _tx_cte(conn, month, filters)
    keys = "coalesce(category,''), coalesce(subcategory,'')" if by == "category" else ("coalesce(source_name,'')" if by == "account" else "coalesce(payer,'')")
    where = (where + " AND " if where else " WHERE ") + "flow_type IN ('spend','fee','refund','reversal')"
    where += " AND coalesce(category,'') NOT IN (" + ",".join("?" * len(HIDDEN_CATEGORIES)) + ")"
    where_args = list(where_args) + list(HIDDEN_CATEGORIES)
    sql = (cte + f"SELECT {keys}, "
           "SUM(CASE WHEN flow_type IN ('spend','fee') THEN abs(amount) WHEN flow_type IN ('refund','reversal') THEN -abs(amount) ELSE 0 END) AS net, "
           f"count(*) AS n FROM eff{where} GROUP BY {keys}")
    rows = conn.execute(sql, cte_args + where_args).fetchall()

    def finish(items: list[dict], total: float, share_key: str = "share") -> list[dict]:
        items = [i for i in items if i["value"] > 0]
        items.sort(key=lambda i: (-i["value"], i["name"]))
        for i in items:
            i[share_key] = (i["value"] / total) if total > 0 else 0.0
        return items

    if by != "category":
        label_empty = "Unknown source" if by == "account" else "Unknown"
        items = [{"key": r[0], "name": r[0] or label_empty, "value": max(0.0, r["net"] or 0.0), "count": r["n"]} for r in rows]
        return finish(items, sum(i["value"] for i in items))

    parents: dict[str, dict] = {}
    for r in rows:
        cat, sub = r[0], r[1]
        parent = parents.setdefault(cat, {"key": cat or UNCATEGORISED_FILTER, "name": cat or UNCATEGORISED_LABEL, "net": 0.0, "count": 0, "children": []})
        net = r["net"] or 0.0
        parent["net"] += net
        parent["count"] += r["n"]
        parent["children"].append({"key": sub or NO_SUBCATEGORY_FILTER, "name": sub or NO_SUBCATEGORY_LABEL, "value": max(0.0, net), "count": r["n"]})
    items = []
    for p in parents.values():
        value = max(0.0, p["net"])
        items.append({"key": p["key"], "name": p["name"], "value": value, "count": p["count"], "children": finish(p["children"], value, "share_of_parent")})
    return finish(items, sum(i["value"] for i in items))


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


# Drawer chrome shared by the Review queue and the Transactions edit drawer (same class names, one stylesheet).
DRAWER_CSS = '.review-scrim{position:fixed;inset:0;background:rgba(20,35,30,.3);opacity:0;visibility:hidden;transition:opacity 250ms;z-index:30}.review-scrim.open{opacity:1;visibility:visible}.review-drawer{position:fixed;right:0;top:0;width:420px;max-width:100%;height:100vh;background:var(--surface);box-shadow:var(--shadow-hover);border-radius:var(--radius-xl) 0 0 var(--radius-xl);transform:translateX(105%);visibility:hidden;transition:transform 250ms cubic-bezier(.16,1,.3,1),visibility 250ms;z-index:31;overflow:hidden}.review-drawer.open{transform:translateX(0);visibility:visible}.review-drawer.closing{transition-duration:180ms}.review-drawer form{height:100%;display:flex;flex-direction:column}.drawer-scroll{padding:var(--sp-5);overflow:auto;flex:1}.drawer-head{display:flex;justify-content:space-between;align-items:flex-start;gap:var(--sp-3)}.drawer-amount{font:700 var(--text-display)/1.1 var(--font-num);letter-spacing:-.01em}.evidence{margin-top:var(--sp-5)}.kv{display:flex;justify-content:space-between;gap:var(--sp-4);padding:var(--sp-2) 0}.kv>span,.evidence-label{color:var(--ink-400);font-size:var(--text-label);font-weight:600}.kv strong{font-size:var(--text-body);font-weight:600;text-align:right}.drawer-source{display:flex;align-items:center;gap:var(--sp-2)}.statement-description{background:var(--brand-050);border-radius:var(--radius-sm);padding:var(--sp-3);overflow-wrap:anywhere;white-space:pre-wrap}.evidence-label{margin-top:var(--sp-3)}.why-block{margin-top:var(--sp-5)}.why-block label{color:var(--ink-900)}.why-block .caption{margin:var(--sp-2) 0}.guess-chip{display:inline-flex;border:1px dashed var(--ink-400);border-radius:var(--radius-full);padding:3px 8px;color:var(--ink-600);font-size:var(--text-caption);font-weight:600}.guess-wrap{display:inline-flex;align-items:center;gap:6px;color:var(--ink-600);font-size:var(--text-caption);font-weight:600}.verdict{border-top:1px solid var(--border);margin-top:var(--sp-5);padding-top:var(--sp-4)}.future-split-space{height:12px}.verdict>label:not(:first-of-type){margin-top:var(--sp-4)}.combo-wrap{position:relative}.combo-wrap input{padding-right:32px}.combo-caret{position:absolute;right:10px;top:50%;width:16px;height:16px;margin-top:-8px;color:var(--ink-400);pointer-events:none}.combo-none{border:1px dashed var(--ink-400);border-radius:var(--radius-sm);margin:4px;color:var(--ink-600);font-weight:600}.combo-none svg{width:16px;vertical-align:-3px;margin-right:var(--sp-1)}.remember-note{margin:var(--sp-2) 0 0 46px}.remember-warn{margin:var(--sp-2) 0 0 46px;padding:var(--sp-2) var(--sp-3);border-radius:var(--radius-sm);background:var(--warn-100);color:var(--warn-700)}.combo-options{position:absolute;left:0;right:0;top:calc(100% + 4px);max-height:210px;overflow:auto;background:var(--surface);box-shadow:var(--shadow-hover);border-radius:var(--radius-md);z-index:2;display:none}.combo-options.open{display:block}.combo-option{padding:var(--sp-2) var(--sp-3);cursor:pointer}.combo-option:hover,.combo-option.active{background:var(--brand-050)}.combo-option svg{width:16px;vertical-align:-3px;margin-right:var(--sp-1)}.remember-row{display:flex!important;align-items:center;gap:var(--sp-2);margin-top:var(--sp-4);color:var(--ink-900)!important;cursor:pointer}.switch input{position:absolute;opacity:0}.switch>span{display:block;width:38px;height:22px;padding:2px;border-radius:var(--radius-full);background:var(--ink-400);transition:background 150ms}.switch>span:after{content:"";display:block;width:18px;height:18px;border-radius:50%;background:var(--surface);transition:transform 150ms}.switch input:checked+span{background:var(--brand-700)}.switch input:checked+span:after{transform:translateX(16px)}.remember-copy{margin:var(--sp-2) 0 0 46px}.drawer-actions{position:sticky;bottom:0;padding:var(--sp-3) var(--sp-5);border-top:1px solid var(--border);background:var(--surface);display:flex;flex-wrap:wrap;gap:var(--sp-2)}.exclude-action,.exclude-confirm button{background:transparent;color:var(--ink-600);padding:var(--sp-2)}.exclude-confirm{display:flex;align-items:center;gap:var(--sp-1);color:var(--ink-600)}.exclude-confirm[hidden]{display:none}.form-error{width:100%;margin:0;color:var(--dang-700);font-size:var(--text-caption)}'
DRAWER_MOBILE_CSS = '@media(max-width:767px){.review-drawer{left:0;right:0;top:auto;bottom:0;width:100%;height:85vh;border-radius:var(--radius-xl) var(--radius-xl) 0 0;transform:translateY(105%)}.review-drawer.open{transform:translateY(0)}.review-drawer .sheet-handle{display:block;width:40px;height:4px;background:var(--border);border-radius:var(--radius-full);margin:8px auto -8px}}'


def taxonomy_options(conn: sqlite3.Connection) -> tuple[list[str], dict[str, list[str]]]:
    """Canonical categories/subcategories plus anything actually used by visible transactions."""
    sql, args = effective_tx_sql(None, seed_data_enabled(conn))
    rows = conn.execute(f"WITH eff AS ({sql}) SELECT DISTINCT category, subcategory FROM eff", args).fetchall()
    categories = sorted({*PARENT_CATEGORIES, *INCOME_CATEGORIES, *HIDDEN_CATEGORIES, *[r["category"] for r in rows if r["category"]]})
    subcategories = {c: list(SUBCATEGORIES.get(c, [])) for c in categories}
    for r in rows:
        if r["category"] and r["subcategory"] and r["subcategory"] not in subcategories.setdefault(r["category"], []):
            subcategories[r["category"]].append(r["subcategory"])
    return categories, subcategories


BREAKDOWN_VIEWS = (("category", "Category", "tags"), ("account", "Account", "landmark"), ("person", "Person", "users"))
BREAKDOWN_VISIBLE_ROWS = 8


def render_breakdown_panel(items: list[dict], view: str, filters: dict, tx_url) -> str:
    """Money-out breakdown card: segmented view switch + clickable rows that act as filters on the table."""
    tabs = "".join(
        f'<a role="tab" class="{"active" if v == view else ""}" aria-selected="{"true" if v == view else "false"}" data-view="{v}" href="{html.escape(tx_url(view=v), quote=True)}"><i data-lucide="{icon}"></i><span>{label}</span></a>'
        for v, label, icon in BREAKDOWN_VIEWS
    )
    caption = "Money out only — spend and fees, net of refunds. Same basis as the Dashboard. Click a row to filter the list below."
    if not items:
        body = empty_state("pie-chart", "Nothing to break down — no money-out transactions match these filters.")
        return f'<section class="card breakdown-card" data-breakdown-view="{view}"><div class="bd-head"><div><h2>Breakdown</h2><p class="caption">{caption}</p></div><div class="chart-toggle bd-toggle-group" role="tablist" aria-label="Breakdown by">{tabs}</div></div>{body}</section>'

    def stats(it: dict, share: float, dense: bool = False) -> str:
        pct = f"{share * 100:.0f}%" if share >= 0.005 or share == 0 else "<1%"
        n = it["count"]
        return (f'<span class="bd-count">{n} txn{"s" if n != 1 else ""}</span><span class="bd-amount num">{money(it["value"])}</span>'
                f'<span class="bd-share">{pct}</span><span class="bd-bar"><span class="progress"><span class="progress-fill" style="width:{min(100, share * 100):.1f}%"></span></span></span>')

    rows = []
    if view == "category":
        active_cat, active_sub = filters.get("category") or "", filters.get("subcategory") or ""
        for idx, it in enumerate(items):
            is_active = it["key"] == active_cat
            href = tx_url(category=None, subcategory=None) if is_active and not active_sub else tx_url(category=it["key"], subcategory=None)
            chip = render_category_chip(None if it["key"] == UNCATEGORISED_FILTER else it["name"], link=False)
            kids = it.get("children") or []
            if len(kids) == 1 and kids[0]["key"] == NO_SUBCATEGORY_FILTER:
                kids = []  # a lone "(no subcategory)" bucket adds nothing
            expanded = is_active
            toggle = (f'<button type="button" class="bd-toggle" aria-expanded="{"true" if expanded else "false"}" aria-label="Show subcategories of {html.escape(it["name"], quote=True)}"><i data-lucide="chevron-right"></i></button>'
                      if kids else '<span class="bd-toggle bd-toggle-blank"></span>')
            children = "".join(
                f'<a class="bd-row bd-child{" active" if is_active and c["key"] == active_sub else ""}" href="{html.escape(tx_url(category=None, subcategory=None) if is_active and c["key"] == active_sub else tx_url(category=it["key"], subcategory=c["key"]), quote=True)}" aria-current="{"true" if is_active and c["key"] == active_sub else "false"}">'
                f'<span class="bd-name"><span class="bd-sub-dot"></span>{html.escape(c["name"])}</span>{stats(c, c["share_of_parent"], True)}</a>'
                for c in kids
            )
            rows.append(
                f'<div class="bd-group{" expanded" if expanded else ""}{" bd-overflow" if idx >= BREAKDOWN_VISIBLE_ROWS and not is_active else ""}" data-key="{html.escape(it["key"], quote=True)}">'
                f'<div class="bd-row bd-parent{" active" if is_active else ""}">{toggle}<a class="bd-main" href="{html.escape(href, quote=True)}" aria-current="{"true" if is_active else "false"}"><span class="bd-name">{chip}</span>{stats(it, it["share"])}</a></div>'
                f'<div class="bd-children">{children}</div></div>'
            )
    else:
        key_name = "source" if view == "account" else "payer"
        active = filters.get(key_name) or ""
        for idx, it in enumerate(items):
            is_active = bool(it["key"]) and it["key"] == active
            href = tx_url(**{key_name: None}) if is_active else tx_url(**{key_name: it["key"]})
            if view == "account":
                name = f'<span class="bd-source">{render_bank_logo(it["name"], True, False, source_id_for(it["key"]))}<span>{html.escape(it["name"])}</span></span>'
            else:
                name = f'<span class="bd-person"><i data-lucide="user-round"></i><span>{html.escape(it["name"])}</span></span>'
            rows.append(
                f'<div class="bd-group{" bd-overflow" if idx >= BREAKDOWN_VISIBLE_ROWS and not is_active else ""}" data-key="{html.escape(it["key"], quote=True)}">'
                f'<div class="bd-row bd-parent{" active" if is_active else ""}"><span class="bd-toggle bd-toggle-blank"></span><a class="bd-main" href="{html.escape(href, quote=True)}" aria-current="{"true" if is_active else "false"}"><span class="bd-name">{name}</span>{stats(it, it["share"])}</a></div></div>'
            )
    overflow = sum(1 for r in rows if "bd-overflow" in r)
    more = f'<button type="button" class="bd-more secondary" data-more="{overflow}">Show all {len(items)}</button>' if overflow else ""
    return (f'<section class="card breakdown-card" data-breakdown-view="{view}"><div class="bd-head"><div><h2>Breakdown</h2><p class="caption">{caption}</p></div>'
            f'<div class="chart-toggle bd-toggle-group" role="tablist" aria-label="Breakdown by">{tabs}</div></div><div class="bd-list">{"".join(rows)}</div>{more}</section>')


def render_tx_drawer(items: dict[str, dict], categories: list[str], subcategories: dict[str, list[str]]) -> str:
    """Edit drawer for the Transactions page: click a row to change category / subcategory / classification / note.
    Posts to /review (origin=transactions) which writes a manual override, then reloads so the breakdown refreshes."""
    data_json = json.dumps(items).replace("</", "<\\/")
    category_json = json.dumps(categories).replace("</", "<\\/")
    subcategory_json = json.dumps(subcategories).replace("</", "<\\/")
    class_options = "".join(f'<option value="{html.escape(c, quote=True)}">{html.escape(human_label(c))}</option>' for c in CLASSIFICATIONS)
    return f'''
    <div class="review-scrim" id="review-scrim"></div><aside class="review-drawer tx-drawer" id="review-drawer" aria-hidden="true" aria-labelledby="drawer-amount"><div class="sheet-handle"></div>
      <form id="tx-form" method="post" action="/review"><input type="hidden" name="transaction_id" id="drawer-id"><input type="hidden" name="action" id="drawer-action" value="approve"><input type="hidden" name="origin" value="transactions">
        <div class="drawer-scroll"><div class="drawer-head"><div><div class="drawer-amount" id="drawer-amount"></div><div class="caption" id="drawer-direction"></div></div><button type="button" class="icon-button" id="drawer-close" aria-label="Close"><i data-lucide="x"></i></button></div>
          <section class="evidence"><div class="kv"><span>Source</span><strong class="drawer-source" id="drawer-source"></strong></div><div class="kv"><span>Date</span><strong id="drawer-date"></strong></div><div class="kv"><span>Currently</span><strong id="drawer-current"></strong></div><label class="evidence-label">Statement description</label><div class="statement-description" id="drawer-description"></div></section>
          <section class="verdict"><label for="category-input">Category</label><div class="combo-wrap"><input id="category-input" name="category" autocomplete="off" role="combobox" aria-controls="category-options" aria-expanded="false" placeholder="Search categories"><div class="combo-options" id="category-options" role="listbox"></div><i data-lucide="chevron-down" class="combo-caret"></i></div>
            <label for="subcategory-input">Subcategory</label><div class="combo-wrap"><input id="subcategory-input" name="subcategory" autocomplete="off" role="combobox" aria-controls="subcategory-options" aria-expanded="false" placeholder="Optional — pick or type"><div class="combo-options" id="subcategory-options" role="listbox"></div><i data-lucide="chevron-down" class="combo-caret"></i></div>
            <label for="classification-select">Classification</label><select id="classification-select" name="classification">{class_options}</select>
            <label for="review-note">Note</label><textarea id="review-note" name="notes" rows="3" placeholder="Add a note"></textarea>
            <label class="remember-row"><span class="switch"><input type="checkbox" id="remember-toggle" name="remember" value="yes"><span></span></span><strong>Remember for future matches</strong></label><p class="remember-copy caption" id="remember-copy" hidden></p><p class="remember-warn caption" id="remember-warn" hidden></p><p class="remember-note caption" id="remember-note" hidden>Can’t remember an unknown — this sends the transaction back to Review.</p>
          </section></div>
        <div class="drawer-actions"><button type="submit" class="approve-action" data-action="approve">Save</button><button type="submit" class="secondary" data-action="transfer">Mark as transfer</button><button type="button" class="exclude-action" id="exclude-action">Exclude</button><span class="exclude-confirm" id="exclude-confirm" hidden>Exclude? <button type="submit" data-action="exclude">Yes</button><button type="button" id="exclude-no">No</button></span><p class="form-error" id="review-error" role="alert"></p></div>
      </form></aside>
    <script>
    (()=>{{const items={data_json},categories={category_json},subcategories={subcategory_json},drawer=document.getElementById('review-drawer'),scrim=document.getElementById('review-scrim'),form=document.getElementById('tx-form'),input=document.getElementById('category-input'),options=document.getElementById('category-options'),subInput=document.getElementById('subcategory-input'),subOptions=document.getElementById('subcategory-options'),classSel=document.getElementById('classification-select'),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;let current=null;
      const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
      function renderCombo(inp,box,list,pinNone){{const raw=inp.value.trim(),q=raw.toLowerCase(),isNone=!!pinNone&&q==='uncategorised',exactMatch=isNone||(!!q&&list.some(x=>x.toLowerCase()===q)),matches=(!q||exactMatch)?list:list.filter(x=>x.toLowerCase().includes(q));let out='';if(pinNone&&(!q||exactMatch||'uncategorised'.startsWith(q)))out+=`<div class="combo-option combo-none${{isNone?' active':''}}" role="option" data-value="__none__"><i data-lucide="circle-help"></i>Uncategorised — send back for review</div>`;out+=matches.slice(0,30).map(x=>`<div class="combo-option${{exactMatch&&x.toLowerCase()===q?' active':''}}" role="option" data-value="${{esc(x)}}">${{esc(x)}}</div>`).join('');if(q&&!matches.length&&!isNone&&!out)out+=`<div class="combo-option create" role="option" data-value="${{esc(raw)}}"><i data-lucide="plus"></i>Create '${{esc(raw)}}'</div>`;box.innerHTML=out;box.classList.toggle('open',(!!q||document.activeElement===inp)&&!!box.innerHTML);inp.setAttribute('aria-expanded',box.classList.contains('open'));box.querySelector('.active')?.scrollIntoView({{block:'nearest'}});lucide.createIcons()}}
      const subList=()=>{{const c=input.value.trim();return (c&&subcategories[c])||[]}};
      let conflictTimer,conflictSeq=0;function checkConflicts(){{const warn=document.getElementById('remember-warn'),on=document.getElementById('remember-toggle').checked;clearTimeout(conflictTimer);if(!on||!current){{warn.hidden=true;return}}const seq=++conflictSeq;conflictTimer=setTimeout(async()=>{{try{{const res=await fetch('/api/rule-conflicts?'+new URLSearchParams({{transaction_id:current.id,category:input.value.trim(),subcategory:subInput.value.trim()}})),data=await res.json();if(seq!==conflictSeq)return;const c=data.conflicts||[],diff=c.filter(x=>!x.same_outcome),same=c.filter(x=>x.same_outcome);let msg='';if(diff.length){{const r=diff[0],target=r.category?r.category+(r.subcategory?' › '+r.subcategory:''):'no category';msg=r.remembered?`Replaces your earlier rule (${{target}}) for this merchant.`:`Overrides '${{r.name}}' (${{target}}) for this merchant. That rule keeps applying to other merchants.`}}else if(same.length)msg=`Already covered by '${{same[0].name}}' — nothing new to remember.`;warn.textContent=msg;warn.hidden=!msg}}catch(e){{warn.hidden=true}}}},200)}}
      function updateRemember(){{const on=document.getElementById('remember-toggle').checked,copy=document.getElementById('remember-copy');copy.hidden=!on;copy.innerHTML=`New transactions matching '${{esc(current?.description||'')}}' will be auto-categorized as ${{esc(input.value||'the selected category')}}${{subInput.value.trim()?' › '+esc(subInput.value.trim()):''}}. <a href="/rules">Re-apply rules</a> to update past transactions.`;checkConflicts()}}
      function open(id){{current=items[id];if(!current)return;document.getElementById('drawer-id').value=id;document.getElementById('drawer-amount').textContent=current.amount;document.getElementById('drawer-direction').textContent=current.direction;document.getElementById('drawer-date').textContent=current.date;document.getElementById('drawer-description').textContent=current.description;document.getElementById('drawer-current').innerHTML=current.current_chip||'<span class="muted">Uncategorised</span>';document.getElementById('drawer-source').innerHTML=(document.querySelector(`[data-tx-id="${{CSS.escape(id)}}"] .tx-source-cell`)?.innerHTML||esc(current.source));setNone(false);input.value=current.category||'';subInput.value=current.subcategory||'';classSel.value=current.classification||'controllable';document.getElementById('review-note').value=current.note||'';document.getElementById('remember-toggle').checked=false;document.getElementById('remember-copy').hidden=true;document.getElementById('review-error').textContent='';document.getElementById('exclude-action').hidden=false;document.getElementById('exclude-confirm').hidden=true;if(window.lucide)lucide.createIcons();drawer.classList.remove('closing');drawer.classList.add('open');scrim.classList.add('open');drawer.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';drawer.querySelector('.drawer-scroll').scrollTop=0;setTimeout(()=>document.getElementById('drawer-close').focus(),reduced?0:250)}}
      function close(done){{drawer.classList.add('closing');drawer.classList.remove('open');scrim.classList.remove('open');drawer.setAttribute('aria-hidden','true');document.body.style.overflow='';setTimeout(done||(()=>{{}}),reduced?0:180)}}
      document.querySelectorAll('.tx-row[data-tx-id]').forEach(row=>{{row.onclick=e=>{{if(e.target.closest('a'))return;open(row.dataset.txId)}};row.onkeydown=e=>{{if((e.key==='Enter'||e.key===' ')&&e.target===row){{e.preventDefault();open(row.dataset.txId)}}}}}});scrim.onclick=()=>close();document.getElementById('drawer-close').onclick=()=>close();document.addEventListener('keydown',e=>{{if(e.key==='Escape'&&drawer.classList.contains('open'))close()}});
      let noneMode=false;function setNone(on){{noneMode=on;document.getElementById('drawer-action').value=on?'uncategorise':'approve';if(on)subInput.value='';subInput.disabled=on;const t=document.getElementById('remember-toggle');if(on)t.checked=false;t.disabled=on;document.getElementById('remember-note').hidden=!on;if(on){{document.getElementById('remember-copy').hidden=true;document.getElementById('remember-warn').hidden=true}}}}input.oninput=()=>{{if(noneMode&&input.value.trim().toLowerCase()!=='uncategorised')setNone(false);renderCombo(input,options,categories,true);updateRemember()}};input.onfocus=()=>{{input.select();renderCombo(input,options,categories,true)}};options.onclick=e=>{{const opt=e.target.closest('.combo-option');if(!opt)return;options.classList.remove('open');if(opt.dataset.value==='__none__'){{input.value='Uncategorised';setNone(true);return}}if(noneMode)setNone(false);input.value=opt.dataset.value;if(opt.classList.contains('create')&&!categories.some(x=>x.toLowerCase()===input.value.toLowerCase()))categories.push(input.value);if(subInput.value&&!subList().includes(subInput.value))subInput.value='';updateRemember()}};
      subInput.oninput=()=>{{renderCombo(subInput,subOptions,subList());updateRemember()}};subInput.onfocus=()=>{{subInput.select();renderCombo(subInput,subOptions,subList())}};subOptions.onclick=e=>{{const opt=e.target.closest('.combo-option');if(!opt)return;subInput.value=opt.dataset.value;if(opt.classList.contains('create')){{const c=input.value.trim();if(c)(subcategories[c]=subcategories[c]||[]).push(subInput.value)}}subOptions.classList.remove('open');updateRemember()}};
      document.addEventListener('click',e=>{{if(!e.target.closest('.combo-wrap')){{options.classList.remove('open');subOptions.classList.remove('open')}}}});document.getElementById('remember-toggle').onchange=updateRemember;document.getElementById('exclude-action').onclick=()=>{{document.getElementById('exclude-action').hidden=true;document.getElementById('exclude-confirm').hidden=false}};document.getElementById('exclude-no').onclick=()=>{{document.getElementById('exclude-action').hidden=false;document.getElementById('exclude-confirm').hidden=true}};
      form.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>{{document.getElementById('drawer-action').value=(b.dataset.action==='approve'&&noneMode)?'uncategorise':b.dataset.action}});
      form.onsubmit=async e=>{{e.preventDefault();const action=document.getElementById('drawer-action').value,error=document.getElementById('review-error');error.textContent='';if(action==='approve'&&!input.value.trim()){{error.textContent='Choose a category before saving';input.focus();return}}const id=current.id;try{{const res=await fetch('/review',{{method:'POST',headers:{{'Accept':'application/json','X-Requested-With':'fetch','Content-Type':'application/x-www-form-urlencoded'}},body:new URLSearchParams(new FormData(form))}}),payload=await res.json();if(!res.ok)throw Error(payload.error||'Could not save');close(()=>{{const row=document.querySelector(`[data-tx-id="${{CSS.escape(id)}}"]`);if(row&&payload.row){{row.querySelector('.tx-cat-cell').innerHTML=payload.row.category_chip;row.querySelector('.tx-class-cell').innerHTML=payload.row.classification_chip;row.querySelector('.tx-flow-cell').innerHTML=payload.row.flow_chip;row.classList.add('tx-row-saved');if(window.lucide)lucide.createIcons()}}const n=(payload.superseded||[]).length,msg=payload.action==='uncategorise'?'Sent back for review':payload.rule_id?(n?`Transaction updated · rule saved, replaced ${{n}} earlier rule${{n>1?'s':''}}`:'Transaction updated · rule saved'):'Transaction updated';setTimeout(()=>{{const u=new URL(location.href);u.searchParams.set('toast',msg);location.replace(u)}},reduced?0:450)}})}}catch(err){{error.textContent=err.message}}}};lucide.createIcons()}})();
    </script>'''


TX_PAGE_CSS = """<style>
.tx-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:var(--sp-3);align-items:end;margin:0 0 var(--sp-4)}
.tx-filters label,.tx-filters .tx-field{display:flex;flex-direction:column;gap:var(--sp-1);margin:0;min-width:0}.tx-filters label>span,.tx-filters .tx-field>span{font-size:var(--text-caption);font-weight:600;color:var(--ink-600)}
.ms{position:relative}.ms-summary{width:100%;min-height:38px;display:flex;align-items:center;justify-content:space-between;gap:var(--sp-2);padding:0 var(--sp-3);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-md);color:var(--ink-900);font-size:var(--text-body);font-weight:500;text-align:left}.ms-summary .ms-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ms-summary svg{width:16px;height:16px;color:var(--ink-400);flex:none}.ms.open .ms-summary{border-color:var(--brand-700);box-shadow:0 0 0 3px rgba(18,138,99,.15)}
.ms-panel{position:absolute;left:0;top:calc(100% + 4px);min-width:220px;z-index:6;background:var(--surface);border-radius:var(--radius-md);box-shadow:var(--shadow-hover);padding:var(--sp-2)}.ms-option{display:flex!important;flex-direction:row!important;align-items:center;gap:var(--sp-2);padding:var(--sp-2) var(--sp-2);border-radius:var(--radius-sm);cursor:pointer;font-weight:500!important;color:var(--ink-900)!important}.ms-option:hover{background:var(--brand-050)}.tx-filters .ms-option input{width:16px;height:16px;min-height:0;margin:0;padding:0;accent-color:var(--brand-700);flex:none}.ms-option span{font-size:var(--text-body)!important;font-weight:500!important;color:var(--ink-900)!important}.ms-all{border-bottom:1px solid var(--border);border-radius:0;margin-bottom:var(--sp-1);font-weight:600!important}.ms-options{max-height:340px;overflow:auto}.ms-actions{display:flex;gap:var(--sp-2);padding-top:var(--sp-2);margin-top:var(--sp-1);border-top:1px solid var(--border)}.ms-actions button{min-height:32px;padding:0 var(--sp-3);font-size:var(--text-label)}
.tx-filter-card{overflow:visible}.tx-filter-card h2 .count,.count{color:var(--ink-400);font-weight:600}.tx-sticky-sentinel{height:1px;margin-top:-1px}
.tx-filters-collapse>summary{display:none;list-style:none}.tx-filters-collapse>summary::-webkit-details-marker{display:none}
@media(min-width:768px){.tx-filter-card{position:sticky;top:calc(var(--header-h,72px) + 8px);z-index:5;transition:box-shadow 150ms}.tx-filter-card.stuck{box-shadow:var(--shadow-hover)}.tx-filters-collapse>summary{display:none}}
@media(max-width:767px){.tx-filters-collapse>summary{display:flex;align-items:center;gap:var(--sp-2);min-height:40px;padding:0 var(--sp-2);margin:0 0 var(--sp-3);border-radius:var(--radius-md);background:var(--brand-050);color:var(--brand-700);font-weight:600;cursor:pointer}.tx-filters-collapse>summary svg{width:16px;height:16px}.tx-collapse-caret{margin-left:auto;transition:transform 150ms}.tx-filters-collapse[open] .tx-collapse-caret{transform:rotate(180deg)}.tx-filter-fab{position:fixed;right:16px;bottom:16px;z-index:20;display:inline-flex;align-items:center;gap:6px;min-height:40px;padding:0 var(--sp-4);border-radius:var(--radius-full);background:var(--brand-700);color:#fff;box-shadow:var(--shadow-hover);font-weight:600;text-decoration:none}.tx-filter-fab svg{width:16px;height:16px}}
.tx-filters select,.tx-filters input{min-height:38px;font-size:var(--text-body)}.tx-search{grid-column:span 2}
.tx-filter-actions{display:flex;gap:var(--sp-2);align-items:center}.tx-filter-actions .btn{min-height:38px;display:inline-flex;align-items:center}
.tx-summary{display:flex;flex-wrap:wrap;gap:var(--sp-2) var(--sp-3);align-items:center;color:var(--ink-600);font-size:var(--text-label)}
.tx-summary .num b{color:var(--ink-900)}.filter-chip{background:var(--brand-100);color:var(--brand-700)}.filter-chip:hover{background:var(--dang-100);color:var(--dang-700);text-decoration:none}
.tx-table{min-width:1000px}.tx-table td{vertical-align:middle}.tx-table td.amount{white-space:nowrap}.tx-date,.tx-source{white-space:nowrap;color:var(--ink-600)}.tx-desc{max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tx-row[data-tx-id]{cursor:pointer;transition:background 150ms ease-out}.tx-row[data-tx-id]:hover,.tx-row[data-tx-id]:focus{background:var(--brand-050);outline:none}.tx-row-saved td{background:var(--pos-100)!important;transition:background 250ms}
.tx-sort{display:inline-flex;align-items:center;gap:4px;color:inherit;text-decoration:none}.tx-sort svg{width:14px;height:14px}.tx-sort:hover{color:var(--brand-700)}
.tx-pager{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:var(--sp-2);margin-top:var(--sp-4);color:var(--ink-600);font-size:var(--text-label)}.tx-pages{display:flex;gap:4px;flex-wrap:wrap}.tx-pages a,.tx-pages span{min-width:34px;height:34px;padding:0 var(--sp-2);display:inline-flex;align-items:center;justify-content:center;border-radius:var(--radius-full);text-decoration:none;color:var(--ink-600)}.tx-pages a:hover{background:var(--brand-050)}.tx-pages .current{background:var(--brand-700);color:#fff;font-weight:600}.tx-pages .disabled{opacity:.4}
.section-gap{margin-top:var(--sp-5)}
.breakdown-card .bd-head{display:flex;justify-content:space-between;align-items:flex-start;gap:var(--sp-3);margin-bottom:var(--sp-3)}.bd-head h2{margin:0 0 var(--sp-1)}.bd-head .caption{margin:0;color:var(--ink-600)}
.chart-toggle{display:flex;padding:3px;background:var(--brand-050);border-radius:var(--radius-full);overflow:auto;flex:none}.chart-toggle a{min-height:34px;padding:var(--sp-1) var(--sp-3);border-radius:var(--radius-full);color:var(--ink-600);font-size:var(--text-label);font-weight:600;display:inline-flex;align-items:center;gap:6px;text-decoration:none;white-space:nowrap}.chart-toggle a.active{background:var(--surface);color:var(--brand-700);box-shadow:var(--shadow-card)}.chart-toggle svg{width:16px;height:16px}
.bd-list{display:flex;flex-direction:column}.bd-group.bd-overflow{display:none}.bd-list.show-all .bd-group.bd-overflow{display:block}
.bd-row{display:flex;align-items:center;gap:var(--sp-2);border-top:1px solid var(--border)}.bd-group:first-child>.bd-row{border-top:0}
.bd-main,.bd-child{display:grid;grid-template-columns:minmax(160px,1.6fr) 70px 110px 48px minmax(80px,1fr);align-items:center;gap:var(--sp-3);flex:1;min-width:0;padding:var(--sp-2) var(--sp-2);border-radius:var(--radius-md);color:inherit;text-decoration:none;transition:background 120ms}
.bd-main:hover,.bd-child:hover{background:var(--brand-050)}.bd-parent.active>.bd-main,.bd-child.active{background:var(--brand-050);box-shadow:inset 3px 0 0 var(--brand-700)}
.bd-name{display:flex;align-items:center;gap:var(--sp-2);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}.bd-count{color:var(--ink-400);font-size:var(--text-caption);white-space:nowrap}.bd-amount{font:600 var(--text-body)/1.4 var(--font-num);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.bd-share{color:var(--ink-600);font-size:var(--text-caption);text-align:right;font-variant-numeric:tabular-nums}
.bd-bar{display:block;min-width:0}.bd-bar .progress{display:block;margin:0}.bd-bar .progress-fill{display:block}.bd-toggle{flex:none;width:28px;height:28px;display:grid;place-items:center;border-radius:var(--radius-full);background:transparent;color:var(--ink-400);padding:0}.bd-toggle:hover{background:var(--brand-050);color:var(--brand-700)}.bd-toggle svg{width:16px;height:16px;transition:transform 150ms}.bd-group.expanded>.bd-row .bd-toggle svg{transform:rotate(90deg)}.bd-toggle-blank{pointer-events:none}
.bd-children{display:none;padding-left:36px}.bd-group.expanded>.bd-children{display:block}.bd-child{border-top:1px dashed var(--border);border-radius:0;font-weight:500}.bd-child .bd-name{font-weight:500;color:var(--ink-600)}.bd-sub-dot{width:6px;height:6px;border-radius:50%;background:var(--ink-400);flex:none}.bd-child .progress-fill{background:var(--brand-700);opacity:.55}
.bd-source,.bd-person{display:flex;align-items:center;gap:var(--sp-2);min-width:0}.bd-source .bank-logo.dense{width:24px;height:24px;flex-basis:24px}.bd-person svg{width:18px;height:18px;color:var(--ink-400)}
.bd-more{margin-top:var(--sp-3)}
.tx-drawer .verdict select{width:100%;min-height:38px;margin-top:var(--sp-1)}.tx-drawer #drawer-current{min-width:0}.tx-drawer #drawer-current .chip{vertical-align:middle;white-space:normal;text-align:left;max-width:100%}
@media(max-width:767px){.tx-search{grid-column:span 1}.table-scroll{overflow-x:auto}.bd-head{flex-direction:column}.bd-main,.bd-child{grid-template-columns:minmax(0,1fr) 90px;grid-template-areas:"name amount" "bar bar"}.bd-name{grid-area:name}.bd-amount{grid-area:amount}.bd-count,.bd-share{display:none}.bd-bar{grid-area:bar}.chart-toggle a span{display:none}}
@media(prefers-reduced-motion:reduce){.review-scrim,.review-drawer,.tx-row,.bd-toggle svg,.bd-main,.bd-child{transition:none!important;animation:none!important}}
</style>"""

TX_PAGE_SCRIPT = """<script>
(()=>{const header=document.querySelector('header'),card=document.querySelector('[data-filter-card]'),sentinel=document.querySelector('[data-sticky-sentinel]');const setH=()=>{if(header)document.documentElement.style.setProperty('--header-h',header.offsetHeight+'px')};setH();addEventListener('resize',setH);
if(card&&sentinel&&'IntersectionObserver' in window){new IntersectionObserver(([e])=>card.classList.toggle('stuck',!e.isIntersecting&&matchMedia('(min-width:768px)').matches),{rootMargin:'-'+((header?header.offsetHeight:72)+9)+'px 0px 0px 0px',threshold:0}).observe(sentinel)}
if(card&&matchMedia('(max-width:767px)').matches){const det=card.querySelector('details');if(det&&card.dataset.activeCount==='0')det.open=false;const fab=document.createElement('a');fab.className='tx-filter-fab';fab.href='#';fab.innerHTML='<i data-lucide="sliders-horizontal"></i><span>Filters</span>';fab.onclick=e=>{e.preventDefault();const d=card.querySelector('details');if(d)d.open=true;card.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'})};document.body.appendChild(fab);if(window.lucide)lucide.createIcons();new IntersectionObserver(([e])=>{fab.style.display=e.isIntersecting?'none':'inline-flex'}).observe(card)}
document.querySelectorAll('.bd-toggle:not(.bd-toggle-blank)').forEach(b=>b.onclick=()=>{const g=b.closest('.bd-group'),on=g.classList.toggle('expanded');b.setAttribute('aria-expanded',on?'true':'false')});
const more=document.querySelector('.bd-more');if(more)more.onclick=()=>{document.querySelector('.bd-list').classList.add('show-all');more.remove()};
const sub=document.querySelector('select[name=subcategory]'),cat=document.querySelector('select[name=category]');if(sub&&cat){const sync=()=>{const parent=cat.value;[...sub.options].forEach(o=>{if(!o.dataset.parent)return;const show=!!parent&&parent!=='__none__'&&o.dataset.parent===parent;o.hidden=!show;if(!show&&o.selected)sub.value=''});sub.disabled=!parent||parent==='__none__'};cat.addEventListener('change',()=>{sub.value='';sync()});sync()}})();
</script>"""



def render_review_workbench(items: list[dict], categories: list[str], subcategories: dict[str, list[str]] | None = None, open_id: str = "") -> str:
    rows = "".join(f'''<tr class="review-row" data-review-id="{html.escape(item['id'])}" tabindex="0" role="button" aria-label="Review {html.escape(item['description'])}">
      <td>{html.escape(item['date'])}</td><td><span class="review-source">{render_bank_logo(item['source'], True, False, item['source_id'])}<span>{html.escape(item['source'])}</span></span></td>
      <td>{html.escape(item['description'])}</td><td>{item['reason_chips']}</td><td class="review-amount">{html.escape(item['amount'])}<i data-lucide="chevron-right"></i></td></tr>''' for item in items)
    empty_hidden = " hidden" if items else ""
    data_json = json.dumps({item["id"]: {k: v for k, v in item.items() if k != "reason_chips"} for item in items}).replace("</", "<\\/")
    category_json = json.dumps(categories).replace("</", "<\\/")
    subcategory_json = json.dumps(subcategories or {}).replace("</", "<\\/")
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
            <div class="why-block"><label>Why it’s here</label><div id="drawer-reasons"></div><p class="caption" id="drawer-why"></p><span class="guess-wrap" id="drawer-guess-chip"></span></div></section>
          <section class="verdict"><div class="future-split-space" aria-hidden="true"></div><label for="category-input">Category</label><div class="combo-wrap"><input id="category-input" name="category" autocomplete="off" role="combobox" aria-controls="category-options" aria-expanded="false" placeholder="Search categories"><div class="combo-options" id="category-options" role="listbox"></div><i data-lucide="chevron-down" class="combo-caret"></i></div>
            <label for="subcategory-input">Subcategory</label><div class="combo-wrap"><input id="subcategory-input" name="subcategory" autocomplete="off" role="combobox" aria-controls="subcategory-options" aria-expanded="false" placeholder="Optional — pick or type"><div class="combo-options" id="subcategory-options" role="listbox"></div><i data-lucide="chevron-down" class="combo-caret"></i></div>
            <label for="review-note">Note</label><textarea id="review-note" name="notes" rows="3" placeholder="Add a note — e.g. 'advance for the sofa'"></textarea>
            <label class="remember-row"><span class="switch"><input type="checkbox" id="remember-toggle" name="remember" value="yes"><span></span></span><strong>Remember for future matches</strong></label><p class="remember-copy caption" id="remember-copy" hidden></p><p class="remember-warn caption" id="remember-warn" hidden></p><p class="remember-note caption" id="remember-note" hidden>Can’t remember an unknown — this sends the transaction back to Review.</p>
          </section></div>
        <div class="drawer-actions"><button type="submit" class="approve-action" data-action="approve">Approve</button><button type="submit" class="secondary" data-action="transfer">Mark as transfer</button><button type="button" class="exclude-action" id="exclude-action">Exclude</button><span class="exclude-confirm" id="exclude-confirm" hidden>Exclude? <button type="submit" data-action="exclude">Yes</button><button type="button" id="exclude-no">No</button></span><p class="form-error" id="review-error" role="alert"></p></div>
      </form></aside><canvas id="queue-confetti" aria-hidden="true"></canvas>
    <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/dist/confetti.browser.min.js"></script>
    <style>
      .review-source .bank-logo.dense{{width:24px;height:24px;flex-basis:24px}}.drawer-source .bank-logo.dense{{width:28px;height:28px;flex-basis:28px}}.drawer-skeleton{{display:none;padding:var(--sp-5)}}.review-drawer.details-loading .drawer-skeleton{{display:block}}.review-drawer.details-loading form{{visibility:hidden}}.drawer-sk-amount{{width:120px;height:32px;margin-bottom:var(--sp-5)}}.drawer-sk-pair{{display:flex;justify-content:space-between;gap:var(--sp-4);margin:var(--sp-4) 0}}.drawer-sk-pair .sk-line{{width:55%;margin:0}}
      .review-title{{display:flex;justify-content:space-between}}.review-title h2{{margin-bottom:var(--sp-1)}}.review-title p{{margin:0}}.review-row{{cursor:pointer;transition:background 150ms ease-out}}.review-row:hover,.review-row:focus{{background:var(--brand-050);outline:none}}.review-source{{display:flex;align-items:center;gap:var(--sp-2);min-width:170px}}.reason-chips{{display:flex;flex-wrap:wrap;gap:var(--sp-1)}}.reason-chip{{display:inline-flex;padding:3px 8px;border-radius:var(--radius-full);background:var(--brand-050);color:var(--ink-600);font-size:var(--text-caption);font-weight:600}}.reason-more{{background:var(--border)}}.review-amount{{text-align:right!important;font:600 var(--text-body)/1.5 var(--font-num);white-space:nowrap;color:var(--ink-900)}}.review-amount svg{{width:16px;height:16px;color:var(--ink-400);vertical-align:-3px;margin-left:var(--sp-2);opacity:0;transition:opacity 150ms}}.review-row:hover .review-amount svg,.review-row:focus .review-amount svg{{opacity:1}}
      {DRAWER_CSS}.queue-empty{{padding:var(--sp-7) var(--sp-4);text-align:center}}.empty-icon,.victory-icon{{width:48px;height:48px;margin:0 auto var(--sp-3);display:grid;place-items:center;border-radius:50%;background:var(--brand-100);color:var(--brand-700)}}.queue-empty h2{{font-weight:800;margin-bottom:var(--sp-2)}}.queue-empty p{{color:var(--ink-600);font-size:var(--text-caption);margin:0}}.victory-line.landed{{animation:victory-in 300ms ease-out both}}#queue-confetti{{position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:100;display:none}}.review-row.resolved{{overflow:hidden;animation:row-collapse 250ms ease-out forwards}}.beacon.happy{{animation:beacon-happy 400ms cubic-bezier(.34,1.56,.64,1)}}@keyframes row-collapse{{to{{opacity:0;transform:scaleY(0);height:0}}}}@keyframes victory-in{{from{{opacity:0;transform:scale(.9)}}to{{opacity:1;transform:scale(1)}}}}@keyframes beacon-happy{{50%{{transform:translateY(-3px)}}}}.sheet-handle{{display:none}}
      {DRAWER_MOBILE_CSS}
      @media(prefers-reduced-motion:reduce){{.review-scrim,.review-drawer,.review-row,.victory-line,.beacon{{transition:none!important;animation:none!important}}.review-row.resolved{{display:none}}}}
    </style>
    <script>
    (()=>{{const items={data_json},categories={category_json},subcategories={subcategory_json},drawer=document.getElementById('review-drawer'),scrim=document.getElementById('review-scrim'),form=document.getElementById('review-form'),input=document.getElementById('category-input'),options=document.getElementById('category-options'),subInput=document.getElementById('subcategory-input'),subOptions=document.getElementById('subcategory-options'),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;let current=null,armed=Object.keys(items).length>0,celebrated=false;
      const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
      function renderCombo(inp,box,list,pinNone){{const raw=inp.value.trim(),q=raw.toLowerCase(),isNone=!!pinNone&&q==='uncategorised',exactMatch=isNone||(!!q&&list.some(x=>x.toLowerCase()===q)),matches=(!q||exactMatch)?list:list.filter(x=>x.toLowerCase().includes(q));let out='';if(pinNone&&(!q||exactMatch||'uncategorised'.startsWith(q)))out+=`<div class="combo-option combo-none${{isNone?' active':''}}" role="option" data-value="__none__"><i data-lucide="circle-help"></i>Uncategorised — send back for review</div>`;out+=matches.slice(0,30).map(x=>`<div class="combo-option${{exactMatch&&x.toLowerCase()===q?' active':''}}" role="option" data-value="${{esc(x)}}">${{esc(x)}}</div>`).join('');if(q&&!matches.length&&!isNone&&!out)out+=`<div class="combo-option create" role="option" data-value="${{esc(raw)}}"><i data-lucide="plus"></i>Create '${{esc(raw)}}'</div>`;box.innerHTML=out;box.classList.toggle('open',(!!q||document.activeElement===inp)&&!!box.innerHTML);inp.setAttribute('aria-expanded',box.classList.contains('open'));box.querySelector('.active')?.scrollIntoView({{block:'nearest'}});lucide.createIcons()}}
      function subList(){{const c=input.value.trim();return (c&&subcategories[c])||[]}}
      function renderOptions(){{renderCombo(input,options,categories,true)}}
      function renderSubOptions(){{renderCombo(subInput,subOptions,subList())}}
      let conflictTimer,conflictSeq=0;function checkConflicts(){{const warn=document.getElementById('remember-warn'),on=document.getElementById('remember-toggle').checked;clearTimeout(conflictTimer);if(!on||!current){{warn.hidden=true;return}}const seq=++conflictSeq;conflictTimer=setTimeout(async()=>{{try{{const res=await fetch('/api/rule-conflicts?'+new URLSearchParams({{transaction_id:current.id,category:input.value.trim(),subcategory:subInput.value.trim()}})),data=await res.json();if(seq!==conflictSeq)return;const c=data.conflicts||[],diff=c.filter(x=>!x.same_outcome),same=c.filter(x=>x.same_outcome);let msg='';if(diff.length){{const r=diff[0],target=r.category?r.category+(r.subcategory?' › '+r.subcategory:''):'no category';msg=r.remembered?`Replaces your earlier rule (${{target}}) for this merchant.`:`Overrides '${{r.name}}' (${{target}}) for this merchant. That rule keeps applying to other merchants.`}}else if(same.length)msg=`Already covered by '${{same[0].name}}' — nothing new to remember.`;warn.textContent=msg;warn.hidden=!msg}}catch(e){{warn.hidden=true}}}},200)}}
      function updateRemember(){{const on=document.getElementById('remember-toggle').checked,copy=document.getElementById('remember-copy');copy.hidden=!on;copy.innerHTML=`New transactions matching '${{esc(current?.description||'')}}' will be auto-categorized as ${{esc(input.value||'the selected category')}}${{subInput.value.trim()?' › '+esc(subInput.value.trim()):''}}, with this note attached. <a href="/rules">Re-apply rules</a> to update past transactions.`;checkConflicts()}}
      function open(id){{current=items[id];if(!current)return;document.getElementById('drawer-id').value=id;document.getElementById('drawer-amount').textContent=current.amount;document.getElementById('drawer-direction').textContent=current.direction;document.getElementById('drawer-date').textContent=current.date;document.getElementById('drawer-description').textContent=current.description;document.getElementById('drawer-time-row').hidden=!current.time;document.getElementById('drawer-time').textContent=current.time;document.getElementById('drawer-reasons').innerHTML=current.reasons.map(x=>`<span class="reason-chip">${{esc(x)}}</span>`).join('');document.getElementById('drawer-why').textContent=`Flagged because ${{current.reasons.join(' and ').toLowerCase()}}${{current.guess?' for '+current.guess:''}}.`;const guess=document.getElementById('drawer-guess-chip');guess.hidden=!current.guess;guess.innerHTML=current.guess?'<span>Best guess:</span>'+current.guess_chip:'';if(current.guess&&window.lucide)lucide.createIcons();setNone(false);input.value=current.guess;subInput.value=current.subcategory||'';document.getElementById('review-note').value=current.note;document.getElementById('remember-toggle').checked=false;document.getElementById('remember-copy').hidden=true;document.getElementById('drawer-extra').innerHTML=current.extra.map(x=>`<div class="kv"><span>${{esc(x.label)}}</span><strong>${{esc(x.value)}}</strong></div>`).join('');document.getElementById('drawer-source').innerHTML=(document.querySelector(`[data-review-id="${{CSS.escape(id)}}"] .review-source`)?.innerHTML||esc(current.source));drawer.classList.remove('closing');drawer.classList.add('open');scrim.classList.add('open');drawer.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';setTimeout(()=>document.getElementById('drawer-close').focus(),reduced?0:250)}}
      function close(done){{drawer.classList.add('closing');drawer.classList.remove('open');scrim.classList.remove('open');drawer.setAttribute('aria-hidden','true');document.body.style.overflow='';setTimeout(done||(()=>{{}}),reduced?0:180)}}
      function celebrate(){{if(!armed||celebrated)return;celebrated=true;const empty=document.getElementById('queue-empty'),normal=empty.querySelector('.empty-normal'),victory=empty.querySelector('.victory-line'),lines=['Reviewed like a champ 🏆','Queue zero. Legend.','Nothing left — you did it.','All clear. Go enjoy your money.'],n=Number(sessionStorage.getItem('queueVictoryIndex')||0);sessionStorage.setItem('queueVictoryIndex',(n+1)%lines.length);normal.hidden=true;victory.hidden=false;victory.querySelector('h2').textContent=reduced?'✨ '+lines[n%lines.length]:lines[n%lines.length];victory.classList.toggle('landed',!reduced);if(reduced){{setTimeout(()=>{{victory.hidden=true;normal.hidden=false}},1600);return}}setTimeout(()=>{{const canvas=document.getElementById('queue-confetti');canvas.style.display='block';if(window.confetti){{const fire=confetti.create(canvas,{{resize:true,useWorker:true}}),colors=['#128A63','#58B893','#4E79A7','#8A6FBF','#F2C94C'];for(let i=0;i<10;i++)setTimeout(()=>fire({{particleCount:20,angle:270,spread:55,startVelocity:10,gravity:.65,drift:(i-4.5)*.06,origin:{{x:(i+.5)/10,y:-.04}},colors,shapes:['square','circle'],scalar:.85,ticks:180}}),i*45)}}document.getElementById('coverage-beacon')?.classList.add('happy');setTimeout(()=>document.getElementById('coverage-beacon')?.classList.remove('happy'),400);setTimeout(()=>canvas.remove(),3000)}},700);setTimeout(()=>{{victory.hidden=true;normal.hidden=false}},3500)}}
      document.querySelectorAll('.review-row').forEach(row=>{{row.onclick=()=>open(row.dataset.reviewId);row.onkeydown=e=>{{if(e.key==='Enter'||e.key===' '){{e.preventDefault();open(row.dataset.reviewId)}}}}}});scrim.onclick=()=>close();document.getElementById('drawer-close').onclick=()=>close();document.addEventListener('keydown',e=>{{if(e.key==='Escape'&&drawer.classList.contains('open'))close()}});let noneMode=false;function setNone(on){{noneMode=on;document.getElementById('drawer-action').value=on?'uncategorise':'approve';if(on)subInput.value='';subInput.disabled=on;const t=document.getElementById('remember-toggle');if(on)t.checked=false;t.disabled=on;document.getElementById('remember-note').hidden=!on;if(on){{document.getElementById('remember-copy').hidden=true;document.getElementById('remember-warn').hidden=true}}}}input.oninput=()=>{{if(noneMode&&input.value.trim().toLowerCase()!=='uncategorised')setNone(false);renderOptions();updateRemember()}};input.onfocus=()=>{{input.select();renderOptions()}};options.onclick=e=>{{const opt=e.target.closest('.combo-option');if(!opt)return;options.classList.remove('open');if(opt.dataset.value==='__none__'){{input.value='Uncategorised';setNone(true);return}}if(noneMode)setNone(false);input.value=opt.dataset.value;if(opt.classList.contains('create')&&!categories.some(x=>x.toLowerCase()===input.value.toLowerCase()))categories.push(input.value);if(subInput.value&&!subList().includes(subInput.value))subInput.value='';updateRemember()}};subInput.oninput=()=>{{renderSubOptions();updateRemember()}};subInput.onfocus=()=>{{subInput.select();renderSubOptions()}};subOptions.onclick=e=>{{const opt=e.target.closest('.combo-option');if(!opt)return;subInput.value=opt.dataset.value;if(opt.classList.contains('create')){{const c=input.value.trim();if(c)(subcategories[c]=subcategories[c]||[]).push(subInput.value)}}subOptions.classList.remove('open');updateRemember()}};document.addEventListener('click',e=>{{if(!e.target.closest('.combo-wrap')){{options.classList.remove('open');subOptions.classList.remove('open')}}}});document.getElementById('remember-toggle').onchange=updateRemember;document.getElementById('exclude-action').onclick=()=>{{document.getElementById('exclude-action').hidden=true;document.getElementById('exclude-confirm').hidden=false;if(!document.getElementById('review-note').value)document.getElementById('review-note').placeholder='Why exclude? (optional)'}};document.getElementById('exclude-no').onclick=()=>{{document.getElementById('exclude-action').hidden=false;document.getElementById('exclude-confirm').hidden=true}};
      form.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>{{document.getElementById('drawer-action').value=(b.dataset.action==='approve'&&noneMode)?'uncategorise':b.dataset.action}});form.onsubmit=async e=>{{e.preventDefault();const action=document.getElementById('drawer-action').value,error=document.getElementById('review-error');error.textContent='';if(action==='approve'&&!input.value.trim()){{error.textContent='Choose a category before approving';input.focus();return}}const id=current.id;try{{const res=await fetch('/review',{{method:'POST',headers:{{'Accept':'application/json','X-Requested-With':'fetch','Content-Type':'application/x-www-form-urlencoded'}},body:new URLSearchParams(new FormData(form))}}),payload=await res.json();if(!res.ok)throw Error(payload.error||'Could not resolve item');if(payload.action==='uncategorise'){{close();setTimeout(()=>window.showToast?.('Kept in review — marked as not sure'),300);return}}if((payload.superseded||[]).length)setTimeout(()=>window.showToast?.(`Rule saved — replaced ${{payload.superseded.length}} earlier rule${{payload.superseded.length>1?'s':''}}`),600);close(()=>{{const row=document.querySelector(`[data-review-id="${{CSS.escape(id)}}"]`);row.classList.add('resolved');setTimeout(()=>{{row.remove();delete items[id];const count=document.getElementById('review-count'),from=Number(count.textContent),to=payload.remaining,start=performance.now();function tick(now){{const p=reduced?1:Math.min(1,(now-start)/400);count.textContent=Math.round(from+(to-from)*p);if(p<1)requestAnimationFrame(tick)}}requestAnimationFrame(tick);document.getElementById('review-list').hidden=to===0;document.getElementById('queue-empty').hidden=to!==0;if(to===0)celebrate();else document.getElementById('review-list').focus()}},reduced?0:250)}})}}catch(err){{error.textContent=err.message}}}};lucide.createIcons();const requested={json.dumps(open_id)};if(requested&&items[requested])setTimeout(()=>open(requested),0)}})();
    </script>'''


def render_page(title: str, body: str, authed: bool = True, beacon_month: str | None = None) -> bytes:
    nav = ""
    if authed:
        nav = """
        <nav aria-label="Primary navigation">
          <a href="/" data-page="Varavu.Selavu"><i data-lucide="layout-dashboard"></i>Dashboard</a>
          <a href="/transactions" data-page="Transactions"><i data-lucide="arrow-left-right"></i>Transactions</a>
          <a href="/review" data-page="Review Queue"><i data-lucide="check-check"></i>Review</a>
          <a href="/rules" class="nav-right" data-page="Rules"><i data-lucide="sliders-horizontal"></i>Rules</a>
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
      --viz-inflow:#0B6B4D; --viz-invest:#2AA07C; --viz-fixed:#4E79A7; --viz-other:#8A6FBF; --viz-surplus:#58B893; --viz-shortfall:#C0703A;
      --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px; --sp-6:32px; --sp-7:40px; --sp-8:48px; --sp-9:64px;
      --radius-sm:8px; --radius-md:10px; --radius-lg:16px; --radius-xl:20px; --radius-full:999px;
      --shadow-card:0 1px 2px rgba(20,35,30,.05),0 2px 8px rgba(20,35,30,.06); --shadow-hover:0 4px 12px rgba(20,35,30,.08),0 8px 24px rgba(20,35,30,.08);
    }}
    * {{ box-sizing:border-box; }}
    html {{ -webkit-text-size-adjust:100%; overflow-x:hidden; }}
    body {{ margin:0; background:var(--page-bg); color:var(--ink-900); font:400 var(--text-body)/1.5 var(--font-ui); }} /* no overflow-x on body: it would make body the scroll container and break position:sticky (header, filter card) */
    header {{ padding:var(--sp-4) max(var(--sp-5),calc((100vw - 1240px)/2 + var(--sp-5))); border-bottom:1px solid var(--border); background:var(--surface); position:sticky; top:0; z-index:3; }}
    .brand {{ display:flex; align-items:center; justify-content:space-between; gap:var(--sp-4); }} .brand-actions {{ display:flex;align-items:center;gap:var(--sp-2); }}
    h1 {{ font-size:var(--text-h1); line-height:1.2; font-weight:800; margin:0; letter-spacing:-.01em; }}
    h2 {{ font-size:var(--text-h2); line-height:1.25; font-weight:700; margin:0 0 var(--sp-4); letter-spacing:-.01em; }}
    nav {{ display:flex; gap:var(--sp-1); flex-wrap:wrap; margin-top:var(--sp-3); }}
    nav a {{ display:inline-flex; align-items:center; gap:var(--sp-2); text-decoration:none; color:var(--ink-600); padding:var(--sp-2) var(--sp-3); border-radius:var(--radius-md); font-size:var(--text-label); font-weight:600; transition:background 150ms ease-out,color 150ms ease-out; }}
    nav a:hover {{ background:var(--brand-050); color:var(--brand-700); }} nav a.active {{ background:var(--brand-100); color:var(--brand-700); }}
    nav a.nav-right {{ margin-left:auto; }}
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
    .chip {{ display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:var(--radius-full); font-size:var(--text-label); line-height:1.4; font-weight:600; white-space:nowrap; text-decoration:none; vertical-align:middle; }}
    .chip svg {{ width:14px; height:14px; flex:none; }} .chip .chip-sub {{ font-weight:500; opacity:.8; }}
    a.cat-chip:hover, a.cat-chip:focus-visible {{ filter:brightness(.95); text-decoration:none; }}
    {CATEGORY_CHIP_CSS}
    .cat-none {{ background:transparent; color:var(--ink-600); border:1px dashed var(--ink-400); }}
    .kind-chip {{ background:var(--page-bg); color:var(--ink-600); font-weight:500; }}
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
  <header><div class="brand"><h1>Varavu.Selavu</h1><div class="brand-actions">{beacon}{admin}<span class="pill blue">Varavu.Selavu v1</span></div></div>{nav}</header>
  <div class="page-skeleton" id="page-skeleton" aria-hidden="true" data-skeleton-page="{html.escape(title)}"><div class="skeleton-kpis">{''.join('<div class="skeleton-card"><div class="skeleton-block sk-label"></div><div class="skeleton-block sk-value"></div></div>' for _ in range(4))}</div><div class="skeleton-layout"><div class="skeleton-block sk-chart"></div><div class="sk-panel"><div class="skeleton-block sk-line short"></div>{''.join('<div class="skeleton-block sk-line medium"></div>' for _ in range(5))}</div></div><div class="skeleton-form"><div class="sk-form-row">{''.join('<div class="sk-field"><div class="skeleton-block sk-label"></div><div class="skeleton-block sk-input"></div></div>' for _ in range(3))}</div></div><div class="skeleton-table"><div class="skeleton-block sk-line short"></div>{''.join('<div class="sk-table-row">'+''.join('<div class="skeleton-block"></div>' for _ in range(5))+'</div>' for _ in range(5))}</div></div>
  {garden_overlay}<main>{body}</main><div class="app-toast" id="app-toast" role="status" aria-live="polite"><i data-lucide="check-circle-2"></i><span></span></div>
  <script>(()=>{{const REDUCED=matchMedia('(prefers-reduced-motion: reduce)').matches,SKELETON_DELAY=150,SKELETON_MIN=300,PAGE_FADE=150;const main=document.querySelector('main');main.classList.add('page-ready');let toastTimer;window.showToast=(message,icon='check-circle-2',duration=2500)=>{{const toast=document.getElementById('app-toast');toast.querySelector('span').textContent=message;toast.querySelector('i,svg')?.setAttribute('data-lucide',icon);toast.classList.add('show');if(window.lucide)lucide.createIcons();clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.classList.remove('show'),duration)}};const params=new URLSearchParams(location.search),toastMessage=params.get('toast');if(toastMessage)showToast(toastMessage,'check-circle-2',Number(params.get('toast_ms'))||2500);document.querySelectorAll('[data-toast-on-load]').forEach(el=>showToast(el.dataset.toastOnLoad,el.dataset.toastIcon||'check-circle-2',Number(el.dataset.toastMs)||2500));function scheduleSkeleton(){{let shown=0;setTimeout(()=>{{shown=performance.now();document.body.classList.add('loading-skeleton');setTimeout(()=>document.body.classList.remove('loading-skeleton'),3000)}},SKELETON_DELAY);window.addEventListener('pageshow',()=>{{if(shown)setTimeout(()=>document.body.classList.remove('loading-skeleton'),Math.max(0,SKELETON_MIN-(performance.now()-shown)))}} ,{{once:true}})}}document.addEventListener('click',e=>{{const a=e.target.closest('a[href]');if(a&&a.origin===location.origin&&!a.hasAttribute('download')&&!a.href.includes('#'))scheduleSkeleton()}});document.addEventListener('submit',e=>{{if(!e.defaultPrevented)scheduleSkeleton()}});document.querySelectorAll('tbody tr,.logo-row,.garden-row').forEach(el=>el.classList.add('list-transition-item'));document.querySelectorAll('nav a[data-page]').forEach(a=>a.classList.toggle('active',a.dataset.page==={json.dumps(title)})); if(window.lucide) lucide.createIcons({{attrs:{{'stroke-width':1.75}}}}); const beacon=document.getElementById('coverage-beacon'),garden=document.getElementById('account-garden'),scrim=document.getElementById('garden-scrim'); function closeGarden(){{if(!garden)return;garden.classList.remove('open');scrim.classList.remove('open');garden.setAttribute('aria-hidden','true');beacon.setAttribute('aria-expanded','false')}} if(beacon){{beacon.addEventListener('click',e=>{{e.stopPropagation();const opening=!garden.classList.contains('open');closeGarden();if(opening){{garden.classList.add('open');scrim.classList.add('open');garden.setAttribute('aria-hidden','false');beacon.setAttribute('aria-expanded','true')}}}});document.getElementById('garden-close').addEventListener('click',closeGarden);scrim.addEventListener('click',closeGarden);document.addEventListener('click',e=>{{if(!e.target.closest('.beacon-wrap'))closeGarden()}});document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeGarden()}})}}}})();</script>
  <script>(()=>{{const nav=performance.getEntriesByType('navigation')[0];if(!nav||nav.responseEnd-nav.startTime<=150)return;const started=performance.now(),content=document.querySelector('main');document.body.classList.add('loading-skeleton');setTimeout(()=>{{document.body.classList.remove('loading-skeleton');content.classList.remove('page-ready');void content.offsetWidth;content.classList.add('page-ready')}},Math.max(0,300-(performance.now()-started)))}})();</script>
  <script>(()=>{{const beacon=document.getElementById('coverage-beacon'),panel=document.getElementById('account-garden');if(!beacon||!panel)return;const position=()=>{{if(matchMedia('(max-width:767px)').matches){{panel.style.removeProperty('right');panel.style.removeProperty('top');panel.style.removeProperty('max-height');return}}const rect=beacon.getBoundingClientRect(),top=Math.min(rect.bottom+8,innerHeight-16);panel.style.right=Math.max(16,innerWidth-rect.right)+'px';panel.style.top=top+'px';panel.style.maxHeight=Math.max(120,innerHeight-top-16)+'px'}};beacon.addEventListener('click',position,true);panel.addEventListener('click',event=>event.stopPropagation());addEventListener('resize',position);addEventListener('scroll',()=>{{if(panel.classList.contains('open'))position()}},true)}})();</script>
  <script>(()=>{{const brief32Fetch=window.fetch.bind(window);window.fetch=async(...args)=>{{const response=await brief32Fetch(...args);const target=String(args[0]);if(response.ok&&target==='/review')window.showToast?.(String(args[1]?.body||'').includes('origin=transactions')?'Transaction updated':'Transaction approved');return response}}}})();</script>
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
            "/import/status": self.import_status,
            "/admin/logo": self.admin_logo,
            "/admin/password": self.admin_password,
            "/admin/seed-data": self.admin_seed_data,
            "/admin/pocket-change": self.admin_pocket_change,
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
        flows_json = json.dumps(data["category_flows"]).replace("</", "<\\/")
        income_json = json.dumps(data["income_flows"]).replace("</", "<\\/")
        ratio = surplus / s["total_inflow"] if s["total_inflow"] else (-1 if surplus < 0 else 0)
        mood = "healthy" if ratio >= .15 else "tight" if ratio >= 0 else "negative"
        def count_value(value, index):
            return f'<div class="value" data-count-value="{float(value):.2f}" data-count-index="{index}">{money(value)}</div>'
        def creep_row(c):
            over = c["variance"] > 0
            progress = min(100, max(0, (c["actual"] / c["planned"] * 100) if c["planned"] else 0))
            label = html.escape(c["category"]) + (" / " + html.escape(c["subcategory"]) if c["subcategory"] else "")
            chip = render_category_chip(c["category"], c["subcategory"], month=month)
            return (f'<div class="creep-row {"over" if over else "within"}"><div class="creep-top"><span class="creep-name">{chip}</span><span class="pill creep-status">{"Over" if over else "Within"}</span></div>'
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
          <div class="card metric"><div class="metric-label"><span class="metric-icon"><i data-lucide="receipt"></i></span>Total expenses</div>{count_value(s['total_expenses'],1)}</div>
          <div class="card metric"><div class="metric-label"><span class="metric-icon"><i data-lucide="sprout"></i></span>Investments</div>{count_value(s['investments'],2)}</div>
          <div class="card metric"><div class="metric-label"><span class="metric-icon"><i data-lucide="coins"></i></span>Total dividends received</div>{count_value(s['dividends'],3)}</div>
        </section>
        <section class="grid two section-gap dashboard-content"><div class="card cash-card" id="cash-card"><div class="cash-head"><h2>Cash flow</h2><div class="cash-tools"><div class="chart-toggle" role="tablist" aria-label="Cash flow view">
          <button class="active" data-view="sankey" title="Sankey"><i data-lucide="git-branch"></i><span>Sankey</span></button><button data-view="waterfall" title="Waterfall"><i data-lucide="bar-chart-3"></i><span>Waterfall</span></button><button data-view="treemap" title="Treemap"><i data-lucide="layout-grid"></i><span>Treemap</span></button><button data-view="bars" title="Bars"><i data-lucide="align-left"></i><span>Bars</span></button></div><button type="button" class="chart-expand" id="chart-expand" title="Expand chart" aria-label="Expand chart" aria-pressed="false"><i data-lucide="maximize-2"></i></button></div></div><div id="cash-chart" class="chart" data-default-view="sankey"></div></div>
          <div class="card"><h2>Creep Watch</h2><div class="creep-list">{creep_rows or '<p class="muted">No active baselines for this month.</p>'}</div></div></section>
        <section class="card section-gap dashboard-content review-preview"><h2>Review Queue</h2><p class="muted"><span class="num">{s['review_count']}</span> to review</p><div class="table-scroll"><table><thead><tr><th>Date</th><th>Source</th><th>Description</th><th>Reason</th><th class="right">Amount</th></tr></thead><tbody>{review_rows}</tbody></table></div></section>
        <style>.review-preview-row{{cursor:pointer;transition:background 150ms ease-out}}.review-preview-row:hover,.review-preview-row:focus{{background:var(--brand-050);outline:none}}.review-source{{display:flex;align-items:center;gap:var(--sp-2);min-width:170px}}.reason-chips{{display:flex;gap:var(--sp-1);flex-wrap:wrap}}.reason-chip{{display:inline-flex;padding:3px 8px;border-radius:var(--radius-full);background:var(--brand-050);color:var(--ink-600);font-size:var(--text-caption);font-weight:600}}.reason-more{{background:var(--border)}}.review-amount{{text-align:right;font:600 var(--text-body)/1.5 var(--font-num);white-space:nowrap;color:var(--ink-900)}}.review-amount svg{{width:16px;height:16px;color:var(--ink-400);vertical-align:-3px;margin-left:var(--sp-2);opacity:0;transition:opacity 150ms}}.review-preview-row:hover .review-amount svg,.review-preview-row:focus .review-amount svg{{opacity:1}}</style>'''
        body = f'''
        <style>
          .hero {{ min-height:96px;padding:var(--sp-5);border-radius:var(--radius-lg);display:flex;align-items:center;justify-content:space-between;gap:var(--sp-5);background-size:200% 200%;animation:hero-breathe 16s ease-in-out infinite alternate; }} .hero.healthy{{background-image:linear-gradient(135deg,#DFF1E8,#EAF6EE)}} .hero.tight{{background-image:linear-gradient(135deg,#FCEFD6,#FaF3E4)}} .hero.negative{{background-image:linear-gradient(135deg,#FBE6E3,#F9EFEA)}} @keyframes hero-breathe{{from{{background-position:0 0}}to{{background-position:100% 100%}}}}
          .greeting {{ font-size:var(--text-h2);font-weight:800;margin:0 0 var(--sp-1); }} .month-nav,.month-core {{ display:flex;align-items:center;gap:var(--sp-1); }} .month-nav {{ position:relative;gap:var(--sp-2); }} .month-label {{ min-width:140px;background:transparent;color:var(--ink-900);padding:var(--sp-2);font-size:var(--text-h2);font-weight:700; }} .month-label:hover{{background:var(--brand-050)}} .today-button{{background:transparent;color:var(--brand-700);font-size:var(--text-label);padding:var(--sp-2);}} .today-button:hover{{background:var(--brand-050)}} .month-popover{{position:absolute;right:0;top:44px;width:292px;padding:var(--sp-4);background:var(--surface);border-radius:var(--radius-xl);box-shadow:var(--shadow-hover);z-index:10;display:none}} .month-popover.open{{display:block}} .year-stepper{{display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--sp-3)}} .month-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--sp-1)}} .month-grid button{{background:transparent;color:var(--ink-600);min-height:36px;padding:var(--sp-1)}} .month-grid button:hover,.month-grid button.selected{{background:var(--brand-100);color:var(--brand-700)}} .month-grid button:disabled{{opacity:.4;cursor:not-allowed}}
          .cash-head{{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3);margin-bottom:var(--sp-4)}} .cash-head h2{{margin:0}} .chart-toggle{{display:flex;padding:3px;background:var(--brand-050);border-radius:var(--radius-full);overflow:auto}} .chart-toggle button{{min-height:34px;padding:var(--sp-1) var(--sp-3);border-radius:var(--radius-full);background:transparent;color:var(--ink-600);font-size:var(--text-label)}} .chart-toggle button.active{{background:var(--surface);color:var(--brand-700);box-shadow:var(--shadow-card)}} .chart-toggle svg{{width:16px;height:16px}} .cash-tools{{display:flex;align-items:center;gap:var(--sp-2);min-width:0}} .chart-expand{{flex:none;width:36px;height:36px;display:grid;place-items:center;border-radius:var(--radius-full);background:var(--brand-050);color:var(--ink-600);transition:background 120ms ease,color 120ms ease}} .chart-expand:hover,.chart-expand[aria-pressed="true"]{{background:var(--surface);color:var(--brand-700);box-shadow:var(--shadow-card)}} .chart-expand svg{{width:16px;height:16px}} .cash-card.expanded{{position:fixed;inset:0;z-index:60;border-radius:0;padding:var(--sp-5);display:flex;flex-direction:column;overflow:auto;box-shadow:none}} .cash-card.expanded .cash-head{{flex:none}} .cash-card.expanded #cash-chart{{flex:1;min-height:0}} body.chart-expanded{{overflow:hidden}} #cash-chart.switching{{opacity:0;transform:translateY(4px)}} #cash-chart{{transition:opacity 150ms ease,transform 150ms ease}} .chart-bar-label{{font:500 12px var(--font-ui);fill:var(--ink-600)}} .chart-amount{{font:600 12px var(--font-num);fill:var(--ink-900);font-variant-numeric:tabular-nums}} .treemap-label{{fill:white;font:600 12.5px var(--font-ui)}} .treemap-amount{{fill:white;font:600 12px var(--font-num)}} .treemap-percent{{fill:rgba(255,255,255,.72);font:500 12px var(--font-ui)}} .treemap-tile{{transition:filter 120ms ease}} .treemap-tile:hover{{filter:brightness(1.06)}} .treemap-legend{{display:flex;gap:var(--sp-2);flex-wrap:wrap;padding-top:var(--sp-3)}} .treemap-chip{{display:inline-flex;align-items:center;gap:var(--sp-2);font-size:var(--text-caption);color:var(--ink-600);text-decoration:none}} a.treemap-chip:hover{{color:var(--brand-700)}} .inflow-legend{{padding-top:var(--sp-2)}} .inflow-legend[hidden]{{display:none}} .inflow-legend b{{color:var(--ink-900)}} .treemap-swatch{{width:10px;height:10px;border-radius:2px}} .empty-state{{text-align:center;padding:var(--sp-8);margin-top:var(--sp-5)}} .empty-icon{{width:48px;height:48px;border-radius:50%;display:grid;place-items:center;margin:0 auto var(--sp-3);background:var(--brand-050);color:var(--brand-700)}} .empty-icon svg{{width:24px}}
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
          const picker=document.getElementById('month-picker'),label=document.getElementById('month-label'),yearEl=document.getElementById('picker-year');let pickerYear=+month.slice(0,4);label.onclick=e=>{{e.stopPropagation();picker.classList.toggle('open')}};document.addEventListener('click',e=>{{if(!e.target.closest('.month-nav'))picker.classList.remove('open')}});function updatePicker(){{yearEl.textContent=pickerYear;document.querySelectorAll('[data-pick-month]').forEach(b=>{{const value=pickerYear+'-'+b.dataset.pickMonth;b.disabled=value<earliest||value>current;b.classList.toggle('selected',value===month);b.onclick=()=>openTx(value)}})}}document.getElementById('year-prev').onclick=()=>{{pickerYear--;updatePicker()}};document.getElementById('year-next').onclick=()=>{{pickerYear++;updatePicker()}};updatePicker();
          const hour=new Date().getHours(),day=Math.floor((new Date()-new Date(new Date().getFullYear(),0,0))/86400000);const lines=hour>=5&&hour<12?['Rise and reconcile, Vignesh ☀️','Fresh coffee, fresh numbers.','Morning, Vignesh — the rupees kept busy overnight.']:hour<17&&hour>=12?['Midday money check — nice.','Back so soon? The spreadsheets missed you.','Afternoon, Vignesh. Let’s see what’s cooking.']:hour<22&&hour>=17?['Evening, Vignesh — let’s tuck the money in.','The markets rest. Your dashboard doesn’t.','Good evening — your rupees have been counted and accounted.']:['Burning the midnight ₹, are we?','Late-night ledger vibes.','The night shift auditor has arrived.'];document.getElementById('greeting').textContent=lines[day%lines.length];
          document.querySelectorAll('[data-count-value]').forEach((el,i)=>{{const end=+el.dataset.countValue,delay=el.dataset.countIndex?+el.dataset.countIndex*60:0;if(reduced){{el.textContent='₹'+nf.format(Math.round(end));return}}el.textContent='₹0';setTimeout(()=>{{const start=performance.now();function frame(now){{const t=Math.min(1,(now-start)/400),ease=1-Math.pow(1-t,4);el.textContent='₹'+nf.format(Math.round(end*ease));if(t<1)requestAnimationFrame(frame)}}requestAnimationFrame(frame)}},delay)}});const fillProgress=()=>document.querySelectorAll('[data-progress]').forEach(el=>el.style.width=el.dataset.progress+'%');reduced?fillProgress():requestAnimationFrame(fillProgress);
          const chart=document.getElementById('cash-chart');if(!chart)return;const card=document.getElementById('cash-card'),flows={flows_json},values=[...flows,{{name:'Surplus',value:{s['surplus']},color:'--viz-surplus'}}],incomes={income_json},MONTH={json.dumps(month)},UNCAT={json.dumps(UNCATEGORISED_LABEL)},catKey=n=>n===UNCAT?'__none__':n,linkTo=(category,flow)=>'/transactions?'+new URLSearchParams(flow?{{month:MONTH,category,flow}}:{{month:MONTH,category}}),openTx=(category,flow)=>{{if(category)location.href=linkTo(category,flow)}},inflow={s['total_inflow']},styles=getComputedStyle(document.documentElement),color=v=>v&&v.startsWith('--')?styles.getPropertyValue(v).trim():(v||styles.getPropertyValue('--ink-400').trim()),fmt=v=>'₹'+nf.format(Math.round(v)),expanded=()=>card.classList.contains('expanded'),outflow=flows.reduce((a,v)=>a+v.value,0),base=Math.max(1,inflow,outflow),baseLabel=inflow>=outflow?'of inflow':'of outflow',pct=v=>(v/base*100).toFixed(1)+'% '+baseLabel;let graph;
          function baseH(){{if(expanded())return Math.max(300,chart.clientHeight-8);return matchMedia('(max-width:560px)').matches?340:420}}
          function svgBase(minH){{chart.innerHTML='';const w=Math.max(300,chart.clientWidth||700),h=Math.max(baseH(),minH||0);return [d3.select(chart).append('svg').attr('width',w).attr('height',h),w,h]}}
          function sankey(){{if(!graph||!window.d3||!d3.sankey){{chart.innerHTML='<div class="chart-fallback">Cash flow could not render. Refresh once to retry.</div>';return}}const rightN=Math.max(1,graph.nodes.length-1),minH=rightN*(expanded()?26:22)+16,[svg,w,h]=svgBase(minH),pad=Math.max(6,Math.min(14,(h-16)/rightN-10)),layout=d3.sankey().nodeId(d=>d.name).nodeSort(null).nodeWidth(16).nodePadding(pad).extent([[8,8],[w-8,h-8]]),d=layout({{nodes:graph.nodes.map(x=>({{...x}})),links:graph.links.map(x=>({{...x}}))}}),nodeColor=x=>color(x.cssVar||x.color||'--ink-400');svg.append('g').selectAll('path').data(d.links).join('path').attr('d',d3.sankeyLinkHorizontal()).attr('stroke',x=>nodeColor(x.target)).attr('stroke-width',x=>Math.max(1,x.width)).attr('fill','none').attr('opacity',reduced ? .28 : 0).transition().duration(reduced?0:350).attr('opacity',.28);const n=svg.append('g').selectAll('g').data(d.nodes).join('g');n.append('rect').attr('x',x=>x.x0).attr('y',x=>x.y0).attr('height',x=>Math.max(1,x.y1-x.y0)).attr('width',x=>x.x1-x.x0).attr('rx',4).attr('fill',nodeColor);n.append('title').text(x=>x.name+' · '+fmt(x.value)+(x.sourceLinks&&x.sourceLinks.length?'':' ('+pct(x.value)+')')+((x.kind==='income'||x.kind==='spend')&&x.category?' · click to see transactions':''));n.filter(x=>(x.kind==='income'||x.kind==='spend')&&x.category).style('cursor','pointer').on('click',(e,x)=>openTx(x.category,x.kind==='income'?'income':null));n.append('text').attr('x',x=>x.x0<w/2?x.x1+7:x.x0-7).attr('y',x=>(x.y0+x.y1)/2).attr('dy','.35em').attr('text-anchor',x=>x.x0<w/2?'start':'end').attr('class','chart-bar-label').text(x=>x.name+' · '+fmt(x.value))}}
          function waterfall(){{const [svg,w,h]=svgBase(),spends=flows.filter(v=>v.value>0),incomeItems=(incomes.length?incomes:[{{name:'Total inflow',value:inflow,color:'--viz-inflow',category:null}}]).map(v=>({{...v,kind:'income'}})),items=[...incomeItems,...spends.map(v=>({{...v,value:-v.value}})),{{name:'Surplus',value:{s['surplus']},color:'--viz-surplus'}}],crowded=items.length>6&&(w/items.length)<110,footer=crowded?92:55,max=Math.max(1,inflow,...spends.map(v=>v.value)),bw=Math.min(72,(w-50)/items.length*.55),x=d3.scaleBand().domain(d3.range(items.length)).range([32,w-16]).padding(.3),y=d3.scaleLinear().domain([0,max]).range([h-footer,18]),short=n=>n.length>14?n.slice(0,13)+'…':n;let running=inflow,cum=0;items.forEach((d,i)=>{{let top,bottom;if(d.kind==='income'){{bottom=cum;cum+=d.value;top=cum}}else if(i===items.length-1){{top=d.value;bottom=0}}else{{top=running;running+=d.value;bottom=Math.max(0,running)}}const rect=svg.append('rect').attr('x',x(i)+(x.bandwidth()-bw)/2).attr('width',bw).attr('y',reduced?y(top):y(bottom)).attr('height',reduced?Math.abs(y(bottom)-y(top)):0).attr('rx',6).attr('fill',color(d.color));rect.append('title').text(d.name+' · '+(d.value<0?'−':'')+fmt(Math.abs(d.value))+(d.name==='Surplus'||(d.kind==='income'&&!d.category)?'':' · click to see transactions'));if(d.kind==='income'&&d.category)rect.style('cursor','pointer').on('click',()=>openTx(d.category,'income'));else if(!d.kind&&d.name!=='Surplus')rect.style('cursor','pointer').on('click',()=>openTx(catKey(d.name)));rect.transition().delay(reduced?0:i*60).duration(reduced?0:280).attr('y',y(top)).attr('height',Math.abs(y(bottom)-y(top)));const cx=x(i)+x.bandwidth()/2;if(crowded){{svg.append('text').attr('transform',`translate(${{cx}},${{h-footer+14}}) rotate(-45)`).attr('text-anchor','end').attr('class','chart-bar-label').text(short(d.name)).append('title').text(d.name);if(x.bandwidth()>=68||i===0||i===items.length-1)svg.append('text').attr('x',cx).attr('y',y(top)-6).attr('text-anchor','middle').attr('class','chart-amount').text((d.value<0?'−':'')+fmt(Math.abs(d.value)))}}else{{svg.append('text').attr('x',cx).attr('y',h-30).attr('text-anchor','middle').attr('class','chart-bar-label').text(short(d.name)).append('title').text(d.name);svg.append('text').attr('x',cx).attr('y',h-12).attr('text-anchor','middle').attr('class','chart-amount').text((d.value<0?'−':'')+fmt(Math.abs(d.value)))}}if(i>0&&i<items.length-1)svg.append('line').attr('x1',x(i-1)+x.bandwidth()).attr('x2',x(i)).attr('y1',y(top)).attr('y2',y(top)).attr('stroke',color('--border')).attr('stroke-dasharray','4 4')}})}}
          function treemap(){{chart.innerHTML='';const w=Math.max(300,chart.clientWidth||700),fullH=baseH(),tiny=values.filter(v=>v.value>0&&v.value/base<.03),items=values.filter(v=>v.value>0&&v.value/base>=.03),h=fullH-(tiny.length?20+24*Math.ceil(tiny.length/Math.max(1,Math.floor(w/220))):0),svg=d3.select(chart).append('svg').attr('width',w).attr('height',h),root=d3.hierarchy({{children:items}}).sum(d=>d.value).sort((a,b)=>b.value-a.value);d3.treemap().tile(d3.treemapSquarify.ratio(1.4)).size([w,h]).paddingInner(4).round(true)(root);root.leaves().forEach((leaf,i)=>{{const tw=leaf.x1-leaf.x0,th=leaf.y1-leaf.y0,g=svg.append('g').attr('class','treemap-tile').attr('transform',`translate(${{leaf.x0}},${{leaf.y0}}) scale(${{reduced?1:.96}})`).attr('opacity',reduced?1:0);g.append('rect').attr('width',Math.max(0,tw)).attr('height',Math.max(0,th)).attr('rx',8).attr('fill',color(leaf.data.color));g.append('title').text(leaf.data.name+' · '+fmt(leaf.data.value)+' ('+pct(leaf.data.value)+')'+(leaf.data.name==='Surplus'?'':' · click to see transactions'));if(leaf.data.name!=='Surplus')g.style('cursor','pointer').on('click',()=>openTx(catKey(leaf.data.name)));if(tw>=90&&th>=56){{g.append('text').attr('x',10).attr('y',22).attr('class','treemap-label').text(leaf.data.name);g.append('text').attr('x',10).attr('y',42).attr('class','treemap-amount').text(fmt(leaf.data.value));if(th>=76)g.append('text').attr('x',10).attr('y',61).attr('class','treemap-percent').text(pct(leaf.data.value))}}else if(tw>=58&&th>=32)g.append('text').attr('x',10).attr('y',22).attr('class','treemap-amount').text(fmt(leaf.data.value));g.transition().delay(reduced?0:i*40).duration(reduced?0:360).attr('opacity',1).attr('transform',`translate(${{leaf.x0}},${{leaf.y0}}) scale(1)`)}});if(tiny.length){{const legend=d3.select(chart).append('div').attr('class','treemap-legend');tiny.forEach(d=>{{const chip=legend.append('span').attr('class','treemap-chip');chip.append('span').attr('class','treemap-swatch').style('background',color(d.color));chip.append('span').text(d.name+' · '+fmt(d.value)+' ('+(d.value/base*100).toFixed(1)+'%)')}})}}}}
          function bars(){{const items=values.filter(v=>v.value>0).sort((a,b)=>b.value-a.value),[svg,w,h]=svgBase(items.length*40+40),labelW=Math.min(140,Math.max(96,w*.24)),amountW=Math.min(126,Math.max(92,w*.22)),trackX=labelW,trackW=Math.max(40,w-labelW-amountW-12),rowGap=16,totalH=items.length*24+(items.length-1)*rowGap,startY=Math.max(20,(h-totalH)/2);items.forEach((d,i)=>{{const y=startY+i*(24+rowGap),raw=trackW*(d.value/base),fillW=d.value>0?Math.max(4,Math.min(trackW,raw)):0;svg.append('text').attr('x',0).attr('y',y+16).attr('class','chart-bar-label').text(d.name);svg.append('rect').attr('class','bar-track').attr('x',trackX).attr('y',y).attr('width',trackW).attr('height',24).attr('rx',6).attr('fill',color('--brand-050'));svg.append('rect').attr('class','bar-fill').attr('x',trackX).attr('y',y).attr('height',24).attr('width',reduced?fillW:0).attr('rx',6).attr('fill',color(d.color)).transition().delay(reduced?0:i*60).duration(reduced?0:400).ease(d3.easeCubicOut).attr('width',fillW);svg.append('text').attr('x',w-4).attr('y',y+16).attr('text-anchor','end').attr('class','chart-amount').text(fmt(d.value));if(d.name!=='Surplus'){{const hit=svg.append('rect').attr('x',0).attr('y',y-rowGap/2).attr('width',w).attr('height',24+rowGap).attr('fill','transparent').style('cursor','pointer').on('click',()=>openTx(catKey(d.name)));hit.append('title').text(d.name+' · click to see transactions')}}}})}}
          const draws={{sankey,waterfall,treemap,bars}};let legend=document.getElementById('inflow-legend');if(!legend&&incomes.length){{legend=document.createElement('div');legend.id='inflow-legend';legend.className='treemap-legend inflow-legend';legend.innerHTML='<span class="treemap-chip"><b>Inflow</b></span>'+incomes.map(v=>`<a class="treemap-chip" href="${{linkTo(v.category,'income')}}"><span class="treemap-swatch" style="background:${{color(v.color)}}"></span>${{String(v.name).replace(/&/g,'&amp;').replace(/</g,'&lt;')}} · ${{fmt(v.value)}}</a>`).join('');chart.parentNode.insertBefore(legend,chart.nextSibling)}}function select(view){{if(legend)legend.hidden=!(view==='treemap'||view==='bars');document.querySelectorAll('.chart-toggle button').forEach(b=>b.classList.toggle('active',b.dataset.view===view));chart.classList.add('switching');const draw=()=>{{draws[view]();if(reduced)chart.classList.remove('switching');else requestAnimationFrame(()=>chart.classList.remove('switching'))}};if(reduced)draw();else setTimeout(draw,100)}}document.querySelectorAll('.chart-toggle button').forEach(b=>b.onclick=()=>select(b.dataset.view));const activeView=()=>(document.querySelector('.chart-toggle .active')||{{dataset:{{view:'sankey'}}}}).dataset.view,expandBtn=document.getElementById('chart-expand');function setExpanded(on){{card.classList.toggle('expanded',on);document.body.classList.toggle('chart-expanded',on);expandBtn.setAttribute('aria-pressed',on?'true':'false');expandBtn.title=expandBtn.ariaLabel=on?'Exit expanded view':'Expand chart';expandBtn.innerHTML=`<i data-lucide="${{on?'minimize-2':'maximize-2'}}"></i>`;if(window.lucide)lucide.createIcons();select(activeView())}}expandBtn.onclick=()=>setExpanded(!expanded());document.addEventListener('keydown',e=>{{if(e.key==='Escape'&&expanded())setExpanded(false)}});fetch('/api/sankey?month={urllib.parse.quote(month)}').then(r=>r.json()).then(d=>{{graph=d;select('sankey')}}).catch(()=>select('sankey'));window.addEventListener('resize',(()=>{{let t;return()=>{{clearTimeout(t);t=setTimeout(()=>select(document.querySelector('.chart-toggle .active').dataset.view),180)}}}})());
        }})();
        </script>'''
        self.send_html("Varavu.Selavu", body, beacon_month=month)

    def admin(self, method: str):
        with db() as conn:
            custom_ids = {row[0] for row in conn.execute("SELECT source_id FROM account_logos").fetchall()}
            use_seed = seed_data_enabled(conn)
            pocket_threshold = pocket_change_threshold(conn)
            seed_status = "Seed data is visible on the dashboard." if use_seed else "Seed data is hidden from the dashboard."
            password_state = {
                row["source_id"]: row
                for row in conn.execute(
                    "SELECT source_id, encrypted_password, password_pattern, account_number_hint FROM account_passwords"
                ).fetchall()
            }
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
        password_key_missing = not STATEMENT_PASSWORD_KEY
        if password_key_missing:
            password_card = '''<div class="card account-passwords-card"><h2>Accounts &amp; statement passwords</h2>
              <p class="muted">Set the <code>STATEMENT_PASSWORD_KEY</code> environment variable to manage statement passwords.</p></div>'''
        else:
            password_rows = []
            for source_id, source_name, _payer, _status in SOURCES:
                existing = password_state.get(source_id)
                has_password = bool(existing and existing["encrypted_password"])
                pattern_value = html.escape((existing["password_pattern"] or "") if existing else "")
                hint_value = html.escape((existing["account_number_hint"] or "") if existing else "")
                password_rows.append(f'''
                <div class="password-row" data-password-row data-account-id="{html.escape(source_id)}">
                  <div class="password-account">{render_bank_logo(source_name, True, False, source_id)}<strong>{html.escape(source_name)}</strong></div>
                  <div class="password-fields">
                    <label class="password-field"><span>Statement password</span>
                      <span class="password-input-wrap">
                        <input type="password" data-password-input autocomplete="off" placeholder="{'Password saved — enter a new one to replace' if has_password else 'Not set'}">
                        <button type="button" class="icon-button reveal-password" data-reveal aria-label="Hold to reveal password"><i data-lucide="eye"></i></button>
                      </span>
                    </label>
                    <label class="password-field"><span>Password pattern</span><input type="text" data-pattern-input value="{pattern_value}" placeholder="e.g. First 4 letters of name + DDMM"></label>
                    <label class="password-field"><span>Account number hint</span><input type="text" data-hint-input value="{hint_value}" placeholder="Used to cross-check account auto-match"></label>
                  </div>
                  <button type="button" class="btn secondary save-password" data-save>Save</button>
                  <p class="password-status" role="status" data-password-status></p>
                  <p class="password-error" role="alert" data-password-error></p>
                </div>''')
            password_card = f'''<div class="card account-passwords-card"><h2>Accounts &amp; statement passwords</h2>
              <p class="muted">Used to auto-decrypt statements on import. Passwords are encrypted at rest and never shown again after saving.</p>
              <p class="caption">Password pattern helper text: e.g. first 4 letters of name + DDMM — used to derive the password automatically when no stored password works.</p>
              <div class="password-rows">{''.join(password_rows)}</div></div>'''
        body = rf'''
        <section class="admin-sections"><div class="card seed-data-card"><div class="setting-row"><div><h2>Use seed data</h2><p class="muted">Show the demo transactions while you explore the app.</p></div><label class="settings-switch"><input type="checkbox" data-seed-toggle {'checked' if use_seed else ''} aria-label="Use seed data"><span aria-hidden="true"></span></label></div><p class="setting-status" data-seed-status role="status">{html.escape(seed_status)}</p></div><div class="card pocket-change-card"><div class="setting-row"><div><h2>Pocket change threshold</h2><p class="muted">Uncategorised money-out below this amount is filed under <strong>Pocket change</strong> automatically, so tiny transactions never reach the review queue. Money-in and transfers are never affected. Set 0 to turn it off. New imports pick this up automatically; saving re-files existing transactions right away.</p></div><form class="pocket-form" data-pocket-form><label class="pocket-field"><span>₹</span><input type="number" name="threshold" min="0" step="10" inputmode="numeric" value="{pocket_threshold}" data-pocket-input aria-label="Pocket change threshold in rupees"></label><button class="secondary" type="submit">Save</button></form></div><p class="setting-status" data-pocket-status role="status"></p></div><div class="card bank-logos-card"><h2>Bank logos</h2><p class="muted">Upload a logo for each account. It appears everywhere the account is shown.</p><p class="caption">Tip: use the bank's square symbol, not the full wordmark — wordmarks blur at small sizes.</p>
          <div class="logo-rows">{''.join(rows)}</div></div>{password_card}</section>
        <style>
          .admin-sections{{display:grid;gap:var(--sp-5)}} .pocket-form{{display:flex;align-items:center;gap:var(--sp-2);flex:none}} .pocket-field{{display:inline-flex;align-items:center;gap:6px;margin:0;font-weight:600;color:var(--ink-600)}} .pocket-field input{{width:110px;min-height:38px;font:600 var(--text-body)/1.4 var(--font-num)}} .setting-status.ok{{color:var(--pos-700)}} .setting-row{{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-5)}} .setting-row h2{{margin:0 0 var(--sp-1)}} .setting-row p{{margin:0}} .setting-status{{min-height:18px;margin:var(--sp-2) 0 0;color:var(--dang-700);font-size:var(--text-caption)}} .settings-switch{{position:relative;display:inline-flex;flex:none;cursor:pointer}} .settings-switch input{{position:absolute;opacity:0;pointer-events:none}} .settings-switch span{{width:50px;height:30px;padding:3px;border-radius:var(--radius-full);background:var(--ink-400);transition:background 150ms ease;box-shadow:inset 0 0 0 1px rgba(20,35,30,.08)}} .settings-switch span::after{{content:"";display:block;width:24px;height:24px;border-radius:50%;background:var(--surface);box-shadow:0 1px 3px rgba(20,35,30,.28);transition:transform 180ms cubic-bezier(.16,1,.3,1)}} .settings-switch input:checked+span{{background:var(--brand-700)}} .settings-switch input:checked+span::after{{transform:translateX(20px)}} .settings-switch input:focus-visible+span{{outline:3px solid var(--focus);outline-offset:3px}} .settings-switch input:disabled+span{{opacity:.58;cursor:wait}} .bank-logos-card>h2{{margin-bottom:var(--sp-1)}} .bank-logos-card>.muted{{margin:0 0 var(--sp-1)}} .bank-logos-card>.caption{{margin:0}}
          .logo-rows{{margin-top:var(--sp-5);border-top:1px solid var(--border)}} .logo-row{{display:grid;grid-template-columns:150px minmax(180px,1fr) auto;align-items:center;gap:var(--sp-4);padding:var(--sp-4);margin:0 calc(-1*var(--sp-4));border-bottom:1px solid var(--border);border-radius:var(--radius-md);position:relative}}
          .logo-row.drag-over{{background:var(--brand-050);box-shadow:inset 0 0 0 2px var(--brand-600);border-bottom-color:transparent}} .logo-previews{{display:flex;align-items:end;gap:var(--sp-3)}} .logo-previews>div{{display:grid;justify-items:center;gap:var(--sp-1)}} .logo-previews small{{font-size:var(--text-caption);color:var(--ink-400)}} .logo-account{{font-size:var(--text-body-lg);font-weight:600}}
          .logo-actions{{display:flex;align-items:center;justify-content:flex-end;gap:var(--sp-2)}} .logo-input{{position:absolute;width:1px;height:1px;opacity:0;clip-path:inset(50%)}} .upload-logo{{cursor:pointer}} .upload-logo svg{{width:16px;height:16px}} .remove-logo{{display:none;background:transparent;color:var(--dang-700);padding:var(--sp-2)}} .remove-logo:hover{{background:var(--dang-100)}} .remove-logo.visible{{display:inline-flex}} .logo-row.uploading .upload-logo{{pointer-events:none;opacity:.72}} .logo-row.uploading .upload-logo::after{{content:"";width:14px;height:14px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:logo-spin 700ms linear infinite}}
          .remove-confirm{{display:none;grid-column:2/4;justify-content:flex-end;align-items:center;gap:var(--sp-2);color:var(--dang-700);font-size:var(--text-caption)}} .remove-confirm.open{{display:flex}} .remove-confirm button{{min-height:32px;padding:var(--sp-1) var(--sp-3);font-size:var(--text-caption)}} .logo-status,.logo-error{{grid-column:2/4;font-size:var(--text-caption);margin:0;min-height:18px}} .logo-status{{color:var(--pos-700)}} .logo-error{{color:var(--dang-700)}}
          .setting-status{{color:var(--ink-400)}}
          @keyframes logo-spin{{to{{transform:rotate(360deg)}}}}
          @media(max-width:767px){{.logo-row{{grid-template-columns:110px 1fr;gap:var(--sp-3)}}.logo-actions{{grid-column:1/3;justify-content:flex-start}}.remove-confirm,.logo-status,.logo-error{{grid-column:1/3;justify-content:flex-start}}}}
          @media(prefers-reduced-motion:reduce){{.logo-row.uploading .upload-logo::after{{animation:none}}}}
          .account-passwords-card>h2{{margin-bottom:var(--sp-1)}} .account-passwords-card>.muted{{margin:0 0 var(--sp-1)}} .account-passwords-card>.caption{{margin:0}}
          .password-rows{{margin-top:var(--sp-5);border-top:1px solid var(--border)}}
          .password-row{{display:grid;grid-template-columns:200px 1fr auto;align-items:start;gap:var(--sp-4);padding:var(--sp-4);margin:0 calc(-1*var(--sp-4));border-bottom:1px solid var(--border)}}
          .password-account{{display:flex;align-items:center;gap:var(--sp-2);padding-top:var(--sp-2)}} .password-account strong{{font-size:var(--text-body)}}
          .password-fields{{display:grid;grid-template-columns:repeat(3,1fr);gap:var(--sp-3)}}
          .password-field{{display:grid;gap:var(--sp-1);font-size:var(--text-caption);color:var(--ink-600)}} .password-field input{{min-height:36px}}
          .password-input-wrap{{position:relative;display:flex}} .password-input-wrap input{{flex:1;padding-right:40px}}
          .reveal-password{{position:absolute;right:0;top:0;width:36px;height:36px}}
          .save-password{{margin-top:var(--sp-2)}}
          .password-status,.password-error{{grid-column:1/4;font-size:var(--text-caption);margin:0;min-height:18px}} .password-status{{color:var(--pos-700)}} .password-error{{color:var(--dang-700)}}
          @media(max-width:900px){{.password-row{{grid-template-columns:1fr}}.password-fields{{grid-template-columns:1fr}}.password-status,.password-error{{grid-column:1}}}}
        </style>
        <script>
        (()=>{{
          const invalid='PNG, SVG, WebP or JPG under 1 MB';
          const pocketForm=document.querySelector('[data-pocket-form]'),pocketStatus=document.querySelector('[data-pocket-status]');
          if(pocketForm)pocketForm.onsubmit=async e=>{{e.preventDefault();const input=pocketForm.querySelector('[data-pocket-input]'),button=pocketForm.querySelector('button');button.disabled=true;pocketStatus.textContent='';pocketStatus.classList.remove('ok');try{{const res=await fetch('/admin/pocket-change',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}},body:new URLSearchParams({{threshold:input.value}}),credentials:'same-origin'}}),data=await res.json();if(!res.ok)throw Error(data.error||'Could not save threshold');input.value=data.threshold;pocketStatus.textContent=data.status||'';pocketStatus.classList.add('ok');window.showToast?.(data.toast||'Threshold saved','check-circle-2',2200)}}catch(error){{pocketStatus.textContent=error.message;window.showToast?.('Could not save threshold.','alert-circle',1800)}}finally{{button.disabled=false}}}};
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
          function setPasswordStatus(row,message,kind='info'){{
            const status=row.querySelector('[data-password-status]');
            const err=row.querySelector('[data-password-error]');
            status.textContent=kind==='error'?'':message;
            err.textContent=kind==='error'?message:'';
          }}
          document.querySelectorAll('[data-password-row]').forEach(row=>{{
            const passwordInput=row.querySelector('[data-password-input]'),reveal=row.querySelector('[data-reveal]');
            const showPassword=()=>passwordInput.type='text',hidePassword=()=>passwordInput.type='password';
            reveal.addEventListener('mousedown',showPassword);reveal.addEventListener('touchstart',showPassword);
            ['mouseup','mouseleave','touchend','touchcancel'].forEach(evt=>reveal.addEventListener(evt,hidePassword));
            row.querySelector('[data-save]').onclick=async()=>{{
              const saveBtn=row.querySelector('[data-save]');
              setPasswordStatus(row,'Saving...');
              saveBtn.disabled=true;
              try{{
                const res=await fetch('/admin/password',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:new URLSearchParams({{
                  source_id:row.dataset.accountId,
                  password:passwordInput.value,
                  password_pattern:row.querySelector('[data-pattern-input]').value,
                  account_number_hint:row.querySelector('[data-hint-input]').value,
                }}),credentials:'same-origin'}});
                const data=await readJson(res);
                if(!res.ok)throw new Error(data.error||'Could not save');
                if(passwordInput.value)passwordInput.placeholder='Password saved — enter a new one to replace';
                passwordInput.value='';
                hidePassword();
                setPasswordStatus(row,'Saved.');
                window.showToast?.('Statement password settings saved');
              }}catch(e){{
                setPasswordStatus(row,e.message||'Could not save. Refresh and retry.','error');
              }}finally{{
                saveBtn.disabled=false;
              }}
            }};
          }});
        }})();
        </script>'''
        self.send_html("Admin", body)

    def admin_pocket_change(self, method: str):
        if method != "POST":
            return self.not_found()
        data = self.form()
        raw = (data.get("threshold") or "").strip()
        try:
            value = int(float(raw))
        except ValueError:
            return self.json_response({"error": "Enter a whole rupee amount (0 turns it off)"}, 400)
        if value < 0 or value > 1_000_000:
            return self.json_response({"error": "Threshold must be between 0 and 10,00,000"}, 400)
        with db() as conn:
            set_pocket_change_threshold(conn, value)
            result = reapply_rules(conn)
            audit(conn, "dashboard", "reapply_rules", "rules", "all", after={**result, "trigger": "pocket_change_threshold"})
        n = result["updated"]
        status = (f"Pocket change is off. {n} transaction{'s' if n != 1 else ''} re-filed." if value == 0
                  else f"Money-out under {money(value)} is filed under Pocket change. {n} transaction{'s' if n != 1 else ''} re-filed.")
        toast = "Threshold saved" + (f" · {n} transaction{'s' if n != 1 else ''} re-filed" if n else "")
        return self.json_response({"ok": True, "threshold": value, "updated": n, "scanned": result.get("scanned"), "status": status, "toast": toast})

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

    def admin_password(self, method: str):
        if method != "POST":
            return self.not_found()
        data = self.form()
        source_id = data.get("source_id", "")
        if not any(r[0] == source_id for r in SOURCES):
            return self.json_response({"error": "Unknown account"}, 400)
        password = data.get("password", "")
        pattern = (data.get("password_pattern", "") or "").strip() or None
        hint = (data.get("account_number_hint", "") or "").strip() or None
        if password:
            try:
                encrypted = encrypt_password(password)
            except PasswordKeyMissing:
                return self.json_response({"error": "Set STATEMENT_PASSWORD_KEY to manage statement passwords"}, 400)
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO account_passwords(source_id, encrypted_password, password_pattern, account_number_hint, updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET encrypted_password=excluded.encrypted_password, password_pattern=excluded.password_pattern, account_number_hint=excluded.account_number_hint, updated_at=excluded.updated_at
                    """,
                    (source_id, encrypted, pattern, hint, now_iso()),
                )
                audit(conn, "dashboard", "save_account_password", "source", source_id, after={"has_password": True, "password_pattern_set": bool(pattern), "account_number_hint_set": bool(hint)})
        else:
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO account_passwords(source_id, encrypted_password, password_pattern, account_number_hint, updated_at)
                    VALUES(?, NULL, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET password_pattern=excluded.password_pattern, account_number_hint=excluded.account_number_hint, updated_at=excluded.updated_at
                    """,
                    (source_id, pattern, hint, now_iso()),
                )
                audit(conn, "dashboard", "save_account_password", "source", source_id, after={"has_password": None, "password_pattern_set": bool(pattern), "account_number_hint_set": bool(hint)})
        return self.json_response({"ok": True})

    def transactions(self, method: str):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        def param(name: str) -> str:
            return (query.get(name) or [""])[0].strip()

        month = param("month") or self.active_month()
        month_filter = None if month == "all" else month
        filters = {k: param(k) for k in TX_FILTER_KEYS}
        filters["flow"] = ",".join(flow_values(filters["flow"]))
        if not filters["category"] or filters["category"] == UNCATEGORISED_FILTER:
            filters["subcategory"] = ""
        sort = param("sort") if param("sort") in TX_SORTS else "date"
        direction = "asc" if param("dir") == "asc" else "desc"
        view = param("view") if param("view") in BREAKDOWN_FACETS else "category"
        try:
            page = int(param("page") or 1)
        except ValueError:
            page = 1
        with db() as conn:
            result = query_transactions(conn, month_filter, filters, sort, direction, page)
            items = breakdown(conn, month_filter, filters, view)
            present = tx_present_values(conn, None)
            categories, subcategories = taxonomy_options(conn)
        rows = result["rows"]
        page = result["page"]

        current = {"month": month, **filters, "view": view, "sort": sort, "dir": direction, "page": str(page)}
        defaults = {"view": "category", "sort": "date", "dir": "desc", "page": "1"}

        def tx_url(**overrides) -> str:
            params = dict(current)
            if any(k not in ("page", "sort", "dir") for k in overrides):
                params["page"] = ""
            for k, v in overrides.items():
                params[k] = "" if v is None else str(v)
            kept = {k: v for k, v in params.items() if v and defaults.get(k) != v}
            return "/transactions" + ("?" + urllib.parse.urlencode(kept) if kept else "")

        # --- filter form -----------------------------------------------------------------
        def options(values: list[tuple[str, str]], selected: str, empty_label: str) -> str:
            opts = [f'<option value="" {"selected" if not selected else ""}>{html.escape(empty_label)}</option>']
            opts += [f'<option value="{html.escape(v, quote=True)}" {"selected" if v == selected else ""}>{html.escape(label)}</option>' for v, label in values]
            return "".join(opts)
        month_values = [(m, human_month(m)) for m in present["months"]]
        if month_filter and month_filter not in present["months"]:
            month_values.insert(0, (month_filter, human_month(month_filter)))
        month_options = (f'<option value="all"{" selected" if month == "all" else ""}>All months</option>'
                         + "".join(f'<option value="{html.escape(m, quote=True)}"{" selected" if m == month else ""}>{html.escape(label)}</option>' for m, label in month_values))
        category_values = [(c, c) for c in PARENT_CATEGORIES + INCOME_CATEGORIES + HIDDEN_CATEGORIES + sorted(set(present["categories"]) - set(PARENT_CATEGORIES) - set(INCOME_CATEGORIES) - set(HIDDEN_CATEGORIES))] + [(UNCATEGORISED_FILTER, UNCATEGORISED_LABEL)]
        sub_opts = [f'<option value="">Any subcategory</option>']
        for parent in categories:
            for sub in subcategories.get(parent, []):
                sub_opts.append(f'<option value="{html.escape(sub, quote=True)}" data-parent="{html.escape(parent, quote=True)}"{" selected" if filters["category"] == parent and filters["subcategory"] == sub else ""}>{html.escape(sub)}</option>')
        sub_opts.append(f'<option value="{NO_SUBCATEGORY_FILTER}"{" selected" if filters["subcategory"] == NO_SUBCATEGORY_FILTER else ""}>{NO_SUBCATEGORY_LABEL}</option>')
        known_sources = [s_[1] for s_ in SOURCES]
        source_values = [(x, x) for x in known_sources] + [(x, x) for x in sorted(set(present["sources"]) - set(known_sources))]
        payer_values = [(x, x) for x in PAYERS] + [(x, x) for x in sorted(set(present["payers"]) - set(PAYERS))]
        auto = "onchange='this.form.submit()'"
        has_filters = any(filters.values()) or month != self.active_month()
        clear_link = '<a class="btn secondary" href="/transactions">Clear</a>' if has_filters else ''
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">' for k, v in (("view", view), ("sort", sort), ("dir", direction)) if defaults.get(k) != v)
        filter_form = f"""
        <form method="get" class="tx-filters" role="search" aria-label="Filter transactions">{hidden}
          <label><span>Month</span><select name="month" {auto}>{month_options}</select></label>
          <label><span>Category</span><select name="category" {auto}>{options(category_values, filters["category"], "All categories")}</select></label>
          <label><span>Subcategory</span><select name="subcategory" {auto}>{"".join(sub_opts)}</select></label>
          <label><span>Account</span><select name="source" {auto}>{options(source_values, filters["source"], "All accounts")}</select></label>
          <label><span>Payer</span><select name="payer" {auto}>{options(payer_values, filters["payer"], "Anyone")}</select></label>
          <div class="tx-field"><span>Flow</span>{render_multi_select("flow", [(f, human_label(f)) for f in FLOW_TYPES], flow_values(filters["flow"]), "Any flow")}</div>
          <label><span>Classification</span><select name="classification" {auto}>{options([(c, human_label(c)) for c in CLASSIFICATIONS], filters["classification"], "Any classification")}</select></label>
          <label class="tx-search"><span>Search</span><input type="search" name="q" value="{html.escape(filters["q"], quote=True)}" placeholder="Description, merchant, note…"></label>
          <div class="tx-filter-actions"><button class="secondary" type="submit">Apply</button>{clear_link}</div>
        </form>"""

        # --- active-filter chips + summary strip ------------------------------------------------
        active: list[tuple[str, str, str, dict]] = []
        if month != "all":
            active.append(("month", "Month", human_month(month), {"month": "all"}))
        if filters["category"]:
            active.append(("category", "Category", UNCATEGORISED_LABEL if filters["category"] == UNCATEGORISED_FILTER else filters["category"], {"category": None, "subcategory": None}))
        if filters["subcategory"]:
            active.append(("subcategory", "Subcategory", NO_SUBCATEGORY_LABEL if filters["subcategory"] == NO_SUBCATEGORY_FILTER else filters["subcategory"], {"subcategory": None}))
        for key, label in (("source", "Account"), ("payer", "Payer")):
            if filters[key]:
                active.append((key, label, filters[key], {key: None}))
        if flow_values(filters["flow"]):
            active.append(("flow", "Flow", ", ".join(human_label(v) for v in flow_values(filters["flow"])), {"flow": None}))
        if filters["classification"]:
            active.append(("classification", "Classification", human_label(filters["classification"]), {"classification": None}))
        if filters["q"]:
            active.append(("q", "Search", f'"{filters["q"]}"', {"q": None}))
        active_chips = "".join(
            f'<a class="chip filter-chip" href="{html.escape(tx_url(**drop), quote=True)}" aria-label="Remove filter {html.escape(label)}"><span>{html.escape(label)}: {html.escape(value)}</span><i data-lucide="x"></i></a>'
            for _k, label, value, drop in active
        )
        total = result["total"]
        plural = "s" if total != 1 else ""
        summary = (f'<div class="tx-summary"><span class="num"><b>{total}</b> transaction{plural}</span>'
                   f'<span class="num">{money(-result["money_out"])} out</span><span class="num">{money(result["money_in"])} in</span>{active_chips}</div>')

        # --- breakdown panel ------------------------------------------------------------------
        breakdown_panel = render_breakdown_panel(items, view, filters, tx_url)

        # --- table + pager ---------------------------------------------------------------------
        def sort_header(key: str, label: str, cls: str = "") -> str:
            is_active = sort == key
            next_dir = "asc" if (is_active and direction == "desc") else "desc"
            icon = f'<i data-lucide="{"arrow-down" if direction == "desc" else "arrow-up"}"></i>' if is_active else ""
            aria = f' aria-sort="{"descending" if direction == "desc" else "ascending"}"' if is_active else ""
            return f'<th class="{cls}"{aria}><a class="tx-sort" href="{html.escape(tx_url(sort=key, dir=next_dir), quote=True)}">{label}{icon}</a></th>'
        drawer_items: dict[str, dict] = {}
        body_rows = []
        for r in rows:
            txid = r["transaction_id"]
            drawer_items[txid] = {"id": txid, "date": human_date(r["transaction_date"]), "description": r["description"], "amount": money(r["amount"]),
                                 "direction": "Money out" if r["amount"] < 0 else "Money in", "source": r["source_name"] or "Unknown source",
                                 "category": r["category"] or "", "subcategory": r["subcategory"] or "", "classification": r["classification"] or "",
                                 "note": r["notes"] or "", "current_chip": render_category_chip(r["category"], r["subcategory"], link=False) if r["category"] else ""}
            body_rows.append(
                f'<tr class="tx-row" data-tx-id="{html.escape(txid, quote=True)}" tabindex="0" role="button" aria-label="Edit {html.escape(r["description"], quote=True)}"><td class="tx-date">{html.escape(human_date(r["transaction_date"]))}</td><td class="tx-desc" title="{html.escape(r["description"], quote=True)}">{html.escape(r["description"])}</td>'
                f'<td class="right amount">{money(r["amount"])}</td><td class="tx-flow-cell">{render_kind_chip("flow", r["flow_type"])}</td><td class="tx-cat-cell">{render_category_chip(r["category"], r["subcategory"], month=month)}</td>'
                f'<td class="tx-class-cell">{render_kind_chip("classification", r["classification"])}</td><td>{html.escape(r["payer"] or "")}</td><td class="tx-source tx-source-cell">{html.escape(r["source_name"] or "")}</td></tr>'
            )
        pager = ""
        if result["pages"] > 1:
            first = (page - 1) * result["per_page"] + 1
            last = min(total, page * result["per_page"])
            window = [n for n in range(1, result["pages"] + 1) if n <= 2 or n > result["pages"] - 2 or abs(n - page) <= 2]
            links = []
            prev_n = None
            for n in window:
                if prev_n and n - prev_n > 1:
                    links.append('<span class="gap">…</span>')
                links.append(f'<span class="current" aria-current="page">{n}</span>' if n == page else f'<a href="{html.escape(tx_url(page=n), quote=True)}">{n}</a>')
                prev_n = n
            prev_link = f'<a href="{html.escape(tx_url(page=page - 1), quote=True)}" aria-label="Previous page"><i data-lucide="chevron-left"></i></a>' if page > 1 else '<span class="disabled"><i data-lucide="chevron-left"></i></span>'
            next_link = f'<a href="{html.escape(tx_url(page=page + 1), quote=True)}" aria-label="Next page"><i data-lucide="chevron-right"></i></a>' if page < result["pages"] else '<span class="disabled"><i data-lucide="chevron-right"></i></span>'
            pager = f'<nav class="tx-pager" aria-label="Pagination"><span>Showing {first}–{last} of {total}</span><div class="tx-pages">{prev_link}{"".join(links)}{next_link}</div></nav>'
        if rows:
            content = (f"<div class='table-scroll'><table class='keep-table tx-table'><thead><tr>{sort_header('date', 'Date', 'tx-date')}<th>Description</th>{sort_header('amount', 'Amount', 'right')}"
                       f"<th>Flow</th><th>Category</th><th>Classification</th><th>Payer</th><th>Source</th></tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>{pager}")
        elif has_filters or present["months"]:
            content = '<div class="empty-state"><span class="empty-icon"><i data-lucide="filter-x"></i></span><p>No transactions match these filters.</p><a class="btn secondary" href="/transactions?month=all">Clear filters</a></div>'
        else:
            content = empty_state("receipt-text", "No transactions yet. Import a statement to get started.", "Import statement", "/import")
        table_card = f"<section class='card section-gap'><h2>Transactions <span class='count' data-tx-count>({total})</span></h2>{content}</section>"
        drawer = (f"<style>{DRAWER_CSS}{DRAWER_MOBILE_CSS}</style>" + render_tx_drawer(drawer_items, categories, subcategories)) if rows else ""
        active_count = len(active)
        mobile_label = f"Filters · {active_count} active" if active_count else "Filters"
        body = (f"{TX_PAGE_CSS}<div class='tx-sticky-sentinel' data-sticky-sentinel></div><div class='card tx-filter-card' data-filter-card data-active-count='{active_count}'>"
                f"<details class='tx-filters-collapse' open><summary><i data-lucide='sliders-horizontal'></i><span>{html.escape(mobile_label)}</span><i data-lucide='chevron-down' class='tx-collapse-caret'></i></summary>{filter_form}</details>{summary}</div>"
                f"<div class='section-gap'>{breakdown_panel}</div>{table_card}{drawer}{TX_PAGE_SCRIPT}{MULTI_SELECT_SCRIPT}")
        self.send_html("Transactions", body, beacon_month=month_filter)

    def review(self, method: str):
        if method == "POST":
            data = self.form()
            with db() as conn:
                txid, action = data["transaction_id"], data.get("action", "approve")
                found = conn.execute("SELECT * FROM transactions WHERE transaction_id=?", (txid,)).fetchone()
                if not found:
                    return self.json_response({"error": "Transaction not found"}, 404)
                # Edit against the *effective* row so re-editing keeps earlier override fields.
                eff_sql, eff_args = effective_tx_sql(None, True, ("t.transaction_id=?",), (txid,))
                effective = conn.execute(eff_sql, eff_args).fetchone()
                before = dict(effective) if effective else dict(found)
                from_transactions = data.get("origin") == "transactions"
                category = data.get("category") or None
                if action == "approve" and not category:
                    return self.json_response({"error": "Choose a category before approving"}, 400)
                flow_type = before.get("flow_type") or "spend"
                classification = data.get("classification") if data.get("classification") in CLASSIFICATIONS else (before.get("classification") or "controllable")
                # The Transactions drawer sends the subcategory field as the whole truth (empty = clear); Review keeps its fallback.
                # An override NULL means "no change", so a cleared subcategory is stored as '' (rendered as none everywhere).
                subcategory = (data.get("subcategory") or "") if from_transactions else (data.get("subcategory") or before.get("subcategory"))
                if action == "transfer":
                    flow_type, classification, category = "transfer", "excluded", None
                elif action == "exclude":
                    flow_type, classification, category = "unknown", "excluded", None
                elif action == "uncategorise":
                    # '' (not NULL) so the override overlay clears the category; NULLIF turns it back into None everywhere.
                    category, subcategory = "", ""
                oid = "override_" + stable_hash(txid, time.time())
                conn.execute(
                    """
                    INSERT INTO manual_overrides(manual_override_id, transaction_id, category, subcategory, classification, flow_type, merchant_payee, notes, created_at, created_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (oid, txid, category, subcategory, classification, flow_type, before.get("merchant_payee") or before["description"], data.get("notes") or None, now_iso(), "dashboard"),
                )
                conn.execute("UPDATE transactions SET manual_override_id=? WHERE transaction_id=?", (oid, txid))
                if action == "uncategorise":
                    create_review_item(conn, txid, "manual_uncategorised")
                else:
                    conn.execute("UPDATE review_items SET status='resolved', resolved_at=? WHERE transaction_id=?", (now_iso(), txid))
                audit(conn, "dashboard", f"{'edit' if from_transactions else 'review'}_{action}", "transaction", txid, before=before, after=data)
                rule_id = None
                superseded: list[str] = []
                conflicts: list[dict] = []
                if data.get("remember") == "yes" and action == "approve":
                    pattern = before.get("merchant_payee") or before["description"]
                    rule_id = "rule_" + stable_hash("remember", pattern, category)
                    conflicts = rule_conflicts(conn, pattern, before.get("source_name"), category=category, subcategory=subcategory or None)
                    # A newer "remember" for the same merchant/account replaces the older one instead of tying with it.
                    superseded = [r[0] for r in conn.execute(
                        "SELECT rule_id FROM rules WHERE enabled=1 AND name LIKE 'Remember %' AND match_type='exact_merchant' AND pattern=? AND coalesce(source_name,'')=coalesce(?,'') AND rule_id != ?",
                        (pattern, before.get("source_name"), rule_id)).fetchall()]
                    for old_id in superseded:
                        conn.execute("UPDATE rules SET enabled=0, updated_at=? WHERE rule_id=?", (now_iso(), old_id))
                        audit(conn, "dashboard", "supersede_rule_from_remember", "rule", old_id, after={"replaced_by": rule_id})
                    conn.execute(
                        """
                        INSERT INTO rules(rule_id, name, match_type, pattern, source_name, category, subcategory, classification, flow_type, merchant_payee, confidence, notes, created_at, updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(rule_id) DO UPDATE SET category=excluded.category, subcategory=excluded.subcategory, classification=excluded.classification,
                          flow_type=excluded.flow_type, notes=excluded.notes, enabled=1, updated_at=excluded.updated_at
                        """,
                        (rule_id, f"Remember {pattern[:48]}", "exact_merchant", pattern, before.get("source_name"), category, subcategory or None, classification, flow_type, before.get("merchant_payee"), 0.94, data.get("notes") or None, now_iso(), now_iso()),
                    )
                    audit(conn, "dashboard", "create_rule_from_review", "rule", rule_id, after=data)
                remaining = conn.execute("""SELECT COUNT(DISTINCT ri.transaction_id) FROM review_items ri JOIN transactions t ON t.transaction_id=ri.transaction_id LEFT JOIN import_batches ib ON ib.import_batch_id=t.import_batch_id WHERE ri.status='open' AND (t.import_batch_id IS NULL OR (ib.deleted_at IS NULL AND ib.excluded_at IS NULL))""").fetchone()[0]
            if self.headers.get("Accept") == "application/json" or self.headers.get("X-Requested-With") == "fetch":
                payload = {"ok": True, "remaining": remaining, "action": action, "rule_id": rule_id, "superseded": superseded, "conflicts": conflicts}
                if from_transactions:
                    payload["row"] = {"category_chip": render_category_chip(category or None, subcategory or None), "classification_chip": render_kind_chip("classification", classification), "flow_chip": render_kind_chip("flow", flow_type)}
                return self.json_response(payload)
            if from_transactions:
                return self.redirect("/transactions?toast=" + urllib.parse.quote("Sent back for review" if action == "uncategorise" else "Transaction updated"))
            return self.redirect("/review")
        with db() as conn:
            # Effective rows (override overlay applied) so a row sent back from Transactions doesn't show its old category as the guess.
            eff_sql, eff_args = effective_tx_sql(None, seed_data_enabled(conn))
            rows = conn.execute(
                f"""
                WITH eff AS ({eff_sql})
                SELECT group_concat(ri.reason, ',') AS reason, eff.*
                FROM review_items ri JOIN eff ON eff.transaction_id=ri.transaction_id
                WHERE ri.status='open'
                GROUP BY eff.transaction_id
                ORDER BY max(ri.created_at) DESC
                """,
                eff_args,
            ).fetchall()
            categories, subcategories = taxonomy_options(conn)
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
            items.append({"id": r["transaction_id"], "date": human_date(r["transaction_date"]), "description": r["description"], "amount": money(r["amount"]), "direction": "Money out" if r["amount"] < 0 else "Money in", "source": r["source_name"] or "Unknown source", "source_id": source_id_for(r["source_name"]), "reasons": reason_labels(r["reason"]), "reason_chips": render_reason_chips(r["reason"]), "guess": r["category"] or "", "guess_chip": render_category_chip(r["category"], r["subcategory"], link=False) if r["category"] else "", "subcategory": r["subcategory"] or "", "note": r["notes"] or "", "time": time_value, "extra": extra})
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        body = render_review_workbench(items, categories, subcategories, query.get("open", [""])[0])
        self.send_html("Review Queue", body)

    def rules(self, method: str):
        if method == "POST":
            data = self.form()
            with db() as conn:
                if data.get("action") == "toggle":
                    conn.execute("UPDATE rules SET enabled=1-enabled, updated_at=? WHERE rule_id=?", (now_iso(), data["rule_id"]))
                    audit(conn, "dashboard", "toggle_rule", "rule", data["rule_id"])
                elif data.get("action") == "reapply":
                    result = reapply_rules(conn)
                    audit(conn, "dashboard", "reapply_rules", "rules", "all", after=result)
                    return self.redirect("/rules?toast=" + urllib.parse.quote(f"Re-applied rules: {result['updated']} of {result['scanned']} transactions updated"))
                else:
                    rid = data.get("rule_id") or "rule_" + stable_hash(data.get("name"), time.time())
                    conn.execute(
                        """
                        INSERT INTO rules(rule_id,name,match_type,pattern,source_name,category,subcategory,classification,flow_type,merchant_payee,confidence,enabled,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(rule_id) DO UPDATE SET name=excluded.name, match_type=excluded.match_type, pattern=excluded.pattern, category=excluded.category,
                        subcategory=excluded.subcategory, classification=excluded.classification, flow_type=excluded.flow_type, confidence=excluded.confidence, updated_at=excluded.updated_at
                        """,
                        (rid, data["name"], data["match_type"], data["pattern"], data.get("source_name") or None, data.get("category") or None, resolve_subcategory(data), data.get("classification") or None, data.get("flow_type") or None, data.get("merchant_payee") or None, float(data.get("confidence") or 0.8), 1, now_iso(), now_iso()),
                    )
                    audit(conn, "dashboard", "upsert_rule", "rule", rid, after=data)
            return self.redirect("/rules?toast=" + urllib.parse.quote("Rule saved" if data.get("action") != "toggle" else "Rule updated"))
        with db() as conn:
            rows = conn.execute("SELECT * FROM rules ORDER BY enabled DESC, confidence DESC, name").fetchall()
        table = "".join(
            f"<tr><td>{html.escape(r['name'])}</td><td>{html.escape(human_label(r['match_type']))}</td><td>{html.escape(r['pattern'])}</td><td>{html.escape(r['category'] or 'Any category')}{(' › ' + html.escape(r['subcategory'])) if r['subcategory'] else ''}</td><td>{html.escape(human_label(r['classification']))}</td><td class='amount'>{r['confidence']:.0%}</td><td>{'Enabled' if r['enabled'] else 'Paused'}</td><td><form method='post'><input type='hidden' name='action' value='toggle'><input type='hidden' name='rule_id' value='{r['rule_id']}'><button class='secondary'>{'Pause' if r['enabled'] else 'Enable'}</button></form></td></tr>"
            for r in rows
        )
        form = f"""
        <div class="card"><h2>Add rule</h2><form method="post" class="form-grid">
          <div><label>Name</label><input name="name" required></div><div><label>Match type</label><select name="match_type"><option value="description_contains">Description contains</option><option value="exact_merchant">Exact merchant</option><option value="normalized_merchant">Similar merchant</option><option value="regex">Regex</option></select></div><div><label>Pattern</label><input name="pattern" required></div>
          <div><label>Category</label>{select('category', ['']+PARENT_CATEGORIES, '')}</div><div><label>Subcategory</label>{subcategory_control('category', 'subcategory', '')}</div><div><label>Classification</label>{select('classification', ['']+CLASSIFICATIONS, '')}</div>
          <div><label>Flow type</label>{select('flow_type', ['']+FLOW_TYPES, '')}</div><div><label>Confidence</label><input name="confidence" value="0.86"></div><div class="align-end"><button>Save rule</button></div>
        </form></div>
        {SUBCATEGORY_CONTROL_SCRIPT}
        """
        listing = f"<div class='table-scroll'><table><thead><tr><th>Name</th><th>Match</th><th>Pattern</th><th>Category</th><th>Classification</th><th>Confidence</th><th>Status</th><th></th></tr></thead><tbody>{table}</tbody></table></div>" if rows else empty_state("sliders-horizontal", "No rules yet. Add one above to automate categorisation.")
        reapply = """<form method='post' class='reapply-form'><input type='hidden' name='action' value='reapply'><button class='secondary'>Re-apply rules to existing transactions</button><span class='caption'>Runs every enabled rule over transactions you haven't manually reviewed. Use after adding or editing rules.</span></form><style>.reapply-form{display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap;margin-bottom:var(--sp-4)}</style>"""
        self.send_html("Rules", form + f"<div class='card section-gap'><h2>Rule manager</h2>{reapply}{listing}</div>")

    def baselines(self, method: str):
        if method == "POST":
            data = self.form()
            with db() as conn:
                subcategory = resolve_subcategory(data)
                bid = data.get("baseline_id") or "baseline_" + stable_hash(data.get("category"), subcategory, data.get("effective_month"), time.time())
                conn.execute(
                    """
                    INSERT INTO baselines(baseline_id, scope, category, subcategory, amount, effective_month, updated_source, active, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (bid, "subcategory" if subcategory else "parent_category", data["category"], subcategory, float(data["amount"]), data["effective_month"], "dashboard", 1, now_iso(), now_iso()),
                )
                audit(conn, "dashboard", "create_baseline", "baseline", bid, after=data)
            return self.redirect("/baselines?toast=" + urllib.parse.quote("Baseline saved"))
        with db() as conn:
            rows = conn.execute("SELECT * FROM baselines WHERE active=1 ORDER BY category, subcategory").fetchall()
        table = "".join(f"<tr><td>{html.escape(r['category'])}</td><td>{html.escape(r['subcategory'] or 'All spending')}</td><td class='right amount'>{money(r['amount'])}</td><td>{html.escape(human_month(r['effective_month']))}</td><td>{html.escape(human_label(r['updated_source']))}</td></tr>" for r in rows)
        body = f"""
        <div class="card"><h2>Add baseline or cap</h2><form method="post" class="form-grid">
          <div><label>Category</label>{select('category', PARENT_CATEGORIES, 'Food')}</div><div><label>Subcategory</label>{subcategory_control('category', 'subcategory', '')}</div><div><label>Amount</label><input name="amount" value="12000" required></div>
          <div><label>Effective month</label>{month_control('effective_month', date.today().strftime('%Y-%m'))}</div><div class="align-end"><button>Save baseline</button></div>
        </form></div>
        {SUBCATEGORY_CONTROL_SCRIPT}
        <div class="card section-gap"><h2>Active baselines</h2>{f'<div class="table-scroll"><table><thead><tr><th>Category</th><th>Subcategory</th><th class="right">Amount</th><th>Effective</th><th>Source</th></tr></thead><tbody>{table}</tbody></table></div>' if rows else empty_state('ruler', 'No baselines yet. Add one above to start planning.')}</div>
        """
        self.send_html("Baselines", body)

    def import_status(self, method: str):
        if method != "GET":
            return self.not_found()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        batch_id = (query.get("batch_id") or [""])[0]
        with db() as conn:
            row = conn.execute("SELECT status, stage, notes FROM import_batches WHERE import_batch_id=?", (batch_id,)).fetchone()
        if not row:
            return self.json_response({"error": "Unknown import"}, 404)
        return self.json_response({"status": row["status"], "stage": row["stage"], "notes": row["notes"]})

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
                        # Remove the uploaded file too, unless another live batch still points at it.
                        file_removed = False
                        file_name = batch["file_name"]
                        if file_name and batch_id != SEED_BATCH_ID:
                            still_used = conn.execute(
                                "SELECT 1 FROM import_batches WHERE file_name=? AND import_batch_id!=? AND deleted_at IS NULL LIMIT 1",
                                (file_name, batch_id),
                            ).fetchone()
                            if not still_used:
                                try:
                                    (UPLOAD_DIR / file_name).unlink()
                                    file_removed = True
                                except FileNotFoundError:
                                    pass
                        audit(conn, "dashboard", "delete_import_batch", "import_batch", batch_id, after={"file_removed": file_removed})
                        message = "Import deleted"
                    elif action == "retry":
                        if batch["status"] not in ("needs_parser", "failed", "unable_to_parse"):
                            return self.redirect("/import?toast=" + urllib.parse.quote("Retry is only available when processing fails."))
                        conn.execute("UPDATE import_batches SET status='pending_pdf_extraction', notes=? WHERE import_batch_id=?", ("Waiting for extraction retry. Passwords are never stored.", batch_id))
                        audit(conn, "dashboard", "retry_import_batch", "import_batch", batch_id)
                        message = "Extraction retry started"
                        retry_password = resolve_statement_password(
                            conn, batch["source_id"] or "", batch["source_name"] or "", data.get("retry_password") or None
                        ) if batch["source_id"] else (data.get("retry_password") or None)
                        if data.get("retry_password") and data.get("save_password") == "1" and batch["source_id"]:
                            try:
                                encrypted = encrypt_password(data["retry_password"])
                                conn.execute(
                                    """
                                    INSERT INTO account_passwords(source_id, encrypted_password, updated_at) VALUES(?,?,?)
                                    ON CONFLICT(source_id) DO UPDATE SET encrypted_password=excluded.encrypted_password, updated_at=excluded.updated_at
                                    """,
                                    (batch["source_id"], encrypted, now_iso()),
                                )
                                audit(conn, "dashboard", "save_account_password", "source", batch["source_id"], after={"has_password": True, "via": "retry_prompt"})
                            except PasswordKeyMissing:
                                pass
                    elif action == "confirm_pdf_import":
                        if batch["status"] != "pending_review":
                            return self.redirect("/import?toast=" + urllib.parse.quote("This import is no longer pending review."))
                        info = commit_pdf_batch(conn, batch)
                        message = f"Imported {info['added']} transactions. {info['duplicates']} duplicates skipped." if info["duplicates"] else f"Imported {info['added']} transactions."
                    elif action == "cancel_pdf_import":
                        if batch["status"] != "pending_review":
                            return self.redirect("/import?toast=" + urllib.parse.quote("This import is no longer pending review."))
                        conn.execute("UPDATE import_batches SET status='cancelled', notes=? WHERE import_batch_id=?", ("Cancelled by user before import. No transactions were created.", batch_id))
                        audit(conn, "dashboard", "cancel_pdf_import", "import_batch", batch_id)
                        message = "Import cancelled. No transactions were created."
                    else:
                        return self.not_found()
                if action == "retry":
                    start_import_worker(batch_id, retry_password)
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
                            message = f"Imported {info['added']} rows. Duplicates skipped: {info['duplicates']}."
                        elif suffix == ".json":
                            info = import_json(conn, file_bytes, filename, source_name, statement_month, statement_start_date, statement_end_date)
                            seed_removed = info.get("seed_removed", False)
                            message = f"Imported {info['added']} rows. Duplicates skipped: {info['duplicates']}."
                        else:
                            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                            safe_name = f"{int(time.time())}_{slug(filename) or 'statement'}{suffix}"
                            (UPLOAD_DIR / safe_name).write_bytes(file_bytes)
                            batch_id = "batch_" + stable_hash(filename, time.time())
                            source_id = source_id_for(source_name)
                            inline_password = fields.get("pdf_password") or None
                            password = resolve_statement_password(conn, source_id, source_name, inline_password) if source_id else inline_password
                            conn.execute(
                                """
                                INSERT INTO import_batches(import_batch_id, created_at, source_id, source_name, statement_month, statement_start_date, statement_end_date, file_name, file_type, status, notes)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                                """,
                                (batch_id, now_iso(), source_id or None, source_name, statement_month, statement_start_date, statement_end_date, safe_name, suffix.lstrip(".") or "file", "pending_pdf_extraction", "PDF/file stored for statement parser. Password was not stored."),
                            )
                            audit(conn, "dashboard", "upload_pdf_pending_extraction", "import_batch", batch_id, after={"file": safe_name, "source": source_name, "statement_start_date": statement_start_date, "statement_end_date": statement_end_date})
                            if inline_password and fields.get("save_password") == "1" and source_id:
                                try:
                                    encrypted = encrypt_password(inline_password)
                                    conn.execute(
                                        """
                                        INSERT INTO account_passwords(source_id, encrypted_password, updated_at) VALUES(?,?,?)
                                        ON CONFLICT(source_id) DO UPDATE SET encrypted_password=excluded.encrypted_password, updated_at=excluded.updated_at
                                        """,
                                        (source_id, encrypted, now_iso()),
                                    )
                                    audit(conn, "dashboard", "save_account_password", "source", source_id, after={"has_password": True, "via": "import_prompt"})
                                except PasswordKeyMissing:
                                    pass
                            seed_removed = disable_seed_data_after_statement(conn, "dashboard")
                            conn.commit()
                            start_import_worker(batch_id, password)
                            message = "Upload complete. Extraction started automatically; the password was not stored."
                    # Post/Redirect/Get: never render the upload result on the POST response.
                    # A later refresh/poll of that page would re-submit the file (seen as a
                    # ~2 s loop of duplicate batches when extraction failed quickly).
                    if seed_removed:
                        message += " Seed data has been removed from the dashboard."
                    return self.redirect("/import?toast=" + urllib.parse.quote(message) + "&toast_ms=4000")
        options = "".join(f'<option value="{html.escape(s[1])}" {"selected" if s[1] == requested_source else ""}>{html.escape(s[1])}</option>' for s in SOURCES)
        with db() as conn:
            include_seed = seed_data_enabled(conn)
            batches = conn.execute(
                """
                SELECT import_batch_id, created_at, source_name, statement_month, statement_start_date, statement_end_date,
                       file_name, file_type, status, stage, row_count, duplicate_count, notes, excluded_at, reconciliation_status
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
            retry = (f"<form method='post' class='retry-form' data-retry-form><input type='hidden' name='batch_id' value='{html.escape(r['import_batch_id'])}'>"
                     f"<div class='retry-password-row' data-retry-password-row hidden>"
                     f"<input type='password' name='retry_password' placeholder='Statement password' autocomplete='off'>"
                     f"<label class='retry-save-label'><input type='checkbox' name='save_password' value='1'> Save</label></div>"
                     f"<button type='button' class='icon-button retry-password-toggle' data-retry-password-toggle title='Enter a password before retrying' aria-label='Enter a password before retrying' {'disabled' if not retryable else ''}><i data-lucide='key'></i></button>"
                     f"<button class='secondary retry-button' name='action' value='retry' title='{retry_title}' {'disabled' if not retryable else ''}><i data-lucide='rotate-cw'></i><span>Retry</span></button></form>")
            filename = html.escape(display_filename(r), quote=True)
            account = html.escape(r["source_name"] or "Unknown account", quote=True)
            confirmation = html.escape(json.dumps(f"Delete import?\n\nArchive {display_filename(r)} from {r['source_name'] or 'this account'}?\n\nThis cannot be undone. The uploaded file will be deleted."), quote=True)
            delete = (f"<form method='post' onsubmit=\"return confirm({confirmation})\"><input type='hidden' name='batch_id' value='{html.escape(r['import_batch_id'])}'>"
                      f"<button class='delete-import' name='action' value='delete' title='Delete import' aria-label='Delete import: {filename} from {account}'><i data-lucide='trash-2'></i></button></form>")
            return f"<div class='import-actions'>{retry}{delete}</div>"
        def import_row(r):
            filename = display_filename(r)
            source = r["source_name"] or "Unknown account"
            source_meta = "Credit card" if "card" in source.lower() or "diners" in source.lower() else ("Bank account" if "bank" in source.lower() else "")
            account_meta = f'<small class="account-meta">{html.escape(source_meta)}</small>' if source_meta else ""
            rows_value = str(r["row_count"] if r["row_count"] is not None else 0) if r["status"] in successful_statuses else "—"
            return (
            f"<tr data-import-status='{html.escape(r['status'])}' data-batch-id='{html.escape(r['import_batch_id'])}'><td class='file-cell'><span class='file-inner'><i data-lucide='file-text'></i><span title='{html.escape(filename, quote=True)}'>{html.escape(filename)}</span></span></td>"
            f"<td class='account-cell'><strong>{html.escape(source)}</strong>{account_meta}</td>"
            f"<td class='status-cell'><span class='status-wrap'>{render_import_status_chip(r['status'], r['stage'] if 'stage' in r.keys() else None)}{render_import_detail(r['notes'])}</span></td>"
            f"<td class='right amount rows-cell'>{rows_value}</td><td class='imported-cell'>{html.escape(human_datetime(r['created_at']))}</td>"
            f"<td>{included_control(r)}</td><td>{import_actions(r)}</td></tr>"
            )
        def import_summary_card(r):
            chip_class = {"ok": "green", "failed": "red", "unavailable": "blue"}.get(r["reconciliation_status"], "blue")
            chip_label = {"ok": "Balance check ✓", "failed": "Balance check ✗", "unavailable": "Balance check: not available"}.get(r["reconciliation_status"], "Balance check: not available")
            return f'''<div class="card import-summary-card">
              <h2>Import summary</h2>
              <p class="import-summary-text">{html.escape(r["notes"] or "")}</p>
              <span class="pill {chip_class}">{html.escape(chip_label)}</span>
              <div class="import-summary-actions">
                <form method="post"><input type="hidden" name="batch_id" value="{html.escape(r['import_batch_id'])}"><button name="action" value="confirm_pdf_import">Import {r['row_count'] or 0} transactions</button></form>
                <form method="post"><input type="hidden" name="batch_id" value="{html.escape(r['import_batch_id'])}"><button class="secondary" name="action" value="cancel_pdf_import">Cancel</button></form>
              </div>
            </div>'''
        summary_cards = "".join(import_summary_card(r) for r in batches if r["status"] == "pending_review")
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
            <div class="span-2"><label>PDF password</label><input type="password" name="pdf_password" placeholder="Only used if this bank has no stored password" autocomplete="off"></div>
            <div class="align-end save-password-cell"><label class="save-password-check"><input type="checkbox" name="save_password" value="1"><i data-lucide="key-round"></i><span>Save this password for this account</span></label></div>
            <div class="form-actions"><button>Import statement</button></div>
          </form>
        </div>
        {summary_cards}
        <div class="card section-gap"><div class="import-head"><div><h2>Import history</h2><p class="muted">Manage imported statements and retry extraction when needed.</p><p class="muted import-explainer">Excluded imports remain stored but aren't included in calculations, transactions, or review.</p></div><form method="post"><button class="secondary" name="action" value="process_pending">Process pending imports</button></form></div>
          {f'<div class="table-scroll"><table class="keep-table import-history"><colgroup><col class="col-file"><col class="col-account"><col class="col-status"><col class="col-rows"><col class="col-imported"><col class="col-included"><col class="col-actions"></colgroup><thead><tr><th>File</th><th>Account</th><th>Status</th><th class="right">Rows</th><th>Imported</th><th>Included</th><th>Actions</th></tr></thead><tbody>{batch_rows}</tbody></table></div>' if batches else empty_state('upload', 'No statements imported yet. Use the form above to add one.')}
        </div>
        <style>.import-card{{padding-top:var(--sp-4);padding-bottom:var(--sp-4)}}.import-card h2{{margin-bottom:var(--sp-2)}}.import-card .form-grid{{margin-top:var(--sp-3)}}.import-card .form-actions{{grid-column:1/-1}}.import-card .save-password-cell{{display:flex;align-items:center;min-height:40px}}.import-card .save-password-check{{display:inline-flex;align-items:center;gap:6px;margin:0;font-size:var(--text-caption);font-weight:500;color:var(--ink-600);cursor:pointer;white-space:nowrap}}.import-card .save-password-check input[type=checkbox]{{width:16px;height:16px;min-height:0;padding:0;margin:0;flex:none;accent-color:var(--brand-700);cursor:pointer}}.import-card .save-password-check svg{{width:14px;height:14px;color:var(--brand-700);flex:none}}.import-summary-card{{margin-bottom:var(--sp-5)}}.import-summary-card h2{{margin-bottom:var(--sp-2)}}.import-summary-text{{font:600 var(--text-body)/1.5 var(--font-num);margin:0 0 var(--sp-3)}}.import-summary-actions{{display:flex;gap:var(--sp-2);margin-top:var(--sp-4)}}.import-summary-actions form{{margin:0}}.import-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:var(--sp-3);margin-bottom:var(--sp-2)}}.import-head h2,.import-head p{{margin:0 0 var(--sp-1)}}.import-explainer{{font-size:var(--text-caption)}}.import-history{{min-width:1080px;table-layout:fixed}}.col-file{{width:20%}}.col-account{{width:17%}}.col-status{{width:19%}}.col-rows{{width:6%}}.col-imported{{width:14%}}.col-included{{width:10%}}.col-actions{{width:14%}}.import-history td{{vertical-align:middle;padding-top:var(--sp-2);padding-bottom:var(--sp-2)}}.file-inner{{display:flex;align-items:center;gap:var(--sp-2);min-width:0}}.file-inner svg{{width:18px;flex:none;color:var(--ink-400)}}.file-inner>span{{display:block;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.account-cell strong{{display:block;font-weight:600}}.account-meta{{display:block;margin-top:3px;color:var(--ink-400);font-size:var(--text-caption);font-weight:400;line-height:1.3}}.status-wrap{{display:inline-flex;align-items:center;gap:var(--sp-1);position:relative}}.status-badge{{white-space:nowrap}}{IMPORT_STATUS_CSS}.status-info{{position:relative;display:inline-flex}}.status-info-btn{{width:26px;height:26px;padding:0;display:grid;place-items:center;background:transparent;color:var(--ink-400);border:0;border-radius:50%;cursor:pointer;min-height:0}}.status-info-btn:hover,.status-info-btn:focus-visible{{background:var(--brand-050);color:var(--brand-700)}}.status-info-btn svg{{width:16px;height:16px}}.status-pop{{display:none;position:absolute;left:0;top:calc(100% + 6px);z-index:6;min-width:260px;max-width:340px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-md);box-shadow:var(--shadow-hover);padding:var(--sp-3);font-size:var(--text-caption);color:var(--ink-900);white-space:normal;text-align:left}}.status-info:hover .status-pop,.status-info:focus-within .status-pop,.status-info.open .status-pop{{display:block}}.status-pop ul{{margin:0;padding-left:18px;display:grid;gap:4px}}.status-pop p{{margin:0;line-height:1.4}}.rows-cell,.imported-cell{{white-space:nowrap}}.included-form,.import-actions{{display:flex;align-items:center;gap:var(--sp-2);margin:0;white-space:nowrap}}.included-label{{min-width:57px;font-size:var(--text-caption);font-weight:600}}.import-switch{{position:relative;display:block;cursor:pointer}}.import-switch input{{position:absolute;opacity:0}}.import-switch>span:not(.sr-only){{display:block;width:36px;height:20px;padding:2px;border-radius:var(--radius-full);background:var(--ink-400);transition:background 150ms}}.import-switch>span:not(.sr-only):after{{content:'';display:block;width:16px;height:16px;border-radius:50%;background:var(--surface);transition:transform 150ms}}.import-switch input:checked+span{{background:var(--brand-700)}}.import-switch input:checked+span:after{{transform:translateX(16px)}}.import-switch input:disabled+span{{background:var(--border);box-shadow:inset 0 0 0 1px var(--ink-400)}}.import-switch:has(input:disabled){{cursor:not-allowed;opacity:.72}}.import-switch input:focus-visible+span{{outline:2px solid rgba(18,138,99,.4);outline-offset:2px}}.sr-only{{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}.import-actions form{{margin:0}}.import-actions button{{min-height:32px;font-size:var(--text-caption)}}.retry-button{{display:inline-flex;align-items:center;gap:var(--sp-1);padding:var(--sp-1) var(--sp-2)}}.retry-button svg{{width:15px}}.retry-button:disabled{{opacity:.42;cursor:not-allowed;background:var(--page-bg);color:var(--ink-400);border-color:var(--border)}}.delete-import{{width:32px;min-height:32px;padding:0;display:grid;place-items:center;background:transparent;color:var(--ink-400)}}.delete-import svg{{width:17px}}.delete-import:hover,.delete-import:focus-visible{{background:var(--dang-100);color:var(--dang-700)}}.retry-password-toggle{{width:32px;height:32px}}.retry-form{{position:relative}}.retry-password-row{{display:flex;align-items:center;gap:var(--sp-2);position:absolute;right:0;bottom:calc(100% + 6px);z-index:5;background:var(--surface);box-shadow:var(--shadow-hover);border:1px solid var(--border);border-radius:var(--radius-md);padding:var(--sp-2)}}.retry-password-row[hidden]{{display:none}}.retry-password-row input[type=password]{{min-height:32px;font-size:var(--text-caption);width:150px}}.retry-save-label{{display:flex;align-items:center;gap:4px;font-size:var(--text-caption);white-space:nowrap}}@media(max-width:767px){{.import-head{{flex-direction:column}}.keep-table{{min-width:1080px}}.table-scroll{{overflow-x:auto}}}}</style>
        <script>(()=>{{
          document.querySelectorAll('.status-info-btn').forEach(btn=>btn.onclick=e=>{{e.stopPropagation();const wrap=btn.parentElement,open=!wrap.classList.contains('open');document.querySelectorAll('.status-info.open').forEach(w=>{{w.classList.remove('open');w.querySelector('button').setAttribute('aria-expanded','false')}});wrap.classList.toggle('open',open);btn.setAttribute('aria-expanded',String(open))}});document.addEventListener('click',()=>document.querySelectorAll('.status-info.open').forEach(w=>{{w.classList.remove('open');w.querySelector('button').setAttribute('aria-expanded','false')}}));
          document.querySelectorAll('[data-retry-password-toggle]').forEach(btn=>btn.onclick=()=>{{const row=btn.closest('form').querySelector('[data-retry-password-row]');row.hidden=!row.hidden}});
          document.querySelectorAll('.retry-form').forEach(form=>form.addEventListener('submit',()=>{{const button=form.querySelector('.retry-button');if(button.disabled)return;button.disabled=true;button.querySelector('span').textContent='Retrying…';const row=form.closest('tr'),badge=row.querySelector('.status-badge');if(badge){{badge.className='chip status-chip status-extracting status-badge';badge.querySelector('.status-label').textContent='Processing…'}}row.querySelector('.status-info')?.remove()}}));
          const stageLabels={{decrypting:'Reading statement…',parsing:'Extracting transactions…',extracting:'Extracting transactions…',validating:'Checking balances…'}};
          const inFlightRows=[...document.querySelectorAll('tr[data-import-status="pending_pdf_extraction"],tr[data-import-status="extracting"]')];
          if(inFlightRows.length){{
            const poll=async()=>{{
              let anyInFlight=false;
              for(const row of inFlightRows){{
                try{{
                  const res=await fetch('/import/status?batch_id='+encodeURIComponent(row.dataset.batchId),{{credentials:'same-origin'}});
                  if(!res.ok)continue;
                  const data=await res.json();
                  if(data.status==='pending_pdf_extraction'||data.status==='extracting'){{
                    anyInFlight=true;
                    const badge=row.querySelector('.status-badge');
                    if(badge){{badge.className='chip status-chip status-extracting status-badge';const l=badge.querySelector('.status-label');if(l)l.textContent=stageLabels[data.stage]||'Processing…'}}
                  }}
                }}catch(e){{}}
              }}
              if(anyInFlight)setTimeout(poll,2000);
              else{{const u=new URL(location.href);u.searchParams.delete('toast');u.searchParams.delete('toast_ms');location.replace(u.pathname+u.search)}}
            }};
            setTimeout(poll,2000);
          }}
        }})();</script>
        """
        self.send_html("Import", body)

    def api(self, path: str):
        if not self.is_authed():
            self.send_response(401)
            self.end_headers()
            return
        with db() as conn:
            if path == "/api/rule-conflicts":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                txid = (query.get("transaction_id") or [""])[0]
                eff_sql, eff_args = effective_tx_sql(None, True, ("t.transaction_id=?",), (txid,))
                row = conn.execute(eff_sql, eff_args).fetchone()
                if not row:
                    return self.json_response({"error": "Transaction not found"}, 404)
                pattern = row["merchant_payee"] or row["description"]
                conflicts = rule_conflicts(conn, pattern, row["source_name"], category=(query.get("category") or [""])[0].strip() or None, subcategory=(query.get("subcategory") or [""])[0].strip() or None)
                return self.json({"conflicts": conflicts, "pattern": pattern})
            if path == "/api/sankey":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                month = dashboard_month(conn, (query.get("month") or [None])[0])
                data = dashboard_data(conn, month)
                s = data["summary"]
                flows = data["category_flows"]
                inflow = s["total_inflow"]
                outflow = sum(f["value"] for f in flows)
                shortfall = max(0, outflow - inflow)
                # Left column: total inflow (+ a shortfall source in deficit months, since d3-sankey
                # would otherwise inflate the inflow node to the sum of its outgoing links).
                # Right column: one node per category (netted spend) plus surplus.
                sources = data["income_flows"] or ([{"name": "Total inflow", "value": inflow, "color": "--viz-inflow", "category": None}] if inflow > 0 else [])
                nodes = [{"name": src["name"], "color": None if str(src["color"] or "").startswith("--") else src["color"], "cssVar": src["color"] if str(src["color"] or "").startswith("--") else None, "kind": "income", "category": src["category"]} for src in sources]
                nodes.append({"name": SHORTFALL_LABEL, "cssVar": "--viz-shortfall", "kind": "shortfall"})
                nodes += [{"name": f["name"], "color": f["color"], "kind": "spend", "category": UNCATEGORISED_FILTER if f["name"] == UNCATEGORISED_LABEL else f["name"]} for f in flows]
                nodes.append({"name": "Surplus / unallocated", "cssVar": "--viz-surplus", "kind": "surplus"})
                links = []
                # Each income source funds every category (and the surplus) in proportion to its share of inflow,
                # so category totals stay exact and each source node keeps its true value.
                income_share = inflow / outflow if (shortfall > 0 and outflow) else 1.0
                for src in sources:
                    src_share = (src["value"] / inflow) if inflow else 0
                    for f in flows:
                        value = f["value"] * income_share * src_share
                        if value > 0:
                            links.append({"source": src["name"], "target": f["name"], "value": value})
                    if shortfall <= 0 and s["surplus"] > 0:
                        links.append({"source": src["name"], "target": "Surplus / unallocated", "value": s["surplus"] * src_share})
                if shortfall > 0:
                    for f in flows:
                        links.append({"source": SHORTFALL_LABEL, "target": f["name"], "value": f["value"] * (1 - income_share)})
                if not links:
                    # Nothing this month: draw a single hairline so the layout doesn't collapse.
                    nodes.insert(0, {"name": "Total inflow", "cssVar": "--viz-inflow", "kind": "income", "category": None})
                    links.append({"source": "Total inflow", "target": "Surplus / unallocated", "value": 1})
                used = {lk["source"] for lk in links} | {lk["target"] for lk in links}
                nodes = [n for n in nodes if n["name"] in used]
                summary = {"inflow": inflow, "outflow": outflow, "shortfall": shortfall, "surplus": s["surplus"]}
                return self.json({"nodes": nodes, "links": links, "summary": summary})
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


def subcategory_control(category_field: str, name: str, selected: str | None, extra: dict[str, list[str]] | None = None) -> str:
    """Dependent subcategory <select>: options carry data-parent and are filtered client-side by the
    sibling category <select name=category_field>. Includes an "Other…" option that reveals a text input.
    `extra` adds already-used subcategories per parent that aren't in SUBCATEGORIES."""
    selected = selected or ""
    listed: set[str] = set()
    opts = ["<option value=''>Any / none</option>"]
    for parent in PARENT_CATEGORIES:
        subs = list(SUBCATEGORIES.get(parent, []))
        for s in (extra or {}).get(parent, []):
            if s and s not in subs:
                subs.append(s)
        for sub in subs:
            listed.add(sub)
            mark = " selected" if sub == selected else ""
            opts.append(f'<option value="{html.escape(sub, quote=True)}" data-parent="{html.escape(parent, quote=True)}"{mark}>{html.escape(sub)}</option>')
    custom_selected = bool(selected) and selected not in listed
    opts.append(f'<option value="{SUBCATEGORY_CUSTOM}"{" selected" if custom_selected else ""}>Other…</option>')
    custom_value = html.escape(selected, quote=True) if custom_selected else ""
    return (
        f'<span class="subcat-wrap"><select name="{html.escape(name, quote=True)}" data-category-field="{html.escape(category_field, quote=True)}" data-subcategory-select>'
        + "".join(opts) + "</select>"
        + f'<input name="{html.escape(name, quote=True)}_custom" placeholder="New subcategory" value="{custom_value}"{"" if custom_selected else " hidden"}></span>'
    )


SUBCATEGORY_CONTROL_SCRIPT = """
<script>
(()=>{document.querySelectorAll('[data-subcategory-select]').forEach(sel=>{const form=sel.closest('form');const cat=form&&form.querySelector(`[name="${sel.dataset.categoryField}"]`);const custom=sel.parentElement.querySelector('input');
function sync(focus){const parent=cat?cat.value:'';[...sel.options].forEach(o=>{if(!o.dataset.parent)return;const show=!parent||o.dataset.parent===parent;o.hidden=!show;if(!show&&o.selected)sel.value=''});custom.hidden=sel.value!=='__custom__';if(focus&&!custom.hidden)custom.focus()}
if(cat)cat.addEventListener('change',()=>{sel.value='';sync(false)});sel.addEventListener('change',()=>sync(true));sync(false)})})();
</script>"""


def resolve_subcategory(data: dict, name: str = "subcategory") -> str | None:
    """Value of a subcategory_control() field: the chosen option, or the custom text when 'Other…' was picked."""
    value = (data.get(name) or "").strip()
    if value == SUBCATEGORY_CUSTOM:
        value = (data.get(f"{name}_custom") or "").strip()
    return value or None


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


def start_auto_reload(interval: float = 1.0) -> None:
    """Dev helper: restart the process in place whenever app.py changes on disk.

    Opt-in via KANAKKU_RELOAD=1 or `python app.py --reload`; a plain http.server never
    reloads code by itself, so edits are otherwise invisible until a manual restart."""
    source = Path(__file__).resolve()
    started = source.stat().st_mtime

    def watch() -> None:
        while True:
            time.sleep(interval)
            try:
                if source.stat().st_mtime != started:
                    print("app.py changed — restarting", flush=True)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
            except FileNotFoundError:
                continue

    threading.Thread(target=watch, name="auto-reload", daemon=True).start()
    print("Auto-reload on: the server restarts when app.py changes.")


def run() -> None:
    init_db()
    start_import_worker()
    if env("KANAKKU_RELOAD", "0") == "1" or "--reload" in sys.argv[1:]:
        start_auto_reload()
    port = int(env("KANAKKU_PORT", "5010"))
    print(f"Varavu.Selavu running on http://0.0.0.0:{port}")
    print(f"Login user: {APP_USER} | Set KANAKKU_PASSWORD before real use.")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "chat":
        raise SystemExit(chat_command(sys.argv[2:]))
    run()

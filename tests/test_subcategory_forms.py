"""Subcategory pickers on Review / Rules / Baselines persist what the user chose."""
from __future__ import annotations

import json
import re
import urllib.parse

import app as app_module
from tests.conftest import get, login, post


def _open_review_txn(desc_fragment: str) -> str:
    with app_module.db() as conn:
        row = conn.execute(
            "SELECT t.transaction_id FROM transactions t JOIN review_items ri ON ri.transaction_id=t.transaction_id "
            "WHERE ri.status='open' AND t.description LIKE ? LIMIT 1", (f"%{desc_fragment}%",)).fetchone()
    assert row, f"no open review item for {desc_fragment}"
    return row[0]


def test_review_page_has_subcategory_picker_and_categories_are_parents_only(seeded_server):
    html = get(seeded_server, "/review", login(seeded_server))
    assert 'id="subcategory-input"' in html
    assert 'name="subcategory"' in html
    m = re.search(r"categories=(\[.*?\]),subcategories=(\{.*?\}),drawer=", html)
    assert m, "review script should expose categories + subcategories"
    categories = json.loads(m.group(1))
    subcategories = json.loads(m.group(2))
    assert "Food Delivery" not in categories  # no longer mixed into the category list
    assert set(app_module.PARENT_CATEGORIES) <= set(categories)
    assert "Cab" in subcategories["Transport"]


def test_review_approve_persists_subcategory_on_override_and_remembered_rule(seeded_server):
    cookie = login(seeded_server)
    txid = _open_review_txn("Amazon Seller Services")
    resp = post(seeded_server, "/review", cookie,
                {"transaction_id": txid, "action": "approve", "category": "Shopping", "subcategory": "Online / Amazon", "remember": "yes", "notes": ""},
                headers={"Accept": "application/json"})
    assert resp.status == 200
    with app_module.db() as conn:
        ov = conn.execute("SELECT category, subcategory FROM manual_overrides WHERE transaction_id=? ORDER BY created_at DESC LIMIT 1", (txid,)).fetchone()
        assert (ov["category"], ov["subcategory"]) == ("Shopping", "Online / Amazon")
        rule = conn.execute("SELECT category, subcategory FROM rules WHERE name LIKE 'Remember %Amazon%'").fetchone()
        assert (rule["category"], rule["subcategory"]) == ("Shopping", "Online / Amazon")


def test_rules_form_custom_subcategory_and_dropdown(seeded_server):
    cookie = login(seeded_server)
    html = get(seeded_server, "/rules", cookie)
    assert 'data-parent="Transport"' in html and 'value="__custom__"' in html
    resp = post(seeded_server, "/rules", cookie,
                {"name": "Auto rides", "match_type": "description_contains", "pattern": "RAPIDO", "category": "Transport",
                 "subcategory": "__custom__", "subcategory_custom": "Auto rickshaw", "classification": "controllable", "flow_type": "spend", "confidence": "0.8"})
    assert resp.status == 302
    with app_module.db() as conn:
        assert conn.execute("SELECT subcategory FROM rules WHERE name='Auto rides'").fetchone()[0] == "Auto rickshaw"
    resp = post(seeded_server, "/rules", cookie,
                {"name": "Cabs", "match_type": "description_contains", "pattern": "SHOFFR", "category": "Transport",
                 "subcategory": "Cab", "classification": "controllable", "flow_type": "spend", "confidence": "0.8"})
    assert resp.status == 302
    with app_module.db() as conn:
        assert conn.execute("SELECT subcategory FROM rules WHERE name='Cabs'").fetchone()[0] == "Cab"


def test_baselines_form_subcategory_dropdown_persists(seeded_server):
    cookie = login(seeded_server)
    html = get(seeded_server, "/baselines", cookie)
    assert 'data-parent="Food"' in html
    resp = post(seeded_server, "/baselines", cookie, {"category": "Food", "subcategory": "Food Delivery", "amount": "5000", "effective_month": "2026-06"})
    assert resp.status == 302
    with app_module.db() as conn:
        row = conn.execute("SELECT scope, subcategory FROM baselines WHERE category='Food' AND subcategory='Food Delivery'").fetchone()
        assert (row["scope"], row["subcategory"]) == ("subcategory", "Food Delivery")


def test_rules_page_reapply_button_updates_transactions(seeded_server):
    cookie = login(seeded_server)
    assert "Re-apply rules" in get(seeded_server, "/rules", cookie)
    with app_module.db() as conn:
        conn.execute("UPDATE transactions SET category=NULL, subcategory=NULL, rule_id=NULL, confidence=0.2 WHERE description='Swiggy Instamart'")
        conn.commit()
    resp = post(seeded_server, "/rules", cookie, {"action": "reapply"})
    assert resp.status == 302 and "Re-applied" in urllib.parse.unquote(resp.headers.get("Location", ""))
    with app_module.db() as conn:
        row = conn.execute("SELECT category, subcategory FROM transactions WHERE description='Swiggy Instamart'").fetchone()
        assert (row["category"], row["subcategory"]) == ("Groceries & Household", "Groceries")


def test_page_skeleton_ignores_fetch_submitted_forms(seeded_server):
    """The Review drawer submits via fetch (preventDefault); the shared page-transition script must
    not hide <main> behind the loading skeleton for such submits (regression: blank page after Approve)."""
    html = get(seeded_server, "/review", login(seeded_server))
    assert "document.addEventListener('submit',e=>{if(!e.defaultPrevented)scheduleSkeleton()})" in html
    assert "setTimeout(()=>document.body.classList.remove('loading-skeleton'),3000)" in html


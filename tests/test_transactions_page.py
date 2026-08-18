"""Category chips and the /transactions filter bar."""
from __future__ import annotations

import html as html_mod
import json
import re

import app as app_module
from tests.conftest import get, login


# --- chip renderers -----------------------------------------------------------

def test_category_chip_known_category_has_slug_icon_and_link():
    chip = app_module.render_category_chip("Food")
    assert 'class="chip cat-chip cat-food"' in chip
    assert 'data-lucide="utensils"' in chip
    assert 'href="/transactions?category=Food"' in chip
    assert ">Food<" in chip


def test_category_chip_uncategorised_is_dashed_and_filters_none():
    chip = app_module.render_category_chip(None)
    assert "cat-none" in chip
    assert "category=__none__" in chip
    assert "Uncategorised" in chip


def test_category_chip_unknown_category_falls_back_to_neutral():
    chip = app_module.render_category_chip("Something New")
    assert "cat-other" in chip
    assert 'data-lucide="tag"' in chip
    assert "Something New" in chip


def test_category_chip_subcategory_and_no_link():
    chip = app_module.render_category_chip("Food", "Dining & Delivery", link=False)
    assert chip.startswith("<span")
    assert "Dining &amp; Delivery" in chip
    assert "href=" not in chip


def test_kind_chip_for_classification_and_flow():
    assert 'data-lucide="lock"' in app_module.render_kind_chip("classification", "fixed")
    assert "Money out" in app_module.render_kind_chip("flow", "spend")
    assert app_module.render_kind_chip("classification", None) == ""


# --- /transactions filters ------------------------------------------------------

def _rows(html: str) -> list[str]:
    return re.findall(r"<tr class=\"tx-row\"[^>]*>(.*?)</tr>", html, flags=re.S)


def test_transactions_page_has_filter_bar_and_keep_table(seeded_server):
    html = get(seeded_server, "/transactions?month=all", login(seeded_server))
    assert 'class="tx-filters"' in html
    assert "keep-table" in html
    assert len(_rows(html)) == 16


def test_filter_by_category(seeded_server):
    cookie = login(seeded_server)
    html = get(seeded_server, "/transactions?month=all&category=Food", cookie)
    rows = _rows(html)
    assert len(rows) == 1
    assert all("cat-food" in r for r in rows)
    assert "Category: Food" in html  # active filter chip
    html = get(seeded_server, "/transactions?month=all&category=Groceries+%26+Household", cookie)
    rows = _rows(html)
    assert len(rows) == 1
    assert all("cat-groceries" in r for r in rows)


def test_filter_uncategorised(seeded_server):
    html = get(seeded_server, "/transactions?month=all&category=__none__", login(seeded_server))
    rows = _rows(html)
    assert len(rows) == 3
    assert all("cat-none" in r for r in rows)


def test_filter_search_and_source_combine(seeded_server):
    cookie = login(seeded_server)
    assert len(_rows(get(seeded_server, "/transactions?month=all&q=swiggy", cookie))) == 1
    assert len(_rows(get(seeded_server, "/transactions?month=all&source=Vignesh+Kotak+Bank", cookie))) == 4
    assert len(_rows(get(seeded_server, "/transactions?month=all&source=Vignesh+Kotak+Bank&flow=fee", cookie))) == 1
    assert len(_rows(get(seeded_server, "/transactions?month=2026-04", cookie))) == 0
    assert "No transactions match" in get(seeded_server, "/transactions?month=2026-04", cookie)


# --- revamp: default month, new filters, chips, breakdown, paging, drawer edit ------------

def _hrefs(html: str, cls: str) -> list[str]:
    return re.findall(rf'<a class="{cls}[^"]*" href="([^"]+)"', html)


def test_default_month_follows_dashboard_session(seeded_server):
    cookie = login(seeded_server)
    # Fresh session: last completed month, which has no seed rows.
    html = get(seeded_server, "/transactions", cookie)
    assert len(_rows(html)) == 0 and "No transactions match" in html
    assert 'value="all">All months' in html
    # Dashboard month navigation sets the session month; Transactions follows it.
    get(seeded_server, "/?month=2026-05", cookie)
    html = get(seeded_server, "/transactions", cookie)
    assert len(_rows(html)) == 16
    assert "Month: May 2026" in html
    assert len(_rows(get(seeded_server, "/transactions?month=2026-05", cookie))) == 16


def test_subcategory_and_payer_filters_and_chip_removal(seeded_server):
    cookie = login(seeded_server)
    html = get(seeded_server, "/transactions?month=all&category=Food&subcategory=Food+Delivery&payer=Vignesh", cookie)
    assert len(_rows(html)) == 1
    assert "Subcategory: Food Delivery" in html and "Payer: Vignesh" in html
    chips = _hrefs(html, "chip filter-chip")
    # Removing the subcategory chip keeps month/category/payer.
    sub_chip = next(h for h in chips if "subcategory" not in h and "category=Food" in h)
    assert "month=all" in sub_chip and "payer=Vignesh" in sub_chip
    # Removing the category chip drops subcategory too.
    cat_chip = next(h for h in chips if "category" not in h)
    assert "payer=Vignesh" in cat_chip and "month=all" in cat_chip
    assert len(_rows(get(seeded_server, "/transactions?month=all&payer=Jananiya", cookie))) == 5
    # Subcategory without a category is ignored.
    assert len(_rows(get(seeded_server, "/transactions?month=all&subcategory=Food+Delivery", cookie))) == 16


def test_breakdown_panel_lists_all_categories_and_marks_active(seeded_server):
    cookie = login(seeded_server)
    with app_module.db() as conn:
        expected = app_module.breakdown(conn, None, {}, "category")
    html = get(seeded_server, "/transactions?month=all", cookie)
    assert 'data-breakdown-view="category"' in html
    groups = re.findall(r'<div class="bd-group[^"]*" data-key="([^"]+)"', html)
    assert [html_mod.unescape(g) for g in groups] == [i["key"] for i in expected]
    # Filtering by Food keeps every row but marks Food active + expanded with subcategory links.
    html = get(seeded_server, "/transactions?month=all&category=Food", cookie)
    assert re.findall(r'<div class="bd-group[^"]*" data-key="([^"]+)"', html) == groups
    food = re.search(r'<div class="bd-group([^"]*)" data-key="Food">(.*?)<div class="bd-children">(.*?)</div></div>', html, flags=re.S)
    assert food and "expanded" in food.group(1) and "bd-parent active" in food.group(2)
    assert "subcategory=Food+Delivery" in food.group(3)
    # Clicking the active row again removes the filter.
    active_href = re.search(r'<div class="bd-row bd-parent active">.*?<a class="bd-main" href="([^"]+)"', html, flags=re.S).group(1)
    assert "category=" not in active_href
    # Account + person views.
    html = get(seeded_server, "/transactions?month=all&view=account", cookie)
    assert 'data-breakdown-view="account"' in html
    with app_module.db() as conn:
        accounts = app_module.breakdown(conn, None, {}, "account")
    assert len(re.findall(r'<div class="bd-group', html)) == len(accounts)
    html = get(seeded_server, "/transactions?month=all&view=person", cookie)
    assert "Vignesh" in html and 'data-breakdown-view="person"' in html
    # Income-only filter -> empty panel with explanation.
    html = get(seeded_server, "/transactions?month=all&flow=income", cookie)
    assert "Nothing to break down" in html


def test_pagination_and_sorting(server):
    cookie = login(server)
    with app_module.db() as conn:
        conn.execute("INSERT INTO import_batches(import_batch_id, created_at, status) VALUES('b1', ?, 'imported')", (app_module.now_iso(),))
        for i in range(60):
            ok, _ = app_module.insert_transaction(conn, "b1", None, {"source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-10", "description": f"ROW {i}", "amount": -(i + 1), "category": "Food", "classification": "controllable", "flow_type": "spend"}, create_review=False)
            assert ok
    html = get(server, "/transactions?month=2026-06", cookie)
    assert len(_rows(html)) == 50 and "Showing 1–50 of 60" in html
    html2 = get(server, "/transactions?month=2026-06&page=2", cookie)
    assert len(_rows(html2)) == 10 and "Showing 51–60 of 60" in html2
    assert len(_rows(get(server, "/transactions?month=2026-06&page=999", cookie))) == 10  # clamped to last page
    html = get(server, "/transactions?month=2026-06&sort=amount&dir=desc", cookie)
    assert "ROW 59" in _rows(html)[0]
    assert 'aria-sort="descending"' in html
    html = get(server, "/transactions?month=2026-06&sort=amount&dir=asc", cookie)
    assert "ROW 0<" in _rows(html)[0]


def test_row_edit_via_review_endpoint_from_transactions(seeded_server):
    from tests.conftest import post
    cookie = login(seeded_server)
    html = get(seeded_server, "/transactions?month=all&category=Food&subcategory=Food+Delivery", cookie)
    txid = re.search(r'data-tx-id="([^"]+)"', html).group(1)
    assert 'id="tx-form"' in html and 'name="origin" value="transactions"' in html
    resp = post(seeded_server, "/review", cookie, {"transaction_id": txid, "action": "approve", "category": "Travel", "subcategory": "Vacation", "classification": "one_off", "notes": "trip", "origin": "transactions"}, headers={"X-Requested-With": "fetch"})
    assert resp.status == 200
    payload = json.loads(resp.read().decode())
    assert payload["ok"] and "cat-travel" in payload["row"]["category_chip"] and "One off" in payload["row"]["classification_chip"]
    html = get(seeded_server, "/transactions?month=all&category=Travel", cookie)
    row = next(r for r in _rows(html) if "Vacation" in r)
    assert "One off" in row
    # Second edit with only notes keeps the earlier classification and clears the subcategory (explicit empty).
    resp = post(seeded_server, "/review", cookie, {"transaction_id": txid, "action": "approve", "category": "Travel", "notes": "again", "origin": "transactions"}, headers={"X-Requested-With": "fetch"})
    assert resp.status == 200
    with app_module.db() as conn:
        row = next(r for r in app_module.effective_transactions(conn) if r["transaction_id"] == txid)
    assert row["classification"] == "one_off" and row["subcategory"] is None and row["notes"] == "again"
    # Remember -> rule created; review page still works.
    resp = post(seeded_server, "/review", cookie, {"transaction_id": txid, "action": "approve", "category": "Travel", "remember": "yes", "origin": "transactions"}, headers={"X-Requested-With": "fetch"})
    assert json.loads(resp.read().decode())["rule_id"]
    assert "review-drawer" in get(seeded_server, "/review", cookie)


# --- drawer combobox affordance + "remember" conflict preview / supersede -----------------

def test_drawer_combobox_shows_full_list_on_exact_match_and_has_caret(seeded_server):
    cookie = login(seeded_server)
    for path in ("/transactions?month=all", "/review"):
        html = get(seeded_server, path, cookie)
        assert "exactMatch" in html, path
        assert html.count('class="combo-caret"') == 2, path
        assert "inp.select()" in html or "input.select()" in html, path


def test_rule_conflicts_api_and_supersede_on_remember(seeded_server):
    from tests.conftest import post
    cookie = login(seeded_server)
    html = get(seeded_server, "/transactions?month=all&q=swiggy", cookie)
    txid = re.search(r'data-tx-id="([^"]+)"', html).group(1)
    # Preview: the built-in Swiggy/Zomato keyword rule matches; a different category is a real conflict.
    data = json.loads(get(seeded_server, f"/api/rule-conflicts?transaction_id={txid}&category=Shopping", cookie))
    assert data["conflicts"] and any(not c["same_outcome"] and not c["remembered"] for c in data["conflicts"])
    same = json.loads(get(seeded_server, f"/api/rule-conflicts?transaction_id={txid}&category=Food&subcategory=Food+Delivery", cookie))
    assert any(c["same_outcome"] for c in same["conflicts"])
    assert get_status(seeded_server, "/api/rule-conflicts?transaction_id=nope", cookie) == 404
    # Remember as Shopping, then as Travel: the second call supersedes the first remembered rule.
    r1 = json.loads(post(seeded_server, "/review", cookie, {"transaction_id": txid, "action": "approve", "category": "Shopping", "remember": "yes", "origin": "transactions"}, headers={"X-Requested-With": "fetch"}).read().decode())
    assert r1["rule_id"] and r1["superseded"] == [] and r1["conflicts"]
    r2 = json.loads(post(seeded_server, "/review", cookie, {"transaction_id": txid, "action": "approve", "category": "Travel", "remember": "yes", "origin": "transactions"}, headers={"X-Requested-With": "fetch"}).read().decode())
    assert r2["superseded"] == [r1["rule_id"]] and r2["rule_id"] != r1["rule_id"]
    assert any(c["remembered"] and c["rule_id"] == r1["rule_id"] for c in r2["conflicts"])
    with app_module.db() as conn:
        assert conn.execute("SELECT enabled FROM rules WHERE rule_id=?", (r1["rule_id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT enabled FROM rules WHERE rule_id=?", (r2["rule_id"],)).fetchone()[0] == 1
        row = conn.execute("SELECT description, source_name FROM transactions WHERE transaction_id=?", (txid,)).fetchone()
        applied = app_module.apply_rules(conn, {"description": row["description"], "source_name": row["source_name"], "amount": -100})
    assert applied["category"] == "Travel" and applied["rule_id"] == r2["rule_id"]


def get_status(base, path, cookie):
    import urllib.error, urllib.request
    try:
        return urllib.request.urlopen(urllib.request.Request(base + path, headers={"Cookie": cookie})).status
    except urllib.error.HTTPError as e:
        return e.code


# --- "Uncategorised — send back for review" ------------------------------------------------

def test_uncategorise_action_clears_category_and_reopens_review(seeded_server):
    from tests.conftest import post
    cookie = login(seeded_server)
    html = get(seeded_server, "/transactions?month=all&category=Food&subcategory=Food+Delivery", cookie)
    assert "combo-none" in html and 'id="remember-note"' in html
    txid = re.search(r'data-tx-id="([^"]+)"', html).group(1)
    with app_module.db() as conn:
        before_uncat = app_module.query_transactions(conn, None, {"category": app_module.UNCATEGORISED_FILTER})["total"]
        open_before = conn.execute("SELECT count(*) FROM review_items WHERE transaction_id=? AND status='open'", (txid,)).fetchone()[0]
    resp = post(seeded_server, "/review", cookie, {"transaction_id": txid, "action": "uncategorise", "category": "Uncategorised", "remember": "yes", "origin": "transactions"}, headers={"X-Requested-With": "fetch"})
    assert resp.status == 200
    payload = json.loads(resp.read().decode())
    assert payload["action"] == "uncategorise" and payload["rule_id"] is None and "cat-none" in payload["row"]["category_chip"]
    with app_module.db() as conn:
        row = next(r for r in app_module.effective_transactions(conn) if r["transaction_id"] == txid)
        assert row["category"] is None and row["subcategory"] is None and row["classification"] == "controllable"
        assert app_module.query_transactions(conn, None, {"category": app_module.UNCATEGORISED_FILTER})["total"] == before_uncat + 1
        items = conn.execute("SELECT reason, status FROM review_items WHERE transaction_id=? AND status='open'", (txid,)).fetchall()
        assert any(i["reason"] == "manual_uncategorised" for i in items) and len(items) >= open_before + 1
        uncat = next(i for i in app_module.breakdown(conn, None, {}, "category") if i["key"] == app_module.UNCATEGORISED_FILTER)
        assert uncat["count"] >= 1
    # Review page lists it with the "sent back" reason and no stale guess.
    review = get(seeded_server, "/review", cookie)
    assert "Sent back for review" in review
    assert re.search(rf'data-review-id="{re.escape(txid)}"', review)
    item_json = re.search(r'const items=(\{.*?\}),categories=', review, flags=re.S).group(1)
    assert json.loads(item_json)[txid]["guess"] == ""
    # It can be re-categorised afterwards; the sent-back item resolves.
    post(seeded_server, "/review", cookie, {"transaction_id": txid, "action": "approve", "category": "Travel", "origin": "transactions"}, headers={"X-Requested-With": "fetch"})
    with app_module.db() as conn:
        assert conn.execute("SELECT count(*) FROM review_items WHERE transaction_id=? AND status='open'", (txid,)).fetchone()[0] == 0
        assert next(r for r in app_module.effective_transactions(conn) if r["transaction_id"] == txid)["category"] == "Travel"


# --- Pocket change on the page + admin threshold ----------------------------------------------

def test_pocket_change_visible_on_transactions_and_admin_threshold(server):
    from tests.conftest import post
    cookie = login(server)
    with app_module.db() as conn:
        conn.execute("INSERT INTO import_batches(import_batch_id, created_at, status) VALUES('b1', ?, 'imported')", (app_module.now_iso(),))
        for i, amt in enumerate((-40, -120, -180, -900)):
            ok, _ = app_module.insert_transaction(conn, "b1", None, {"source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-10", "description": f"UPI/RANDOM STALL {i}/UPI", "amount": amt}, create_review=True)
            assert ok
    html = get(server, "/transactions?month=2026-06&category=Pocket+change", cookie)
    assert len(_rows(html)) == 3 and "cat-pocket" in html
    html = get(server, "/transactions?month=2026-06", cookie)
    assert 'data-key="Pocket change"' in html and "cat-pocket" in html
    # Admin page shows the current threshold; saving a new one re-files rows.
    admin = get(server, "/admin", cookie)
    assert 'data-pocket-input' in admin and f'value="{app_module.POCKET_CHANGE_DEFAULT}"' in admin
    resp = post(server, "/admin/pocket-change", cookie, {"threshold": "150"}, headers={"Accept": "application/json"})
    assert resp.status == 200
    payload = json.loads(resp.read().decode())
    assert payload["ok"] and payload["threshold"] == 150 and payload["updated"] == 1  # the ₹180 row leaves the bucket
    assert len(_rows(get(server, "/transactions?month=2026-06&category=Pocket+change", cookie))) == 2
    assert len(_rows(get(server, "/transactions?month=2026-06&category=__none__", cookie))) == 2
    for bad in ("-5", "abc", ""):
        assert post(server, "/admin/pocket-change", cookie, {"threshold": bad}, headers={"Accept": "application/json"}).status == 400
    resp = post(server, "/admin/pocket-change", cookie, {"threshold": "0"}, headers={"Accept": "application/json"})
    assert json.loads(resp.read().decode())["updated"] == 2
    assert len(_rows(get(server, "/transactions?month=2026-06&category=Pocket+change", cookie))) == 0
    with app_module.db() as conn:
        assert app_module.pocket_change_threshold(conn) == 0


# --- heading count, multi-select flow, sticky filter card ------------------------------------

def test_heading_count_reflects_filters(seeded_server):
    cookie = login(seeded_server)
    assert "<h2>Transactions <span class='count' data-tx-count>(16)</span></h2>" in get(seeded_server, "/transactions?month=all", cookie)
    assert "(1)</span>" in get(seeded_server, "/transactions?month=all&category=Food&subcategory=Food+Delivery", cookie)


def test_flow_filter_is_multi_select(seeded_server):
    cookie = login(seeded_server)
    with app_module.db() as conn:
        spend = app_module.query_transactions(conn, None, {"flow": "spend"})["total"]
        fee = app_module.query_transactions(conn, None, {"flow": "fee"})["total"]
        both = app_module.query_transactions(conn, None, {"flow": "spend,fee"})["total"]
        assert both == spend + fee and fee >= 1
        assert app_module.query_transactions(conn, None, {"flow": "spend,bogus"})["total"] == spend
        assert app_module.query_transactions(conn, None, {"flow": ",".join(app_module.FLOW_TYPES)})["total"] == 16  # all = no filter
    html = get(seeded_server, "/transactions?month=all&flow=spend,fee", cookie)
    assert len(_rows(html)) == both
    assert "Flow: Money out, Fee" in html
    assert 'data-ms-value>' in html or 'data-ms-value' in html
    assert '<input type="checkbox" value="spend" data-ms-opt checked>' in html
    assert '<input type="checkbox" value="income" data-ms-opt>' in html
    assert 'data-ms-all>' in html  # master box unchecked when a subset is selected
    assert 'data-ms-apply' in html and "Any flow" in html
    html = get(seeded_server, "/transactions?month=all", cookie)
    assert 'data-ms-all checked>' in html
    assert app_module.flow_values("fee, spend,fee") == ["fee", "spend"]


def test_filter_card_is_sticky_and_collapsible(seeded_server):
    html = get(seeded_server, "/transactions?month=all&category=Food", login(seeded_server))
    assert 'data-sticky-sentinel' in html and "class='card tx-filter-card' data-filter-card data-active-count='1'" in html
    assert "<details class='tx-filters-collapse' open>" in html and "Filters · 1 active" in html
    assert ".tx-filter-card{position:sticky" in html and "--header-h" in html

"""query_transactions() / breakdown(): SQL filtering, paging, sorting and the money-out rollups."""
from __future__ import annotations

import app as app_module

MAY = "2026-05"


def _bd(month=MAY, filters=None, by="category"):
    with app_module.db() as conn:
        return app_module.breakdown(conn, month, filters or {}, by)


def _q(month=MAY, filters=None, **kw):
    with app_module.db() as conn:
        return app_module.query_transactions(conn, month, filters or {}, **kw)


def _add(conn, i, amount=-100, category="Food", subcategory=None, flow="spend", date="2026-06-10", source="Vignesh Kotak Bank"):
    ok, txid = app_module.insert_transaction(conn, "b1", None, {
        "source_name": source, "transaction_date": date, "description": f"ROW {i}", "amount": amount,
        "category": category, "subcategory": subcategory, "classification": "controllable", "flow_type": flow,
    }, create_review=False)
    assert ok, i
    return txid


def _batch(conn):
    conn.execute("INSERT INTO import_batches(import_batch_id, created_at, status) VALUES('b1', ?, 'imported')", (app_module.now_iso(),))


# --- breakdown ---------------------------------------------------------------

def test_category_breakdown_matches_dashboard(seeded_server):
    with app_module.db() as conn:
        dash = app_module.dashboard_data(conn, MAY)
    items = _bd()
    assert sum(i["value"] for i in items) == dash["summary"]["total_spend"]
    assert [(i["name"], i["value"]) for i in items] == [(f["name"], f["value"]) for f in dash["category_flows"]]
    assert abs(sum(i["share"] for i in items) - 1) < 1e-9
    food = next(i for i in items if i["name"] == "Food")
    assert food["children"] and all(c["value"] > 0 for c in food["children"])
    assert abs(sum(c["share_of_parent"] for c in food["children"]) - 1) < 1e-9


def test_breakdown_ignores_its_own_facet_but_respects_others(seeded_server):
    full = _bd()
    assert [i["name"] for i in _bd(filters={"category": "Food", "subcategory": "Food Delivery"})] == [i["name"] for i in full]
    # Another facet still narrows the category panel.
    narrowed = _bd(filters={"source": "Vignesh Kotak Bank"})
    assert 0 < len(narrowed) < len(full)
    # Account view ignores `source` but respects `category`.
    accounts = _bd(filters={"source": "Vignesh Kotak Bank", "category": "Food"}, by="account")
    assert len(accounts) >= 1 and all(a["value"] > 0 for a in accounts)
    people = _bd(by="person")
    assert {p["name"] for p in people} <= set(app_module.PAYERS)
    assert sum(p["value"] for p in people) == sum(i["value"] for i in full)


def test_breakdown_buckets_uncategorised_and_no_subcategory(server):
    with app_module.db() as conn:
        _batch(conn)
        _add(conn, 1, category=None, amount=-500)  # above the pocket-change threshold
        _add(conn, 2, category="Food", subcategory=None)
        _add(conn, 3, category="Food", subcategory="Dining", amount=-300)
        _add(conn, 4, category="Food", subcategory="Dining", amount=50, flow="refund")
        _add(conn, 5, category="Food", flow="income", amount=1000)  # never counts
    items = _bd(month="2026-06")
    names = {i["name"]: i for i in items}
    assert names[app_module.UNCATEGORISED_LABEL]["key"] == app_module.UNCATEGORISED_FILTER
    food = names["Food"]
    assert food["value"] == 100 + 300 - 50
    kids = {c["name"]: c for c in food["children"]}
    assert kids["Dining"]["value"] == 250 and kids["Dining"]["count"] == 2
    assert kids[app_module.NO_SUBCATEGORY_LABEL]["key"] == app_module.NO_SUBCATEGORY_FILTER
    assert _bd(month="2026-06", filters={"flow": "income"}) == []


# --- query_transactions ------------------------------------------------------

def test_query_filters_and_totals(seeded_server):
    res = _q(filters={"category": "Food", "subcategory": "Food Delivery"})
    assert res["total"] == 1 and len(res["rows"]) == 1
    assert _q(filters={"category": "Food", "subcategory": app_module.NO_SUBCATEGORY_FILTER})["total"] == 0
    assert _q(filters={"payer": "Jananiya"})["total"] == 5
    assert _q(filters={"q": "swiggy"})["total"] == 1
    assert _q(filters={"source": "Vignesh Kotak Bank", "flow": "fee"})["total"] == 1
    everything = _q(month=None)
    assert everything["total"] == 16 and everything["pages"] == 1
    assert everything["money_out"] > 0 and everything["money_in"] > 0


def test_query_paging_and_sorting(server):
    with app_module.db() as conn:
        _batch(conn)
        for i in range(60):
            _add(conn, i, amount=-(i + 1))
    p1 = _q(month="2026-06")
    assert p1["total"] == 60 and p1["pages"] == 2 and len(p1["rows"]) == 50
    p2 = _q(month="2026-06", page=2)
    assert len(p2["rows"]) == 10
    assert _q(month="2026-06", page=999)["page"] == 2  # clamped
    assert _q(month="2026-06", page=0)["page"] == 1
    top = _q(month="2026-06", sort="amount", direction="desc")["rows"][0]
    assert top["amount"] == -60
    low = _q(month="2026-06", sort="amount", direction="asc")["rows"][0]
    assert low["amount"] == -1
    assert _q(month="2026-06", per_page=7)["pages"] == 9


def test_query_uses_override_overlay(seeded_server):
    with app_module.db() as conn:
        txid = next(r["transaction_id"] for r in app_module.effective_transactions(conn, MAY) if r["category"] == "Food")
        conn.execute(
            "INSERT INTO manual_overrides(manual_override_id, transaction_id, category, created_at, created_by) VALUES('o1', ?, 'Travel', ?, 't')",
            (txid, app_module.now_iso()),
        )
    before = _q(filters={"category": "Food"})
    assert txid in {r["transaction_id"] for r in _q(filters={"category": "Travel"})["rows"]}
    assert txid not in {r["transaction_id"] for r in before["rows"]}

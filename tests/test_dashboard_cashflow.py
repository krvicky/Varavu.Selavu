"""Cash-flow chart data: per-category flows, surplus, /api/sankey shape and the expand control."""
from __future__ import annotations

import json
import re

import app as app_module
from tests.conftest import get, login

MAY = "2026-05"
# "Investments" is also a real category, so only the synthetic bucket names must be absent.
BUCKET_NAMES = {"Fixed expenses", "Other expenses", "Surplus"}


def _flows(month=MAY):
    with app_module.db() as conn:
        return app_module.dashboard_data(conn, month)


def test_category_flows_are_netted_sorted_and_coloured(seeded_server):
    data = _flows()
    flows = data["category_flows"]
    by_name = {f["name"]: f for f in flows}
    # Fuel spend 4181 minus the 41 surcharge-waiver refund on the same category.
    assert by_name["Transport"]["value"] == 4181 - 41
    # Spend + fee on Home & Utilities: rent 70000 + bank charges 52.
    assert by_name["Home & Utilities"]["value"] == 70052
    assert [f["value"] for f in flows] == sorted((f["value"] for f in flows), reverse=True)
    assert all(f["value"] > 0 for f in flows)
    assert by_name["Food"]["color"] == app_module.CATEGORY_STYLE["Food"][3]
    # Category totals line up with the summary spend and the surplus closes the loop.
    s = data["summary"]
    assert sum(f["value"] for f in flows) == s["total_spend"]
    assert s["surplus"] == s["total_inflow"] - s["total_spend"]
    assert "Uncategorized" not in by_name


def test_uncategorised_spend_uses_british_label(server):
    with app_module.db() as conn:
        app_module.insert_transaction(
            conn,
            None,
            None,
            {"transaction_date": "2026-06-02", "description": "Mystery POS", "amount": -500, "flow_type": "spend",
             "category": None, "classification": "controllable", "payer": "Vignesh", "source_name": "Vignesh Kotak Bank"},
            create_review=False,
        )
        conn.commit()
        data = app_module.dashboard_data(conn, "2026-06")
    names = {f["name"] for f in data["category_flows"]}
    assert app_module.UNCATEGORISED_LABEL in names
    assert data["category_flows"][0]["color"] == app_module.OTHER_CATEGORY_STYLE[3]


def test_api_sankey_fans_inflow_into_categories(seeded_server):
    cookie = login(seeded_server)
    payload = json.loads(get(seeded_server, f"/api/sankey?month={MAY}", cookie))
    names = [n["name"] for n in payload["nodes"]]
    income_nodes = [n for n in payload["nodes"] if n["kind"] == "income"]
    # Seed salary credit is uncategorised income → a single "Other inflow" source on the left.
    assert [n["name"] for n in income_nodes] == [app_module.OTHER_INFLOW_LABEL]
    assert names[0] == app_module.OTHER_INFLOW_LABEL and names[-1] == "Surplus / unallocated"
    assert {"Food", "Transport", "Travel", "Home & Utilities"} <= set(names)
    assert not BUCKET_NAMES & set(names)
    assert all(lk["source"] == app_module.OTHER_INFLOW_LABEL and lk["value"] > 0 for lk in payload["links"])
    assert {lk["target"] for lk in payload["links"]} == set(names) - {app_module.OTHER_INFLOW_LABEL}
    assert app_module.SHORTFALL_LABEL not in names  # surplus month: no savings drawdown
    node = next(n for n in payload["nodes"] if n["name"] == "Food")
    assert node["color"] == app_module.CATEGORY_STYLE["Food"][3] and node["kind"] == "spend" and node["category"] == "Food"
    assert payload["nodes"][0]["cssVar"] == "--viz-inflow"


def test_api_sankey_splits_inflow_by_income_category(seeded_server):
    server = seeded_server
    with app_module.db() as conn:
        for tx in (
            _tx("2026-07-01", "NEFT CR-EMPLOYER PAYROLL IND CONSULT", 90000, "income", None),
            _tx("2026-07-02", "NACH-ECS-CR-OBEROIREAL 1", 500, "income", None),
            _tx("2026-07-03", "NACH-10-CR-CDSL 2", 500, "income", None),
            _tx("2026-07-04", "Random credit", 1000, "income", None),
            _tx("2026-07-05", "Rent", -30000, "spend", "Home & Utilities"),
            _tx("2026-07-06", "Zomato", -6000, "spend", "Food"),
        ):
            assert app_module.insert_transaction(conn, None, None, tx, create_review=False)[0]
        conn.commit()
        data = app_module.dashboard_data(conn, "2026-07")
    assert [(f["name"], f["value"]) for f in data["income_flows"]] == [("Salary", 90000), ("Dividend income", 1000), (app_module.OTHER_INFLOW_LABEL, 1000)]
    assert data["summary"]["dividends"] == 1000 and data["summary"]["total_inflow"] == 92000
    assert not any(f["name"] in ("Salary", "Dividend income") for f in data["category_flows"])  # money-out only
    cookie = login(server)
    payload = json.loads(get(server, "/api/sankey?month=2026-07", cookie))
    incomes = {n["name"]: n for n in payload["nodes"] if n["kind"] == "income"}
    assert set(incomes) == {"Salary", "Dividend income", app_module.OTHER_INFLOW_LABEL}
    assert incomes["Salary"]["category"] == "Salary" and incomes[app_module.OTHER_INFLOW_LABEL]["category"] == app_module.UNCATEGORISED_FILTER
    out_of = lambda src: round(sum(lk["value"] for lk in payload["links"] if lk["source"] == src), 4)
    into = lambda tgt: round(sum(lk["value"] for lk in payload["links"] if lk["target"] == tgt), 4)
    assert out_of("Salary") == 90000 and out_of("Dividend income") == 1000
    assert into("Home & Utilities") == 30000 and into("Food") == 6000 and into("Surplus / unallocated") == 56000


def test_api_sankey_empty_month_still_returns_a_link(seeded_server):
    # 2026-07 is after the seed month (2026-05) but before today, so it is not clamped away.
    cookie = login(seeded_server)
    payload = json.loads(get(seeded_server, "/api/sankey?month=2026-07", cookie))
    assert len(payload["links"]) == 1
    assert {n["name"] for n in payload["nodes"]} == {"Total inflow", "Surplus / unallocated"}


def test_dashboard_embeds_category_flows_and_expand_button(seeded_server):
    cookie = login(seeded_server)
    html = get(seeded_server, f"/?month={MAY}", cookie)
    assert 'id="chart-expand"' in html
    assert 'data-lucide="maximize-2"' in html
    assert '"name": "Travel"' in html
    # The inline chart data no longer carries the old bucket entries.
    assert "name:'Fixed expenses'" not in html and "name:'Other expenses'" not in html


def test_summary_has_total_expenses_and_dividends(seeded_server):
    s = _flows()["summary"]
    assert s["total_expenses"] == s["total_spend"] - s["investments"]
    assert s["dividends"] == 0  # seed month has no dividend credits


def test_kpi_tiles_are_inflow_expenses_investments_dividends(seeded_server):
    cookie = login(seeded_server)
    html = get(seeded_server, f"/?month={MAY}", cookie)
    tiles = re.findall(r'<div class="card metric"><div class="metric-label">(.*?)</div>', html, re.S)
    labels = [re.sub(r"<[^>]+>", "", t).strip() for t in tiles]
    assert labels == ["Total inflow", "Total expenses", "Investments", "Total dividends received"]


def _tx(date, desc, amount, flow, category):
    return {"transaction_date": date, "description": desc, "amount": amount, "flow_type": flow, "category": category,
            "classification": "controllable", "payer": "Vignesh", "source_name": "Vignesh Kotak Bank"}


def test_api_sankey_deficit_month_adds_shortfall_source(server):
    with app_module.db() as conn:
        for tx in (
            _tx("2026-07-01", "Small credit", 1000, "income", None),
            _tx("2026-07-02", "Rent", -2000, "spend", "Home & Utilities"),
            _tx("2026-07-03", "Zomato", -1000, "spend", "Food"),
        ):
            assert app_module.insert_transaction(conn, None, None, tx, create_review=False)[0]
        conn.commit()
    cookie = login(server)
    payload = json.loads(get(server, "/api/sankey?month=2026-07", cookie))
    names = {n["name"] for n in payload["nodes"]}
    assert app_module.SHORTFALL_LABEL in names and "Surplus / unallocated" not in names
    out_of = lambda src: round(sum(lk["value"] for lk in payload["links"] if lk["source"] == src), 4)
    into = lambda tgt: round(sum(lk["value"] for lk in payload["links"] if lk["target"] == tgt), 4)
    assert out_of(app_module.OTHER_INFLOW_LABEL) == 1000  # d3-sankey will now show the true inflow, not the outflow total
    assert out_of(app_module.SHORTFALL_LABEL) == 2000
    assert into("Home & Utilities") == 2000 and into("Food") == 1000
    assert payload["summary"] == {"inflow": 1000, "outflow": 3000, "shortfall": 2000, "surplus": 0}



def test_dashboard_charts_link_to_transactions(seeded_server):
    cookie = login(seeded_server)
    html = get(seeded_server, f"/?month={MAY}", cookie)
    assert f'MONTH="{MAY}"' in html and "linkTo=(category,flow)=>'/transactions?'" in html
    assert "click to see transactions" in html
    assert "incomes=[" in html and '"name": "Other inflow"' in html
    assert "Total dividends received" in html

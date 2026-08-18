"""Category / subcategory taxonomy and the subcategory form helpers."""
from __future__ import annotations

import app as app_module


def test_every_parent_has_a_subcategory_list_and_vice_versa():
    assert set(app_module.SUBCATEGORIES) == set(app_module.PARENT_CATEGORIES) | set(app_module.INCOME_CATEGORIES) | set(app_module.HIDDEN_CATEGORIES)
    for parent, subs in app_module.SUBCATEGORIES.items():
        assert len(subs) == len(set(subs)), f"duplicate subcategory under {parent}"
        assert all(s.strip() == s and s for s in subs)


def test_agreed_taxonomy_edits_are_present():
    subs = app_module.SUBCATEGORIES
    assert {"Fuel", "Cab", "Wallet Loading"} <= set(subs["Transport"])
    assert {"Vacation", "Flight Tickets"} <= set(subs["Travel"])
    assert {"Doctor Consultation", "Diagnostics / Tests", "Medicines"} <= set(subs["Health"])
    assert "Medical" not in subs["Health"]
    assert {"Essentials", "Doctor Consultation", "Medicines", "Shopping"} <= set(subs["Baby"])
    assert "Needs Review" not in {s for lst in subs.values() for s in lst}
    assert set(subs["Food"]) == {"Dining", "Food Delivery", "Cafes & Coffee"}
    assert set(subs["Groceries & Household"]) == {"Groceries", "Meat & Fish", "Household Supplies"}
    assert app_module.PARENT_CATEGORIES.index("Groceries & Household") == app_module.PARENT_CATEGORIES.index("Food") + 1
    assert "Groceries & Household" in app_module.CATEGORY_STYLE
    # subcategories confirmed by the categorization experiment (Aug 2026)
    assert {"Loan EMI", "Electricity & Utilities", "Internet & Phone", "Household Help & Services", "Repairs & Maintenance", "Taxes", "Bank Fees"} <= set(subs["Home & Utilities"])
    assert {"Software & AI Tools", "Entertainment", "Fitness & Sports", "Personal Care", "Work & Coworking"} <= set(subs["Lifestyle"])
    assert {"Online / Amazon", "Clothing", "Electronics", "Home & Kitchen"} <= set(subs["Shopping"])
    assert {"Grooming", "Boarding & Training"} <= set(subs["Idli"])
    assert "US Stocks" in subs["Investments"]


def test_resolve_subcategory_listed_custom_and_blank():
    assert app_module.resolve_subcategory({"subcategory": "Cab"}) == "Cab"
    assert app_module.resolve_subcategory({"subcategory": "__custom__", "subcategory_custom": "  Auto rickshaw "}) == "Auto rickshaw"
    assert app_module.resolve_subcategory({"subcategory": "__custom__", "subcategory_custom": ""}) is None
    assert app_module.resolve_subcategory({"subcategory": ""}) is None
    assert app_module.resolve_subcategory({}) is None


def test_subcategory_control_renders_grouped_options_and_custom_escape():
    html = app_module.subcategory_control("category", "subcategory", "Cab")
    assert "name='subcategory'" in html or 'name="subcategory"' in html
    assert 'data-parent="Transport"' in html and ">Cab<" in html
    assert " selected" in html.split(">Cab<")[0].rsplit("<option", 1)[1]
    assert 'value="__custom__"' in html
    assert "subcategory_custom" in html
    assert 'data-category-field="category"' in html

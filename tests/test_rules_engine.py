"""apply_rules + the built-in default rules (previously untested)."""
from __future__ import annotations

import pytest

import app as app_module


def _apply(desc: str, amount: float = -100.0) -> dict:
    with app_module.db() as conn:
        return app_module.apply_rules(conn, {"description": desc, "amount": amount, "source_name": "Vignesh Kotak Bank"})


@pytest.mark.parametrize(
    "desc,category,subcategory,flow",
    [
        ("UPI/SWIGGY INSTAMA/ICIC/126320939098/UPI", "Groceries & Household", "Groceries", "spend"),
        ("UPI/Zomato/HDFC/126960554002/UP IIntent", "Food", "Food Delivery", "spend"),
        ("UPI/Blinkit/HDFC/126423693884/Pay via Razo", "Groceries & Household", "Groceries", "spend"),
        ("Rent Sona Singh", "Home & Utilities", "Rent", "spend"),
        ("NACH-MUT-DR-GROWW PAY SERVICES P-0000WO11SDVIWM75", "Investments", "SIP / Investment", "spend"),
        ("UPI/M/S.KLEVER K9/737270450102/Payment from Ph", "Idli", "Boarding & Training", "spend"),
        ("UPI/Superbottoms Baby/1/UPI", "Baby", "Essentials", "spend"),
        # --- Kotak (UPI/...) ---
        ("UPI/Licious/ICIC/126519531886/UPI Collect", "Groceries & Household", "Meat & Fish", "spend"),
        ("UPI/AKSHAYAKALPAFA/AIRP/620000 609467/MANDATE", "Groceries & Household", "Groceries", "spend"),
        ("UPI/Firstclub1/AIRP/091809596755/U PIIntent", "Groceries & Household", "Groceries", "spend"),
        ("UPI/COFFEE MAKERS/YESB/555196659654/Payme nt from", "Food", "Cafes & Coffee", "spend"),
        ("UPI/IDLY BAR/AIRP/356810859143/PaymentToI DL", "Food", "Dining", "spend"),
        ("UPI/APPLE MEDIA SE/HDFC/103528943804/Execution te", "Lifestyle", "Subscriptions", "spend"),
        ("PCI/8954/ANTHROPIC* CLAUDE SUB/+141523250626/00:03", "Lifestyle", "Software & AI Tools", "spend"),
        ("PCI/8954/OPENROUTER, INC/+184829744130726/21:01", "Lifestyle", "Software & AI Tools", "spend"),
        ("UPI/SHOFFR MOBILIT/YESB/125506787298/UPI", "Transport", "Cab", "spend"),
        ("UPI/National Highwa/123149213327/UPI", "Transport", "Wallet Loading", "spend"),
        ("UPI/Snabbit/YESB/124881842371/UPI", "Home & Utilities", "Household Help & Services", "spend"),
        ("UPI/WWW AIRTEL IN/HDFC/103542678123/UPIIntent", "Home & Utilities", "Internet & Phone", "spend"),
        ("UPI/CBDT TIN 2 0/HDFC/126958381954/UPIIntent", "Home & Utilities", "Taxes", "spend"),
        ("MB:Second EMI", "Home & Utilities", "Loan EMI", "spend"),
        ("UPI/GARIMA TOMAR/613999565015/UPI", "Idli", "Boarding & Training", "spend"),
        ("UPI/WIZARD OF PAWS/UTIB/617111269724/UPI", "Idli", "Grooming", "spend"),
        ("UPI/CHETHAN M R/CNRB/620337341692/UPI", "Health", "Doctor Consultation", "spend"),
        ("UPI/R for Rabbit/AIRP/125271601518/RforRabbi t", "Baby", "Shopping", "spend"),
        ("UPI/UNIQLO INDIA PR/122515785620/UPI", "Shopping", "Clothing", "spend"),
        ("UPI/Amazon India/599674429918/You are paying", "Shopping", "Online / Amazon", "spend"),
        ("KR, TT USD200 TO INDMONE, REFF80870062283682", "Investments", "US Stocks", "spend"),
        ("CHRG: DCC FEE FOR 8954 ECOM TXN ON 14-MAY-2026", "Home & Utilities", "Bank Fees", "fee"),
        # --- HDFC Diners (MERCHANTCITY) ---
        ("ETERNAL LIMITEDGURGAON", "Food", "Food Delivery", "spend"),
        ("SWIGGY INBANGALORE", "Food", "Food Delivery", "spend"),
        ("SWIGGY INSTAMARTGURGAON", "Groceries & Household", "Groceries", "spend"),
        ("SWIGGY PVT LTD GROCERY1BENGALURU", "Groceries & Household", "Groceries", "spend"),
        ("BLINK COMMERCE PVT LTDBANGALORE", "Groceries & Household", "Groceries", "spend"),
        ("AMAZON SELLER SERVICESBANGALORE", "Shopping", "Online / Amazon", "spend"),
        ("AMAZON IN GROCERYMUMBAI", "Groceries & Household", "Groceries", "spend"),
        ("ABHIVRUDHI TECHBANGALORE", "Home & Utilities", "Repairs & Maintenance", "spend"),
        ("M S ALL THINGS BABY INBENGALURU", "Baby", "Shopping", "spend"),
        ("SUPERTAILS IOSBANGALORE", "Idli", "Essentials / Care", "spend"),
        ("Burma Burma RestaurantBANGALORE", "Food", "Dining", "spend"),
        ("THIRD WAVE COFFEEBANGALORE", "Food", "Cafes & Coffee", "spend"),
        ("EVOLVE BACKMUMBAI", "Travel", "Vacation", "spend"),
        ("upg*paymentico.comNicosia", "Lifestyle", "Subscriptions", "spend"),
        ("CANVA* I04887-154836237372853388", "Lifestyle", "Software & AI Tools", "spend"),
        ("IGST-VPS2713629599509-RATE 18.0 -29 (Ref# DT261360085008080000002)", "Home & Utilities", "Bank Fees", "fee"),
    ],
)
def test_default_rules_categorise_real_descriptions(seeded_server, desc, category, subcategory, flow):
    tx = _apply(desc)
    assert tx["category"] == category
    if subcategory is not None:
        assert tx["subcategory"] == subcategory
    assert tx["flow_type"] == flow
    assert tx["rule_id"]


def test_rent_rule_does_not_fire_on_trent_dividend_or_zorent(seeded_server):
    for desc in ("NACH-10-CR-TRENT DIV202526- 00000000000000487864", "UPI/ZORENT TECHNOL/IDFB/618139092207/UPI"):
        tx = _apply(desc, amount=250.0)
        assert tx.get("category") != "Home & Utilities", desc


@pytest.mark.parametrize(
    "desc,flow,amount",
    [
        ("ONLINE PYMT RECD-DHDF1CD1L1P42M (Ref# ST261470084000011432162)", "card_payment", 231852.0),
        ("PG OSHDFCCC", "card_payment", -185716.0),
        ("Sweep Trf From: 1234567890", "transfer", 10000.0),
        ("FD PREMAT PROCEEDS: 1234567890", "transfer", 10.0),
        ("NEFT CITIN26676959178 EMPLOYER PAYROLL IND CONSULT", "income", 250000.0),
        ("NACH-10-CR-TRENT DIV202526- 00000000000000487864", "income", 250.0),
        ("Int.Pd:0912271208:01-04-2026 to 30- 06-2026", "income", 122.0),
        ("MB:SENT NEFT JANANIYA R 00000000000000 BANK OF", "transfer", -15000.0),
        ("PETRO SURCHARGE WAIVER", "refund", 41.4),
    ],
)
def test_money_movement_rules(seeded_server, desc, flow, amount):
    tx = _apply(desc, amount=amount)
    assert tx["flow_type"] == flow
    assert tx["rule_id"]
    assert tx["confidence"] >= 0.75


def test_confirmed_amazon_orders_are_not_flagged_ambiguous(seeded_server):
    with app_module.db() as conn:
        batch = conn.execute("SELECT import_batch_id FROM import_batches LIMIT 1").fetchone()[0]
        tx = app_module.apply_rules(conn, {"description": "AMAZON SELLER SERVICESBANGALORE", "amount": -412.0, "source_name": "Vignesh HDFC Diners", "transaction_date": "2026-06-01"})
        assert tx["confidence"] >= 0.75 and tx["category"] == "Shopping"
        ok, txid = app_module.insert_transaction(conn, batch, None, tx, create_review=True)
        assert ok
        reasons = {r[0] for r in conn.execute("SELECT reason FROM review_items WHERE transaction_id=?", (txid,)).fetchall()}
        assert "ambiguous merchant" not in reasons
        assert "low confidence" not in reasons


def test_amazon_rule_has_no_needs_review_placeholder(seeded_server):
    tx = _apply("UPI/Amazon India/599674429918/You are paying")
    assert tx["category"] == "Shopping"
    assert tx.get("subcategory") == "Online / Amazon"
    with app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rules WHERE subcategory='Needs Review'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE subcategory='Needs Review'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category='Health' AND subcategory='Medical'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category='Health' AND subcategory='Medicines'").fetchone()[0] == 1


def test_seed_defaults_adds_missing_rules_but_keeps_user_edits(seeded_server):
    with app_module.db() as conn:
        rid = conn.execute("SELECT rule_id FROM rules WHERE name='Rent'").fetchone()[0]
        conn.execute("UPDATE rules SET category='Travel', enabled=0 WHERE rule_id=?", (rid,))
        conn.execute("DELETE FROM rules WHERE name='Investments'")
        conn.commit()
    with app_module.db() as conn:
        app_module.seed_defaults(conn)
        conn.commit()
    with app_module.db() as conn:
        rent = conn.execute("SELECT category, enabled FROM rules WHERE rule_id=?", (rid,)).fetchone()
        assert (rent["category"], rent["enabled"]) == ("Travel", 0)
        assert conn.execute("SELECT COUNT(*) FROM rules WHERE name='Investments'").fetchone()[0] == 1


def test_seed_defaults_migrates_old_taxonomy_values(seeded_server):
    with app_module.db() as conn:
        conn.execute("UPDATE transactions SET category='Food', subcategory='Groceries / Quick Commerce' WHERE description='Swiggy Instamart'")  # legacy label
        conn.execute("UPDATE transactions SET category='Food', subcategory='Dining & Delivery' WHERE description='Zomato Eternal'")
        conn.commit()
    with app_module.db() as conn:
        app_module.seed_defaults(conn)
        conn.commit()
    with app_module.db() as conn:
        insta = conn.execute("SELECT category, subcategory FROM transactions WHERE description='Swiggy Instamart'").fetchone()
        zom = conn.execute("SELECT category, subcategory FROM transactions WHERE description='Zomato Eternal'").fetchone()
        assert tuple(insta) == ("Groceries & Household", "Groceries")  # migrated from "Groceries / Quick Commerce"
        assert tuple(zom) == ("Food", "Food Delivery")


def test_no_match_falls_back_to_flow_inference(seeded_server):
    tx = _apply("UPI/SOME RANDOM PERSON/123/UPI", amount=-1200)  # above the pocket-change threshold
    assert tx["category"] is None
    assert tx["flow_type"] == "spend"
    assert tx["confidence"] == 0.2


def _insert(conn, desc: str, amount: float = -500.0) -> str:
    batch = conn.execute("SELECT import_batch_id FROM import_batches LIMIT 1").fetchone()[0]
    ok, txid = app_module.insert_transaction(conn, batch, None, {"description": desc, "amount": amount, "source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-01"}, create_review=True)
    assert ok
    return txid


def test_reapply_rules_categorises_existing_rows_and_rebuilds_review_items(seeded_server):
    with app_module.db() as conn:
        conn.execute("UPDATE rules SET enabled=0")
        txid = _insert(conn, "UPI/Snabbit/YESB/124881842371/UPI")
        row = conn.execute("SELECT category, rule_id FROM transactions WHERE transaction_id=?", (txid,)).fetchone()
        assert row["category"] is None and not row["rule_id"]
        assert "missing category" in {r[0] for r in conn.execute("SELECT reason FROM review_items WHERE transaction_id=? AND status='open'", (txid,))}
        conn.execute("UPDATE rules SET enabled=1")
        result = app_module.reapply_rules(conn)
        conn.commit()
    assert result["updated"] >= 1
    with app_module.db() as conn:
        row = conn.execute("SELECT category, subcategory, rule_id, confidence FROM transactions WHERE transaction_id=?", (txid,)).fetchone()
        assert (row["category"], row["subcategory"]) == ("Home & Utilities", "Household Help & Services")
        assert row["rule_id"] and row["confidence"] >= 0.75
        reasons = {r[0] for r in conn.execute("SELECT reason FROM review_items WHERE transaction_id=? AND status='open'", (txid,))}
        assert "missing category" not in reasons and "low confidence" not in reasons


def test_reapply_rules_leaves_manual_overrides_alone(seeded_server):
    with app_module.db() as conn:
        txid = _insert(conn, "UPI/Snabbit/YESB/1/UPI")
        conn.execute("INSERT INTO manual_overrides(manual_override_id, transaction_id, category, subcategory, classification, flow_type, merchant_payee, notes, created_at, created_by) VALUES('ov1',?,'Idli','Grooming','controllable','spend','x',NULL,?, 'test')", (txid, app_module.now_iso()))
        conn.execute("UPDATE transactions SET manual_override_id='ov1', category='Idli', subcategory='Grooming' WHERE transaction_id=?", (txid,))
        app_module.reapply_rules(conn)
        conn.commit()
        row = conn.execute("SELECT category, subcategory FROM transactions WHERE transaction_id=?", (txid,)).fetchone()
        assert (row["category"], row["subcategory"]) == ("Idli", "Grooming")


def test_seed_defaults_auto_reapplies_when_new_default_rules_arrive(seeded_server):
    with app_module.db() as conn:
        conn.execute("DELETE FROM rules WHERE rule_id='rule_household_help___home_services'")
        txid = _insert(conn, "UPI/Snabbit/YESB/2/UPI")
        assert conn.execute("SELECT category FROM transactions WHERE transaction_id=?", (txid,)).fetchone()[0] is None
        conn.commit()
    with app_module.db() as conn:
        app_module.seed_defaults(conn)  # re-inserts the missing default rule -> triggers re-apply for uncategorised rows
        conn.commit()
    with app_module.db() as conn:
        assert conn.execute("SELECT category FROM transactions WHERE transaction_id=?", (txid,)).fetchone()[0] == "Home & Utilities"




def test_rule_conflicts_lists_every_matching_rule_in_engine_order(seeded_server):
    with app_module.db() as conn:
        hits = app_module.rule_conflicts(conn, "SWIGGY ORDER 123", "Vignesh Kotak Bank", category="Food", subcategory="Food Delivery")
        assert hits and all(h["match_type"] in ("description_contains", "keyword") for h in hits)
        assert hits[0]["same_outcome"] is True and hits[0]["remembered"] is False
        assert app_module.rule_conflicts(conn, "SWIGGY ORDER 123", None, category="Shopping", subcategory=None)[0]["same_outcome"] is False
        conn.execute(
            "INSERT INTO rules(rule_id,name,match_type,pattern,source_name,category,subcategory,classification,flow_type,confidence,enabled,created_at,updated_at) VALUES('r_rem','Remember SWIGGY ORDER 123','exact_merchant','SWIGGY ORDER 123',NULL,'Shopping',NULL,'controllable','spend',0.94,1,'x','x')",
        )
        hits = app_module.rule_conflicts(conn, "SWIGGY ORDER 123", None, category="Travel", subcategory=None)
        assert hits[0]["rule_id"] == "r_rem" and hits[0]["remembered"] and not hits[0]["same_outcome"]  # 0.94 sorts first
        assert len(hits) >= 2
        assert app_module.rule_conflicts(conn, "TOTALLY UNKNOWN MERCHANT", None, category="Food", subcategory=None) == []



# --- Pocket change fallback ---------------------------------------------------------------

def test_pocket_change_files_small_unknown_money_out_only(seeded_server):
    small = _apply("UPI/CHAI STALL/9911/UPI", amount=-90)
    assert small["category"] == app_module.POCKET_CHANGE and small["subcategory"] is None
    assert small["classification"] == "controllable" and small["rule_id"] == app_module.POCKET_CHANGE_RULE_ID and small["confidence"] == 0.8
    assert _apply("UPI/CHAI STALL/9911/UPI", amount=-250)["category"] is None          # above threshold
    assert _apply("UPI/CHAI STALL/9911/UPI", amount=90)["category"] is None            # money in never qualifies
    assert _apply("UPI/CHAI STALL/9911/UPI", amount=-200)["category"] is None          # boundary: strictly below
    with app_module.db() as conn:
        tr = app_module.apply_rules(conn, {"description": "UPI TRANSFER OWN ACCOUNT", "amount": -50, "source_name": "Vignesh Kotak Bank"})
    assert tr["flow_type"] == "transfer" and tr["category"] != app_module.POCKET_CHANGE
    # A real rule still wins for a small amount.
    assert _apply("Swiggy Instamart", amount=-50)["category"] != app_module.POCKET_CHANGE
    # No review item is raised for pocket change rows.
    with app_module.db() as conn:
        txid = _insert(conn, "UPI/CHAI STALL/9911/UPI", amount=-90)
        assert conn.execute("SELECT count(*) FROM review_items WHERE transaction_id=? AND status='open'", (txid,)).fetchone()[0] == 0


def test_pocket_change_threshold_setting_and_reapply(seeded_server):
    with app_module.db() as conn:
        assert app_module.pocket_change_threshold(conn) == app_module.POCKET_CHANGE_DEFAULT
        engine_row = _insert(conn, "UPI/CHAI STALL/1/UPI", amount=-90)
        manual_row = _insert(conn, "UPI/CHAI STALL/2/UPI", amount=-90)
        conn.execute("INSERT INTO manual_overrides(manual_override_id, transaction_id, category, created_at, created_by) VALUES('o_pc', ?, 'Food', ?, 't')", (manual_row, app_module.now_iso()))
        conn.execute("UPDATE transactions SET manual_override_id='o_pc' WHERE transaction_id=?", (manual_row,))
        # Turn it off → the engine-placed row goes back to uncategorised (and gets a review item); the manual one is untouched.
        app_module.set_pocket_change_threshold(conn, 0)
        assert app_module.pocket_change_threshold(conn) == 0
        assert _apply_conn(conn, "UPI/CHAI STALL/3/UPI", -50)["category"] is None
        app_module.reapply_rules(conn)
        e = conn.execute("SELECT category, rule_id, confidence FROM transactions WHERE transaction_id=?", (engine_row,)).fetchone()
        assert e["category"] is None and e["rule_id"] is None and e["confidence"] == 0.2
        assert conn.execute("SELECT count(*) FROM review_items WHERE transaction_id=? AND status='open'", (engine_row,)).fetchone()[0] >= 1
        m = conn.execute("SELECT category, rule_id FROM transactions WHERE transaction_id=?", (manual_row,)).fetchone()
        assert m["category"] == app_module.POCKET_CHANGE and m["rule_id"] == app_module.POCKET_CHANGE_RULE_ID  # raw row untouched (override wins anyway)
        # Raise it → the row is filed again.
        app_module.set_pocket_change_threshold(conn, 150)
        app_module.reapply_rules(conn)
        e = conn.execute("SELECT category FROM transactions WHERE transaction_id=?", (engine_row,)).fetchone()
        assert e["category"] == app_module.POCKET_CHANGE
        assert conn.execute("SELECT count(*) FROM review_items WHERE transaction_id=? AND status='open'", (engine_row,)).fetchone()[0] == 0


def _apply_conn(conn, desc: str, amount: float) -> dict:
    return app_module.apply_rules(conn, {"description": desc, "amount": amount, "source_name": "Vignesh Kotak Bank"})


def test_reapply_never_files_import_provided_categories_as_pocket_change(seeded_server):
    with app_module.db() as conn:
        batch = conn.execute("SELECT import_batch_id FROM import_batches LIMIT 1").fetchone()[0]
        ok, txid = app_module.insert_transaction(conn, batch, None, {"description": "PARKING METER", "amount": -60, "source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-01", "category": "Transport"}, create_review=False)
        assert ok
        assert conn.execute("SELECT category FROM transactions WHERE transaction_id=?", (txid,)).fetchone()[0] == "Transport"
        app_module.reapply_rules(conn)
        assert conn.execute("SELECT category FROM transactions WHERE transaction_id=?", (txid,)).fetchone()[0] == "Transport"


def test_startup_backfill_files_pre_existing_small_uncategorised_rows(server):
    with app_module.db() as conn:
        conn.execute("INSERT INTO import_batches(import_batch_id, created_at, status) VALUES('b1', ?, 'imported')", (app_module.now_iso(),))
        # Simulate rows imported before the feature existed: uncategorised, no rule, low confidence.
        app_module.set_pocket_change_threshold(conn, 0)
        ids = []
        for i, amt in enumerate((-40, -164, -900)):
            ok, txid = app_module.insert_transaction(conn, "b1", None, {"source_name": "Vignesh Kotak Bank", "transaction_date": "2026-05-2%d" % i, "description": f"UPI/SOMEONE {i}/UPI", "amount": amt}, create_review=True)
            assert ok
            ids.append(txid)
        ok, manual = app_module.insert_transaction(conn, "b1", None, {"source_name": "Vignesh Kotak Bank", "transaction_date": "2026-05-29", "description": "UPI/NOT SURE/UPI", "amount": -50}, create_review=True)
        conn.execute("INSERT INTO manual_overrides(manual_override_id, transaction_id, category, created_at, created_by) VALUES('o_ns', ?, '', ?, 't')", (manual, app_module.now_iso()))
        conn.execute("UPDATE transactions SET manual_override_id='o_ns' WHERE transaction_id=?", (manual,))
        assert all(r[0] is None for r in conn.execute("SELECT category FROM transactions WHERE import_batch_id='b1'"))
        conn.execute("DELETE FROM app_settings WHERE key=?", (app_module.POCKET_CHANGE_SETTING,))  # back to the default threshold
    app_module.init_db(seed=False)  # what a server restart does
    with app_module.db() as conn:
        cats = {r["transaction_id"]: r["category"] for r in conn.execute("SELECT transaction_id, category FROM transactions WHERE import_batch_id='b1'")}
        assert cats[ids[0]] == app_module.POCKET_CHANGE and cats[ids[1]] == app_module.POCKET_CHANGE
        assert cats[ids[2]] is None                      # above threshold
        assert cats[manual] is None                     # manual "not sure" wins
        assert conn.execute("SELECT count(*) FROM audit_log WHERE action='reapply_rules' AND after_json LIKE '%startup_backfill%'").fetchone()[0] >= 1



# --- income categories ---------------------------------------------------------------------

def test_salary_and_dividend_rules_set_income_categories(seeded_server):
    sal = _apply("NEFT CR-HDFC0000001-EMPLOYER PAYROLL IND CONSULT-JULY", amount=150000)
    assert (sal["category"], sal["flow_type"], sal["classification"]) == ("Salary", "income", "excluded")
    for desc in ("NACH-ECS-CR-OBEROIREAL1STINT2627-80033", "NACH-10-CR-CDSL FNL 2025 26-1310818"):
        d = _apply(desc, amount=26)
        assert (d["category"], d["flow_type"], d["classification"]) == ("Dividend income", "income", "excluded")
    interest = _apply("INT.PD:50100123:01-04-2026 to 30-06-2026", amount=300)
    assert interest.get("category") is None and interest["flow_type"] == "income"
    with app_module.db() as conn:
        for name in ("Salary", "Dividend income"):
            assert name in app_module.INCOME_CATEGORIES and name in app_module.CATEGORY_STYLE
        # Money-in never enters the money-out breakdown.
        _insert(conn, "NACH-ECS-CR-TEST", amount=100)
        assert not any(i["name"] in ("Salary", "Dividend income") for i in app_module.breakdown(conn, None, {}, "category"))


def test_rule_updates_migrate_old_default_rule_and_backfill_rows(seeded_server):
    """A DB created before these rules carried categories: the old combined dividends rule is removed (unless customised),
    the salary rule gains its category, and rows they had matched (category NULL) get categories on the next boot."""
    with app_module.db() as conn:
        # Simulate the pre-change state.
        conn.execute("DELETE FROM rules WHERE rule_id IN ('rule_dividend_income','rule_interest_received')")
        conn.execute("INSERT INTO rules(rule_id,name,match_type,pattern,category,classification,flow_type,confidence,enabled,created_at,updated_at) VALUES('rule_dividends___interest','Dividends & interest','description_contains','NACH-ECS-CR|NACH-10-CR|INT.PD:',NULL,'excluded','income',0.9,1,'x','x')")
        conn.execute("UPDATE rules SET category=NULL WHERE rule_id='rule_salary'")
        batch = conn.execute("SELECT import_batch_id FROM import_batches LIMIT 1").fetchone()[0]
        ok, div = app_module.insert_transaction(conn, batch, None, {"description": "NACH-ECS-CR-OBEROI 1", "amount": 40, "source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-02"}, create_review=False)
        ok, sal = app_module.insert_transaction(conn, batch, None, {"description": "NEFT-EMPLOYER PAYROLL IND CONSULT", "amount": 90000, "source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-03"}, create_review=False)
        assert tuple(conn.execute("SELECT category, rule_id FROM transactions WHERE transaction_id=?", (div,)).fetchone()) == (None, "rule_dividends___interest")
        assert conn.execute("SELECT category FROM transactions WHERE transaction_id=?", (sal,)).fetchone()[0] is None
    app_module.init_db(seed=True)  # boot: RULE_UPDATES + new default rules + startup backfill
    with app_module.db() as conn:
        assert conn.execute("SELECT count(*) FROM rules WHERE rule_id='rule_dividends___interest'").fetchone()[0] == 0
        assert conn.execute("SELECT category FROM rules WHERE rule_id='rule_dividend_income'").fetchone()[0] == "Dividend income"
        assert conn.execute("SELECT category FROM rules WHERE rule_id='rule_salary'").fetchone()[0] == "Salary"
        assert conn.execute("SELECT count(*) FROM rules WHERE rule_id='rule_interest_received'").fetchone()[0] == 1
        assert conn.execute("SELECT category FROM transactions WHERE transaction_id=?", (div,)).fetchone()[0] == "Dividend income"
        assert conn.execute("SELECT category FROM transactions WHERE transaction_id=?", (sal,)).fetchone()[0] == "Salary"
        # A user-customised old rule (category set) is kept.
        conn.execute("INSERT INTO rules(rule_id,name,match_type,pattern,category,classification,flow_type,confidence,enabled,created_at,updated_at) VALUES('rule_dividends___interest','Mine','description_contains','NACH-ECS-CR','Lifestyle','excluded','income',0.9,1,'x','x')")
    app_module.init_db(seed=True)
    with app_module.db() as conn:
        assert conn.execute("SELECT category FROM rules WHERE rule_id='rule_dividends___interest'").fetchone()[0] == "Lifestyle"



# --- Bank internal transfers · additive default-rule patterns · Groceries rename -----------------

def test_bank_internal_transfers_are_categorised_and_hidden_from_rollups(seeded_server):
    for desc, amt in (("SWEEP TRANSFER TO [2219535632]", -150000), ("FD PREMAT PROCEEDS: 2215518578", 61), ("SWEEP TRF FROM 123", 5000)):
        tx = _apply(desc, amount=amt)
        assert (tx["category"], tx["flow_type"], tx["classification"]) == ("Bank internal transfers", "transfer", "excluded"), desc
    with app_module.db() as conn:
        txid = _insert(conn, "SWEEP TRANSFER TO [1]", amount=-150000)
        # Even a *spend*-flow row in this category is kept out of every rollup.
        ok, spend_id = app_module.insert_transaction(conn, conn.execute("SELECT import_batch_id FROM import_batches LIMIT 1").fetchone()[0], None,
            {"description": "MANUAL MOVE", "amount": -999, "source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-01", "category": "Bank internal transfers", "flow_type": "spend", "classification": "controllable"}, create_review=False)
        assert not any(i["name"] == "Bank internal transfers" for i in app_module.breakdown(conn, None, {}, "category"))
        data = app_module.dashboard_data(conn, "2026-06")
        assert not any(f["name"] == "Bank internal transfers" for f in data["category_flows"]) and data["summary"]["total_spend"] == 0
        # Still in the ledger, filterable by category.
        assert app_module.query_transactions(conn, None, {"category": "Bank internal transfers"})["total"] == 2
        assert "Bank internal transfers" in app_module.taxonomy_options(conn)[0]


def test_default_rule_patterns_sync_additively_and_backfill(seeded_server):
    with app_module.db() as conn:
        # Simulate an old DB: stale pattern plus a user-added alternative, and an uncategorised Blink row.
        conn.execute("UPDATE rules SET pattern='BLINKIT|INSTAMART|MYLOCALSHOP' WHERE rule_id='rule_quick_commerce_groceries'")
        conn.execute("UPDATE rules SET category=NULL WHERE rule_id='rule_fd_auto_sweep'")
        batch = conn.execute("SELECT import_batch_id FROM import_batches LIMIT 1").fetchone()[0]
        for desc, amt in (("BLINK COMMERCE PVT LTDBANGALORE", -297), ("SWEEP TRANSFER TO [22]", -100000), ("UPI/MYLOCALSHOP/1", -50)):
            ok, _ = app_module.insert_transaction(conn, batch, None, {"description": desc, "amount": amt, "source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-05"}, create_review=False)
        conn.execute("UPDATE transactions SET category=NULL, subcategory=NULL, rule_id=NULL, confidence=0.2 WHERE description='BLINK COMMERCE PVT LTDBANGALORE'")
        conn.execute("UPDATE transactions SET category=NULL WHERE description='SWEEP TRANSFER TO [22]'")
    app_module.init_db(seed=True)
    with app_module.db() as conn:
        pattern = conn.execute("SELECT pattern FROM rules WHERE rule_id='rule_quick_commerce_groceries'").fetchone()[0]
        assert pattern.startswith("BLINKIT|INSTAMART|MYLOCALSHOP|") and "BLINK COMMERCE" in pattern and "ZEPTO" in pattern
        rows = {r["description"]: (r["category"], r["subcategory"]) for r in conn.execute("SELECT description, category, subcategory FROM transactions WHERE transaction_date='2026-06-05'")}
        assert rows["BLINK COMMERCE PVT LTDBANGALORE"] == ("Groceries & Household", "Groceries")
        assert rows["SWEEP TRANSFER TO [22]"][0] == "Bank internal transfers"
        assert rows["UPI/MYLOCALSHOP/1"] == ("Groceries & Household", "Groceries")  # user-added alternative kept


def test_groceries_subcategory_rename_migrates_everywhere(seeded_server):
    with app_module.db() as conn:
        batch = conn.execute("SELECT import_batch_id FROM import_batches LIMIT 1").fetchone()[0]
        ok, txid = app_module.insert_transaction(conn, batch, None, {"description": "OLD LABEL ROW", "amount": -10, "source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-06", "category": "Groceries & Household", "subcategory": "Groceries / Quick Commerce"}, create_review=False)
        conn.execute("INSERT INTO manual_overrides(manual_override_id, transaction_id, category, subcategory, created_at, created_by) VALUES('o_g', ?, 'Groceries & Household', 'Groceries / Quick Commerce', ?, 't')", (txid, app_module.now_iso()))
        conn.execute("INSERT INTO baselines(baseline_id, scope, category, subcategory, amount, effective_month, updated_source, created_at, updated_at) VALUES('b_g','subcategory','Groceries & Household','Groceries / Quick Commerce',5000,'2026-06','x',?,?)", (app_module.now_iso(), app_module.now_iso()))
        conn.execute("UPDATE rules SET subcategory='Groceries / Quick Commerce' WHERE rule_id='rule_quick_commerce_groceries'")
    app_module.init_db(seed=True)
    with app_module.db() as conn:
        for table in ("transactions", "manual_overrides", "baselines", "rules"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE subcategory='Groceries / Quick Commerce'").fetchone()[0] == 0, table
        assert conn.execute("SELECT subcategory FROM manual_overrides WHERE manual_override_id='o_g'").fetchone()[0] == "Groceries"
        assert conn.execute("SELECT subcategory FROM baselines WHERE baseline_id='b_g'").fetchone()[0] == "Groceries"
    assert app_module.SUBCATEGORIES["Groceries & Household"][0] == "Groceries"



# --- Nanny salary / Loan EMI (regex rules) -------------------------------------------------------

def test_regex_rules_for_nanny_salary_and_loan_emi(seeded_server):
    nanny = _apply("SentIMPS000000000001NANNY NAME/HDFCX5625/KKBKTrans", amount=-15000)
    assert (nanny["category"], nanny["subcategory"], nanny["classification"], nanny["flow_type"], nanny["notes"]) == ("Home & Utilities", "Household Help & Services", "fixed", "spend", "Nanny salary")
    emi = _apply("IB:Sent NEFT KKBKH26182754104/JANANIYA R/BANK OF", amount=-28555)
    assert (emi["category"], emi["subcategory"], emi["classification"], emi["flow_type"]) == ("Home & Utilities", "Loan EMI", "fixed", "spend")
    # Other transfers to family still go to the generic rule.
    fam = _apply("UPI/JANANIYA R/HDFC/1234/UPI", amount=-500)
    assert fam.get("category") is None and fam["flow_type"] == "transfer" and fam["rule_id"] == "rule_family_transfers"
    with app_module.db() as conn:
        hits = app_module.rule_conflicts(conn, "SentIMPS000000000001NANNY NAME/HDFCX5625/KKBKTrans", None, category="Home & Utilities", subcategory="Household Help & Services")
        assert [h["rule_id"] for h in hits][:2] == ["rule_nanny_salary", "rule_family_transfers"] and hits[0]["same_outcome"]
        # An invalid regex never matches and never crashes the engine.
        conn.execute("INSERT INTO rules(rule_id,name,match_type,pattern,category,classification,flow_type,confidence,enabled,created_at,updated_at) VALUES('r_bad','Bad','regex','(unclosed','Food','controllable','spend',0.99,1,'x','x')")
        assert app_module.apply_rules(conn, {"description": "(unclosed", "amount": -10, "source_name": None}).get("rule_id") != "r_bad"


def test_backfill_refiles_rows_previously_matched_by_family_transfers(seeded_server):
    with app_module.db() as conn:
        batch = conn.execute("SELECT import_batch_id FROM import_batches LIMIT 1").fetchone()[0]
        conn.execute("UPDATE rules SET enabled=0 WHERE rule_id IN ('rule_nanny_salary','rule_home_loan_emi__jananiya')")  # pretend the rules didn't exist yet
        ids = {}
        for key, desc, amt in (("n1", "SentIMPS000000000001NANNY NAME/HDFCX5625/KKBKTrans", -15000), ("n2", "SentIMPS000000000002NANNY NAME/HDFCX5625/KKBKTrans", -15000), ("emi", "IB:Sent NEFT KKBKH26182754104/JANANIYA R/BANK OF", -28555)):
            ok, ids[key] = app_module.insert_transaction(conn, batch, None, {"description": desc, "amount": amt, "source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-07"}, create_review=False)
            assert ok
        rows = {k: conn.execute("SELECT category, rule_id, flow_type FROM transactions WHERE transaction_id=?", (v,)).fetchone() for k, v in ids.items()}
        assert all(r["category"] is None and r["rule_id"] == "rule_family_transfers" and r["flow_type"] == "transfer" for r in rows.values())
        conn.execute("UPDATE rules SET enabled=1 WHERE rule_id IN ('rule_nanny_salary','rule_home_loan_emi__jananiya')")
    app_module.init_db(seed=True)  # restart → startup backfill
    with app_module.db() as conn:
        n1 = conn.execute("SELECT category, subcategory, notes, flow_type, classification FROM transactions WHERE transaction_id=?", (ids["n1"],)).fetchone()
        assert tuple(n1) == ("Home & Utilities", "Household Help & Services", "Nanny salary", "spend", "fixed")
        emi = conn.execute("SELECT category, subcategory, flow_type FROM transactions WHERE transaction_id=?", (ids["emi"],)).fetchone()
        assert tuple(emi) == ("Home & Utilities", "Loan EMI", "spend")
        # A rule-bound row whose rule still gives the same answer is untouched; a manual override always wins.
        conn.execute("INSERT INTO manual_overrides(manual_override_id, transaction_id, category, created_at, created_by) VALUES('o_n2', ?, 'Shopping', ?, 't')", (ids["n2"], app_module.now_iso()))
        conn.execute("UPDATE transactions SET manual_override_id='o_n2' WHERE transaction_id=?", (ids["n2"],))
    app_module.init_db(seed=True)
    with app_module.db() as conn:
        assert next(r for r in app_module.effective_transactions(conn) if r["transaction_id"] == ids["n2"])["category"] == "Shopping"

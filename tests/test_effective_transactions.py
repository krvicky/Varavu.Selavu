"""effective_transactions(): the SQL override overlay must behave exactly like the old per-row loop."""
from __future__ import annotations

import app as app_module


def _batch(conn, batch_id, **cols):
    base = {"created_at": app_module.now_iso(), "status": "imported"}
    base.update(cols)
    keys = ", ".join(["import_batch_id", *base])
    marks = ", ".join(["?"] * (len(base) + 1))
    conn.execute(f"INSERT INTO import_batches({keys}) VALUES({marks})", (batch_id, *base.values()))


def _tx(conn, batch, desc, amount, **kw):
    tx = {"source_name": "Vignesh Kotak Bank", "transaction_date": "2026-06-10", "description": desc, "amount": amount,
          "category": kw.get("category"), "subcategory": kw.get("subcategory"), "classification": kw.get("classification", "controllable"),
          "flow_type": kw.get("flow_type", "spend"), "notes": kw.get("notes")}
    ok, txid = app_module.insert_transaction(conn, batch, None, tx, create_review=False)
    assert ok
    return txid


def _override(conn, txid, oid, created_at, **fields):
    cols = {"category": None, "subcategory": None, "classification": None, "flow_type": None, "merchant_payee": None, "notes": None}
    cols.update(fields)
    conn.execute(
        "INSERT INTO manual_overrides(manual_override_id, transaction_id, category, subcategory, classification, flow_type, merchant_payee, notes, created_at, created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (oid, txid, cols["category"], cols["subcategory"], cols["classification"], cols["flow_type"], cols["merchant_payee"], cols["notes"], created_at, "test"),
    )


def test_latest_override_overlays_only_its_non_null_fields(server):
    with app_module.db() as conn:
        _batch(conn, "b1")
        txid = _tx(conn, "b1", "ZOMATO ORDER", -450, category="Food", subcategory="Dining", notes="raw note")
        # Older override sets category+notes; newer (same second) sets category only.
        _override(conn, txid, "o_old", "2026-06-11T10:00:00", category="Shopping", notes="old note")
        _override(conn, txid, "o_new", "2026-06-11T10:00:00", category="Travel")
        rows = {r["transaction_id"]: r for r in app_module.effective_transactions(conn)}
    row = rows[txid]
    assert row["category"] == "Travel"           # newest wins (rowid tiebreak on equal created_at)
    assert row["notes"] == "raw note"            # only the latest override overlays; NULL falls back to the raw row
    assert row["subcategory"] == "Dining"        # untouched by any override
    assert row["manual_override_id"] == "o_new"
    assert row["classification"] == "controllable"


def test_untouched_rows_and_key_set_unchanged(server):
    with app_module.db() as conn:
        _batch(conn, "b1")
        txid = _tx(conn, "b1", "PLAIN", -100, category="Food")
        rows = app_module.effective_transactions(conn)
    assert len(rows) == 1
    row = rows[0]
    expected_keys = {"transaction_id", "raw_import_id", "import_batch_id", "transaction_date", "description", "amount", "flow_type",
                     "category", "subcategory", "classification", "merchant_payee", "payer", "source_name", "confidence", "rule_id",
                     "manual_override_id", "notes", "fingerprint", "created_at"}
    assert set(row) == expected_keys
    assert row["transaction_id"] == txid and row["manual_override_id"] is None


def test_hidden_batches_and_month_filter(server):
    with app_module.db() as conn:
        _batch(conn, "ok")
        _batch(conn, "gone", deleted_at=app_module.now_iso())
        _batch(conn, "excl", excluded_at=app_module.now_iso())
        keep = _tx(conn, "ok", "KEEP", -10)
        _tx(conn, "gone", "DELETED", -20)
        _tx(conn, "excl", "EXCLUDED", -30)
        conn.execute("UPDATE transactions SET transaction_date='2026-05-01' WHERE transaction_id=?", (keep,))
        assert [r["transaction_id"] for r in app_module.effective_transactions(conn)] == [keep]
        assert app_module.effective_transactions(conn, month="2026-05")[0]["transaction_id"] == keep
        assert app_module.effective_transactions(conn, month="2026-06") == []


def test_seed_batch_hidden_when_seed_disabled(seeded_server):
    with app_module.db() as conn:
        assert len(app_module.effective_transactions(conn, include_seed=True)) == 16
        assert app_module.effective_transactions(conn, include_seed=False) == []


def test_empty_string_override_clears_category_and_subcategory(server):
    with app_module.db() as conn:
        _batch(conn, "b1")
        txid = _tx(conn, "b1", "MYSTERY", -75, category="Food", subcategory="Dining")
        _override(conn, txid, "o_clear", "2026-06-11T10:00:00", category="", subcategory="")
        row = app_module.effective_transactions(conn)[0]
    assert row["category"] is None and row["subcategory"] is None and row["manual_override_id"] == "o_clear"

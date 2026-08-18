import pytest

import app as app_module


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    app_module.init_db(seed=False)
    connection = app_module.db()
    yield connection
    connection.close()


def _tx(**overrides):
    base = {
        "transaction_date": "2026-07-01",
        "description": "AMAZON PURCHASE",
        "amount": -500.0,
        "source_name": "Vignesh Axis Bank",
    }
    base.update(overrides)
    return base


def test_duplicate_with_differing_whitespace_and_case_is_rejected(conn):
    inserted1, _ = app_module.insert_transaction(conn, None, None, _tx())
    inserted2, _ = app_module.insert_transaction(
        conn, None, None, _tx(description="  amazon   purchase  ")
    )
    assert inserted1 is True
    assert inserted2 is False


def test_transactions_differing_only_by_amount_are_both_accepted(conn):
    inserted1, _ = app_module.insert_transaction(conn, None, None, _tx(amount=-500.0))
    inserted2, _ = app_module.insert_transaction(conn, None, None, _tx(amount=-501.0))
    assert inserted1 is True
    assert inserted2 is True


def test_description_is_stored_verbatim_despite_normalized_hash(conn):
    app_module.insert_transaction(conn, None, None, _tx(description="  Amazon   Purchase  "))
    row = conn.execute("SELECT description FROM transactions").fetchone()
    assert row["description"] == "  Amazon   Purchase  "

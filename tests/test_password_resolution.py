import pytest

import app as app_module


def test_evaluate_password_pattern_first_n_letters_of_name():
    guess = app_module.evaluate_password_pattern("first 4 letters of name", "Vignesh Axis Bank")
    assert guess == "VIGN"


def test_evaluate_password_pattern_returns_none_when_pattern_is_empty():
    assert app_module.evaluate_password_pattern(None, "Vignesh Axis Bank") is None
    assert app_module.evaluate_password_pattern("", "Vignesh Axis Bank") is None


def test_evaluate_password_pattern_returns_none_for_unresolvable_date_token():
    # DDMM would need a stored date-of-birth we don't have -- must not guess.
    guess = app_module.evaluate_password_pattern("First 4 letters of name + DDMM", "Vignesh Axis Bank")
    assert guess is None


def test_evaluate_password_pattern_returns_none_for_unrecognized_shape():
    assert app_module.evaluate_password_pattern("some unrelated instruction", "Vignesh Axis Bank") is None


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
    monkeypatch.setattr(app_module, "STATEMENT_PASSWORD_KEY", "kzFAQCL6SyLI4mbXQkqC2es_FTEqawXowlhv3AsYWWw=")
    app_module.init_db(seed=False)
    connection = app_module.db()
    yield connection
    connection.close()


def test_resolve_statement_password_prefers_inline_password(conn):
    result = app_module.resolve_statement_password(conn, "axis_vignesh", "Vignesh Axis Bank", "typed-password")
    assert result == "typed-password"


def test_resolve_statement_password_falls_back_to_stored_password(conn):
    conn.execute(
        "INSERT INTO account_passwords(source_id, encrypted_password, updated_at) VALUES(?,?,?)",
        ("axis_vignesh", app_module.encrypt_password("stored-secret"), app_module.now_iso()),
    )
    result = app_module.resolve_statement_password(conn, "axis_vignesh", "Vignesh Axis Bank", None)
    assert result == "stored-secret"


def test_resolve_statement_password_falls_back_to_pattern_when_no_stored_password(conn):
    conn.execute(
        "INSERT INTO account_passwords(source_id, password_pattern, updated_at) VALUES(?,?,?)",
        ("axis_vignesh", "first 4 letters of name", app_module.now_iso()),
    )
    result = app_module.resolve_statement_password(conn, "axis_vignesh", "Vignesh Axis Bank", None)
    assert result == "VIGN"


def test_resolve_statement_password_returns_none_when_nothing_available(conn):
    result = app_module.resolve_statement_password(conn, "axis_vignesh", "Vignesh Axis Bank", None)
    assert result is None

import pytest

import app as app_module


@pytest.fixture
def with_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(app_module, "STATEMENT_PASSWORD_KEY", Fernet.generate_key().decode())


@pytest.fixture
def without_key(monkeypatch):
    monkeypatch.setattr(app_module, "STATEMENT_PASSWORD_KEY", "")


def test_encrypt_then_decrypt_roundtrips(with_key):
    blob = app_module.encrypt_password("correct-horse-battery-staple")
    assert app_module.decrypt_password(blob) == "correct-horse-battery-staple"


def test_encrypted_blob_does_not_contain_plaintext(with_key):
    blob = app_module.encrypt_password("super-secret-value")
    assert b"super-secret-value" not in blob


def test_encrypt_without_key_raises_password_key_missing(without_key):
    with pytest.raises(app_module.PasswordKeyMissing):
        app_module.encrypt_password("anything")


def test_decrypt_without_key_raises_password_key_missing(without_key):
    with pytest.raises(app_module.PasswordKeyMissing):
        app_module.decrypt_password(b"irrelevant")

import os
from pathlib import Path

import pikepdf
import pytest

from pdf_import.decrypt import DecryptError, WrongPasswordError, decrypted_copy, is_encrypted


@pytest.fixture
def encrypted_pdf(tmp_path) -> Path:
    path = tmp_path / "statement.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(path, encryption=pikepdf.Encryption(user="correct-horse", owner="correct-horse"))
    return path


@pytest.fixture
def plain_pdf(tmp_path) -> Path:
    path = tmp_path / "plain.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(path)
    return path


def test_is_encrypted_true_for_password_protected_pdf(encrypted_pdf):
    assert is_encrypted(encrypted_pdf) is True


def test_is_encrypted_false_for_plain_pdf(plain_pdf):
    assert is_encrypted(plain_pdf) is False


def test_decrypted_copy_with_correct_password_yields_openable_file(encrypted_pdf):
    with decrypted_copy(encrypted_pdf, "correct-horse") as decrypted_path:
        assert decrypted_path.exists()
        with pikepdf.open(decrypted_path) as reopened:
            assert len(reopened.pages) == 1


def test_decrypted_copy_deletes_temp_file_after_success(encrypted_pdf):
    captured = {}
    with decrypted_copy(encrypted_pdf, "correct-horse") as decrypted_path:
        captured["path"] = decrypted_path
    assert not captured["path"].exists()


def test_decrypted_copy_deletes_temp_file_after_exception(encrypted_pdf):
    captured = {}
    with pytest.raises(RuntimeError):
        with decrypted_copy(encrypted_pdf, "correct-horse") as decrypted_path:
            captured["path"] = decrypted_path
            raise RuntimeError("boom")
    assert not captured["path"].exists()


def test_wrong_password_raises_wrong_password_error(encrypted_pdf):
    with pytest.raises(WrongPasswordError):
        with decrypted_copy(encrypted_pdf, "not-the-password"):
            pass


def test_wrong_password_error_never_embeds_the_password(encrypted_pdf):
    with pytest.raises(WrongPasswordError) as exc_info:
        with decrypted_copy(encrypted_pdf, "super-secret-value"):
            pass
    assert "super-secret-value" not in str(exc_info.value)

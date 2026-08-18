"""Regression tests for the /import upload flow.

Covers the failure chain seen in production:
  1. multipart upload POST rendered HTML inline (no redirect) so the client-side
     poll's location.reload() re-submitted the file every ~2.5 s;
  2. a missing PDF dependency surfaced as a bare "ModuleNotFoundError";
  3. the retry password popover ignored `hidden` and overlapped the table.
"""
from __future__ import annotations

import io
import urllib.request
import urllib.error
import urllib.parse

import pytest

import app as app_module
from tests.conftest import NoRedirect as _NoRedirect, login as _login


def _multipart(fields: dict, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----pytestboundary"
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"statement\"; filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n".encode())
    buf.write(content)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _post_upload(base: str, cookie: str):
    body, ctype = _multipart({"source_name": app_module.SOURCES[0][1], "statement_month": "2026-05"}, "stmt.pdf", b"%PDF-1.4 fake")
    req = urllib.request.Request(base + "/import", data=body, method="POST", headers={"Content-Type": ctype, "Cookie": cookie})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        return opener.open(req)
    except urllib.error.HTTPError as e:
        return e


def test_pdf_upload_redirects_instead_of_rendering(server):
    """PRG: a successful multipart upload must answer 302 → /import so that a later
    page refresh can never re-submit the file."""
    cookie = _login(server)
    resp = _post_upload(server, cookie)
    assert resp.status == 302
    assert resp.headers["Location"].startswith("/import")
    with app_module.db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM import_batches WHERE status='pending_pdf_extraction'").fetchone()[0]
    assert n == 1


def test_import_page_poll_never_uses_location_reload(server):
    """location.reload() on a POST result re-POSTs; the poll must navigate with replace()."""
    cookie = _login(server)
    req = urllib.request.Request(server + "/import", headers={"Cookie": cookie})
    html = urllib.request.urlopen(req).read().decode()
    assert "location.reload()" not in html
    assert "location.replace(" in html


def test_import_page_hides_retry_password_popover_by_default(server):
    cookie = _login(server)
    _post_upload(server, cookie)  # ensures at least one row exists in history
    req = urllib.request.Request(server + "/import", headers={"Cookie": cookie})
    html = urllib.request.urlopen(req).read().decode()
    assert ".retry-password-row[hidden]{display:none" in html
    assert ".retry-form{position:relative" in html


def test_missing_pdf_dependency_is_reported_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path / "inbox")
    app_module.init_db(seed=False)
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("pdf_import.decrypt"):
            raise ModuleNotFoundError("No module named 'pikepdf'", name="pikepdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    row = {"source_id": "x", "file_name": "f.pdf", "import_batch_id": "b1", "source_name": "X"}
    status, note = app_module._process_pdf_batch(row, None)
    assert status == "needs_parser"
    assert "pikepdf" in note
    assert "requirements.txt" in note


def test_delete_import_removes_uploaded_file(server):
    cookie = _login(server)
    _post_upload(server, cookie)
    with app_module.db() as conn:
        row = conn.execute("SELECT import_batch_id, file_name FROM import_batches WHERE file_type='pdf' ORDER BY created_at DESC LIMIT 1").fetchone()
    path = app_module.UPLOAD_DIR / row["file_name"]
    assert path.is_file()
    data = urllib.parse.urlencode({"batch_id": row["import_batch_id"], "action": "delete"}).encode()
    req = urllib.request.Request(server + "/import", data=data, method="POST", headers={"Cookie": cookie})
    try:
        resp = urllib.request.build_opener(_NoRedirect).open(req)
    except urllib.error.HTTPError as e:
        resp = e
    assert resp.status == 302
    assert not path.exists()
    with app_module.db() as conn:
        deleted_at = conn.execute("SELECT deleted_at FROM import_batches WHERE import_batch_id=?", (row["import_batch_id"],)).fetchone()[0]
    assert deleted_at


def test_import_history_file_cell_stays_a_table_cell(server):
    cookie = _login(server)
    _post_upload(server, cookie)
    req = urllib.request.Request(server + "/import", headers={"Cookie": cookie})
    html = urllib.request.urlopen(req).read().decode()
    assert 'class="file-inner"' in html or "class='file-inner'" in html
    assert ".file-cell{display:flex" not in html
    assert "The uploaded file will be deleted" in html


def test_status_chip_and_detail_popover():
    chip = app_module.render_import_status_chip("failed", stage=None)
    assert "status-chip status-failed" in chip and 'data-lucide="alert-circle"' in chip and "Failed" in chip
    chip = app_module.render_import_status_chip("extracting", stage="parsing")
    assert "status-extracting" in chip and "Extracting transactions…" in chip
    pop = app_module.render_import_detail("Acct · 01 May–31 May · 137 transactions found · Balance check ✓")
    assert pop.count("<li>") == 4 and "Balance check ✓" in pop
    assert "<li>" not in app_module.render_import_detail("Worker failed: boom.")
    assert app_module.render_import_detail("") == ""


def test_import_history_shows_status_chip_not_inline_note(server):
    cookie = _login(server)
    _post_upload(server, cookie)
    req = urllib.request.Request(server + "/import", headers={"Cookie": cookie})
    html = urllib.request.urlopen(req).read().decode()
    assert "status-chip status-pending_pdf_extraction" in html
    assert 'class="status-note"' not in html and "class='status-note'" not in html
    assert "status-pop" in html


def test_import_form_has_no_date_inputs_and_period_is_inferred(server):
    cookie = _login(server)
    req = urllib.request.Request(server + "/import", headers={"Cookie": cookie})
    html = urllib.request.urlopen(req).read().decode()
    assert 'name="statement_start_date"' not in html and 'name="statement_end_date"' not in html
    assert 'name="statement_month"' in html
    assert html.index('name="pdf_password"') < html.index('name="save_password"')
    assert 'class="save-password-check"' in html and 'data-lucide="key-round"' in html
    _post_upload(server, cookie)  # posts month only
    with app_module.db() as conn:
        row = conn.execute("SELECT statement_start_date, statement_end_date FROM import_batches WHERE file_type='pdf' ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row["statement_start_date"] and row["statement_end_date"]

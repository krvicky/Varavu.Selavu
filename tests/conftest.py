"""Shared fixtures: an in-process app server bound to a temp SQLite DB."""
from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import app as app_module


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _serve(tmp_path, monkeypatch, seed: bool):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path / "inbox")
    monkeypatch.setattr(app_module, "start_import_worker", lambda *a, **k: None)
    app_module.init_db(seed=seed)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Empty DB (no seed rules / transactions)."""
    httpd = _serve(tmp_path, monkeypatch, seed=False)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def seeded_server(tmp_path, monkeypatch):
    """DB with the built-in seed rules and sample transactions."""
    httpd = _serve(tmp_path, monkeypatch, seed=True)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def login(base: str) -> str:
    """Log in with the default credentials; return the session Cookie header value."""
    data = urllib.parse.urlencode({"username": app_module.APP_USER, "password": app_module.APP_PASSWORD}).encode()
    opener = urllib.request.build_opener(NoRedirect)
    try:
        resp = opener.open(urllib.request.Request(base + "/login", data=data, method="POST"))
    except urllib.error.HTTPError as e:
        resp = e
    cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
    assert cookie, "login did not set a session cookie"
    return cookie


def get(base: str, path: str, cookie: str) -> str:
    req = urllib.request.Request(base + path, headers={"Cookie": cookie})
    return urllib.request.urlopen(req).read().decode()


def post(base: str, path: str, cookie: str, data: dict, headers: dict | None = None):
    """POST a form; return the response (or HTTPError, which quacks the same) without following redirects."""
    body = urllib.parse.urlencode(data).encode()
    hdrs = {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"}
    hdrs.update(headers or {})
    req = urllib.request.Request(base + path, data=body, method="POST", headers=hdrs)
    try:
        return urllib.request.build_opener(NoRedirect).open(req)
    except urllib.error.HTTPError as e:
        return e

"""Tests for the browser login page (web_login)."""

import json
import re
from unittest.mock import MagicMock

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

import garmin_mcp
from garmin_mcp import web_login


class _FakeGarmin:
    """Stands in for garminconnect.Garmin; behaviour set per test."""

    needs_mfa = False
    instances = []

    def __init__(self, email=None, password=None, is_cn=False, return_on_mfa=False):
        self.email = email
        self.client = MagicMock()
        self.logged_in_from = None
        self.resumed_with = None

        def _login(email, password, return_on_mfa=False):
            if password == "wrong":
                raise garmin_mcp.GarminConnectAuthenticationError("401 Unauthorized")
            return ("needs_mfa", {"state": 1}) if _FakeGarmin.needs_mfa else (None, None)

        def _dump(path):
            with open(f"{path}/garmin_tokens.json", "w") as f:
                json.dump({"token": "abc"}, f)

        self.client.login.side_effect = _login
        self.client.dump.side_effect = _dump
        _FakeGarmin.instances.append(self)

    def login(self, tokenstore):
        self.logged_in_from = tokenstore

    def resume_login(self, client_state, code):
        if code != "123456":
            raise garmin_mcp.GarminConnectAuthenticationError("MFA code invalid")
        self.resumed_with = (client_state, code)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    _FakeGarmin.needs_mfa = False
    _FakeGarmin.instances = []
    web_login._pending_mfa.clear()
    monkeypatch.setattr(web_login, "Garmin", _FakeGarmin)
    pending = garmin_mcp._PendingGarminClient(timeout=1.0)
    pending.start(lambda: None)  # startup login fails: no tokens
    pending._ready.wait(1.0)
    app = FastMCP("test")
    tokenstore = str(tmp_path / "tokens")
    web_login.register_login_routes(app, pending, tokenstore, is_cn=False)
    client = TestClient(garmin_mcp._BearerAuthMiddleware(app.streamable_http_app(), "k"))
    return client, pending, tokenstore


def test_login_page_requires_key(setup):
    client, _, _ = setup
    assert client.get("/login").status_code == 401
    page = client.get("/login?key=k")
    assert page.status_code == 200
    assert 'action="/login?key=k"' in page.text


def test_login_without_mfa_replaces_client(setup):
    client, pending, tokenstore = setup
    assert pending.login_state() == "failed"
    resp = client.post("/login?key=k", data={"email": "a@b.c", "password": "pw"})
    assert resp.status_code == 200
    assert "Kirjautuminen onnistui" in resp.text
    assert pending.login_state() == "ready"
    # The swapped-in client is the token-based one, logged in from the store.
    assert pending.logged_in_from == tokenstore


def test_login_with_mfa(setup):
    client, pending, _ = setup
    _FakeGarmin.needs_mfa = True
    resp = client.post("/login?key=k", data={"email": "a@b.c", "password": "pw"})
    assert "Vahvistuskoodi" in resp.text
    nonce = re.search(r'name="nonce" value="([^"]+)"', resp.text).group(1)

    bad = client.post("/login/mfa?key=k", data={"nonce": nonce, "code": "000000"})
    assert bad.status_code == 400
    assert pending.login_state() == "failed"

    ok = client.post("/login/mfa?key=k", data={"nonce": nonce, "code": "123456"})
    assert "Kirjautuminen onnistui" in ok.text
    assert pending.login_state() == "ready"
    assert _FakeGarmin.instances[0].resumed_with == ({"state": 1}, "123456")
    assert nonce not in web_login._pending_mfa


def test_wrong_password_shows_error(setup):
    client, pending, _ = setup
    resp = client.post("/login?key=k", data={"email": "a@b.c", "password": "wrong"})
    assert resp.status_code == 400
    assert "tarkista" in resp.text
    assert pending.login_state() == "failed"


def test_unknown_mfa_nonce(setup):
    client, _, _ = setup
    resp = client.post("/login/mfa?key=k", data={"nonce": "nope", "code": "123456"})
    assert resp.status_code == 400
    assert "vanheni" in resp.text

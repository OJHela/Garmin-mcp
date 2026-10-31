"""End-to-end test of the OAuth sign-in flow (Garmin mocked)."""

import base64
import hashlib
import json
import re
import secrets
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

import garmin_mcp
from garmin_mcp import oauth, web_login

ISSUER = "http://localhost:8000"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class _FakeGarmin:
    """garminconnect.Garmin stand-in; ``display_name`` tells sessions apart."""

    needs_mfa = False

    def __init__(self, email=None, password=None, is_cn=False, return_on_mfa=False):
        self.display_name = None
        self._email = email
        self.client = MagicMock()

        def _login(email, password, return_on_mfa=False):
            if password == "wrong":
                raise garmin_mcp.GarminConnectAuthenticationError("401 Unauthorized")
            return ("needs_mfa", None) if _FakeGarmin.needs_mfa else (None, None)

        def _dump(path):
            with open(f"{path}/garmin_tokens.json", "w") as f:
                json.dump({"user": self._email}, f)

        self.client.login.side_effect = _login
        self.client.dump.side_effect = _dump

    def login(self, tokenstore):
        with open(f"{tokenstore}/garmin_tokens.json") as f:
            self.display_name = json.load(f)["user"]

    def resume_login(self, client_state, code):
        if code != "123456":
            raise garmin_mcp.GarminConnectAuthenticationError("MFA code invalid")

    def get_full_name(self):
        return f"name of {self.display_name}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    _FakeGarmin.needs_mfa = False
    monkeypatch.setattr(oauth, "Garmin", _FakeGarmin)
    monkeypatch.setattr(web_login, "Garmin", _FakeGarmin)
    provider = oauth.GarminOAuthProvider(str(tmp_path / "oauth"), ISSUER)
    fallback = MagicMock()
    garmin = garmin_mcp._GarminProxy(oauth.SessionClientRouter(provider, fallback), timeout=5)
    app = FastMCP("test", stateless_http=True, json_response=True, **garmin_mcp._oauth_settings(provider, ISSUER))

    @app.tool()
    async def whoami() -> str:
        return garmin.get_full_name()

    oauth.register_oauth_routes(app, provider, is_cn=False)
    with TestClient(app.streamable_http_app(), base_url=ISSUER) as tc:
        yield tc, provider


def _register(tc):
    resp = tc.post("/register", json={
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


def _authorize(tc, client_id):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    resp = tc.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
    }, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    login_url = resp.headers["location"]
    assert login_url.startswith(f"{ISSUER}/oauth/login?req=")
    return verifier, parse_qs(urlparse(login_url).query)["req"][0]


def _exchange(tc, client_id, redirect, verifier):
    query = parse_qs(urlparse(redirect).query)
    assert query["state"] == ["xyz"]
    resp = tc.post("/token", data={
        "grant_type": "authorization_code", "code": query["code"][0],
        "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": verifier,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _call_whoami(tc, access_token):
    resp = tc.post("/mcp", headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "whoami", "arguments": {}}})
    return resp


def _sign_in(tc, email, mfa=False):
    client_id = _register(tc)
    verifier, req = _authorize(tc, client_id)
    assert "Kirjaudu Garminiin" in tc.get(f"/oauth/login?req={req}").text
    resp = tc.post("/oauth/login", data={"req": req, "email": email, "password": "pw"},
                   follow_redirects=False)
    if mfa:
        assert "Vahvistuskoodi" in resp.text
        bad = tc.post("/oauth/mfa", data={"req": req, "code": "000000"}, follow_redirects=False)
        assert bad.status_code == 400
        resp = tc.post("/oauth/mfa", data={"req": req, "code": "123456"}, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers["location"].startswith(REDIRECT)
    return client_id, _exchange(tc, client_id, resp.headers["location"], verifier)


def test_discovery_metadata(client):
    tc, _ = client
    meta = tc.get("/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"] == f"{ISSUER}/authorize"
    assert meta["registration_endpoint"] == f"{ISSUER}/register"
    resource = tc.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == f"{ISSUER}/mcp"


def test_mcp_requires_token(client):
    tc, _ = client
    resp = _call_whoami(tc, "bogus")
    assert resp.status_code == 401
    assert "resource_metadata" in resp.headers.get("www-authenticate", "")


def test_full_flow_uses_signed_in_account(client):
    tc, _ = client
    _, tokens = _sign_in(tc, "alice@example.com")
    resp = _call_whoami(tc, tokens["access_token"])
    assert resp.status_code == 200
    assert "name of alice@example.com" in resp.text


def test_two_users_are_isolated(client):
    tc, _ = client
    _, alice = _sign_in(tc, "alice@example.com")
    _, bob = _sign_in(tc, "bob@example.com")
    assert "alice@example.com" in _call_whoami(tc, alice["access_token"]).text
    assert "bob@example.com" in _call_whoami(tc, bob["access_token"]).text


def test_mfa_flow(client):
    tc, _ = client
    _FakeGarmin.needs_mfa = True
    _, tokens = _sign_in(tc, "carol@example.com", mfa=True)
    assert "carol@example.com" in _call_whoami(tc, tokens["access_token"]).text


def test_wrong_password_stays_on_form(client):
    tc, _ = client
    client_id = _register(tc)
    _, req = _authorize(tc, client_id)
    resp = tc.post("/oauth/login", data={"req": req, "email": "a@b.c", "password": "wrong"})
    assert resp.status_code == 400
    assert "tarkista" in resp.text


def test_expired_request(client):
    tc, _ = client
    assert tc.get("/oauth/login?req=nope").status_code == 400


def test_refresh_token(client):
    tc, _ = client
    client_id, tokens = _sign_in(tc, "dave@example.com")
    resp = tc.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    })
    assert resp.status_code == 200, resp.text
    new = resp.json()
    assert "dave@example.com" in _call_whoami(tc, new["access_token"]).text
    # Refresh tokens rotate: the old one is spent.
    again = tc.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    })
    assert again.status_code == 400


def test_state_survives_restart(client, tmp_path):
    tc, provider = client
    _, tokens = _sign_in(tc, "erin@example.com")
    # A new provider on the same directory (process restart) still accepts
    # the token and reloads the Garmin session from disk.
    reborn = oauth.GarminOAuthProvider(str(tmp_path / "oauth"), ISSUER)
    import asyncio

    access = asyncio.run(reborn.load_access_token(tokens["access_token"]))
    assert access is not None
    assert reborn.garmin_for(access.subject).display_name == "erin@example.com"

"""OAuth sign-in: the MCP client opens a Garmin login window.

With ``GARMIN_MCP_OAUTH=true`` the server is its own OAuth 2.1 authorization
server (dynamic client registration, PKCE) built on the MCP SDK's auth
support. When a client such as claude.ai connects, it is sent to
``/oauth/login``, where the user enters their Garmin email, password and MFA
code. The resulting Garmin tokens are stored per sign-in ("session") and the
OAuth access token issued to the client is bound to that session, so every
tool call uses the Garmin account of whoever signed in. Nothing has to be
configured on the host.

State (registered clients, issued tokens, Garmin tokens per session) lives
under ``<token dir>/oauth``. Without a persistent volume it is lost when the
container is replaced; clients then simply get a 401 and sign in again.
"""

import html
import json
import os
import secrets
import sys
import threading
import time

from garminconnect import Garmin
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from garmin_mcp import token_utils
from garmin_mcp.web_login import _error_text, _page, start_login

ACCESS_TOKEN_TTL = 24 * 3600
REFRESH_TOKEN_TTL = 180 * 24 * 3600  # Garmin's own tokens last about six months
AUTH_CODE_TTL = 300
LOGIN_REQUEST_TTL = 900


class GarminOAuthProvider:
    """OAuthAuthorizationServerProvider whose login step is a Garmin login."""

    def __init__(self, state_dir, issuer_url, is_cn=False):
        self._dir = state_dir
        self._sessions_dir = os.path.join(state_dir, "sessions")
        self._state_file = os.path.join(state_dir, "state.json")
        self._issuer = str(issuer_url).rstrip("/")
        self._is_cn = is_cn
        self._lock = threading.RLock()
        self._clients = {}
        self._access = {}
        self._refresh = {}
        self._codes = {}
        self._requests = {}  # login request id -> pending authorization
        self._garmin = {}  # session id -> logged-in Garmin client
        os.makedirs(self._sessions_dir, exist_ok=True)
        token_utils.secure_token_dir(state_dir)
        self._load()

    # --- persistence --------------------------------------------------------

    def _load(self):
        try:
            with open(self._state_file) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            print(f"OAuth state unreadable, starting fresh: {err}", file=sys.stderr)
            return
        self._clients = {
            k: OAuthClientInformationFull.model_validate(v)
            for k, v in data.get("clients", {}).items()
        }
        self._access = {k: AccessToken.model_validate(v) for k, v in data.get("access", {}).items()}
        self._refresh = {k: RefreshToken.model_validate(v) for k, v in data.get("refresh", {}).items()}

    def _save(self):
        now = time.time()
        with self._lock:
            self._access = {k: v for k, v in self._access.items() if not v.expires_at or v.expires_at > now}
            self._refresh = {k: v for k, v in self._refresh.items() if not v.expires_at or v.expires_at > now}
            data = {
                "clients": {k: v.model_dump(mode="json") for k, v in self._clients.items()},
                "access": {k: v.model_dump(mode="json") for k, v in self._access.items()},
                "refresh": {k: v.model_dump(mode="json") for k, v in self._refresh.items()},
            }
            tmp = self._state_file + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._state_file)

    def _session_dir(self, session_id):
        return os.path.join(self._sessions_dir, session_id)

    # --- OAuthAuthorizationServerProvider ------------------------------------

    async def get_client(self, client_id):
        return self._clients.get(client_id)

    async def register_client(self, client_info):
        with self._lock:
            self._clients[client_info.client_id] = client_info
        self._save()

    async def authorize(self, client, params: AuthorizationParams):
        request_id = secrets.token_urlsafe(24)
        with self._lock:
            self._prune_requests()
            self._requests[request_id] = {
                "client_id": client.client_id,
                "params": params,
                "expires_at": time.time() + LOGIN_REQUEST_TTL,
            }
        return f"{self._issuer}/oauth/login?req={request_id}"

    async def load_authorization_code(self, client, authorization_code):
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(self, client, authorization_code):
        with self._lock:
            self._codes.pop(authorization_code.code, None)
        return self._issue_tokens(client.client_id, authorization_code.scopes, authorization_code.subject)

    async def load_refresh_token(self, client, refresh_token):
        token = self._refresh.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        if token.expires_at and token.expires_at < time.time():
            return None
        return token

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if not os.path.isdir(self._session_dir(refresh_token.subject or "")):
            raise TokenError("invalid_grant", "Garmin session no longer exists; sign in again.")
        with self._lock:
            self._refresh.pop(refresh_token.token, None)
        return self._issue_tokens(client.client_id, scopes or refresh_token.scopes, refresh_token.subject)

    async def load_access_token(self, token):
        access = self._access.get(token)
        if access is None:
            return None
        if access.expires_at and access.expires_at < time.time():
            return None
        if not os.path.isdir(self._session_dir(access.subject or "")):
            return None
        return access

    async def revoke_token(self, token):
        with self._lock:
            self._access.pop(token.token, None)
            self._refresh.pop(token.token, None)
        self._save()

    def _issue_tokens(self, client_id, scopes, subject):
        now = int(time.time())
        access = AccessToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            subject=subject,
        )
        refresh = RefreshToken(
            token=secrets.token_urlsafe(32),
            client_id=client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            subject=subject,
        )
        with self._lock:
            self._access[access.token] = access
            self._refresh[refresh.token] = refresh
        self._save()
        return OAuthToken(
            access_token=access.token,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh.token,
        )

    # --- Garmin sessions -----------------------------------------------------

    def _prune_requests(self):
        now = time.time()
        for rid in [r for r, v in self._requests.items() if v["expires_at"] < now]:
            del self._requests[rid]

    def get_request(self, request_id):
        with self._lock:
            self._prune_requests()
            return self._requests.get(request_id)

    def complete_login(self, request_id, garmin):
        """Store the Garmin session; return the client redirect URL with a code.

        Blocking (Garmin network calls); run it off the event loop.
        """
        with self._lock:
            pending = self._requests.pop(request_id, None)
        if pending is None:
            raise LookupError("login request expired")
        session_id = secrets.token_urlsafe(16)
        session_dir = self._session_dir(session_id)
        os.makedirs(session_dir, exist_ok=True)
        garmin.client.dump(session_dir)
        token_utils.secure_token_dir(session_dir)
        # The return_on_mfa login skips profile loading; log in again from the
        # saved tokens so display_name and units are set (as init_api does).
        client = Garmin(is_cn=self._is_cn)
        client.login(session_dir)
        params = pending["params"]
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=session_id,
        )
        with self._lock:
            self._garmin[session_id] = client
            self._codes[code.code] = code
        return construct_redirect_uri(str(params.redirect_uri), code=code.code, state=params.state)

    def garmin_for(self, session_id):
        """The Garmin client for a session, loading it from disk after a restart."""
        with self._lock:
            client = self._garmin.get(session_id)
        if client is not None:
            return client
        session_dir = self._session_dir(session_id)
        if not os.path.isdir(session_dir):
            raise RuntimeError("Garmin session not found. Reconnect the Garmin connector to sign in again.")
        client = Garmin(is_cn=self._is_cn)
        client.login(session_dir)
        with self._lock:
            self._garmin[session_id] = client
        return client


class SessionClientRouter:
    """Resolves Garmin client attributes for the signed-in user of this request.

    Passed to ``_GarminProxy`` as its client. Requests carrying an OAuth access
    token use that token's Garmin session; anything else (stdio, tests) falls
    back to the single server-wide client.
    """

    def __init__(self, provider, fallback):
        self._provider = provider
        self._fallback = fallback

    def __getattr__(self, name):
        token = get_access_token()
        if token is None or not token.subject:
            return getattr(self._fallback, name)
        return getattr(self._provider.garmin_for(token.subject), name)


# --- login window ------------------------------------------------------------


def _login_form(request_id, error=None):
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    rid = html.escape(request_id, quote=True)
    return _page(
        f"""<h1>Kirjaudu Garminiin</h1>
<p>Kirjaudu Garmin Connect -tunnuksillasi, niin sovellus pääsee Garmin-tietoihisi.
Salasanaa ei tallenneta, vain kirjautumistokenit.</p>{err}
<form method="post" action="/oauth/login">
  <input type="hidden" name="req" value="{rid}">
  <label for="email">Sähköposti</label>
  <input id="email" name="email" type="email" autocomplete="username" required>
  <label for="password">Salasana</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Kirjaudu</button>
</form>""",
        status_code=400 if error else 200,
    )


def _mfa_form(request_id, error=None):
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    rid = html.escape(request_id, quote=True)
    return _page(
        f"""<h1>Vahvistuskoodi</h1>
<p>Garmin lähetti vahvistuskoodin sähköpostiisi tai tekstiviestinä.</p>{err}
<form method="post" action="/oauth/mfa">
  <input type="hidden" name="req" value="{rid}">
  <label for="code">Koodi</label>
  <input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" required autofocus>
  <button type="submit">Vahvista</button>
</form>""",
        status_code=400 if error else 200,
    )


def _expired_page():
    return _page(
        """<h1>Kirjautuminen vanheni</h1>
<p class="err">Tämä kirjautumisikkuna on vanhentunut. Sulje se ja yhdistä Garmin-yhteys uudelleen sovelluksessa.</p>""",
        status_code=400,
    )


def register_oauth_routes(fastmcp, provider, is_cn):
    """Add the ``/oauth/login`` and ``/oauth/mfa`` pages to the HTTP app."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import RedirectResponse

    async def _finish(request_id, garmin):
        redirect_url = await run_in_threadpool(provider.complete_login, request_id, garmin)
        print("OAuth Garmin sign-in succeeded.", file=sys.stderr)
        return RedirectResponse(redirect_url, status_code=302)

    @fastmcp.custom_route("/oauth/login", methods=["GET"])
    async def oauth_login_form(request):
        request_id = request.query_params.get("req", "")
        if provider.get_request(request_id) is None:
            return _expired_page()
        return _login_form(request_id)

    @fastmcp.custom_route("/oauth/login", methods=["POST"])
    async def oauth_login_submit(request):
        form = await request.form()
        request_id = form.get("req") or ""
        pending = provider.get_request(request_id)
        if pending is None:
            return _expired_page()
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        if not email or not password:
            return _login_form(request_id, "Anna sähköposti ja salasana.")
        try:
            garmin, client_state = await run_in_threadpool(start_login, email, password, is_cn)
            if client_state is not None:
                pending["garmin"] = garmin
                pending["mfa_state"] = client_state
                return _mfa_form(request_id)
            return await _finish(request_id, garmin)
        except Exception as err:  # noqa: BLE001 - shown to the user
            print(f"OAuth Garmin sign-in failed: {type(err).__name__}", file=sys.stderr)
            return _login_form(request_id, _error_text(err))

    @fastmcp.custom_route("/oauth/mfa", methods=["POST"])
    async def oauth_mfa_submit(request):
        form = await request.form()
        request_id = form.get("req") or ""
        pending = provider.get_request(request_id)
        if pending is None or "garmin" not in pending:
            return _expired_page()
        code = (form.get("code") or "").strip()
        if not code:
            return _mfa_form(request_id, "Anna vahvistuskoodi.")
        garmin = pending["garmin"]
        try:
            await run_in_threadpool(garmin.resume_login, pending["mfa_state"], code)
            return await _finish(request_id, garmin)
        except Exception as err:  # noqa: BLE001 - shown to the user
            print(f"OAuth Garmin MFA step failed: {type(err).__name__}", file=sys.stderr)
            return _mfa_form(request_id, _error_text(err))

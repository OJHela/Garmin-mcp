"""Garmin login for hosted (HTTP) deployments, without a terminal.

On a host such as Railway there is no terminal for ``garmin-mcp-auth`` and its
MFA prompt. This module offers two ways in, both ending the same way: tokens
are saved to the token directory and the running server switches to the new
client, so no restart is needed.

- MCP tools ``garmin_login`` and ``garmin_submit_mfa_code``, so the user can
  log in from their MCP client without touching the host.
- A ``/login`` browser page (only with ``MCP_AUTH_TOKEN``), opened as
  ``/login?key=<MCP_AUTH_TOKEN>``. The key stays in the form URLs so every
  step passes the same check as ``/mcp``.
"""

import base64
import html
import json
import os
import secrets
import sys
import threading
import time

from garminconnect import Garmin

from garmin_mcp import token_utils

# Pending MFA logins: nonce -> (Garmin instance, client_state, expires_at).
_MFA_TTL_SECONDS = 600
_pending_mfa = {}
_pending_lock = threading.Lock()

_PAGE = """<!doctype html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Garmin-kirjautuminen</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --fg: #1d2330;
          --muted: #5c6475; --line: #d9dde5; --accent: #0b6bcb; --err: #b42318; --ok: #157f3b; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #12151b; --card: #1b2029; --fg: #e8ebf1; --muted: #a3abba;
            --line: #2e3542; --accent: #5aa7f0; --err: #f97066; --ok: #47cd89; }}
  }}
  body {{ margin: 0; background: var(--bg); color: var(--fg);
         font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  main {{ max-width: 420px; margin: 8vh auto; padding: 0 16px; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 24px; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 4px; }}
  p {{ color: var(--muted); margin: 0 0 16px; }}
  label {{ display: block; font-weight: 600; margin: 12px 0 4px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px 12px; font: inherit; color: var(--fg);
          background: var(--bg); border: 1px solid var(--line); border-radius: 8px; }}
  button {{ margin-top: 20px; width: 100%; padding: 11px; font: inherit; font-weight: 600;
           color: #fff; background: var(--accent); border: 0; border-radius: 8px; cursor: pointer; }}
  .err {{ color: var(--err); font-weight: 600; }}
  .ok {{ color: var(--ok); font-weight: 600; }}
  details {{ margin-top: 16px; color: var(--muted); }}
  textarea {{ width: 100%; box-sizing: border-box; height: 7em; margin-top: 8px; font: 12px monospace;
             color: var(--fg); background: var(--bg); border: 1px solid var(--line); border-radius: 8px; }}
</style>
</head>
<body><main><div class="card">{body}</div></main></body>
</html>"""


def _page(body, status_code=200):
    from starlette.responses import HTMLResponse

    return HTMLResponse(_PAGE.format(body=body), status_code=status_code)


def _action(request, path):
    """Form target that keeps the ``?key=`` query, so the POST passes auth."""
    query = request.url.query
    return html.escape(path + ("?" + query if query else ""), quote=True)


def _credentials_form(request, state, error=None):
    status = {
        "ready": '<p class="ok">Palvelin on jo kirjautunut Garminiin. Voit kirjautua uudelleen toisella tilillä tai uusia tokenit.</p>',
        "pending": "<p>Kirjautuminen tallennetuilla tokeneilla on käynnissä…</p>",
    }.get(state, "<p>Kirjaudu Garmin Connect -tunnuksillasi. Salasanaa ei tallenneta, vain kirjautumistokenit.</p>")
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page(
        f"""<h1>Garmin-kirjautuminen</h1>{status}{err}
<form method="post" action="{_action(request, '/login')}">
  <label for="email">Sähköposti</label>
  <input id="email" name="email" type="email" autocomplete="username" required>
  <label for="password">Salasana</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Kirjaudu</button>
</form>""",
        status_code=400 if error else 200,
    )


def _mfa_form(request, nonce, error=None):
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page(
        f"""<h1>Vahvistuskoodi</h1>
<p>Garmin lähetti vahvistuskoodin sähköpostiisi tai tekstiviestinä.</p>{err}
<form method="post" action="{_action(request, '/login/mfa')}">
  <input type="hidden" name="nonce" value="{html.escape(nonce, quote=True)}">
  <label for="code">Koodi</label>
  <input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" required autofocus>
  <button type="submit">Vahvista</button>
</form>""",
        status_code=400 if error else 200,
    )


def _success_page(token_b64):
    return _page(
        f"""<h1>Kirjautuminen onnistui</h1>
<p class="ok">Garmin-tili on yhdistetty. MCP-työkalut toimivat nyt ilman uudelleenkäynnistystä.</p>
<p>Jos palvelussa ei ole Volumea polussa <code>/root/.garminconnect</code>, tokenit katoavat
uudelleenkäynnistyksessä. Silloin joko lisää Volume tai kopioi alla oleva arvo Railwayn
muuttujaan <code>GARMIN_TOKENS_BASE64</code>.</p>
<details><summary>Näytä GARMIN_TOKENS_BASE64</summary>
<textarea readonly onclick="this.select()">{html.escape(token_b64)}</textarea></details>"""
    )


def _prune_expired():
    now = time.monotonic()
    with _pending_lock:
        for nonce in [n for n, (_, _, exp) in _pending_mfa.items() if exp < now]:
            del _pending_mfa[nonce]


def _error_text(err):
    msg = str(err)
    name = type(err).__name__
    if "TooManyRequests" in name or "429" in msg:
        return "Garmin rajoitti kirjautumisyrityksiä. Odota hetki ja yritä uudelleen."
    if "Authentication" in name or "401" in msg:
        return "Kirjautuminen epäonnistui: tarkista sähköposti, salasana ja koodi."
    return f"Kirjautuminen epäonnistui: {msg.split(':')[0] or name}"


def start_login(email, password, is_cn):
    """Exchange credentials. Returns ``(garmin, client_state or None)``.

    ``client_state`` is set when Garmin wants an MFA code; pass it with the
    code to ``garmin.resume_login``.
    """
    garmin = Garmin(email=email, password=password, is_cn=is_cn, return_on_mfa=True)
    result1, result2 = garmin.client.login(email, password, return_on_mfa=True)
    return garmin, (result2 if result1 == "needs_mfa" else None)


def finish_login(garmin, pending_client, tokenstore, is_cn):
    """Save tokens, load a fully initialised client and swap it in.

    Returns the tokens base64-encoded, the GARMIN_TOKENS_BASE64 format.
    """
    os.makedirs(tokenstore, exist_ok=True)
    garmin.client.dump(tokenstore)
    token_utils.secure_token_dir(tokenstore)
    # The return_on_mfa login skips profile loading; log in again from the
    # saved tokens, as init_api does, so display_name and units are set.
    client = Garmin(is_cn=is_cn)
    client.login(tokenstore)
    pending_client.set_client(client)
    with open(os.path.join(tokenstore, "garmin_tokens.json")) as f:
        return base64.b64encode(f.read().encode()).decode()


def register_login_routes(fastmcp, pending_client, tokenstore, is_cn):
    """Add ``/login`` routes to the FastMCP HTTP app."""
    from starlette.concurrency import run_in_threadpool

    def _finish(garmin):
        return finish_login(garmin, pending_client, tokenstore, is_cn)

    @fastmcp.custom_route("/login", methods=["GET"])
    async def login_form(request):
        return _credentials_form(request, pending_client.login_state())

    @fastmcp.custom_route("/login", methods=["POST"])
    async def login_submit(request):
        form = await request.form()
        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        state = pending_client.login_state()
        if not email or not password:
            return _credentials_form(request, state, "Anna sähköposti ja salasana.")

        try:
            garmin, client_state = await run_in_threadpool(start_login, email, password, is_cn)
            if client_state is not None:
                _prune_expired()
                nonce = secrets.token_urlsafe(24)
                with _pending_lock:
                    _pending_mfa[nonce] = (garmin, client_state, time.monotonic() + _MFA_TTL_SECONDS)
                return _mfa_form(request, nonce)
            token_b64 = await run_in_threadpool(_finish, garmin)
        except Exception as err:  # noqa: BLE001 - shown to the user, not raised
            print(f"Web login failed: {type(err).__name__}", file=sys.stderr)
            return _credentials_form(request, state, _error_text(err))
        print("Web login succeeded; Garmin client replaced.", file=sys.stderr)
        return _success_page(token_b64)

    @fastmcp.custom_route("/login/mfa", methods=["POST"])
    async def login_mfa(request):
        form = await request.form()
        nonce = form.get("nonce") or ""
        code = (form.get("code") or "").strip()
        _prune_expired()
        with _pending_lock:
            pending = _pending_mfa.get(nonce)
        if pending is None:
            return _credentials_form(
                request, pending_client.login_state(),
                "Vahvistus vanheni. Kirjaudu uudelleen.",
            )
        garmin, client_state, _ = pending
        if not code:
            return _mfa_form(request, nonce, "Anna vahvistuskoodi.")
        try:
            await run_in_threadpool(garmin.resume_login, client_state, code)
            token_b64 = await run_in_threadpool(_finish, garmin)
        except Exception as err:  # noqa: BLE001 - shown to the user, not raised
            print(f"Web login MFA step failed: {type(err).__name__}", file=sys.stderr)
            return _mfa_form(request, nonce, _error_text(err))
        with _pending_lock:
            _pending_mfa.pop(nonce, None)
        print("Web login succeeded; Garmin client replaced.", file=sys.stderr)
        return _success_page(token_b64)


# The MCP tools serve one user, so one pending MFA login is enough; a new
# garmin_login call replaces it.
_TOOL_MFA_KEY = "__mcp_tool__"


def register_login_tools(app, pending_client, tokenstore, is_cn):
    """Add the garmin_login_status / garmin_login / garmin_submit_mfa_code tools."""
    from mcp.types import ToolAnnotations
    from starlette.concurrency import run_in_threadpool

    def _result(status, message, **extra):
        return json.dumps({"status": status, "message": message, **extra})

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def garmin_login_status() -> str:
        """Report whether the server is logged in to Garmin Connect.

        Call this first when a Garmin tool fails with a login error. If the
        status is not "ready", ask the user for their Garmin Connect email and
        password and call garmin_login.
        """
        state = pending_client.login_state()
        if state == "ready":
            return _result("ready", "Logged in to Garmin Connect.")
        if state == "pending":
            return _result("pending", "Login with saved tokens is still running; try again shortly.")
        return _result(
            "not_logged_in",
            "Not logged in. Ask the user for their Garmin Connect email and "
            "password, then call garmin_login.",
        )

    @app.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
    ))
    async def garmin_login(email: str, password: str) -> str:
        """Log the server in to Garmin Connect with the user's credentials.

        The password is used once to obtain login tokens and is not stored.
        If the result status is "needs_mfa", ask the user for the code Garmin
        just sent them and call garmin_submit_mfa_code. Tokens last about six
        months; after that, log in again.

        Args:
            email: Garmin Connect account email
            password: Garmin Connect account password
        """
        email = (email or "").strip()
        if not email or not password:
            return _result("error", "Email and password are required.")
        try:
            garmin, client_state = await run_in_threadpool(start_login, email, password, is_cn)
            if client_state is not None:
                _prune_expired()
                with _pending_lock:
                    _pending_mfa[_TOOL_MFA_KEY] = (
                        garmin, client_state, time.monotonic() + _MFA_TTL_SECONDS,
                    )
                return _result(
                    "needs_mfa",
                    "Garmin sent a verification code by email or SMS. Ask the "
                    "user for it and call garmin_submit_mfa_code within 10 minutes.",
                )
            await run_in_threadpool(finish_login, garmin, pending_client, tokenstore, is_cn)
        except Exception as err:  # noqa: BLE001 - reported to the caller
            print(f"MCP login failed: {type(err).__name__}", file=sys.stderr)
            return _result("error", _error_text(err))
        print("MCP login succeeded; Garmin client replaced.", file=sys.stderr)
        return _result("ready", "Logged in to Garmin Connect. Garmin tools work now.")

    @app.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True,
    ))
    async def garmin_submit_mfa_code(code: str) -> str:
        """Finish a Garmin login that returned "needs_mfa".

        Args:
            code: The verification code Garmin sent to the user
        """
        _prune_expired()
        with _pending_lock:
            pending = _pending_mfa.get(_TOOL_MFA_KEY)
        if pending is None:
            return _result("error", "No login is waiting for a code, or it expired. Call garmin_login again.")
        code = (code or "").strip()
        if not code:
            return _result("error", "The verification code is required.")
        garmin, client_state, _ = pending
        try:
            await run_in_threadpool(garmin.resume_login, client_state, code)
            await run_in_threadpool(finish_login, garmin, pending_client, tokenstore, is_cn)
        except Exception as err:  # noqa: BLE001 - reported to the caller
            print(f"MCP login MFA step failed: {type(err).__name__}", file=sys.stderr)
            return _result("error", _error_text(err))
        with _pending_lock:
            _pending_mfa.pop(_TOOL_MFA_KEY, None)
        print("MCP login succeeded; Garmin client replaced.", file=sys.stderr)
        return _result("ready", "Logged in to Garmin Connect. Garmin tools work now.")

    return app

"""EVE SSO login and skill/fitting import.

Two ways to log in (see docs/ESI.md):

  Your own EVE application (recommended) - set in the MCP server's environment:
      EVE_ESI_CLIENT_ID      client ID from developers.eveonline.com
      EVE_ESI_CLIENT_SECRET  secret key (optional with PKCE, but use it if your app has one)
      EVE_ESI_CALLBACK_URL   must match the app's callback exactly (default http://localhost:8765/callback)
  EVE redirects straight to the local callback server.

  Pyfa's public application (fallback when EVE_ESI_CLIENT_ID is unset) - zero setup, consent page
  says "pyfa"; EVE redirects to pyfa-org.github.io/Pyfa/callback which forwards to our local server.

Either way: authorization code + PKCE, JWT validated against EVE's keys, refresh tokens encrypted with
Pyfa's per-install key in data/esi_tokens.json. Pyfa supplies the SSO metadata, JWT validation,
encryption, endpoint paths and the callback HTTP server.
"""
import base64
import datetime
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse

import requests

from . import pilots
from .errors import FitError
from .pyfa_bridge import DATA_DIR, boot

TOKENS = os.path.join(DATA_DIR, "esi_tokens.json")
SCOPES = ["esi-skills.read_skills.v1", "esi-fittings.read_fittings.v1"]
DEFAULT_CALLBACK = "http://localhost:8765/callback"
LOGIN_TIMEOUT_S = 600
USER_AGENT = "eve-fit-mcp (https://github.com/apettey/eve-fit-mcp)"

_access = None
_server = None
_server_thread = None
_login = {"status": "idle"}
_pending = {}
_lock = threading.Lock()


class App:
    """Which EVE application we authenticate as."""

    def __init__(self):
        import config
        cid = os.environ.get("EVE_ESI_CLIENT_ID", "").strip()
        if cid:
            self.kind = "own"
            self.client_id = cid
            self.secret = os.environ.get("EVE_ESI_CLIENT_SECRET", "").strip() or None
            self.callback = os.environ.get("EVE_ESI_CALLBACK_URL", "").strip() or DEFAULT_CALLBACK
        else:
            server = config.supported_servers["Tranquility"]
            self.kind = "pyfa"
            self.client_id = server.client_id
            self.secret = None
            self.callback = server.callback

    def describe(self):
        return {"app": self.kind, "client_id": self.client_id, "callback": self.callback,
                "uses_secret": bool(self.secret), "scopes": SCOPES}

    def token_request(self, data):
        acc = _esi()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT}
        auth = None
        if self.secret:
            auth = (self.client_id, self.secret)  # HTTP Basic, per EVE SSO docs
        else:
            data = dict(data, client_id=self.client_id)
        r = requests.post(acc.oauth_token, data=data, headers=headers, auth=auth, timeout=30)
        if r.status_code != 200:
            raise FitError("EVE SSO token request failed (%s): %s" % (r.status_code, r.text[:300]))
        return r.json()


def _esi():
    """Pyfa's EsiAccess: SSO metadata (cached) and JWT validation."""
    global _access
    boot()
    if _access is None:
        import service.esiAccess as esi_access
        _access = esi_access.EsiAccess()
    return _access


def app_info():
    boot()
    return App().describe()


# ---- token storage -------------------------------------------------------------------------

def _read_tokens():
    if not os.path.isfile(TOKENS):
        return {}
    with open(TOKENS, encoding="utf-8") as f:
        return json.load(f)


def _write_tokens(tokens):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOKENS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=1)
    os.replace(tmp, TOKENS)


def _store(character_id, name, token, app):
    import config
    tokens = _read_tokens()
    tokens[str(character_id)] = {
        "name": name,
        "client_id": app.client_id,  # refresh tokens are bound to the app that issued them
        "refresh_token": config.cipher.encrypt(token["refresh_token"].encode()).decode(),
    }
    _write_tokens(tokens)


def _access_token(character_id):
    """Fresh access token via the stored refresh token (EVE rotates it; we store the new one)."""
    import config
    rec = _read_tokens().get(str(character_id))
    if rec is None:
        raise FitError("No ESI login for character %s - run esi_login first" % character_id)
    app = App()
    if rec.get("client_id") != app.client_id:
        raise FitError("%s was logged in with a different EVE app (client %s); run esi_login again"
                       % (rec["name"], rec.get("client_id")))
    refresh = config.cipher.decrypt(rec["refresh_token"].encode()).decode()
    token = app.token_request({"grant_type": "refresh_token", "refresh_token": refresh})
    if "refresh_token" in token:
        _store(character_id, rec["name"], token, app)
    return token["access_token"]


def _get(character_id, endpoint, access_token=None):
    from service.const import EsiEndpoints
    acc = _esi()
    token = access_token or _access_token(character_id)
    url = acc.esi_url + getattr(EsiEndpoints, endpoint).value.format(character_id=character_id)
    r = requests.get(url, params={"datasource": "tranquility"}, timeout=30,
                     headers={"Authorization": "Bearer " + token, "User-Agent": USER_AGENT,
                              "Accept": "application/json"})
    if r.status_code >= 400:
        raise FitError("ESI %s failed (%s): %s" % (endpoint, r.status_code, r.text[:300]))
    return r.json()


# ---- login ---------------------------------------------------------------------------------

def login_start(pilot_name=None):
    """Begin SSO login; returns the URL the user must open."""
    global _server, _server_thread
    from service.server import AuthHandler, StoppableHTTPServer
    acc = _esi()
    app = App()

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).decode().rstrip("=")
    nonce = secrets.token_urlsafe(24)

    with _lock:
        _stop_server()
        if app.kind == "own":
            cb = urllib.parse.urlparse(app.callback)
            if cb.hostname not in ("localhost", "127.0.0.1") or not cb.port:
                raise FitError("EVE_ESI_CALLBACK_URL must be http://localhost:<port>/... (got %s)" % app.callback)
            try:
                _server = StoppableHTTPServer((cb.hostname, cb.port), AuthHandler)
            except OSError as e:
                raise FitError("Can't listen on %s (%s) - is another login running or the port taken?"
                               % (app.callback, e))
            redirect_uri, state = app.callback, nonce
        else:
            # Pyfa's callback page reads {mode, redirect, state} from state and forwards to `redirect`
            _server = StoppableHTTPServer(("localhost", 0), AuthHandler)
            local = "http://localhost:%d" % _server.socket.getsockname()[1]
            redirect_uri = app.callback
            state = base64.b64encode(json.dumps({"mode": 0, "redirect": local, "state": nonce}).encode()).decode()
        uri = "%s?%s" % (acc.oauth_authorize, urllib.parse.urlencode({
            "response_type": "code", "redirect_uri": redirect_uri, "client_id": app.client_id,
            "scope": " ".join(SCOPES), "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"}))
        _pending.clear()
        _pending.update(nonce=nonce, verifier=verifier.decode(), redirect_uri=redirect_uri, app=app,
                        pilot_name=pilot_name)
        _login.clear()
        _login.update(status="waiting", app=app.kind, started=time.time())
        server = _server
        _server_thread = threading.Thread(target=server.serve, args=(_on_callback,), name="esi-callback",
                                          daemon=True)
        _server_thread.start()
    t = threading.Timer(LOGIN_TIMEOUT_S, _expire, args=(server,))
    t.daemon = True
    t.start()
    return uri


def _stop_server():
    # serve() polls with a 1s timeout and closes the socket on exit; wait so a fixed port can be rebound
    global _server, _server_thread
    if _server is not None:
        _server.stop()
    if _server_thread is not None and _server_thread is not threading.current_thread():
        _server_thread.join(timeout=5)
    _server = _server_thread = None


def _expire(server):
    server.stop()
    with _lock:
        if _login.get("status") == "waiting" and _server is server:
            _login.update(status="error", error="login timed out after %ds" % LOGIN_TIMEOUT_S)


def _on_callback(message):
    # Runs on the callback server thread; a GenericSsoError's text is shown in the browser.
    from service.esiAccess import GenericSsoError
    try:
        result = _finish(message)
    except Exception as e:
        with _lock:
            _login.update(status="error", error=str(e))
        raise GenericSsoError(str(e))
    with _lock:
        _login.update(status="done", **result)


def _state_nonce(state):
    with _lock:
        expected = _pending.get("nonce")
    if state == expected:
        return state
    try:  # Pyfa-app flow wraps the nonce
        return json.loads(base64.b64decode(state))["state"]
    except Exception:
        return None


def _finish(message):
    with _lock:
        pending = dict(_pending)
    if not pending:
        raise FitError("No login in progress - call esi_login first")
    if not message or "code" not in message:
        raise FitError("No authorisation code in callback (%s)" % (message or {}).get("error", "empty"))
    if _state_nonce(message.get("state")) != pending["nonce"]:
        raise FitError("SSO state mismatch - start the login again")

    app = pending["app"]
    token = app.token_request({"grant_type": "authorization_code", "code": message["code"],
                               "code_verifier": pending["verifier"]})
    jwt = _esi().validate_eve_jwt(token["access_token"])
    sub = jwt["sub"].split(":")
    if len(sub) != 3 or sub[0] != "CHARACTER":
        raise FitError("Unexpected JWT subject %r" % jwt["sub"])
    cid = int(sub[-1])
    granted = jwt.get("scp") or []
    granted = [granted] if isinstance(granted, str) else granted
    _store(cid, jwt["name"], token, app)
    with _lock:
        _pending.clear()
    pilot = _fetch_skills(cid, pending.get("pilot_name") or jwt["name"], token["access_token"])
    return {"character": jwt["name"], "character_id": cid, "pilot": pilot["name"],
            "skills": len(pilot["skills"]), "total_sp": pilot["total_sp"], "scopes": granted}


def login_complete(code):
    """Manual fallback: paste the code from Pyfa's callback page, or the full redirect URL."""
    code = code.strip()
    if code.startswith("http"):
        message = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(code).query).items()}
    else:
        try:
            message = json.loads(base64.b64decode(code))
        except Exception:
            raise FitError("Paste either the code from the callback page or the full URL the browser ended on")
    result = _finish(message)
    with _lock:
        _login.update(status="done", **result)
        _stop_server()
    return result


def login_status():
    with _lock:
        return {k: v for k, v in _login.items() if k != "started"}


# ---- data ----------------------------------------------------------------------------------

def _fetch_skills(character_id, pilot_name, access_token=None):
    data = _get(character_id, "CHAR_SKILLS", access_token)
    # active_skill_level respects alpha clone limits; that's what applies in space
    skills = {s["skill_id"]: s["active_skill_level"] for s in data["skills"]}
    return pilots.save_esi(pilot_name, character_id, skills, total_sp=data.get("total_sp"))


def characters():
    return [{"character_id": int(cid), "name": rec["name"]} for cid, rec in _read_tokens().items()]


def _character_id(pilot_or_character):
    q = str(pilot_or_character).lower()
    for c in characters():
        if q in (c["name"].lower(), str(c["character_id"])):
            return c["character_id"]
    for p in pilots.list_pilots():
        if p["kind"] == "esi" and p["name"].lower() == q:
            return p["character_id"]
    raise FitError("No ESI character or pilot %r; logged in: %s" % (
        pilot_or_character, ", ".join(c["name"] for c in characters()) or "none"))


def refresh_skills(pilot_or_character):
    """Re-pull skills for a logged-in character into its pilot."""
    cid = _character_id(pilot_or_character)
    name = next((p["name"] for p in pilots.list_pilots()
                 if p["kind"] == "esi" and p.get("character_id") == cid),
                _read_tokens()[str(cid)]["name"])
    rec = _fetch_skills(cid, name)
    return pilots.describe(rec["name"])


def list_fittings(pilot_or_character):
    from .fitting import get_item
    out = []
    for f in _get(_character_id(pilot_or_character), "CHAR_FITTINGS"):
        try:
            ship = get_item(f["ship_type_id"]).typeName
        except FitError:
            ship = f["ship_type_id"]
        out.append({"fitting_id": f["fitting_id"], "name": f["name"], "ship": ship, "items": len(f["items"])})
    return out


def fitting_eft(pilot_or_character, fitting_id):
    """Fetch one in-game fitting and convert it to EFT with Pyfa's ESI importer."""
    from .pyfa_bridge import P
    fit = next((f for f in _get(_character_id(pilot_or_character), "CHAR_FITTINGS")
                if f["fitting_id"] == int(fitting_id)), None)
    if fit is None:
        raise FitError("No fitting %s on that character" % fitting_id)
    P.load()
    eos_fit = P.Port.importESI(json.dumps(fit))
    return P.Port.exportEft(eos_fit, {o.value: True for o in P.PortEftOptions})


def logout(pilot_or_character):
    cid = _character_id(pilot_or_character)
    tokens = _read_tokens()
    tokens.pop(str(cid), None)
    _write_tokens(tokens)
    return {"logged_out": cid}

"""Offline end-to-end test of the SSO login: real local callback server, mocked EVE endpoints."""
import base64
import hashlib
import json
import socket
import time
import urllib.parse
import urllib.request

import pytest

from eve_fit_mcp import esi, pilots


def _free_port():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


class FakeAccess:
    oauth_authorize = "https://login.example/v2/oauth/authorize"
    oauth_token = "https://login.example/v2/oauth/token"
    esi_url = "https://esi.example"

    def validate_eve_jwt(self, token):
        assert token == "ACCESS1"
        return {"sub": "CHARACTER:EVE:90000001", "name": "Test Pilot", "scp": esi.SCOPES}


class Resp:
    def __init__(self, data, code=200):
        self._data, self.status_code, self.text = data, code, json.dumps(data)

    def json(self):
        return self._data


@pytest.fixture
def env(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("EVE_ESI_CLIENT_ID", "my-client")
    monkeypatch.setenv("EVE_ESI_CLIENT_SECRET", "my-secret")
    monkeypatch.setenv("EVE_ESI_CALLBACK_URL", "http://localhost:%d/callback" % port)
    monkeypatch.setattr(esi, "TOKENS", str(tmp_path / "tokens.json"))
    monkeypatch.setattr(pilots, "PILOT_DIR", str(tmp_path / "pilots"))
    monkeypatch.setattr(esi, "_esi", lambda: FakeAccess())
    calls = []

    def fake_post(url, data, headers, auth, timeout):
        calls.append(("post", url, dict(data), auth))
        if data["grant_type"] == "authorization_code":
            return Resp({"access_token": "ACCESS1", "refresh_token": "REFRESH1", "expires_in": 1199})
        assert data["grant_type"] == "refresh_token"
        return Resp({"access_token": "ACCESS2", "refresh_token": "REFRESH2", "expires_in": 1199})

    def fake_get(url, params, timeout, headers):
        calls.append(("get", url, headers["Authorization"]))
        assert url == "https://esi.example/v4/characters/90000001/skills/"
        return Resp({"total_sp": 12345678, "skills": [
            {"skill_id": 3436, "active_skill_level": 5, "trained_skill_level": 5},   # Drones
            {"skill_id": 3332, "active_skill_level": 3, "trained_skill_level": 4},   # Gallente Cruiser
        ]})

    monkeypatch.setattr(esi.requests, "post", fake_post)
    monkeypatch.setattr(esi.requests, "get", fake_get)
    pilots._cache.clear()
    yield port, calls
    with esi._lock:
        esi._stop_server()
    pilots._cache.clear()


def _wait(status, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        st = esi.login_status()
        if st["status"] != "waiting":
            return st
        time.sleep(0.05)
    raise AssertionError("login still waiting: %s" % status)


def test_own_app_login_end_to_end(env):
    port, calls = env
    assert esi.app_info()["app"] == "own" and esi.app_info()["uses_secret"]
    url = esi.login_start("Main")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["client_id"] == "my-client"
    assert q["redirect_uri"] == "http://localhost:%d/callback" % port
    assert q["scope"] == "esi-skills.read_skills.v1 esi-fittings.read_fittings.v1"
    assert q["code_challenge_method"] == "S256"

    # wrong state is rejected and leaves no token
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen("http://localhost:%d/callback?code=abc&state=forged" % port, timeout=5)
    assert esi.login_status()["status"] == "error"

    url = esi.login_start("Main")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    with urllib.request.urlopen("http://localhost:%d/callback?code=abc&state=%s" % (port, q["state"]),
                                timeout=5) as r:
        assert r.status == 200
    st = _wait("done")
    assert st["status"] == "done", st
    assert st["pilot"] == "Main" and st["character_id"] == 90000001 and st["total_sp"] == 12345678

    post = next(c for c in calls if c[0] == "post")
    assert post[3] == ("my-client", "my-secret")              # Basic auth with the secret
    verifier = post[2]["code_verifier"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == q["code_challenge"]                   # PKCE pair matches

    raw = open(esi.TOKENS, encoding="utf-8").read()
    assert "REFRESH1" not in raw                              # encrypted at rest
    assert esi.characters() == [{"character_id": 90000001, "name": "Test Pilot"}]

    info = pilots.describe("Main", ["Drones", "Gallente Cruiser"])
    assert info["kind"] == "esi"
    assert info["levels"]["Drones"] == 5 and info["levels"]["Gallente Cruiser"] == 3  # active, not trained

    # refresh rotates the stored refresh token
    esi.refresh_skills("Main")
    assert any(c[0] == "post" and c[2].get("refresh_token") == "REFRESH1" for c in calls)
    esi.refresh_skills("Test Pilot")
    assert any(c[0] == "post" and c[2].get("refresh_token") == "REFRESH2" for c in calls)

    esi.logout("Main")
    assert esi.characters() == []


def test_switching_app_requires_relogin(env, monkeypatch):
    port, _ = env
    url = esi.login_start()
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))["state"]
    urllib.request.urlopen("http://localhost:%d/callback?code=abc&state=%s" % (port, state), timeout=5).close()
    assert _wait("done")["status"] == "done"
    monkeypatch.setenv("EVE_ESI_CLIENT_ID", "other-client")
    with pytest.raises(Exception, match="different EVE app"):
        esi.refresh_skills("Test Pilot")

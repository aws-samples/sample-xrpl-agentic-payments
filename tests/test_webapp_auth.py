# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for how webapp/app.py refuses an unauthenticated caller.

Two bugs live here, and both were invisible because the refusal LOOKED fine from
the server side:

1. /api/metrics, /api/last-payment and /api/traces answered an unauthenticated
   caller with HTTP 200 and a body of {"error": "unauthorized"}. The dashboard
   tested `data.type === 'metrics'`, which was simply false, so it kept
   displaying the numbers it had from before the session expired. A 200 that
   means "denied" is a lie told to every caller that reads status codes.

2. The WebSocket handshake is refused BEFORE accept(), which is correct — but
   that means there is no close frame, so the ws.close(code=1008) in
   _accept_authenticated never reaches the client. The browser gets HTTP 403 and
   reports 1006. Both pages branched on `e.code === 1008`, so the branch was
   dead and they reconnected forever. The fix is GET /api/session, which the
   pages probe on close to tell an expired session from a dropped network.

So these tests assert on the STATUS, not the body: the status is the part the
clients act on, and the part that was wrong.
"""

import hashlib
import hmac
import importlib
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("fastapi", reason="FastAPI not installed")
pytest.importorskip("starlette", reason="Starlette not installed")

from starlette.testclient import TestClient  # noqa: E402

USERNAME = "test-user"
PASSWORD = "test-password"

# Endpoints that must never serve data to an unauthenticated caller. /api/session
# is deliberately in the list: it is the probe the browser uses to decide whether
# to redirect to /login, so if it ever answers 200 without a cookie the pages
# reconnect forever again.
PROTECTED_JSON_ENDPOINTS = [
    "/api/session",
    "/api/metrics",
    "/api/last-payment",
    "/api/traces",
]


@pytest.fixture(scope="module")
def webapp(tmp_path_factory):
    """Import webapp.app with credentials set, and hand back the module.

    The module raises at import time if the credentials are absent and derives
    SESSION_SECRET from the password at import time too, so the environment has
    to be in place before the import and the module has to be reloaded if it was
    already imported by another test.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("XRPL_AGENTIC_USERNAME", USERNAME)
    monkeypatch.setenv("XRPL_AGENTIC_PASSWORD", PASSWORD)
    # Without this the HTTPS middleware answers every test request with a 307 to
    # https:// and nothing below ever reaches a route.
    monkeypatch.setenv("XRPL_AGENTIC_INSECURE_COOKIES", "1")
    # Keep the tests off any real AWS account: an unset runtime id is the
    # "not deployed" path, which _get_agent_metrics reports locally.
    monkeypatch.delenv("AGENTCORE_RUNTIME_ID", raising=False)
    monkeypatch.delenv("AGENTCORE_GATEWAY_ID", raising=False)

    if "webapp.app" in sys.modules:
        module = importlib.reload(sys.modules["webapp.app"])
    else:
        module = importlib.import_module("webapp.app")

    yield module

    monkeypatch.undo()


@pytest.fixture
def client(webapp):
    return TestClient(webapp.app)


def _session_cookie(webapp, username=USERNAME, issued_at=None):
    """Mint a valid session cookie the way the app's own /login does."""
    issued_at = int(time.time()) if issued_at is None else issued_at
    return webapp._sign_session(username, issued_at)


@pytest.fixture(scope="module")
def live_server():
    """A real uvicorn process, for the one assertion TestClient cannot make.

    Everything else in this file uses TestClient, which is faster and needs no
    port. But TestClient speaks ASGI directly and never performs an HTTP
    handshake, so it cannot show what a browser is told when a WebSocket is
    refused — and that is the whole subject of the test below.
    """
    import socket
    import subprocess
    import urllib.error
    import urllib.request

    with socket.socket() as probe:          # let the OS pick a free port
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = {
        **os.environ,
        "XRPL_AGENTIC_USERNAME": USERNAME,
        "XRPL_AGENTIC_PASSWORD": PASSWORD,
        "XRPL_AGENTIC_INSECURE_COOKIES": "1",
    }
    # Suppresses semgrep's dangerous-subprocess-use-audit. No shell, and the
    # argv is fully closed: every element is a literal except sys.executable
    # (this interpreter's own path) and str(port), an int handed to us by the
    # kernel above. Nothing here is attacker-influenced, so there is no
    # injection surface for the audit rule to be pointing at.
    #
    # Deliberately bare rather than `# nosemgrep: <rule-id>`. An id-qualified
    # directive only suppresses on an exact id match, and this rule's id
    # differs by how the ruleset was loaded — the Holmes report calls it
    # python.lang.security.audit.dangerous-subprocess-use-audit, a registry
    # (`--config r/...`) run calls it that plus a repeated
    # `.dangerous-subprocess-use-audit`. Naming either one silently fails
    # against the other; bare matches both.
    # nosemgrep
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "webapp.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 30
        while True:
            if proc.poll() is not None:
                stderr = (proc.stderr.read() or b"").decode()[-2000:]
                pytest.skip(f"uvicorn failed to start: {stderr}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except urllib.error.HTTPError:
                break                      # any HTTP reply means it is serving
            except OSError:
                if time.time() > deadline:
                    pytest.skip("uvicorn did not become ready within 30s")
                time.sleep(0.2)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _handshake_status_line(port, path):
    """Send a WebSocket upgrade by hand and return the server's status line.

    Raw socket rather than a WebSocket client library: the point is to read the
    HTTP response to the handshake, which a client library turns into an
    exception. No third-party dependency either.
    """
    import base64
    import socket

    key = base64.b64encode(b"0123456789abcdef").decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: http://127.0.0.1\r\n"
        "\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request.encode())
        response = b""
        while b"\r\n" not in response and len(response) < 4096:
            chunk = sock.recv(1024)
            if not chunk:
                break
            response += chunk
    return response.split(b"\r\n")[0].decode(errors="replace")


# ─────────────────────────────────────────────────────────────────────────────
# Bug 1: a refusal must be a 401, not a 200 with an error in the body
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", PROTECTED_JSON_ENDPOINTS)
def test_no_cookie_is_401(client, path):
    """No cookie at all: the status must say denied."""
    response = client.get(path)
    assert response.status_code == 401, (
        f"{path} answered {response.status_code}; a caller that trusts the "
        f"status code would treat this as data"
    )


@pytest.mark.parametrize("path", PROTECTED_JSON_ENDPOINTS)
def test_forged_signature_is_401(client, webapp, path):
    """A cookie with the right shape and a wrong HMAC is not a session."""
    forged = f"{USERNAME}:{int(time.time())}:{'0' * 16}"
    client.cookies.set(webapp.SESSION_COOKIE, forged)
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", PROTECTED_JSON_ENDPOINTS)
def test_expired_cookie_is_401(client, webapp, path):
    """A correctly signed cookie past SESSION_TTL is refused.

    This is the case the dashboard actually hit — the user left the tab open
    overnight — and the case that used to render stale numbers indefinitely.
    """
    stale = int(time.time()) - webapp.SESSION_TTL - 60
    client.cookies.set(webapp.SESSION_COOKIE, _session_cookie(webapp, issued_at=stale))
    assert client.get(path).status_code == 401


def test_another_users_key_cannot_sign_a_session(client, webapp):
    """A token signed with a different secret is refused.

    SESSION_SECRET is derived from the configured password, so this stands in for
    "an attacker guessed the token format but not the password".
    """
    other_secret = hashlib.sha256(b"xrpl-agentic-session-not-the-password").digest()
    payload = f"{USERNAME}:{int(time.time())}"
    sig = hmac.new(other_secret, payload.encode(), hashlib.sha256).hexdigest()[:16]
    client.cookies.set(webapp.SESSION_COOKIE, f"{payload}:{sig}")
    assert client.get("/api/session").status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# The authenticated side, so the tests above cannot pass by refusing everyone
# ─────────────────────────────────────────────────────────────────────────────


def test_valid_cookie_gets_200_from_session_probe(client, webapp):
    client.cookies.set(webapp.SESSION_COOKIE, _session_cookie(webapp))
    response = client.get("/api/session")
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}


def test_valid_cookie_gets_200_from_last_payment(client, webapp):
    """Chosen because it touches no AWS API — the auth path is the same one."""
    client.cookies.set(webapp.SESSION_COOKIE, _session_cookie(webapp))
    response = client.get("/api/last-payment")
    assert response.status_code == 200
    assert response.json()["type"] == "last_payment"


def test_login_issues_a_cookie_that_the_probe_accepts(client, webapp):
    """End to end: the cookie /login sets is one /api/session honours.

    Pins the two halves together. _sign_session is used directly above, so
    without this a change to /login's cookie format could pass every other test
    while logging nobody in.
    """
    login = client.post(
        "/login",
        data={"username": USERNAME, "password": PASSWORD},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert webapp.SESSION_COOKIE in login.cookies
    assert client.get("/api/session").status_code == 200


def test_wrong_password_does_not_issue_a_cookie(client, webapp):
    login = client.post(
        "/login",
        data={"username": USERNAME, "password": "wrong"},
        follow_redirects=False,
    )
    assert webapp.SESSION_COOKIE not in login.cookies
    assert client.get("/api/session").status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Bug 2: the WebSocket refusal is an HTTP rejection, not a 1008 close frame
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/ws/chat", "/ws/metrics"])
def test_websocket_is_refused_before_accept(client, path):
    """An unauthenticated handshake never becomes a usable connection.

    This is the security property: refusing before accept() means the client
    cannot reach a state where it can send a frame. Note what this test does NOT
    tell you — see test_real_server_refuses_handshake_with_http_403 below.
    """
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path):
            pass


@pytest.mark.parametrize("path", ["/ws/chat", "/ws/metrics"])
def test_real_server_refuses_handshake_with_http_403(live_server, path):
    """The refusal a BROWSER sees is HTTP 403, which it reports as 1006.

    Needs a real server on a real socket. TestClient drives the ASGI app
    in-process and never performs an HTTP handshake, so it hands back the
    app's close(code=1008) verbatim — which is exactly the misreading that put
    an unreachable `e.code === 1008` branch in two pages. Under uvicorn the
    handshake is answered with 403 and no WebSocket ever exists, so the browser
    has no close code to read but 1006.

    If this ever starts returning 101-then-1008, the /api/session probe in
    index.html and observability.html could be replaced by reading e.code — and
    this test failing is the notification.
    """
    status_line = _handshake_status_line(live_server, path)
    assert status_line.startswith("HTTP/1.1 403"), (
        f"expected an HTTP 403 handshake rejection, got {status_line!r}; if the "
        f"handshake now succeeds the clients can read the close code directly"
    )


@pytest.mark.parametrize("path", ["/ws/chat", "/ws/metrics"])
def test_websocket_needs_a_session_cookie(client, webapp, path):
    """A valid cookie plus a same-origin request is accepted.

    Guards the happy path: the tests above would also pass if the endpoints
    refused everybody.
    """
    client.cookies.set(webapp.SESSION_COOKIE, _session_cookie(webapp))
    with client.websocket_connect(path, headers={"Origin": "http://testserver"}):
        pass


@pytest.mark.parametrize("origin", ["http://evil.example", ""])
def test_websocket_rejects_foreign_and_absent_origin(client, webapp, origin):
    """A cookie is not enough: browsers attach it to cross-origin ws:// too.

    An absent Origin is refused as well — a browser always sends one, so its
    absence is either a non-browser client or an attempt to dodge the check.
    """
    from starlette.websockets import WebSocketDisconnect

    client.cookies.set(webapp.SESSION_COOKIE, _session_cookie(webapp))
    headers = {"Origin": origin} if origin else {}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/metrics", headers=headers):
            pass

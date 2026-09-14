# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — Web Application

Serves three views:
1. / — Conversational AI interface (chat with payment agents)
2. /observability — Real-time agent metrics and traces
3. /architecture — Static architecture diagram with AWS icons + Ripple logo

Backend: FastAPI + WebSocket for streaming agent responses
Frontend: Vanilla JS (no build step, served as static files)
Deployment: Docker container behind an ALB (custom domain optional)
Authentication: Username/password with session cookie

This process also HOSTS the agents: the /ws/chat handler runs
src.agents.orchestrator.run_multi_agent_payment(), which drives five scoped
Strands agents in sequence. There is no managed Harness in this path.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Project imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.payments.request import PaymentRequestError, parse_payment_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("xrpl_agentic_payments.webapp")

app = FastAPI(
    title="XRPL Agentic Payments",
    description="Autonomous Cross-Border Payments on XRPL",
)

# ─────────────────────────────────────────────────────────────────────────────
# Deployed AgentCore resource identifiers
# ─────────────────────────────────────────────────────────────────────────────

# These are generated per-deployment. They used to be hardcoded to the ids of
# one particular deployment, which meant the observability tab silently reported
# on someone else's resources — or on nothing at all. Empty means "not deployed";
# the metrics endpoint degrades to reporting only local agent activity.
REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2"))
RUNTIME_ID = os.environ.get("AGENTCORE_RUNTIME_ID", "")
GATEWAY_ID = os.environ.get("AGENTCORE_GATEWAY_ID", "")
PAYMENT_MANAGER_ID = os.environ.get("PAYMENT_MANAGER_ID", "")
RUNTIME_LOG_GROUP = os.environ.get(
    "AGENTCORE_RUNTIME_LOG_GROUP",
    f"/aws/bedrock-agentcore/runtime/{RUNTIME_ID}" if RUNTIME_ID else "",
)

# boto3 clients are cached: constructing one costs credential and endpoint
# resolution, and the observability endpoints run on a 5-second poll loop.
# Built lazily so the app still imports on a machine with no AWS credentials.
_boto_clients: dict = {}


def _client(service: str):
    """Return a cached boto3 client for `service` in the configured region."""
    if service not in _boto_clients:
        import boto3

        _boto_clients[service] = boto3.client(service, region_name=REGION)
    return _boto_clients[service]

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# ─────────────────────────────────────────────────────────────────────────────
# Authentication (session-based, username/password)
# ─────────────────────────────────────────────────────────────────────────────

# Credentials from environment — no hardcoded fallback.
# Set XRPL_AGENTIC_USERNAME / XRPL_AGENTIC_PASSWORD before starting the app
# (see .env.example). In AWS, source XRPL_AGENTIC_PASSWORD from Secrets Manager
# rather than a plaintext environment variable.
AUTH_USERNAME = os.environ.get("XRPL_AGENTIC_USERNAME")
AUTH_PASSWORD = os.environ.get("XRPL_AGENTIC_PASSWORD")

if not AUTH_USERNAME or not AUTH_PASSWORD:
    raise RuntimeError(
        "XRPL_AGENTIC_USERNAME and XRPL_AGENTIC_PASSWORD must be set. "
        "Copy .env.example to .env and set your own values, or inject them "
        "from AWS Secrets Manager in production."
    )

AUTH_PASSWORD_HASH = hashlib.sha256(AUTH_PASSWORD.encode()).hexdigest()

# HMAC-signed cookie key (derived from password — stable across pod restarts)
import hmac
SESSION_SECRET = hashlib.sha256(f"xrpl-agentic-session-{AUTH_PASSWORD}".encode()).digest()
SESSION_COOKIE = "xrpl_agentic_session"
SESSION_TTL = 86400  # 24 hours

# The session cookie authorises payments from the execution wallet, so it is
# Secure by default: without that flag a browser will send it over plain HTTP,
# where anything on the path can read and replay it. The opt-out exists only for
# running against http://localhost, where no browser will accept a Secure cookie.
INSECURE_COOKIES = os.environ.get("XRPL_AGENTIC_INSECURE_COOKIES", "").lower() in ("1", "true", "yes")
if INSECURE_COOKIES:
    logger.warning(
        "XRPL_AGENTIC_INSECURE_COOKIES is set — the session cookie will be sent "
        "over plain HTTP. Local development only; never set this in a deployment."
    )

# Browsers do not apply the same-origin policy to WebSocket handshakes, so a page
# on any origin can open ws:// to this app and the cookie rides along. The Origin
# header is the only thing distinguishing our own page from an attacker's, and it
# is one a browser will not let script forge. Comma-separated; empty means
# same-host-only, derived from the Host header.
ALLOWED_WS_ORIGINS = frozenset(
    origin.strip().rstrip("/").lower()
    for origin in os.environ.get("XRPL_AGENTIC_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
)


def _sign_session(username: str, issued_at: int) -> str:
    """Create an HMAC-signed session token that any pod can verify."""
    payload = f"{username}:{issued_at}"
    sig = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}:{sig}"


def _verify_session_token(token: str | None) -> bool:
    """Verify an HMAC-signed session token — no shared state needed.

    Takes the raw token rather than a Request so the WebSocket handlers can use
    the same check. A WebSocket is a `WebSocket`, not a `Request`, which is how
    /ws/chat and /ws/metrics ended up with no authentication at all: the only
    session check in the app could not be applied to them.
    """
    if not token:
        return False
    parts = token.split(":")
    if len(parts) != 3:
        return False
    username, issued_at_str, sig = parts
    try:
        issued_at = int(issued_at_str)
    except ValueError:
        return False
    if time.time() - issued_at > SESSION_TTL:
        return False
    expected = hmac.new(SESSION_SECRET, f"{username}:{issued_at}".encode(), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(sig, expected)


def verify_session(request: Request) -> bool:
    """Verify the session cookie on an HTTP request."""
    return _verify_session_token(request.cookies.get(SESSION_COOKIE))


def _origin_allowed(ws: WebSocket) -> bool:
    """Check a WebSocket handshake's Origin against the allowlist.

    A missing Origin is rejected. Browsers always send one on a WebSocket
    handshake, so its absence means either a non-browser client — which has no
    cookie jar to be abused, but also no business here — or an attempt to dodge
    the check.
    """
    origin = (ws.headers.get("origin") or "").strip().rstrip("/").lower()
    if not origin:
        return False
    if ALLOWED_WS_ORIGINS:
        return origin in ALLOWED_WS_ORIGINS

    # No allowlist configured: accept only this app's own origin. Behind the ALB
    # the scheme the browser used is in x-forwarded-proto, not the internal
    # request scheme, so both are accepted for the app's own host.
    host = (ws.headers.get("host") or "").strip().lower()
    if not host:
        return False
    return origin in (f"https://{host}", f"http://{host}")


async def _accept_authenticated(ws: WebSocket, endpoint: str) -> bool:
    """Authenticate a WebSocket handshake, then accept it. False if refused.

    The close happens BEFORE accept() so an unauthenticated client never reaches
    a state where it can send a frame. Keep it that way.

    THE 1008 BELOW NEVER REACHES THE BROWSER. Closing before accept() means no
    WebSocket connection was ever established, so there is no close frame to put
    a code in: the ASGI server answers the handshake with HTTP 403, and the
    browser reports close code 1006 (abnormal) for all three refusal reasons.
    Verified against uvicorn — bad cookie, foreign Origin and absent Origin all
    return 403.

    So a client CANNOT distinguish "your session expired" from "the network
    dropped" by reading e.code, and the two pages that tried had an unreachable
    branch and reconnected forever. They now probe GET /api/session on close,
    which answers 401 or 200. If you add a WebSocket endpoint here, do the same
    in its client.
    """
    if not _origin_allowed(ws):
        logger.warning(
            "Rejected %s: disallowed Origin %r", endpoint, ws.headers.get("origin")
        )
        await ws.close(code=1008)
        return False
    if not _verify_session_token(ws.cookies.get(SESSION_COOKIE)):
        logger.warning("Rejected %s: no valid session cookie", endpoint)
        await ws.close(code=1008)
        return False
    await ws.accept()
    return True


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Login page."""
    return (TEMPLATES / "login.html").read_text()


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Authenticate user and create signed session cookie."""
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    if username == AUTH_USERNAME and password_hash == AUTH_PASSWORD_HASH:
        token = _sign_session(username, int(time.time()))
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=not INSECURE_COOKIES,
            samesite="lax",
            max_age=SESSION_TTL,
            path="/",
        )
        logger.info(f"Login successful: {username}")
        return response
    logger.warning(f"Login failed: {username}")
    return RedirectResponse(url="/login?error=1", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    """Destroy session cookie."""
    response = RedirectResponse(url="/login", status_code=303)
    # The attributes must match the ones it was set with, or the browser keeps
    # the original cookie and the "logout" leaves the session live.
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=not INSECURE_COOKIES,
        samesite="lax",
        path="/",
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Transport security
# ─────────────────────────────────────────────────────────────────────────────


@app.middleware("http")
async def enforce_https(request: Request, call_next):
    """Redirect plain HTTP to HTTPS and set HSTS.

    The app runs behind an ALB that terminates TLS, so the request reaching this
    process is always http:// — the browser's scheme is only in
    x-forwarded-proto. A Secure cookie on its own is not enough: without this,
    a first request to http:// is answered normally and the login form is
    submitted in the clear.
    """
    if INSECURE_COOKIES:
        return await call_next(request)

    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = forwarded_proto or request.url.scheme

    # The health check is exempt: the ALB target group probes over plain HTTP
    # from inside the VPC and a redirect would mark the target unhealthy.
    if scheme == "http" and request.url.path != "/health":
        return RedirectResponse(
            url=str(request.url.replace(scheme="https")), status_code=307
        )

    response = await call_next(request)
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
    )
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Overview page — redirects to /overview."""
    if not verify_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url="/overview", status_code=303)


@app.get("/overview", response_class=HTMLResponse)
async def overview(request: Request):
    """Prototype overview — why, what, how."""
    if not verify_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return (TEMPLATES / "overview.html").read_text()


@app.get("/payments", response_class=HTMLResponse)
async def payments(request: Request):
    """Payment execution interface (requires auth)."""
    if not verify_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return (TEMPLATES / "index.html").read_text()


@app.get("/observability", response_class=HTMLResponse)
async def observability(request: Request):
    """Agent observability dashboard (requires auth)."""
    if not verify_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return (TEMPLATES / "observability.html").read_text()


@app.get("/architecture", response_class=HTMLResponse)
async def architecture(request: Request):
    """Architecture diagram page (requires auth)."""
    if not verify_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return (TEMPLATES / "architecture.html").read_text()


@app.get("/health")
async def health():
    """Health check for the ALB target group."""
    return {"status": "healthy", "service": "xrpl-agentic-payments"}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — Conversational Agent Interface
# ─────────────────────────────────────────────────────────────────────────────

# The observability page reads the most recent run's events after a navigation.
# It used to reach into the orchestrator's module-level event bus, which no
# longer exists — each payment now owns its own bus — so the completed run is
# recorded here instead. One run only, and only what was already streamed to a
# browser, so nothing accumulates.
_LAST_PAYMENT: dict = {"events": []}


def _record_last_payment(result: dict):
    """Keep the finished run's events for /api/last-payment."""
    _LAST_PAYMENT["events"] = result.get("events", [])


def _format_thought(event: dict) -> str:
    """Format an agent event into a human-readable chain-of-thought message."""
    agent = event.get("agent", "")
    evt = event.get("event", "")
    data = event.get("data", {})

    if evt == "agent_start":
        labels = {
            "compliance": "🛡️ COMPLIANCE AGENT starting — screening counterparty against OFAC sanctions list...",
            "fx_intelligence": "📊 FX INTELLIGENCE AGENT starting — querying XRPL DEX for real-time rates...",
            "routing": "🗺️ ROUTING AGENT starting — analyzing optimal payment path...",
            "execution": "⚡ EXECUTION AGENT starting — constructing and signing XRPL transaction...",
            "settlement_monitor": "✅ SETTLEMENT MONITOR starting — watching XRPL ledger for confirmation...",
        }
        return labels.get(agent, f"Agent {agent} starting...")

    if evt == "agent_complete":
        if agent == "compliance":
            status = data.get("status", "?")
            return f"🛡️ Compliance result: **{status}** — {'safe to proceed' if status == 'CLEAR' else 'PAYMENT BLOCKED'}"
        if agent == "fx_intelligence":
            return f"📊 FX analysis complete — {data.get('paths_found', 0)} path(s) found on XRPL DEX"
        if agent == "routing":
            return f"🗺️ Routing decision: **{data.get('decision', '?')}** — direct path, 0 intermediaries"
        if agent == "execution":
            h = data.get("hash", "")
            return f"⚡ Transaction submitted — hash: `{h[:16]}...`" if h else "⚡ Execution complete"
        if agent == "settlement_monitor":
            return f"✅ **SETTLED** — confirmed on XRPL ledger #{data.get('ledger_index', '?')}. Irreversible."

    if evt == "tool_call":
        tool = data.get("tool", "?")
        return f"  → Calling tool: `{tool}`"

    if evt == "compliance_result":
        return f"  → Sanctions check: **{data.get('status', '?')}** for {data.get('entity', '?')}"

    if evt == "payment_submitted":
        return f"  → Payment broadcast to XRPL network (hash: `{data.get('hash', '?')[:20]}...`)"

    if evt == "settlement_confirmed":
        return f"  → Finality confirmed on ledger #{data.get('ledger_index', '?')}"

    if evt == "approval_required":
        limit = data.get("threshold_usd")
        limit_str = f"${limit:,.0f}" if isinstance(limit, (int, float)) else "the"
        return (
            f"⏸️ **HUMAN APPROVAL REQUIRED** — {data.get('amount', '?')} "
            f"{data.get('currency', '')} exceeds {limit_str} autonomous limit. "
            "Execution halted; no transaction was submitted."
        )

    return ""


@app.websocket("/ws/chat")
async def chat_ws(ws: WebSocket):
    """WebSocket endpoint for real-time agent conversation.

    This socket can spend from the execution wallet, so it is authenticated on
    the handshake — it used to accept every connection, which meant anyone who
    could reach the port could submit a payment without ever logging in.
    """
    if not await _accept_authenticated(ws, "/ws/chat"):
        return
    logger.info("Client connected to chat WebSocket")

    async def send_safe(payload: dict):
        """Send JSON, ignoring if the connection is already closed."""
        try:
            await ws.send_json(payload)
        except Exception:
            pass

    try:
        while True:
            data = await ws.receive_text()

            # Handle ping from client heartbeat
            if data == '{"type":"ping"}':
                await send_safe({"type": "pong"})
                continue

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await send_safe({"type": "error", "message": "Malformed JSON"})
                continue
            if not isinstance(msg, dict):
                await send_safe({"type": "error", "message": "Expected a JSON object"})
                continue

            instruction = msg.get("message", "")

            if not instruction:
                await send_safe({"type": "error", "message": "Empty instruction"})
                continue

            # Validate the financial fields before anything else happens. There
            # are deliberately no defaults here: the previous code fell back to
            # 100 USD to a hardcoded test address whenever the browser's regex
            # failed to parse the instruction, so a typo became a real payment
            # to an address the user never named.
            try:
                payment_request = parse_payment_request(msg)
            except PaymentRequestError as e:
                await send_safe({
                    "type": "error",
                    "message": f"I could not read that as a payment: {e}",
                })
                continue

            await send_safe({"type": "status", "agent": "orchestrator", "message": "Processing payment instruction..."})
            await send_safe({"type": "request", "data": payment_request.to_dict()})

            try:
                from src.agents.orchestrator import PaymentContext, run_multi_agent_payment
                import threading

                loop = asyncio.get_event_loop()
                async_queue = asyncio.Queue()

                def on_event(event):
                    loop.call_soon_threadsafe(async_queue.put_nowait, event.to_dict())

                # One context per payment, listener attached before the run
                # starts. Nothing is shared with any other connection: the old
                # module-level bus meant a second user's request reset the first
                # user's event stream and leaked the first user's events to them.
                context = PaymentContext()
                context.bus.on_event(on_event)

                result_holder = [None]
                error_holder = [None]
                done_event = asyncio.Event()

                def run_agents():
                    try:
                        result_holder[0] = run_multi_agent_payment(
                            payment_request, context=context
                        )
                    except Exception as e:
                        error_holder[0] = str(e)
                        logger.error(f"Agent thread error: {e}", exc_info=True)
                    finally:
                        loop.call_soon_threadsafe(done_event.set)

                thread = threading.Thread(target=run_agents, daemon=True)
                thread.start()

                # Stream events without blocking the event loop
                idle_count = 0
                while not done_event.is_set():
                    try:
                        event = await asyncio.wait_for(async_queue.get(), timeout=1.5)
                        idle_count = 0
                        cot_msg = _format_thought(event)
                        if cot_msg:
                            await send_safe({"type": "thought", "data": event, "message": cot_msg})
                    except asyncio.TimeoutError:
                        idle_count += 1
                        # Send thinking indicator every 1.5s so client knows we're alive
                        await send_safe({"type": "thinking", "ts": time.time(), "seconds": idle_count * 1.5})

                # Let any in-flight call_soon_threadsafe callbacks land
                await asyncio.sleep(0.1)

                # Drain remaining events
                while not async_queue.empty():
                    event = async_queue.get_nowait()
                    cot_msg = _format_thought(event)
                    if cot_msg:
                        await send_safe({"type": "thought", "data": event, "message": cot_msg})

                # The context and its bus go out of scope with the request, so
                # there is nothing to unsubscribe — the listener cannot outlive
                # the socket it writes to.
                if error_holder[0]:
                    await send_safe({"type": "error", "message": error_holder[0]})
                elif result_holder[0]:
                    _record_last_payment(result_holder[0])
                    await send_safe({"type": "result", "data": result_holder[0]})
                else:
                    await send_safe({"type": "error", "message": "No result returned from agents"})

            except ImportError as e:
                logger.error(f"Import error: {e}")
                await send_safe({"type": "error", "message": f"Server misconfiguration: {e}"})
            except Exception as e:
                logger.error(f"Agent execution error: {e}", exc_info=True)
                await send_safe({"type": "error", "message": str(e)})

    except WebSocketDisconnect:
        logger.info("Client disconnected from chat")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — Observability Metrics Stream
# ─────────────────────────────────────────────────────────────────────────────


@app.websocket("/ws/metrics")
async def metrics_ws(ws: WebSocket):
    """WebSocket endpoint for real-time observability metrics.

    Authenticated for the same reason /api/metrics is: it streams wallet
    balances, resource ids and agent activity. The REST endpoint checked the
    session and this one did not, so the check was trivially bypassed.
    """
    if not await _accept_authenticated(ws, "/ws/metrics"):
        return
    logger.info("Client connected to metrics WebSocket")

    try:
        # Send immediate ack so client knows connection is alive
        await ws.send_json({"type": "connected", "ts": time.time()})

        while True:
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(_get_agent_metrics), timeout=12
                )
                await ws.send_json(data)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "metrics", "error": "timeout fetching metrics", "timestamp": time.time()})
            except Exception as e:
                await ws.send_json({"type": "metrics", "error": str(e), "timestamp": time.time()})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        logger.info("Client disconnected from metrics")
    except Exception as e:
        logger.error(f"Metrics WS error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# REST endpoint — Observability data (fallback for non-WS clients)
# ─────────────────────────────────────────────────────────────────────────────


def _unauthorized() -> JSONResponse:
    """401, in place of a bare {"error": ...} dict with a 200 status.

    A 200 that means "denied" makes every caller parse the body to find out
    whether it worked, and the dashboard did not: it tested
    `data.type === 'metrics'`, found nothing, and went on rendering stale
    numbers indefinitely once the session expired.

    A new response each call rather than one shared instance: enforce_https()
    mutates response headers on the way out, and a module-level singleton would
    be handing the same mutable object to every concurrent request.
    """
    return JSONResponse({"error": "unauthorized"}, status_code=401)


@app.get("/api/session")
async def api_session(request: Request):
    """Is this session still valid? 200 if yes, 401 if not.

    Exists for the browser's benefit. Rejecting a WebSocket handshake before
    accept() means the browser is told HTTP 403 and reports close code 1006 —
    indistinguishable from the network dropping. The pages probe this endpoint
    on an unclean close to tell "log in again" apart from "retry": it touches no
    AWS API and does no work beyond an HMAC check, so it is safe to call on
    every reconnect.
    """
    if not verify_session(request):
        return _unauthorized()
    return {"authenticated": True}


@app.get("/api/metrics")
async def api_metrics(request: Request):
    """REST endpoint for observability data (polled by dashboard)."""
    if not verify_session(request):
        return _unauthorized()
    return await asyncio.to_thread(_get_agent_metrics)


@app.get("/api/last-payment")
async def api_last_payment(request: Request):
    """Return the last payment result (survives page navigation)."""
    if not verify_session(request):
        return _unauthorized()
    events = _LAST_PAYMENT.get("events") or []
    return {"type": "last_payment", "events": events, "count": len(events)}


@app.get("/api/traces")
async def api_traces(request: Request):
    """Return recent agent execution traces from the event bus."""
    if not verify_session(request):
        return _unauthorized()
    return await asyncio.to_thread(_get_recent_traces)


def _get_recent_traces() -> dict:
    """Get recent Runtime tool-call traces from CloudWatch Logs Insights."""
    if not RUNTIME_LOG_GROUP:
        return {
            "type": "traces",
            "traces": [],
            "count": 0,
            "note": "AGENTCORE_RUNTIME_ID / AGENTCORE_RUNTIME_LOG_GROUP not set — "
                    "no Runtime to query. The five-agent orchestrator runs in this "
                    "process; see /api/last-payment for its trace.",
        }

    try:
        from datetime import datetime, timezone

        logs = _client("logs")
        now = datetime.now(timezone.utc)

        try:
            response = logs.start_query(
                logGroupName=RUNTIME_LOG_GROUP,
                startTime=int(now.timestamp() - 3600),
                endTime=int(now.timestamp()),
                queryString='fields @timestamp, @message | filter @message like /tools\\/call/ | sort @timestamp desc | limit 20',
            )
        except logs.exceptions.ResourceNotFoundException:
            return {
                "type": "traces",
                "traces": [],
                "count": 0,
                "note": f"Log group {RUNTIME_LOG_GROUP} not found — the Runtime may "
                        "not have emitted logs yet",
            }

        query_id = response["queryId"]
        # Logs Insights is asynchronous. Poll instead of a blind sleep so a fast
        # query returns fast and a slow one still gets a fair chance.
        results = {}
        for _ in range(10):
            time.sleep(0.5)
            results = logs.get_query_results(queryId=query_id)
            if results.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
                break

        traces = [
            {
                "timestamp": entry.get("@timestamp", ""),
                "message": entry.get("@message", "")[:200],
            }
            for entry in ({f["field"]: f["value"] for f in row} for row in results.get("results", []))
        ]
        return {"type": "traces", "traces": traces, "count": len(traces)}
    except Exception as e:
        logger.warning(f"Trace query failed: {e}")
        return {"type": "traces", "error": str(e)}


def _get_agent_metrics() -> dict:
    """Fetch real agent runtime metrics from CloudWatch + AgentCore."""
    try:
        from datetime import datetime, timezone, timedelta

        cw = _client("cloudwatch")
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=1)

        # Metric queries for AgentCore runtime (correct namespace: AWS/Bedrock-AgentCore)
        metric_queries = [
            {"Id": "invocations", "MetricStat": {"Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": "Invocations", "Dimensions": [{"Name": "AggregateOperation", "Value": "InvokeAgentRuntime"}]}, "Period": 300, "Stat": "Sum"}},
            {"Id": "sessions", "MetricStat": {"Metric": {"Namespace": "AWS/Bedrock-AgentCore", "MetricName": "Sessions", "Dimensions": [{"Name": "AggregateOperation", "Value": "InvokeAgentRuntime"}]}, "Period": 300, "Stat": "Sum"}},
        ]

        cw_data = {}
        try:
            resp = cw.get_metric_data(
                MetricDataQueries=metric_queries,
                StartTime=start,
                EndTime=now,
            )
            for result in resp.get("MetricDataResults", []):
                values = result.get("Values", [])
                cw_data[result["Id"]] = sum(values) if values else 0
        except Exception as e:
            logger.warning(f"CloudWatch metric query failed: {e}")

        # Runtime status from the AgentCore control plane. NOT_DEPLOYED is the
        # honest answer when no runtime id is configured — the previous code
        # reported on a hardcoded id from someone else's account.
        runtime_status = "NOT_DEPLOYED"
        if RUNTIME_ID:
            runtime_status = "UNKNOWN"
            try:
                rt = _client("bedrock-agentcore-control").get_agent_runtime(
                    agentRuntimeId=RUNTIME_ID
                )
                runtime_status = rt.get("status", "UNKNOWN")
            except Exception as e:
                logger.warning(f"AgentCore status check failed: {e}")

        # Agent activity from the last completed run. Sourced from the recorded
        # result rather than a process-wide bus, so the figures describe one
        # payment instead of an accumulation of everyone's.
        agent_activity = {}
        recent_events = []
        agent_spans = {}
        try:
            all_events = _LAST_PAYMENT.get("events") or []
            recent_events = all_events[-30:]
            # Compute per-agent spans (start → complete timing)
            starts = {}
            for evt in all_events:
                agent = evt.get("agent", "")
                event_type = evt.get("event", "")
                ts = evt.get("timestamp", 0)
                if event_type == "agent_start":
                    starts[agent] = ts
                elif event_type == "agent_complete" and agent in starts:
                    duration_ms = round((ts - starts[agent]) * 1000)
                    agent_spans[agent] = {
                        "duration_ms": duration_ms,
                        "status": evt.get("data", {}).get("status", evt.get("data", {}).get("decision", "complete")),
                        "started_at": starts[agent],
                        "completed_at": ts,
                    }
                if agent not in agent_activity:
                    agent_activity[agent] = {"last_event": event_type, "last_ts": ts, "count": 0, "tool_calls": 0}
                agent_activity[agent]["count"] += 1
                agent_activity[agent]["last_event"] = event_type
                agent_activity[agent]["last_ts"] = ts
                if event_type == "tool_call":
                    agent_activity[agent]["tool_calls"] += 1
        except Exception:
            pass

        return {
            "type": "metrics",
            "timestamp": time.time(),
            "runtime": {
                "id": RUNTIME_ID or "not-deployed",
                "status": runtime_status,
                "invocations_1h": int(cw_data.get("invocations", 0)),
                "sessions_1h": int(cw_data.get("sessions", 0)),
            },
            "agents": agent_activity,
            "agent_spans": agent_spans,
            "recent_events": recent_events[-10:],
            # These two were previously hardcoded to "READY" regardless of
            # reality. Absent an id there is nothing to report on.
            "payment_manager": {
                "id": PAYMENT_MANAGER_ID or "not-deployed",
                "status": "CONFIGURED" if PAYMENT_MANAGER_ID else "NOT_DEPLOYED",
            },
            "gateway": {
                "id": GATEWAY_ID or "not-deployed",
                "status": "CONFIGURED" if GATEWAY_ID else "NOT_DEPLOYED",
            },
        }
    except Exception as e:
        logger.error(f"Metrics collection error: {e}")
        return {"type": "metrics", "error": str(e), "timestamp": time.time()}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

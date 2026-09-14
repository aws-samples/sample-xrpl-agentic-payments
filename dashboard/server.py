# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — Local WebSocket Dashboard

A minimal, unauthenticated local dashboard for watching the five-agent payment
pipeline run. Superseded by webapp/ (which adds auth, the observability tab and
the architecture view) — this is kept as a dependency-light local harness.

- GET  /         serves the self-contained React dashboard
- WS   /ws       streams agent events live; accepts {"action":"send_payment"}
- POST /payment  triggers a payment without a WebSocket

Do not expose this port publicly: there is no authentication and it can spend
from the execution wallet.

Run:  python dashboard/server.py
Open: http://localhost:8765
"""

import asyncio
import json
import threading
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

app = FastAPI(title="XRPL Agentic Payments Dashboard")

# Connected WebSocket clients
clients: set[WebSocket] = set()

# The server's event loop, captured at startup. Agent code runs in a worker
# thread, so every send has to be marshalled back onto this loop — the previous
# implementation called asyncio.run() per event, which spun up a fresh loop and
# tried to write to a socket owned by a different one.
_loop: asyncio.AbstractEventLoop | None = None

# Only one payment flow at a time: the agents share a process-global event bus,
# so two concurrent runs would interleave into an unreadable stream.
_payment_lock = threading.Lock()

# Dashboard HTML (self-contained React app)
DASHBOARD_HTML = Path(__file__).parent / "index.html"


@app.on_event("startup")
async def _capture_loop():
    global _loop
    _loop = asyncio.get_running_loop()


@app.get("/")
async def get_dashboard():
    """Serve the dashboard UI."""
    if DASHBOARD_HTML.exists():
        return HTMLResponse(DASHBOARD_HTML.read_text())
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """WebSocket endpoint for real-time agent events."""
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            data = await ws.receive_text()
            if not data.startswith("{"):
                continue
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"event": "error", "data": {"error": "malformed JSON"}}))
                continue
            if msg.get("action") == "send_payment":
                if _payment_lock.locked():
                    await ws.send_text(json.dumps(
                        {"event": "error", "data": {"error": "a payment is already running"}}
                    ))
                    continue
                threading.Thread(target=_run_payment_flow, args=(msg,), daemon=True).start()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


async def _send_to_all(payload: str):
    """Fan a pre-serialized payload out to every client, dropping dead sockets."""
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        clients.discard(ws)


def broadcast_threadsafe(event: dict):
    """Broadcast from any thread by scheduling the send on the server's loop."""
    if _loop is None or _loop.is_closed():
        return
    payload = json.dumps(event, default=str)
    asyncio.run_coroutine_threadsafe(_send_to_all(payload), _loop)


def _run_payment_flow(msg: dict):
    """Execute a multi-agent payment and stream its events to the dashboard."""
    from src.agents.orchestrator import PaymentContext, run_multi_agent_payment
    from src.payments.request import PaymentRequestError, parse_payment_request

    def on_event(event):
        broadcast_threadsafe(event.to_dict())

    with _payment_lock:
        try:
            # Validate before anything else: a malformed request should be a
            # rejection the operator can read, not a payment of some default
            # amount to some default address.
            request = parse_payment_request(msg)
        except PaymentRequestError as e:
            broadcast_threadsafe({"event": "error", "data": {"error": f"Invalid payment request: {e}"}})
            return

        # The context is created here so the listener is attached before the run
        # starts and no early event is missed. It belongs to this payment alone,
        # so there is no listener or event state to tear down afterwards.
        context = PaymentContext()
        context.bus.on_event(on_event)
        try:
            result = run_multi_agent_payment(request, context=context)
            broadcast_threadsafe({"event": "payment_complete", "data": result})
        except Exception as e:
            broadcast_threadsafe({"event": "error", "data": {"error": str(e)}})


@app.post("/payment")
async def trigger_payment(payload: dict):
    """REST endpoint to trigger a payment (alternative to the WebSocket).

    The agent pipeline is synchronous and takes seconds, so it runs in a worker
    thread rather than blocking the event loop and stalling every WS client.
    """
    from src.agents.orchestrator import run_multi_agent_payment
    from src.payments.request import PaymentRequestError, parse_payment_request

    if _payment_lock.locked():
        return {"status": "rejected", "error": "a payment is already running"}

    try:
        request = parse_payment_request(payload)
    except PaymentRequestError as e:
        return {"status": "rejected", "error": f"Invalid payment request: {e}"}

    def _run():
        with _payment_lock:
            return run_multi_agent_payment(request)

    return await asyncio.to_thread(_run)


if __name__ == "__main__":
    # Bound to localhost: no auth, and it can spend from the execution wallet.
    uvicorn.run(app, host="127.0.0.1", port=8765)

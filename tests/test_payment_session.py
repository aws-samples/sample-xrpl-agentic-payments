# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for per-payment session identity in src/agents/orchestrator.py.

Every tool call in one payment must carry one session id, and two payments must
not share one. `_call_tool` used to pass no session id at all, so
src/agentcore_client.py minted a throwaway one per call.

What that identifier is FOR changed when the tool path moved to the AgentCore
Gateway. On the old Runtime path it bought warm microVMs: Runtime treats an
unseen runtimeSessionId as a new session, a session is a microVM, so an id per
call meant ~6 cold starts per payment. Gateway routes to Lambda, which has no
session to provision — so the id now earns its keep on the ledger instead, as the
`session_id` in the XRPL attribution memo that submit_payment writes
(functions/xrpl_core.py). The sharing contract is unchanged; only the reason is.

Session identity is still the only observable — nothing about correctness depends
on it — which is why it is asserted directly.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("strands", reason="Strands Agents SDK not installed")
pytest.importorskip("boto3", reason="boto3 not installed")

from src.agents import orchestrator  # noqa: E402
from src.agents.orchestrator import PaymentContext, _call_tool, _context  # noqa: E402
from src.agentcore_client import new_payment_session_id  # noqa: E402


@pytest.fixture
def recorded_sessions(monkeypatch):
    """Replace the Gateway call with a recorder, and hand back the session IDs seen.

    Patched on the orchestrator module rather than on agentcore_client, because
    the orchestrator imported the name at module scope — patching the source
    module would leave the orchestrator's binding pointing at the real function
    and every test here would try to reach AWS.
    """
    seen: list[str] = []

    def fake_invoke(tool_name, arguments, session_id=None):
        seen.append(session_id)
        return {"result": {"ok": True}, "payment": {"status": "free"}}

    monkeypatch.setattr(orchestrator, "invoke_tool_with_payment", fake_invoke)
    return seen


def run_tools(names, ctx=None):
    """Call `_call_tool` once per name inside a single PaymentContext."""
    ctx = ctx if ctx is not None else PaymentContext()
    token = _context.set(ctx)
    try:
        for name in names:
            _call_tool(name, {})
    finally:
        _context.reset(token)
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
# One payment, one session
# ─────────────────────────────────────────────────────────────────────────────


def test_every_tool_call_in_one_payment_shares_one_session(recorded_sessions):
    """The regression itself: six calls used to carry six unrelated ids."""
    ctx = run_tools(
        ["screen_sanctions", "get_orderbook", "get_paths",
         "submit_payment", "check_transaction", "get_balance"]
    )

    assert len(recorded_sessions) == 6
    assert set(recorded_sessions) == {ctx.session_id}, (
        f"expected one session ID for the whole payment, saw "
        f"{len(set(recorded_sessions))}: {sorted(set(recorded_sessions))}"
    )


def test_the_session_id_is_never_none(recorded_sessions):
    """A None here is what made agentcore_client mint a throwaway ID."""
    run_tools(["screen_sanctions"])
    assert recorded_sessions == [recorded_sessions[0]]
    assert recorded_sessions[0] is not None


# ─────────────────────────────────────────────────────────────────────────────
# Two payments, two sessions — the attribution half
# ─────────────────────────────────────────────────────────────────────────────


def test_two_payments_do_not_share_a_session(recorded_sessions):
    """Two payments must be distinguishable on the ledger.

    Guards against "fixing" this the cheap way — a module-level session ID
    reused forever — which would stamp every unrelated user's payment with the
    same session_id in its attribution memo.
    """
    first = run_tools(["screen_sanctions"])
    second = run_tools(["screen_sanctions"])

    assert first.session_id != second.session_id
    assert recorded_sessions == [first.session_id, second.session_id]


def test_a_fresh_context_gets_an_id_without_being_asked():
    """The default_factory is the whole mechanism; an unset field is the bug."""
    assert PaymentContext().session_id


# ─────────────────────────────────────────────────────────────────────────────
# The ID itself must stay valid for the Runtime API too
# ─────────────────────────────────────────────────────────────────────────────


def test_session_ids_meet_the_33_character_minimum():
    """InvokeAgentRuntime rejects a runtimeSessionId shorter than 33 characters.

    The Gateway imposes no such floor — it is not a session id to the Gateway at
    all — but the Runtime-hosted MCP server is still deployed and directly
    invocable, so an id minted here has to remain one that path would accept.
    """
    session_id = new_payment_session_id()
    assert len(session_id) >= 33, f"{session_id!r} is {len(session_id)} chars"
    assert re.fullmatch(r"[A-Za-z0-9._-]+", session_id), (
        f"{session_id!r} contains characters outside the documented set"
    )


def test_generated_session_ids_are_distinct():
    assert len({new_payment_session_id() for _ in range(50)}) == 50

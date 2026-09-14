# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — Multi-Agent Orchestrator

Five specialized agents, each constructed with its own scoped tool list:

1. FX Intelligence Agent — market data only, no payment access
2. Compliance Agent — sanctions screening only, no payment access
3. Routing Agent — path selection only, no payment access
4. Execution Agent — payment submission only, cannot skip compliance
5. Settlement Monitor — transaction verification only

They run in a fixed sequence:
  Compliance → FX → Routing → Execution → Settlement

The sequencing is deterministic Python, not an LLM supervisor: run_multi_agent_payment()
calls each agent in turn and the gates between steps are `if` statements, so a model
cannot talk its way past compliance. The agents never call each other — every piece of
state passes through the orchestrator via the EventBus.

Tool scoping is enforced by the `tools=[...]` list each Agent is built with: an agent
has no way to invoke a tool it was not given, so a prompt injection cannot widen its
reach beyond its own step. That containment is application-level. Cedar / Amazon
Verified Permissions policies on the Gateway are a target, NOT implemented here.
"""

import json
import logging
import os
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

# Project imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agentcore_client import invoke_tool_with_payment, new_payment_session_id
from src.payments.request import PaymentRequest, PaymentRequestError, parse_payment_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("xrpl_agentic_payments.orchestrator")

# Model config
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
REGION = "us-west-2"

# Payments at or above this value are held for a human rather than executed.
# Decimal, not float, so the comparison against a Decimal amount is exact: as a
# float this boundary is representable, but 10000.0 == Decimal("10000") only
# holds by luck and the same code with a threshold of 0.1 would not.
HUMAN_APPROVAL_THRESHOLD_USD = Decimal("10000")

WALLETS_FILE = PROJECT_ROOT / "config" / "wallets.json"


# ═══════════════════════════════════════════════════════════════════════════════
# Wallet ADDRESSES — resolved on first use, and never from a file that has seeds
# ═══════════════════════════════════════════════════════════════════════════════

# This module needs exactly two values, both of them public XRPL addresses: the
# account the Execution Agent pays from, and the RLUSD issuer. It does not sign
# anything — signing happens in the MCP server on AgentCore Runtime and in the
# Lambda tools, each of which reads the seed itself at invoke time.
#
# It used to read them out of config/wallets.json, which is the file that also
# contains every SEED. That is why infra/lib/eks-stack.ts mounted the wallets
# secret into the web pod at all: two address strings pulled the treasury and
# execution private keys into a long-lived, multi-tenant process that had no use
# for them. Every user's payment runs in that same interpreter, so the seeds sat
# one bug or one prompt injection away from code handling other people's
# requests — for no benefit, because nothing in this process can spend them.
#
# Resolution order, first hit wins:
#   1. XRPL_EXECUTION_ADDRESS / XRPL_RLUSD_ISSUER. These are what the deployment
#      sets (infra/lib/eks-stack.ts, from CDK context) — public values, passed
#      the same way domainName is, so no secret has to be mounted to supply them.
#   2. config/wallets.json — local development only, where the file is on disk
#      anyway and there is one developer.
# Resolved lazily, because a fresh clone has no wallets.json and reading it at
# import time made `import src.agents.orchestrator` fail outright and took the
# whole web app down with it.

_addresses_cache: dict[str, str] | None = None

_MISSING_ADDRESSES = (
    "Could not resolve the XRPL execution and RLUSD issuer addresses. Set "
    "XRPL_EXECUTION_ADDRESS and XRPL_RLUSD_ISSUER, or run "
    "scripts/provision_wallets.py to create config/wallets.json for local use."
)


def _addresses_from_env() -> dict[str, str] | None:
    execution = os.environ.get("XRPL_EXECUTION_ADDRESS", "").strip()
    issuer = os.environ.get("XRPL_RLUSD_ISSUER", "").strip()
    if execution and issuer:
        return {"execution": execution, "rlusd_issuer": issuer}
    return None


def _addresses_from_wallets_file() -> dict[str, str] | None:
    if not WALLETS_FILE.exists():
        return None
    with open(WALLETS_FILE) as f:
        data = json.load(f)
    return {
        "execution": data["execution"]["address"],
        "rlusd_issuer": data["_metadata"]["rlusd_issuer"],
    }


def _addresses() -> dict[str, str]:
    """The two public addresses this module needs, resolved once and cached."""
    global _addresses_cache
    if _addresses_cache is None:
        for source in (_addresses_from_env, _addresses_from_wallets_file):
            resolved = source()
            if resolved is not None:
                _addresses_cache = resolved
                break
        else:
            raise RuntimeError(_MISSING_ADDRESSES)
    return _addresses_cache


def execution_address() -> str:
    """XRPL address the Execution Agent pays from."""
    return _addresses()["execution"]


def issuer_address() -> str:
    """Testnet RLUSD issuer address."""
    return _addresses()["rlusd_issuer"]


# ═══════════════════════════════════════════════════════════════════════════════
# Event Bus — real-time agent activity events (consumed by dashboard via WS)
# ═══════════════════════════════════════════════════════════════════════════════


class EventType(Enum):
    AGENT_START = "agent_start"
    AGENT_COMPLETE = "agent_complete"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PAYMENT_SUBMITTED = "payment_submitted"
    SETTLEMENT_CONFIRMED = "settlement_confirmed"
    COMPLIANCE_RESULT = "compliance_result"
    APPROVAL_REQUIRED = "approval_required"
    ERROR = "error"


@dataclass
class AgentEvent:
    event_type: EventType
    agent_name: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "event": self.event_type.value,
            "agent": self.agent_name,
            "data": self.data,
            "timestamp": self.timestamp,
        }


class EventBus:
    """Simple event bus for agent activity — feeds the WebSocket dashboard."""

    def __init__(self):
        self.events: list[AgentEvent] = []
        self.listeners: list[Callable] = []

    def emit(self, event: AgentEvent):
        self.events.append(event)
        for listener in self.listeners:
            listener(event)
        logger.info(f"[{event.agent_name}] {event.event_type.value}: {json.dumps(event.data, default=str)[:100]}")

    def on_event(self, listener: Callable):
        self.listeners.append(listener)

    def remove_listener(self, listener: Callable):
        """Detach a listener. Callers that attach per-request MUST call this in a
        finally block, or every completed request leaves a listener behind that
        writes to a closed socket on the next payment."""
        try:
            self.listeners.remove(listener)
        except ValueError:
            pass

    def get_all(self) -> list[dict]:
        return [e.to_dict() for e in self.events]


# ═══════════════════════════════════════════════════════════════════════════════
# Request-scoped execution context
# ═══════════════════════════════════════════════════════════════════════════════

# The event bus and the raw tool payloads USED TO BE process globals that every
# payment cleared and rewrote. The web app starts one thread per request against
# the same module, so two concurrent payments interleaved: one request could read
# the other's compliance verdict, routing decision, transaction hash or
# settlement event. A sanctioned request could observe another request's CLEAR.
# Clearing the listener list also tore down listeners belonging to live requests.
#
# Each payment now owns a PaymentContext. Tools reach it through a ContextVar
# rather than an argument, because Strands calls them with only the arguments the
# model supplied. A ContextVar is the right scope here: threading.Thread starts
# each worker with a fresh context, so one request cannot see another's, and
# nothing has to be reset between runs.

_MISSING_CONTEXT = (
    "No PaymentContext is active. The agent tools in this module may only be "
    "called from inside run_multi_agent_payment()."
)

_MISSING_REQUEST = (
    "No validated PaymentRequest on the active PaymentContext. Financial tools "
    "refuse to act on model-supplied amounts alone."
)


@dataclass
class PaymentContext:
    """Everything one payment run reads and writes. Never shared between runs."""

    bus: EventBus = field(default_factory=EventBus)

    # The server-validated request. This — not any argument a model passes to a
    # tool — is the authoritative amount, destination and currency for every gate.
    request: PaymentRequest | None = None

    # Events carry summaries (a path count, a status) because they are streamed to
    # the browser. The Routing Agent needs the FULL get_paths payload, so tools
    # stash their raw result here for the orchestrator to pick up. This is what
    # removes the second, redundant get_paths round-trip the FX step used to make
    # to recover data it had already paid for and thrown away.
    tool_results: dict[str, Any] = field(default_factory=dict)

    # One session id for the whole payment. Every _call_tool() passes it, so the
    # ~6 tool calls a payment makes carry one identifier — and submit_payment
    # writes it into the XRPL attribution memo, which is what makes a payment
    # traceable on the ledger as a single run. Per-payment rather than
    # per-process on purpose: a module-level id would stamp every user's payment
    # with the same session. See new_payment_session_id().
    session_id: str = field(default_factory=new_payment_session_id)


_context: ContextVar[PaymentContext | None] = ContextVar("payment_context", default=None)


def _ctx() -> PaymentContext:
    """The active payment's context, or raise — a tool call outside a run is a bug."""
    ctx = _context.get()
    if ctx is None:
        raise RuntimeError(_MISSING_CONTEXT)
    return ctx


def _emit(event: AgentEvent):
    """Emit onto the active payment's bus."""
    _ctx().bus.emit(event)


def _call_tool(tool_name: str, arguments: dict) -> dict:
    """Invoke an MCP tool through the AgentCore Gateway, recording the raw payload.

    Gateway, not Runtime: invoke_tool_with_payment signs a POST to the Gateway's
    MCP endpoint, which routes to the tool's Lambda (functions/<tool>.py).
    """
    ctx = _ctx()
    response = invoke_tool_with_payment(
        tool_name, arguments, session_id=ctx.session_id
    )
    result = response["result"]
    ctx.tool_results[tool_name] = result
    ctx.tool_results[f"{tool_name}__payment"] = response["payment"]
    return result


def _tool_result(tool_name: str) -> dict:
    """Raw payload from the last call to `tool_name` in this payment."""
    value = _ctx().tool_results.get(tool_name)
    return value if isinstance(value, dict) else {}


def _payment_status(tool_name: str) -> str:
    """x402 charge status recorded for the last call to `tool_name`."""
    return _tool_result(f"{tool_name}__payment").get("status", "unknown")


# ═══════════════════════════════════════════════════════════════════════════════
# Human approval gate
# ═══════════════════════════════════════════════════════════════════════════════

# This is the authorisation boundary, and it is deliberately plain Python. The
# Routing Agent reports a decision for the model's benefit, but a model cannot be
# the thing that decides whether a payment needs a human: prompt text is advice,
# not a control. Every path through here either returns a definite USD figure or
# fails closed.


def _normalize_amount(raw) -> Decimal | None:
    """Best-effort Decimal for comparing a model's restatement against the request."""
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError, TypeError):
        return None
    return value if value.is_finite() else None


_USD_CODES = ("USD", "RLUSD")


def _xrp_to_usd(amount: Decimal) -> Decimal | None:
    """Convert XRP to USD using the order book the FX Agent already fetched.

    Returns None when no usable rate is on the context, which the caller treats
    as "needs a human" — an unpriceable payment is exactly the case where an
    autonomous limit must not be assumed to hold.
    """
    book = _tool_result("get_orderbook")
    mid = _normalize_amount(book.get("mid"))
    if mid is None or mid <= 0:
        return None

    # get_orderbook returns `mid` as QUOTE units per 1 BASE unit for the pair the
    # model chose, so the rate has to be oriented before it is applied. Inverting
    # this by accident would turn a $50k payment into a $9 one and wave it past
    # the gate, so an unrecognised pair is refused rather than guessed at.
    base, _, quote = str(book.get("pair", "")).upper().partition("/")
    if base == "XRP" and quote in _USD_CODES:
        return amount * mid  # USD per XRP
    if base in _USD_CODES and quote == "XRP":
        return amount / mid  # XRP per USD
    return None


def requires_human_approval(request: PaymentRequest) -> tuple[bool, str]:
    """Decide whether `request` must be held for a human.

    Returns (needs_approval, reason). Fails closed: anything that cannot be
    priced in USD requires approval rather than proceeding unpriced.
    """
    threshold = f"${HUMAN_APPROVAL_THRESHOLD_USD:,.0f}"

    if request.currency in ("USD", "RLUSD"):
        amount_usd = request.amount
    elif request.currency == "XRP":
        amount_usd = _xrp_to_usd(request.amount)
        if amount_usd is None:
            return True, (
                f"{request.amount_str} XRP could not be priced in USD (no order "
                f"book rate available), so the {threshold} autonomous limit "
                "cannot be checked"
            )
    else:
        # parse_payment_request rejects unsupported currencies, so reaching this
        # means the supported set grew without the gate being updated.
        return True, f"no USD conversion is defined for {request.currency}"

    # "At or above" — a payment of exactly the threshold is held, matching the
    # documented limit. The old `>` let $10,000.00 through autonomously.
    if amount_usd >= HUMAN_APPROVAL_THRESHOLD_USD:
        return True, (
            f"{request.amount_str} {request.currency} (~${amount_usd:,.2f}) is at "
            f"or above the {threshold} autonomous limit"
        )
    return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# Settlement confirmation
# ═══════════════════════════════════════════════════════════════════════════════

DROPS_PER_XRP = Decimal("1000000")


def _xrpl_amount_to_decimal(raw, currency: str) -> Decimal | None:
    """Normalize an XRPL amount field to a Decimal in `currency`'s own units.

    XRPL represents XRP as an integer-drops string and issued currencies as an
    object. Returns None when the shape or the currency does not match.
    """
    if currency == "XRP":
        if not isinstance(raw, str):
            return None
        drops = _normalize_amount(raw)
        return None if drops is None else drops / DROPS_PER_XRP
    if isinstance(raw, dict):
        # RLUSD is issued under the 3-character code "USD" on testnet, so a
        # request for "RLUSD" is settled by a "USD" issued amount.
        if str(raw.get("currency", "")).upper() not in _USD_CODES:
            return None
        return _normalize_amount(raw.get("value"))
    return None


def _confirm_settlement(
    check: dict, expected_hash: str, request: PaymentRequest
) -> tuple[bool, str]:
    """Decide whether `check` confirms THIS payment settled.

    Returns (settled, note). Fails closed: anything unverifiable is not settled.
    """
    if not check:
        return False, "settlement was never verified against the ledger"

    if check.get("error"):
        return False, f"ledger lookup failed: {check['error']}"

    # The hash has to be the one we submitted. Otherwise a model that hallucinated
    # a hash, or reused one from an earlier run, could produce a confirmation for
    # a payment this run never made.
    if str(check.get("hash", "")).upper() != expected_hash.upper():
        return False, "the verified transaction is not the one this run submitted"

    if check.get("validated") is not True:
        return False, "not yet validated on the ledger"

    tx_result = check.get("transaction_result")
    if tx_result != "tesSUCCESS":
        return False, f"validated but failed on ledger: {tx_result}"

    if check.get("destination") and check["destination"] != request.destination:
        return False, "the settled transaction paid a different destination"

    # delivered_amount is what arrived; amount/DeliverMax is only the ceiling the
    # sender authorised, so a partial payment would otherwise look complete.
    delivered_raw = check.get("delivered_amount")
    if delivered_raw is None:
        delivered_raw = check.get("amount")
        source = "amount"
    else:
        source = "delivered_amount"
    delivered = _xrpl_amount_to_decimal(delivered_raw, request.currency)
    if delivered is None:
        return False, f"could not read the settled {source} as {request.currency}"
    if delivered != request.amount:
        return False, (
            f"settled {source} {delivered} does not match the requested "
            f"{request.amount_str} {request.currency}"
        )

    return True, ""


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 1: FX Intelligence
# ═══════════════════════════════════════════════════════════════════════════════


@tool
def fx_get_orderbook(base_currency: str = "USD", quote_currency: str = "XRP") -> dict[str, Any]:
    """Query XRPL DEX order book for a trading pair to find current market rates.

    Args:
        base_currency: Base currency (default USD for RLUSD)
        quote_currency: Quote currency (default XRP)
    """
    _emit(AgentEvent(EventType.TOOL_CALL, "fx_intelligence", {"tool": "get_orderbook", "pair": f"{base_currency}/{quote_currency}"}))
    result = _call_tool("get_orderbook", {"base_currency": base_currency, "quote_currency": quote_currency})
    _emit(AgentEvent(EventType.TOOL_RESULT, "fx_intelligence", {"tool": "get_orderbook", "offers": len(result.get("offers", [])), "x402": _payment_status("get_orderbook")}))
    return result


@tool
def fx_find_paths(source: str, destination: str, amount: str, currency: str = "USD") -> dict[str, Any]:
    """Find optimal payment paths on XRPL with cost estimates.

    Args:
        source: Source XRPL address
        destination: Destination XRPL address
        amount: Amount to deliver at destination
        currency: Destination currency (default USD)
    """
    _emit(AgentEvent(EventType.TOOL_CALL, "fx_intelligence", {"tool": "find_paths", "amount": amount, "currency": currency}))
    result = _call_tool("get_paths", {"source": source, "destination": destination, "dest_amount": amount, "dest_currency": currency})
    _emit(AgentEvent(EventType.TOOL_RESULT, "fx_intelligence", {"tool": "find_paths", "paths_found": result.get("paths_found", 0), "x402": _payment_status("get_paths")}))
    return result


FX_SYSTEM_PROMPT = """You are the FX Intelligence Agent in the XRPL Agentic Payments system.
Your role: Analyze XRPL DEX liquidity and find optimal payment paths.
Source wallet: {execution_address}
RLUSD issuer: {issuer_address}

When asked to analyze a payment route:
1. Query the order book for relevant trading pairs
2. Find available payment paths between source and destination
3. Return a clear recommendation of the best path with cost estimate

You can ONLY read market data. You CANNOT execute payments or skip compliance."""


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 2: Compliance
# ═══════════════════════════════════════════════════════════════════════════════


@tool
def compliance_screen(entity_name: str, entity_country: str = "") -> dict[str, Any]:
    """Screen an entity against OFAC sanctions lists. Returns CLEAR or BLOCKED.

    Args:
        entity_name: Name of person or organization to screen
        entity_country: Country of the entity (optional but recommended)
    """
    _emit(AgentEvent(EventType.TOOL_CALL, "compliance", {"tool": "screen_sanctions", "entity": entity_name, "country": entity_country}))
    result = _call_tool("screen_sanctions", {"entity_name": entity_name, "entity_country": entity_country})
    _emit(AgentEvent(EventType.COMPLIANCE_RESULT, "compliance", {"status": result["status"], "entity": entity_name, "x402": _payment_status("screen_sanctions")}))
    return result


COMPLIANCE_SYSTEM_PROMPT = """You are the Compliance Agent in the XRPL Agentic Payments system.
Your role: Screen counterparties against sanctions lists before any payment.

When asked to check a recipient:
1. Call compliance_screen with the entity name and country
2. Return the result clearly: CLEAR (safe to proceed) or BLOCKED (must refuse)
3. If BLOCKED, include the reason and matched sanctioned entity

You can ONLY screen entities. You CANNOT execute payments or modify routes.
You MUST screen every entity asked. Never skip or shortcut compliance."""


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 3: Routing
# ═══════════════════════════════════════════════════════════════════════════════


@tool
def routing_analyze(
    fx_data: str,
    compliance_status: str,
    amount: str,
    destination: str,
) -> dict[str, Any]:
    """Analyze routing options and select the optimal payment path.

    Args:
        fx_data: JSON string of FX intelligence data (paths and rates)
        compliance_status: CLEAR or BLOCKED from compliance check
        amount: Payment amount
        destination: Destination XRPL address
    """
    _emit(AgentEvent(EventType.TOOL_CALL, "routing", {"tool": "analyze", "amount": amount}))

    if compliance_status != "CLEAR":
        result = {"decision": "BLOCKED", "reason": "Compliance check did not pass"}
        _emit(AgentEvent(EventType.TOOL_RESULT, "routing", result))
        return result

    # Every financial field here arrives as a model-generated string, so none of
    # them decide anything: the authoritative amount, currency and destination
    # come from the validated PaymentRequest on the context. A model that
    # under-reports the amount cannot talk its way past the threshold, and one
    # that names a different destination is contradicted here instead of being
    # believed.
    request = _ctx().request
    if request is None:
        result = {"decision": "BLOCKED", "reason": "No validated payment request on this run"}
        _emit(AgentEvent(EventType.TOOL_RESULT, "routing", result))
        return result

    needs_approval, approval_reason = requires_human_approval(request)

    mismatches = []
    if _normalize_amount(amount) != request.amount:
        mismatches.append(f"amount {amount!r} != {request.amount_str}")
    if destination.strip() != request.destination:
        mismatches.append(f"destination {destination!r} != {request.destination}")
    if mismatches:
        # Not fatal on its own — the gate already used the validated values — but
        # a divergence is the signal that the model lost track of the payment,
        # so it is recorded rather than swallowed.
        _emit(AgentEvent(
            EventType.ERROR, "routing",
            {"error": "model restated the payment incorrectly", "mismatches": mismatches},
        ))

    # Parse FX data
    try:
        fx = json.loads(fx_data) if isinstance(fx_data, str) else fx_data
    except (json.JSONDecodeError, TypeError):
        fx = {}

    # Direct USD→USD payments always use a direct path (no cross-currency routing needed)
    recommended = fx.get("recommended_path") or {}

    decision = {
        "decision": "APPROVAL_REQUIRED" if needs_approval else "APPROVED",
        "path_type": "direct",
        "hops": recommended.get("hops", 0),
        "estimated_cost": recommended.get(
            "source_cost", f"{request.amount_str} {request.currency}"
        ),
        "estimated_time_seconds": recommended.get("estimated_time_seconds", 4),
        "destination": request.destination,
        "amount": request.amount_str,
        "currency": request.currency,
        "requires_human_approval": needs_approval,
    }
    if needs_approval:
        decision["reason"] = approval_reason
    if mismatches:
        decision["restatement_mismatches"] = mismatches

    _emit(AgentEvent(EventType.TOOL_RESULT, "routing", decision))
    return decision


ROUTING_SYSTEM_PROMPT = f"""You are the Routing Agent in the XRPL Agentic Payments system.
Your role: Select the optimal payment route based on FX intelligence and compliance results.

When asked to route a payment:
1. Call routing_analyze with the FX data, compliance status, amount, and destination
2. If compliance is not CLEAR, the route MUST be BLOCKED
3. If the amount is at or above ${HUMAN_APPROVAL_THRESHOLD_USD:,.0f}, the decision is APPROVAL_REQUIRED and
   the payment stops for a human — it is NOT approved
4. Return the routing decision clearly

routing_analyze recomputes the amount, currency and destination from the
server-validated request, so its returned decision is authoritative even if it
disagrees with the values you passed in. Report what it returns; do not restate
or override it.

You can ONLY analyze routes. You CANNOT execute payments or override compliance."""


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 4: Execution
# ═══════════════════════════════════════════════════════════════════════════════


@tool
def execute_payment(destination: str, amount: str, currency: str = "USD") -> dict[str, Any]:
    """Execute a payment on XRPL. Only call this AFTER compliance is CLEAR and routing is APPROVED.

    Args:
        destination: XRPL destination address
        amount: Amount to send
        currency: Currency (default USD for RLUSD)
    """
    # The payment that gets signed is the validated one, not the one the model
    # restated. A model that has drifted — or been steered by a poisoned tool
    # response — cannot redirect funds or change the amount at the last step.
    request = _ctx().request
    if request is None:
        raise RuntimeError(_MISSING_REQUEST)

    _emit(AgentEvent(EventType.TOOL_CALL, "execution", {
        "tool": "submit_payment",
        "destination": request.destination,
        "amount": request.amount_str,
    }))
    result = _call_tool("submit_payment", {
        "destination": request.destination,
        "amount": request.amount_str,
        "currency": request.currency,
        "source_wallet": "execution",
    })

    # A transaction can be accepted for submission, and even land in a ledger,
    # and still have failed. Only tesSUCCESS moved funds; every tec* code is a
    # validated failure that charged a fee and delivered nothing. Announcing
    # PAYMENT_SUBMITTED on status alone reported those as successful payments.
    tx_result = result.get("transaction_result")
    tx_hash = result.get("hash", "")
    if result.get("status") == "success" and tx_result == "tesSUCCESS" and tx_hash:
        _emit(AgentEvent(EventType.PAYMENT_SUBMITTED, "execution", {
            "hash": tx_hash,
            "amount": request.amount_str,
            "currency": request.currency,
            "destination": request.destination,
        }))
    else:
        _emit(AgentEvent(EventType.ERROR, "execution", {
            "error": tx_result or result.get("error") or "unknown",
            "hash": tx_hash,
        }))
    return result


EXECUTION_SYSTEM_PROMPT = """You are the Execution Agent in the XRPL Agentic Payments system.
Your role: Submit signed XRPL transactions ONLY after compliance and routing approve.

When asked to execute a payment:
1. Verify that you have been told compliance is CLEAR and routing is APPROVED
2. Call execute_payment with the destination, amount, and currency
3. Return the transaction hash and status

CRITICAL RULES:
- You CANNOT execute if compliance status is not CLEAR
- You CANNOT execute if routing decision is not APPROVED
- You can ONLY call execute_payment — no other tools available to you
- Report success or failure honestly"""


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 5: Settlement Monitor
# ═══════════════════════════════════════════════════════════════════════════════


@tool
def monitor_transaction(tx_hash: str) -> dict[str, Any]:
    """Verify that a transaction has been confirmed on the XRPL ledger.

    Args:
        tx_hash: The 64-character hex transaction hash to verify
    """
    _emit(AgentEvent(EventType.TOOL_CALL, "settlement_monitor", {"tool": "verify_transaction", "hash": tx_hash[:16] + "..."}))
    result = _call_tool("check_transaction", {"tx_hash": tx_hash})

    # Settlement means two separate things and both have to hold: the ledger
    # containing the transaction is validated (irreversible), AND the
    # transaction itself succeeded. A tec* code in a validated ledger is a
    # permanent, irreversible failure — the old check called that "settled".
    if result.get("validated") is True and result.get("transaction_result") == "tesSUCCESS":
        _emit(AgentEvent(EventType.SETTLEMENT_CONFIRMED, "settlement_monitor", {
            "hash": tx_hash,
            "ledger_index": result.get("ledger_index"),
            "delivered_amount": result.get("delivered_amount"),
        }))
    elif result.get("validated") is True:
        _emit(AgentEvent(EventType.ERROR, "settlement_monitor", {
            "error": f"validated but failed: {result.get('transaction_result', 'unknown')}",
            "hash": tx_hash,
        }))
    return result


@tool
def monitor_balance(account: str) -> dict[str, Any]:
    """Check the current balance of an XRPL account to verify receipt.

    Args:
        account: XRPL account address to check
    """
    _emit(AgentEvent(EventType.TOOL_CALL, "settlement_monitor", {"tool": "check_balance", "account": account[:12] + "..."}))
    return _call_tool("get_balance", {"account": account})


SETTLEMENT_SYSTEM_PROMPT = """You are the Settlement Monitor Agent in the XRPL Agentic Payments system.
Your role: Confirm that XRPL transactions reach finality and generate settlement receipts.

When asked to verify a transaction:
1. Call monitor_transaction with the transaction hash
2. Read the status: "succeeded" means the transaction is in a validated ledger
   AND returned tesSUCCESS — that, and only that, is irreversible settlement.
   "failed" means it is validated but did NOT move funds; report it as a
   failure, never as settled. "pending" means not yet validated.
3. Generate a settlement receipt with: hash, ledger index, timestamp, status

You can ONLY read transaction and balance data. You CANNOT execute or modify anything."""


# ═══════════════════════════════════════════════════════════════════════════════
# Supervisor Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

# One BedrockModel is shared by all five agents. It is a stateless config wrapper
# around a boto3 client — conversation state lives on each Agent — so building it
# once saves five client setups per payment.

_model: BedrockModel | None = None


def _get_model() -> BedrockModel:
    """Get or create the shared Bedrock model config."""
    global _model
    if _model is None:
        _model = BedrockModel(model_id=MODEL_ID, region_name=REGION, streaming=True)
    return _model


def create_agent(system_prompt: str, tools: list) -> Agent:
    """Factory: create a specialized agent with scoped tools."""
    return Agent(model=_get_model(), system_prompt=system_prompt, tools=tools)


def run_multi_agent_payment(
    request: PaymentRequest,
    context: PaymentContext | None = None,
) -> dict[str, Any]:
    """
    Execute the full multi-agent payment flow.

    Sequence: Compliance → FX → Routing → Execution → Settlement
    Each agent only has access to its own tools.

    Args:
        request: A PaymentRequest already validated by parse_payment_request.
            The caller does the validating so an invalid request is rejected at
            the edge, with a message the client can act on, before any agent or
            any paid tool call happens.
        context: The PaymentContext for this run. Pass one in to subscribe to
            events before the run starts; otherwise a fresh one is created.

    All state for the run lives on that context, which is bound to a ContextVar
    for the duration of the call. Two concurrent payments therefore cannot see
    each other's events or tool results — with module-level globals, a second
    request arriving mid-flight reset the first one's bus and overwrote the
    tool results the first one's gates were about to read.
    """
    ctx = context if context is not None else PaymentContext()
    ctx.request = request
    token = _context.set(ctx)
    try:
        return _run_payment(request, ctx)
    finally:
        # Reset rather than clear: restores whatever context was active before,
        # so a nested or wrapping run is not left without one.
        _context.reset(token)


def _run_payment(request: PaymentRequest, ctx: PaymentContext) -> dict[str, Any]:
    """The payment flow itself. Runs with `ctx` already bound to the ContextVar."""
    start_time = time.time()

    destination = request.destination
    amount = request.amount_str
    currency = request.currency
    recipient_name = request.recipient_name
    recipient_country = request.recipient_country

    source_address = execution_address()

    result = {
        "status": "pending",
        "steps": {},
        "events": [],
        "request": request.to_dict(),
    }

    def finish(status: str, reason: str | None = None) -> dict[str, Any]:
        """Stamp the terminal fields every early return needs."""
        result["status"] = status
        if reason is not None:
            result["reason"] = reason
        result["events"] = ctx.bus.get_all()
        result["elapsed_seconds"] = round(time.time() - start_time, 2)
        return result

    # ─── Step 1: Compliance Agent ───────────────────────────────────────
    _emit(AgentEvent(EventType.AGENT_START, "compliance", {"recipient": recipient_name}))

    compliance_agent = create_agent(COMPLIANCE_SYSTEM_PROMPT, [compliance_screen])
    compliance_response = compliance_agent(
        f"Screen this recipient: {recipient_name}, country: {recipient_country}"
    )
    compliance_text = str(compliance_response)

    # Trust the tool's own result, not the model's summary of it.
    compliance_status = _tool_result("screen_sanctions").get("status", "UNKNOWN")

    _emit(AgentEvent(EventType.AGENT_COMPLETE, "compliance", {"status": compliance_status}))
    result["steps"]["compliance"] = {"status": compliance_status, "response": compliance_text[:200]}

    if compliance_status != "CLEAR":
        return finish("blocked", f"Compliance blocked: {compliance_text[:200]}")

    # ─── Step 2: FX Intelligence Agent ──────────────────────────────────
    _emit(AgentEvent(EventType.AGENT_START, "fx_intelligence", {"amount": amount, "currency": currency}))

    fx_agent = create_agent(
        FX_SYSTEM_PROMPT.format(
            execution_address=source_address,
            issuer_address=issuer_address(),
        ),
        [fx_get_orderbook, fx_find_paths],
    )
    fx_response = fx_agent(
        f"Find the best payment path to send {amount} {currency} from {source_address} to {destination}"
    )
    fx_text = str(fx_response)

    # The raw payload the FX agent already fetched — no second call for it.
    fx_data = _tool_result("get_paths")

    _emit(AgentEvent(EventType.AGENT_COMPLETE, "fx_intelligence", {"paths_found": fx_data.get("paths_found", 0)}))
    result["steps"]["fx_intelligence"] = {"paths_found": fx_data.get("paths_found", 0), "response": fx_text[:200]}

    # ─── Step 3: Routing Agent ──────────────────────────────────────────
    _emit(AgentEvent(EventType.AGENT_START, "routing", {"amount": amount}))

    routing_agent = create_agent(ROUTING_SYSTEM_PROMPT, [routing_analyze])
    routing_response = routing_agent(
        f"Route this payment: amount={amount}, destination={destination}, "
        f"compliance_status={compliance_status}, fx_data={json.dumps(fx_data)}"
    )
    routing_text = str(routing_response)

    # Extract routing decision
    routing_events = [e for e in ctx.bus.events if e.event_type == EventType.TOOL_RESULT and e.agent_name == "routing"]
    routing_data = routing_events[-1].data if routing_events else {}
    routing_decision = routing_data.get("decision", "UNKNOWN")

    _emit(AgentEvent(EventType.AGENT_COMPLETE, "routing", {"decision": routing_decision}))
    result["steps"]["routing"] = {"decision": routing_decision, "response": routing_text[:200]}

    # The threshold gate is re-evaluated HERE, in Python, from the validated
    # request. routing_analyze reports the same answer for the model's benefit,
    # but the decision the payment actually obeys is this one: an agent that
    # never called the tool, or whose call failed, cannot produce an implicit
    # approval by leaving `requires_human_approval` absent.
    needs_approval, approval_reason = requires_human_approval(request)
    if needs_approval or routing_data.get("requires_human_approval"):
        _emit(AgentEvent(
            EventType.APPROVAL_REQUIRED, "routing",
            {
                "amount": amount,
                "currency": currency,
                "threshold_usd": float(HUMAN_APPROVAL_THRESHOLD_USD),
                "reason": approval_reason or routing_data.get("reason", ""),
            },
        ))
        result["requires_human_approval"] = True
        # The Execution Agent is never constructed, so no agent in this process
        # is holding submit_payment when the run returns.
        return finish(
            "awaiting_approval",
            approval_reason
            or f"{amount} {currency} is at or above the "
               f"${HUMAN_APPROVAL_THRESHOLD_USD:,.0f} autonomous limit — held for human approval",
        )

    if routing_decision != "APPROVED":
        return finish("blocked", f"Routing blocked: {routing_text[:200]}")

    # ─── Step 4: Execution Agent ────────────────────────────────────────
    _emit(AgentEvent(EventType.AGENT_START, "execution", {"amount": amount, "destination": destination}))

    execution_agent = create_agent(EXECUTION_SYSTEM_PROMPT, [execute_payment])
    execution_response = execution_agent(
        f"Execute this payment: {amount} {currency} to {destination}. "
        f"Compliance status: CLEAR. Routing decision: APPROVED."
    )
    execution_text = str(execution_response)

    submit_result = _tool_result("submit_payment")
    tx_hash = submit_result.get("hash", "")
    submit_tx_result = submit_result.get("transaction_result", "")

    _emit(AgentEvent(EventType.AGENT_COMPLETE, "execution", {
        "hash": tx_hash,
        "transaction_result": submit_tx_result,
    }))
    result["steps"]["execution"] = {
        "hash": tx_hash,
        "transaction_result": submit_tx_result,
        "response": execution_text[:200],
    }

    if not tx_hash:
        return finish("failed", "No transaction hash returned")

    # A hash proves a transaction was submitted, not that it worked. Carrying on
    # to settlement monitoring with a tec* result meant the run reported a real
    # hash for a payment that never moved funds.
    if submit_result.get("status") != "success" or submit_tx_result != "tesSUCCESS":
        result["tx_hash"] = tx_hash
        return finish(
            "failed",
            f"Submission did not succeed: {submit_tx_result or submit_result.get('error', 'unknown')}",
        )

    # ─── Step 5: Settlement Monitor ─────────────────────────────────────
    _emit(AgentEvent(EventType.AGENT_START, "settlement_monitor", {"hash": tx_hash}))

    monitor_agent = create_agent(SETTLEMENT_SYSTEM_PROMPT, [monitor_transaction, monitor_balance])
    monitor_response = monitor_agent(
        f"Verify this transaction settled: {tx_hash}"
    )
    monitor_text = str(monitor_response)

    # Settlement is confirmed from the tool's own payload, not from the presence
    # of an event the model's narration might have caused, and not from the
    # model's prose. The transaction is also checked to be the one we submitted,
    # to the destination and for the amount that was validated — a hash the model
    # invented, or one for someone else's payment, does not confirm this payment.
    check = _tool_result("check_transaction")
    settled, settlement_note = _confirm_settlement(check, tx_hash, request)
    ledger_index = check.get("ledger_index", 0) if settled else 0

    _emit(AgentEvent(EventType.AGENT_COMPLETE, "settlement_monitor", {"settled": settled, "ledger_index": ledger_index}))
    result["steps"]["settlement"] = {
        "settled": settled,
        "ledger_index": ledger_index,
        "transaction_result": check.get("transaction_result", ""),
        "delivered_amount": check.get("delivered_amount"),
        "response": monitor_text[:200],
    }
    if settlement_note:
        result["steps"]["settlement"]["note"] = settlement_note

    # ─── Final Result ───────────────────────────────────────────────────
    result["tx_hash"] = tx_hash
    result["ledger_index"] = ledger_index
    result["agents_invoked"] = 5

    if settled:
        return finish("settled")
    return finish("pending", settlement_note or None)


# ─────────────────────────────────────────────────────────────────────────────
# CLI entrypoint
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("═" * 70)
    print("  XRPL Agentic Payments — Multi-Agent Autonomous Payment")
    print("  5 agents: Compliance → FX → Routing → Execution → Settlement")
    print("═" * 70)

    # Defaults for the local end-to-end run against testnet. Unlike the web
    # layer, where a default amount or destination is an unintended payment,
    # a developer invoking this file directly is asking for the canned case —
    # and it still goes through the same validation as a real request.
    payload = {
        "destination": "r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg",
        "amount": "100",
        "currency": "USD",
        "recipient_name": "Acme Corp",
        "recipient_country": "United States",
    }

    # Simple positional arg parsing: amount destination recipient country
    for key, position in (
        ("amount", 1),
        ("destination", 2),
        ("recipient_name", 3),
        ("recipient_country", 4),
    ):
        if len(sys.argv) > position:
            payload[key] = sys.argv[position]

    try:
        payment_request = parse_payment_request(payload)
    except PaymentRequestError as exc:
        print(f"\n  Invalid payment request: {exc}\n")
        sys.exit(2)

    print(f"\n  Payment: {payment_request.amount_str} {payment_request.currency} "
          f"→ {payment_request.destination}")
    print(f"  Recipient: {payment_request.recipient_name} ({payment_request.recipient_country})")
    print(f"\n{'─' * 70}")

    result = run_multi_agent_payment(payment_request)

    print(f"{'─' * 70}")
    print(f"\n  Result: {result['status'].upper()}")
    if result.get("tx_hash"):
        print(f"  TX Hash: {result['tx_hash']}")
        print(f"  Ledger: {result.get('ledger_index', 'N/A')}")
    print(f"  Elapsed: {result['elapsed_seconds']}s")
    print(f"  Agents: {result.get('agents_invoked', 0)}")
    print(f"  Events: {len(result.get('events', []))}")

    if result["status"] in ("blocked", "failed", "awaiting_approval"):
        print(f"  Reason: {result.get('reason', 'Unknown')[:100]}")

    print(f"\n{'═' * 70}")

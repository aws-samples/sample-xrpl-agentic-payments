# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
AgentCore Client — Invokes tools via Bedrock AgentCore Gateway and manages x402 micropayments.

This module provides:
1. invoke_tool() — calls MCP tools through the AgentCore Gateway via JSON-RPC 2.0
2. PaymentGate — creates payment sessions and processes x402 charges for premium tools

The Gateway is the tool path. It fronts the eight tool Lambdas
(``functions/*.py``), one Gateway target each, declared in
infra/lib/tools-stack.ts. There is no boto3 ``invoke_gateway`` operation — a
gateway is an MCP endpoint — so invoke_tool() signs an HTTPS POST itself with
SigV4 and the caller's own IAM identity (EKS Pod Identity in the deployment),
authorized by ``bedrock-agentcore:InvokeGateway``.

Until this existed the orchestrator called invoke_agent_runtime() against the
Runtime-hosted MCP server instead, and the Gateway was referenced only by a
health check and a metrics tile — so the eight Lambdas were deployed and
unreachable while every architecture diagram drew the hop through them. The
Runtime and its MCP server remain deployed (deploy/main.py) and are still a
second, independent implementation of the same tools; they are simply no longer
what a payment calls.

Configuration is read from environment variables (see .env.example). These are
deployment-specific outputs — each deployment gets its own Gateway and Payment
Manager, so nothing here is a real default:
    AGENTCORE_GATEWAY_URL: Gateway MCP endpoint. Required for any tool call.
    AGENTCORE_GATEWAY_ID: Gateway identifier, for the health check and metrics
    AGENTCORE_MCP_PROTOCOL_VERSION: must be a version the Gateway accepts
    PAYMENT_MANAGER_ARN: Payment Manager with Coinbase connector
    AWS_DEFAULT_REGION: AWS region (defaults to us-west-2)
"""

import json
import logging
import os
import time
from typing import Any, Optional
from uuid import uuid4

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — populated from your own deployment, never hardcoded here.
# Set these in .env after running scripts/setup_agentcore_payments.py.
# ─────────────────────────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
GATEWAY_URL = os.environ.get("AGENTCORE_GATEWAY_URL", "")
GATEWAY_ID = os.environ.get("AGENTCORE_GATEWAY_ID", "")
PAYMENT_MANAGER_ARN = os.environ.get("PAYMENT_MANAGER_ARN", "")
PAYMENT_CONNECTOR_ID = os.environ.get("PAYMENT_CONNECTOR_ID", "")

# The Runtime-hosted MCP server. Still deployed, no longer the tool path — kept
# because deploy/main.py publishes it and it is directly invocable for local and
# one-off use. invoke_tool() does NOT read this.
RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")

# Must be one of the gateway's protocolConfiguration.mcp.supportedVersions. The
# EKS stack sets this from MCP_PROTOCOL_VERSION in infra/lib/tools-stack.ts, the
# same constant that pins the gateway, so the two cannot disagree. The default
# here matches that constant for anyone running outside the deployment.
MCP_PROTOCOL_VERSION = os.environ.get(
    "AGENTCORE_MCP_PROTOCOL_VERSION", "2025-11-25"
)

# SigV4 signing name for AgentCore. Not derived from the endpoint host, because
# the gateway hostname (…gateway.bedrock-agentcore.<region>.amazonaws.com) would
# yield the wrong service name if parsed naively.
SIGNING_SERVICE = "bedrock-agentcore"

# ─────────────────────────────────────────────────────────────────────────────
# Gateway tool naming
# ─────────────────────────────────────────────────────────────────────────────

# Gateway exposes every tool as "<targetName>___<toolName>" so tools from
# different targets cannot collide, so a bare "get_balance" is not a name the
# Gateway knows. This table maps each tool to its target and MUST match the
# targetName values in infra/lib/tools-stack.ts — a target renamed on one side
# only produces "tool not found" at the Gateway, which is why
# tests/test_gateway_invocation.py asserts the two agree.
#
# Target names are kebab-case because the CreateGatewayTarget API restricts them
# to alphanumerics and hyphens; the tool names keep the snake_case of the Python
# handlers.
GATEWAY_TOOL_SEPARATOR = "___"

TOOL_TARGETS = {
    "get_balance": "get-balance",
    "check_transaction": "check-transaction",
    "get_trust_lines": "get-trust-lines",
    "get_orderbook": "get-orderbook",
    "get_paths": "get-paths",
    "path_find": "path-find",
    "screen_sanctions": "screen-sanctions",
    "submit_payment": "submit-payment",
}


def qualified_tool_name(tool_name: str) -> str:
    """The name the Gateway knows a tool by: `<targetName>___<toolName>`."""
    target = TOOL_TARGETS.get(tool_name)
    if target is None:
        raise ValueError(
            f"Unknown tool {tool_name!r}. It has no Gateway target in "
            f"TOOL_TARGETS, so the Gateway cannot route it. Known tools: "
            f"{sorted(TOOL_TARGETS)}"
        )
    return f"{target}{GATEWAY_TOOL_SEPARATOR}{tool_name}"

# Tool pricing (x402 micropayment fees)
TOOL_PRICING = {
    "get_orderbook": {"amount": "0.003", "currency": "USD"},
    "get_paths": {"amount": "0.003", "currency": "USD"},
    "screen_sanctions": {"amount": "0.01", "currency": "USD"},
}

# Tools that are free (no x402 gate)
FREE_TOOLS = {"submit_payment", "get_balance", "path_find", "check_transaction", "get_trust_lines"}

logger = logging.getLogger("xrpl_agentic_payments.agentcore_client")

# ─────────────────────────────────────────────────────────────────────────────
# Boto3 Clients (lazy-initialized)
# ─────────────────────────────────────────────────────────────────────────────

_data_client = None
_control_client = None


def _get_data_client():
    """Get or create the AgentCore data plane client."""
    global _data_client
    if _data_client is None:
        _data_client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _data_client


def _get_control_client():
    """Get or create the AgentCore control plane client."""
    global _control_client
    if _control_client is None:
        _control_client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    return _control_client


# ─────────────────────────────────────────────────────────────────────────────
# Payment Session Management
# ─────────────────────────────────────────────────────────────────────────────


def new_payment_session_id() -> str:
    """Mint a session ID identifying one payment run.

    Call this ONCE per payment and pass the result to every invoke_tool() in that
    run. It is what ties the six-or-so tool calls a payment makes to each other:
    submit_payment records it in the XRPL attribution memo's ``session_id`` field
    (functions/xrpl_core.py), so one payment's calls share one identifier that is
    visible on the ledger and in CloudWatch, and two payments never do.

    Before the Gateway existed this was a ``runtimeSessionId`` and the reason to
    share it was cost: AgentCore Runtime treats a previously unseen session ID as
    a new session, a session is an isolated microVM, and minting an ID per call
    meant six microVMs per payment — each cold-started and then held for the
    15-minute idle timeout. Routing tools through the Gateway to Lambda retires
    that particular bill: Lambda has no session to provision. Sharing one ID per
    payment is still the contract, now for attribution instead of warm start, and
    the per-payment scope still matters — a module-level ID reused forever would
    stamp every unrelated user's payment with the same on-ledger session.

    The 33-character floor is kept: it was an InvokeAgentRuntime requirement, and
    the format stays valid for the Runtime so a caller using that path directly
    is not handed an ID the API would reject.
    """
    return "xrpl-agentic-payments-session-" + str(uuid4()).replace("-", "")[:20]


# ─────────────────────────────────────────────────────────────────────────────
# Gateway transport — SigV4-signed MCP over HTTPS
# ─────────────────────────────────────────────────────────────────────────────

_http_session: Optional[URLLib3Session] = None

_MISSING_GATEWAY_URL = (
    "AGENTCORE_GATEWAY_URL is not set, so there is no Gateway to call tools "
    "through. The EKS deployment sets it from the Gateway's own URL "
    "(infra/lib/tools-stack.ts → infra/lib/eks-stack.ts); to run outside EKS, "
    "read it from the ToolsStack output:\n"
    "  aws cloudformation describe-stacks "
    "--stack-name XrplAgenticPaymentsToolsStack \\\n"
    "    --query \"Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue\" "
    "--output text"
)


def _get_http_session() -> URLLib3Session:
    """Get or create the pooled HTTPS session used for Gateway calls.

    botocore's own session rather than `requests`: it is already a hard
    dependency via boto3, it pools connections across the several tool calls one
    payment makes, and it takes the signed AWSRequest directly.
    """
    global _http_session
    if _http_session is None:
        _http_session = URLLib3Session()
    return _http_session


def _signed_gateway_request(body: str, tool_name: str) -> AWSRequest:
    """Build the SigV4-signed POST that carries one tools/call to the Gateway."""
    if not GATEWAY_URL:
        raise RuntimeError(_MISSING_GATEWAY_URL)

    request = AWSRequest(
        method="POST",
        url=GATEWAY_URL,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # The Gateway may answer either way; both are accepted so a
            # streaming response is not a 406.
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        },
    )

    # Credentials come from the ambient chain — Pod Identity in the deployment,
    # a profile or env vars locally. Resolved per call rather than cached
    # because they expire and botocore refreshes them behind this call.
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError(
            f"No AWS credentials available to sign the Gateway call for "
            f"'{tool_name}'. The Gateway uses SigV4 inbound auth "
            f"(authorizerType AWS_IAM), so an unauthenticated call is not "
            f"possible."
        )

    SigV4Auth(credentials, SIGNING_SERVICE, REGION).add_auth(request)
    return request


# ─────────────────────────────────────────────────────────────────────────────
# Core: invoke_tool
# ─────────────────────────────────────────────────────────────────────────────


def invoke_tool(tool_name: str, arguments: dict, session_id: Optional[str] = None) -> dict:
    """
    Call a tool through the AgentCore Gateway.

    Sends an MCP ``tools/call`` as JSON-RPC 2.0 in a SigV4-signed POST to the
    Gateway's MCP endpoint. The Gateway validates the arguments against the tool
    schema registered for the target and invokes the matching Lambda
    (``functions/<tool>.py``), which talks to XRPL testnet.

    Args:
        tool_name: Bare tool name, e.g. 'screen_sanctions'. Prefixed with its
            Gateway target name before being sent — see qualified_tool_name().
        arguments: Tool arguments as a dict
        session_id: Identifies the payment this call belongs to. Callers making
            more than one call for the same payment MUST pass the same ID — see
            new_payment_session_id(). For submit_payment it reaches the ledger,
            as the attribution memo's session_id.

    Returns:
        Parsed tool result dict from the JSON-RPC response.

    Raises:
        RuntimeError: If AGENTCORE_GATEWAY_URL is unset, the Gateway returns a
            non-2xx status or a JSON-RPC error, or the response cannot be parsed.
        ValueError: If tool_name has no Gateway target.
    """
    if session_id is None:
        # Generated rather than rejected so a one-off call from a script works.
        # WARNING because inside a payment this is a bug: the calls would no
        # longer share an identifier, and submit_payment would stamp the ledger
        # with a session that correlates to nothing.
        session_id = new_payment_session_id()
        logger.warning(
            "invoke_tool('%s') called with no session_id — this call will not be "
            "correlated with any other. Pass a session_id shared across the "
            "payment.",
            tool_name,
        )

    gateway_tool = qualified_tool_name(tool_name)

    # submit_payment writes the session id into the on-ledger attribution memo.
    # Injected here rather than at every call site so the memo cannot be left
    # stamped with the Lambda request id, which correlates nothing across the
    # payment. Only for the tool whose schema declares the parameter — the
    # Gateway rejects arguments a target's schema does not list.
    if tool_name == "submit_payment":
        arguments = {**arguments, "session_id": session_id}

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": int(time.time() * 1000) % 100000,
        "method": "tools/call",
        "params": {
            "name": gateway_tool,
            "arguments": arguments,
        },
    })

    logger.info(
        "Invoking tool '%s' via AgentCore Gateway (session %s)",
        gateway_tool,
        session_id,
    )
    logger.debug(f"  Payload: {payload[:200]}")

    request = _signed_gateway_request(payload, tool_name)
    response = _get_http_session().send(request.prepare())

    body = response.content.decode("utf-8") if response.content else ""
    logger.debug(f"  Raw response: {body[:500]}")

    # Checked before parsing: a 403 from the SigV4 authorizer or a 424 from a
    # failing target has no JSON-RPC envelope, and the parse error it would
    # otherwise raise would name the wrong problem.
    if response.status_code >= 300:
        raise RuntimeError(
            f"AgentCore Gateway returned HTTP {response.status_code} for "
            f"'{gateway_tool}': {body[:300]}"
        )

    return _parse_response(body, tool_name)


def _parse_response(body: str, tool_name: str) -> dict:
    """Parse the AgentCore Runtime response (JSON-RPC or SSE events)."""
    # Try direct JSON-RPC parse first
    try:
        data = json.loads(body)
        return _extract_from_jsonrpc(data, tool_name)
    except json.JSONDecodeError:
        pass

    # Handle SSE event stream format
    # Events look like: event: message\ndata: {...}\n\n
    lines = body.strip().split("\n")
    json_lines = []
    for line in lines:
        if line.startswith("data: "):
            json_lines.append(line[6:])
        elif line.startswith("{"):
            json_lines.append(line)

    for json_str in reversed(json_lines):
        try:
            data = json.loads(json_str)
            return _extract_from_jsonrpc(data, tool_name)
        except json.JSONDecodeError:
            continue
        except RuntimeError:
            raise

    # Last resort: try to parse the whole body as the result
    raise RuntimeError(f"Could not parse response for tool '{tool_name}': {body[:200]}")


def _extract_from_jsonrpc(data: dict, tool_name: str) -> dict:
    """Extract tool result from a JSON-RPC response object."""
    if "result" in data:
        result = data["result"]
        content = result.get("content", [])
        is_error = result.get("isError", False)

        if content and isinstance(content, list):
            text_content = content[0].get("text", "{}")
            # If the tool itself reported an error via isError flag
            if is_error:
                raise RuntimeError(f"Tool '{tool_name}' execution error: {text_content[:300]}")
            # Try to parse as JSON, fall back to wrapping as text
            try:
                return json.loads(text_content)
            except json.JSONDecodeError:
                return {"text": text_content}
        return result
    elif "error" in data:
        error = data["error"]
        raise RuntimeError(
            f"Tool '{tool_name}' returned JSON-RPC error: [{error.get('code', -1)}] {error.get('message', 'unknown')}"
        )
    return data


# ─────────────────────────────────────────────────────────────────────────────
# x402 Micropayment Integration
# ─────────────────────────────────────────────────────────────────────────────


class PaymentGate:
    """
    Manages x402 micropayment sessions for premium tool access.

    Flow:
    1. Create a payment session with spending limit
    2. Before calling a premium tool, process_payment charges the fee
    3. Tool is invoked only after successful payment
    4. Session tracks cumulative spend

    If payment infrastructure is unavailable (no instrument provisioned),
    falls back to logging the charge and proceeding (graceful degradation).
    """

    def __init__(self, user_id: str = "xrpl-agentic-payments-orchestrator", agent_name: str = "xrpl-agentic-payments"):
        self.user_id = user_id
        self.agent_name = agent_name
        self.session_id: Optional[str] = None
        self.instrument_id: Optional[str] = None
        self.total_charged: float = 0.0
        self._initialized = False

    def initialize(self, max_spend: str = "1.00") -> bool:
        """
        Create a payment session and discover payment instruments.

        Args:
            max_spend: Maximum spending limit for the session in USD.

        Returns:
            True if payment infrastructure is available, False for graceful fallback.
        """
        client = _get_data_client()

        # Create payment session
        try:
            resp = client.create_payment_session(
                userId=self.user_id,
                agentName=self.agent_name,
                paymentManagerArn=PAYMENT_MANAGER_ARN,
                limits={
                    "maxSpendAmount": {
                        "value": max_spend,
                        "currency": "USD",
                    }
                },
                expiryTimeInMinutes=60,
            )
            # CreatePaymentSession nests its result: the response shape is
            # {"paymentSession": {"paymentSessionId": ...}}, with paymentSession as
            # its only top-level member. Reading "paymentSessionId" off the response
            # root always yielded None, which made the `self.session_id and
            # self.instrument_id` guard in charge() unsatisfiable — so process_payment
            # was never reached and every charge silently took the "simulated" path.
            self.session_id = resp["paymentSession"]["paymentSessionId"]
            logger.info(f"Payment session created: {self.session_id}")
        except Exception as e:
            logger.warning(f"Could not create payment session (graceful degradation): {e}")
            self._initialized = True
            return False

        # Discover payment instrument
        try:
            instruments = client.list_payment_instruments(
                userId=self.user_id,
                agentName=self.agent_name,
                paymentManagerArn=PAYMENT_MANAGER_ARN,
            )
            items = instruments.get("paymentInstruments", [])
            if items:
                self.instrument_id = items[0].get("paymentInstrumentId")
                logger.info(f"Payment instrument found: {self.instrument_id}")
            else:
                logger.warning("No payment instrument found — x402 charges will be logged only")
        except Exception as e:
            logger.warning(f"Could not list payment instruments: {e}")

        self._initialized = True
        return self.session_id is not None and self.instrument_id is not None

    def charge(self, tool_name: str) -> dict:
        """
        Process an x402 micropayment for a premium tool.

        Args:
            tool_name: Name of the tool being gated.

        Returns:
            Payment result dict with status and receipt, or simulated receipt on fallback.
        """
        if tool_name in FREE_TOOLS:
            return {"status": "free", "charged": "0", "tool": tool_name}

        pricing = TOOL_PRICING.get(tool_name)
        if not pricing:
            return {"status": "free", "charged": "0", "tool": tool_name}

        if not self._initialized:
            self.initialize()

        amount = pricing["amount"]
        currency = pricing["currency"]

        # If we have full payment infra, process real x402
        if self.session_id and self.instrument_id:
            try:
                client = _get_data_client()
                resp = client.process_payment(
                    userId=self.user_id,
                    agentName=self.agent_name,
                    paymentManagerArn=PAYMENT_MANAGER_ARN,
                    paymentSessionId=self.session_id,
                    paymentInstrumentId=self.instrument_id,
                    paymentType="CRYPTO_X402",
                    paymentInput={
                        "cryptoX402": {
                            "version": "1",
                            "payload": {
                                "tool": tool_name,
                                "amount": amount,
                                "currency": currency,
                                "recipient": "xrpl-agentic-payments-mcp-server",
                            },
                        }
                    },
                )

                status = resp.get("status", "unknown")
                self.total_charged += float(amount)
                logger.info(f"x402 payment processed: ${amount} for {tool_name} (status: {status})")

                return {
                    "status": status,
                    "charged": amount,
                    "currency": currency,
                    "tool": tool_name,
                    "session_id": self.session_id,
                    "total_session_spend": str(self.total_charged),
                }
            except Exception as e:
                logger.warning(f"x402 payment failed (proceeding with fallback): {e}")

        # Graceful fallback: log the charge but allow the tool call
        self.total_charged += float(amount)
        logger.info(f"x402 simulated charge: ${amount} for {tool_name} (no instrument — logged only)")

        return {
            "status": "simulated",
            "charged": amount,
            "currency": currency,
            "tool": tool_name,
            "total_session_spend": str(self.total_charged),
            "note": "Payment instrument not provisioned — charge logged for reconciliation",
        }

    def get_session_summary(self) -> dict:
        """Get summary of the current payment session."""
        return {
            "session_id": self.session_id,
            "instrument_id": self.instrument_id,
            "total_charged_usd": str(self.total_charged),
            "initialized": self._initialized,
            "has_real_payments": self.session_id is not None and self.instrument_id is not None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: invoke_tool_with_payment
# ─────────────────────────────────────────────────────────────────────────────

# Module-level payment gate instance (shared across orchestrator lifecycle)
_payment_gate: Optional[PaymentGate] = None


def get_payment_gate() -> PaymentGate:
    """Get or create the module-level PaymentGate instance."""
    global _payment_gate
    if _payment_gate is None:
        _payment_gate = PaymentGate()
        _payment_gate.initialize()
    return _payment_gate


def invoke_tool_with_payment(tool_name: str, arguments: dict, session_id: Optional[str] = None) -> dict:
    """
    Invoke a tool with automatic x402 payment gating.

    For premium tools (get_orderbook, get_paths, screen_sanctions):
    1. Processes micropayment via AgentCore Payments
    2. Invokes the tool through the AgentCore Gateway
    3. Returns combined result with payment receipt

    For free tools: invokes directly without payment.

    Args:
        tool_name: MCP tool name
        arguments: Tool arguments
        session_id: Identifies the payment, shared across every call in it.
            See new_payment_session_id() for why this should not be left None.

    Returns:
        Dict with 'result' (tool output) and 'payment' (charge info).
    """
    gate = get_payment_gate()

    # Step 1: Process payment (no-op for free tools)
    payment_receipt = gate.charge(tool_name)

    # Step 2: Invoke the tool on AgentCore Runtime
    tool_result = invoke_tool(tool_name, arguments, session_id=session_id)

    return {
        "result": tool_result,
        "payment": payment_receipt,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Gateway Target Health Check
# ─────────────────────────────────────────────────────────────────────────────


def check_gateway_target_status() -> dict:
    """Report the Gateway and the status of every tool target behind it.

    Lists the targets rather than fetching one by id. There are eight — one per
    tool Lambda — so the previous version, which read a single
    AGENTCORE_GATEWAY_TARGET_ID and reported ``mcpServer.endpoint``, described a
    gateway with one MCP-server target that this deployment does not have: it
    would have reported nothing for seven of eight tools and an empty endpoint
    for the eighth, since Lambda targets have no endpoint field.
    """
    if not GATEWAY_ID:
        return {"gateway_id": "", "status": "NOT_DEPLOYED", "targets": []}

    ctrl = _get_control_client()
    try:
        targets: list[dict] = []
        paginator_token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {"gatewayIdentifier": GATEWAY_ID}
            if paginator_token:
                kwargs["nextToken"] = paginator_token
            resp = ctrl.list_gateway_targets(**kwargs)
            for item in resp.get("items", []):
                targets.append({
                    "target_id": item.get("targetId", ""),
                    "name": item.get("name", ""),
                    "status": item.get("status", "UNKNOWN"),
                })
            paginator_token = resp.get("nextToken")
            if not paginator_token:
                break

        not_ready = [t for t in targets if t["status"] != "READY"]
        return {
            "gateway_id": GATEWAY_ID,
            "status": "READY" if targets and not not_ready else "DEGRADED",
            "target_count": len(targets),
            "targets": targets,
        }
    except Exception as e:
        return {"gateway_id": GATEWAY_ID, "status": "ERROR", "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# CLI Test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 60)
    print("  AgentCore Client — Integration Test")
    print("=" * 60)

    # Check gateway targets
    print("\n[1] Checking gateway target status...")
    status = check_gateway_target_status()
    print(f"    Gateway: {status.get('gateway_id') or '(not configured)'}")
    print(f"    Status: {status['status']}")
    for target in status.get("targets", []):
        print(f"      - {target['name']}: {target['status']}")
    if "error" in status:
        print(f"    Error: {status['error']}")

    # Test tool invocation
    print("\n[2] Invoking screen_sanctions via AgentCore Gateway...")
    try:
        result = invoke_tool("screen_sanctions", {"entity_name": "Acme Corp", "entity_country": "US"})
        print(f"    Result: {json.dumps(result, indent=4)}")
    except Exception as e:
        print(f"    Error: {e}")

    # Test with payment
    print("\n[3] Invoking screen_sanctions with x402 payment gate...")
    try:
        full_result = invoke_tool_with_payment("screen_sanctions", {"entity_name": "Acme Corp", "entity_country": "US"})
        print(f"    Tool result: {json.dumps(full_result['result'], indent=4)}")
        print(f"    Payment: {json.dumps(full_result['payment'], indent=4)}")
    except Exception as e:
        print(f"    Error: {e}")

    # Payment session summary
    print("\n[4] Payment session summary:")
    gate = get_payment_gate()
    summary = gate.get_session_summary()
    print(f"    {json.dumps(summary, indent=4)}")

    print("\n" + "=" * 60)

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the AgentCore Gateway tool path in src/agentcore_client.py.

The Gateway was in every architecture diagram and in none of the code: the stack
granted bedrock-agentcore permission to invoke the eight tool Lambdas, nothing
created a gateway or a target, and invoke_tool() called invoke_agent_runtime()
against the Runtime-hosted MCP server instead. The Lambdas were deployed and
unreachable. These tests cover the path that replaced it.

Two things here can only fail after a deploy, so they are asserted against the
CDK source directly rather than mocked:

  * the target names. Gateway exposes tools as `<targetName>___<toolName>`, so
    TOOL_TARGETS and infra/lib/tools-stack.ts must agree exactly or every call
    returns "tool not found" from a gateway that is otherwise healthy.
  * the MCP protocol version. A gateway rejects a version outside its
    supportedVersions, so the client default and the pinned constant must match.
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("boto3", reason="boto3 not installed")

from src import agentcore_client  # noqa: E402
from src.agentcore_client import (  # noqa: E402
    GATEWAY_TOOL_SEPARATOR,
    TOOL_TARGETS,
    invoke_tool,
    qualified_tool_name,
)

TOOLS_STACK = REPO_ROOT / "infra/lib/tools-stack.ts"

GATEWAY_URL = (
    "https://gw-abc123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
)


# ─────────────────────────────────────────────────────────────────────────────
# Test doubles for the HTTP layer
# ─────────────────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raw=None):
        self.status_code = status_code
        if raw is not None:
            self.content = raw.encode("utf-8")
        else:
            body = payload if payload is not None else {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"text": json.dumps({"ok": True})}],
                    "isError": False,
                },
            }
            self.content = json.dumps(body).encode("utf-8")


class FakeHttpSession:
    """Captures the prepared request instead of sending it."""

    def __init__(self, response=None):
        self.sent = []
        self._response = response or FakeResponse()

    def send(self, request):
        self.sent.append(request)
        return self._response


@pytest.fixture
def gateway(monkeypatch):
    """A configured Gateway with credentials available and the network stubbed."""
    # GATEWAY_URL is read into a module constant at import, so the module
    # attribute is what has to be patched — not just the environment.
    monkeypatch.setattr(agentcore_client, "GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    session = FakeHttpSession()
    monkeypatch.setattr(agentcore_client, "_get_http_session", lambda: session)
    return session


def sent_body(session) -> dict:
    """The JSON-RPC envelope of the single request the client sent."""
    assert len(session.sent) == 1, f"expected one request, sent {len(session.sent)}"
    return json.loads(session.sent[0].body)


# ─────────────────────────────────────────────────────────────────────────────
# Tool naming — the half that a deploy cannot reveal until a call is made
# ─────────────────────────────────────────────────────────────────────────────


def test_the_gateway_is_asked_for_the_prefixed_tool_name(gateway):
    """A bare tool name is not a name the Gateway knows."""
    invoke_tool("get_balance", {"account": "rEXAMPLE"})

    assert sent_body(gateway)["params"]["name"] == "get-balance___get_balance"


def test_every_tool_target_matches_the_name_declared_in_the_cdk_stack():
    """TOOL_TARGETS is the client's copy of names the stack owns.

    Parsed out of the TypeScript rather than duplicated in the test, so this
    fails when someone renames a target on one side only — the failure mode is a
    healthy gateway that reports every tool as not found.
    """
    source = TOOLS_STACK.read_text()

    # Each entry in the toolTargets array pairs a targetName with a toolName.
    declared = dict(
        (tool, target)
        for target, tool in re.findall(
            r'targetName:\s*"([^"]+)",\s*\n\s*toolName:\s*"([^"]+)"', source
        )
    )

    assert declared, "found no targetName/toolName pairs in tools-stack.ts"
    assert declared == TOOL_TARGETS, (
        "TOOL_TARGETS disagrees with infra/lib/tools-stack.ts.\n"
        f"  only in the stack:  {sorted(set(declared.items()) - set(TOOL_TARGETS.items()))}\n"
        f"  only in the client: {sorted(set(TOOL_TARGETS.items()) - set(declared.items()))}"
    )


def test_the_separator_matches_the_one_the_lambdas_strip():
    """functions/shared.py recovers the bare name by splitting on this."""
    shared = (REPO_ROOT / "functions/shared.py").read_text()
    assert f'delimiter = "{GATEWAY_TOOL_SEPARATOR}"' in shared


def test_no_target_name_contains_the_separator():
    """Target names must not contain `___`, or the prefix is ambiguous.

    strip_tool_prefix() splits on the FIRST occurrence. The
    CreateGatewayTarget API allows only alphanumerics and hyphens, so this holds
    by construction — asserted because the consequence of breaking it is a tool
    name that silently resolves to the wrong tool.
    """
    for tool, target in TOOL_TARGETS.items():
        assert GATEWAY_TOOL_SEPARATOR not in target, f"{tool} → {target}"
        assert re.fullmatch(r"[0-9a-zA-Z-]+", target), (
            f"{target!r} is not a valid Gateway target name"
        )


def test_every_tool_has_a_lambda_that_implements_it():
    """A target routing to a handler that does not exist deploys clean."""
    for tool in TOOL_TARGETS:
        assert (REPO_ROOT / f"functions/{tool}.py").exists(), (
            f"TOOL_TARGETS names {tool}, but functions/{tool}.py does not exist"
        )


def test_an_unknown_tool_is_refused_before_any_network_call(gateway):
    with pytest.raises(ValueError, match="no Gateway target"):
        invoke_tool("definitely_not_a_tool", {})
    assert gateway.sent == []


# ─────────────────────────────────────────────────────────────────────────────
# The request itself
# ─────────────────────────────────────────────────────────────────────────────


def test_the_request_goes_to_the_gateway_endpoint(gateway):
    invoke_tool("get_balance", {"account": "rEXAMPLE"})

    request = gateway.sent[0]
    assert request.url == GATEWAY_URL
    assert request.method == "POST"


def test_the_request_is_sigv4_signed_for_bedrock_agentcore(gateway):
    """The Gateway's authorizerType is AWS_IAM, so an unsigned call is a 403.

    Also pins the SIGNING service name: signing as the wrong service produces a
    signature mismatch that reads like a credentials problem.
    """
    invoke_tool("get_balance", {"account": "rEXAMPLE"})

    auth = gateway.sent[0].headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 ")
    assert "/us-west-2/bedrock-agentcore/aws4_request" in auth


def test_the_protocol_version_header_is_sent(gateway):
    """A gateway rejects any version outside its supportedVersions."""
    invoke_tool("get_balance", {"account": "rEXAMPLE"})

    headers = gateway.sent[0].headers
    assert headers["MCP-Protocol-Version"] == agentcore_client.MCP_PROTOCOL_VERSION
    # Both, so a streaming answer is not refused with a 406.
    assert headers["Accept"] == "application/json, text/event-stream"


def test_the_client_default_protocol_version_is_the_one_pinned_on_the_gateway():
    """One constant feeds the gateway and the pod; the fallback must match it."""
    declared = re.search(
        r'MCP_PROTOCOL_VERSION = "([^"]+)"', TOOLS_STACK.read_text()
    )
    assert declared, "MCP_PROTOCOL_VERSION not found in tools-stack.ts"
    assert agentcore_client.MCP_PROTOCOL_VERSION == declared.group(1)


def test_a_jsonrpc_tools_call_is_what_gets_sent(gateway):
    invoke_tool("screen_sanctions", {"entity_name": "Acme Corp"})

    body = sent_body(gateway)
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    assert body["params"]["arguments"] == {"entity_name": "Acme Corp"}


# ─────────────────────────────────────────────────────────────────────────────
# Session id reaches the ledger
# ─────────────────────────────────────────────────────────────────────────────


def test_submit_payment_carries_the_session_id_into_its_arguments(gateway):
    """This is what puts the payment's session in the on-ledger memo.

    functions/submit_payment.py prefers event["session_id"] over its own request
    id, so without this injection the memo identifies one Lambda invocation
    rather than the payment.
    """
    invoke_tool("submit_payment", {"destination": "rDEST", "amount": "10"},
                session_id="session-under-test-0123456789abcdef")

    arguments = sent_body(gateway)["params"]["arguments"]
    assert arguments["session_id"] == "session-under-test-0123456789abcdef"
    assert arguments["destination"] == "rDEST"


def test_the_session_id_is_not_injected_into_tools_whose_schema_lacks_it(gateway):
    """The Gateway validates arguments against the target's schema.

    Only submit-payment declares session_id, so adding it elsewhere would be an
    argument the Gateway has no property for.
    """
    invoke_tool("get_balance", {"account": "rEXAMPLE"},
                session_id="session-under-test-0123456789abcdef")

    assert sent_body(gateway)["params"]["arguments"] == {"account": "rEXAMPLE"}


def test_the_caller_supplied_session_id_is_not_overwritten(gateway):
    invoke_tool("submit_payment",
                {"destination": "rDEST", "amount": "10", "session_id": "explicit"},
                session_id="session-under-test-0123456789abcdef")

    # The orchestrator's per-payment id wins: it is the one that correlates the
    # rest of the run, and a tool argument is model-adjacent input.
    assert sent_body(gateway)["params"]["arguments"]["session_id"] == (
        "session-under-test-0123456789abcdef"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Failures name the actual problem
# ─────────────────────────────────────────────────────────────────────────────


def test_an_unset_gateway_url_names_the_variable(monkeypatch):
    """It must not fall back to a path that bypasses the Gateway."""
    monkeypatch.setattr(agentcore_client, "GATEWAY_URL", "")

    with pytest.raises(RuntimeError, match="AGENTCORE_GATEWAY_URL"):
        invoke_tool("get_balance", {"account": "rEXAMPLE"})


def test_an_http_error_is_reported_as_an_http_error(monkeypatch, gateway):
    """A 403 from the SigV4 authorizer has no JSON-RPC envelope.

    Parsed as one it would raise "could not parse response", which points at the
    payload instead of at the authorization failure.
    """
    monkeypatch.setattr(
        agentcore_client,
        "_get_http_session",
        lambda: FakeHttpSession(FakeResponse(status_code=403, raw="AccessDenied")),
    )

    with pytest.raises(RuntimeError, match="HTTP 403"):
        invoke_tool("get_balance", {"account": "rEXAMPLE"})


def test_a_jsonrpc_error_from_a_tool_is_raised(monkeypatch, gateway):
    monkeypatch.setattr(
        agentcore_client,
        "_get_http_session",
        lambda: FakeHttpSession(
            FakeResponse(payload={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32602, "message": "missing required: account"},
            })
        ),
    )

    with pytest.raises(RuntimeError, match="missing required: account"):
        invoke_tool("get_balance", {})


def test_a_successful_call_returns_the_parsed_tool_payload(gateway):
    result = invoke_tool("get_balance", {"account": "rEXAMPLE"})
    assert result == {"ok": True}


def test_qualified_tool_name_is_stable_for_every_known_tool():
    for tool, target in TOOL_TARGETS.items():
        assert qualified_tool_name(tool) == f"{target}___{tool}"

from __future__ import annotations

import json

import pytest

from xrpl_agentcore.gateway_client import GatewayClient, qualified_tool_name


class CredentialsSession:
    def get_credentials(self):
        from botocore.credentials import Credentials

        return Credentials("access", "secret", "token")


class Response:
    def __init__(self, status_code=200, content=None) -> None:
        self.status_code = status_code
        self.content = (
            content or json.dumps({"jsonrpc": "2.0", "result": {"structured": "ok"}}).encode()
        )


class HttpSession:
    def __init__(self, response=None) -> None:
        self.request = None
        self.response = response or Response()

    def send(self, request):
        self.request = request
        return self.response


def test_gateway_client_injects_owner_outside_model_arguments() -> None:
    http = HttpSession()
    client = GatewayClient(
        owner_sub="trusted-owner",
        policy_session_id="run-123",
        gateway_url="https://example.com/mcp",
        region="us-west-2",
        session=CredentialsSession(),
        http_session=http,
    )
    assert client.invoke(
        "get_transfer_status",
        {
            "transfer_id": "tr_test",
            "owner_sub": "forged-model-owner",
            "request": {
                "recipient_address": "rAddress",
                "destination_tag": None,
                "payout_alias": None,
            },
        },
    ) == {"structured": "ok"}
    body = json.loads(http.request.body)
    assert body["params"]["arguments"]["owner_sub"] == "trusted-owner"
    assert body["params"]["arguments"]["request"] == {
        "recipient_address": "rAddress",
    }
    assert http.request.headers["x-amzn-bedrock-agentcore-policy-session-id"] == "run-123"
    assert http.request.headers["MCP-Protocol-Version"] == "2025-06-18"


def test_gateway_failure_diagnostics_redact_owner(caplog) -> None:
    http = HttpSession(
        Response(
            status_code=403,
            content=b'{"message":"trusted-owner is not authorized"}',
        )
    )
    client = GatewayClient(
        owner_sub="trusted-owner",
        policy_session_id="run-123",
        gateway_url="https://example.com/mcp",
        region="us-west-2",
        session=CredentialsSession(),
        http_session=http,
    )

    with pytest.raises(RuntimeError, match=r"\[REDACTED_OWNER\]"):
        client.invoke("list_supported_corridors", {})

    assert "trusted-owner" not in caplog.text
    assert "[REDACTED_OWNER]" in caplog.text


def test_gateway_mcp_error_result_is_not_treated_as_success(caplog) -> None:
    content = json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": "policy denied the tool"}],
                "isError": True,
            },
        }
    ).encode()
    client = GatewayClient(
        owner_sub="trusted-owner",
        policy_session_id="run-123",
        gateway_url="https://example.com/mcp",
        region="us-west-2",
        session=CredentialsSession(),
        http_session=HttpSession(Response(content=content)),
    )

    with pytest.raises(RuntimeError, match="policy denied the tool"):
        client.invoke("list_supported_corridors", {})

    assert "Gateway response failure" in caplog.text


def test_gateway_surface_has_no_approval_or_execution_tool() -> None:
    with pytest.raises(ValueError):
        qualified_tool_name("approve_transfer")
    with pytest.raises(ValueError):
        qualified_tool_name("execute_transfer")

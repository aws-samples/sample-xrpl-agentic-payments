"""SigV4 client for the narrow AgentCore Gateway tool surface."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

POLICY_SESSION_HEADER = "x-amzn-bedrock-agentcore-policy-session-id"
SIGNING_SERVICE = "bedrock-agentcore"
TOOL_SEPARATOR = "___"
LOGGER = logging.getLogger(__name__)
TOOL_TARGETS = {
    "list_supported_corridors": "list-supported-corridors",
    "get_transfer_quote": "get-transfer-quote",
    "create_transfer_intent": "create-transfer-intent",
    "get_transfer_status": "get-transfer-status",
}


class GatewayError(RuntimeError):
    pass


def _safe_diagnostic(value: str, owner_sub: str) -> str:
    return value.replace(owner_sub, "[REDACTED_OWNER]")[:300]


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none(child) for key, child in value.items() if child is not None}
    if isinstance(value, list):
        return [_without_none(child) for child in value if child is not None]
    return value


def qualified_tool_name(tool_name: str) -> str:
    try:
        return f"{TOOL_TARGETS[tool_name]}{TOOL_SEPARATOR}{tool_name}"
    except KeyError:
        raise ValueError(f"unsupported Gateway tool {tool_name!r}") from None


def _extract_result(data: Any, tool_name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GatewayError(f"Gateway returned a non-object response for {tool_name}")
    if data.get("error"):
        error = data["error"]
        message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        raise GatewayError(f"Gateway denied or failed {tool_name}: {message}")
    result = data.get("result", data)
    if isinstance(result, dict) and result.get("isError") is True:
        messages = [
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        detail = " ".join(message for message in messages if message)
        raise GatewayError(
            f"Gateway tool error for {tool_name}: {detail or 'unspecified target failure'}"
        )
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for item in result["content"]:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("structuredContent"), dict):
                return item["structuredContent"]
            text = item.get("text")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    if isinstance(result, dict):
        return result
    raise GatewayError(f"Gateway returned no structured result for {tool_name}")


def _parse_response(body: str, tool_name: str) -> dict[str, Any]:
    try:
        return _extract_result(json.loads(body), tool_name)
    except json.JSONDecodeError:
        pass
    for line in body.splitlines():
        candidate = line[6:] if line.startswith("data: ") else line
        if not candidate.startswith("{"):
            continue
        try:
            return _extract_result(json.loads(candidate), tool_name)
        except json.JSONDecodeError:
            continue
    raise GatewayError(f"Gateway returned an unreadable response for {tool_name}")


class GatewayClient:
    """The caller injects owner identity; it is never exposed as a model argument."""

    def __init__(
        self,
        *,
        owner_sub: str,
        policy_session_id: str,
        gateway_url: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        http_session: URLLib3Session | None = None,
    ) -> None:
        if not owner_sub.strip():
            raise ValueError("owner_sub is required")
        if not policy_session_id.strip():
            raise ValueError("policy_session_id is required")
        self.owner_sub = owner_sub
        self.policy_session_id = policy_session_id
        self.gateway_url = gateway_url or os.environ.get("AGENTCORE_GATEWAY_URL", "")
        self.region = region or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
        self.session = session or boto3.Session()
        self.http_session = http_session or URLLib3Session()

    def invoke(self, tool_name: str, model_arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.gateway_url:
            raise GatewayError("AGENTCORE_GATEWAY_URL is required")
        arguments = _without_none({**model_arguments, "owner_sub": self.owner_sub})
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": int(time.time() * 1000) % 100_000,
                "method": "tools/call",
                "params": {
                    "name": qualified_tool_name(tool_name),
                    "arguments": arguments,
                },
            },
            separators=(",", ":"),
        )
        request = AWSRequest(
            method="POST",
            url=self.gateway_url,
            data=body.encode(),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": os.environ.get(
                    "AGENTCORE_MCP_PROTOCOL_VERSION", "2025-06-18"
                ),
                POLICY_SESSION_HEADER: self.policy_session_id,
            },
        )
        credentials = self.session.get_credentials()
        if credentials is None:
            raise GatewayError("AWS credentials are required for AgentCore Gateway")
        SigV4Auth(credentials, SIGNING_SERVICE, self.region).add_auth(request)
        response = self.http_session.send(request.prepare())
        response_body = response.content.decode() if response.content else ""
        if response.status_code >= 300:
            diagnostic = _safe_diagnostic(response_body, self.owner_sub)
            LOGGER.warning(
                "Gateway HTTP failure tool=%s status=%s response=%s",
                tool_name,
                response.status_code,
                diagnostic,
            )
            raise GatewayError(f"Gateway returned HTTP {response.status_code}: {diagnostic}")
        try:
            return _parse_response(response_body, tool_name)
        except GatewayError as error:
            LOGGER.warning(
                "Gateway response failure tool=%s detail=%s",
                tool_name,
                _safe_diagnostic(str(error), self.owner_sub),
            )
            raise

#!/usr/bin/env python3
"""Discover the Gateway's tools and a skill through AWS Agent Registry alone.

Proves the registry is a real access boundary, not just a directory: it
assumes RegistryConsumerDemoRole — a consumer identity that has never seen
this stack's Gateway URL or ARNs and can do nothing else in this account — and
uses only its Agent Registry permissions to search the catalog, read back the
Gateway's live-synced tool list and URL, and then invoke a tool live. It also
pulls the xrpl-agent-wallet skill's full SKILL.md straight out of the
registry, no second hop needed.

The registry's own MCP endpoint only exposes discovery (search/list/get); it
does not proxy tool invocation (see
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-mcp-endpoint.html).
So the Gateway call below is a second, direct SigV4 hop, made with the same
demo role's credentials and the same GatewayClient the Runtime itself uses
(src/xrpl_agentcore/gateway_client.py) — not a shortcut, but how a real
consumer would do it too, after the registry told them where to look.

See docs/architecture.md#agent-registry. Run any time after scripts/deploy.sh
has enabled the Runtime; the Gateway record's live sync needs the Gateway
deployed, which happens in the first CDK pass, before DeployAgentRuntime=true.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env_file import read_env_value  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from xrpl_agentcore.gateway_client import GatewayClient  # noqa: E402

DEFAULT_STACK_NAME = "XrplAgentCorePoc"
DEMO_SESSION_ID = "registry-discovery-demo"


def stack_outputs(stack_name: str, region: str) -> dict[str, str]:
    client = boto3.client("cloudformation", region_name=region)
    (stack,) = client.describe_stacks(StackName=stack_name)["Stacks"]
    return {
        output["OutputKey"]: output.get("OutputValue", "") for output in stack.get("Outputs", [])
    }


def assume_demo_role(role_arn: str, region: str) -> boto3.Session:
    sts = boto3.client("sts", region_name=region)
    credentials = sts.assume_role(RoleArn=role_arn, RoleSessionName=DEMO_SESSION_ID)[
        "Credentials"
    ]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


def find_record(client: Any, registry_arn: str, query: str, record_type: str) -> dict[str, Any]:
    """Cold search: the caller supplies only a natural-language query."""
    response = client.search_discoverable_registry_records(
        registryIds=[registry_arn],
        searchQuery=query,
        maxResults=10,
    )
    matches = [
        record
        for record in response.get("registryRecords", [])
        if record.get("recordType") == record_type
    ]
    if not matches:
        raise SystemExit(f"no {record_type} record matched search query {query!r}")
    return matches[0]


def full_record(client: Any, registry_arn: str, record_id: str) -> dict[str, Any]:
    response = client.batch_get_discoverable_registry_record(
        entries=[{"registryId": registry_arn, "recordIds": [record_id]}]
    )
    if response.get("errors"):
        raise SystemExit(f"batch_get_discoverable_registry_record errors: {response['errors']}")
    (record,) = response["registryRecords"]
    return record


def _tool_names(tools_payload: str) -> list[str]:
    parsed = json.loads(tools_payload)
    tools = parsed.get("tools", parsed) if isinstance(parsed, dict) else parsed
    return [tool["name"] for tool in tools if isinstance(tool, dict) and "name" in tool]


def demo_gateway(
    registry_client: Any, session: boto3.Session, registry_arn: str, region: str
) -> dict[str, Any]:
    summary = find_record(
        registry_client, registry_arn, "XRPL cross-border transfer Gateway tools", "MCP"
    )
    record = full_record(registry_client, registry_arn, summary["recordId"])
    mcp_server = record["descriptors"]["mcpServer"]
    gateway_url = mcp_server["source"]["fromUrl"]["url"]
    tool_names = _tool_names(mcp_server["additionalData"]["tools"]["data"])

    # Second hop: the registry told us where and what, not how to call it —
    # that's a direct SigV4 call, same as the Runtime's own GatewayClient.
    gateway_client = GatewayClient(
        owner_sub="registry-demo-consumer",
        policy_session_id=DEMO_SESSION_ID,
        gateway_url=gateway_url,
        region=region,
        session=session,
    )
    live_result = gateway_client.invoke("list_supported_corridors", {})

    return {
        "record_name": record["name"],
        "discovered_gateway_url": gateway_url,
        "discovered_tool_names": tool_names,
        "live_invocation_result": live_result,
    }


def demo_skill(registry_client: Any, registry_arn: str) -> dict[str, Any]:
    summary = find_record(
        registry_client, registry_arn, "XRPL wallet loading signing skill", "SKILL"
    )
    record = full_record(registry_client, registry_arn, summary["recordId"])
    skill_md = record["descriptors"]["agentSkillsDefinition"]["additionalData"]["skillMd"]["data"]
    return {
        "record_name": record["name"],
        "skill_md_length": len(skill_md),
        "skill_md_preview": skill_md.splitlines()[0:6],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack-name", default=os.environ.get("STACK_NAME", DEFAULT_STACK_NAME))
    parser.add_argument("--region", default=None)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    region = (
        args.region
        or os.environ.get("AWS_DEFAULT_REGION")
        or read_env_value(args.env_file, "AWS_DEFAULT_REGION")
    )
    if not region:
        raise SystemExit(
            f"AWS_DEFAULT_REGION is required. Set it in {args.env_file} or pass --region."
        )

    outputs = stack_outputs(args.stack_name, region)
    registry_arn = outputs.get("AgentRegistryArn")
    demo_role_arn = outputs.get("RegistryConsumerDemoRoleArn")
    if not registry_arn or not demo_role_arn:
        raise SystemExit(
            f"stack {args.stack_name} is missing AgentRegistryArn/RegistryConsumerDemoRoleArn "
            "outputs. Deploy the stack first."
        )

    # Everything below runs on the demo role's own narrow credentials, not
    # this shell's broader ones — it can search the registry, read approved
    # records, and invoke exactly this one Gateway. Nothing else.
    demo_session = assume_demo_role(demo_role_arn, region)
    registry_client = demo_session.client("agent-registry")

    result = {
        "gateway": demo_gateway(registry_client, demo_session, registry_arn, region),
        "skill": demo_skill(registry_client, registry_arn),
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()

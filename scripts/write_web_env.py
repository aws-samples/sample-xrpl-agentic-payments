#!/usr/bin/env python3
"""Write web/.env.local from the deployed stack's outputs and fixture recipient.

Reads the Amazon Cognito, HTTP API, and AgentCore Runtime values straight from
CloudFormation, in the same way scripts/deploy.sh derives them, so nothing
printed to a terminal has to be copy-pasted. The fixture recipient address and
payout alias come from .env, which scripts/verify_and_set_env.py writes.

Run this after scripts/deploy.sh has enabled the Runtime (its second, Runtime
pass). Nothing here is a secret: the API URL, Cognito IDs, and Runtime URL are
all endpoints, not credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _env_file import read_env_value, upsert_env  # noqa: E402

DEFAULT_STACK_NAME = "XrplAgentCorePoc"
DEFAULT_PAYOUT_ALIAS = "fixture-bank-token"


def stack_outputs(stack_name: str, region: str) -> dict[str, str]:
    client = boto3.client("cloudformation", region_name=region)
    (stack,) = client.describe_stacks(StackName=stack_name)["Stacks"]
    return {
        output["OutputKey"]: output.get("OutputValue", "") for output in stack.get("Outputs", [])
    }


def runtime_url(runtime_arn: str, region: str) -> str:
    # Matches the InvokeAgentRuntime HTTPS contract used by scripts/deploy.sh.
    encoded_arn = urllib.parse.quote(runtime_arn, safe="")
    return f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=default"


def web_env_values(stack_name: str, region: str, env_file: Path) -> dict[str, str]:
    outputs = stack_outputs(stack_name, region)
    missing_outputs = [
        key for key in ("ApiUrl", "UserPoolId", "UserPoolClientId") if not outputs.get(key)
    ]
    if missing_outputs:
        raise RuntimeError(f"stack {stack_name} is missing outputs: {sorted(missing_outputs)}")
    runtime_arn = outputs.get("RuntimeArn", "")
    if not runtime_arn:
        raise RuntimeError(
            f"stack {stack_name} has no RuntimeArn output yet. Deploy with "
            "DeployAgentRuntime=true first (scripts/deploy.sh does this in its "
            "second pass) before writing web/.env.local."
        )

    recipient = read_env_value(env_file, "NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS")
    if not recipient:
        raise RuntimeError(
            f"NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS is not set in {env_file}. Run "
            "scripts/verify_and_set_env.py first."
        )
    payout_alias = read_env_value(env_file, "NEXT_PUBLIC_DEMO_PAYOUT_ALIAS") or DEFAULT_PAYOUT_ALIAS

    return {
        "AGENTCORE_RUNTIME_URL": runtime_url(runtime_arn, region),
        "NEXT_PUBLIC_API_BASE_URL": outputs["ApiUrl"],
        "NEXT_PUBLIC_COGNITO_USER_POOL_ID": outputs["UserPoolId"],
        "NEXT_PUBLIC_COGNITO_CLIENT_ID": outputs["UserPoolClientId"],
        "NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS": recipient,
        "NEXT_PUBLIC_DEMO_PAYOUT_ALIAS": payout_alias,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack-name", default=os.environ.get("STACK_NAME", DEFAULT_STACK_NAME))
    parser.add_argument("--region", default=None)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, default=Path("web/.env.local"))
    args = parser.parse_args()

    # AWS_DEFAULT_REGION has exactly one source of truth: .env (see
    # .env.example). No hardcoded default here. An exported value or --region
    # wins; otherwise read it straight from .env, so this works without
    # sourcing .env into the shell first.
    region = (
        args.region
        or os.environ.get("AWS_DEFAULT_REGION")
        or read_env_value(args.env_file, "AWS_DEFAULT_REGION")
    )
    if not region:
        raise SystemExit(
            f"AWS_DEFAULT_REGION is required. Set it in {args.env_file} "
            "(see .env.example) or pass --region."
        )

    values = web_env_values(args.stack_name, region, args.env_file)
    upsert_env(args.output, values)
    print(json.dumps({"env_file": str(args.output), "env_keys_written": sorted(values)}, indent=2))


if __name__ == "__main__":
    main()

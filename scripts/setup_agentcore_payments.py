# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — AgentCore Payments Setup

Creates the AWS Bedrock AgentCore Payments infrastructure:
1. PaymentCredentialProvider — stores Coinbase CDP credentials securely
2. PaymentManager — manages payment sessions and budgets
3. PaymentConnector — links the manager to Coinbase CDP for x402 settlement

Prerequisites:
  - Coinbase CDP API key at ~/Downloads/cdp_api_key.json
  - Coinbase CDP wallet secret at ~/Downloads/cdp_wallet_secret.txt
  - AWS credentials configured (us-west-2)
  - Delegated signing enabled in CDP Portal

After running this script, the agent can create payment sessions and process
x402 micropayments for gated MCP tools (compliance checks, premium FX data).

Usage:
    python scripts/setup_agentcore_payments.py
"""

import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


AWS_REGION = "us-west-2"
CDP_KEY_FILE = Path.home() / "Downloads" / "cdp_api_key.json"
CDP_WALLET_SECRET_FILE = Path.home() / "Downloads" / "cdp_wallet_secret.txt"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "config" / "payments.json"


def load_cdp_credentials() -> tuple[dict, str]:
    """Load Coinbase CDP credentials from Downloads."""
    if not CDP_KEY_FILE.exists():
        print(f"  ❌ CDP API key not found: {CDP_KEY_FILE}")
        print("  Download from: portal.cdp.coinbase.com → API Keys → Create")
        sys.exit(1)

    if not CDP_WALLET_SECRET_FILE.exists():
        print(f"  ❌ CDP wallet secret not found: {CDP_WALLET_SECRET_FILE}")
        print("  Download from: portal.cdp.coinbase.com → Wallets → Export Secret")
        sys.exit(1)

    with open(CDP_KEY_FILE) as f:
        cdp_key = json.load(f)

    with open(CDP_WALLET_SECRET_FILE) as f:
        wallet_secret = f.read().strip()

    # Extract key ID — CDP key files may use "name" (organizations/.../apiKeys/<ID>) or "id" field
    key_name = cdp_key.get("name", "")
    key_id = key_name.split("/")[-1] if "/" in key_name else cdp_key.get("id", key_name)

    return cdp_key, wallet_secret, key_id


def setup_agentcore_payments() -> dict:
    """Create AgentCore Payments infrastructure."""
    print("═" * 60)
    print("  XRPL Agentic Payments — AgentCore Payments Setup")
    print("═" * 60)

    # Load credentials
    print("\n  Loading Coinbase CDP credentials...")
    cdp_key, wallet_secret, key_id = load_cdp_credentials()
    print(f"    API Key ID: {key_id[:8]}...{key_id[-4:]}")
    print(f"    Private key: {'✅ present' if cdp_key.get('privateKey') else '❌ missing'}")
    print(f"    Wallet secret: {'✅ present' if wallet_secret else '❌ missing'}")

    # Initialize boto3 client
    print(f"\n  Connecting to AWS ({AWS_REGION})...")
    try:
        client = boto3.client("bedrock-agentcore-control", region_name=AWS_REGION)
        print("    ✅ Connected to bedrock-agentcore-control")
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        print("\n  Note: bedrock-agentcore-control may not be available in your")
        print("  boto3 version. Update boto3 or check service availability.")
        sys.exit(1)

    results = {}

    # Step 1: Create Payment Credential Provider
    print("\n  Step 1: Creating Payment Credential Provider...")
    try:
        cred_response = client.create_payment_credential_provider(
            name="xrpl-agentic-payments-coinbase",
            credentialProviderVendor="CoinbaseCDP",
            providerConfigurationInput={
                "coinbaseCdpConfiguration": {
                    "apiKeyId": key_id,
                    "apiKeySecret": cdp_key["privateKey"],
                    "walletSecret": wallet_secret,
                }
            },
        )
        cred_arn = cred_response["credentialProviderArn"]
        results["credential_provider_arn"] = cred_arn
        print(f"    ✅ Created: {cred_arn}")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ConflictException":
            print("    ⚠️  Already exists (ConflictException) — retrieving existing...")
            # List and find existing
            list_response = client.list_payment_credential_providers()
            for provider in list_response.get("credentialProviders", []):
                if "xrpl-agentic-payments" in provider.get("name", ""):
                    cred_arn = provider["credentialProviderArn"]
                    results["credential_provider_arn"] = cred_arn
                    print(f"    ✅ Found existing: {cred_arn}")
                    break
        else:
            print(f"    ❌ Failed: {e}")
            sys.exit(1)

    # Step 2: Create Payment Manager
    print("\n  Step 2: Creating Payment Manager...")
    try:
        # Get current account ID for the role ARN
        sts = boto3.client("sts", region_name=AWS_REGION)
        account_id = sts.get_caller_identity()["Account"]
        role_name = "xrpl-agentic-payments-payments-role"
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

        # Create IAM role if it doesn't exist
        iam = boto3.client("iam")
        try:
            iam.get_role(RoleName=role_name)
            print(f"    IAM role exists: {role_arn}")
        except iam.exceptions.NoSuchEntityException:
            print(f"    Creating IAM role: {role_name}...")
            trust_policy = json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "bedrock.amazonaws.com"
                        },
                        "Action": "sts:AssumeRole",
                    }
                ],
            })
            iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=trust_policy,
                Description="XRPL Agentic Payments - allows AgentCore to manage payment sessions",
            )
            # Attach basic permissions for payment processing
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
            )
            print(f"    ✅ Role created: {role_arn}")
            # Wait briefly for IAM propagation
            import time
            time.sleep(5)

        manager_response = client.create_payment_manager(
            name="xrplAgenticPayments",
            authorizerType="AWS_IAM",
            roleArn=role_arn,
        )
        manager_id = manager_response["paymentManagerId"]
        results["payment_manager_id"] = manager_id
        print(f"    ✅ Created: {manager_id}")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ConflictException":
            print("    ⚠️  Already exists — retrieving existing...")
            list_response = client.list_payment_managers()
            for mgr in list_response.get("paymentManagers", []):
                if "xrplAgenticPayments" in mgr.get("name", ""):
                    manager_id = mgr["paymentManagerId"]
                    results["payment_manager_id"] = manager_id
                    print(f"    ✅ Found existing: {manager_id}")
                    break
        else:
            print(f"    ❌ Failed: {e}")
            sys.exit(1)

    # Step 3: Create Payment Connector
    print("\n  Step 3: Creating Payment Connector...")
    try:
        connector_response = client.create_payment_connector(
            paymentManagerId=manager_id,
            name="xrpl-agentic-payments-coinbase-connector",
            type="CoinbaseCDP",
            credentialProviderConfigurations=[
                {
                    "coinbaseCDP": {
                        "credentialProviderArn": results["credential_provider_arn"],
                    }
                }
            ],
        )
        connector_id = connector_response["paymentConnectorId"]
        results["payment_connector_id"] = connector_id
        print(f"    ✅ Created: {connector_id}")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ConflictException":
            print("    ⚠️  Already exists — using existing connector")
            results["payment_connector_id"] = "existing"
        else:
            print(f"    ❌ Failed: {e}")
            sys.exit(1)

    # Save results
    results["aws_region"] = AWS_REGION
    results["network"] = "BASE"  # Base L2 for USDC x402 payments
    results["session_defaults"] = {
        "max_spend_amount": "5.00",
        "currency": "USDC",
        "expiry_seconds": 900,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n{'═' * 60}")
    print(f"  ✅ AgentCore Payments setup complete")
    print(f"  📁 Config saved to: {OUTPUT_FILE}")
    print(f"\n  Resources created:")
    print(f"    Credential Provider: {results.get('credential_provider_arn', 'N/A')}")
    print(f"    Payment Manager:     {results.get('payment_manager_id', 'N/A')}")
    print(f"    Payment Connector:   {results.get('payment_connector_id', 'N/A')}")
    print(f"\n  Next steps:")
    print(f"    1. Create payment sessions at runtime (per agent invocation)")
    print(f"    2. x402 tools will auto-debit from session budget")
    print(f"    3. Session expires after 15 min or $5.00 spent")
    print(f"{'═' * 60}\n")

    return results


if __name__ == "__main__":
    setup_agentcore_payments()

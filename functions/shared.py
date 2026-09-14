# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shared initialization for XRPL Agentic Payments Lambda tool functions.

This module is imported at Lambda init time (outside the handler).
Connections and secrets persist across invocations in the same
execution environment, eliminating per-request overhead.

AWS Well-Architected Agentic AI Lens (AGENTPERF06-BP02):
"For AWS Lambda-based tools, initializing connections outside the
handler function so they persist across invocations within the same
execution environment turns a handshake for each request into one
per environment."
"""

import json
import logging
import os

import boto3
from xrpl.clients import JsonRpcClient
from xrpl.wallet import Wallet

# NO nest_asyncio here, deliberately.
#
# This module used to call nest_asyncio.apply() "so xrpl-py sync wrappers work
# in Lambda". They already do. JsonRpcClient.request() calls asyncio.run(), and
# every handler in this directory is a plain `def lambda_handler` with no async
# anywhere, so there is never an outer event loop for a nested one to sit
# inside — which is the only thing nest_asyncio exists to allow.
#
# On the python3.14 runtime the patch is not merely unnecessary, it breaks every
# tool. nest_asyncio 1.6.0 monkeypatches asyncio's task internals, current_task()
# then returns None inside httpx's AsyncClient.__aexit__, and anyio's
# AsyncShieldCancellation does _task_states[host_task] -> weakref.ref(None):
#
#   TypeError: cannot create weak reference to 'NoneType' object
#
# It fails during teardown, after the XRPL request has already succeeded, so the
# ledger data is fetched and then thrown away as an internal error.
#
# src/mcp_server/server.py is a genuinely async server and keeps its own
# nest_asyncio.apply() — that one has an outer loop to contend with.

# Logging
logger = logging.getLogger("xrpl_agentic_payments")
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────────────────────────
# Secrets (loaded once at init, reused across invocations)
# ─────────────────────────────────────────────────────────────────────────────

_sm = boto3.client("secretsmanager", region_name="us-west-2")
_secret_response = _sm.get_secret_value(SecretId="xrpl-agentic-payments/wallets")
WALLETS: dict = json.loads(_secret_response["SecretString"])

METADATA: dict = WALLETS.get("_metadata", {})
RLUSD_ISSUER: str = METADATA.get("rlusd_issuer", "")
RLUSD_CURRENCY: str = METADATA.get("rlusd_currency", "USD")
RPC_URL: str = METADATA.get("rpc_url", "https://s.altnet.rippletest.net:51234")

# ─────────────────────────────────────────────────────────────────────────────
# XRPL Client (persistent connection, reused across invocations)
# ─────────────────────────────────────────────────────────────────────────────

xrpl_client = JsonRpcClient(RPC_URL)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def get_wallet(name: str) -> Wallet:
    """Load a wallet by name from secrets."""
    if name not in WALLETS:
        raise ValueError(
            f"Wallet '{name}' not found. Available: {[k for k in WALLETS if k != '_metadata']}"
        )
    return Wallet.from_seed(WALLETS[name]["seed"])


def get_wallet_address(name: str) -> str:
    """Get wallet address without loading the full wallet object."""
    if name not in WALLETS:
        raise ValueError(f"Wallet '{name}' not found.")
    return WALLETS[name]["address"]


def strip_tool_prefix(context) -> str:
    """Extract the bare tool name from Gateway context (strips target prefix)."""
    try:
        tool_name = context.client_context.custom.get("bedrockAgentCoreToolName", "")
        delimiter = "___"
        if delimiter in tool_name:
            return tool_name[tool_name.index(delimiter) + len(delimiter):]
        return tool_name
    except Exception:
        return ""

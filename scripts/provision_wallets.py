# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — XRPL Testnet Wallet Provisioning

Creates 6 funded wallets on XRPL Testnet:
- treasury: Holds RLUSD reserves, simulates the enterprise treasury
- execution: Signs and submits cross-border payments (KMS-backed in prod)
- fx_agent: Used by FX Intelligence Agent for DEX queries
- compliance: Used by Compliance Agent for sanctions screening fees
- routing: Used by Routing Agent for path discovery
- monitor: Used by Settlement Monitor to subscribe to tx status

Each wallet is funded with ~1000 XRP from the testnet faucet.

The faucet is rate-limited per IP and rejects back-to-back requests with HTTP
429, so this script is written to survive that: every wallet is written to
config/wallets.json as soon as it is funded, each request is retried with
exponential backoff, and re-running only creates the names that are still
missing. A seed that is generated but not saved is unrecoverable — the wallet
stays funded on the ledger with nobody holding its key — so nothing is kept in
memory across a failure.
"""

import json
import os
import sys
import time
from pathlib import Path

from xrpl.clients import JsonRpcClient
from xrpl.wallet import generate_faucet_wallet


XRPL_TESTNET_RPC = "https://s.altnet.rippletest.net:51234"

WALLET_NAMES = [
    "treasury",
    "execution",
    "fx_agent",
    "compliance",
    "routing",
    "monitor",
]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "config" / "wallets.json"

# Seconds to wait between faucet requests. Two was not enough: the faucet
# answers the first request and 429s the second. Override with
# FAUCET_DELAY_SECONDS if the limit changes.
FAUCET_DELAY_SECONDS = float(os.environ.get("FAUCET_DELAY_SECONDS", "10"))

# Attempts per wallet, with the delay doubling each time.
FAUCET_ATTEMPTS = int(os.environ.get("FAUCET_ATTEMPTS", "5"))


def _load_existing() -> dict:
    """Wallets already provisioned, so a re-run resumes instead of restarting."""
    if not OUTPUT_PATH.exists():
        return {}
    try:
        with open(OUTPUT_PATH) as f:
            existing = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ⚠️  {OUTPUT_PATH} exists but could not be read ({e}).")
        print("      Move it aside and re-run to start clean.")
        sys.exit(1)
    return existing if isinstance(existing, dict) else {}


def _save(wallets: dict):
    """Persist after every wallet. Written 0600: this file holds private seeds."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(wallets, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, OUTPUT_PATH)


def _create_one(client: JsonRpcClient, name: str) -> dict:
    """Fund one wallet, retrying the rate-limited faucet with backoff."""
    delay = FAUCET_DELAY_SECONDS
    for attempt in range(1, FAUCET_ATTEMPTS + 1):
        try:
            wallet = generate_faucet_wallet(client, debug=False)
            return {
                "address": wallet.address,
                "seed": wallet.seed,
                "public_key": wallet.public_key,
                "classic_address": wallet.address,
            }
        except Exception as e:
            if attempt == FAUCET_ATTEMPTS:
                raise
            print(f"\n      attempt {attempt}/{FAUCET_ATTEMPTS} failed ({e})")
            print(f"      retrying in {delay:.0f}s...", end=" ", flush=True)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def provision_wallets() -> dict:
    """Generate funded testnet wallets for all XRPL Agentic Payments agents."""
    client = JsonRpcClient(XRPL_TESTNET_RPC)
    wallets = _load_existing()

    print("═" * 60)
    print("  XRPL Agentic Payments — XRPL Testnet Wallet Provisioning")
    print("═" * 60)
    print(f"\n  Network: {XRPL_TESTNET_RPC}")

    missing = [name for name in WALLET_NAMES if name not in wallets]
    if wallets:
        print(f"  Already provisioned: {len(wallets)} ({', '.join(wallets)})")
    if not missing:
        print("  Nothing to do — all wallets are present.\n")
        return wallets
    print(f"  Wallets to create: {len(missing)}\n")

    for i, name in enumerate(missing):
        print(f"  Creating '{name}' wallet...", end=" ", flush=True)
        try:
            wallets[name] = _create_one(client, name)
        except Exception as e:
            print(f"❌  Failed: {e}")
            # Whatever succeeded is already on disk, so re-running picks up
            # from here rather than orphaning funded wallets.
            print(f"\n  {len(wallets)}/{len(WALLET_NAMES)} wallets saved to {OUTPUT_PATH}.")
            print("  Re-run this script to create the rest (it resumes).")
            print("  If the faucet is rate-limiting, wait a minute or raise")
            print("  FAUCET_DELAY_SECONDS (currently "
                  f"{FAUCET_DELAY_SECONDS:.0f}s between requests).")
            sys.exit(1)

        # Save immediately: an unsaved seed is a wallet nobody can spend.
        _save(wallets)
        print(f"✅  {wallets[name]['address']}")

        if i < len(missing) - 1:
            time.sleep(FAUCET_DELAY_SECONDS)

    print(f"\n{'═' * 60}")
    print(f"  ✅ All {len(wallets)} wallets created and funded")
    print(f"  📁 Saved to: {OUTPUT_PATH}")
    print(f"{'═' * 60}\n")

    # Print summary table
    print(f"  {'Wallet':<12} {'Address':<36} {'Purpose'}")
    print(f"  {'─' * 12} {'─' * 36} {'─' * 30}")
    purposes = {
        "treasury": "RLUSD reserves / enterprise treasury",
        "execution": "Signs + submits XRPL payments",
        "fx_agent": "DEX order book queries",
        "compliance": "Sanctions screening fees",
        "routing": "Path discovery + optimization",
        "monitor": "Tx status subscriptions",
    }
    for name, data in wallets.items():
        print(f"  {name:<12} {data['address']:<36} {purposes.get(name, '')}")

    return wallets


if __name__ == "__main__":
    provision_wallets()

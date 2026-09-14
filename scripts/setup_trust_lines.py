# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — RLUSD Trust Line Setup (Testnet)

On XRPL testnet, there is no official RLUSD issuer. This script:
1. Uses the treasury wallet as the RLUSD issuer (simulates Ripple's issuer)
2. Creates trust lines from all agent wallets TO the treasury for RLUSD
3. Issues RLUSD from treasury to the execution wallet (for sending payments)

Trust line model:
  treasury (issuer) ──RLUSD──> execution (holds 10,000 RLUSD for payments)
  treasury (issuer) ──RLUSD──> fx_agent (holds small balance for fees)
  treasury (issuer) ──RLUSD──> compliance (holds small balance for fees)
  treasury (issuer) ──RLUSD──> routing (holds small balance for fees)
  treasury (issuer) ──RLUSD──> monitor (holds small balance for fees)

After running this script, the execution wallet can send RLUSD payments to any
destination that also has a trust line to treasury for RLUSD.
"""

import json
import sys
import time
from pathlib import Path

from xrpl.clients import JsonRpcClient
from xrpl.models import (
    AccountSet,
    AccountSetAsfFlag,
    IssuedCurrencyAmount,
    Payment,
    TrustSet,
)
from xrpl.transaction import submit_and_wait
from xrpl.wallet import Wallet


XRPL_TESTNET_RPC = "https://s.altnet.rippletest.net:51234"
WALLETS_FILE = Path(__file__).resolve().parent.parent / "config" / "wallets.json"
CURRENCY_CODE = "USD"  # 3-char code for RLUSD on testnet (XRPL uses 3-char or 40-hex)

# How much RLUSD to issue to each wallet
ISSUANCE = {
    "execution": "10000",  # Main payment wallet
    "fx_agent": "100",     # Small balance for potential fees
    "compliance": "100",   # Small balance for potential fees
    "routing": "100",      # Small balance for potential fees
    "monitor": "100",      # Small balance for potential fees
}


def load_wallets() -> dict:
    """Load wallet credentials from config."""
    if not WALLETS_FILE.exists():
        print(f"  ❌ Wallet file not found: {WALLETS_FILE}")
        print("  Run scripts/provision_wallets.py first.")
        sys.exit(1)

    with open(WALLETS_FILE) as f:
        return json.load(f)


def setup_issuer(client: JsonRpcClient, treasury_wallet: Wallet) -> None:
    """Enable Default Ripple on the treasury wallet (required for issuers)."""
    print("  Setting DefaultRipple flag on treasury (issuer)...", end=" ", flush=True)
    tx = AccountSet(
        account=treasury_wallet.address,
        set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
    )
    response = submit_and_wait(tx, client, treasury_wallet)
    result = response.result.get("meta", {}).get("TransactionResult", "unknown")
    if result == "tesSUCCESS":
        print("✅")
    else:
        print(f"❌ {result}")
        sys.exit(1)


def create_trust_line(
    client: JsonRpcClient,
    wallet: Wallet,
    issuer_address: str,
    limit: str = "1000000",
) -> str:
    """Create a trust line from wallet to issuer for RLUSD."""
    tx = TrustSet(
        account=wallet.address,
        limit_amount=IssuedCurrencyAmount(
            currency=CURRENCY_CODE,
            issuer=issuer_address,
            value=limit,
        ),
    )
    response = submit_and_wait(tx, client, wallet)
    return response.result.get("meta", {}).get("TransactionResult", "unknown")


def issue_rlusd(
    client: JsonRpcClient,
    issuer_wallet: Wallet,
    destination: str,
    amount: str,
) -> str:
    """Issue RLUSD from treasury to a destination wallet."""
    tx = Payment(
        account=issuer_wallet.address,
        destination=destination,
        amount=IssuedCurrencyAmount(
            currency=CURRENCY_CODE,
            issuer=issuer_wallet.address,
            value=amount,
        ),
    )
    response = submit_and_wait(tx, client, issuer_wallet)
    return response.result.get("meta", {}).get("TransactionResult", "unknown")


def setup_trust_lines() -> None:
    """Main flow: set up issuer, trust lines, and issue RLUSD."""
    wallets_data = load_wallets()
    client = JsonRpcClient(XRPL_TESTNET_RPC)

    # Reconstruct Wallet objects from seeds. Keys beginning with "_" are config
    # blocks, not wallets (this script writes "_metadata" below), so skip them —
    # same convention as functions/shared.py. Without this, re-running the script
    # dies with KeyError: 'seed' on _metadata before doing any work.
    wallets = {}
    for name, data in wallets_data.items():
        if name.startswith("_"):
            continue
        wallets[name] = Wallet.from_seed(data["seed"])

    treasury = wallets["treasury"]
    issuer_address = treasury.address

    print("═" * 60)
    print("  XRPL Agentic Payments — RLUSD Trust Line Setup (Testnet)")
    print("═" * 60)
    print(f"\n  RLUSD Issuer (treasury): {issuer_address}")
    print(f"  Currency code: {CURRENCY_CODE}")
    print()

    # Step 1: Enable DefaultRipple on treasury
    setup_issuer(client, treasury)
    time.sleep(1)

    # Step 2: Create trust lines from each agent wallet to treasury
    print("\n  Creating trust lines:")
    for name in ISSUANCE.keys():
        wallet = wallets[name]
        print(f"    {name:<12} → treasury ...", end=" ", flush=True)
        result = create_trust_line(client, wallet, issuer_address)
        if result == "tesSUCCESS":
            print("✅")
        else:
            print(f"❌ {result}")
            sys.exit(1)
        time.sleep(1)

    # Step 3: Issue RLUSD from treasury to each wallet
    print("\n  Issuing RLUSD:")
    for name, amount in ISSUANCE.items():
        wallet = wallets[name]
        print(f"    treasury → {name:<12} ({amount} {CURRENCY_CODE}) ...", end=" ", flush=True)
        result = issue_rlusd(client, treasury, wallet.address, amount)
        if result == "tesSUCCESS":
            print("✅")
        else:
            print(f"❌ {result}")
            sys.exit(1)
        time.sleep(1)

    # Step 4: Create a "destination" wallet for end-to-end testing
    print("\n  Creating destination wallet for E2E tests...")
    from xrpl.wallet import generate_faucet_wallet

    dest_wallet = generate_faucet_wallet(client, debug=False)
    print(f"    Destination: {dest_wallet.address}")

    # Trust line on destination
    print(f"    Creating trust line on destination...", end=" ", flush=True)
    result = create_trust_line(client, dest_wallet, issuer_address)
    if result == "tesSUCCESS":
        print("✅")
    else:
        print(f"❌ {result}")

    # Save destination wallet and issuer info
    wallets_data["destination"] = {
        "address": dest_wallet.address,
        "seed": dest_wallet.seed,
        "public_key": dest_wallet.public_key,
        "classic_address": dest_wallet.address,
    }
    wallets_data["_metadata"] = {
        "rlusd_issuer": issuer_address,
        "rlusd_currency": CURRENCY_CODE,
        "network": "testnet",
        "rpc_url": XRPL_TESTNET_RPC,
    }

    with open(WALLETS_FILE, "w") as f:
        json.dump(wallets_data, f, indent=2)

    print(f"\n{'═' * 60}")
    print(f"  ✅ Trust lines created and RLUSD issued")
    print(f"  📁 Updated: {WALLETS_FILE}")
    print(f"\n  RLUSD Balances:")
    for name, amount in ISSUANCE.items():
        print(f"    {name:<12}: {amount} {CURRENCY_CODE}")
    print(f"    {'destination':<12}: 0 {CURRENCY_CODE} (ready to receive)")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    setup_trust_lines()

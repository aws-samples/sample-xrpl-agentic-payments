#!/usr/bin/env python3
"""Provision isolated XRPL Testnet wallets, trust lines, balances, and liquidity."""

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
from xrpl.clients import JsonRpcClient
from xrpl.models import IssuedCurrencyAmount
from xrpl.models.transactions import AccountSet, OfferCreate, Payment, TrustSet
from xrpl.models.transactions.account_set import AccountSetAsfFlag
from xrpl.transaction import submit_and_wait
from xrpl.wallet import Wallet, generate_faucet_wallet

DEFAULT_RPC = "https://s.altnet.rippletest.net:51234"
WALLET_NAMES = (
    "usd_issuer",
    "mxn_issuer",
    "execution",
    "recipient",
    "payout",
    "liquidity",
    "fee_payer",
    "fee_merchant",
)
SECRET_NAMES = {
    "execution": "xrpl-agentcore/testnet/execution-seed",
    "fee_payer": "xrpl-agentcore/testnet/fee-payer-seed",
    "fee_merchant": "xrpl-agentcore/testnet/fee-merchant-seed",
}


def write_private_json(path: Path, value: dict[str, Any]) -> None:
    """Write signing material with mode 0600 from the moment the file exists."""

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            descriptor = -1
            json.dump(value, handle, indent=2)
            handle.write("\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_fixture_state(path: Path, rpc_url: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "network": "testnet",
            "rpc_url": rpc_url,
            "wallets": {},
            "completed_steps": {},
            "fixture_transaction_hashes": [],
        }
    state = json.loads(path.read_text())
    if state.get("network") != "testnet":
        raise ValueError("refusing to resume a non-Testnet fixture file")
    if not isinstance(state.get("wallets"), dict):
        raise ValueError("fixture wallet checkpoint is invalid")
    if not isinstance(state.get("completed_steps", {}), dict):
        raise ValueError("fixture step checkpoint is invalid")
    state["rpc_url"] = rpc_url
    state.setdefault("completed_steps", {})
    state.setdefault("fixture_transaction_hashes", [])
    return state


def issued(currency: str, issuer: str, value: str) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(currency=currency, issuer=issuer, value=value)


def submit(client: JsonRpcClient, wallet: Wallet, transaction: Any) -> str:
    response = submit_and_wait(transaction, client, wallet)
    result = dict(response.result)
    meta = result.get("meta") or {}
    engine_result = meta.get("TransactionResult")
    if result.get("validated") is not True or engine_result != "tesSUCCESS":
        raise RuntimeError(
            f"fixture transaction failed validation: {engine_result or response.status}"
        )
    return str(result.get("hash") or result.get("tx_json", {}).get("hash") or "")


def establish_trust(
    client: JsonRpcClient,
    wallet: Wallet,
    currency: str,
    issuer: Wallet,
    limit: str,
) -> str:
    return submit(
        client,
        wallet,
        TrustSet(
            account=wallet.address,
            limit_amount=issued(currency, issuer.address, limit),
        ),
    )


def issue(
    client: JsonRpcClient,
    issuer: Wallet,
    destination: Wallet,
    currency: str,
    value: str,
) -> str:
    return submit(
        client,
        issuer,
        Payment(
            account=issuer.address,
            destination=destination.address,
            amount=issued(currency, issuer.address, value),
        ),
    )


def secret_upsert(region: str, name: str, wallet: Wallet) -> None:
    client = boto3.client("secretsmanager", region_name=region)
    payload = json.dumps({"seed": wallet.seed, "address": wallet.address, "network": "testnet"})
    try:
        client.create_secret(Name=name, SecretString=payload)
    except client.exceptions.ResourceExistsException:
        client.put_secret_value(SecretId=name, SecretString=payload)


def provision(rpc_url: str, output: Path) -> tuple[dict[str, Wallet], list[str]]:
    if "altnet.rippletest.net" not in rpc_url and "testnet" not in rpc_url:
        raise ValueError("refusing to provision a non-Testnet XRPL endpoint")
    client = JsonRpcClient(rpc_url)
    state = load_fixture_state(output, rpc_url)
    wallet_records = state["wallets"]
    wallets: dict[str, Wallet] = {}
    for name in WALLET_NAMES:
        existing = wallet_records.get(name)
        if existing:
            wallet = Wallet.from_seed(existing["seed"])
            if wallet.address != existing.get("address"):
                raise ValueError(f"fixture wallet checkpoint for {name} has an address mismatch")
        else:
            wallet = generate_faucet_wallet(
                client,
                debug=False,
                usage_context="agentcore-poc",
            )
            wallet_records[name] = {"address": wallet.address, "seed": wallet.seed}
            write_private_json(output, state)
        wallets[name] = wallet

    completed_steps: dict[str, str] = state["completed_steps"]

    def run_step(step: str, action: Any) -> None:
        if step in completed_steps:
            return
        completed_steps[step] = action()
        state["fixture_transaction_hashes"] = list(completed_steps.values())
        write_private_json(output, state)

    for issuer_name in ("usd_issuer", "mxn_issuer"):
        issuer = wallets[issuer_name]
        run_step(
            f"enable_default_ripple_{issuer_name}",
            lambda issuer=issuer: submit(
                client,
                issuer,
                AccountSet(
                    account=issuer.address,
                    set_flag=AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
                ),
            ),
        )

    run_step(
        "trust_execution_usd",
        lambda: establish_trust(
            client,
            wallets["execution"],
            "USD",
            wallets["usd_issuer"],
            "50000",
        ),
    )
    run_step(
        "trust_liquidity_usd",
        lambda: establish_trust(
            client,
            wallets["liquidity"],
            "USD",
            wallets["usd_issuer"],
            "50000",
        ),
    )
    run_step(
        "trust_liquidity_mxn",
        lambda: establish_trust(
            client,
            wallets["liquidity"],
            "MXN",
            wallets["mxn_issuer"],
            "1000000",
        ),
    )
    for destination in ("recipient", "payout"):
        run_step(
            f"trust_{destination}_mxn",
            lambda destination=destination: establish_trust(
                client,
                wallets[destination],
                "MXN",
                wallets["mxn_issuer"],
                "1000000",
            ),
        )

    run_step(
        "issue_execution_usd",
        lambda: issue(
            client,
            wallets["usd_issuer"],
            wallets["execution"],
            "USD",
            "20000",
        ),
    )
    run_step(
        "issue_liquidity_mxn",
        lambda: issue(
            client,
            wallets["mxn_issuer"],
            wallets["liquidity"],
            "MXN",
            "500000",
        ),
    )
    run_step(
        "create_usd_mxn_offer",
        lambda: submit(
            client,
            wallets["liquidity"],
            OfferCreate(
                account=wallets["liquidity"].address,
                taker_pays=issued(
                    "USD",
                    wallets["usd_issuer"].address,
                    "20000",
                ),
                taker_gets=issued(
                    "MXN",
                    wallets["mxn_issuer"].address,
                    "345000",
                ),
            ),
        ),
    )
    return wallets, list(completed_steps.values())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create disposable wallets and USD/MXN liquidity on XRPL Testnet."
    )
    parser.add_argument("--rpc-url", default=DEFAULT_RPC)
    parser.add_argument("--region", default=None)
    parser.add_argument("--output", type=Path, default=Path(".testnet-fixtures.json"))
    parser.add_argument(
        "--write-secrets",
        action="store_true",
        help="Create/update the three signer Secrets Manager entries.",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    wallets, _transaction_hashes = provision(args.rpc_url, args.output)

    corridor = [
        {
            "id": "usd-mxn-testnet",
            "label": "USD to MXN Testnet demo",
            "source_asset": {
                "currency": "USD",
                "issuer": wallets["usd_issuer"].address,
            },
            "destination_asset": {
                "currency": "MXN",
                "issuer": wallets["mxn_issuer"].address,
            },
            "fixture_rate": "17.25",
            "enabled": True,
        }
    ]
    corridor_path = Path("config/corridors.json")
    corridor_path.parent.mkdir(parents=True, exist_ok=True)
    corridor_path.write_text(json.dumps(corridor, indent=2) + "\n")

    if args.write_secrets:
        # AWS_DEFAULT_REGION has exactly one source of truth: .env (see
        # .env.example). No hardcoded default here.
        region = (
            args.region
            or os.environ.get("AWS_DEFAULT_REGION")
            or read_env_value(args.env_file, "AWS_DEFAULT_REGION")
        )
        if not region:
            raise SystemExit(
                f"AWS_DEFAULT_REGION is required with --write-secrets. Set it in "
                f"{args.env_file} (see .env.example) or pass --region."
            )
        for wallet_name, secret_name in SECRET_NAMES.items():
            secret_upsert(region, secret_name, wallets[wallet_name])

    public = {name: wallet.address for name, wallet in wallets.items()}
    print(json.dumps({"wallets": public, "secrets_written": args.write_secrets}, indent=2))
    print(f"Sensitive fixture material written to {args.output} with mode 0600.")


if __name__ == "__main__":
    main()

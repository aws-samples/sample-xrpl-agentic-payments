#!/usr/bin/env python3
"""Verify the provisioned XRPL Testnet fixtures, then record their addresses in .env.

Only public addresses are written. Seeds stay in .testnet-fixtures.json and
Secrets Manager.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from xrpl.clients import JsonRpcClient
from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.models.requests import AccountInfo, AccountLines, AccountOffers, ServerInfo

DEFAULT_RPC = "https://s.altnet.rippletest.net:51234"
LSF_DEFAULT_RIPPLE = 0x00800000

# .env key -> fixture wallet. The first six are the deploy.sh stack parameters.
ENV_ADDRESSES = {
    "XRPL_EXECUTION_ADDRESS": "execution",
    "XRPL_PAYOUT_ADDRESS": "payout",
    "XRPL_FEE_PAYER_ADDRESS": "fee_payer",
    "XRPL_FEE_MERCHANT_ADDRESS": "fee_merchant",
    "XRPL_USD_ISSUER_ADDRESS": "usd_issuer",
    "XRPL_MXN_ISSUER_ADDRESS": "mxn_issuer",
    "NEXT_PUBLIC_DEMO_RECIPIENT_ADDRESS": "recipient",
}


def request(client: JsonRpcClient, value: Any) -> dict[str, Any]:
    response = client.request(value)
    if not response.is_successful():
        raise RuntimeError(f"XRPL fixture verification request failed: {response.status}")
    return dict(response.result)


def field(value: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return None


def account_data(client: JsonRpcClient, address: str) -> dict[str, Any]:
    result = request(client, AccountInfo(account=address, ledger_index="validated"))
    data = field(result, "account_data", "accountData")
    if not isinstance(data, dict):
        raise RuntimeError(f"missing validated AccountRoot for {address}")
    return data


def account_lines(client: JsonRpcClient, address: str) -> list[dict[str, Any]]:
    result = request(client, AccountLines(account=address, ledger_index="validated"))
    lines = result.get("lines")
    if not isinstance(lines, list):
        raise RuntimeError(f"missing validated trust lines for {address}")
    return [line for line in lines if isinstance(line, dict)]


def line_balance(
    client: JsonRpcClient,
    *,
    holder: str,
    issuer: str,
    currency: str,
) -> Decimal:
    for line in account_lines(client, holder):
        if line.get("account") == issuer and line.get("currency") == currency:
            return Decimal(str(line["balance"]))
    raise RuntimeError(f"{holder} has no {currency} trust line to {issuer}")


def issued_amount_matches(
    value: Any,
    *,
    currency: str,
    issuer: str,
) -> bool:
    return (
        isinstance(value, dict)
        and field(value, "currency", "Currency") == currency
        and field(value, "issuer", "Issuer") == issuer
        and Decimal(str(field(value, "value", "Value"))) > 0
    )


def verify(rpc_url: str, fixture_path: Path) -> dict[str, Any]:
    if "altnet.rippletest.net" not in rpc_url and "testnet" not in rpc_url:
        raise ValueError("refusing to verify a non-Testnet XRPL endpoint")
    fixture = json.loads(fixture_path.read_text())
    if fixture.get("network") != "testnet":
        raise ValueError("fixture checkpoint is not marked as Testnet")
    wallets = fixture.get("wallets")
    if not isinstance(wallets, dict):
        raise ValueError("fixture checkpoint has no wallets")
    addresses = {
        name: str(record["address"])
        for name, record in wallets.items()
        if isinstance(record, dict) and record.get("address")
    }
    required = {
        "usd_issuer",
        "mxn_issuer",
        "execution",
        "recipient",
        "payout",
        "liquidity",
        "fee_payer",
        "fee_merchant",
    }
    if set(addresses) != required:
        raise ValueError("fixture checkpoint does not contain exactly the required wallets")

    client = JsonRpcClient(rpc_url)
    server = request(client, ServerInfo())
    info = server.get("info")
    if not isinstance(info, dict) or info.get("network_id") != 1:
        raise RuntimeError("XRPL endpoint did not prove Testnet network ID 1")

    for issuer_name in ("usd_issuer", "mxn_issuer"):
        flags = int(field(account_data(client, addresses[issuer_name]), "Flags", "flags") or 0)
        if flags & LSF_DEFAULT_RIPPLE == 0:
            raise RuntimeError(f"{issuer_name} does not have Default Ripple enabled")

    balances = {
        "execution_usd": line_balance(
            client,
            holder=addresses["execution"],
            issuer=addresses["usd_issuer"],
            currency="USD",
        ),
        "liquidity_usd": line_balance(
            client,
            holder=addresses["liquidity"],
            issuer=addresses["usd_issuer"],
            currency="USD",
        ),
        "liquidity_mxn": line_balance(
            client,
            holder=addresses["liquidity"],
            issuer=addresses["mxn_issuer"],
            currency="MXN",
        ),
        "recipient_mxn": line_balance(
            client,
            holder=addresses["recipient"],
            issuer=addresses["mxn_issuer"],
            currency="MXN",
        ),
        "payout_mxn": line_balance(
            client,
            holder=addresses["payout"],
            issuer=addresses["mxn_issuer"],
            currency="MXN",
        ),
    }
    if balances["execution_usd"] <= 0:
        raise RuntimeError("execution wallet has no spendable issued USD balance")
    if balances["liquidity_mxn"] <= 0:
        raise RuntimeError("liquidity wallet has no spendable issued MXN balance")

    offer_result = request(
        client,
        AccountOffers(account=addresses["liquidity"], ledger_index="validated"),
    )
    offers = offer_result.get("offers")
    if not isinstance(offers, list):
        raise RuntimeError("liquidity wallet offer response is invalid")
    matching_offers = [
        offer
        for offer in offers
        if isinstance(offer, dict)
        and issued_amount_matches(
            field(offer, "taker_pays", "TakerPays"),
            currency="USD",
            issuer=addresses["usd_issuer"],
        )
        and issued_amount_matches(
            field(offer, "taker_gets", "TakerGets"),
            currency="MXN",
            issuer=addresses["mxn_issuer"],
        )
    ]
    if not matching_offers:
        raise RuntimeError("no validated USD/MXN liquidity offer was found")

    for fee_wallet in ("fee_payer", "fee_merchant"):
        drops = Decimal(
            str(field(account_data(client, addresses[fee_wallet]), "Balance", "balance") or "0")
        )
        if drops <= 0:
            raise RuntimeError(f"{fee_wallet} has no Testnet XRP")

    return {
        "network_id": 1,
        "wallet_count": len(addresses),
        "default_ripple_issuers": 2,
        "trust_line_count": len(balances),
        "execution_usd": format(balances["execution_usd"], "f"),
        "liquidity_mxn": format(balances["liquidity_mxn"], "f"),
        "matching_offer_count": len(matching_offers),
        "funded_fee_wallets": 2,
    }


def fixture_env_values(fixture_path: Path) -> dict[str, str]:
    wallets = json.loads(fixture_path.read_text())["wallets"]
    values = {key: str(wallets[name]["address"]) for key, name in ENV_ADDRESSES.items()}
    for key, address in values.items():
        if not is_valid_classic_address(address):
            raise ValueError(f"{key} is not a classic XRPL address")
    return values


def upsert_env(env_path: Path, values: dict[str, str], template: Path | None = None) -> None:
    """Set each key in place, keep every other line, and append keys not yet present."""

    if env_path.exists():
        lines = env_path.read_text().splitlines()
    elif template is not None and template.exists():
        lines = template.read_text().splitlines()
    else:
        lines = []
    pending = dict(values)
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            lines[index] = f"{key}={pending.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in pending.items())

    # .env may hold other local credentials, so write it atomically and owner-only.
    handle, temporary = tempfile.mkstemp(dir=env_path.parent, prefix=".env.")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, env_path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpc-url", default=DEFAULT_RPC)
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path(".testnet-fixtures.json"),
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Check the ledger state without writing the env file.",
    )
    args = parser.parse_args()
    report = verify(args.rpc_url, args.fixtures)
    if not args.verify_only:
        values = fixture_env_values(args.fixtures)
        upsert_env(args.env_file, values, template=Path(".env.example"))
        report["env_file"] = str(args.env_file)
        report["env_keys_written"] = sorted(values)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

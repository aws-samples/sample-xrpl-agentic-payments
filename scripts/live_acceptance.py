#!/usr/bin/env python3
"""Exercise idempotent direct-wallet and simulated-fiat REST acceptance lanes."""

from __future__ import annotations

import argparse
import os
import time
import uuid
from typing import Any

import httpx

TERMINAL = {"COMPLETED", "EXPIRED", "FAILED", "FAILED_REFUNDED"}


def request(
    client: httpx.Client,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    response = client.request(method, path, json=payload)
    response.raise_for_status()
    return response.json()


def run_lane(
    client: httpx.Client,
    *,
    payout_mode: str,
    recipient_address: str | None,
    amount: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    quote = request(
        client,
        "POST",
        "/v1/quotes",
        {
            "corridor_id": "usd-mxn-testnet",
            "destination_amount": amount,
            "payout_mode": payout_mode,
            "recipient_address": recipient_address,
            "recipient_name": "Acceptance Fixture",
            "recipient_country": "MX",
            "payout_alias": (
                "fixture-bank-token" if payout_mode == "LOCAL_FIAT_SIMULATED" else None
            ),
            "slippage_bps": 100,
        },
    )
    transfer = request(
        client,
        "POST",
        "/v1/transfers",
        {"quote_id": quote["quote_id"]},
    )
    approval = {
        "approval_hash": transfer["approval_hash"],
        "idempotency_key": f"acceptance-{uuid.uuid4()}",
    }
    request(
        client,
        "POST",
        f"/v1/transfers/{transfer['transfer_id']}/approve",
        approval,
    )
    # Deliberate replay proves the approval/outbox boundary is idempotent.
    request(
        client,
        "POST",
        f"/v1/transfers/{transfer['transfer_id']}/approve",
        approval,
    )

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        transfer = request(
            client,
            "GET",
            f"/v1/transfers/{transfer['transfer_id']}",
        )
        if transfer["status"] in TERMINAL:
            break
        time.sleep(3)
    if transfer["status"] != "COMPLETED":
        raise RuntimeError(
            f"{payout_mode} lane ended in {transfer['status']}: {transfer.get('failure_code')}"
        )
    if not transfer.get("fee_tx_hash") or not transfer.get("payment_tx_hash"):
        raise RuntimeError("completed transfer is missing validated transaction hashes")
    if payout_mode == "LOCAL_FIAT_SIMULATED" and not transfer.get("payout_reference"):
        raise RuntimeError("simulated fiat lane is missing its payout reference")
    return transfer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument(
        "--access-token",
        help=(
            "Cognito access token; prefer COGNITO_ACCESS_TOKEN to keep it out of process arguments."
        ),
    )
    parser.add_argument("--recipient-address", required=True)
    parser.add_argument("--amount", default="100")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    access_token = args.access_token or os.environ.get("COGNITO_ACCESS_TOKEN")
    if not access_token:
        parser.error("--access-token or COGNITO_ACCESS_TOKEN is required")
    with httpx.Client(
        base_url=args.api_url.rstrip("/"),
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    ) as client:
        direct = run_lane(
            client,
            payout_mode="XRPL_WALLET",
            recipient_address=args.recipient_address,
            amount=args.amount,
            timeout_seconds=args.timeout_seconds,
        )
        simulated = run_lane(
            client,
            payout_mode="LOCAL_FIAT_SIMULATED",
            recipient_address=None,
            amount=args.amount,
            timeout_seconds=args.timeout_seconds,
        )
    print(
        {
            "direct_transfer": direct["transfer_id"],
            "direct_hash": direct["payment_tx_hash"],
            "simulated_transfer": simulated["transfer_id"],
            "simulated_hash": simulated["payment_tx_hash"],
            "payout_reference": simulated["payout_reference"],
        }
    )


if __name__ == "__main__":
    main()

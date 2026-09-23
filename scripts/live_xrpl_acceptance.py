#!/usr/bin/env python3
"""Run both transfer lanes against XRPL Testnet without requiring AWS deployment."""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from xrpl.wallet import Wallet

from xrpl_agentcore.domain import PayoutMode, TransferStatus
from xrpl_agentcore.execution import (
    ExecutionServices,
    complete_payout,
    mark_fee_pending,
    reconcile_fee,
    reconcile_payment,
    submit_fee,
    submit_payment,
)
from xrpl_agentcore.repository import (
    InMemoryExecutionArtifactRepository,
    InMemoryTransferRepository,
)
from xrpl_agentcore.services import (
    CorridorService,
    FixturePathQuoteProvider,
    QuoteRequest,
    QuoteService,
    TransferService,
)
from xrpl_agentcore.xrpl_payments import JsonRpcXrplClient, XrplTransactionEngine

DEFAULT_RPC = "https://s.altnet.rippletest.net:51234"


def load_wallets(path: Path) -> dict[str, Wallet]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError("fixture seed file must not be readable by group or other users")
    value = json.loads(path.read_text())
    if value.get("network") != "testnet":
        raise ValueError("fixture seed file is not marked as Testnet")
    records = value.get("wallets")
    if not isinstance(records, dict):
        raise ValueError("fixture seed file has no wallets")
    return {
        name: Wallet.from_seed(record["seed"])
        for name, record in records.items()
        if isinstance(record, dict) and isinstance(record.get("seed"), str)
    }


def await_result(
    operation: Callable[[], dict[str, Any]],
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = operation()
        if result.get("done") is True:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError("XRPL acceptance reconciliation timed out")
        time.sleep(4)


def run_lane(
    *,
    services: ExecutionServices,
    quote_service: QuoteService,
    transfer_service: TransferService,
    payout_mode: PayoutMode,
    recipient_address: str | None,
    destination_amount: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    owner = f"local-live-{payout_mode.value.lower()}"
    quote = quote_service.create(
        owner,
        QuoteRequest(
            corridor_id="usd-mxn-testnet",
            destination_amount=destination_amount,
            payout_mode=payout_mode,
            recipient_address=recipient_address,
            recipient_name="Live Testnet Acceptance",
            recipient_country="MX",
            payout_alias=(
                "simulated-payout-token" if payout_mode == PayoutMode.LOCAL_FIAT_SIMULATED else None
            ),
            slippage_bps=100,
        ),
    )
    transfer = transfer_service.create(owner, quote.quote_id)
    transfer = services.transfers.approve(
        transfer.transfer_id,
        owner,
        transfer.approval_hash,
        f"local-live-{uuid.uuid4()}",
    )
    mark_fee_pending(services, transfer.transfer_id, transfer.approval_hash)
    submit_fee(services, transfer.transfer_id)
    fee = await_result(
        lambda: reconcile_fee(services, transfer.transfer_id),
        timeout_seconds=timeout_seconds,
    )
    if fee.get("status") != TransferStatus.FEE_PAID:
        raise RuntimeError(f"x402 fee did not settle: {fee}")

    payment = submit_payment(services, transfer.transfer_id)
    if payment.get("refund"):
        raise RuntimeError(f"cross-currency payment could not be submitted: {payment}")
    settlement = await_result(
        lambda: reconcile_payment(services, transfer.transfer_id),
        timeout_seconds=timeout_seconds,
    )
    if settlement.get("status") != TransferStatus.SETTLED:
        raise RuntimeError(f"cross-currency payment did not settle: {settlement}")
    complete_payout(services, transfer.transfer_id)
    completed = services.transfers.get_transfer(transfer.transfer_id, owner)
    if completed.status != TransferStatus.COMPLETED:
        raise RuntimeError(f"transfer did not complete: {completed.status}")
    return {
        "transfer_id": completed.transfer_id,
        "status": completed.status,
        "fee_tx_hash": completed.fee_tx_hash,
        "payment_tx_hash": completed.payment_tx_hash,
        "ledger_index": completed.ledger_index,
        "explorer_url": completed.explorer_url,
        "payout_reference": completed.payout_reference,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path(".testnet-fixtures.json"),
    )
    parser.add_argument("--rpc-url", default=DEFAULT_RPC)
    parser.add_argument("--amount", default="10")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    if "altnet.rippletest.net" not in args.rpc_url and "testnet" not in args.rpc_url:
        raise ValueError("refusing to run live acceptance against a non-Testnet endpoint")

    wallets = load_wallets(args.fixtures)
    os.environ["CORRIDOR_CONFIG_PATH"] = "config/corridors.json"
    repository = InMemoryTransferRepository()
    artifacts = InMemoryExecutionArtifactRepository()
    client = JsonRpcXrplClient(args.rpc_url)
    quote_service = QuoteService(
        repository,
        CorridorService.from_environment(),
        FixturePathQuoteProvider(),
        source_account=wallets["execution"].address,
        payout_account=wallets["payout"].address,
        fee_merchant=wallets["fee_merchant"].address,
    )
    transfer_service = TransferService(repository)
    services = ExecutionServices(
        repository,
        artifacts,
        XrplTransactionEngine(client, wallets["execution"], artifacts),
        XrplTransactionEngine(client, wallets["fee_payer"], artifacts),
        XrplTransactionEngine(client, wallets["fee_merchant"], artifacts),
    )

    direct = run_lane(
        services=services,
        quote_service=quote_service,
        transfer_service=transfer_service,
        payout_mode=PayoutMode.XRPL_WALLET,
        recipient_address=wallets["recipient"].address,
        destination_amount=args.amount,
        timeout_seconds=args.timeout_seconds,
    )
    simulated = run_lane(
        services=services,
        quote_service=quote_service,
        transfer_service=transfer_service,
        payout_mode=PayoutMode.LOCAL_FIAT_SIMULATED,
        recipient_address=None,
        destination_amount=args.amount,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps({"direct": direct, "simulated_fiat": simulated}, indent=2))


if __name__ == "__main__":
    main()

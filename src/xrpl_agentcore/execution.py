"""Deterministic Step Functions task handlers for approved transfers."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from functools import lru_cache, wraps
from typing import Any

import boto3

from .domain import PayoutMode, TransferStatus, utc_now
from .repository import (
    DynamoExecutionArtifactRepository,
    DynamoTransferRepository,
    ExecutionArtifactRepository,
    TransferRepository,
)
from .x402 import XrplX402Adapter
from .xrpl_payments import (
    PreparedTransaction,
    SettlementMismatch,
    TransactionRejected,
    WalletIdentity,
    XrplError,
    XrplTransactionEngine,
    configured_client,
    wallet_from_secret,
)


class ExecutionServices:
    def __init__(
        self,
        transfers: TransferRepository,
        artifacts: ExecutionArtifactRepository,
        execution_engine: XrplTransactionEngine,
        fee_engine: XrplTransactionEngine,
        refund_engine: XrplTransactionEngine | None = None,
    ) -> None:
        self.transfers = transfers
        self.artifacts = artifacts
        self.execution_engine = execution_engine
        self.fee_engine = fee_engine
        self.refund_engine = refund_engine
        self.x402 = XrplX402Adapter(fee_engine)


def workflow_task(
    handler: Callable[..., dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    """Give every Step Functions task a stable, fail-closed result contract."""

    @wraps(handler)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = handler(*args, **kwargs)
        normalized = {
            "done": False,
            "terminal": False,
            "refund": False,
            **result,
        }
        for field in ("done", "terminal", "refund"):
            if not isinstance(normalized[field], bool):
                raise TypeError(f"workflow result field {field!r} must be boolean")
        return normalized

    return wrapped


@lru_cache(maxsize=1)
def configured_execution_services() -> ExecutionServices:
    region = os.environ["AWS_DEFAULT_REGION"]
    dynamodb = boto3.resource("dynamodb", region_name=region)
    transfers = DynamoTransferRepository(dynamodb.Table(os.environ["TRANSFER_TABLE_NAME"]))
    artifacts = DynamoExecutionArtifactRepository(
        dynamodb.Table(os.environ["EXECUTION_ARTIFACT_TABLE_NAME"])
    )
    client = configured_client()
    mode = os.environ.get("EXECUTION_ROLE_MODE", "SIGNER").upper()
    if mode == "SIGNER":
        execution_wallet = wallet_from_secret(os.environ["XRPL_EXECUTION_SEED_SECRET_ID"], region)
        fee_wallet = wallet_from_secret(
            os.environ.get(
                "XRPL_FEE_SEED_SECRET_ID",
                os.environ["XRPL_EXECUTION_SEED_SECRET_ID"],
            ),
            region,
        )
        refund_secret = os.environ.get("XRPL_FEE_MERCHANT_SEED_SECRET_ID")
        refund_wallet = wallet_from_secret(refund_secret, region) if refund_secret else None
    elif mode == "RECONCILER":
        execution_wallet = WalletIdentity(os.environ["XRPL_EXECUTION_ADDRESS"])
        fee_wallet = WalletIdentity(os.environ["XRPL_FEE_PAYER_ADDRESS"])
        refund_address = os.environ.get("XRPL_FEE_MERCHANT_ADDRESS")
        refund_wallet = WalletIdentity(refund_address) if refund_address else None
    else:
        raise ValueError("EXECUTION_ROLE_MODE must be SIGNER or RECONCILER")
    refund_engine = (
        XrplTransactionEngine(client, refund_wallet, artifacts) if refund_wallet else None
    )
    return ExecutionServices(
        transfers,
        artifacts,
        XrplTransactionEngine(client, execution_wallet, artifacts),
        XrplTransactionEngine(client, fee_wallet, artifacts),
        refund_engine,
    )


def _prepared(services: ExecutionServices, transfer_id: str, kind: str) -> PreparedTransaction:
    item = services.artifacts.get(transfer_id, kind)
    if not item:
        raise RuntimeError(f"prepared {kind} transaction not found")
    return PreparedTransaction(**item)


def _resubmit_same_blob(
    engine: XrplTransactionEngine,
    prepared: PreparedTransaction,
) -> str:
    """Best-effort reliable submission using only the persisted signed identity."""

    try:
        return engine.submit(prepared)
    except TransactionRejected:
        return "EXPIRED"
    except XrplError:
        return "SUBMISSION_UNKNOWN"


@workflow_task
def mark_fee_pending(
    services: ExecutionServices, transfer_id: str, approval_hash: str
) -> dict[str, Any]:
    transfer = services.transfers.get_transfer(transfer_id)
    if transfer.approval_hash != approval_hash or not transfer.verify_approval_hash():
        raise ValueError("execution approval commitment mismatch")
    if utc_now() >= transfer.quote.expires_at:
        expired = services.transfers.transition(
            transfer_id,
            TransferStatus.APPROVED,
            TransferStatus.EXPIRED,
        )
        return {"done": True, "terminal": True, "status": expired.status}
    updated = services.transfers.transition(
        transfer_id,
        TransferStatus.APPROVED,
        TransferStatus.FEE_PENDING,
    )
    return {"done": False, "status": updated.status}


@workflow_task
def submit_fee(services: ExecutionServices, transfer_id: str) -> dict[str, Any]:
    transfer = services.transfers.get_transfer(transfer_id)
    if transfer.status != TransferStatus.FEE_PENDING:
        raise ValueError("fee can only be submitted while FEE_PENDING")
    challenge = services.x402.challenge(transfer)
    prepared = services.x402.prepare_payment(transfer, challenge)
    try:
        engine_result = services.fee_engine.submit(prepared)
    except TransactionRejected:
        failed = services.transfers.transition(
            transfer_id,
            TransferStatus.FEE_PENDING,
            TransferStatus.FAILED,
            failure_code="X402_FEE_REJECTED",
            failure_reason="the XRPL Testnet rejected the x402 fee transaction",
        )
        return {"terminal": True, "status": failed.status}
    except XrplError:
        # The signed identity is durable. Reconciliation decides the outcome.
        engine_result = "SUBMISSION_UNKNOWN"
    return {
        "terminal": False,
        "status": TransferStatus.FEE_PENDING,
        "tx_hash": prepared.tx_hash,
        "engine_result": engine_result,
        "payment_required": challenge.header_value(),
    }


@workflow_task
def reconcile_fee(services: ExecutionServices, transfer_id: str) -> dict[str, Any]:
    transfer = services.transfers.get_transfer(transfer_id)
    if transfer.status == TransferStatus.FEE_PAID:
        return {"done": True, "status": transfer.status, "tx_hash": transfer.fee_tx_hash}
    if transfer.status.terminal:
        return {"done": True, "terminal": True, "status": transfer.status}
    prepared = _prepared(services, transfer_id, "X402_FEE")
    challenge = services.x402.challenge(transfer)
    try:
        result = services.fee_engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.service_fee.pay_to,
            expected_amount=transfer.quote.service_fee.amount,
            expected_kind="X402_FEE",
            expected_source_tag=challenge.source_tag,
            expected_invoice_id=challenge.invoice_id,
        )
    except SettlementMismatch:
        failed = services.transfers.transition(
            transfer_id,
            TransferStatus.FEE_PENDING,
            TransferStatus.FAILED,
            failure_code="X402_FEE_SETTLEMENT_MISMATCH",
            failure_reason="the validated x402 fee did not match its approved commitment",
        )
        return {"done": True, "terminal": True, "status": failed.status}
    if result.state == "PENDING":
        resubmission = _resubmit_same_blob(services.fee_engine, prepared)
        if resubmission == "EXPIRED":
            failed = services.transfers.transition(
                transfer_id,
                TransferStatus.FEE_PENDING,
                TransferStatus.FAILED,
                failure_code="X402_FEE_EXPIRED",
                failure_reason="signed fee transaction expired without settlement",
            )
            return {"done": True, "terminal": True, "status": failed.status}
        return {
            "done": False,
            "status": transfer.status,
            "tx_hash": prepared.tx_hash,
            "resubmission": resubmission,
        }
    if result.state == "EXPIRED":
        failed = services.transfers.transition(
            transfer_id,
            TransferStatus.FEE_PENDING,
            TransferStatus.FAILED,
            failure_code="X402_FEE_EXPIRED",
            failure_reason="signed fee transaction expired without settlement",
        )
        return {"done": True, "terminal": True, "status": failed.status}
    paid = services.transfers.transition(
        transfer_id,
        TransferStatus.FEE_PENDING,
        TransferStatus.FEE_PAID,
        fee_tx_hash=prepared.tx_hash,
    )
    payment_response = services.x402.proof(
        prepared.tx_hash,
        services.fee_engine.wallet.address,
    ).header_value()
    return {
        "done": True,
        "status": paid.status,
        "tx_hash": prepared.tx_hash,
        "payment_response": payment_response,
    }


@workflow_task
def submit_payment(services: ExecutionServices, transfer_id: str) -> dict[str, Any]:
    transfer = services.transfers.get_transfer(transfer_id)
    if transfer.status in {
        TransferStatus.SUBMITTED,
        TransferStatus.SUBMISSION_UNKNOWN,
    }:
        return {
            "terminal": False,
            "status": transfer.status,
            "tx_hash": transfer.payment_tx_hash,
        }
    if transfer.status in {
        TransferStatus.SETTLED,
        TransferStatus.PAYOUT_PENDING,
        TransferStatus.COMPLETED,
    }:
        return {"done": True, "terminal": False, "status": transfer.status}
    if transfer.status == TransferStatus.REFUND_PENDING:
        return {"terminal": False, "refund": True, "status": transfer.status}
    if transfer.status.terminal:
        return {"done": True, "terminal": True, "status": transfer.status}
    if transfer.status == TransferStatus.FEE_PAID:
        transfer = services.transfers.transition(
            transfer_id,
            TransferStatus.FEE_PAID,
            TransferStatus.PREPARING,
        )
    elif transfer.status != TransferStatus.PREPARING:
        raise ValueError(f"payment cannot be submitted while {transfer.status}")
    try:
        prepared = services.execution_engine.prepare(
            transfer,
            kind="TRANSFER",
            destination=transfer.quote.recipient.ledger_destination,
            destination_tag=transfer.quote.recipient.destination_tag,
            destination_amount=transfer.quote.destination_amount,
            send_max=transfer.quote.send_max,
            paths=transfer.quote.paths,
        )
        engine_result = services.execution_engine.submit(prepared)
    except TransactionRejected:
        refunded = services.transfers.transition(
            transfer_id,
            TransferStatus.PREPARING,
            TransferStatus.REFUND_PENDING,
            failure_code="PAYMENT_REJECTED",
            failure_reason="the XRPL Testnet rejected the transfer transaction",
        )
        return {"terminal": False, "refund": True, "status": refunded.status}
    except Exception:
        artifact = services.artifacts.get(transfer_id, "TRANSFER")
        if not artifact:
            refunded = services.transfers.transition(
                transfer_id,
                TransferStatus.PREPARING,
                TransferStatus.REFUND_PENDING,
                failure_code="PAYMENT_PREPARATION_FAILED",
                failure_reason=(
                    "the transfer could not be prepared and no payment transaction was broadcast"
                ),
            )
            return {"terminal": False, "refund": True, "status": refunded.status}
        unknown = services.transfers.transition(
            transfer_id,
            TransferStatus.PREPARING,
            TransferStatus.SUBMISSION_UNKNOWN,
            payment_tx_hash=artifact["tx_hash"],
        )
        return {"terminal": False, "status": unknown.status, "tx_hash": artifact["tx_hash"]}
    submitted = services.transfers.transition(
        transfer_id,
        TransferStatus.PREPARING,
        (
            TransferStatus.SUBMISSION_UNKNOWN
            if engine_result == "SUBMISSION_UNKNOWN"
            else TransferStatus.SUBMITTED
        ),
        payment_tx_hash=prepared.tx_hash,
    )
    return {"terminal": False, "status": submitted.status, "tx_hash": prepared.tx_hash}


@workflow_task
def reconcile_payment(services: ExecutionServices, transfer_id: str) -> dict[str, Any]:
    transfer = services.transfers.get_transfer(transfer_id)
    if transfer.status == TransferStatus.SETTLED:
        return {"done": True, "status": transfer.status}
    if transfer.status not in {
        TransferStatus.SUBMITTED,
        TransferStatus.SUBMISSION_UNKNOWN,
    }:
        return {"done": transfer.status.terminal, "status": transfer.status}
    prepared = _prepared(services, transfer_id, "TRANSFER")
    try:
        result = services.execution_engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.recipient.ledger_destination,
            expected_amount=transfer.quote.destination_amount,
            expected_kind="TRANSFER",
            expected_destination_tag=transfer.quote.recipient.destination_tag,
            expected_send_max=transfer.quote.send_max,
            expected_paths=transfer.quote.paths,
        )
    except SettlementMismatch as error:
        refund = services.transfers.transition(
            transfer_id,
            {TransferStatus.SUBMITTED, TransferStatus.SUBMISSION_UNKNOWN},
            TransferStatus.REFUND_PENDING,
            failure_code="SETTLEMENT_MISMATCH",
            failure_reason=str(error),
        )
        return {"done": True, "refund": True, "status": refund.status}
    if result.state == "PENDING":
        resubmission = _resubmit_same_blob(services.execution_engine, prepared)
        if resubmission == "EXPIRED":
            refund = services.transfers.transition(
                transfer_id,
                {TransferStatus.SUBMITTED, TransferStatus.SUBMISSION_UNKNOWN},
                TransferStatus.REFUND_PENDING,
                failure_code="PAYMENT_EXPIRED",
                failure_reason="signed payment expired without appearing in a validated ledger",
            )
            return {"done": True, "refund": True, "status": refund.status}
        return {
            "done": False,
            "status": transfer.status,
            "tx_hash": prepared.tx_hash,
            "resubmission": resubmission,
        }
    if result.state == "EXPIRED":
        refund = services.transfers.transition(
            transfer_id,
            {TransferStatus.SUBMITTED, TransferStatus.SUBMISSION_UNKNOWN},
            TransferStatus.REFUND_PENDING,
            failure_code="PAYMENT_EXPIRED",
            failure_reason="signed payment expired without appearing in a validated ledger",
        )
        return {"done": True, "refund": True, "status": refund.status}
    explorer_base = os.environ.get(
        "XRPL_EXPLORER_URL", "https://testnet.xrpl.org/transactions"
    ).rstrip("/")
    settled = services.transfers.transition(
        transfer_id,
        {TransferStatus.SUBMITTED, TransferStatus.SUBMISSION_UNKNOWN},
        TransferStatus.SETTLED,
        ledger_index=result.ledger_index,
        delivered_amount=result.delivered_amount,
        explorer_url=f"{explorer_base}/{prepared.tx_hash}",
    )
    return {"done": True, "status": settled.status, "tx_hash": prepared.tx_hash}


@workflow_task
def complete_payout(services: ExecutionServices, transfer_id: str) -> dict[str, Any]:
    transfer = services.transfers.get_transfer(transfer_id)
    if transfer.status == TransferStatus.COMPLETED:
        return {"done": True, "status": transfer.status}
    if transfer.quote.payout_mode == PayoutMode.XRPL_WALLET:
        completed = services.transfers.transition(
            transfer_id,
            TransferStatus.SETTLED,
            TransferStatus.COMPLETED,
        )
    else:
        services.transfers.transition(
            transfer_id,
            TransferStatus.SETTLED,
            TransferStatus.PAYOUT_PENDING,
        )
        completed = services.transfers.transition(
            transfer_id,
            TransferStatus.PAYOUT_PENDING,
            TransferStatus.COMPLETED,
            payout_reference=f"SIM-{uuid.uuid4().hex[:12].upper()}",
        )
    return {"done": True, "status": completed.status}


@workflow_task
def submit_refund(services: ExecutionServices, transfer_id: str) -> dict[str, Any]:
    transfer = services.transfers.get_transfer(transfer_id)
    if services.refund_engine is None:
        failed = services.transfers.transition(
            transfer_id,
            TransferStatus.REFUND_PENDING,
            TransferStatus.FAILED,
            failure_code="REFUND_SIGNER_NOT_CONFIGURED",
            failure_reason="manual Testnet fee refund required",
        )
        return {"done": True, "status": failed.status}
    prepared = services.refund_engine.prepare(
        transfer,
        kind="X402_REFUND",
        destination=services.fee_engine.wallet.address,
        destination_tag=None,
        destination_amount=transfer.quote.service_fee.amount,
    )
    try:
        services.refund_engine.submit(prepared)
    except XrplError:
        pass
    return {"done": False, "status": transfer.status, "tx_hash": prepared.tx_hash}


@workflow_task
def reconcile_refund(services: ExecutionServices, transfer_id: str) -> dict[str, Any]:
    transfer = services.transfers.get_transfer(transfer_id)
    if services.refund_engine is None:
        return {"done": True, "status": transfer.status}
    prepared = _prepared(services, transfer_id, "X402_REFUND")
    try:
        result = services.refund_engine.reconcile(
            transfer,
            prepared,
            expected_destination=services.fee_engine.wallet.address,
            expected_amount=transfer.quote.service_fee.amount,
            expected_kind="X402_REFUND",
        )
    except SettlementMismatch:
        failed = services.transfers.transition(
            transfer_id,
            TransferStatus.REFUND_PENDING,
            TransferStatus.FAILED,
            failure_code="REFUND_SETTLEMENT_MISMATCH",
            failure_reason="the validated fee refund did not match its prepared transaction",
        )
        return {"done": True, "status": failed.status}
    if result.state == "PENDING":
        resubmission = _resubmit_same_blob(services.refund_engine, prepared)
        if resubmission == "EXPIRED":
            failed = services.transfers.transition(
                transfer_id,
                TransferStatus.REFUND_PENDING,
                TransferStatus.FAILED,
                failure_code="REFUND_EXPIRED",
                failure_reason="signed refund expired without settlement",
            )
            return {"done": True, "status": failed.status}
        return {
            "done": False,
            "status": transfer.status,
            "tx_hash": prepared.tx_hash,
            "resubmission": resubmission,
        }
    if result.state == "SETTLED":
        completed = services.transfers.transition(
            transfer_id,
            TransferStatus.REFUND_PENDING,
            TransferStatus.FAILED_REFUNDED,
        )
        return {"done": True, "status": completed.status}
    failed = services.transfers.transition(
        transfer_id,
        TransferStatus.REFUND_PENDING,
        TransferStatus.FAILED,
        failure_code="REFUND_EXPIRED",
        failure_reason="signed refund expired without settlement",
    )
    return {"done": True, "status": failed.status}


TASKS = {
    "mark_fee_pending": mark_fee_pending,
    "submit_fee": submit_fee,
    "reconcile_fee": reconcile_fee,
    "submit_payment": submit_payment,
    "reconcile_payment": reconcile_payment,
    "complete_payout": complete_payout,
    "submit_refund": submit_refund,
    "reconcile_refund": reconcile_refund,
}


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    task = event.get("task")
    transfer_id = event.get("transfer_id")
    if task not in TASKS or not isinstance(transfer_id, str):
        raise ValueError("task and transfer_id are required")
    allowed_tasks = {
        value.strip()
        for value in os.environ.get("ALLOWED_EXECUTION_TASKS", "").split(",")
        if value.strip()
    }
    if allowed_tasks and task not in allowed_tasks:
        raise ValueError(f"task {task!r} is not allowed for this worker")
    services = configured_execution_services()
    if task == "mark_fee_pending":
        approval_hash = event.get("approval_hash")
        if not isinstance(approval_hash, str):
            raise ValueError("approval_hash is required")
        return mark_fee_pending(services, transfer_id, approval_hash)
    return TASKS[task](services, transfer_id)

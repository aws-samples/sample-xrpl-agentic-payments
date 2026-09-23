from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import Any

import pytest
from conftest import direct_quote_request

from xrpl_agentcore.domain import TransferStatus
from xrpl_agentcore.execution import (
    ExecutionServices,
    mark_fee_pending,
    reconcile_fee,
    reconcile_payment,
    submit_fee,
    submit_payment,
    workflow_task,
)
from xrpl_agentcore.repository import InMemoryExecutionArtifactRepository
from xrpl_agentcore.x402 import XrplX402Adapter
from xrpl_agentcore.xrpl_payments import (
    PreparedTransaction,
    SettlementMismatch,
    XrplTransactionEngine,
    amount_to_xrpl,
)


class FakeXrplClient:
    def __init__(self) -> None:
        self.current_ledger = 10
        self.transactions: dict[str, dict[str, Any]] = {}
        self.submitted: list[str] = []
        self.filled = None

    def autofill(self, payment):
        self.filled = replace(
            payment,
            fee="12",
            sequence=1,
            last_ledger_sequence=20,
        )
        return self.filled

    def simulate(self, payment):
        return {"engine_result": "tesSUCCESS"}

    def submit_blob(self, blob):
        self.submitted.append(blob)
        return {"engine_result": "tesSUCCESS"}

    def transaction(self, tx_hash):
        return self.transactions.get(tx_hash)

    def ledger_index(self):
        return self.current_ledger


class AmbiguousSubmitClient(FakeXrplClient):
    def submit_blob(self, blob):
        self.submitted.append(blob)
        raise TimeoutError("connection closed after broadcast")


def _approved_transfer(app_services, wallets):
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))
    transfer = app_services.transfers.create("owner-a", quote.quote_id)
    return app_services.repository.approve(
        transfer.transfer_id,
        "owner-a",
        transfer.approval_hash,
        "approval-key-0001",
    )


def test_workflow_result_contract_supplies_all_routing_flags(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    artifacts = InMemoryExecutionArtifactRepository()
    services = ExecutionServices(
        app_services.repository,
        artifacts,
        XrplTransactionEngine(FakeXrplClient(), wallets["source"], artifacts),
        XrplTransactionEngine(FakeXrplClient(), wallets["fee"], artifacts),
    )

    result = mark_fee_pending(
        services,
        transfer.transfer_id,
        transfer.approval_hash,
    )

    assert result["done"] is False
    assert result["terminal"] is False
    assert result["refund"] is False


def test_workflow_result_contract_rejects_non_boolean_flags() -> None:
    @workflow_task
    def malformed_task() -> dict[str, Any]:
        return {"done": "yes", "status": "PENDING"}

    with pytest.raises(TypeError, match="must be boolean"):
        malformed_task()


def test_persist_before_broadcast_and_same_blob_resubmission(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    artifacts = InMemoryExecutionArtifactRepository()
    client = FakeXrplClient()
    engine = XrplTransactionEngine(client, wallets["source"], artifacts)

    prepared = engine.prepare(
        transfer,
        kind="TRANSFER",
        destination=transfer.quote.recipient.ledger_destination,
        destination_tag=None,
        destination_amount=transfer.quote.destination_amount,
        send_max=transfer.quote.send_max,
        paths=transfer.quote.paths,
    )
    assert artifacts.get(transfer.transfer_id, "TRANSFER")["signed_blob"]
    engine.submit(prepared)
    engine.submit(prepared)
    assert client.submitted == [prepared.signed_blob, prepared.signed_blob]

    duplicate = engine.prepare(
        transfer,
        kind="TRANSFER",
        destination=transfer.quote.recipient.ledger_destination,
        destination_tag=None,
        destination_amount=transfer.quote.destination_amount,
        send_max=transfer.quote.send_max,
        paths=transfer.quote.paths,
    )
    assert duplicate == prepared


def test_reconciliation_requires_exact_validated_non_partial_payment(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    artifacts = InMemoryExecutionArtifactRepository()
    client = FakeXrplClient()
    engine = XrplTransactionEngine(client, wallets["source"], artifacts)
    prepared = engine.prepare(
        transfer,
        kind="TRANSFER",
        destination=transfer.quote.recipient.ledger_destination,
        destination_tag=None,
        destination_amount=transfer.quote.destination_amount,
        send_max=transfer.quote.send_max,
        paths=transfer.quote.paths,
    )
    tx = client.filled.to_xrpl()
    tx["DeliverMax"] = tx.pop("Amount")
    tx["hash"] = prepared.tx_hash
    delivered = amount_to_xrpl(transfer.quote.destination_amount)
    if not isinstance(delivered, str):
        delivered = {
            "currency": delivered.currency,
            "issuer": delivered.issuer,
            "value": delivered.value,
        }
    client.transactions[prepared.tx_hash] = {
        "validated": True,
        "hash": prepared.tx_hash,
        "ledger_index": 19,
        "tx_json": tx,
        "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": delivered},
    }
    result = engine.reconcile(
        transfer,
        prepared,
        expected_destination=transfer.quote.recipient.ledger_destination,
        expected_amount=transfer.quote.destination_amount,
        expected_kind="TRANSFER",
    )
    assert result.state == "SETTLED"
    assert result.ledger_index == 19

    client.transactions[prepared.tx_hash]["tx_json"]["Flags"] = 0x00020000
    with pytest.raises(SettlementMismatch, match="partial"):
        engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.recipient.ledger_destination,
            expected_amount=transfer.quote.destination_amount,
            expected_kind="TRANSFER",
        )


def test_reconciliation_binds_destination_tag_sendmax_and_path(app_services, wallets) -> None:
    quote = app_services.quotes.create(
        "owner-a",
        direct_quote_request(wallets, destination_tag=77),
    )
    transfer = app_services.transfers.create("owner-a", quote.quote_id)
    transfer = app_services.repository.approve(
        transfer.transfer_id,
        "owner-a",
        transfer.approval_hash,
        "approval-key-0002",
    )
    artifacts = InMemoryExecutionArtifactRepository()
    client = FakeXrplClient()
    engine = XrplTransactionEngine(client, wallets["source"], artifacts)
    prepared = engine.prepare(
        transfer,
        kind="TRANSFER",
        destination=transfer.quote.recipient.ledger_destination,
        destination_tag=transfer.quote.recipient.destination_tag,
        destination_amount=transfer.quote.destination_amount,
        send_max=transfer.quote.send_max,
        paths=transfer.quote.paths,
    )
    tx = client.filled.to_xrpl()
    tx["hash"] = prepared.tx_hash
    delivered = amount_to_xrpl(transfer.quote.destination_amount)
    assert not isinstance(delivered, str)
    client.transactions[prepared.tx_hash] = {
        "validated": True,
        "hash": prepared.tx_hash,
        "ledger_index": 19,
        "tx_json": tx,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "delivered_amount": {
                "currency": delivered.currency,
                "issuer": delivered.issuer,
                "value": delivered.value,
            },
        },
    }
    engine.reconcile(
        transfer,
        prepared,
        expected_destination=transfer.quote.recipient.ledger_destination,
        expected_destination_tag=77,
        expected_amount=transfer.quote.destination_amount,
        expected_send_max=transfer.quote.send_max,
        expected_paths=transfer.quote.paths,
        expected_kind="TRANSFER",
    )

    client.transactions[prepared.tx_hash]["tx_json"]["DestinationTag"] = 78
    with pytest.raises(SettlementMismatch, match="destination tag"):
        engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.recipient.ledger_destination,
            expected_destination_tag=77,
            expected_amount=transfer.quote.destination_amount,
            expected_send_max=transfer.quote.send_max,
            expected_paths=transfer.quote.paths,
            expected_kind="TRANSFER",
        )
    client.transactions[prepared.tx_hash]["tx_json"]["DestinationTag"] = 77

    original_send_max = client.transactions[prepared.tx_hash]["tx_json"]["SendMax"]
    client.transactions[prepared.tx_hash]["tx_json"]["SendMax"] = {
        **original_send_max,
        "value": "999",
    }
    with pytest.raises(SettlementMismatch, match="SendMax"):
        engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.recipient.ledger_destination,
            expected_destination_tag=77,
            expected_amount=transfer.quote.destination_amount,
            expected_send_max=transfer.quote.send_max,
            expected_paths=transfer.quote.paths,
            expected_kind="TRANSFER",
        )
    client.transactions[prepared.tx_hash]["tx_json"]["SendMax"] = original_send_max

    client.transactions[prepared.tx_hash]["tx_json"]["Paths"] = [
        [{"currency": "USD", "issuer": wallets["merchant"].address}]
    ]
    with pytest.raises(SettlementMismatch, match="path"):
        engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.recipient.ledger_destination,
            expected_destination_tag=77,
            expected_amount=transfer.quote.destination_amount,
            expected_send_max=transfer.quote.send_max,
            expected_paths=transfer.quote.paths,
            expected_kind="TRANSFER",
        )


def test_reconciliation_binds_payment_amount(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    artifacts = InMemoryExecutionArtifactRepository()
    client = FakeXrplClient()
    engine = XrplTransactionEngine(client, wallets["source"], artifacts)
    prepared = engine.prepare(
        transfer,
        kind="TRANSFER",
        destination=transfer.quote.recipient.ledger_destination,
        destination_tag=None,
        destination_amount=transfer.quote.destination_amount,
        send_max=transfer.quote.send_max,
        paths=transfer.quote.paths,
    )
    tx = client.filled.to_xrpl()
    tx["hash"] = prepared.tx_hash
    tx["Amount"]["value"] = "999"
    delivered = amount_to_xrpl(transfer.quote.destination_amount)
    assert not isinstance(delivered, str)
    client.transactions[prepared.tx_hash] = {
        "validated": True,
        "hash": prepared.tx_hash,
        "ledger_index": 19,
        "tx_json": tx,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "delivered_amount": {
                "currency": delivered.currency,
                "issuer": delivered.issuer,
                "value": delivered.value,
            },
        },
    }
    with pytest.raises(SettlementMismatch, match="Amount"):
        engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.recipient.ledger_destination,
            expected_amount=transfer.quote.destination_amount,
            expected_kind="TRANSFER",
        )


def test_ledger_sequence_expiry_is_not_premature_failure(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    client = FakeXrplClient()
    engine = XrplTransactionEngine(
        client,
        wallets["source"],
        InMemoryExecutionArtifactRepository(),
    )
    prepared = engine.prepare(
        transfer,
        kind="TRANSFER",
        destination=transfer.quote.recipient.ledger_destination,
        destination_tag=None,
        destination_amount=transfer.quote.destination_amount,
        send_max=transfer.quote.send_max,
        paths=[],
    )
    assert (
        engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.recipient.ledger_destination,
            expected_amount=transfer.quote.destination_amount,
            expected_kind="TRANSFER",
        ).state
        == "PENDING"
    )
    client.current_ledger = 21
    assert (
        engine.reconcile(
            transfer,
            prepared,
            expected_destination=transfer.quote.recipient.ledger_destination,
            expected_amount=transfer.quote.destination_amount,
            expected_kind="TRANSFER",
        ).state
        == "EXPIRED"
    )
    assert transfer.status == TransferStatus.APPROVED


def test_duplicate_fee_submission_reuses_one_signed_identity(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.APPROVED,
        TransferStatus.FEE_PENDING,
    )
    artifacts = InMemoryExecutionArtifactRepository()
    fee_client = FakeXrplClient()
    services = ExecutionServices(
        app_services.repository,
        artifacts,
        XrplTransactionEngine(FakeXrplClient(), wallets["source"], artifacts),
        XrplTransactionEngine(fee_client, wallets["fee"], artifacts),
    )

    first = submit_fee(services, transfer.transfer_id)
    second = submit_fee(services, transfer.transfer_id)

    assert first["tx_hash"] == second["tx_hash"]
    assert len(fee_client.submitted) == 2
    assert fee_client.submitted[0] == fee_client.submitted[1]
    assert artifacts.get(transfer.transfer_id, "X402_FEE")["tx_hash"] == first["tx_hash"]


def test_x402_fee_uses_v2_xrpl_testnet_envelopes_and_invoice_binding(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.APPROVED,
        TransferStatus.FEE_PENDING,
    )
    artifacts = InMemoryExecutionArtifactRepository()
    fee_client = FakeXrplClient()
    engine = XrplTransactionEngine(fee_client, wallets["fee"], artifacts)
    adapter = XrplX402Adapter(engine)
    challenge = adapter.challenge(transfer)

    payment_required = json.loads(base64.b64decode(challenge.header_value()))
    requirement = payment_required["accepts"][0]
    assert payment_required["x402Version"] == 2
    assert requirement["network"] == "xrpl:1"
    assert requirement["scheme"] == "exact"
    assert requirement["asset"] == "XRP"
    assert requirement["amount"] == "1000"
    assert requirement["extra"]["invoiceId"] == challenge.invoice_id
    assert "signedTxBlob" not in str(payment_required)

    prepared = adapter.prepare_payment(transfer, challenge)
    payment_signature = json.loads(
        base64.b64decode(adapter.payment_signature_header(challenge, prepared))
    )
    assert payment_signature["accepted"] == requirement
    assert payment_signature["payload"]["signedTxBlob"] == prepared.signed_blob
    assert payment_signature["payload"]["invoiceId"] == challenge.invoice_id

    tx = fee_client.filled.to_xrpl()
    assert tx["InvoiceID"]
    assert tx["SourceTag"] == transfer.quote.service_fee.source_tag
    assert len(tx["Memos"]) == 2

    payment_response = json.loads(
        base64.b64decode(adapter.proof(prepared.tx_hash, wallets["fee"].address).header_value())
    )
    assert payment_response == {
        "success": True,
        "transaction": prepared.tx_hash,
        "network": "xrpl:1",
        "payer": wallets["fee"].address,
    }


def test_pending_fee_reconciliation_resubmits_persisted_blob(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.APPROVED,
        TransferStatus.FEE_PENDING,
    )
    artifacts = InMemoryExecutionArtifactRepository()
    fee_client = FakeXrplClient()
    services = ExecutionServices(
        app_services.repository,
        artifacts,
        XrplTransactionEngine(FakeXrplClient(), wallets["source"], artifacts),
        XrplTransactionEngine(fee_client, wallets["fee"], artifacts),
    )
    submitted = submit_fee(services, transfer.transfer_id)
    prepared = PreparedTransaction(**artifacts.get(transfer.transfer_id, "X402_FEE"))

    pending = reconcile_fee(services, transfer.transfer_id)

    assert pending["done"] is False
    assert pending["tx_hash"] == submitted["tx_hash"]
    assert pending["resubmission"] == "tesSUCCESS"
    assert fee_client.submitted == [prepared.signed_blob, prepared.signed_blob]


def test_ambiguous_broadcast_enters_unknown_then_reconciles(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.APPROVED,
        TransferStatus.FEE_PENDING,
    )
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.FEE_PENDING,
        TransferStatus.FEE_PAID,
        fee_tx_hash="A" * 64,
    )
    artifacts = InMemoryExecutionArtifactRepository()
    payment_client = AmbiguousSubmitClient()
    engine = XrplTransactionEngine(payment_client, wallets["source"], artifacts)
    services = ExecutionServices(
        app_services.repository,
        artifacts,
        engine,
        XrplTransactionEngine(FakeXrplClient(), wallets["fee"], artifacts),
    )

    submitted = submit_payment(services, transfer.transfer_id)
    assert submitted["status"] == TransferStatus.SUBMISSION_UNKNOWN
    prepared = PreparedTransaction(**artifacts.get(transfer.transfer_id, "TRANSFER"))
    tx = payment_client.filled.to_xrpl()
    tx["hash"] = prepared.tx_hash
    delivered = amount_to_xrpl(transfer.quote.destination_amount)
    assert not isinstance(delivered, str)
    payment_client.transactions[prepared.tx_hash] = {
        "validated": True,
        "hash": prepared.tx_hash,
        "ledger_index": 19,
        "tx_json": tx,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "delivered_amount": {
                "currency": delivered.currency,
                "issuer": delivered.issuer,
                "value": delivered.value,
            },
        },
    }

    reconciled = reconcile_payment(services, transfer.transfer_id)
    assert reconciled["status"] == TransferStatus.SETTLED
    assert (
        app_services.repository.get_transfer(transfer.transfer_id).status == TransferStatus.SETTLED
    )


def test_pending_payment_reconciliation_resubmits_persisted_blob(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.APPROVED,
        TransferStatus.FEE_PENDING,
    )
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.FEE_PENDING,
        TransferStatus.FEE_PAID,
        fee_tx_hash="A" * 64,
    )
    artifacts = InMemoryExecutionArtifactRepository()
    payment_client = FakeXrplClient()
    services = ExecutionServices(
        app_services.repository,
        artifacts,
        XrplTransactionEngine(payment_client, wallets["source"], artifacts),
        XrplTransactionEngine(FakeXrplClient(), wallets["fee"], artifacts),
    )
    submitted = submit_payment(services, transfer.transfer_id)
    prepared = PreparedTransaction(**artifacts.get(transfer.transfer_id, "TRANSFER"))

    pending = reconcile_payment(services, transfer.transfer_id)

    assert pending["done"] is False
    assert pending["tx_hash"] == submitted["tx_hash"]
    assert pending["resubmission"] == "tesSUCCESS"
    assert payment_client.submitted == [prepared.signed_blob, prepared.signed_blob]


def test_retried_submit_task_reuses_progressed_payment(app_services, wallets) -> None:
    transfer = _approved_transfer(app_services, wallets)
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.APPROVED,
        TransferStatus.FEE_PENDING,
    )
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.FEE_PENDING,
        TransferStatus.FEE_PAID,
        fee_tx_hash="A" * 64,
    )
    artifacts = InMemoryExecutionArtifactRepository()
    payment_client = FakeXrplClient()
    services = ExecutionServices(
        app_services.repository,
        artifacts,
        XrplTransactionEngine(payment_client, wallets["source"], artifacts),
        XrplTransactionEngine(FakeXrplClient(), wallets["fee"], artifacts),
    )

    first = submit_payment(services, transfer.transfer_id)
    replay = submit_payment(services, transfer.transfer_id)

    assert first["tx_hash"] == replay["tx_hash"]
    assert replay["status"] == TransferStatus.SUBMITTED
    assert len(payment_client.submitted) == 1

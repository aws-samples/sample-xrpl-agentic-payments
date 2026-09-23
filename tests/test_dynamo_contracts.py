from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from conftest import direct_quote_request

from xrpl_agentcore.domain import TransferStatus, utc_now
from xrpl_agentcore.outbox import lambda_handler as dispatch_outbox
from xrpl_agentcore.repository import DynamoTransferRepository


class FakeDynamoClient:
    def __init__(self) -> None:
        self.transaction: dict[str, Any] | None = None

    def transact_write_items(self, **kwargs: Any) -> None:
        self.transaction = kwargs


class FakeMeta:
    def __init__(self) -> None:
        self.client = FakeDynamoClient()


class FakeTable:
    name = "transfer-table"

    def __init__(self, transfer) -> None:
        self.transfer = transfer
        self.meta = FakeMeta()
        self.put: dict[str, Any] | None = None
        self.update: dict[str, Any] | None = None

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "Item": {
                "owner_sub": self.transfer.owner_sub,
                "payload": self.transfer.model_dump(mode="json", exclude_none=True),
            }
        }

    def update_item(self, **kwargs: Any) -> None:
        self.update = kwargs

    def put_item(self, **kwargs: Any) -> None:
        self.put = kwargs


def test_transfer_row_persists_beyond_quote_ttl(app_services, wallets) -> None:
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))
    transfer = app_services.transfers.create("owner-a", quote.quote_id)
    table = FakeTable(transfer)
    repository = DynamoTransferRepository(table)

    repository.create_transfer(transfer)

    assert table.put is not None
    item = table.put["Item"]
    assert "expires_at" not in item
    assert item["approval_expires_at"] == int(quote.expires_at.timestamp())


def test_dynamo_approval_condition_and_outbox_are_one_transaction(app_services, wallets) -> None:
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))
    transfer = app_services.transfers.create("owner-a", quote.quote_id)
    table = FakeTable(transfer)
    repository = DynamoTransferRepository(table)

    approved = repository.approve(
        transfer.transfer_id,
        transfer.owner_sub,
        transfer.approval_hash,
        "approval-key-0001",
        now=utc_now(),
    )

    transaction = table.meta.client.transaction
    assert transaction is not None
    writes = transaction["TransactItems"]
    assert len(writes) == 2
    update = writes[0]["Update"]
    outbox = writes[1]["Put"]
    assert (
        update["ConditionExpression"]
        == "#status = :awaiting AND owner_sub = :owner AND approval_hash = :hash "
        "AND revision = :revision AND approval_expires_at > :now"
    )
    assert outbox["ConditionExpression"] == "attribute_not_exists(pk)"
    assert update["Key"] == {
        "pk": f"TRANSFER#{transfer.transfer_id}",
        "sk": "META",
    }
    assert update["ExpressionAttributeValues"][":revision"] == 0
    assert update["ExpressionAttributeValues"][":now"] <= int(transfer.quote.expires_at.timestamp())
    assert outbox["Item"]["event_type"] == "EXECUTE_APPROVED_TRANSFER"
    assert outbox["Item"]["transfer_id"] == transfer.transfer_id
    assert outbox["Item"]["approval_hash"] == transfer.approval_hash
    assert approved.status == TransferStatus.APPROVED


def test_dynamo_transition_is_status_and_revision_conditional(app_services, wallets) -> None:
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))
    transfer = app_services.transfers.create("owner-a", quote.quote_id)
    table = FakeTable(transfer)
    repository = DynamoTransferRepository(table)

    updated = repository.transition(
        transfer.transfer_id,
        TransferStatus.AWAITING_APPROVAL,
        TransferStatus.EXPIRED,
    )

    assert table.update is not None
    assert table.update["ConditionExpression"] == "#status = :expected AND revision = :revision"
    assert table.update["ExpressionAttributeValues"][":expected"] == "AWAITING_APPROVAL"
    assert table.update["ExpressionAttributeValues"][":revision"] == 0
    assert updated.status == TransferStatus.EXPIRED


class FakeStepFunctions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def start_execution(self, **kwargs: Any) -> None:
        if self.calls:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ExecutionAlreadyExists",
                        "Message": "same transfer execution",
                    }
                },
                "StartExecution",
            )
        self.calls.append(kwargs)


def test_outbox_dispatch_uses_transfer_id_as_idempotent_execution_name(
    monkeypatch,
) -> None:
    step_functions = FakeStepFunctions()
    monkeypatch.setenv("TRANSFER_STATE_MACHINE_ARN", "arn:aws:states:test")
    monkeypatch.setattr(
        "xrpl_agentcore.outbox.boto3.client",
        lambda *_args, **_kwargs: step_functions,
    )
    transfer_id = "tr_" + "a" * 32
    image = {
        key: TypeSerializer().serialize(value)
        for key, value in {
            "event_type": "EXECUTE_APPROVED_TRANSFER",
            "transfer_id": transfer_id,
            "approval_hash": "b" * 64,
            "expires_at": int((utc_now() + timedelta(minutes=10)).timestamp()),
        }.items()
    }
    record = {
        "eventName": "INSERT",
        "dynamodb": {"NewImage": image},
    }

    result = dispatch_outbox({"Records": [record, record]}, None)

    assert result == {"started": 1}
    assert step_functions.calls[0]["name"] == transfer_id
    assert json.loads(step_functions.calls[0]["input"]) == {
        "transfer_id": transfer_id,
        "approval_hash": "b" * 64,
    }

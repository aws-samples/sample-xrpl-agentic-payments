"""Transfer persistence ports and DynamoDB adapters."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from botocore.exceptions import ClientError

from .domain import (
    Quote,
    Transfer,
    TransferStatus,
    assert_transition,
    utc_now,
)


class RepositoryError(RuntimeError):
    pass


class NotFound(RepositoryError):
    pass


class OwnershipError(RepositoryError):
    pass


class Conflict(RepositoryError):
    pass


class Expired(RepositoryError):
    pass


class TransferRepository(Protocol):
    def save_quote(self, quote: Quote, owner_sub: str) -> Quote: ...

    def get_quote(self, quote_id: str, owner_sub: str | None = None) -> Quote: ...

    def create_transfer(self, transfer: Transfer) -> Transfer: ...

    def get_transfer(self, transfer_id: str, owner_sub: str | None = None) -> Transfer: ...

    def list_transfers(self, owner_sub: str) -> list[Transfer]: ...

    def approve(
        self,
        transfer_id: str,
        owner_sub: str,
        approval_hash: str,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> Transfer: ...

    def transition(
        self,
        transfer_id: str,
        expected: TransferStatus | set[TransferStatus],
        target: TransferStatus,
        **updates: Any,
    ) -> Transfer: ...


class ExecutionArtifactRepository(Protocol):
    def put_prepared(
        self,
        transfer_id: str,
        kind: str,
        tx_hash: str,
        signed_blob: str,
        sequence: int,
        last_ledger_sequence: int,
    ) -> dict[str, Any]: ...

    def get(self, transfer_id: str, kind: str) -> dict[str, Any] | None: ...


class InMemoryTransferRepository:
    """Thread-safe adapter used by local development and hermetic tests."""

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._quotes: dict[str, Quote] = {}
        self._quote_owners: dict[str, str] = {}
        self._transfers: dict[str, Transfer] = {}
        self._outbox: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @property
    def outbox(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._outbox.values())

    def save_quote(self, quote: Quote, owner_sub: str) -> Quote:
        with self._lock:
            existing = self._quotes.get(quote.quote_id)
            if existing and (
                existing != quote or self._quote_owners.get(quote.quote_id) != owner_sub
            ):
                raise Conflict("quote_id already contains a different quote")
            self._quotes[quote.quote_id] = quote
            self._quote_owners[quote.quote_id] = owner_sub
            return quote

    def get_quote(self, quote_id: str, owner_sub: str | None = None) -> Quote:
        with self._lock:
            try:
                quote = self._quotes[quote_id]
            except KeyError:
                raise NotFound("quote not found") from None
            if owner_sub is not None and self._quote_owners.get(quote_id) != owner_sub:
                raise NotFound("quote not found")
            return quote

    def create_transfer(self, transfer: Transfer) -> Transfer:
        with self._lock:
            existing = self._transfers.get(transfer.transfer_id)
            if existing:
                if existing.approval_hash != transfer.approval_hash:
                    raise Conflict("transfer_id already contains a different intent")
                return existing
            self._transfers[transfer.transfer_id] = transfer
            return transfer

    def get_transfer(self, transfer_id: str, owner_sub: str | None = None) -> Transfer:
        with self._lock:
            try:
                transfer = self._transfers[transfer_id]
            except KeyError:
                raise NotFound("transfer not found") from None
            if owner_sub is not None and transfer.owner_sub != owner_sub:
                # Avoid disclosing that another user's identifier exists.
                raise NotFound("transfer not found")
            return transfer

    def list_transfers(self, owner_sub: str) -> list[Transfer]:
        with self._lock:
            return sorted(
                (
                    transfer
                    for transfer in self._transfers.values()
                    if transfer.owner_sub == owner_sub
                ),
                key=lambda transfer: transfer.created_at,
                reverse=True,
            )

    def approve(
        self,
        transfer_id: str,
        owner_sub: str,
        approval_hash: str,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> Transfer:
        now = now or self._clock()
        with self._lock:
            current = self.get_transfer(transfer_id, owner_sub)
            if (
                current.approval_idempotency_key == idempotency_key
                and current.approval_hash == approval_hash
                and current.approved_at is not None
            ):
                return current
            if current.status != TransferStatus.AWAITING_APPROVAL:
                raise Conflict(f"transfer cannot be approved from {current.status}")
            if now >= current.quote.expires_at:
                expired = current.model_copy(
                    update={
                        "status": TransferStatus.EXPIRED,
                        "updated_at": now,
                        "revision": current.revision + 1,
                    }
                )
                self._transfers[transfer_id] = expired
                raise Expired("quote expired before approval")
            if approval_hash != current.approval_hash or not current.verify_approval_hash():
                raise Conflict("approval hash does not match the immutable transfer")
            approved = current.model_copy(
                update={
                    "status": TransferStatus.APPROVED,
                    "approved_at": now,
                    "approval_idempotency_key": idempotency_key,
                    "updated_at": now,
                    "revision": current.revision + 1,
                }
            )
            self._transfers[transfer_id] = approved
            self._outbox.setdefault(
                transfer_id,
                {
                    "event_type": "EXECUTE_APPROVED_TRANSFER",
                    "transfer_id": transfer_id,
                    "owner_sub": owner_sub,
                    "approval_hash": approval_hash,
                    "created_at": now.isoformat(),
                },
            )
            return approved

    def transition(
        self,
        transfer_id: str,
        expected: TransferStatus | set[TransferStatus],
        target: TransferStatus,
        **updates: Any,
    ) -> Transfer:
        with self._lock:
            current = self.get_transfer(transfer_id)
            allowed = {expected} if isinstance(expected, TransferStatus) else expected
            if current.status not in allowed:
                if current.status == target:
                    return current
                raise Conflict(
                    f"transfer {transfer_id} is {current.status}; expected "
                    f"{sorted(str(value) for value in allowed)}"
                )
            assert_transition(current.status, target)
            updated = current.model_copy(
                update={
                    **updates,
                    "status": target,
                    "updated_at": self._clock(),
                    "revision": current.revision + 1,
                }
            )
            self._transfers[transfer_id] = updated
            return updated


class InMemoryExecutionArtifactRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def put_prepared(
        self,
        transfer_id: str,
        kind: str,
        tx_hash: str,
        signed_blob: str,
        sequence: int,
        last_ledger_sequence: int,
    ) -> dict[str, Any]:
        key = (transfer_id, kind)
        item = {
            "transfer_id": transfer_id,
            "kind": kind,
            "tx_hash": tx_hash,
            "signed_blob": signed_blob,
            "sequence": sequence,
            "last_ledger_sequence": last_ledger_sequence,
        }
        with self._lock:
            existing = self._items.get(key)
            if existing:
                if existing != item:
                    raise Conflict("a distinct signed transaction is already prepared")
                return dict(existing)
            self._items[key] = item
            return dict(item)

    def get(self, transfer_id: str, kind: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get((transfer_id, kind))
            return dict(item) if item else None


def _item_payload(model: Quote | Transfer) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


class DynamoTransferRepository:
    """DynamoDB repository with conditional state and transactional approval outbox."""

    def __init__(self, table: Any, clock: Callable[[], datetime] = utc_now) -> None:
        self._table = table
        self._clock = clock

    @staticmethod
    def _pk(kind: str, identifier: str) -> str:
        return f"{kind}#{identifier}"

    @staticmethod
    def _conditional(error: ClientError) -> bool:
        return error.response.get("Error", {}).get("Code") in {
            "ConditionalCheckFailedException",
            "TransactionCanceledException",
        }

    def save_quote(self, quote: Quote, owner_sub: str) -> Quote:
        item = {
            "pk": self._pk("QUOTE", quote.quote_id),
            "sk": "META",
            "entity_type": "QUOTE",
            "owner_sub": owner_sub,
            "expires_at": int(quote.expires_at.timestamp()),
            "payload": _item_payload(quote),
        }
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as error:
            if not self._conditional(error):
                raise
            existing = self.get_quote(quote.quote_id, owner_sub)
            if existing != quote:
                raise Conflict("quote_id already contains a different quote") from error
        return quote

    def get_quote(self, quote_id: str, owner_sub: str | None = None) -> Quote:
        response = self._table.get_item(
            Key={"pk": self._pk("QUOTE", quote_id), "sk": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item or (owner_sub is not None and item.get("owner_sub") != owner_sub):
            raise NotFound("quote not found")
        return Quote.model_validate(item["payload"])

    def create_transfer(self, transfer: Transfer) -> Transfer:
        item = {
            "pk": self._pk("TRANSFER", transfer.transfer_id),
            "sk": "META",
            "entity_type": "TRANSFER",
            "owner_sub": transfer.owner_sub,
            "status": transfer.status.value,
            "approval_hash": transfer.approval_hash,
            "approval_expires_at": int(transfer.quote.expires_at.timestamp()),
            "revision": transfer.revision,
            "gsi1pk": f"OWNER#{transfer.owner_sub}",
            "gsi1sk": transfer.created_at.isoformat(),
            "payload": _item_payload(transfer),
        }
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as error:
            if not self._conditional(error):
                raise
            existing = self.get_transfer(transfer.transfer_id)
            if existing.approval_hash != transfer.approval_hash:
                raise Conflict("transfer_id already contains a different intent") from error
            return existing
        return transfer

    def get_transfer(self, transfer_id: str, owner_sub: str | None = None) -> Transfer:
        response = self._table.get_item(
            Key={"pk": self._pk("TRANSFER", transfer_id), "sk": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item or (owner_sub is not None and item.get("owner_sub") != owner_sub):
            raise NotFound("transfer not found")
        return Transfer.model_validate(item["payload"])

    def list_transfers(self, owner_sub: str) -> list[Transfer]:
        response = self._table.query(
            IndexName="owner-created-index",
            KeyConditionExpression="gsi1pk = :owner",
            ExpressionAttributeValues={":owner": f"OWNER#{owner_sub}"},
            ScanIndexForward=False,
        )
        return [
            Transfer.model_validate(item["payload"])
            for item in response.get("Items", [])
            if item.get("entity_type") == "TRANSFER"
        ]

    def approve(
        self,
        transfer_id: str,
        owner_sub: str,
        approval_hash: str,
        idempotency_key: str,
        *,
        now: datetime | None = None,
    ) -> Transfer:
        now = now or self._clock()
        current = self.get_transfer(transfer_id, owner_sub)
        if (
            current.approval_idempotency_key == idempotency_key
            and current.approval_hash == approval_hash
            and current.approved_at is not None
        ):
            return current
        if now >= current.quote.expires_at:
            raise Expired("quote expired before approval")
        if current.status != TransferStatus.AWAITING_APPROVAL:
            raise Conflict(f"transfer cannot be approved from {current.status}")
        if approval_hash != current.approval_hash or not current.verify_approval_hash():
            raise Conflict("approval hash does not match the immutable transfer")

        approved = current.model_copy(
            update={
                "status": TransferStatus.APPROVED,
                "approved_at": now,
                "approval_idempotency_key": idempotency_key,
                "updated_at": now,
                "revision": current.revision + 1,
            }
        )
        table_name = self._table.name
        transfer_key = {"pk": self._pk("TRANSFER", transfer_id), "sk": "META"}
        outbox_item = {
            "pk": self._pk("TRANSFER", transfer_id),
            "sk": "OUTBOX#EXECUTE",
            "entity_type": "OUTBOX",
            "event_type": "EXECUTE_APPROVED_TRANSFER",
            "transfer_id": transfer_id,
            "owner_sub": owner_sub,
            "approval_hash": approval_hash,
            "created_at": now.isoformat(),
            "expires_at": int(current.quote.expires_at.timestamp()) + 86_400,
        }
        values = {
            ":awaiting": TransferStatus.AWAITING_APPROVAL.value,
            ":owner": owner_sub,
            ":hash": approval_hash,
            ":revision": current.revision,
            ":approved": TransferStatus.APPROVED.value,
            ":payload": _item_payload(approved),
            ":next_revision": approved.revision,
            ":key": idempotency_key,
            ":now": int(now.timestamp()),
        }
        try:
            self._table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": table_name,
                            "Key": transfer_key,
                            "ConditionExpression": (
                                "#status = :awaiting AND owner_sub = :owner AND "
                                "approval_hash = :hash AND revision = :revision AND "
                                "approval_expires_at > :now"
                            ),
                            "UpdateExpression": (
                                "SET #status = :approved, payload = :payload, "
                                "revision = :next_revision, approval_idempotency_key = :key"
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": values,
                        }
                    },
                    {
                        "Put": {
                            "TableName": table_name,
                            "Item": outbox_item,
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except ClientError as error:
            if not self._conditional(error):
                raise
            latest = self.get_transfer(transfer_id, owner_sub)
            if (
                latest.approval_hash == approval_hash
                and latest.approval_idempotency_key == idempotency_key
                and latest.approved_at is not None
            ):
                return latest
            raise Conflict("approval transaction conflicted") from error
        return approved

    def transition(
        self,
        transfer_id: str,
        expected: TransferStatus | set[TransferStatus],
        target: TransferStatus,
        **updates: Any,
    ) -> Transfer:
        current = self.get_transfer(transfer_id)
        allowed = {expected} if isinstance(expected, TransferStatus) else expected
        if current.status not in allowed:
            if current.status == target:
                return current
            raise Conflict(f"transfer is {current.status}, expected {allowed}")
        assert_transition(current.status, target)
        updated = current.model_copy(
            update={
                **updates,
                "status": target,
                "updated_at": self._clock(),
                "revision": current.revision + 1,
            }
        )
        try:
            self._table.update_item(
                Key={"pk": self._pk("TRANSFER", transfer_id), "sk": "META"},
                ConditionExpression="#status = :expected AND revision = :revision",
                UpdateExpression=(
                    "SET #status = :target, payload = :payload, revision = :next_revision"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":expected": current.status.value,
                    ":revision": current.revision,
                    ":target": target.value,
                    ":payload": _item_payload(updated),
                    ":next_revision": updated.revision,
                },
            )
        except ClientError as error:
            if self._conditional(error):
                raise Conflict("state transition conflicted") from error
            raise
        return updated


class DynamoExecutionArtifactRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    def put_prepared(
        self,
        transfer_id: str,
        kind: str,
        tx_hash: str,
        signed_blob: str,
        sequence: int,
        last_ledger_sequence: int,
    ) -> dict[str, Any]:
        item = {
            "transfer_id": transfer_id,
            "kind": kind,
            "tx_hash": tx_hash,
            "signed_blob": signed_blob,
            "sequence": sequence,
            "last_ledger_sequence": last_ledger_sequence,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(transfer_id) AND attribute_not_exists(#kind)"
                ),
                ExpressionAttributeNames={"#kind": "kind"},
            )
            return item
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            existing = self.get(transfer_id, kind)
            if existing == item:
                return item
            raise Conflict("a distinct signed transaction is already prepared") from error

    def get(self, transfer_id: str, kind: str) -> dict[str, Any] | None:
        response = self._table.get_item(
            Key={"transfer_id": transfer_id, "kind": kind},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return dict(item) if item else None

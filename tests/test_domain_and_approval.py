from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import direct_quote_request

from xrpl_agentcore.domain import Amount, Asset, TransferStatus, canonical_json, utc_now
from xrpl_agentcore.repository import Conflict, Expired, NotFound
from xrpl_agentcore.services import ApproveTransferRequest


def test_decimal_canonicalization_never_uses_binary_float() -> None:
    amount = Amount(asset=Asset(currency="XRP"), value="0.100000")
    assert amount.value == Decimal("0.100000")
    assert amount.canonical_value() == "0.100000"
    assert canonical_json({"amount": amount.value}) == '{"amount":"0.100000"}'


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", "0.0000001"])
def test_invalid_xrp_amounts_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        Amount(asset=Asset(currency="XRP"), value=value)


def test_approval_binds_route_sendmax_fee_owner_and_recipient(app_services, wallets) -> None:
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))
    transfer = app_services.transfers.create("owner-a", quote.quote_id)
    assert transfer.verify_approval_hash()

    tampered_quote = transfer.quote.model_copy(
        update={
            "send_max": transfer.quote.send_max.model_copy(
                update={"value": transfer.quote.send_max.value + Decimal("1")}
            )
        }
    )
    tampered = transfer.model_copy(update={"quote": tampered_quote})
    assert not tampered.verify_approval_hash()

    tampered_route = transfer.model_copy(
        update={
            "quote": transfer.quote.model_copy(
                update={
                    "paths": [[{"currency": "MXN"}]],
                    "route_label": "forged route",
                }
            )
        }
    )
    assert not tampered_route.verify_approval_hash()

    forged_owner = transfer.model_copy(update={"owner_sub": "owner-b"})
    assert not forged_owner.verify_approval_hash()


def test_approval_is_owner_scoped_transactional_and_idempotent(app_services, wallets) -> None:
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))
    transfer = app_services.transfers.create("owner-a", quote.quote_id)
    request = ApproveTransferRequest(
        approval_hash=transfer.approval_hash,
        idempotency_key="approval-key-0001",
    )

    approved = app_services.transfers.approve("owner-a", transfer.transfer_id, request)
    duplicate = app_services.transfers.approve("owner-a", transfer.transfer_id, request)

    assert approved == duplicate
    assert approved.status == TransferStatus.APPROVED
    assert len(app_services.repository.outbox) == 1
    app_services.repository.transition(
        transfer.transfer_id,
        TransferStatus.APPROVED,
        TransferStatus.FEE_PENDING,
    )
    progressed_duplicate = app_services.transfers.approve(
        "owner-a",
        transfer.transfer_id,
        request,
    )
    assert progressed_duplicate.status == TransferStatus.FEE_PENDING
    assert len(app_services.repository.outbox) == 1
    with pytest.raises(NotFound):
        app_services.transfers.get("owner-b", transfer.transfer_id)


def test_forged_hash_and_expired_approval_are_rejected(app_services, wallets) -> None:
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))
    transfer = app_services.transfers.create("owner-a", quote.quote_id)
    with pytest.raises(Conflict):
        app_services.repository.approve(
            transfer.transfer_id,
            "owner-a",
            "f" * 64,
            "approval-key-forged",
        )
    with pytest.raises(Expired):
        app_services.repository.approve(
            transfer.transfer_id,
            "owner-a",
            transfer.approval_hash,
            "approval-key-expired",
            now=utc_now() + timedelta(minutes=10),
        )
    assert (
        app_services.repository.get_transfer(transfer.transfer_id).status == TransferStatus.EXPIRED
    )


def test_quote_cannot_be_used_to_create_another_users_intent(app_services, wallets) -> None:
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))
    with pytest.raises(NotFound):
        app_services.transfers.create("owner-b", quote.quote_id)


def test_retried_intent_creation_returns_the_same_transfer(app_services, wallets) -> None:
    quote = app_services.quotes.create("owner-a", direct_quote_request(wallets))

    first = app_services.transfers.create("owner-a", quote.quote_id)
    replay = app_services.transfers.create("owner-a", quote.quote_id)

    assert replay == first
    assert replay.transfer_id == first.transfer_id
    assert replay.approval_hash == first.approval_hash

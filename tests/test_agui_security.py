from __future__ import annotations

import jwt
import pytest
from fastapi import HTTPException

from xrpl_agentcore.agent_runtime import (
    SAFE_RENDER_TOOLS,
    trusted_owner_from_authorization,
    validate_agui_input,
)
from xrpl_agentcore.domain import AgentTransferProjection
from xrpl_agentcore.security import REDACTED, redact_for_log


def test_agui_accepts_only_application_owned_components() -> None:
    allowed = next(iter(SAFE_RENDER_TOOLS))
    value = validate_agui_input(
        {
            "state": {"memoryOptIn": False},
            "tools": [{"name": allowed}],
        }
    )
    assert value["tools"][0]["name"] == allowed
    safe_state = validate_agui_input(
        {
            "state": {
                "activeTransfer": {
                    "transfer_id": "tr_safe",
                    "status": "SUBMITTED",
                    "payout_mode": "XRPL_WALLET",
                    "revision": 4,
                }
            },
            "tools": [],
        }
    )
    assert safe_state["state"]["activeTransfer"]["status"] == "SUBMITTED"
    with pytest.raises(HTTPException, match="unapproved client tool"):
        validate_agui_input({"state": {}, "tools": [{"name": "execute_transfer"}]})


@pytest.mark.parametrize(
    "state",
    [
        {"walletSeed": "s..."},
        {"activeTransfer": {"signed_blob": "ABC"}},
        {"bankAccount": "1234"},
        {"modelGeneratedHtml": "<script />"},
        {"activeTransfer": {"approval_hash": "a" * 64}},
        {"activeTransfer": {"recipient_address": "rPII"}},
    ],
)
def test_agui_rejects_secret_pii_and_unknown_state(state) -> None:
    with pytest.raises(HTTPException):
        validate_agui_input({"state": state, "tools": []})


def test_owner_comes_from_runtime_validated_bearer_claim(monkeypatch) -> None:
    monkeypatch.delenv("ALLOW_DEMO_AUTH", raising=False)
    token = jwt.encode({"sub": "cognito-user-123"}, "test-key", algorithm="HS256")
    assert trusted_owner_from_authorization(f"Bearer {token}") == "cognito-user-123"
    with pytest.raises(HTTPException):
        trusted_owner_from_authorization(None)


def test_agui_rejects_sensitive_or_unknown_forwarded_properties() -> None:
    with pytest.raises(HTTPException):
        validate_agui_input(
            {
                "state": {},
                "tools": [],
                "forwardedProps": {"preferences": {}, "bankAccount": "1234"},
            }
        )


def test_safe_projection_and_log_redaction_exclude_credentials_and_pii(
    app_services, wallets
) -> None:
    from conftest import direct_quote_request

    quote = app_services.quotes.create("private-cognito-sub", direct_quote_request(wallets))
    transfer = app_services.transfers.create("private-cognito-sub", quote.quote_id)
    projection = AgentTransferProjection.from_transfer(transfer).model_dump(mode="json")
    serialized = str(projection)
    assert transfer.owner_sub not in serialized
    assert transfer.quote.recipient.ledger_destination not in serialized
    assert transfer.approval_hash not in serialized
    assert "paths" not in projection

    redacted = redact_for_log(
        {
            "authorization": "Bearer jwt",
            "nested": {
                "wallet_seed": "s-secret",
                "signed_blob": "DEADBEEF",
                "recipient_address": wallets["recipient"].address,
                "status": "SUBMITTED",
            },
        }
    )
    assert redacted["authorization"] == REDACTED
    assert redacted["nested"]["wallet_seed"] == REDACTED
    assert redacted["nested"]["signed_blob"] == REDACTED
    assert redacted["nested"]["recipient_address"] == REDACTED
    assert redacted["nested"]["status"] == "SUBMITTED"

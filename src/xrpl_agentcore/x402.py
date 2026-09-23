"""A replaceable x402 v2 exact-scheme adapter for XRPL Testnet."""

from __future__ import annotations

import base64
import json
from typing import Any, Literal

from pydantic import Field

from .domain import ServiceFee, StrictModel, Transfer, commitment
from .xrpl_payments import (
    PreparedTransaction,
    XrplTransactionEngine,
    amount_to_xrpl,
)

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


class X402Challenge(StrictModel):
    x402_version: Literal[2] = 2
    scheme: Literal["exact"] = "exact"
    network: Literal["xrpl:1"] = "xrpl:1"
    resource: str
    transfer_id: str
    amount_drops: str = Field(pattern=r"^[1-9][0-9]*$")
    asset: Literal["XRP"] = "XRP"
    pay_to: str
    max_timeout_seconds: int = Field(ge=1, le=600)
    invoice_id: str = Field(min_length=1, max_length=200)
    source_tag: int = Field(ge=0, le=4_294_967_295)
    challenge_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def commitment_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude={"challenge_hash"})

    def requirement_payload(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "amount": self.amount_drops,
            "asset": self.asset,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": {
                "invoiceId": self.invoice_id,
                "sourceTag": self.source_tag,
            },
        }

    def payment_required_payload(self) -> dict[str, Any]:
        return {
            "x402Version": self.x402_version,
            "resource": {
                "url": self.resource,
                "description": "Execute an approved AgentCore XRPL transfer",
                "mimeType": "application/json",
            },
            "accepts": [self.requirement_payload()],
            "extensions": {},
        }

    def header_value(self) -> str:
        raw = json.dumps(
            self.payment_required_payload(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.b64encode(raw).decode()


class X402Proof(StrictModel):
    success: Literal[True] = True
    transaction_hash: str
    network: Literal["xrpl:1"] = "xrpl:1"
    payer: str

    def payload(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "transaction": self.transaction_hash,
            "network": self.network,
            "payer": self.payer,
        }

    def header_value(self) -> str:
        raw = json.dumps(self.payload(), separators=(",", ":"), sort_keys=True).encode()
        return base64.b64encode(raw).decode()


class XrplX402Adapter:
    """Deterministic payer-side adapter; no model receives a signing capability."""

    def __init__(self, engine: XrplTransactionEngine) -> None:
        self.engine = engine

    @staticmethod
    def challenge(transfer: Transfer) -> X402Challenge:
        fee: ServiceFee = transfer.quote.service_fee
        drops = amount_to_xrpl(fee.amount)
        if not isinstance(drops, str):
            raise ValueError("x402 exact XRP amount must encode as drops")
        data = {
            "x402_version": 2,
            "scheme": fee.scheme,
            "network": fee.network,
            "resource": f"https://poc.local/v1/transfers/{transfer.transfer_id}/execute",
            "transfer_id": transfer.transfer_id,
            "amount_drops": drops,
            "asset": fee.amount.asset.currency,
            "pay_to": fee.pay_to,
            "max_timeout_seconds": fee.max_timeout_seconds,
            "invoice_id": commitment(
                {
                    "kind": "X402_FEE",
                    "transfer_id": transfer.transfer_id,
                    "approval_hash": transfer.approval_hash,
                }
            ),
            "source_tag": fee.source_tag,
        }
        return X402Challenge(**data, challenge_hash=commitment(data))

    def prepare_payment(self, transfer: Transfer, challenge: X402Challenge) -> PreparedTransaction:
        expected = self.challenge(transfer)
        if challenge != expected:
            raise ValueError("x402 challenge does not match the approved service fee")
        return self.engine.prepare(
            transfer,
            kind="X402_FEE",
            destination=transfer.quote.service_fee.pay_to,
            destination_tag=None,
            destination_amount=transfer.quote.service_fee.amount,
            source_tag=challenge.source_tag,
            invoice_id=challenge.invoice_id,
        )

    @staticmethod
    def payment_signature_header(
        challenge: X402Challenge,
        prepared: PreparedTransaction,
    ) -> str:
        if prepared.transfer_id != challenge.transfer_id or prepared.kind != "X402_FEE":
            raise ValueError("prepared transaction does not match the x402 challenge")
        payload = {
            "x402Version": 2,
            "resource": challenge.payment_required_payload()["resource"],
            "accepted": challenge.requirement_payload(),
            "payload": {
                "signedTxBlob": prepared.signed_blob,
                "invoiceId": challenge.invoice_id,
            },
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return base64.b64encode(raw).decode()

    @staticmethod
    def proof(tx_hash: str, payer: str) -> X402Proof:
        return X402Proof(
            transaction_hash=tx_hash,
            payer=payer,
        )

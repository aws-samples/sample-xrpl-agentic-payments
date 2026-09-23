"""Server-authoritative transfer contracts and state transitions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from xrpl.core.addresscodec import is_valid_classic_address, is_valid_xaddress

CONTRACT_VERSION = 1
TRANSFER_ID_RE = re.compile(r"^tr_[0-9a-f]{32}$")
QUOTE_ID_RE = re.compile(r"^qt_[0-9a-f]{32}$")
MAX_AMOUNT = Decimal("100000000000")


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    """Return stable JSON suitable for an approval commitment."""

    def normalize(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return normalize(item.model_dump(mode="json", exclude_none=True))
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(item, dict):
            return {str(key): normalize(item[key]) for key in sorted(item)}
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return item

    return json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def commitment(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PayoutMode(StrEnum):
    XRPL_WALLET = "XRPL_WALLET"
    LOCAL_FIAT_SIMULATED = "LOCAL_FIAT_SIMULATED"


class TransferStatus(StrEnum):
    QUOTED = "QUOTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    FEE_PENDING = "FEE_PENDING"
    FEE_PAID = "FEE_PAID"
    PREPARING = "PREPARING"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    SUBMITTED = "SUBMITTED"
    SETTLED = "SETTLED"
    PAYOUT_PENDING = "PAYOUT_PENDING"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    REFUND_PENDING = "REFUND_PENDING"
    FAILED_REFUNDED = "FAILED_REFUNDED"

    @property
    def terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.EXPIRED,
            self.FAILED,
            self.FAILED_REFUNDED,
        }


LEGAL_TRANSITIONS: dict[TransferStatus, frozenset[TransferStatus]] = {
    TransferStatus.QUOTED: frozenset({TransferStatus.AWAITING_APPROVAL, TransferStatus.EXPIRED}),
    TransferStatus.AWAITING_APPROVAL: frozenset({TransferStatus.APPROVED, TransferStatus.EXPIRED}),
    TransferStatus.APPROVED: frozenset(
        {TransferStatus.FEE_PENDING, TransferStatus.EXPIRED, TransferStatus.FAILED}
    ),
    TransferStatus.FEE_PENDING: frozenset({TransferStatus.FEE_PAID, TransferStatus.FAILED}),
    TransferStatus.FEE_PAID: frozenset({TransferStatus.PREPARING, TransferStatus.REFUND_PENDING}),
    TransferStatus.PREPARING: frozenset(
        {
            TransferStatus.SUBMISSION_UNKNOWN,
            TransferStatus.SUBMITTED,
            TransferStatus.REFUND_PENDING,
        }
    ),
    TransferStatus.SUBMISSION_UNKNOWN: frozenset(
        {
            TransferStatus.SUBMITTED,
            TransferStatus.SETTLED,
            TransferStatus.REFUND_PENDING,
        }
    ),
    TransferStatus.SUBMITTED: frozenset(
        {
            TransferStatus.SUBMISSION_UNKNOWN,
            TransferStatus.SETTLED,
            TransferStatus.REFUND_PENDING,
        }
    ),
    TransferStatus.SETTLED: frozenset({TransferStatus.PAYOUT_PENDING, TransferStatus.COMPLETED}),
    TransferStatus.PAYOUT_PENDING: frozenset({TransferStatus.COMPLETED, TransferStatus.FAILED}),
    TransferStatus.REFUND_PENDING: frozenset(
        {TransferStatus.FAILED_REFUNDED, TransferStatus.FAILED}
    ),
    TransferStatus.COMPLETED: frozenset(),
    TransferStatus.EXPIRED: frozenset(),
    TransferStatus.FAILED: frozenset(),
    TransferStatus.FAILED_REFUNDED: frozenset(),
}


class Asset(StrictModel):
    currency: str = Field(min_length=3, max_length=40)
    issuer: str | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,40}", value):
            raise ValueError("currency must be 3-40 uppercase alphanumeric characters")
        return value

    @model_validator(mode="after")
    def validate_issuer(self) -> Asset:
        if self.currency == "XRP" and self.issuer is not None:
            raise ValueError("native XRP must not have an issuer")
        if self.currency != "XRP":
            if not self.issuer or not is_valid_classic_address(self.issuer):
                raise ValueError("issued assets require a valid classic issuer address")
        return self


class Amount(StrictModel):
    asset: Asset
    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("amount must be a decimal string")
        try:
            parsed = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as error:
            raise ValueError("amount is not a valid decimal") from error
        if not parsed.is_finite() or parsed <= 0 or parsed > MAX_AMOUNT:
            raise ValueError("amount must be finite, positive, and within the POC ceiling")
        return parsed

    @model_validator(mode="after")
    def validate_precision(self) -> Amount:
        normalized = self.value.normalize()
        decimal_places = max(0, -normalized.as_tuple().exponent)
        if self.asset.currency == "XRP" and decimal_places > 6:
            raise ValueError("XRP supports at most six decimal places")
        if self.asset.currency != "XRP" and len(normalized.as_tuple().digits) > 15:
            raise ValueError("issued-currency values support at most 15 significant digits")
        return self

    def canonical_value(self) -> str:
        return format(self.value, "f")


class Recipient(StrictModel):
    ledger_destination: str
    destination_tag: int | None = Field(default=None, ge=0, le=4_294_967_295)
    display_name: str = Field(min_length=1, max_length=200)
    country: str = Field(default="", max_length=100)
    payout_alias: str | None = Field(default=None, max_length=200)

    @field_validator("ledger_destination")
    @classmethod
    def validate_destination(cls, value: str) -> str:
        value = value.strip()
        if is_valid_xaddress(value):
            raise ValueError("use a classic address plus destination_tag")
        if not is_valid_classic_address(value):
            raise ValueError("ledger_destination must be a classic XRPL address")
        return value


class ServiceFee(StrictModel):
    network: Literal["xrpl:1"] = "xrpl:1"
    scheme: Literal["exact"] = "exact"
    amount: Amount
    pay_to: str
    source_tag: int = Field(default=20_260_601, ge=0, le=4_294_967_295)
    max_timeout_seconds: int = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def validate_xrp_fee(self) -> ServiceFee:
        if self.amount.asset.currency != "XRP":
            raise ValueError("the POC x402 fee must be denominated in XRP")
        return self

    @field_validator("pay_to")
    @classmethod
    def validate_pay_to(cls, value: str) -> str:
        if not is_valid_classic_address(value):
            raise ValueError("fee pay_to must be a classic XRPL address")
        return value


class Corridor(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9-]{3,80}$")
    label: str = Field(min_length=1, max_length=120)
    source_asset: Asset
    destination_asset: Asset
    fixture_rate: Decimal | None = None
    enabled: bool = True


class Quote(StrictModel):
    quote_id: str
    corridor_id: str
    source_account: str
    payout_mode: PayoutMode
    recipient: Recipient
    destination_amount: Amount
    source_amount: Amount
    send_max: Amount
    slippage_bps: int = Field(ge=0, le=1_000)
    paths: list[list[dict[str, Any]]] = Field(default_factory=list)
    route_label: str
    service_fee: ServiceFee
    issued_at: datetime
    expires_at: datetime
    demo_fixture: bool = False
    quote_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("quote_id")
    @classmethod
    def validate_quote_id(cls, value: str) -> str:
        if not QUOTE_ID_RE.fullmatch(value):
            raise ValueError("invalid quote_id")
        return value

    @field_validator("source_account")
    @classmethod
    def validate_source(cls, value: str) -> str:
        if not is_valid_classic_address(value):
            raise ValueError("source_account must be a classic XRPL address")
        return value

    def hash_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"quote_hash"},
            exclude_none=True,
        )

    def verify_hash(self) -> bool:
        return commitment(self.hash_payload()) == self.quote_hash

    @model_validator(mode="after")
    def validate_contract(self) -> Quote:
        if self.expires_at <= self.issued_at:
            raise ValueError("quote expiry must be after issuance")
        if self.source_amount.asset != self.send_max.asset:
            raise ValueError("source amount and SendMax must use the same asset")
        if self.send_max.value < self.source_amount.value:
            raise ValueError("SendMax cannot be below the quoted source amount")
        return self


class Transfer(StrictModel):
    transfer_id: str
    owner_sub: str = Field(min_length=1, max_length=200)
    quote: Quote
    status: TransferStatus
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    approval_idempotency_key: str | None = None
    fee_tx_hash: str | None = None
    payment_tx_hash: str | None = None
    ledger_index: int | None = None
    delivered_amount: Amount | None = None
    explorer_url: str | None = None
    payout_reference: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    revision: int = 0

    @field_validator("transfer_id")
    @classmethod
    def validate_transfer_id(cls, value: str) -> str:
        if not TRANSFER_ID_RE.fullmatch(value):
            raise ValueError("invalid transfer_id")
        return value

    def approval_payload(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "owner_sub": self.owner_sub,
            "transfer_id": self.transfer_id,
            "quote": self.quote.hash_payload(),
            "quote_hash": self.quote.quote_hash,
        }

    def verify_approval_hash(self) -> bool:
        return commitment(self.approval_payload()) == self.approval_hash


class TransferProjection(StrictModel):
    transfer_id: str
    status: TransferStatus
    payout_mode: PayoutMode
    recipient_display: str
    recipient_destination: str
    destination_tag: int | None = None
    destination_amount: Amount
    source_amount: Amount
    send_max: Amount
    slippage_bps: int
    service_fee: ServiceFee
    route_label: str
    expires_at: datetime
    approval_hash: str
    fee_tx_hash: str | None = None
    payment_tx_hash: str | None = None
    ledger_index: int | None = None
    explorer_url: str | None = None
    payout_reference: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    revision: int

    @classmethod
    def from_transfer(cls, transfer: Transfer) -> TransferProjection:
        recipient = transfer.quote.recipient.display_name
        return cls(
            transfer_id=transfer.transfer_id,
            status=transfer.status,
            payout_mode=transfer.quote.payout_mode,
            recipient_display=recipient,
            recipient_destination=transfer.quote.recipient.ledger_destination,
            destination_tag=transfer.quote.recipient.destination_tag,
            destination_amount=transfer.quote.destination_amount,
            source_amount=transfer.quote.source_amount,
            send_max=transfer.quote.send_max,
            slippage_bps=transfer.quote.slippage_bps,
            service_fee=transfer.quote.service_fee,
            route_label=transfer.quote.route_label,
            expires_at=transfer.quote.expires_at,
            approval_hash=transfer.approval_hash,
            fee_tx_hash=transfer.fee_tx_hash,
            payment_tx_hash=transfer.payment_tx_hash,
            ledger_index=transfer.ledger_index,
            explorer_url=transfer.explorer_url,
            payout_reference=transfer.payout_reference,
            failure_code=transfer.failure_code,
            failure_reason=transfer.failure_reason,
            revision=transfer.revision,
        )


class AgentTransferProjection(StrictModel):
    """Status view safe for Gateway tool results and model context."""

    transfer_id: str
    status: TransferStatus
    payout_mode: PayoutMode
    recipient_display: str
    destination_amount: Amount
    source_amount: Amount
    send_max: Amount
    service_fee_amount: Amount
    route_label: str
    expires_at: datetime
    fee_tx_hash: str | None = None
    payment_tx_hash: str | None = None
    ledger_index: int | None = None
    explorer_url: str | None = None
    payout_reference: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    revision: int

    @classmethod
    def from_transfer(cls, transfer: Transfer) -> AgentTransferProjection:
        return cls(
            transfer_id=transfer.transfer_id,
            status=transfer.status,
            payout_mode=transfer.quote.payout_mode,
            recipient_display=transfer.quote.recipient.display_name,
            destination_amount=transfer.quote.destination_amount,
            source_amount=transfer.quote.source_amount,
            send_max=transfer.quote.send_max,
            service_fee_amount=transfer.quote.service_fee.amount,
            route_label=transfer.quote.route_label,
            expires_at=transfer.quote.expires_at,
            fee_tx_hash=transfer.fee_tx_hash,
            payment_tx_hash=transfer.payment_tx_hash,
            ledger_index=transfer.ledger_index,
            explorer_url=transfer.explorer_url,
            payout_reference=transfer.payout_reference,
            failure_code=transfer.failure_code,
            failure_reason=transfer.failure_reason,
            revision=transfer.revision,
        )


def assert_transition(current: TransferStatus, target: TransferStatus) -> None:
    if target not in LEGAL_TRANSITIONS[current]:
        raise ValueError(f"illegal transfer transition: {current} -> {target}")

"""Corridor, quote, and transfer application services."""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from .domain import (
    Amount,
    Asset,
    Corridor,
    PayoutMode,
    Quote,
    Recipient,
    ServiceFee,
    StrictModel,
    Transfer,
    TransferProjection,
    TransferStatus,
    commitment,
    utc_now,
)
from .repository import Expired, NotFound, TransferRepository

DEFAULT_ISSUER_USD = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
DEFAULT_ISSUER_MXN = "r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg"


class QuoteRequest(StrictModel):
    corridor_id: str
    destination_amount: str
    payout_mode: PayoutMode
    recipient_address: str | None = None
    destination_tag: int | None = Field(default=None, ge=0, le=4_294_967_295)
    recipient_name: str = Field(min_length=1, max_length=200)
    recipient_country: str = Field(default="", max_length=100)
    payout_alias: str | None = Field(default=None, max_length=200)
    slippage_bps: int = Field(default=100, ge=0, le=1_000)


class CreateTransferRequest(StrictModel):
    quote_id: str


class ApproveTransferRequest(StrictModel):
    approval_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=200)


class PathQuote(StrictModel):
    source_amount: Amount
    paths: list[list[dict[str, Any]]] = Field(default_factory=list)
    route_label: str
    demo_fixture: bool = False


class PathQuoteProvider(Protocol):
    def quote(
        self,
        corridor: Corridor,
        source_account: str,
        recipient: Recipient,
        destination_amount: Amount,
    ) -> PathQuote: ...


class FixturePathQuoteProvider:
    """Deterministic local provider; every result is explicitly marked as a fixture."""

    def quote(
        self,
        corridor: Corridor,
        source_account: str,
        recipient: Recipient,
        destination_amount: Amount,
    ) -> PathQuote:
        del source_account, recipient
        if corridor.fixture_rate is None or corridor.fixture_rate <= 0:
            raise ValueError("fixture corridor requires a positive fixture_rate")
        source_value = (destination_amount.value / corridor.fixture_rate).quantize(
            Decimal("0.000001"), rounding=ROUND_UP
        )
        return PathQuote(
            source_amount=Amount(asset=corridor.source_asset, value=source_value),
            paths=[],
            route_label=f"Demo XRPL path at {corridor.fixture_rate} destination/source",
            demo_fixture=True,
        )


class CorridorService:
    def __init__(self, corridors: list[Corridor]) -> None:
        self._corridors = {corridor.id: corridor for corridor in corridors if corridor.enabled}

    @classmethod
    def from_environment(cls) -> CorridorService:
        config_path = os.environ.get("CORRIDOR_CONFIG_PATH", "config/corridors.json")
        path = Path(config_path)
        if path.exists():
            raw = json.loads(path.read_text())
            return cls([Corridor.model_validate(item) for item in raw])
        usd_issuer = os.environ.get("XRPL_USD_ISSUER_ADDRESS") or DEFAULT_ISSUER_USD
        mxn_issuer = os.environ.get("XRPL_MXN_ISSUER_ADDRESS") or DEFAULT_ISSUER_MXN
        fixture_rate = os.environ.get("XRPL_FIXTURE_RATE", "17.25")
        return cls(
            [
                Corridor(
                    id="usd-mxn-testnet",
                    label="USD to MXN Testnet demo",
                    source_asset=Asset(currency="USD", issuer=usd_issuer),
                    destination_asset=Asset(currency="MXN", issuer=mxn_issuer),
                    fixture_rate=Decimal(fixture_rate),
                )
            ]
        )

    def list(self) -> list[Corridor]:
        return list(self._corridors.values())

    def get(self, corridor_id: str) -> Corridor:
        try:
            return self._corridors[corridor_id]
        except KeyError:
            raise KeyError(f"unsupported corridor {corridor_id!r}") from None


class QuoteService:
    def __init__(
        self,
        repository: TransferRepository,
        corridors: CorridorService,
        provider: PathQuoteProvider,
        *,
        source_account: str,
        payout_account: str,
        fee_merchant: str,
        quote_ttl_seconds: int = 120,
        fee_xrp: str = "0.001",
    ) -> None:
        self.repository = repository
        self.corridors = corridors
        self.provider = provider
        self.source_account = source_account
        self.payout_account = payout_account
        self.fee_merchant = fee_merchant
        self.quote_ttl_seconds = quote_ttl_seconds
        self.fee_xrp = fee_xrp

    def create(self, owner_sub: str, request: QuoteRequest) -> Quote:
        corridor = self.corridors.get(request.corridor_id)
        ledger_destination = (
            self.payout_account
            if request.payout_mode == PayoutMode.LOCAL_FIAT_SIMULATED
            else (request.recipient_address or "")
        )
        recipient = Recipient(
            ledger_destination=ledger_destination,
            destination_tag=request.destination_tag,
            display_name=request.recipient_name,
            country=request.recipient_country,
            payout_alias=request.payout_alias,
        )
        destination_amount = Amount(
            asset=corridor.destination_asset,
            value=request.destination_amount,
        )
        path_quote = self.provider.quote(
            corridor,
            self.source_account,
            recipient,
            destination_amount,
        )
        multiplier = Decimal(10_000 + request.slippage_bps) / Decimal(10_000)
        send_max_value = (path_quote.source_amount.value * multiplier).quantize(
            Decimal("0.000001"), rounding=ROUND_UP
        )
        now = utc_now()
        quote_data = {
            "quote_id": f"qt_{uuid.uuid4().hex}",
            "corridor_id": corridor.id,
            "source_account": self.source_account,
            "payout_mode": request.payout_mode,
            "recipient": recipient,
            "destination_amount": destination_amount,
            "source_amount": path_quote.source_amount,
            "send_max": Amount(asset=corridor.source_asset, value=send_max_value),
            "slippage_bps": request.slippage_bps,
            "paths": path_quote.paths,
            "route_label": path_quote.route_label,
            "service_fee": ServiceFee(
                amount=Amount(asset=Asset(currency="XRP"), value=self.fee_xrp),
                pay_to=self.fee_merchant,
            ),
            "issued_at": now,
            "expires_at": now + timedelta(seconds=self.quote_ttl_seconds),
            "demo_fixture": path_quote.demo_fixture,
        }
        quote = Quote(**quote_data, quote_hash=commitment(quote_data))
        return self.repository.save_quote(quote, owner_sub)


class TransferService:
    def __init__(self, repository: TransferRepository) -> None:
        self.repository = repository

    def create(self, owner_sub: str, quote_id: str) -> Transfer:
        quote = self.repository.get_quote(quote_id, owner_sub)
        transfer_id = (
            "tr_"
            + commitment(
                {
                    "contract_version": 1,
                    "intent": "cross_border_transfer",
                    "owner_sub": owner_sub,
                    "quote_id": quote.quote_id,
                }
            )[:32]
        )
        try:
            return self.repository.get_transfer(transfer_id, owner_sub)
        except NotFound:
            pass
        now = utc_now()
        if now >= quote.expires_at:
            raise Expired("quote expired before transfer creation")
        if not quote.verify_hash():
            raise ValueError("stored quote hash is invalid")
        transfer_data = {
            "transfer_id": transfer_id,
            "owner_sub": owner_sub,
            "quote": quote,
            "status": TransferStatus.AWAITING_APPROVAL,
            "created_at": now,
            "updated_at": now,
        }
        provisional = Transfer(**transfer_data, approval_hash="0" * 64)
        transfer = provisional.model_copy(
            update={"approval_hash": commitment(provisional.approval_payload())}
        )
        if not transfer.verify_approval_hash():
            raise RuntimeError("failed to create approval commitment")
        return self.repository.create_transfer(transfer)

    def approve(
        self,
        owner_sub: str,
        transfer_id: str,
        request: ApproveTransferRequest,
    ) -> Transfer:
        return self.repository.approve(
            transfer_id,
            owner_sub,
            request.approval_hash,
            request.idempotency_key,
        )

    def get(self, owner_sub: str, transfer_id: str) -> TransferProjection:
        return TransferProjection.from_transfer(
            self.repository.get_transfer(transfer_id, owner_sub)
        )

    def list(self, owner_sub: str) -> list[TransferProjection]:
        return [
            TransferProjection.from_transfer(transfer)
            for transfer in self.repository.list_transfers(owner_sub)
        ]

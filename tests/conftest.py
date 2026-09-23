from __future__ import annotations

from typing import Any

import pytest
from xrpl.wallet import Wallet

from xrpl_agentcore.api import AppServices
from xrpl_agentcore.repository import InMemoryTransferRepository
from xrpl_agentcore.services import (
    CorridorService,
    FixturePathQuoteProvider,
    QuoteRequest,
    QuoteService,
    TransferService,
)


@pytest.fixture
def wallets() -> dict[str, Wallet]:
    return {
        "source": Wallet.create(),
        "recipient": Wallet.create(),
        "payout": Wallet.create(),
        "merchant": Wallet.create(),
        "fee": Wallet.create(),
    }


@pytest.fixture
def app_services(wallets: dict[str, Wallet]) -> AppServices:
    repository = InMemoryTransferRepository()
    corridors = CorridorService.from_environment()
    quotes = QuoteService(
        repository,
        corridors,
        FixturePathQuoteProvider(),
        source_account=wallets["source"].address,
        payout_account=wallets["payout"].address,
        fee_merchant=wallets["merchant"].address,
    )
    return AppServices(
        repository,
        corridors,
        quotes,
        TransferService(repository),
    )


def direct_quote_request(wallets: dict[str, Wallet], **updates: Any) -> QuoteRequest:
    values: dict[str, Any] = {
        "corridor_id": "usd-mxn-testnet",
        "destination_amount": "100.25",
        "payout_mode": "XRPL_WALLET",
        "recipient_address": wallets["recipient"].address,
        "recipient_name": "Demo Recipient",
        "recipient_country": "MX",
        "slippage_bps": 100,
    }
    values.update(updates)
    return QuoteRequest(**values)

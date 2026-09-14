# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Live x402 XRPL tests against the reference facilitator and XRPL testnet.

Skipped unless X402_LIVE_TESTS=1, because these need network access, a funded
testnet wallet, and a third-party service that can be down for reasons unrelated
to this repository. Run them when changing src/payments/xrpl_exact.py:

    X402_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_x402_xrpl_live.py -v

These are the tests that would catch the facilitator changing its contract — the
offline tests in test_x402_xrpl.py pin what we believe the contract to be, and
only these confirm it is still true.

NOTHING HERE SETTLES. Every assertion stops at /verify, which does not touch the
ledger, so a full run spends nothing beyond the XRPL read requests. There is
deliberately no live /settle test: settlement is not idempotent and a test that
moves money on every run is a test nobody dares to run. The transactions signed
below are valid bearer instruments until their LastLedgerSequence passes (~1
minute), and are then dead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("X402_LIVE_TESTS") != "1",
    reason="live network tests; set X402_LIVE_TESTS=1 to run",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WALLETS_FILE = PROJECT_ROOT / "config" / "wallets.json"
TESTNET_RPC = "https://s.altnet.rippletest.net:51234"


@pytest.fixture(scope="module")
def wallets():
    if not WALLETS_FILE.exists():
        pytest.skip(f"{WALLETS_FILE} not found; run scripts/provision_wallets.py")
    return json.loads(WALLETS_FILE.read_text())


@pytest.fixture(scope="module")
def facilitator():
    from src.payments.facilitator import Facilitator

    return Facilitator()


@pytest.fixture(scope="module")
def x402(wallets):
    from xrpl.clients import JsonRpcClient
    from xrpl.wallet import Wallet

    from src.payments.x402 import X402Client
    from src.payments.xrpl_exact import XRPL_TESTNET, XrplExactProvider

    payer = Wallet.from_seed(wallets["execution"]["seed"])
    return X402Client(
        [XrplExactProvider(payer, JsonRpcClient(TESTNET_RPC), network=XRPL_TESTNET)]
    )


def make_challenge(pay_to, asset, amount, extra, network="xrpl:1", timeout=60):
    from src.payments.x402 import X402_VERSION, PaymentRequired

    return PaymentRequired.from_wire(
        {
            "x402Version": X402_VERSION,
            "error": "payment required",
            "resource": {
                "url": "https://api.example.com/orderbook",
                "description": "XRPL orderbook snapshot",
                "mimeType": "application/json",
            },
            "accepts": [
                {
                    "scheme": "exact",
                    "network": network,
                    "amount": amount,
                    "asset": asset,
                    "payTo": pay_to,
                    "maxTimeoutSeconds": timeout,
                    "extra": extra,
                }
            ],
            "extensions": {},
        }
    )


def test_facilitator_still_supports_xrpl(facilitator):
    """If this fails, the XRPL rail has no facilitator and the demo cannot work."""
    xrpl_kinds = [k for k in facilitator.supported() if k.network.startswith("xrpl:")]
    assert xrpl_kinds, "facilitator no longer advertises any xrpl:* network"
    kind = xrpl_kinds[0]
    assert kind.scheme == "exact"
    assert kind.x402_version == 2
    # The provider refuses fee-sponsored requirements outright; if this flips to
    # true, that refusal becomes a blocker rather than a safeguard.
    assert kind.extra.get("areFeesSponsored") is False


def test_native_xrp_payment_verifies(x402, facilitator, wallets):
    challenge = make_challenge(
        wallets["destination"]["address"], "XRP", "1000", {"areFeesSponsored": False}
    )
    payload = x402.pay(challenge)
    result = facilitator.verify(payload, challenge.accepts[0])
    assert result.is_valid, result.failure
    assert result.payer == wallets["execution"]["address"]


def test_issued_currency_payment_verifies(x402, facilitator, wallets):
    meta = wallets["_metadata"]
    challenge = make_challenge(
        wallets["destination"]["address"],
        meta["rlusd_currency"],
        "0.01",
        {"areFeesSponsored": False, "issuer": meta["rlusd_issuer"]},
    )
    payload = x402.pay(challenge)
    result = facilitator.verify(payload, challenge.accepts[0])
    assert result.is_valid, result.failure


@pytest.mark.parametrize(
    "asset,amount,extra,expected_reason",
    [
        # Object asset forms — the mistake that is easiest to make from the v2 spec,
        # whose EVM examples show `asset` as an address string but whose XRPL
        # behaviour is undocumented.
        ({"name": "XRP"}, "1000", {"areFeesSponsored": False}, "asset_mismatch"),
        ({"currency": "XRP"}, "1000", {"areFeesSponsored": False}, "asset_mismatch"),
        # Issuer inlined into the asset string instead of extra.issuer.
        ("USD.rsLyZgEU6SkWV52FaEtUngKo8JmUTE5iRb", "0.01", {"areFeesSponsored": False}, "iou_issuer_missing"),
    ],
)
def test_facilitator_still_rejects_wrong_asset_shapes(
    facilitator, wallets, asset, amount, extra, expected_reason
):
    """These reasons are what the offline tests' error messages promise."""
    from xrpl.clients import JsonRpcClient
    from xrpl.wallet import Wallet

    from src.payments.x402 import PaymentPayload
    from src.payments.xrpl_exact import XRPL_TESTNET, XrplExactProvider

    pay_to = wallets["destination"]["address"]
    challenge = make_challenge(pay_to, asset, amount, extra)
    requirements = challenge.accepts[0]

    # The provider refuses these locally, which is the point — so a well-formed
    # payment is signed against a *valid* requirement and then paired with the
    # malformed one, to observe the facilitator's own verdict.
    payer = Wallet.from_seed(wallets["execution"]["seed"])
    provider = XrplExactProvider(payer, JsonRpcClient(TESTNET_RPC), network=XRPL_TESTNET)
    valid = make_challenge(pay_to, "XRP", "1000", {"areFeesSponsored": False})
    signed = provider.build_payload(valid.accepts[0], valid.resource)

    result = facilitator.verify(
        PaymentPayload(challenge.resource, requirements, signed), requirements
    )
    assert not result.is_valid
    assert expected_reason in result.failure, result.failure


def test_last_ledger_sequence_above_the_window_is_rejected(facilitator, wallets):
    """The reason autofill's default is overridden. See xrpl_exact.ledger_window."""
    from dataclasses import replace

    from xrpl.clients import JsonRpcClient
    from xrpl.core.binarycodec import encode
    from xrpl.models.requests import Ledger
    from xrpl.models.transactions import Payment
    from xrpl.transaction import autofill, sign
    from xrpl.wallet import Wallet

    from src.payments.x402 import PaymentPayload

    client = JsonRpcClient(TESTNET_RPC)
    payer = Wallet.from_seed(wallets["execution"]["seed"])
    pay_to = wallets["destination"]["address"]
    current = client.request(Ledger(ledger_index="validated")).result["ledger_index"]

    challenge = make_challenge(pay_to, "XRP", "1000", {"areFeesSponsored": False}, timeout=60)
    requirements = challenge.accepts[0]

    # ledger_window(60) == 14; +40 is comfortably outside it.
    tx = autofill(
        Payment(account=payer.classic_address, destination=pay_to, amount="1000"), client
    )
    tx = replace(tx, last_ledger_sequence=current + 40)
    payload = {"signedTxBlob": encode(sign(tx, payer).to_xrpl())}

    result = facilitator.verify(
        PaymentPayload(challenge.resource, requirements, payload), requirements
    )
    assert not result.is_valid
    assert "lastledgersequence_too_large" in result.failure, result.failure


def test_full_loop_merchant_challenge_to_verified_payment(facilitator, wallets, x402):
    """The whole protocol, both sides, against the real facilitator.

    Merchant publishes a price -> payer receives 402 -> payer signs -> merchant
    verifies -> merchant would settle. Stops before settling: see the module
    docstring.
    """
    from src.payments.merchant import Merchant, PaymentAccepted, PaymentRequiredResult, Price
    from src.payments.x402 import (
        HEADER_PAYMENT_REQUIRED,
        HEADER_PAYMENT_SIGNATURE,
        PaymentRequired,
    )

    resource = "https://api.example.com/orderbook"
    merchant = Merchant(
        facilitator,
        {
            resource: Price(
                amount="1000",
                asset="XRP",
                network="xrpl:1",
                pay_to=wallets["destination"]["address"],
                extra={"areFeesSponsored": False},
                description="XRPL orderbook snapshot",
            )
        },
    )

    # 1. The payer asks with no payment and is refused.
    refused = merchant.require_payment(resource, {})
    assert isinstance(refused, PaymentRequiredResult)

    # 2. The payer reads the terms out of the 402 header — not out of a local price
    #    table. This is the step the earlier implementation skipped entirely.
    challenge = PaymentRequired.from_header(refused.headers[HEADER_PAYMENT_REQUIRED])

    # 3. The payer signs for exactly those terms.
    payload = x402.pay(challenge)

    # 4. The payer retries; the merchant verifies with the real facilitator.
    accepted = merchant.require_payment(
        resource, {HEADER_PAYMENT_SIGNATURE: payload.to_header()}
    )
    assert isinstance(accepted, PaymentAccepted), getattr(accepted, "reason", accepted)
    assert accepted.payer == wallets["execution"]["address"]


def test_full_loop_rejects_a_forged_cheaper_payment(facilitator, wallets, x402):
    """A payer that signs for less than the published price is refused.

    The refusal happens at the merchant, before the facilitator is consulted —
    the facilitator would call this payment valid, because on its own terms it is.
    """
    import base64
    import json

    from src.payments.merchant import Merchant, PaymentRequiredResult, Price
    from src.payments.x402 import (
        HEADER_PAYMENT_REQUIRED,
        HEADER_PAYMENT_SIGNATURE,
        PaymentRequired,
    )

    resource = "https://api.example.com/orderbook"
    price = Price(
        amount="1000",
        asset="XRP",
        network="xrpl:1",
        pay_to=wallets["destination"]["address"],
        extra={"areFeesSponsored": False},
    )
    merchant = Merchant(facilitator, {resource: price})

    # The payer builds a challenge for 1 drop instead of the 1000 demanded, signs a
    # genuinely valid 1-drop payment, then presents it against the real resource.
    cheap = make_challenge(
        wallets["destination"]["address"], "XRP", "1", {"areFeesSponsored": False}
    )
    cheap_payload = x402.pay(cheap)

    # Confirm the forged payment is valid *in isolation* — that is what makes the
    # merchant-side check necessary rather than redundant.
    assert facilitator.verify(cheap_payload, cheap.accepts[0]).is_valid

    published = PaymentRequired.from_header(
        merchant.require_payment(resource, {}).headers[HEADER_PAYMENT_REQUIRED]
    )
    forged = base64.b64encode(
        json.dumps(
            {
                "x402Version": 2,
                "resource": published.resource.to_wire(),
                "accepted": dict(cheap.accepts[0].raw),
                "payload": dict(cheap_payload.payload),
            }
        ).encode()
    ).decode()

    result = merchant.require_payment(resource, {HEADER_PAYMENT_SIGNATURE: forged})
    assert isinstance(result, PaymentRequiredResult)
    assert "do not match" in result.reason


@pytest.mark.parametrize("tx_drops,required_drops", [("1000", "2000"), ("2000", "1000")])
def test_exact_means_exact_in_both_directions(facilitator, wallets, tx_drops, required_drops):
    """Overpaying fails too — this is `exact`, not `at least`."""
    from xrpl.clients import JsonRpcClient
    from xrpl.wallet import Wallet

    from src.payments.x402 import PaymentPayload
    from src.payments.xrpl_exact import XRPL_TESTNET, XrplExactProvider

    pay_to = wallets["destination"]["address"]
    client = JsonRpcClient(TESTNET_RPC)
    provider = XrplExactProvider(
        Wallet.from_seed(wallets["execution"]["seed"]), client, network=XRPL_TESTNET
    )

    paid = make_challenge(pay_to, "XRP", tx_drops, {"areFeesSponsored": False})
    signed = provider.build_payload(paid.accepts[0], paid.resource)

    demanded = make_challenge(pay_to, "XRP", required_drops, {"areFeesSponsored": False})
    result = facilitator.verify(
        PaymentPayload(demanded.resource, demanded.accepts[0], signed), demanded.accepts[0]
    )
    assert not result.is_valid
    assert "amount_mismatch" in result.failure, result.failure

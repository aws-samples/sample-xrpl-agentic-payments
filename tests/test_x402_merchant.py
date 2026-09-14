# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The merchant side: 402 first, verify before working, settle after.

These tests assert the properties that make this access control rather than
metering — chiefly that the server can refuse, and that it refuses the specific
attack a payer-priced protocol invites: echoing back cheaper terms than the ones
the merchant published.

The facilitator is stubbed. What it would tell us (is this signature valid?) is
covered against the real service in test_x402_xrpl_live.py; what is under test here
is the merchant's own decision logic, including the parts that must not depend on
the facilitator's answer.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from src.payments.facilitator import VerifyResult
from src.payments.merchant import (
    Merchant,
    MerchantConfigError,
    PaymentAccepted,
    PaymentRequiredResult,
    Price,
)
from src.payments.x402 import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    X402_VERSION,
    PaymentRequired,
    SettlementResponse,
)

RESOURCE = "https://api.example.com/orderbook"
PAY_TO = "rmzqdaAdZXen3KupfNdcYRRm9P6WGBfKx"
ATTACKER = "rQhWct2Fv4VC4KhLN6ZSBh3rasbmvB39hc"

PRICE = Price(
    amount="1000",
    asset="XRP",
    network="xrpl:1",
    pay_to=PAY_TO,
    extra={"areFeesSponsored": False},
    description="XRPL orderbook snapshot",
)


class StubFacilitator:
    """Answers verify/settle without a network, and records what it was asked."""

    def __init__(self, is_valid=True, invalid_reason=None, settle_success=True):
        self.is_valid = is_valid
        self.invalid_reason = invalid_reason
        self.settle_success = settle_success
        self.verified: list = []
        self.settled: list = []

    def verify(self, payload, requirements):
        self.verified.append((payload, requirements))
        return VerifyResult(
            is_valid=self.is_valid,
            payer="rJLHDUX1SMoMSKt75xWDDJZJ8z1hE7VnZy" if self.is_valid else "",
            invalid_reason=self.invalid_reason,
        )

    def settle(self, payload, requirements):
        self.settled.append((payload, requirements))
        return SettlementResponse(
            success=self.settle_success,
            transaction="A1B2C3" if self.settle_success else "",
            network=requirements.network,
            payer="rJLHDUX1SMoMSKt75xWDDJZJ8z1hE7VnZy",
            error_reason=None if self.settle_success else "insufficient_funds",
        )


@pytest.fixture
def facilitator():
    return StubFacilitator()


@pytest.fixture
def merchant(facilitator):
    return Merchant(facilitator, {RESOURCE: PRICE})


def payment_header(merchant_obj, **overrides) -> str:
    """A well-formed payment, with `accepted` fields overridable to forge terms."""
    challenge = merchant_obj.challenge_for(RESOURCE)
    accepted = dict(challenge.accepts[0].raw)
    accepted.update(overrides)
    return base64.b64encode(
        json.dumps(
            {
                "x402Version": X402_VERSION,
                "resource": challenge.resource.to_wire(),
                "accepted": accepted,
                "payload": {"signedTxBlob": "DEADBEEF"},
                "extensions": {},
            }
        ).encode()
    ).decode()


# ─────────────────────────────────────────────────────────────────────────────
# The server can refuse
# ─────────────────────────────────────────────────────────────────────────────


def test_no_payment_yields_402_and_no_verification(merchant, facilitator):
    result = merchant.require_payment(RESOURCE, {})
    assert isinstance(result, PaymentRequiredResult)
    assert result.status_code == 402
    # Nothing was asked of the facilitator: there was nothing to verify.
    assert facilitator.verified == []


def test_402_carries_the_terms_in_the_payment_required_header(merchant):
    result = merchant.require_payment(RESOURCE, {})
    decoded = json.loads(base64.b64decode(result.headers[HEADER_PAYMENT_REQUIRED]))
    assert decoded["x402Version"] == X402_VERSION
    assert decoded["accepts"][0]["amount"] == "1000"
    assert decoded["accepts"][0]["payTo"] == PAY_TO
    assert decoded["resource"]["url"] == RESOURCE
    # The payer needs to be able to parse its own challenge back.
    assert PaymentRequired.from_header(result.headers[HEADER_PAYMENT_REQUIRED]).accepts


def test_invalid_payment_yields_402_with_the_facilitator_reason():
    facilitator = StubFacilitator(is_valid=False, invalid_reason="invalid_exact_xrpl_payload_expired")
    merchant = Merchant(facilitator, {RESOURCE: PRICE})
    result = merchant.require_payment(RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant)})
    assert isinstance(result, PaymentRequiredResult)
    assert "expired" in result.reason


def test_valid_payment_is_accepted(merchant):
    result = merchant.require_payment(
        RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant)}
    )
    assert isinstance(result, PaymentAccepted)
    assert result.payer == "rJLHDUX1SMoMSKt75xWDDJZJ8z1hE7VnZy"


def test_verification_does_not_settle(merchant, facilitator):
    merchant.require_payment(RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant)})
    # Settlement is the caller's explicit second step, after the work exists.
    assert facilitator.settled == []


# ─────────────────────────────────────────────────────────────────────────────
# Forged terms
# ─────────────────────────────────────────────────────────────────────────────


def test_payer_cannot_downgrade_the_amount(merchant, facilitator):
    """The attack this protocol invites, and the check that stops it.

    A facilitator verifies a payment against the requirement it is *handed*. Hand
    it the payer's own cheaper terms and it will happily call a 1-drop payment
    valid, because it is internally consistent. Only the merchant knows what it
    asked for.
    """
    result = merchant.require_payment(
        RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant, amount="1")}
    )
    assert isinstance(result, PaymentRequiredResult)
    assert "do not match" in result.reason
    assert facilitator.verified == [], "must not reach the facilitator at all"


def test_payer_cannot_redirect_the_recipient(merchant, facilitator):
    result = merchant.require_payment(
        RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant, payTo=ATTACKER)}
    )
    assert isinstance(result, PaymentRequiredResult)
    assert facilitator.verified == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"asset": "USD"},           # cheaper asset
        {"network": "xrpl:2"},      # a network we did not price
        {"scheme": "upto"},         # a scheme with different guarantees
    ],
)
def test_payer_cannot_change_the_other_terms(merchant, overrides):
    result = merchant.require_payment(
        RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant, **overrides)}
    )
    assert isinstance(result, PaymentRequiredResult)


def test_accepted_terms_are_taken_from_the_merchant_not_the_payer(merchant, facilitator):
    # Even on a match, what goes to the facilitator must be the merchant's own
    # requirement object, so any field the payer added cannot influence verification.
    header = payment_header(merchant, someInjectedField="ignore me")
    merchant.require_payment(RESOURCE, {HEADER_PAYMENT_SIGNATURE: header})
    # A matching payment still reaches the facilitator...
    assert len(facilitator.verified) == 1
    _, requirements = facilitator.verified[0]
    assert "someInjectedField" not in requirements.raw


# ─────────────────────────────────────────────────────────────────────────────
# Malformed payments
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"x402Version": 1, "accepted": {}, "payload": {}}, "x402Version"),
        ({"x402Version": X402_VERSION, "payload": {}}, "`accepted`"),
        ({"x402Version": X402_VERSION, "accepted": {}}, "`payload`"),
    ],
)
def test_malformed_payment_is_answered_with_the_challenge(merchant, body, expected):
    header = base64.b64encode(json.dumps(body).encode()).decode()
    result = merchant.require_payment(RESOURCE, {HEADER_PAYMENT_SIGNATURE: header})
    assert isinstance(result, PaymentRequiredResult)
    assert expected in result.reason
    # Still a 402 with the terms attached — the payer's next move is unchanged.
    assert HEADER_PAYMENT_REQUIRED in result.headers


def test_non_base64_payment_is_answered_with_the_challenge(merchant):
    result = merchant.require_payment(RESOURCE, {HEADER_PAYMENT_SIGNATURE: "!!!!"})
    assert isinstance(result, PaymentRequiredResult)


def test_header_lookup_is_case_insensitive(merchant):
    # The casing that arrives depends on the client and any proxy in between.
    for name in ("payment-signature", "Payment-Signature", HEADER_PAYMENT_SIGNATURE):
        result = merchant.require_payment(RESOURCE, {name: payment_header(merchant)})
        assert isinstance(result, PaymentAccepted), name


# ─────────────────────────────────────────────────────────────────────────────
# Settlement
# ─────────────────────────────────────────────────────────────────────────────


def test_settle_submits_the_verified_payment(merchant, facilitator):
    accepted = merchant.require_payment(
        RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant)}
    )
    response = merchant.settle(accepted)
    assert response.success and response.transaction == "A1B2C3"
    assert len(facilitator.settled) == 1


def test_settlement_headers_round_trip(merchant):
    accepted = merchant.require_payment(
        RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant)}
    )
    headers = merchant.settlement_headers(merchant.settle(accepted))
    parsed = SettlementResponse.from_header(headers[HEADER_PAYMENT_RESPONSE])
    assert parsed.success and parsed.transaction == "A1B2C3"


def test_failed_settlement_reports_an_empty_transaction():
    facilitator = StubFacilitator(settle_success=False)
    merchant = Merchant(facilitator, {RESOURCE: PRICE})
    accepted = merchant.require_payment(
        RESOURCE, {HEADER_PAYMENT_SIGNATURE: payment_header(merchant)}
    )
    response = merchant.settle(accepted)
    assert not response.success
    assert response.transaction == ""
    assert response.error_reason == "insufficient_funds"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration guards
# ─────────────────────────────────────────────────────────────────────────────


def test_mainnet_prices_are_refused_by_default(facilitator):
    for network in ("xrpl:0", "eip155:1", "eip155:8453"):
        with pytest.raises(MerchantConfigError, match="not a known testnet"):
            Merchant(facilitator, {RESOURCE: replace(PRICE, network=network)})


def test_mainnet_can_be_enabled_deliberately(facilitator):
    merchant = Merchant(
        facilitator, {RESOURCE: replace(PRICE, network="xrpl:0")}, allow_mainnet=True
    )
    assert merchant.challenge_for(RESOURCE).accepts[0].network == "xrpl:0"


def test_price_without_a_recipient_is_refused(facilitator):
    with pytest.raises(MerchantConfigError, match="no pay_to"):
        Merchant(facilitator, {RESOURCE: replace(PRICE, pay_to="")})


def test_unpriced_resource_is_an_error_not_a_free_pass(merchant):
    # Returning "free" for an unknown resource would make a typo in the price table
    # silently disable payment for that endpoint.
    with pytest.raises(MerchantConfigError, match="no price configured"):
        merchant.require_payment("https://api.example.com/unpriced", {})

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
x402 v2 protocol and the XRPL `exact` scheme.

These tests run offline. The XRPL client and `autofill` are stubbed, because what
is being asserted is the *shape* of what gets signed, and the shape is what the
facilitator rejects payments over.

The constants and rules pinned here were established by probing the reference
facilitator at https://x402.org/facilitator with real signed testnet
transactions; there is no XRPL scheme document in coinbase/x402 to cite instead.
Each assertion names the `invalidReason` it protects against, so if the
facilitator changes its contract these fail with an explanation rather than
leaving a silent incompatibility. The live equivalents are in
test_x402_xrpl_live.py, which is skipped unless X402_LIVE_TESTS=1.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.transactions import Payment
from xrpl.wallet import Wallet

from src.payments import xrpl_exact
from src.payments.facilitator import SupportedKind, VerifyResult
from src.payments.x402 import (
    X402_VERSION,
    NoUsableRequirementError,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettlementResponse,
    X402Client,
    X402Error,
)
from src.payments.xrpl_exact import (
    XRPL_MAINNET,
    XRPL_TESTNET,
    XrplExactProvider,
    XrplPaymentError,
    drops_for_usd,
    ledger_window,
)

PAY_TO = "rmzqdaAdZXen3KupfNdcYRRm9P6WGBfKx"
ISSUER = "rsLyZgEU6SkWV52FaEtUngKo8JmUTE5iRb"
VALIDATED_LEDGER = 20_000_000


@pytest.fixture
def wallet() -> Wallet:
    # A throwaway deterministic testnet key. Never funded, never used to settle:
    # every test here stops before /settle, so no key that touches money is needed.
    return Wallet.create()


class _FakeLedgerResponse:
    def __init__(self, index):
        self.result = {"ledger_index": index}


class _FakeClient:
    """Answers the one request build_payload makes: the validated ledger index."""

    url = "https://stub.invalid"

    def __init__(self, ledger_index=VALIDATED_LEDGER):
        self.ledger_index = ledger_index

    def request(self, _req):
        return _FakeLedgerResponse(self.ledger_index)


@pytest.fixture
def provider(wallet, monkeypatch):
    # autofill would hit the network for Sequence/Fee. Supplying them locally keeps
    # the test offline while leaving the LastLedgerSequence logic under test — that
    # value is deliberately overwritten by build_payload after autofill runs.
    def fake_autofill(tx, _client):
        return replace(tx, sequence=1, fee="10", last_ledger_sequence=VALIDATED_LEDGER + 20)

    monkeypatch.setattr(xrpl_exact, "autofill", fake_autofill)
    return XrplExactProvider(wallet, _FakeClient(), network=XRPL_TESTNET)


def make_requirements(**overrides) -> PaymentRequirements:
    raw = {
        "scheme": "exact",
        "network": XRPL_TESTNET,
        "amount": "1000",
        "asset": "XRP",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "extra": {"areFeesSponsored": False},
    }
    raw.update(overrides)
    return PaymentRequirements.from_wire(raw)


RESOURCE = ResourceInfo(url="https://api.example.com/orderbook", description="snapshot")


def signed_payment(provider: XrplExactProvider, requirements) -> Payment:
    """Decode what the provider actually signed, so its fields can be asserted."""
    from xrpl.core.binarycodec import decode

    blob = provider.build_payload(requirements, RESOURCE)["signedTxBlob"]
    return decode(blob)


# ─────────────────────────────────────────────────────────────────────────────
# Protocol version and parsing
# ─────────────────────────────────────────────────────────────────────────────


def test_v1_challenge_is_refused_not_adapted():
    # A v1 challenge uses maxAmountRequired and expects the X-PAYMENT header back.
    # Silently treating it as v2 would build a payment the server cannot verify, so
    # the parser refuses instead of guessing.
    with pytest.raises(X402Error, match="unsupported x402Version"):
        PaymentRequired.from_wire(
            {"x402Version": 1, "accepts": [{"scheme": "exact", "network": XRPL_TESTNET}]}
        )


def test_challenge_requires_non_empty_accepts():
    for accepts in ([], None, "exact"):
        with pytest.raises(X402Error, match="non-empty array"):
            PaymentRequired.from_wire({"x402Version": X402_VERSION, "accepts": accepts})


def test_missing_required_requirement_field_is_named():
    with pytest.raises(X402Error, match="missing required field 'payTo'"):
        PaymentRequirements.from_wire(
            {"scheme": "exact", "network": XRPL_TESTNET, "amount": "1"}
        )


def test_requirements_keep_unknown_fields_verbatim_for_echo():
    # The facilitator checks the payment against the requirement the MERCHANT
    # published. Re-serialising from parsed fields would drop keys the client does
    # not model, and the mismatch surfaces as an opaque verification failure.
    raw = {
        "scheme": "exact",
        "network": XRPL_TESTNET,
        "amount": "1000",
        "asset": "XRP",
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "extra": {"areFeesSponsored": False},
        "someFutureField": {"nested": [1, 2, 3]},
    }
    requirements = PaymentRequirements.from_wire(raw)
    payload = PaymentPayload(RESOURCE, requirements, {"signedTxBlob": "AB"})
    assert payload.to_wire()["accepted"] == raw


def test_amount_stays_a_string():
    # Atomic units compared for exact equality; a float round-trip could only lose.
    assert make_requirements(amount="100000000000000000001").amount == (
        "100000000000000000001"
    )


def test_header_round_trip():
    challenge = {
        "x402Version": X402_VERSION,
        "resource": {"url": "https://api.example.com/x", "mimeType": "application/json"},
        "accepts": [dict(make_requirements().raw)],
    }
    header = base64.b64encode(json.dumps(challenge).encode()).decode()
    parsed = PaymentRequired.from_header(header)
    assert parsed.accepts[0].network == XRPL_TESTNET
    assert parsed.resource.url == "https://api.example.com/x"


def test_malformed_headers_are_rejected_with_the_header_name():
    for bad in ("", "   ", "!!!not-base64!!!", base64.b64encode(b"[1,2]").decode()):
        with pytest.raises(X402Error, match="PAYMENT-REQUIRED"):
            PaymentRequired.from_header(bad)


def test_settlement_failure_has_empty_transaction():
    # `transaction` is required even on failure, so presence of the key is not
    # evidence that anything settled — only `success` is.
    failed = SettlementResponse.from_wire(
        {"success": False, "errorReason": "insufficient_funds", "transaction": "", "network": XRPL_TESTNET}
    )
    assert not failed.success and failed.transaction == ""


# ─────────────────────────────────────────────────────────────────────────────
# Provider selection
# ─────────────────────────────────────────────────────────────────────────────


def test_network_match_is_exact_so_mainnet_is_never_signed_by_a_testnet_wallet(provider):
    # The same seed is a valid key on every XRPL network. A prefix match on "xrpl:"
    # would let a mainnet challenge be signed with real funds by a provider
    # configured for testnet — the one mistake here that costs money.
    assert provider.supports(make_requirements(network=XRPL_TESTNET))
    assert not provider.supports(make_requirements(network=XRPL_MAINNET))


def test_unsupported_scheme_is_not_signed(provider):
    assert not provider.supports(make_requirements(scheme="upto"))


def test_select_error_names_what_was_offered_and_what_is_configured(provider):
    challenge = PaymentRequired.from_wire(
        {
            "x402Version": X402_VERSION,
            "resource": {"url": "u"},
            "accepts": [dict(make_requirements(network="eip155:84532").raw)],
        }
    )
    client = X402Client([provider])
    with pytest.raises(NoUsableRequirementError) as e:
        client.select(challenge)
    assert "eip155:84532" in str(e.value) and "xrpl-exact" in str(e.value)


def test_first_registered_provider_wins(provider):
    calls = []

    class Recording:
        name = "recording"

        def supports(self, _r):
            return True

        def build_payload(self, _r, _res):
            calls.append("recording")
            return {"signedTxBlob": "00"}

    challenge = PaymentRequired.from_wire(
        {"x402Version": X402_VERSION, "resource": {"url": "u"}, "accepts": [dict(make_requirements().raw)]}
    )
    X402Client([Recording(), provider]).pay(challenge)
    assert calls == ["recording"]


# ─────────────────────────────────────────────────────────────────────────────
# LastLedgerSequence window
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "timeout,expected",
    # ceil(maxTimeoutSeconds / 5) + 2, where 5s is the nominal ledger close time.
    [(10, 4), (30, 8), (60, 14), (61, 15), (300, 62)],
)
def test_ledger_window_formula(timeout, expected):
    assert ledger_window(timeout) == expected


def test_last_ledger_sequence_overrides_autofill_default(provider):
    # autofill sets currentLedger + 20, which exceeds the facilitator's window for
    # every maxTimeoutSeconds below ~90 and is rejected as
    # invalid_exact_xrpl_payload_lastledgersequence_too_large.
    tx = signed_payment(provider, make_requirements(maxTimeoutSeconds=60))
    assert tx["LastLedgerSequence"] == VALIDATED_LEDGER + 14
    assert tx["LastLedgerSequence"] != VALIDATED_LEDGER + 20


def test_longer_timeout_widens_the_window(provider):
    tx = signed_payment(provider, make_requirements(maxTimeoutSeconds=300))
    assert tx["LastLedgerSequence"] == VALIDATED_LEDGER + 62


# ─────────────────────────────────────────────────────────────────────────────
# Amount and asset construction
# ─────────────────────────────────────────────────────────────────────────────


def test_native_xrp_signs_drops_with_no_sendmax(provider):
    tx = signed_payment(provider, make_requirements(asset="XRP", amount="1000"))
    assert tx["Amount"] == "1000"
    assert tx["Destination"] == PAY_TO
    # XRP has no transfer fee, so SendMax buys nothing and is left off.
    assert "SendMax" not in tx


def test_issued_currency_signs_with_sendmax(provider):
    # Required: the facilitator rejects an IOU payment without it as
    # invalid_exact_xrpl_payload_sendmax_required, because without SendMax the
    # ledger may deliver less than Amount after transfer fees.
    tx = signed_payment(
        provider,
        make_requirements(asset="USD", amount="0.01", extra={"areFeesSponsored": False, "issuer": ISSUER}),
    )
    assert tx["Amount"] == {"currency": "USD", "issuer": ISSUER, "value": "0.01"}
    assert tx["SendMax"] == tx["Amount"]


def test_issued_currency_without_issuer_is_refused_locally(provider):
    # Fails as invalid_exact_xrpl_iou_issuer_missing at the facilitator; refusing
    # here names the actual problem.
    with pytest.raises(XrplPaymentError, match="extra.issuer"):
        provider.build_payload(make_requirements(asset="USD", amount="0.01", extra={}), RESOURCE)


@pytest.mark.parametrize("asset", [{"name": "XRP"}, {"currency": "XRP"}, None, 42])
def test_object_assets_are_refused(provider, asset):
    # Every object form is rejected by the facilitator as
    # invalid_exact_xrpl_asset_mismatch; only a bare currency-code string works.
    with pytest.raises(XrplPaymentError, match="currency code string"):
        provider.build_payload(make_requirements(asset=asset), RESOURCE)


@pytest.mark.parametrize("amount", ["0.5", "1e3", "-100", "abc", ""])
def test_non_integer_drops_are_refused(provider, amount):
    with pytest.raises(XrplPaymentError, match="integer drops"):
        provider.build_payload(make_requirements(asset="XRP", amount=amount), RESOURCE)


def test_fee_sponsored_requirements_are_refused(provider):
    # The reference facilitator reports areFeesSponsored=false for xrpl:1 and there
    # is no published construction for the sponsored variant, so guessing one would
    # fail verification for reasons that look like our bug.
    with pytest.raises(XrplPaymentError, match="areFeesSponsored"):
        provider.build_payload(make_requirements(extra={"areFeesSponsored": True}), RESOURCE)


def test_provider_refuses_to_build_for_a_network_it_does_not_serve(provider):
    with pytest.raises(XrplPaymentError, match="cannot sign"):
        provider.build_payload(make_requirements(network=XRPL_MAINNET), RESOURCE)


def test_payload_key_is_signedtxblob(provider):
    # Enforced by the facilitator: "XRPL exact payload requires signedTxBlob".
    payload = provider.build_payload(make_requirements(), RESOURCE)
    assert list(payload) == ["signedTxBlob"]
    bytes.fromhex(payload["signedTxBlob"])  # valid hex


def test_signed_transaction_is_not_submitted(provider):
    # The payer signs; the merchant's facilitator submits. _FakeClient has no
    # submit method at all, so a provider that tried to submit would raise here.
    assert not hasattr(provider.client, "submit")
    provider.build_payload(make_requirements(), RESOURCE)


# ─────────────────────────────────────────────────────────────────────────────
# USD → drops
# ─────────────────────────────────────────────────────────────────────────────


def test_drops_for_usd_rounds_up():
    # `exact` demands equality, so rounding down would build a payment one drop
    # short of the requirement and fail amount_mismatch.
    assert drops_for_usd("0.003", "2.00") == "1500"
    assert drops_for_usd("0.0000001", "2.00") == "1"  # never rounds to zero


def test_drops_for_usd_rejects_nonpositive_price():
    with pytest.raises(ValueError, match="must be positive"):
        drops_for_usd("0.01", "0")


# ─────────────────────────────────────────────────────────────────────────────
# Facilitator response wrappers
# ─────────────────────────────────────────────────────────────────────────────


def test_verify_failure_reason_is_one_line():
    result = VerifyResult.from_wire(
        {"isValid": False, "invalidReason": "invalid_exact_xrpl_asset_mismatch", "invalidMessage": "nope"}
    )
    assert result.failure == "invalid_exact_xrpl_asset_mismatch: nope"
    assert VerifyResult.from_wire({"isValid": False}).failure == "unspecified"


def test_supported_kind_parses_the_xrpl_entry():
    kind = SupportedKind.from_wire(
        {"x402Version": 2, "scheme": "exact", "network": "xrpl:1", "extra": {"areFeesSponsored": False}}
    )
    assert (kind.scheme, kind.network) == ("exact", "xrpl:1")
    assert kind.extra["areFeesSponsored"] is False

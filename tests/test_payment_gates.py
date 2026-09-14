# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the two authorization gates in src/agents/orchestrator.py.

`requires_human_approval` decides whether a payment may be signed without a
human, and `_confirm_settlement` decides whether one that was signed actually
arrived. Both used to be the model's opinion: the threshold was checked inside a
tool the model called with its own restatement of the amount, and settlement was
whatever the Settlement Agent said in prose. Both are now Python, and both fail
closed — these tests pin the closed direction, because an open failure here signs
or confirms a payment that nobody authorised.
"""

import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The orchestrator imports the Strands SDK and boto3 at module scope. They are
# declared in requirements.txt, but the sanctions suite already skips rather than
# errors when the heavier deps are absent, so follow that convention.
pytest.importorskip("strands", reason="Strands Agents SDK not installed")
pytest.importorskip("boto3", reason="boto3 not installed")

from src.agents.orchestrator import (  # noqa: E402
    HUMAN_APPROVAL_THRESHOLD_USD,
    PaymentContext,
    _confirm_settlement,
    _context,
    requires_human_approval,
)
from src.payments.request import PaymentRequest  # noqa: E402

VALID_ADDRESS = "r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg"
OTHER_ADDRESS = "rJ6VE6L87yaVmdyxa9jZFXQpbThqbncQry"
TX_HASH = "A" * 64


def request(amount: str, currency: str = "USD", destination: str = VALID_ADDRESS) -> PaymentRequest:
    return PaymentRequest(
        destination=destination,
        amount=Decimal(amount),
        currency=currency,
        recipient_name="Acme Corp",
        recipient_country="United States",
    )


@contextmanager
def active_context(orderbook=None):
    """Run inside a PaymentContext, optionally with an FX order book recorded.

    `_xrp_to_usd` reads the book off the context, so without one the XRP branch
    would raise instead of exercising the gate.
    """
    ctx = PaymentContext()
    if orderbook is not None:
        ctx.tool_results["get_orderbook"] = orderbook
    token = _context.set(ctx)
    try:
        yield ctx
    finally:
        _context.reset(token)


# ─────────────────────────────────────────────────────────────────────────────
# requires_human_approval — USD and RLUSD are already the threshold's units
# ─────────────────────────────────────────────────────────────────────────────


def test_the_threshold_is_ten_thousand_usd():
    assert HUMAN_APPROVAL_THRESHOLD_USD == Decimal("10000")


@pytest.mark.parametrize("currency", ["USD", "RLUSD"])
def test_below_the_threshold_is_autonomous(currency):
    needs_approval, reason = requires_human_approval(request("9999.99", currency))
    assert needs_approval is False
    assert reason == ""


@pytest.mark.parametrize("currency", ["USD", "RLUSD"])
def test_exactly_the_threshold_requires_approval(currency):
    # The gate is `>=`, not `>`. The documented limit is $10,000, and a payment
    # of exactly $10,000 used to be signed autonomously.
    needs_approval, reason = requires_human_approval(request("10000", currency))
    assert needs_approval is True
    assert "at or above" in reason


@pytest.mark.parametrize("currency", ["USD", "RLUSD"])
def test_above_the_threshold_requires_approval(currency):
    needs_approval, reason = requires_human_approval(request("50000", currency))
    assert needs_approval is True
    assert "$10,000" in reason


def test_one_cent_below_the_threshold_is_autonomous():
    # Decimal, so this is exactly 9999.99 rather than 9999.990000000001.
    assert requires_human_approval(request("9999.99"))[0] is False


# ─────────────────────────────────────────────────────────────────────────────
# requires_human_approval — XRP has to be priced first, and orientation matters
# ─────────────────────────────────────────────────────────────────────────────


def test_xrp_priced_from_an_xrp_over_usd_book():
    # mid is QUOTE per 1 BASE: 0.5 USD per XRP, so 30,000 XRP is $15,000.
    with active_context({"pair": "XRP/USD", "mid": "0.5"}):
        needs_approval, reason = requires_human_approval(request("30000", "XRP"))
    assert needs_approval is True
    assert "$15,000.00" in reason


def test_xrp_priced_from_a_usd_over_xrp_book():
    # The FX agent's default pair. mid is 2 XRP per USD, so 30,000 XRP is
    # $15,000 — the same payment. Multiplying instead of dividing would price it
    # at $60,000 here, and at $9 for a realistic rate, waving it past the gate.
    with active_context({"pair": "USD/XRP", "mid": "2"}):
        needs_approval, reason = requires_human_approval(request("30000", "XRP"))
    assert needs_approval is True
    assert "$15,000.00" in reason


def test_both_book_orientations_agree():
    with active_context({"pair": "XRP/USD", "mid": "0.5"}):
        forward = requires_human_approval(request("30000", "XRP"))
    with active_context({"pair": "USD/XRP", "mid": "2"}):
        inverse = requires_human_approval(request("30000", "XRP"))
    assert forward == inverse


def test_xrp_below_the_threshold_is_autonomous():
    # 10,000 XRP at $0.50 is $5,000 — under the limit despite the large figure.
    with active_context({"pair": "XRP/USD", "mid": "0.5"}):
        assert requires_human_approval(request("10000", "XRP"))[0] is False


def test_xrp_at_exactly_the_threshold_requires_approval():
    with active_context({"pair": "XRP/USD", "mid": "0.5"}):
        assert requires_human_approval(request("20000", "XRP"))[0] is True


@pytest.mark.parametrize(
    "orderbook",
    [
        pytest.param(None, id="no-book-fetched"),
        pytest.param({}, id="empty-book"),
        pytest.param({"pair": "XRP/USD"}, id="book-without-a-mid"),
        pytest.param({"pair": "XRP/USD", "mid": None}, id="null-mid"),
        pytest.param({"pair": "XRP/USD", "mid": "0"}, id="zero-mid"),
        pytest.param({"pair": "XRP/USD", "mid": "-1"}, id="negative-mid"),
        pytest.param({"pair": "XRP/USD", "mid": "NaN"}, id="nan-mid"),
        pytest.param({"pair": "XRP/USD", "mid": "not a number"}, id="unparseable-mid"),
        pytest.param({"pair": "XRP/EUR", "mid": "0.5"}, id="unrecognised-pair"),
        pytest.param({"pair": "", "mid": "0.5"}, id="missing-pair"),
        pytest.param({"pair": "XRP", "mid": "0.5"}, id="malformed-pair"),
    ],
)
def test_unpriceable_xrp_requires_approval(orderbook):
    # An amount that cannot be priced is exactly the case where an autonomous
    # limit must not be assumed to hold. Every one of these is a small XRP
    # payment that would sail through if the gate defaulted to "no approval".
    with active_context(orderbook):
        needs_approval, reason = requires_human_approval(request("1", "XRP"))
    assert needs_approval is True
    assert "could not be priced" in reason


def test_an_unconvertible_currency_requires_approval():
    # parse_payment_request refuses these, so this only fires if the supported
    # currency set grows without the gate being taught the conversion.
    needs_approval, reason = requires_human_approval(request("1", "EUR"))
    assert needs_approval is True
    assert "no USD conversion" in reason


# ─────────────────────────────────────────────────────────────────────────────
# _confirm_settlement
# ─────────────────────────────────────────────────────────────────────────────


def check(**overrides) -> dict:
    base = {
        "hash": TX_HASH,
        "validated": True,
        "transaction_result": "tesSUCCESS",
        "destination": VALID_ADDRESS,
        "amount": {"currency": "USD", "value": "100"},
        "delivered_amount": {"currency": "USD", "value": "100"},
    }
    base.update(overrides)
    return base


def test_a_validated_successful_matching_payment_is_settled():
    settled, note = _confirm_settlement(check(), TX_HASH, request("100"))
    assert settled is True
    assert note == ""


def test_a_lower_case_hash_still_matches():
    settled, _ = _confirm_settlement(check(hash=TX_HASH.lower()), TX_HASH, request("100"))
    assert settled is True


def test_rlusd_is_settled_by_a_usd_issued_amount():
    # RLUSD is issued under the 3-character code "USD" on testnet.
    settled, note = _confirm_settlement(check(), TX_HASH, request("100", "RLUSD"))
    assert settled is True, note


def test_xrp_is_settled_by_a_drops_string():
    settled, note = _confirm_settlement(
        check(amount="100000000", delivered_amount="100000000"), TX_HASH, request("100", "XRP")
    )
    assert settled is True, note


def test_a_missing_delivered_amount_falls_back_to_amount():
    # Older rippled responses and non-partial payments omit delivered_amount.
    settled, note = _confirm_settlement(
        check(delivered_amount=None), TX_HASH, request("100")
    )
    assert settled is True, note


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"validated": False}, "not yet validated", id="not-validated"),
        pytest.param({"validated": "true"}, "not yet validated", id="validated-as-a-string"),
        pytest.param(
            {"transaction_result": "tecUNFUNDED_PAYMENT"},
            "validated but failed",
            id="tec-in-a-validated-ledger",
        ),
        pytest.param(
            {"transaction_result": "tecPATH_DRY"}, "validated but failed", id="path-dry"
        ),
        pytest.param({"hash": "B" * 64}, "not the one this run submitted", id="wrong-hash"),
        pytest.param({"hash": ""}, "not the one this run submitted", id="no-hash"),
        pytest.param(
            {"destination": OTHER_ADDRESS}, "different destination", id="wrong-destination"
        ),
        pytest.param({"error": "txnNotFound"}, "ledger lookup failed", id="lookup-error"),
        pytest.param(
            {"delivered_amount": {"currency": "USD", "value": "1"}},
            "does not match",
            id="partial-payment",
        ),
        pytest.param(
            {"delivered_amount": {"currency": "EUR", "value": "100"}},
            "could not read",
            id="wrong-settled-currency",
        ),
        pytest.param(
            {"delivered_amount": "100000000"},
            "could not read",
            id="drops-string-for-an-issued-currency",
        ),
        pytest.param(
            {"delivered_amount": None, "amount": None},
            "could not read",
            id="no-amount-at-all",
        ),
    ],
)
def test_unconfirmed_settlements_are_not_settled(overrides, expected):
    settled, note = _confirm_settlement(check(**overrides), TX_HASH, request("100"))
    assert settled is False
    assert expected in note


def test_an_empty_check_is_not_settled():
    # The Settlement Agent declining to call the tool at all used to read as
    # "settled" because nothing contradicted it.
    settled, note = _confirm_settlement({}, TX_HASH, request("100"))
    assert settled is False
    assert "never verified" in note


def test_a_partial_xrp_payment_is_not_settled():
    settled, note = _confirm_settlement(
        check(amount="100000000", delivered_amount="1"), TX_HASH, request("100", "XRP")
    )
    assert settled is False
    assert "does not match" in note


def test_an_overpayment_is_not_settled():
    settled, note = _confirm_settlement(
        check(delivered_amount={"currency": "USD", "value": "1000"}), TX_HASH, request("100")
    )
    assert settled is False
    assert "does not match" in note


def test_trailing_zeroes_in_the_settled_value_still_match():
    # Decimal("100.00") == Decimal("100"), unlike the strings.
    settled, note = _confirm_settlement(
        check(delivered_amount={"currency": "USD", "value": "100.00"}), TX_HASH, request("100")
    )
    assert settled is True, note

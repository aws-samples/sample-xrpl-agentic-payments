# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for src/payments/request.py — server-side payment validation.

These cover the cases where a bad request used to become a real payment: a
missing field silently defaulting to 100 USD to a hardcoded address, a float
amount losing precision on the way to integer drops, and NaN slipping past the
approval threshold because every comparison against it is False.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.payments.request import (  # noqa: E402
    MAX_AMOUNT,
    PaymentRequest,
    PaymentRequestError,
    parse_payment_request,
)

# A funded testnet address, used only as a syntactically valid classic address.
VALID_ADDRESS = "r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg"


def payload(**overrides) -> dict:
    base = {
        "destination": VALID_ADDRESS,
        "amount": "100",
        "currency": "USD",
        "recipient_name": "Acme Corp",
        "recipient_country": "United States",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_parses_a_valid_request():
    request = parse_payment_request(payload())
    assert request == PaymentRequest(
        destination=VALID_ADDRESS,
        amount=Decimal("100"),
        currency="USD",
        recipient_name="Acme Corp",
        recipient_country="United States",
    )


def test_currency_is_upper_cased():
    assert parse_payment_request(payload(currency="rlusd")).currency == "RLUSD"


def test_recipient_country_is_optional():
    request = parse_payment_request(payload(recipient_country=""))
    assert request.recipient_country == ""


def test_whitespace_is_stripped():
    request = parse_payment_request(payload(recipient_name="  Acme Corp  "))
    assert request.recipient_name == "Acme Corp"


def test_amount_str_never_uses_scientific_notation():
    # Decimal("1E+3") formats as "1E+3" with str(), which the ledger rejects.
    assert parse_payment_request(payload(amount="1000")).amount_str == "1000"
    assert parse_payment_request(payload(amount="0.000001", currency="XRP")).amount_str == "0.000001"


def test_amount_is_decimal_not_float():
    # float("8.29") * 1_000_000 is 8289999.999999999; Decimal is exact.
    request = parse_payment_request(payload(amount="8.29"))
    assert request.amount == Decimal("8.29")
    assert request.amount * 1_000_000 == Decimal("8290000.00")


# ─────────────────────────────────────────────────────────────────────────────
# No field is ever defaulted
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "field", ["destination", "amount", "currency", "recipient_name"]
)
def test_missing_required_field_is_refused(field):
    data = payload()
    del data[field]
    with pytest.raises(PaymentRequestError):
        parse_payment_request(data)


@pytest.mark.parametrize(
    "field", ["destination", "amount", "currency", "recipient_name"]
)
def test_empty_required_field_is_refused(field):
    with pytest.raises(PaymentRequestError):
        parse_payment_request(payload(**{field: ""}))


def test_a_non_object_payload_is_refused():
    with pytest.raises(PaymentRequestError):
        parse_payment_request("100 USD")  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# Amount
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("amount", ["0", "0.0", "-1", "-0.000001"])
def test_non_positive_amounts_are_refused(amount):
    with pytest.raises(PaymentRequestError, match="greater than zero"):
        parse_payment_request(payload(amount=amount))


@pytest.mark.parametrize("amount", ["NaN", "nan", "Infinity", "-Infinity", "inf"])
def test_non_finite_amounts_are_refused(amount):
    # Every comparison against NaN is False, including `amount >= threshold`,
    # so a NaN amount would pass the human-approval gate silently.
    with pytest.raises(PaymentRequestError, match="finite"):
        parse_payment_request(payload(amount=amount))


def test_amount_above_the_ceiling_is_refused():
    with pytest.raises(PaymentRequestError, match="maximum"):
        parse_payment_request(payload(amount=str(MAX_AMOUNT + 1)))


def test_the_ceiling_itself_is_accepted():
    assert parse_payment_request(payload(amount=str(MAX_AMOUNT))).amount == MAX_AMOUNT


def test_booleans_are_not_amounts():
    # bool subclasses int, so True would otherwise become an amount of 1.
    with pytest.raises(PaymentRequestError):
        parse_payment_request(payload(amount=True))


@pytest.mark.parametrize("amount", ["abc", "1.2.3", "1,000", "one hundred", "0x10"])
def test_unparseable_amounts_are_refused(amount):
    with pytest.raises(PaymentRequestError):
        parse_payment_request(payload(amount=amount))


def test_lists_and_dicts_are_not_amounts():
    for amount in ([100], {"value": 100}, None):
        with pytest.raises(PaymentRequestError):
            parse_payment_request(payload(amount=amount))


def test_xrp_is_limited_to_six_decimal_places():
    # One drop is 1e-6 XRP; a seventh place would be truncated on the way out.
    assert parse_payment_request(payload(amount="1.234567", currency="XRP"))
    with pytest.raises(PaymentRequestError, match="decimal"):
        parse_payment_request(payload(amount="1.2345678", currency="XRP"))


def test_issued_currencies_are_limited_to_fifteen_significant_digits():
    assert parse_payment_request(payload(amount="1.23456789012345"))
    with pytest.raises(PaymentRequestError, match="significant digits"):
        parse_payment_request(payload(amount="1.234567890123456"))


def test_a_float_amount_is_accepted_but_not_via_binary_conversion():
    # str() before Decimal() keeps the decimal literal the client sent rather
        # than importing the float's binary representation.
    assert parse_payment_request(payload(amount=8.29)).amount == Decimal("8.29")


# ─────────────────────────────────────────────────────────────────────────────
# Currency
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("currency", ["EUR", "GBP", "BTC", "USDC", "US"])
def test_unsupported_currencies_are_refused(currency):
    with pytest.raises(PaymentRequestError, match="currency must be one of"):
        parse_payment_request(payload(currency=currency))


# ─────────────────────────────────────────────────────────────────────────────
# Destination
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "destination",
    [
        "nope",
        "r",
        "rrrr",
        # Valid base58 characters, wrong checksum.
        "r3XJToiKCCndKMi1NWmWhBjLBuwmHZimbh",
        # An account id without the r prefix.
        "3XJToiKCCndKMi1NWmWhBjLBuwmHZimbg",
    ],
)
def test_invalid_destinations_are_refused(destination):
    with pytest.raises(PaymentRequestError, match="destination"):
        parse_payment_request(payload(destination=destination))


@pytest.mark.parametrize(
    "x_address",
    [
        # VALID_ADDRESS with destination tag 42, and with no tag.
        "X7thXRcipUsNCRgzsWfQSn7s8PnNWE5DVdraf8UWoDFUGE4",
        "X7thXRcipUsNCRgzsWfQSn7s8PnNWEcbLfRPVUAKzZJhBqi",
    ],
)
def test_x_addresses_are_refused_by_name(x_address):
    # An X-address encodes a destination tag. The tool layer builds a classic
    # Payment and would drop it, delivering to the wrong sub-account — so this
    # is refused with a message that says what to send instead, rather than
    # failing generically as "not a valid address".
    with pytest.raises(PaymentRequestError, match="X-address"):
        parse_payment_request(payload(destination=x_address))


# ─────────────────────────────────────────────────────────────────────────────
# Text fields
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["Acme\nCorp", "Acme\x00Corp", "Acme\tCorp", "Acme\x7fCorp"])
def test_control_characters_are_refused(value):
    # These corrupt the sanctions query, the transaction memo, and any log line
    # that echoes the value back.
    with pytest.raises(PaymentRequestError, match="control characters"):
        parse_payment_request(payload(recipient_name=value))


def test_over_long_names_are_refused():
    with pytest.raises(PaymentRequestError, match="exceeds"):
        parse_payment_request(payload(recipient_name="A" * 201))


def test_non_string_text_fields_are_refused():
    with pytest.raises(PaymentRequestError, match="must be a string"):
        parse_payment_request(payload(recipient_name=42))


def test_to_dict_round_trips_through_validation():
    request = parse_payment_request(payload(amount="8.29"))
    assert parse_payment_request(request.to_dict()) == request

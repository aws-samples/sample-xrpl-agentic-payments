# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — validated payment request.

Every financial field a client sends is parsed and validated HERE, in Python,
before any agent sees it. Nothing in this module trusts model output.

Two rules this module exists to enforce:

1.  Financial fields are never defaulted. The web layer used to fall back to
    100 USD to a hardcoded address when the browser's regex failed to parse an
    instruction, which meant a malformed request silently became a real payment
    to an address the user never named.

2.  Amounts are Decimal, never float. `float("8.29") * 1_000_000` is 8289999.99…,
    and the ledger works in integer drops, so binary floating point cannot be
    allowed anywhere near an amount.

The validated object is what the orchestrator gates on. The Routing Agent also
reports a decision, but that decision is model-mediated and is NOT the
authorisation boundary — see src/agents/orchestrator.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from xrpl.core.addresscodec import is_valid_classic_address, is_valid_xaddress

# RLUSD is the issued-currency ticker; "USD" is how the UI and the tools refer to
# it. XRP is the native asset. Anything else has no issuer configured and no path
# to price it, so it is rejected rather than passed through to the ledger.
SUPPORTED_CURRENCIES = frozenset({"USD", "RLUSD", "XRP"})

# A ceiling that is not the approval threshold: the threshold holds a payment for
# a human, this rejects a request outright. It exists so a fat-fingered or
# injected amount cannot reach the approval queue as a plausible-looking figure.
MAX_AMOUNT = Decimal("1000000")

# One drop is 1e-6 XRP, so more than six decimal places cannot be represented and
# would be silently truncated on the way to the ledger.
XRP_MAX_DECIMAL_PLACES = 6

# Issued currencies carry 15 significant digits on XRPL; beyond that the ledger
# rounds, so accepting more would mean acknowledging an amount we cannot send.
IOU_MAX_SIGNIFICANT_DIGITS = 15

MAX_NAME_LEN = 200
MAX_COUNTRY_LEN = 100


class PaymentRequestError(ValueError):
    """A client-supplied payment request is invalid. Safe to show to the caller."""


@dataclass(frozen=True)
class PaymentRequest:
    """A payment request that has passed every server-side check."""

    destination: str
    amount: Decimal
    currency: str
    recipient_name: str
    recipient_country: str

    @property
    def amount_str(self) -> str:
        """Canonical decimal string for the tool layer (never scientific notation)."""
        return format(self.amount.normalize(), "f")

    def to_dict(self) -> dict:
        return {
            "destination": self.destination,
            "amount": self.amount_str,
            "currency": self.currency,
            "recipient_name": self.recipient_name,
            "recipient_country": self.recipient_country,
        }


def _require_str(payload: dict, field: str, max_len: int, *, required: bool) -> str:
    value = payload.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PaymentRequestError(f"Missing required field: {field}")
        return ""
    if not isinstance(value, str):
        raise PaymentRequestError(f"{field} must be a string, got {type(value).__name__}")
    value = value.strip()
    if len(value) > max_len:
        raise PaymentRequestError(f"{field} exceeds {max_len} characters")
    # Control characters would corrupt the sanctions query, the memo, and any log
    # line that echoes the value back.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise PaymentRequestError(f"{field} contains control characters")
    return value


def _parse_amount(raw, currency: str) -> Decimal:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise PaymentRequestError("Missing required field: amount")
    # bool is a subclass of int, and True would otherwise become 1.
    if isinstance(raw, bool):
        raise PaymentRequestError("amount must be a number or numeric string")
    if not isinstance(raw, (str, int, float, Decimal)):
        raise PaymentRequestError(f"amount must be a number or numeric string, got {type(raw).__name__}")

    try:
        # str() first: Decimal(float) would import the float's binary error.
        amount = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise PaymentRequestError(f"amount is not a valid decimal: {raw!r}") from None

    # NaN and +/-Infinity parse cleanly as Decimal, so they have to be rejected
    # explicitly — every comparison against a NaN is False, including the
    # threshold check, so one would pass the approval gate silently.
    if not amount.is_finite():
        raise PaymentRequestError(f"amount must be finite, got {raw!r}")
    if amount <= 0:
        raise PaymentRequestError(f"amount must be greater than zero, got {raw!r}")
    if amount > MAX_AMOUNT:
        raise PaymentRequestError(f"amount exceeds the maximum of {MAX_AMOUNT:,}")

    exponent = amount.as_tuple().exponent
    decimal_places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if currency == "XRP":
        if decimal_places > XRP_MAX_DECIMAL_PLACES:
            raise PaymentRequestError(
                f"XRP amounts cannot have more than {XRP_MAX_DECIMAL_PLACES} decimal "
                f"places (one drop); got {raw!r}"
            )
    elif len(amount.normalize().as_tuple().digits) > IOU_MAX_SIGNIFICANT_DIGITS:
        raise PaymentRequestError(
            f"issued-currency amounts are limited to {IOU_MAX_SIGNIFICANT_DIGITS} "
            f"significant digits; got {raw!r}"
        )
    return amount


def _parse_destination(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PaymentRequestError("Missing required field: destination")
    destination = raw.strip()
    if is_valid_xaddress(destination):
        # X-addresses encode a destination tag. The tool layer builds a classic
        # Payment and would drop it, sending to the wrong sub-account.
        raise PaymentRequestError(
            "destination is an X-address; supply the classic r-address instead"
        )
    if not is_valid_classic_address(destination):
        raise PaymentRequestError(f"destination is not a valid XRPL address: {destination!r}")
    return destination


def parse_payment_request(payload: dict) -> PaymentRequest:
    """Validate a client payload into a PaymentRequest.

    Raises PaymentRequestError with a caller-safe message. Every financial field
    is required: there is deliberately no default destination, amount or
    currency, because a default here is an unintended payment.
    """
    if not isinstance(payload, dict):
        raise PaymentRequestError("payment request must be an object")

    currency = _require_str(payload, "currency", 10, required=True).upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise PaymentRequestError(
            f"currency must be one of {sorted(SUPPORTED_CURRENCIES)}, got {currency!r}"
        )

    return PaymentRequest(
        destination=_parse_destination(payload.get("destination")),
        amount=_parse_amount(payload.get("amount"), currency),
        currency=currency,
        recipient_name=_require_str(payload, "recipient_name", MAX_NAME_LEN, required=True),
        recipient_country=_require_str(payload, "recipient_country", MAX_COUNTRY_LEN, required=False),
    )

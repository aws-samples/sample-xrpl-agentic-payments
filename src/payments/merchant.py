# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
The merchant side of x402: declare a price, verify before working, then settle.

Everything else in this package is the payer. This module is the counterparty, and
it exists because without it there is no payment protocol at all — only a client
that charges itself. The distinction is worth being blunt about, because the
earlier version of this repository had the second thing and described it as the
first:

    metering        the client looks a price up in its own table, records a
                    charge, and calls the tool. Skipping the charge changes
                    nothing. Nobody can be underpaid because nobody is paid.
    access control  the SERVER names the price, refuses to work until it holds a
                    verified payment, and only then produces the result.

The difference is which side decides, and whether refusal is possible. Only the
second is x402, and it requires the server to be able to say no — which is what
`require_payment()` does.

## Order of operations

    1. request arrives with no PAYMENT-SIGNATURE
       -> 402 + PAYMENT-REQUIRED, no work done
    2. request arrives with PAYMENT-SIGNATURE
       -> facilitator.verify()
          invalid -> 402 again, no work done
          valid   -> DO THE WORK, then facilitator.settle(), then 200 +
                     PAYMENT-RESPONSE

Verify-then-work-then-settle, in that order, and the ordering is the design.
Settling before doing the work would take money for a result that might then fail
to be produced; doing the work before verifying would give it away. Verification is
free and touches no ledger, so it is the right gate, and settlement happens once
the result exists and is about to be handed over.

The residual risk is the narrow window between settling and returning the
response: if the process dies there, the payer has paid and holds nothing. x402
cannot close that window — no two-party protocol without an escrow can — which is
why the `exact` scheme keeps amounts small and `LastLedgerSequence` short. Callers
that cannot tolerate it should reserve rather than settle. The window is named here
so nobody has to rediscover it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .facilitator import Facilitator, VerifyResult
from .x402 import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    X402_VERSION,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettlementResponse,
    X402Error,
    _b64_json_decode,
    _b64_json_encode,
)

logger = logging.getLogger("xrpl_agentic_payments.payments.merchant")

# Networks a merchant in this repository is allowed to price in. Mainnets are
# excluded on purpose: this is sample code, and a misconfigured `pay_to` on a
# mainnet would take real money from whoever ran it. Pass allow_mainnet=True to
# override, deliberately.
TESTNET_NETWORKS = frozenset(
    {"xrpl:1", "xrpl:2", "eip155:84532", "base-sepolia", "solana-devnet", "stellar:testnet", "hedera:testnet"}
)


class MerchantConfigError(ValueError):
    """A price or merchant configuration is unusable."""


@dataclass(frozen=True)
class Price:
    """What one resource costs, on one network.

    `amount` is atomic units as a string, matching the requirement it becomes:
    drops for XRP, base units for a token. It is not a USD figure — converting a
    USD price to a network amount needs a rate, and pinning a rate inside a price
    table silently misprices everything the moment the rate moves. Use
    `xrpl_exact.drops_for_usd` at the point where a rate is actually known.
    """

    amount: str
    asset: Any
    network: str
    pay_to: str
    extra: Mapping[str, Any] = field(default_factory=dict)
    max_timeout_seconds: int = 60
    description: str = ""
    mime_type: str = "application/json"
    scheme: str = "exact"

    def to_requirements(self) -> dict:
        """The `accepts` entry a payer will see."""
        return {
            "scheme": self.scheme,
            "network": self.network,
            "amount": self.amount,
            "asset": self.asset,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True)
class PaymentAccepted:
    """A verified payment. The work may proceed; nothing has settled yet."""

    payload: PaymentPayload
    requirements: PaymentRequirements
    payer: str


@dataclass(frozen=True)
class PaymentRequiredResult:
    """No usable payment. Return this to the caller and do NOT do the work."""

    challenge: PaymentRequired
    reason: str
    status_code: int = 402

    @property
    def headers(self) -> dict[str, str]:
        return {HEADER_PAYMENT_REQUIRED: _b64_json_encode(_challenge_to_wire(self.challenge))}


class Merchant:
    """Prices resources and gates them on verified x402 payments.

    Deliberately framework-agnostic: `require_payment` takes a header mapping and
    returns a decision, so the same logic serves FastAPI, Lambda, or a test with no
    HTTP at all. `src/merchant_app.py` is the thin FastAPI binding.
    """

    def __init__(
        self,
        facilitator: Facilitator,
        prices: Mapping[str, Price],
        *,
        allow_mainnet: bool = False,
    ):
        for resource, price in prices.items():
            if not price.pay_to:
                raise MerchantConfigError(f"price for {resource!r} has no pay_to address")
            if not allow_mainnet and price.network not in TESTNET_NETWORKS:
                raise MerchantConfigError(
                    f"price for {resource!r} uses network {price.network!r}, which is "
                    f"not a known testnet. This is sample code and a wrong pay_to on "
                    f"a mainnet takes real money; pass allow_mainnet=True if that is "
                    f"genuinely intended."
                )
        self.facilitator = facilitator
        self.prices = dict(prices)

    def challenge_for(self, resource: str) -> PaymentRequired:
        """The 402 challenge for a resource."""
        price = self.prices.get(resource)
        if price is None:
            raise MerchantConfigError(f"no price configured for resource {resource!r}")
        return PaymentRequired.from_wire(
            {
                "x402Version": X402_VERSION,
                "error": "payment required",
                "resource": {
                    "url": resource,
                    "description": price.description,
                    "mimeType": price.mime_type,
                },
                "accepts": [price.to_requirements()],
                "extensions": {},
            }
        )

    def require_payment(
        self, resource: str, headers: Mapping[str, str]
    ) -> PaymentAccepted | PaymentRequiredResult:
        """Decide whether work may proceed. Does not settle."""
        challenge = self.challenge_for(resource)
        header_value = _find_header(headers, HEADER_PAYMENT_SIGNATURE)

        if not header_value:
            logger.info("x402: no payment presented for %s, returning 402", resource)
            return PaymentRequiredResult(challenge, "PAYMENT-SIGNATURE header is required")

        try:
            payload, requirements = self._parse_payment(header_value, challenge)
        except X402Error as e:
            # A malformed payment is answered with the challenge again rather than a
            # 400: the payer's next move is the same either way, and repeating the
            # terms is more useful than a parse error.
            logger.warning("x402: unusable payment for %s: %s", resource, e)
            return PaymentRequiredResult(challenge, str(e))

        result = self.facilitator.verify(payload, requirements)
        if not result.is_valid:
            return PaymentRequiredResult(challenge, result.failure)

        logger.info("x402: payment verified for %s from %s", resource, result.payer)
        return PaymentAccepted(payload=payload, requirements=requirements, payer=result.payer)

    def settle(self, accepted: PaymentAccepted) -> SettlementResponse:
        """Submit the verified payment. THIS MOVES MONEY. Call once, after the work."""
        return self.facilitator.settle(accepted.payload, accepted.requirements)

    @staticmethod
    def settlement_headers(response: SettlementResponse) -> dict[str, str]:
        return {
            HEADER_PAYMENT_RESPONSE: _b64_json_encode(
                {
                    "success": response.success,
                    "transaction": response.transaction,
                    "network": response.network,
                    "payer": response.payer,
                    **({"errorReason": response.error_reason} if response.error_reason else {}),
                }
            )
        }

    def _parse_payment(
        self, header_value: str, challenge: PaymentRequired
    ) -> tuple[PaymentPayload, PaymentRequirements]:
        raw = _b64_json_decode(header_value, HEADER_PAYMENT_SIGNATURE)
        version = raw.get("x402Version")
        if version != X402_VERSION:
            raise X402Error(f"payment declares x402Version {version!r}, expected {X402_VERSION}")

        accepted_raw = raw.get("accepted")
        if not isinstance(accepted_raw, Mapping):
            raise X402Error("payment is missing the `accepted` requirement object")
        inner = raw.get("payload")
        if not isinstance(inner, Mapping):
            raise X402Error("payment is missing the scheme `payload` object")

        # The requirement the payer echoed must be one WE published. Without this
        # check a payer could attach its own cheaper terms — say 1 drop instead of
        # 1000 — and the facilitator would verify the payment as internally
        # consistent, because the facilitator checks the payment against the
        # requirement it is handed, not against what this merchant asked for.
        offered = self._match_offered(accepted_raw, challenge)

        payload = PaymentPayload(
            resource=challenge.resource,
            accepted=offered,
            payload=inner,
        )
        return payload, offered

    @staticmethod
    def _match_offered(
        accepted_raw: Mapping[str, Any], challenge: PaymentRequired
    ) -> PaymentRequirements:
        for requirements in challenge.accepts:
            if (
                accepted_raw.get("scheme") == requirements.scheme
                and accepted_raw.get("network") == requirements.network
                and str(accepted_raw.get("amount")) == requirements.amount
                and accepted_raw.get("payTo") == requirements.pay_to
                and accepted_raw.get("asset") == requirements.asset
            ):
                # Return OUR requirement, not the payer's echo, so everything
                # downstream verifies against terms this merchant set.
                return requirements
        raise X402Error(
            "the payment's `accepted` terms do not match any requirement this "
            "merchant published (amount, asset, network, scheme and payTo must all "
            "match)"
        )


def _challenge_to_wire(challenge: PaymentRequired) -> dict:
    return {
        "x402Version": challenge.x402_version,
        "error": challenge.error,
        "resource": challenge.resource.to_wire(),
        "accepts": [dict(r.raw) for r in challenge.accepts],
        "extensions": dict(challenge.extensions or {}),
    }


def _find_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    # HTTP header names are case-insensitive and the casing that arrives depends on
    # the client and any proxy in between, so a direct dict lookup would work in
    # tests and fail in deployment.
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
x402 v2 protocol types and the client-side pay-and-retry loop.

This module is the *protocol*, not a settlement rail. It parses a merchant's 402
challenge, picks a requirement it can satisfy, asks a registered
`SettlementProvider` to turn that requirement into a payment payload, and encodes
the result into the header the merchant expects. Which blockchain the money moves
on is entirely the provider's business.

That split is the whole point. AgentCore Payments settles on EVM and Solana and
cannot sign for XRPL — its API models enumerate
`CryptoWalletNetwork = ['ETHEREUM', 'SOLANA']` and contain no XRPL, XRP or RLUSD
identifier anywhere. So "AgentCore Payments with XRPL" cannot mean AgentCore
signing an XRPL transaction. It means one x402 client with two providers behind
it, each claiming the networks it can actually settle:

    challenge network eip155:*/solana:*  -> AgentCorePaymentsProvider
    challenge network xrpl:*             -> XrplExactProvider

Both produce a `PAYMENT-SIGNATURE` header that is verified by the merchant's
facilitator. Neither pretends to do the other's job.

Version 2, not 1. The field names changed between them and mixing the two is
silently wrong rather than loudly wrong, so they are worth spelling out:

    v1                              v2
    maxAmountRequired               amount
    resource/description/mimeType   hoisted into a single top-level `resource`
      repeated per accepts entry      object shared by every accepts entry
    X-PAYMENT (header)              PAYMENT-SIGNATURE (header)
    payload echoes nothing          payload echoes `resource` and the chosen
                                      requirement as `accepted`

A v1-shaped body sent to a v2 facilitator does not fail cleanly: the reference
facilitator dereferences `paymentPayload.accepted.scheme` and returns
`"Cannot read properties of undefined (reading 'scheme')"`, which reads like a
facilitator bug rather than a client one. Hence `PaymentPayload.to_wire()` below
always emits `accepted`.

References: coinbase/x402 `specs/x402-specification-v2.md` and
`specs/transports-v2/http.md`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger("xrpl_agentic_payments.payments.x402")

X402_VERSION = 2

# The HTTP transport binding carries all three protocol objects in headers as
# base64-encoded JSON; the response body is explicitly "a server implementation
# concern" and the spec's own examples leave it as `{}`. So a client that reads
# the 402 *body* to find the challenge will work against some servers by luck and
# fail against a spec-compliant one — read the header.
HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"


class X402Error(Exception):
    """A challenge could not be parsed, or no provider could satisfy it."""


class NoUsableRequirementError(X402Error):
    """The merchant offered requirements, but no registered provider accepts any.

    Carried separately from X402Error because it is the one failure a caller can
    act on: it means "this rail is not configured", not "the protocol broke".
    """


@dataclass(frozen=True)
class ResourceInfo:
    """The `resource` object: what is being sold, shared by every accepts entry."""

    url: str
    description: str = ""
    mime_type: str = "application/json"

    def to_wire(self) -> dict:
        return {"url": self.url, "description": self.description, "mimeType": self.mime_type}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "ResourceInfo":
        return cls(
            url=str(raw.get("url", "")),
            description=str(raw.get("description", "")),
            mime_type=str(raw.get("mimeType", "application/json")),
        )


@dataclass(frozen=True)
class PaymentRequirements:
    """One way the merchant is willing to be paid (one entry of `accepts`).

    `raw` is the verbatim dict as it arrived. Every consumer of a requirement —
    the facilitator's /verify and /settle, and AgentCore's ProcessPayment — checks
    the payment against the requirement the *merchant* published, so anything
    echoed back has to be byte-for-byte what was received. Re-serialising from
    the parsed fields would drop unknown keys and normalise the known ones, and
    the mismatch surfaces as an opaque verification failure rather than as a
    client bug. Parse for decisions; echo `raw` on the wire.
    """

    scheme: str
    network: str
    amount: str
    asset: Any
    pay_to: str
    max_timeout_seconds: int
    extra: Mapping[str, Any]
    raw: Mapping[str, Any]

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "PaymentRequirements":
        if not isinstance(raw, Mapping):
            raise X402Error(f"accepts entry must be an object, got {type(raw).__name__}")
        try:
            return cls(
                scheme=str(raw["scheme"]),
                network=str(raw["network"]),
                # Kept as a string. Amounts here are atomic units (drops for XRP,
                # base units for a token) and are compared for exact equality by
                # the facilitator, so parsing to float and back could only lose.
                amount=str(raw["amount"]),
                asset=raw.get("asset"),
                pay_to=str(raw["payTo"]),
                max_timeout_seconds=int(raw.get("maxTimeoutSeconds", 60)),
                extra=raw.get("extra") or {},
                raw=raw,
            )
        except KeyError as e:
            raise X402Error(f"accepts entry is missing required field {e.args[0]!r}") from None


@dataclass(frozen=True)
class PaymentRequired:
    """A parsed 402 challenge."""

    x402_version: int
    resource: ResourceInfo
    accepts: Sequence[PaymentRequirements]
    error: Optional[str] = None
    extensions: Mapping[str, Any] = None  # type: ignore[assignment]

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "PaymentRequired":
        if not isinstance(raw, Mapping):
            raise X402Error("PaymentRequired must be an object")
        version = raw.get("x402Version")
        if version != X402_VERSION:
            # Refusing rather than adapting. A v1 challenge needs different field
            # names and a different header on the way back; guessing which the
            # server meant would produce a payment the server cannot verify.
            raise X402Error(
                f"unsupported x402Version {version!r}; this client implements "
                f"version {X402_VERSION} only"
            )
        accepts_raw = raw.get("accepts")
        if not isinstance(accepts_raw, Sequence) or isinstance(accepts_raw, (str, bytes)) or not accepts_raw:
            raise X402Error("PaymentRequired.accepts must be a non-empty array")
        return cls(
            x402_version=version,
            resource=ResourceInfo.from_wire(raw.get("resource") or {}),
            accepts=tuple(PaymentRequirements.from_wire(a) for a in accepts_raw),
            error=raw.get("error"),
            extensions=raw.get("extensions") or {},
        )

    @classmethod
    def from_header(cls, header_value: str) -> "PaymentRequired":
        return cls.from_wire(_b64_json_decode(header_value, HEADER_PAYMENT_REQUIRED))


@dataclass(frozen=True)
class PaymentPayload:
    """What the client sends back: the chosen requirement plus scheme-specific proof."""

    resource: ResourceInfo
    accepted: PaymentRequirements
    payload: Mapping[str, Any]
    x402_version: int = X402_VERSION

    def to_wire(self) -> dict:
        return {
            "x402Version": self.x402_version,
            "resource": self.resource.to_wire(),
            # `accepted.raw`, not a re-serialisation — see PaymentRequirements.raw.
            "accepted": dict(self.accepted.raw),
            "payload": dict(self.payload),
            "extensions": {},
        }

    def to_header(self) -> str:
        return _b64_json_encode(self.to_wire())


@dataclass(frozen=True)
class SettlementResponse:
    """The merchant's `PAYMENT-RESPONSE`: what actually happened on-chain."""

    success: bool
    transaction: str
    network: str
    payer: str = ""
    error_reason: Optional[str] = None

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "SettlementResponse":
        return cls(
            success=bool(raw.get("success")),
            # Required by the spec but "empty string if settlement failed", so a
            # caller must not treat presence of the key as proof of settlement.
            transaction=str(raw.get("transaction", "")),
            network=str(raw.get("network", "")),
            payer=str(raw.get("payer", "")),
            error_reason=raw.get("errorReason"),
        )

    @classmethod
    def from_header(cls, header_value: str) -> "SettlementResponse":
        return cls.from_wire(_b64_json_decode(header_value, HEADER_PAYMENT_RESPONSE))


@runtime_checkable
class SettlementProvider(Protocol):
    """Turns a requirement into a payment payload on one family of networks."""

    name: str

    def supports(self, requirements: PaymentRequirements) -> bool:
        """True if this provider can settle this (scheme, network, asset)."""
        ...

    def build_payload(
        self, requirements: PaymentRequirements, resource: ResourceInfo
    ) -> Mapping[str, Any]:
        """Produce the scheme-specific `payload` object, or raise on failure."""
        ...


class X402Client:
    """Selects a requirement, pays it through the provider that supports it.

    Providers are consulted in registration order, so register the cheapest or
    most-preferred rail first. Selection deliberately does NOT compare prices
    across networks: `amount` is denominated in each network's own atomic units
    (drops here, token base units there), so the integers are not commensurable
    and picking "the smallest number" would be arbitrary.
    """

    def __init__(self, providers: Sequence[SettlementProvider] = ()):
        self._providers: list[SettlementProvider] = list(providers)

    def register(self, provider: SettlementProvider) -> None:
        self._providers.append(provider)

    @property
    def providers(self) -> tuple[SettlementProvider, ...]:
        return tuple(self._providers)

    def select(
        self, challenge: PaymentRequired
    ) -> tuple[PaymentRequirements, SettlementProvider]:
        for requirements in challenge.accepts:
            for provider in self._providers:
                if provider.supports(requirements):
                    return requirements, provider
        offered = sorted({f"{r.scheme}/{r.network}" for r in challenge.accepts})
        configured = sorted(p.name for p in self._providers) or ["<none>"]
        raise NoUsableRequirementError(
            f"merchant accepts {offered}, but the configured providers "
            f"{configured} support none of them"
        )

    def pay(self, challenge: PaymentRequired) -> PaymentPayload:
        """Build the `PAYMENT-SIGNATURE` payload for a challenge."""
        requirements, provider = self.select(challenge)
        logger.info(
            "x402: paying %s %s on %s via %s",
            requirements.amount,
            requirements.asset,
            requirements.network,
            provider.name,
        )
        payload = provider.build_payload(requirements, challenge.resource)
        return PaymentPayload(
            resource=challenge.resource, accepted=requirements, payload=payload
        )


def _b64_json_encode(obj: Any) -> str:
    # separators without spaces: the header value is transmitted verbatim and
    # padding bytes buy nothing.
    return base64.b64encode(
        json.dumps(obj, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _b64_json_decode(header_value: str, header_name: str) -> Mapping[str, Any]:
    if not isinstance(header_value, str) or not header_value.strip():
        raise X402Error(f"{header_name} header is empty")
    try:
        # validate=False (the default) tolerates the whitespace that some proxies
        # fold into long header values.
        decoded = base64.b64decode(header_value, validate=False)
    except (binascii.Error, ValueError) as e:
        raise X402Error(f"{header_name} header is not valid base64: {e}") from None
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as e:
        raise X402Error(f"{header_name} header is not valid JSON: {e}") from None
    if not isinstance(parsed, Mapping):
        raise X402Error(f"{header_name} header must decode to an object")
    return parsed

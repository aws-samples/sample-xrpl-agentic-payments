# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Client for an x402 facilitator: /supported, /verify, /settle.

A facilitator is the party that checks a payment payload against a requirement
and, separately, submits it. The split is the safety property this module exists
to preserve, so it is stated in one place:

    verify()  reads. It does not touch the ledger. Free, idempotent, repeatable.
    settle()  writes. It submits the payer's pre-signed transaction and MOVES
              MONEY. Exactly once, not idempotent.

`verify` returning isValid=True is therefore not a payment — nothing has happened
on-chain — and `settle` is the only call in this codebase that spends. Anything
exploratory must stop at `verify`.

Note also who calls what. In a full deployment the *merchant* calls settle after
receiving the payer's PAYMENT-SIGNATURE header; the payer only signs. settle() is
implemented here because this repository contains both sides of the demo, not
because a payer agent should normally be settling its own payments.

Uses urllib rather than adding an HTTP dependency: nothing in the repo currently
imports requests or httpx directly (see the httpx note in requirements.txt), and
a sample is a poor place to grow the dependency surface for three POSTs.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .x402 import (
    X402_VERSION,
    PaymentPayload,
    PaymentRequirements,
    SettlementResponse,
    X402Error,
)

logger = logging.getLogger("xrpl_agentic_payments.payments.facilitator")

# The reference facilitator, which serves testnets only. It advertises xrpl:1
# among its supported kinds, which is what makes the XRPL rail in this repo work
# without operating a facilitator.
DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"

# x402.org sits behind a CDN that answers Python's default
# "Python-urllib/3.x" User-Agent with HTTP 403 (error code 1010) before the
# request ever reaches the facilitator. The failure looks like an outage rather
# than a blocked client, so the header is set explicitly and must stay set.
_USER_AGENT = "xrpl-agentic-payments/1.0 (+https://github.com/aws-samples)"

DEFAULT_TIMEOUT_SECONDS = 30


class FacilitatorError(X402Error):
    """The facilitator could not be reached, or returned something unusable."""


@dataclass(frozen=True)
class SupportedKind:
    """One (scheme, network) pair a facilitator will handle."""

    x402_version: int
    scheme: str
    network: str
    extra: Mapping[str, Any]

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "SupportedKind":
        return cls(
            x402_version=int(raw.get("x402Version", X402_VERSION)),
            scheme=str(raw.get("scheme", "")),
            network=str(raw.get("network", "")),
            extra=raw.get("extra") or {},
        )


@dataclass(frozen=True)
class VerifyResult:
    """The /verify answer. `is_valid` means "would settle", NOT "has settled"."""

    is_valid: bool
    payer: str = ""
    invalid_reason: Optional[str] = None
    invalid_message: Optional[str] = None

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "VerifyResult":
        return cls(
            is_valid=bool(raw.get("isValid")),
            payer=str(raw.get("payer", "")),
            invalid_reason=raw.get("invalidReason"),
            invalid_message=raw.get("invalidMessage"),
        )

    @property
    def failure(self) -> str:
        """A one-line reason suitable for logs and error messages."""
        parts = [p for p in (self.invalid_reason, self.invalid_message) if p]
        return ": ".join(parts) or "unspecified"


class Facilitator:
    """HTTP client for one facilitator."""

    def __init__(
        self,
        base_url: str = DEFAULT_FACILITATOR_URL,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def supported(self) -> tuple[SupportedKind, ...]:
        """Which (scheme, network) pairs this facilitator will handle."""
        body = self._get("/supported")
        kinds = body.get("kinds")
        if not isinstance(kinds, Sequence):
            raise FacilitatorError("/supported did not return a `kinds` array")
        return tuple(SupportedKind.from_wire(k) for k in kinds)

    def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResult:
        """Check a payment against a requirement. Does not move money."""
        result = VerifyResult.from_wire(
            self._post("/verify", self._envelope(payload, requirements))
        )
        if result.is_valid:
            logger.info("facilitator verified payment from %s", result.payer)
        else:
            logger.warning("facilitator rejected payment: %s", result.failure)
        return result

    def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettlementResponse:
        """Submit the payment. THIS SPENDS MONEY and is not idempotent."""
        logger.warning(
            "settling x402 payment of %s %s on %s — this submits to the ledger",
            requirements.amount,
            requirements.asset,
            requirements.network,
        )
        response = SettlementResponse.from_wire(
            self._post("/settle", self._envelope(payload, requirements))
        )
        if response.success:
            logger.info("settled: tx=%s network=%s", response.transaction, response.network)
        else:
            logger.error("settlement failed: %s", response.error_reason)
        return response

    @staticmethod
    def _envelope(
        payload: PaymentPayload, requirements: PaymentRequirements
    ) -> dict:
        # /verify and /settle share this shape. `paymentRequirements` is the
        # merchant's verbatim requirement — see PaymentRequirements.raw for why it
        # is not rebuilt from parsed fields.
        return {
            "x402Version": X402_VERSION,
            "paymentPayload": payload.to_wire(),
            "paymentRequirements": dict(requirements.raw),
        }

    def _get(self, path: str) -> Mapping[str, Any]:
        return self._request(path, data=None)

    def _post(self, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request(path, data=json.dumps(body).encode("utf-8"))

    def _request(self, path: str, data: Optional[bytes]) -> Mapping[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            # A 4xx from /verify is a protocol answer, not a transport failure, but
            # this facilitator returns those as 200 with isValid=false; a real 4xx
            # means the envelope itself was malformed. Include the body — the
            # message is where the actionable detail lives.
            raise FacilitatorError(f"{url} returned HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise FacilitatorError(f"could not reach {url}: {e.reason}") from None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise FacilitatorError(
                f"{url} returned non-JSON ({e}): {raw[:200]!r}"
            ) from None
        if not isinstance(parsed, Mapping):
            raise FacilitatorError(f"{url} returned {type(parsed).__name__}, expected an object")
        return parsed

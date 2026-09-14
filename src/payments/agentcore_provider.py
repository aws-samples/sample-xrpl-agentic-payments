# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
AgentCore Payments as an x402 settlement provider.

This is the second rail. It settles the networks AgentCore Payments can actually
sign for — EVM and Solana — and refuses everything else, XRPL included.

That refusal is not a limitation of this file; it is a property of the service.
The API models enumerate:

    CryptoWalletNetwork  = ['ETHEREUM', 'SOLANA']
    BlockchainChainId    = ['BASE', 'BASE_SEPOLIA', 'ETHEREUM', 'SOLANA', 'SOLANA_DEVNET']
    Currency             = ['USD']
    PaymentConnectorType = ['CoinbaseCDP', 'StripePrivy']

and contain no XRPL, XRP or RLUSD identifier anywhere in either the data-plane or
control-plane model. AgentCore Payments cannot hold XRPL keys, so it cannot
produce an XRPL signature, so `supports()` below returns False for `xrpl:*` and
`src.payments.xrpl_exact` handles those instead. Registering both providers on one
`X402Client` is what "AgentCore Payments and XRPL in one agent" means here — one
protocol, two rails, each doing only what it can.

## What AgentCore does for you

You forward the merchant's 402 requirements verbatim; AgentCore checks the
session's spending limits, signs with the managed wallet, and returns a proof. The
value is the part that is genuinely awkward to build: custody, per-session and
per-agent spend limits enforced server-side across concurrent agents, and an audit
trail. It supports x402 v1, v2 and MPP.

## Verification status — read before trusting this file

The XRPL provider in this package was developed against a live facilitator and
every rule in it corresponds to an observed rejection. This file is NOT verified
that way. It is written against the API model (authoritative: shapes and field
names are machine-read from botocore) and the service documentation
(authoritative for semantics, but prose).

The gap is deliberate and unavoidable here: exercising ProcessPayment needs an
active AWS Marketplace subscription to a wallet connector, a funded wallet, and an
end user visiting a delegation URL to authorise signing. Those are human steps.
Until they are done, this code path cannot be executed at all, and the specific
thing that stays unconfirmed is the exact shape of
`paymentOutput.cryptoX402.payload` — the docs call it "the signed proof" and say to
attach it to the header, without stating whether it is the complete PaymentPayload
or only the inner scheme payload. `extract_proof()` handles both and says which it
found, rather than assuming.

## What this file will not do

It will not fall back. An earlier version of this integration caught every
exception from ProcessPayment, logged a "simulated" charge, and invoked the tool
anyway — so an unprovisioned deployment looked identical to a working one while
paying nobody. A provider that cannot pay raises. Deciding to proceed unpaid is a
caller's decision to make explicitly, not a default buried in a library.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping, Optional

from .x402 import (
    PaymentPayload,
    PaymentRequirements,
    ResourceInfo,
    X402Error,
)

logger = logging.getLogger("xrpl_agentic_payments.payments.agentcore")

# x402 network identifiers AgentCore Payments can settle, mapped to the
# BlockchainChainId the service knows them by. Both CAIP-2 (v2) and the legacy
# bare names (v1, and what the service documentation's examples use) are accepted,
# because a merchant may publish either and the client does not control which.
SUPPORTED_NETWORKS: Mapping[str, str] = {
    "eip155:8453": "BASE",
    "eip155:84532": "BASE_SEPOLIA",
    "eip155:1": "ETHEREUM",
    "base": "BASE",
    "base-sepolia": "BASE_SEPOLIA",
    "ethereum": "ETHEREUM",
    "solana-devnet": "SOLANA_DEVNET",
    "solana": "SOLANA",
}

# Schemes AgentCore states it handles. `exact` is the baseline; `upto` was added
# alongside MPP support.
SUPPORTED_SCHEMES = frozenset({"exact", "upto"})


class AgentCorePaymentError(X402Error):
    """AgentCore Payments could not produce a proof for this requirement."""


def is_solana(network: str) -> bool:
    return network.startswith("solana")


class AgentCorePaymentsProvider:
    """Signs x402 payments through AgentCore Payments' managed wallet.

    Args:
        client: A `bedrock-agentcore` (data plane) boto3 client.
        payment_manager_arn: The payment manager to charge against.
        payment_session_id: An open session. Its limits are what actually bound
            spending — this class does not re-implement budget checks, because a
            client-side check is advisory and the server-side one is not.
        payment_instrument_id: The wallet to sign with.
        x402_version: Which x402 version to declare to AgentCore. Must match the
            version the merchant's challenge used; the field names inside the
            forwarded payload differ between them.
    """

    def __init__(
        self,
        client,
        payment_manager_arn: str,
        payment_session_id: str,
        payment_instrument_id: str,
        user_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        x402_version: int = 1,
        name: str = "agentcore-payments",
    ):
        if not payment_manager_arn:
            raise ValueError("payment_manager_arn is required")
        if not payment_session_id:
            raise ValueError(
                "payment_session_id is required. Create one with "
                "CreatePaymentSession and read it from "
                "response['paymentSession']['paymentSessionId'] — the id is nested "
                "one level down, and reading it off the response root yields None."
            )
        if not payment_instrument_id:
            raise ValueError("payment_instrument_id is required")
        self.client = client
        self.payment_manager_arn = payment_manager_arn
        self.payment_session_id = payment_session_id
        self.payment_instrument_id = payment_instrument_id
        self.user_id = user_id
        self.agent_name = agent_name
        self.x402_version = x402_version
        self.name = name

    def supports(self, requirements: PaymentRequirements) -> bool:
        return (
            requirements.scheme in SUPPORTED_SCHEMES
            and requirements.network in SUPPORTED_NETWORKS
        )

    def build_payload(
        self, requirements: PaymentRequirements, resource: ResourceInfo
    ) -> Mapping[str, Any]:
        """Return the scheme payload from AgentCore's proof.

        Prefer `build_header()`: AgentCore returns a proof intended to be attached
        to the header whole, and unwrapping it here to have `X402Client` re-wrap it
        risks discarding fields the merchant expects.
        """
        proof = self._process_payment(requirements, resource)
        inner = proof.get("payload") if isinstance(proof, Mapping) else None
        if isinstance(inner, Mapping):
            # A complete PaymentPayload came back; hand X402Client the inner part so
            # it can rebuild an equivalent envelope.
            return inner
        if isinstance(proof, Mapping):
            # Already just the scheme payload.
            return proof
        raise AgentCorePaymentError(
            f"AgentCore returned a proof of type {type(proof).__name__}, expected an object"
        )

    def build_header(
        self, requirements: PaymentRequirements, resource: ResourceInfo
    ) -> str:
        """Produce the header value to send, preserving AgentCore's proof verbatim."""
        import base64
        import json

        proof = self._process_payment(requirements, resource)
        if _looks_like_payment_payload(proof):
            # Send exactly what AgentCore signed. Re-serialising a signed structure
            # is how signatures get invalidated.
            return base64.b64encode(
                json.dumps(proof, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
        return PaymentPayload(resource, requirements, proof).to_header()

    def _process_payment(
        self, requirements: PaymentRequirements, resource: ResourceInfo
    ) -> Mapping[str, Any]:
        if not self.supports(requirements):
            raise AgentCorePaymentError(
                f"AgentCore Payments cannot settle {requirements.scheme}/"
                f"{requirements.network}. It signs for "
                f"{sorted(set(SUPPORTED_NETWORKS.values()))} only; its API model has "
                f"no XRPL network, so xrpl:* requirements must go to "
                f"src.payments.xrpl_exact instead."
            )

        # The merchant's requirement, verbatim. AgentCore reads the amount, asset,
        # recipient and network out of this document to build the transaction, so a
        # payload with our own field names — an earlier version of this integration
        # sent {"tool", "amount", "currency", "recipient"} — carries none of the
        # information the service needs. It does not error: `payload` is an
        # unvalidated document type, so the wrong shape is accepted and produces a
        # proof the merchant cannot verify.
        payload = dict(requirements.raw)
        if self.x402_version == 1 and "resource" not in payload:
            # v1 carries the resource URL inside each requirement; v2 hoists it to a
            # shared top-level object. Restore it when down-converting, or AgentCore
            # signs a proof that names no resource.
            payload["resource"] = resource.url
            payload.setdefault("description", resource.description)
            payload.setdefault("mimeType", resource.mime_type)

        request: dict[str, Any] = {
            "paymentManagerArn": self.payment_manager_arn,
            "paymentSessionId": self.payment_session_id,
            "paymentInstrumentId": self.payment_instrument_id,
            "paymentType": "CRYPTO_X402",
            "paymentInput": {
                "cryptoX402": {
                    "version": str(self.x402_version),
                    "payload": payload,
                }
            },
            # ProcessPayment moves money, so it must not be retried blindly. boto3
            # retries throttles and 5xx by default; without an idempotency token a
            # retried request is a second payment.
            "clientToken": str(uuid.uuid4()),
        }
        if self.user_id:
            request["userId"] = self.user_id
        if self.agent_name:
            request["agentName"] = self.agent_name

        logger.info(
            "AgentCore ProcessPayment: %s %s on %s (session %s)",
            requirements.amount,
            requirements.asset,
            requirements.network,
            self.payment_session_id,
        )

        try:
            response = self.client.process_payment(**request)
        except Exception as e:
            # Deliberately not swallowed — see the module docstring. A failed
            # payment must not look like a successful one.
            raise AgentCorePaymentError(
                f"ProcessPayment failed for {requirements.amount} "
                f"{requirements.asset} on {requirements.network}: {e}"
            ) from e

        status = response.get("status")
        # The response is required to carry a status; anything other than a
        # succeeded state means no proof was produced, and treating the payload as
        # valid would send an unsigned or partial proof to the merchant.
        if status not in ("COMPLETED", "SUCCEEDED", "SUCCESS"):
            raise AgentCorePaymentError(
                f"ProcessPayment returned status {status!r}, not a success state; "
                f"no usable payment proof (processPaymentId="
                f"{response.get('processPaymentId')})"
            )

        proof = (response.get("paymentOutput") or {}).get("cryptoX402") or {}
        inner = proof.get("payload")
        if not isinstance(inner, Mapping):
            raise AgentCorePaymentError(
                "ProcessPayment succeeded but paymentOutput.cryptoX402.payload is "
                f"{type(inner).__name__}, not an object; nothing to send to the merchant"
            )
        logger.info(
            "AgentCore produced a payment proof (processPaymentId=%s, version=%s)",
            response.get("processPaymentId"),
            proof.get("version"),
        )
        return inner


def _looks_like_payment_payload(proof: Any) -> bool:
    """True if `proof` is already a complete PaymentPayload rather than a bare payload.

    Needed because the documentation does not say which of the two
    `paymentOutput.cryptoX402.payload` contains, and the difference decides whether
    to wrap it. Detected structurally: a complete payload declares its version and
    carries a nested `payload`.
    """
    if not isinstance(proof, Mapping):
        return False
    return "payload" in proof and ("x402Version" in proof or "scheme" in proof)

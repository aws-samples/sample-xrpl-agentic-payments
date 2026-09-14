# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
AgentCore Payments as an x402 provider.

The live service cannot be exercised from a test — ProcessPayment needs a
Marketplace subscription to a wallet connector, a funded wallet, and a human
visiting a delegation URL. So these tests stub the boto3 client and assert the
things that were actually wrong before, and that a stub can prove:

  - the request AgentCore receives carries the MERCHANT's requirement verbatim,
    not our own invented field names;
  - a failure raises instead of degrading into a "simulated" charge;
  - XRPL is refused, because the service cannot sign for it.

What these tests cannot prove is the shape of the proof that comes back. See the
verification-status section of src/payments/agentcore_provider.py.
"""

from __future__ import annotations

import pytest

from src.payments.agentcore_provider import (
    SUPPORTED_NETWORKS,
    AgentCorePaymentError,
    AgentCorePaymentsProvider,
)
from src.payments.x402 import PaymentRequirements, ResourceInfo, X402Client
from src.payments.xrpl_exact import XRPL_TESTNET

MANAGER_ARN = "arn:aws:bedrock-agentcore:us-west-2:123456789012:payment-manager/pm-1"
SESSION_ID = "ps-abc123"
INSTRUMENT_ID = "pi-def456"

RESOURCE = ResourceInfo(
    url="https://api.example.com/screen", description="Sanctions screening"
)

# The shape the service documentation uses for an EVM `exact` requirement.
EVM_RAW = {
    "scheme": "exact",
    "network": "base-sepolia",
    "maxAmountRequired": "5000",
    "amount": "5000",
    "resource": "https://api.example.com/screen",
    "description": "Sanctions screening",
    "mimeType": "application/json",
    "payTo": "0x2096930cA4A0Fd0b4Cd0Bd6B0B4Cd0Bd6B0B4Cd0",
    "maxTimeoutSeconds": 300,
    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "extra": {"name": "USDC", "version": "2"},
}

PROOF_PAYLOAD = {
    "signature": "0x2d6a75",
    "authorization": {"from": "0x857b06", "to": "0x209693", "value": "5000"},
}


def evm_requirements(**overrides) -> PaymentRequirements:
    raw = dict(EVM_RAW)
    raw.update(overrides)
    return PaymentRequirements.from_wire(raw)


def xrpl_requirements() -> PaymentRequirements:
    return PaymentRequirements.from_wire(
        {
            "scheme": "exact",
            "network": XRPL_TESTNET,
            "amount": "1000",
            "asset": "XRP",
            "payTo": "rmzqdaAdZXen3KupfNdcYRRm9P6WGBfKx",
            "maxTimeoutSeconds": 60,
            "extra": {"areFeesSponsored": False},
        }
    )


class StubClient:
    """Records ProcessPayment calls and returns a canned response."""

    def __init__(self, status="COMPLETED", payload=None, raises=None):
        self.status = status
        self.payload = PROOF_PAYLOAD if payload is None else payload
        self.raises = raises
        self.calls: list[dict] = []

    def process_payment(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return {
            "processPaymentId": "pp-1",
            "paymentManagerArn": MANAGER_ARN,
            "paymentSessionId": SESSION_ID,
            "paymentInstrumentId": INSTRUMENT_ID,
            "paymentType": "CRYPTO_X402",
            "status": self.status,
            "paymentOutput": {"cryptoX402": {"version": "1", "payload": self.payload}},
        }


def make_provider(client=None, **kwargs):
    return AgentCorePaymentsProvider(
        client=client or StubClient(),
        payment_manager_arn=MANAGER_ARN,
        payment_session_id=SESSION_ID,
        payment_instrument_id=INSTRUMENT_ID,
        **kwargs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Network support — the boundary between the two rails
# ─────────────────────────────────────────────────────────────────────────────


def test_xrpl_is_refused_because_agentcore_cannot_sign_it():
    # CryptoWalletNetwork = ['ETHEREUM','SOLANA']; the API model contains no XRPL
    # identifier at all. This is the reason the repo has a second provider.
    provider = make_provider()
    assert not provider.supports(xrpl_requirements())
    with pytest.raises(AgentCorePaymentError, match="xrpl"):
        provider.build_payload(xrpl_requirements(), RESOURCE)


def test_refusal_does_not_call_the_service():
    client = StubClient()
    with pytest.raises(AgentCorePaymentError):
        make_provider(client).build_payload(xrpl_requirements(), RESOURCE)
    assert client.calls == []


@pytest.mark.parametrize("network", sorted(SUPPORTED_NETWORKS))
def test_evm_and_solana_networks_are_supported(network):
    # Both CAIP-2 (v2) and legacy bare names (v1) are accepted: the merchant
    # chooses which it publishes and the client does not control that.
    assert make_provider().supports(evm_requirements(network=network))


def test_unknown_scheme_is_refused():
    assert not make_provider().supports(evm_requirements(scheme="batch-settlement"))


def test_two_providers_split_traffic_by_network():
    """The integration this repo is demonstrating, asserted directly."""

    class FakeXrpl:
        name = "xrpl-exact"

        def supports(self, r):
            return r.network == XRPL_TESTNET

        def build_payload(self, r, res):
            return {"signedTxBlob": "AB"}

    agentcore = make_provider()
    client = X402Client([agentcore, FakeXrpl()])
    assert client.select(evm_requirements_challenge())[1] is agentcore
    assert client.select(xrpl_challenge())[1].name == "xrpl-exact"


def evm_requirements_challenge():
    from src.payments.x402 import X402_VERSION, PaymentRequired

    return PaymentRequired.from_wire(
        {"x402Version": X402_VERSION, "resource": {"url": "u"}, "accepts": [dict(EVM_RAW)]}
    )


def xrpl_challenge():
    from src.payments.x402 import X402_VERSION, PaymentRequired

    return PaymentRequired.from_wire(
        {
            "x402Version": X402_VERSION,
            "resource": {"url": "u"},
            "accepts": [dict(xrpl_requirements().raw)],
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# The request AgentCore receives
# ─────────────────────────────────────────────────────────────────────────────


def test_merchant_requirement_is_forwarded_verbatim():
    # The bug this asserts against: sending {"tool","amount","currency","recipient"},
    # which shares no field names with an x402 requirement. It did not error,
    # because `payload` is an unvalidated document type — it produced a proof the
    # merchant could not verify.
    client = StubClient()
    make_provider(client).build_payload(evm_requirements(), RESOURCE)
    sent = client.calls[0]["paymentInput"]["cryptoX402"]["payload"]
    assert sent == EVM_RAW
    assert "tool" not in sent and "currency" not in sent


def test_payment_type_and_version_are_declared():
    client = StubClient()
    make_provider(client, x402_version=2).build_payload(evm_requirements(), RESOURCE)
    assert client.calls[0]["paymentType"] == "CRYPTO_X402"
    assert client.calls[0]["paymentInput"]["cryptoX402"]["version"] == "2"


def test_client_token_is_present_and_unique_per_call():
    # ProcessPayment moves money. boto3 retries throttles and 5xx by default, so
    # without an idempotency token a retry is a second payment.
    client = StubClient()
    provider = make_provider(client)
    provider.build_payload(evm_requirements(), RESOURCE)
    provider.build_payload(evm_requirements(), RESOURCE)
    tokens = [c["clientToken"] for c in client.calls]
    assert all(tokens) and len(set(tokens)) == 2


def test_v1_downconversion_restores_the_resource_url():
    # v2 hoists resource/description/mimeType out of each requirement into a shared
    # top-level object; v1 expects them inline. Forwarding a v2 requirement as v1
    # without restoring them signs a proof that names no resource.
    v2_raw = {k: v for k, v in EVM_RAW.items() if k not in ("resource", "description", "mimeType")}
    client = StubClient()
    make_provider(client, x402_version=1).build_payload(
        PaymentRequirements.from_wire(v2_raw), RESOURCE
    )
    sent = client.calls[0]["paymentInput"]["cryptoX402"]["payload"]
    assert sent["resource"] == RESOURCE.url
    assert sent["mimeType"] == "application/json"


def test_v2_does_not_inject_v1_fields():
    v2_raw = {k: v for k, v in EVM_RAW.items() if k not in ("resource", "description", "mimeType")}
    client = StubClient()
    make_provider(client, x402_version=2).build_payload(
        PaymentRequirements.from_wire(v2_raw), RESOURCE
    )
    assert "resource" not in client.calls[0]["paymentInput"]["cryptoX402"]["payload"]


def test_optional_identity_fields_are_omitted_when_unset():
    client = StubClient()
    make_provider(client).build_payload(evm_requirements(), RESOURCE)
    assert "userId" not in client.calls[0]
    make_provider(client, user_id="u1", agent_name="a1").build_payload(
        evm_requirements(), RESOURCE
    )
    assert client.calls[1]["userId"] == "u1"
    assert client.calls[1]["agentName"] == "a1"


# ─────────────────────────────────────────────────────────────────────────────
# No silent degradation
# ─────────────────────────────────────────────────────────────────────────────


def test_service_exception_raises_rather_than_simulating():
    # The original failure mode: catch everything, log a "simulated" charge, invoke
    # the tool anyway. An unprovisioned deployment then looked exactly like a
    # working one while paying nobody.
    client = StubClient(raises=RuntimeError("AccessDeniedException"))
    with pytest.raises(AgentCorePaymentError, match="ProcessPayment failed"):
        make_provider(client).build_payload(evm_requirements(), RESOURCE)


@pytest.mark.parametrize("status", ["FAILED", "PENDING", "DENIED", None])
def test_non_success_status_raises(status):
    with pytest.raises(AgentCorePaymentError, match="not a success state"):
        make_provider(StubClient(status=status)).build_payload(evm_requirements(), RESOURCE)


def test_missing_proof_payload_raises():
    with pytest.raises(AgentCorePaymentError, match="nothing to send"):
        make_provider(StubClient(payload="not-an-object")).build_payload(
            evm_requirements(), RESOURCE
        )


# ─────────────────────────────────────────────────────────────────────────────
# Construction guards
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_session_id_explains_the_nesting_bug():
    # CreatePaymentSession nests its result under `paymentSession`. Reading
    # `paymentSessionId` off the response root yields None, which is how this
    # integration silently never reached ProcessPayment at all.
    with pytest.raises(ValueError, match="nested one level down"):
        AgentCorePaymentsProvider(
            client=StubClient(),
            payment_manager_arn=MANAGER_ARN,
            payment_session_id="",
            payment_instrument_id=INSTRUMENT_ID,
        )


@pytest.mark.parametrize("field", ["payment_manager_arn", "payment_instrument_id"])
def test_required_identifiers_are_checked(field):
    kwargs = {
        "client": StubClient(),
        "payment_manager_arn": MANAGER_ARN,
        "payment_session_id": SESSION_ID,
        "payment_instrument_id": INSTRUMENT_ID,
        field: "",
    }
    with pytest.raises(ValueError, match=field):
        AgentCorePaymentsProvider(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Proof shape handling
# ─────────────────────────────────────────────────────────────────────────────


def test_bare_scheme_payload_is_wrapped_into_a_header():
    header = make_provider(StubClient(payload=PROOF_PAYLOAD)).build_header(
        evm_requirements(), RESOURCE
    )
    import base64
    import json

    decoded = json.loads(base64.b64decode(header))
    assert decoded["payload"] == PROOF_PAYLOAD
    # The requirement is echoed verbatim so the merchant can match it.
    assert decoded["accepted"] == EVM_RAW


def test_complete_payment_payload_is_sent_verbatim():
    # If AgentCore returns a full PaymentPayload, re-serialising our own envelope
    # around it could drop fields it signed over.
    complete = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "base-sepolia",
        "payload": PROOF_PAYLOAD,
    }
    header = make_provider(StubClient(payload=complete)).build_header(
        evm_requirements(), RESOURCE
    )
    import base64
    import json

    assert json.loads(base64.b64decode(header)) == complete

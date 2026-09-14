# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
x402 `exact` scheme on the XRP Ledger — the payer side.

What this produces is a *pre-signed, unsubmitted* XRPL Payment transaction. The
agent signs; the merchant's facilitator submits. That ordering is the part most
worth internalising, because the intuitive flow — pay first, then present the
receipt — is not what x402 does and does not work:

  - If the payer submitted the transaction itself and then handed over the hash,
    the merchant would have to trust that the hash corresponds to a payment it
    can actually claim, and the payer would have spent money before knowing the
    resource would be served. x402 avoids both by moving the *authorisation*
    rather than the funds: the payer authorises exactly one payment and the
    merchant chooses whether to execute it.
  - So `/verify` never touches the ledger, and calling it is free and repeatable.
    `/settle` is what submits. Anything that must not move money must stop at
    `/verify`.

A consequence worth stating because it is a real, non-obvious risk: a signed blob
is a bearer instrument. Anyone holding it can submit it until it expires. The
expiry is `LastLedgerSequence`, and the window the facilitator enforces is narrow
(see LEDGER_WINDOW_SLACK below), which is what keeps the exposure bounded.

## The wire contract

There is no XRPL scheme document in coinbase/x402 — the repository has specs for
evm, svm, algo, aptos, hedera, keeta, stellar and sui, and no XRPL file anywhere
in its tree. The reference facilitator nevertheless advertises XRPL at
`GET /supported`:

    {"x402Version": 2, "scheme": "exact", "network": "xrpl:1",
     "extra": {"areFeesSponsored": false}}

The field shapes below were therefore established by probing that facilitator's
`/verify` with real signed testnet transactions, and each rule here corresponds
to a rejection actually observed. They are recorded in tests/test_x402_xrpl.py so
that a future facilitator change surfaces as a test failure rather than as a
mystery in production:

    asset               native XRP is the plain string "XRP"; any object form
                        ({"name": "XRP"}, {"currency": "XRP"}, ...) is rejected
                        with invalid_exact_xrpl_asset_mismatch
    asset (issued)      the currency code as a bare string, e.g. "USD", with the
                        issuer in extra.issuer — NOT "USD.rIssuer", which fails
                        invalid_exact_xrpl_iou_issuer_missing
    amount              atomic units as a string: drops for XRP, decimal value
                        for an issued currency. Matched for EXACT equality:
                        underpaying and overpaying both fail
                        invalid_exact_xrpl_payload_amount_mismatch
    payload             {"signedTxBlob": "<hex>"}; the key name is enforced
                        ("XRPL exact payload requires signedTxBlob")
    SendMax             REQUIRED on issued-currency payments
                        (invalid_exact_xrpl_payload_sendmax_required); absent for
                        native XRP
    LastLedgerSequence  bounded — see below
    InvoiceID / Memos   not required; verification passes without them

`LastLedgerSequence` is the fiddly one. It must sit inside a window measured
against the ledger *at verification time*, not at signing time:

    LastLedgerSequence - currentLedger <= ceil(maxTimeoutSeconds / 5) + 2

Too high fails `invalid_exact_xrpl_payload_lastledgersequence_too_large`; too low
fails `invalid_exact_xrpl_payload_expired` because the ledger has closed past it
in the meantime. The 5 is roughly XRPL's ledger close interval in seconds. Since
the bound is evaluated when the facilitator looks, every second spent between
signing and verifying eats into the margin — which is why xrpl-py's autofill
default of `currentLedger + 20` is unusable here and is overridden below.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from decimal import Decimal
from typing import Any, Mapping, Optional

from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import Ledger
from xrpl.models.transactions import Payment
from xrpl.transaction import autofill, sign
from xrpl.wallet import Wallet

from .x402 import PaymentRequirements, ResourceInfo, X402Error

logger = logging.getLogger("xrpl_agentic_payments.payments.xrpl_exact")

SCHEME = "exact"

# CAIP-2 references for XRPL. The mapping matters for more than cosmetics: the
# same seed is a valid key on every XRPL network, so a challenge naming the wrong
# network would otherwise be signed happily against mainnet funds. build_payload
# refuses any network its client is not connected to.
XRPL_MAINNET = "xrpl:0"
XRPL_TESTNET = "xrpl:1"
XRPL_DEVNET = "xrpl:2"

# See the module docstring: ceil(maxTimeoutSeconds / 5) + 2, where 5s is the
# nominal ledger close interval.
LEDGER_CLOSE_SECONDS = 5
LEDGER_WINDOW_SLACK = 2

# XRPL native amounts are integer drops; 1 XRP = 1e6 drops.
DROPS_PER_XRP = 1_000_000


class XrplPaymentError(X402Error):
    """An XRPL `exact` payment could not be constructed."""


def ledger_window(max_timeout_seconds: int) -> int:
    """How far above the current ledger `LastLedgerSequence` may sit."""
    return math.ceil(max_timeout_seconds / LEDGER_CLOSE_SECONDS) + LEDGER_WINDOW_SLACK


class XrplExactProvider:
    """Signs XRPL `exact`-scheme payments for x402 challenges.

    This provider does not submit and does not need submit permissions; it only
    needs a read connection to compute the current ledger index and autofill
    Sequence and Fee.
    """

    def __init__(
        self,
        wallet: Wallet,
        client: JsonRpcClient,
        network: str = XRPL_TESTNET,
        name: str = "xrpl-exact",
    ):
        self.wallet = wallet
        self.client = client
        self.network = network
        self.name = name

    def supports(self, requirements: PaymentRequirements) -> bool:
        # The network is compared exactly, not by `xrpl:` prefix. A prefix match
        # would let an `xrpl:0` (mainnet) challenge be signed by a wallet this
        # provider was configured to use on testnet, which is the one mistake
        # here that costs real money.
        return requirements.scheme == SCHEME and requirements.network == self.network

    def build_payload(
        self, requirements: PaymentRequirements, resource: ResourceInfo
    ) -> Mapping[str, Any]:
        if not self.supports(requirements):
            raise XrplPaymentError(
                f"{self.name} is configured for {self.network} and cannot sign a "
                f"{requirements.scheme}/{requirements.network} requirement"
            )

        # `areFeesSponsored` says a third party covers the XRPL transaction fee,
        # which changes who signs what. The reference facilitator reports false
        # for xrpl:1 and there is no specification for the sponsored variant, so
        # rather than guess a construction that would fail verification for
        # unclear reasons, refuse and say why.
        if requirements.extra.get("areFeesSponsored"):
            raise XrplPaymentError(
                "requirement sets extra.areFeesSponsored=true; fee-sponsored XRPL "
                "payments are not implemented (no published construction), so this "
                "payment would not verify"
            )

        amount = self._build_amount(requirements)
        # SendMax is what makes an issued-currency payment "exact": without it the
        # ledger may deliver less than Amount after transfer fees or rippling, and
        # the facilitator rejects the payment outright
        # (invalid_exact_xrpl_payload_sendmax_required). Native XRP has no
        # transfer fee, so SendMax is unnecessary there and is left off.
        send_max = amount if isinstance(amount, IssuedCurrencyAmount) else None

        payment = Payment(
            account=self.wallet.classic_address,
            destination=requirements.pay_to,
            amount=amount,
            send_max=send_max,
        )

        # autofill supplies Sequence, Fee and NetworkID, then LastLedgerSequence is
        # overwritten: autofill's default is currentLedger + 20, which exceeds the
        # facilitator's window for every maxTimeoutSeconds below ~90 and so would
        # be rejected as too_large.
        filled = autofill(payment, self.client)
        last_ledger = self._last_ledger_sequence(requirements.max_timeout_seconds)
        # dataclasses.replace rather than rebuilding from to_dict(): to_dict()
        # emits `transaction_type`, which the constructor does not accept, so the
        # rebuild raises TypeError.
        filled = replace(filled, last_ledger_sequence=last_ledger)

        signed = sign(filled, self.wallet)
        blob = signed.blob()
        logger.info(
            "xrpl x402: signed %s %s -> %s (LastLedgerSequence=%d, unsubmitted)",
            requirements.amount,
            requirements.asset,
            requirements.pay_to,
            last_ledger,
        )
        return {"signedTxBlob": blob}

    def _build_amount(self, requirements: PaymentRequirements):
        asset = requirements.asset
        if not isinstance(asset, str) or not asset:
            # Object assets are what the facilitator rejects as asset_mismatch;
            # failing here names the real problem instead of deferring it to an
            # opaque verification error.
            raise XrplPaymentError(
                f"XRPL requires `asset` to be a currency code string "
                f"(\"XRP\" or e.g. \"USD\"), got {type(asset).__name__}: {asset!r}"
            )

        if asset == "XRP":
            # Amount is already in drops. Validated as an integer rather than
            # passed through, because a fractional value here is a merchant bug
            # that the ledger would reject only after signing.
            if not requirements.amount.isdigit():
                raise XrplPaymentError(
                    f"native XRP amounts are integer drops, got {requirements.amount!r}"
                )
            return requirements.amount

        issuer = requirements.extra.get("issuer")
        if not issuer:
            raise XrplPaymentError(
                f"issued-currency asset {asset!r} requires the issuer in "
                f"extra.issuer (the facilitator rejects it as "
                f"invalid_exact_xrpl_iou_issuer_missing otherwise)"
            )
        try:
            # Decimal, not float: the ledger carries 15 significant digits and
            # binary floating point cannot round-trip a decimal price exactly.
            value = Decimal(requirements.amount)
        except ArithmeticError:
            raise XrplPaymentError(
                f"issued-currency amount is not a valid decimal: {requirements.amount!r}"
            ) from None
        if not value.is_finite() or value <= 0:
            raise XrplPaymentError(f"amount must be positive and finite, got {value}")
        return IssuedCurrencyAmount(
            currency=asset, issuer=str(issuer), value=requirements.amount
        )

    def _last_ledger_sequence(self, max_timeout_seconds: int) -> int:
        # The validated ledger, not the current open one: the facilitator compares
        # against a validated index, and using the open ledger would place the
        # ceiling one higher than it will accept.
        validated = self.client.request(Ledger(ledger_index="validated"))
        index = validated.result.get("ledger_index")
        if not isinstance(index, int):
            raise XrplPaymentError(
                f"could not read the validated ledger index from {self.client.url}"
            )
        return index + ledger_window(max_timeout_seconds)


def drops_for_usd(usd: str | Decimal, xrp_usd_price: str | Decimal) -> str:
    """Convert a USD price to integer drops, for merchants that price in USD.

    Rounds UP. The `exact` scheme demands equality, so rounding down would build
    a payment one drop short of the requirement and fail verification.
    """
    usd = Decimal(str(usd))
    price = Decimal(str(xrp_usd_price))
    if price <= 0:
        raise ValueError(f"xrp_usd_price must be positive, got {price}")
    drops = (usd / price) * DROPS_PER_XRP
    return str(int(drops.to_integral_value(rounding="ROUND_CEILING")))

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments Tool: submit_payment

Submit an XRP or token payment to a destination address on XRPL testnet.
THIS IS THE ONLY TOOL THAT WRITES TO THE LEDGER.

Security: Only this Lambda has access to the execution wallet seed.
All other tools are read-only and do not need wallet signing capability.

XRPL Agent Behavior Tracking (per https://xrpl.org/docs/agents/track-agent-behavior):
- SourceTag: 20260530 (XRPL AI Starter Kit default, identifies agentic transactions)
- Memos: hex-encoded JSON with agent_id, session_id, action, task_id
  This makes every transaction attributable and auditable directly from the
  ledger, independent of application logs.
"""

import uuid

from shared import xrpl_client, get_wallet, RLUSD_ISSUER, logger
from xrpl.models import Payment, IssuedCurrencyAmount
from xrpl.transaction import submit_and_wait

# SourceTag, memo builder and drops conversion are shared with the MCP server
# implementation of this tool so both produce identically attributable ledger
# transactions (see functions/xrpl_core.py).
from xrpl_core import (
    XRPL_AGENTIC_PAYMENTS_SOURCE_TAG,
    XrpAmountError,
    build_attribution_memo,
    xrp_to_drops_str,
)


def lambda_handler(event, context):
    destination = event.get("destination", "")
    amount = event.get("amount", "")
    currency = event.get("currency", "USD")
    issuer = event.get("issuer", "")
    source_wallet = event.get("source_wallet", "execution")

    if not destination or not amount:
        return {"error": "Missing required parameters: destination, amount"}

    logger.info(f"submit_payment: {amount} {currency} → {destination} (from: {source_wallet})")

    wallet = get_wallet(source_wallet)

    # Build amount object
    if currency.upper() == "XRP":
        # Exact decimal → drops conversion; float math would silently underpay.
        try:
            send_amount = xrp_to_drops_str(amount)
        except XrpAmountError as exc:
            return {"error": str(exc)}
    else:
        effective_issuer = issuer if issuer else RLUSD_ISSUER
        if not effective_issuer:
            return {"error": "No issuer specified and no default RLUSD issuer configured"}
        send_amount = IssuedCurrencyAmount(
            currency=currency,
            issuer=effective_issuer,
            value=amount,
        )

    # Tracking IDs for on-chain attribution.
    #
    # The caller's session id wins. The orchestrator mints one per payment and
    # passes it to every tool call (src/agentcore_client.py), so honouring it
    # here is what makes the memo's session_id identify the PAYMENT rather than
    # this single Lambda invocation — a request id correlates with nothing else
    # on the ledger, and every payment is exactly one submit_payment call, so it
    # looked plausible while making the field useless for tracing a run.
    session_id = event.get("session_id", "").strip()
    if not session_id:
        session_id = (
            f"lambda-{context.aws_request_id}" if context
            else f"local-{uuid.uuid4().hex[:8]}"
        )
    task_id = f"pay-{uuid.uuid4().hex[:8]}"

    # Build transaction with SourceTag and Memos for agent behavior tracking
    tx = Payment(
        account=wallet.address,
        destination=destination,
        amount=send_amount,
        source_tag=XRPL_AGENTIC_PAYMENTS_SOURCE_TAG,
        memos=[build_attribution_memo(
            session_id=session_id,
            action="submit_payment",
            task_id=task_id,
        )],
    )

    logger.info(f"Submitting payment (SourceTag={XRPL_AGENTIC_PAYMENTS_SOURCE_TAG}, task={task_id})...")
    response = submit_and_wait(tx, xrpl_client, wallet)
    result = response.result

    tx_result = result.get("meta", {}).get("TransactionResult", "unknown")
    tx_hash = result.get("hash", "")
    ledger_index = result.get("ledger_index", 0)

    logger.info(f"Payment result: {tx_result} (hash: {tx_hash})")

    return {
        "status": "success" if tx_result == "tesSUCCESS" else "failed",
        "transaction_result": tx_result,
        "hash": tx_hash,
        "ledger_index": ledger_index,
        "source": wallet.address,
        "destination": destination,
        "amount": amount,
        "currency": currency,
        "source_tag": XRPL_AGENTIC_PAYMENTS_SOURCE_TAG,
        "task_id": task_id,
        "explorer_url": f"https://testnet.xrpl.org/transactions/{tx_hash}",
    }

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments Tool: check_transaction

Check the status and details of a submitted XRPL transaction.
Read-only, no ledger writes.
"""

from shared import xrpl_client, logger
from xrpl.models import Tx


def lambda_handler(event, context):
    tx_hash = event.get("tx_hash", "")
    if not tx_hash:
        return {"error": "Missing required parameter: tx_hash"}

    logger.info(f"check_transaction: {tx_hash}")

    req = Tx(transaction=tx_hash)
    response = xrpl_client.request(req)

    if not response.is_successful():
        return {"error": f"Transaction lookup failed: {response.result.get('error', 'unknown')}"}

    result = response.result
    meta = result.get("meta", {})
    if not isinstance(meta, dict):
        # A transaction that has not been included in a ledger yet has no
        # metadata; older rippled versions return the string "unavailable".
        meta = {}
    tx_json = result.get("tx_json", result)

    # "Validated" and "succeeded" are different facts and callers need both.
    # A transaction in a validated ledger with a tec* result is a PERMANENT
    # failure that burned a fee and delivered nothing — reporting that as
    # "validated" made every consumer treat it as a completed payment.
    validated = result.get("validated") is True
    tx_result = meta.get("TransactionResult", "unknown")
    if not validated:
        status = "pending"
    elif tx_result == "tesSUCCESS":
        status = "succeeded"
    else:
        status = "failed"

    return {
        "hash": tx_hash,
        "status": status,
        "validated": validated,
        "transaction_result": tx_result,
        "transaction_type": tx_json.get("TransactionType", "unknown"),
        "account": tx_json.get("Account", ""),
        "destination": tx_json.get("Destination", ""),
        "amount": tx_json.get("DeliverMax", tx_json.get("Amount", "")),
        # What the destination actually received. For a partial payment this is
        # the only truthful figure: Amount/DeliverMax is the maximum the sender
        # authorised, not the amount that arrived.
        "delivered_amount": meta.get("delivered_amount", meta.get("DeliveredAmount")),
        "ledger_index": result.get("ledger_index", 0),
        "date": result.get("close_time_iso", ""),
        "fee": tx_json.get("Fee", ""),
    }

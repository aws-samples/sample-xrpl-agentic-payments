# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments Tool: path_find

Find cross-currency payment paths on XRPL using native pathfinding.
Simpler interface than get_paths, focused on basic path discovery.
Read-only, no ledger writes.
"""

from shared import xrpl_client, RLUSD_ISSUER, logger
from xrpl.models import RipplePathFind, IssuedCurrencyAmount

from xrpl_core import XrpAmountError, describe_source_amount, xrp_to_drops_str


def lambda_handler(event, context):
    source = event.get("source", "")
    destination = event.get("destination", "")
    amount = event.get("amount", "")
    currency = event.get("currency", "USD")
    issuer = event.get("issuer", "")

    if not source or not destination or not amount:
        return {"error": "Missing required parameters: source, destination, amount"}

    logger.info(f"path_find: {source} → {destination}, {amount} {currency}")

    effective_issuer = issuer if issuer else RLUSD_ISSUER

    if currency.upper() == "XRP":
        # Exact decimal → drops conversion (float math skews the quoted amount).
        try:
            dest_amount = xrp_to_drops_str(amount)
        except XrpAmountError as exc:
            return {"error": str(exc)}
    else:
        dest_amount = IssuedCurrencyAmount(
            currency=currency,
            issuer=effective_issuer,
            value=amount,
        )

    req = RipplePathFind(
        source_account=source,
        destination_account=destination,
        destination_amount=dest_amount,
    )
    response = xrpl_client.request(req)

    if not response.is_successful():
        return {"error": f"Path find failed: {response.result.get('error', 'unknown')}"}

    alternatives = response.result.get("alternatives", [])
    paths = []
    for i, alt in enumerate(alternatives):
        cost = describe_source_amount(alt.get("source_amount", {}))

        paths.append({
            "path_index": i,
            "source_cost": cost["display"],
            "paths_computed": len(alt.get("paths_computed", [])),
        })

    return {
        "source": source,
        "destination": destination,
        "destination_amount": f"{amount} {currency}",
        "paths_found": len(paths),
        "alternatives": paths,
    }

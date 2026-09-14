# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments Tool: get_paths

Find optimal payment paths with cost estimates.
Returns ranked paths comparing direct, multi-hop, and cross-currency routes.
Read-only, no ledger writes.
"""

from shared import xrpl_client, RLUSD_ISSUER, logger
from xrpl.models import RipplePathFind, IssuedCurrencyAmount

from xrpl_core import XrpAmountError, rank_path_alternatives, xrp_to_drops_str


def lambda_handler(event, context):
    source = event.get("source", "")
    destination = event.get("destination", "")
    dest_amount = event.get("dest_amount", "")
    dest_currency = event.get("dest_currency", "USD")
    dest_issuer = event.get("dest_issuer", "")

    if not source or not destination or not dest_amount:
        return {"error": "Missing required parameters: source, destination, dest_amount"}

    logger.info(f"get_paths: {source} → {destination}, {dest_amount} {dest_currency}")

    effective_issuer = dest_issuer if dest_issuer else RLUSD_ISSUER

    if dest_currency.upper() == "XRP":
        # Exact decimal → drops conversion (float math skews the quoted amount).
        try:
            amount = xrp_to_drops_str(dest_amount)
        except XrpAmountError as exc:
            return {"error": str(exc)}
    else:
        amount = IssuedCurrencyAmount(
            currency=dest_currency,
            issuer=effective_issuer,
            value=dest_amount,
        )

    req = RipplePathFind(
        source_account=source,
        destination_account=destination,
        destination_amount=amount,
    )
    response = xrpl_client.request(req)

    if not response.is_successful():
        return {"error": f"Path find failed: {response.result.get('error', 'unknown')}"}

    alternatives = response.result.get("alternatives", [])

    # Ranking is shared with the MCP implementation: costs are only compared
    # within a currency (5 USD is not cheaper than 10 XRP) and rank is assigned
    # after sorting.
    ranking = rank_path_alternatives(alternatives)
    ranked_paths = ranking["paths"]

    return {
        "source": source,
        "destination": destination,
        "deliver_amount": f"{dest_amount} {dest_currency}",
        "paths_found": len(ranked_paths),
        "recommended_path": ranking["recommended_path"],
        "all_paths": ranked_paths,
        "cost_comparable": ranking["cost_comparable"],
        "cheapest_per_currency": ranking["cheapest_per_currency"],
        "ranking_note": ranking["ranking_note"],
    }

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments Tool: get_trust_lines

Get all trust lines (token relationships) for an XRPL account.
Read-only, no ledger writes.
"""

from shared import xrpl_client, logger

from xrpl_core import fetch_account_lines


def lambda_handler(event, context):
    account = event.get("account", "")
    if not account:
        return {"error": "Missing required parameter: account"}

    logger.info(f"get_trust_lines: {account}")

    # Paginated: AccountLines returns a few hundred lines per page, so the
    # marker has to be followed or the set is silently truncated.
    lines_result = fetch_account_lines(xrpl_client, account)
    if lines_result["error"] and not lines_result["lines"]:
        return {"error": f"Failed to get trust lines: {lines_result['error']}"}

    trust_lines = []
    for line in lines_result["lines"]:
        trust_lines.append({
            "currency": line["currency"],
            "balance": line["balance"],
            "issuer": line["account"],
            "limit": line["limit"],
            "quality_in": line.get("quality_in", 0),
            "quality_out": line.get("quality_out", 0),
        })

    result = {
        "account": account,
        "trust_line_count": len(trust_lines),
        "trust_lines": trust_lines,
    }
    if lines_result["truncated"]:
        result["truncated"] = True
    if lines_result["error"]:
        result["partial_error"] = lines_result["error"]
    return result

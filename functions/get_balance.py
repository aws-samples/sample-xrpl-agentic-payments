# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments Tool: get_balance

Get all balances (XRP + tokens) for an XRPL account.
Read-only, no ledger writes.
"""

from shared import xrpl_client, logger
from xrpl.models import AccountInfo

from xrpl_core import fetch_account_lines


def lambda_handler(event, context):
    account = event.get("account", "")
    if not account:
        return {"error": "Missing required parameter: account"}

    logger.info(f"get_balance: {account}")

    # XRP balance
    req = AccountInfo(account=account, ledger_index="validated")
    response = xrpl_client.request(req)

    if not response.is_successful():
        return {"error": f"Failed to get account info: {response.result.get('error', 'unknown')}"}

    xrp_drops = int(response.result["account_data"]["Balance"])
    xrp_balance = xrp_drops / 1_000_000

    # Token balances (trust lines) — paginated: a single AccountLines page holds
    # only a few hundred lines, so without following the marker balances vanish.
    lines_result = fetch_account_lines(xrpl_client, account)

    token_balances = []
    for line in lines_result["lines"]:
        token_balances.append({
            "currency": line["currency"],
            "balance": line["balance"],
            "issuer": line["account"],
            "limit": line["limit"],
        })

    result = {
        "account": account,
        "xrp_balance": str(xrp_balance),
        "token_balances": token_balances,
        "reserve_base_xrp": "10",
    }
    if lines_result["truncated"]:
        result["truncated"] = True
    if lines_result["error"]:
        result["token_balances_error"] = lines_result["error"]
    return result

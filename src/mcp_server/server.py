# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments — XRPL MCP Server

A single FastMCP server with three tool groups:
1. Payment Tools (free) — submit_payment, get_balance, path_find, check_tx, get_trust_lines
2. Market Tools (priced) — get_orderbook, get_paths
3. Compliance Tools (priced) — screen_sanctions

This server does NOT gate on payment. There is no decorator that checks for a
payment proof: the tools below carry only `@mcp.tool()`, they never inspect an
`X-PAYMENT` header, and they never return HTTP 402. A "priced" tool logs its fee
and adds an `x402_fee_charged` string to its response — both are descriptive
labels, not enforcement. Calling one of these tools without paying anything
succeeds.

The charge happens on the *client* side instead, before the call is dispatched:
`PaymentGate.charge()` in `src/agentcore_client.py` prices the tool from a local
`TOOL_PRICING` dict and calls AgentCore Payments `process_payment`. It then
invokes the tool regardless of the outcome, so a failed or absent charge never
blocks anything. That makes the whole arrangement metering, not access control.

Implementing real x402 would mean inverting the direction: this server declares
the price by returning 402, the client obtains a signed proof and retries with
`X-PAYMENT`, and this server verifies that proof before doing any work. None of
that exists yet.
"""

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import nest_asyncio
nest_asyncio.apply()

from mcp.server.fastmcp import FastMCP
from xrpl.clients import JsonRpcClient
from xrpl.models import (
    AccountInfo,
    BookOffers,
    IssuedCurrency,
    IssuedCurrencyAmount,
    Payment,
    PathFind,
    PathFindSubcommand,
    Tx,
)
from xrpl.transaction import submit_and_wait
from xrpl.wallet import Wallet

# Logic that must be identical to the Lambda tools behind the AgentCore Gateway
# (drops conversion, on-ledger attribution, path ranking, order book math,
# AccountLines pagination) lives in one shared module imported by both layers.
# It has to live in functions/ because that directory is the Lambda code asset
# (infra/lib/tools-stack.ts), where it is a top-level module; the Dockerfile and
# deploy/package.sh copy it to <artifact root>/functions/, which is where the
# package-qualified fallback below finds it (as it does in a repo checkout).
try:
    from xrpl_core import (
        XRPL_AGENTIC_PAYMENTS_SOURCE_TAG,
        XrpAmountError,
        build_attribution_memo,
        describe_source_amount,
        fetch_account_lines,
        format_book_offers,
        orderbook_summary,
        rank_path_alternatives,
        xrp_to_drops_str,
    )
except ModuleNotFoundError:  # MCP artifacts / repo checkout: root on sys.path
    from functions.xrpl_core import (
        XRPL_AGENTIC_PAYMENTS_SOURCE_TAG,
        XrpAmountError,
        build_attribution_memo,
        describe_source_amount,
        fetch_account_lines,
        format_book_offers,
        orderbook_summary,
        rank_path_alternatives,
        xrp_to_drops_str,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("xrpl_agentic_payments.mcp")

# Resolve config directory: try relative to this file first, then CWD
_server_file = Path(__file__).resolve()
_possible_roots = [
    _server_file.parent.parent.parent.parent,  # src/mcp_server/server.py → project root
    Path.cwd(),                                  # Current working directory
]
CONFIG_DIR = None
for root in _possible_roots:
    candidate = root / "config" / "wallets.json"
    if candidate.exists():
        CONFIG_DIR = root / "config"
        break
if CONFIG_DIR is None:
    CONFIG_DIR = _possible_roots[0] / "config"  # Fallback

WALLETS_FILE = CONFIG_DIR / "wallets.json"

# Load wallet config
if WALLETS_FILE.exists():
    with open(WALLETS_FILE) as f:
        _wallets_data = json.load(f)
    METADATA = _wallets_data.get("_metadata", {})
    RLUSD_ISSUER = METADATA.get("rlusd_issuer", "")
    RLUSD_CURRENCY = METADATA.get("rlusd_currency", "USD")
    RPC_URL = METADATA.get("rpc_url", "https://s.altnet.rippletest.net:51234")
else:
    RLUSD_ISSUER = ""
    RLUSD_CURRENCY = "USD"
    RPC_URL = "https://s.altnet.rippletest.net:51234"
    _wallets_data = {}

# XRPL client (reused across tool calls)
xrpl_client = JsonRpcClient(RPC_URL)

# MCP server instance — configured for AgentCore Runtime compatibility
# Contract: host=0.0.0.0, port=8000, path=/mcp, stateless_http=True
mcp = FastMCP(
    "xrpl-agentic-payments",
    host="0.0.0.0",
    stateless_http=True,
    instructions=(
        "XRPL Agentic Payments MCP Server. Provides payment tools for sending RLUSD "
        "on XRP Ledger testnet, market data tools for DEX order books, and "
        "compliance tools for sanctions screening."
    ),
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _get_wallet(name: str) -> Wallet:
    """Load a wallet by name from config."""
    if name not in _wallets_data:
        raise ValueError(f"Wallet '{name}' not found in config. Available: {list(_wallets_data.keys())}")
    return Wallet.from_seed(_wallets_data[name]["seed"])


def _format_balance(balance: dict) -> dict:
    """Normalize balance display."""
    return {
        "currency": balance.get("currency", "XRP"),
        "value": balance.get("value", balance.get("Balance", "0")),
        "issuer": balance.get("account", ""),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 1: Payment Tools (FREE — no x402 gate)
# ═══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def submit_payment(
    destination: str,
    amount: str,
    currency: str = "USD",
    issuer: str = "",
    source_wallet: str = "execution",
) -> dict[str, Any]:
    """
    Submit an XRP or token payment to a destination address on XRPL testnet.

    Args:
        destination: XRPL destination address (r...)
        amount: Amount to send (e.g. "100" for 100 RLUSD)
        currency: Currency code (default "USD" for RLUSD on testnet, or "XRP")
        issuer: Token issuer address (auto-filled for RLUSD if empty)
        source_wallet: Which wallet to send from (default "execution")

    Returns:
        Transaction result with hash, status, ledger index, and the on-ledger
        attribution (source_tag, task_id) plus an explorer URL.
    """
    wallet = _get_wallet(source_wallet)

    # Build amount object
    if currency.upper() == "XRP":
        # XRP amounts are in drops (1 XRP = 1,000,000 drops). Convert through
        # Decimal — float math truncates and silently underpays (8.29 → 8.289999).
        try:
            send_amount = xrp_to_drops_str(amount)
        except XrpAmountError as exc:
            return {"error": str(exc)}
    else:
        # Token payment
        effective_issuer = issuer if issuer else RLUSD_ISSUER
        if not effective_issuer:
            return {"error": "No issuer specified and no default RLUSD issuer configured"}
        send_amount = IssuedCurrencyAmount(
            currency=currency,
            issuer=effective_issuer,
            value=amount,
        )

    # Tracking IDs for on-chain attribution (same contract as the Lambda tool)
    session_id = f"mcp-{uuid.uuid4().hex[:8]}"
    task_id = f"pay-{uuid.uuid4().hex[:8]}"

    # SourceTag + memo make the transaction attributable to this agent directly
    # from the ledger — the Gateway/Lambda path and this path must tag alike, or
    # "filter by SourceTag to find agent activity" misses half the traffic.
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

    logger.info(
        f"Submitting payment: {amount} {currency} → {destination} "
        f"(SourceTag={XRPL_AGENTIC_PAYMENTS_SOURCE_TAG}, task={task_id})"
    )
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


@mcp.tool()
def get_balance(account: str) -> dict[str, Any]:
    """
    Get all balances (XRP + tokens) for an XRPL account.

    Args:
        account: XRPL account address (r...)

    Returns:
        XRP balance and all token balances including RLUSD.
    """
    # Get XRP balance
    req = AccountInfo(account=account, ledger_index="validated")
    response = xrpl_client.request(req)

    if not response.is_successful():
        return {"error": f"Failed to get account info: {response.result.get('error', 'unknown')}"}

    xrp_drops = int(response.result["account_data"]["Balance"])
    xrp_balance = xrp_drops / 1_000_000

    # Get token balances (trust lines) — paginated: one AccountLines page holds
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


@mcp.tool()
def path_find(
    source: str,
    destination: str,
    amount: str,
    currency: str = "USD",
    issuer: str = "",
) -> dict[str, Any]:
    """
    Find optimal cross-currency payment paths on XRPL.

    Uses XRPL's native pathfinding to discover routes between currencies,
    including multi-hop paths through the DEX.

    Args:
        source: Source XRPL address
        destination: Destination XRPL address
        amount: Destination amount to receive
        currency: Destination currency (default "USD" for RLUSD)
        issuer: Token issuer (auto-filled for RLUSD)

    Returns:
        List of available payment paths with costs.
    """
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

    # Use ripple_path_find RPC method
    from xrpl.models import RipplePathFind
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


@mcp.tool()
def check_transaction(tx_hash: str) -> dict[str, Any]:
    """
    Check the status and details of a submitted XRPL transaction.

    Args:
        tx_hash: Transaction hash (64-character hex string)

    Returns:
        status — "succeeded" (in a validated ledger AND tesSUCCESS), "failed"
        (validated but did not move funds), or "pending" (not yet validated);
        plus validated, transaction_result, type, amount, delivered_amount and
        ledger details.
    """
    req = Tx(transaction=tx_hash)
    response = xrpl_client.request(req)

    if not response.is_successful():
        return {"error": f"Transaction lookup failed: {response.result.get('error', 'unknown')}"}

    result = response.result
    meta = result.get("meta", {})
    if not isinstance(meta, dict):
        # A transaction not yet in a ledger has no metadata; some rippled
        # versions return the string "unavailable" rather than an object.
        meta = {}
    # In xrpl-py 5.x, tx fields are under tx_json
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


@mcp.tool()
def get_trust_lines(account: str) -> dict[str, Any]:
    """
    Get all trust lines (token relationships) for an XRPL account.

    Shows which tokens the account can hold and current balances.

    Args:
        account: XRPL account address (r...)

    Returns:
        List of trust lines with currency, balance, issuer, and limit.
    """
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


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 2: Market Tools (x402 GATED — $0.003/call)
# ═══════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_orderbook(
    base_currency: str = "USD",
    base_issuer: str = "",
    quote_currency: str = "XRP",
    quote_issuer: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """
    Query the XRPL DEX order book for a trading pair.

    [x402 GATED: $0.003/query — simulates premium market data feed]

    Shows current asks and bids on the XRPL decentralized exchange. Both sides
    of the book are queried. For the pair BASE/QUOTE, `price` is QUOTE units per
    1 BASE unit and `size` is a BASE quantity, on both sides.

    Args:
        base_currency: Base currency (default "USD" for RLUSD)
        base_issuer: Base currency issuer (auto-filled for RLUSD)
        quote_currency: Quote currency (default "XRP")
        quote_issuer: Quote currency issuer (empty for XRP)
        limit: Max number of offers per side (default 10)

    Returns:
        Order book with asks, bids, best_bid, best_ask, spread, spread_pct and
        mid — each of best_bid/best_ask/spread/mid is null if that side is empty.
    """
    logger.info(f"[x402] get_orderbook called — fee: $0.003")

    # Build currency objects for BookOffers
    # In XRPL, BookOffers uses taker_gets/taker_pays as currency definitions
    # XRP is represented with XRP() model, tokens with IssuedCurrency()
    from xrpl.models.currencies import XRP as XRPCurrency

    effective_base_issuer = base_issuer if base_issuer else RLUSD_ISSUER
    effective_quote_issuer = quote_issuer if quote_issuer else RLUSD_ISSUER

    def _currency(code: str, issuer: str):
        """Build a BookOffers currency object (XRP has no issuer)."""
        if code.upper() == "XRP":
            return XRPCurrency()
        return IssuedCurrency(currency=code, issuer=issuer)

    def _fetch_offers(taker_gets, taker_pays):
        """Request one side of the book. Returns (offers, error)."""
        response = xrpl_client.request(
            BookOffers(taker_gets=taker_gets, taker_pays=taker_pays, limit=limit)
        )
        if not response.is_successful():
            return [], response.result.get("error", "unknown")
        return response.result.get("offers", []), None

    base = _currency(base_currency, effective_base_issuer)
    quote = _currency(quote_currency, effective_quote_issuer)

    # Asks: offers selling BASE for QUOTE (TakerGets=BASE, TakerPays=QUOTE).
    ask_offers, error = _fetch_offers(taker_gets=base, taker_pays=quote)
    if error:
        return {"error": f"Order book query failed: {error}"}

    # Bids: the mirror book — offers selling QUOTE for BASE.
    bid_offers, error = _fetch_offers(taker_gets=quote, taker_pays=base)
    if error:
        return {"error": f"Order book query failed: {error}"}

    asks = format_book_offers(ask_offers, "asks", limit)
    bids = format_book_offers(bid_offers, "bids", limit)
    summary = orderbook_summary(asks, bids)

    return {
        "pair": f"{base_currency}/{quote_currency}",
        "price_convention": f"{quote_currency} per 1 {base_currency}",
        "offers_count": len(asks),
        "offers": asks,  # retained for callers that read the ask side as "offers"
        "asks": asks,
        "bids": bids,
        **summary,
        "x402_fee_charged": "$0.003",
    }


@mcp.tool()
def get_paths(
    source: str,
    destination: str,
    dest_amount: str,
    dest_currency: str = "USD",
    dest_issuer: str = "",
) -> dict[str, Any]:
    """
    Find optimal payment paths with cost estimates.

    [x402 GATED: $0.003/query — simulates premium routing intelligence]

    Returns ranked paths comparing direct, multi-hop, and cross-currency routes.

    Args:
        source: Source XRPL address
        destination: Destination XRPL address
        dest_amount: Amount to deliver at destination
        dest_currency: Destination currency (default "USD" for RLUSD)
        dest_issuer: Destination currency issuer (auto-filled for RLUSD)

    Returns:
        Ranked list of paths with estimated costs and times. Costs are only
        compared within a source currency; when alternatives span several source
        currencies, recommended_path is null and ranking_note explains why.
    """
    logger.info(f"[x402] get_paths called — fee: $0.003")

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

    from xrpl.models import RipplePathFind
    req = RipplePathFind(
        source_account=source,
        destination_account=destination,
        destination_amount=amount,
    )
    response = xrpl_client.request(req)

    if not response.is_successful():
        return {"error": f"Path find failed: {response.result.get('error', 'unknown')}"}

    alternatives = response.result.get("alternatives", [])

    # Ranking is shared with the Lambda implementation: costs are only compared
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
        "x402_fee_charged": "$0.003",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 3: Compliance Tools (x402 GATED — $0.01/call)
# ═══════════════════════════════════════════════════════════════════════════════

# OFAC SDN list path (download from https://www.treasury.gov/ofac/downloads/sdn.csv).
# Resolved independently of CONFIG_DIR: CONFIG_DIR falls back to a guess when
# config/wallets.json is absent, which silently pointed the screening tool at a
# non-existent CSV and degraded it to the 12-name demo list.
_OFAC_CANDIDATES = (
    _server_file.parents[2] / "data" / "ofac_sdn.csv",  # src/mcp_server/server.py → root
    CONFIG_DIR.parent / "data" / "ofac_sdn.csv",
    Path.cwd() / "data" / "ofac_sdn.csv",
)
OFAC_FILE = next(
    (candidate for candidate in _OFAC_CANDIDATES if candidate.exists()),
    _OFAC_CANDIDATES[0],
)

# All matching logic is shared verbatim with the screen_sanctions Lambda behind
# the Gateway, so the two layers cannot return different verdicts for the same
# entity. Same packaging deal as xrpl_core above: the module lives in functions/
# (the Lambda code asset) and is copied into this server's container and zip.
try:
    import sanctions_matcher
except ModuleNotFoundError:  # local dev / tests: repo root on sys.path
    from functions import sanctions_matcher

# Demo sanctions list (used when the OFAC file is not available)
DEMO_SANCTIONED_ENTITIES = [
    "NORTH KOREA",
    "DPRK",
    "IRAN",
    "SYRIA",
    "CUBA",
    "CRIMEA",
    "DONETSK",
    "LUHANSK",
    "TALIBAN",
    "AL-QAEDA",
    "ISIS",
    "HEZBOLLAH",
]

# Parse the 5.6 MB SDN CSV once at import, not once per request.
if OFAC_FILE.exists():
    SDN_INDEX = sanctions_matcher.load_index_from_path(OFAC_FILE)
    SDN_SOURCE = "full"
else:
    logger.warning(f"OFAC SDN file not found at {OFAC_FILE} — using demo list")
    SDN_INDEX = sanctions_matcher.load_index_from_text(
        "\n".join(
            f'{i},"{name}",-0- ,"DEMO",-0- ' for i, name in enumerate(DEMO_SANCTIONED_ENTITIES)
        ),
        cache_key="demo",
    )
    SDN_SOURCE = "demo"

logger.info(f"Loaded {len(SDN_INDEX)} OFAC SDN entries ({SDN_SOURCE})")


@mcp.tool()
def screen_sanctions(
    entity_name: str,
    entity_country: str = "",
    entity_address: str = "",
) -> dict[str, Any]:
    """
    Screen an entity against the official U.S. Treasury OFAC SDN list.

    [x402 GATED: $0.01/check — simulates paid compliance API like Chainalysis]

    Checks whether a counterparty name appears on the OFAC SDN list, and whether
    its name or country falls in a comprehensively sanctioned jurisdiction.

    Args:
        entity_name: Name of entity to screen (person or organization)
        entity_country: Country of entity (optional, for jurisdiction check)
        entity_address: XRPL address of entity (optional, for on-chain screening)

    Returns:
        Screening result: CLEAR (safe to transact) or BLOCKED (sanctions match).
    """
    logger.info(f"[x402] screen_sanctions called — fee: $0.01 — entity: {entity_name}")

    if not entity_name or not entity_name.strip():
        return {"error": "Missing required parameter: entity_name"}

    try:
        result = sanctions_matcher.screen_entity(
            entity_name,
            entity_country,
            entity_address,
            index=SDN_INDEX,
        )
    except Exception as exc:  # fail closed: a screening failure must not clear a payment
        logger.exception("screen_sanctions failed")
        result = {
            "status": "BLOCKED",
            "entity_name": entity_name,
            "entity_country": entity_country,
            "entity_address": entity_address,
            "matches": [],
            "reason": f"Screening error, failing closed: {exc}",
            "confidence": 1.0,
            "data_source": sanctions_matcher.DATA_SOURCE,
            "sdn_entries_searched": len(SDN_INDEX),
        }

    result["x402_fee_charged"] = "$0.01"
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Server entrypoint
# ─────────────────────────────────────────────────────────────────────────────


def main():
    """Run the MCP server.
    
    AgentCore Runtime contract:
    - Host: 0.0.0.0
    - Port: 8000
    - Path: /mcp (default for streamable-http)
    - Transport: streamable-http with stateless_http=True
    
    For local dev, set MCP_TRANSPORT=stdio
    """
    import os
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    
    if transport in ("sse", "streamable-http"):
        # AgentCore Runtime mode
        mcp.run(transport="streamable-http")
    else:
        # Local development mode — stdio
        mcp.run()


if __name__ == "__main__":
    main()

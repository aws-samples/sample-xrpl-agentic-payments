# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shared XRPL tool logic for XRPL Agentic Payments.

There are two deployments of the same 8 tools — the Lambda tools behind the
AgentCore Gateway (``functions/*.py``) and the MCP server
(``src/mcp_server/server.py``). Anything the two MUST agree on lives here and is
imported by both, so the implementations cannot drift:

* drops conversion (exact, never binary float)
* on-ledger attribution (SourceTag + memo) — this is an on-ledger contract
* payment path ranking
* DEX order book math (BASE/QUOTE convention)
* AccountLines pagination

This module has no AWS dependencies (unlike ``shared.py``) so it is safe to
import from the MCP server and from unit tests.

Packaging: this file must live in ``functions/`` because that directory is the
Lambda code asset (infra/lib/tools-stack.ts uses ``Code.fromAsset(functionsDir)``),
where it is importable as the top-level module ``xrpl_core``. ``deploy/package.sh``
and the ``Dockerfile`` copy it to ``<artifact root>/functions/`` for the MCP
server, which imports it as ``functions.xrpl_core`` — same as a repo checkout.
"""

import json
from decimal import Decimal
from typing import Any, Optional

from xrpl.constants import XRPLException
from xrpl.models import AccountLines, Memo
from xrpl.utils import drops_to_xrp, xrp_to_drops

# ─────────────────────────────────────────────────────────────────────────────
# On-ledger attribution (XRPL agent behaviour tracking)
# https://xrpl.org/docs/agents/track-agent-behavior
#
# ⚠ These three values are an on-ledger contract: existing transactions are
# already tagged with them and consumers filter on them. Do not change them.
# ─────────────────────────────────────────────────────────────────────────────

# XRPL AI Starter Kit default SourceTag — identifies all agentic transactions
# on-chain. Any XRPL data API can filter by this tag to find agent activity.
XRPL_AGENTIC_PAYMENTS_SOURCE_TAG = 20260530

# Agent identity recorded in every attribution memo.
ATTRIBUTION_AGENT_ID = "xrpl-agentic-payments-payment-agent"

# "text/json" in hex — the MemoType of every attribution memo.
ATTRIBUTION_MEMO_TYPE = "746578742F6A736F6E"


def build_attribution_memo(session_id: str, action: str, task_id: str) -> Memo:
    """
    Build a hex-encoded JSON memo for on-chain attribution.

    Per XRPL docs: "Where SourceTag answers 'which agent sent this?',
    Memos answer 'why, in what context, and as part of which task?'"

    The memo keys (agent_id, session_id, action, task_id) and the memo_type are
    part of the on-ledger contract — see the warning above.
    """
    payload = json.dumps({
        "agent_id": ATTRIBUTION_AGENT_ID,
        "session_id": session_id,
        "action": action,
        "task_id": task_id,
    }, separators=(",", ":"))
    return Memo(
        memo_data=payload.encode().hex().upper(),
        memo_type=ATTRIBUTION_MEMO_TYPE,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Drops conversion
# ─────────────────────────────────────────────────────────────────────────────


class XrpAmountError(ValueError):
    """Raised for an XRP amount that cannot be represented in drops."""


def xrp_to_drops_str(amount: Any) -> str:
    """
    Convert a whole-XRP amount to a drops string, exactly.

    ``int(float(amount) * 1_000_000)`` truncates binary float error toward zero:
    ``int(float("8.29") * 1_000_000)`` is 8289999, i.e. the payment silently
    ships 0.000001 XRP less than the caller asked for (and the response still
    echoes "8.29"). Going through Decimal keeps the decimal digits exact, and
    xrpl.utils.xrp_to_drops additionally validates the XRP range and precision.

    Raises:
        XrpAmountError: amount is not a number, negative, or out of XRP range.
    """
    try:
        # str() first: a float argument would already carry binary error, and
        # xrp_to_drops rejects raw strings to stop drops being passed as XRP.
        return xrp_to_drops(Decimal(str(amount).strip()))
    except (ArithmeticError, ValueError, TypeError, XRPLException) as exc:
        raise XrpAmountError(f"Invalid XRP amount {amount!r}: {exc}") from exc


def drops_to_xrp_float(drops: Any) -> float:
    """Convert a drops amount to whole XRP as a float (for display/comparison)."""
    return float(drops_to_xrp(str(drops).strip()))


# ─────────────────────────────────────────────────────────────────────────────
# Payment path ranking (get_paths / path_find)
# ─────────────────────────────────────────────────────────────────────────────


def describe_source_amount(source_amount: Any) -> dict[str, Any]:
    """
    Normalize a ripple_path_find ``source_amount`` (drops string or token object).

    Returns a dict with ``display`` ("10.5 XRP"), ``value`` (float) and
    ``currency``.
    """
    if isinstance(source_amount, str):
        value = drops_to_xrp_float(source_amount)
        return {"display": f"{value} XRP", "value": value, "currency": "XRP"}

    currency = source_amount.get("currency", "?")
    raw_value = source_amount.get("value", "?")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = 0.0
    return {"display": f"{raw_value} {currency}", "value": value, "currency": currency}


def rank_path_alternatives(alternatives: list[dict]) -> dict[str, Any]:
    """
    Rank ripple_path_find alternatives by source cost.

    Costs in different source currencies are NOT comparable as bare numbers —
    5 USD is not cheaper than 10 XRP just because 5 < 10. So the sort key
    carries the currency, and a single ``recommended_path`` is only returned
    when every alternative is denominated in the same source currency.
    ``rank`` is assigned after sorting, never before.

    Returns:
        {"paths": [...], "recommended_path": dict|None, "cost_comparable": bool,
         "cheapest_per_currency": {currency: path}, "ranking_note": str|None}
    """
    paths = []
    for alt in alternatives:
        cost = describe_source_amount(alt.get("source_amount", {}))
        paths_computed = alt.get("paths_computed") or []
        paths.append({
            "source_cost": cost["display"],
            "cost_value": cost["value"],
            "cost_currency": cost["currency"],
            "hops": len(paths_computed[0]) if paths_computed else 0,
            "estimated_time_seconds": 4,  # XRPL finality is 3-5 sec
        })

    # Group by currency first (deterministic), cheapest first within a currency.
    paths.sort(key=lambda p: (p["cost_currency"], p["cost_value"]))
    for i, path in enumerate(paths):
        path["rank"] = i + 1

    cheapest_per_currency: dict[str, Any] = {}
    for path in paths:
        cheapest_per_currency.setdefault(path["cost_currency"], path)

    comparable = len(cheapest_per_currency) <= 1
    recommended = paths[0] if (paths and comparable) else None

    note = None
    if not comparable:
        currencies = ", ".join(sorted(cheapest_per_currency))
        note = (
            f"Alternatives are denominated in {len(cheapest_per_currency)} different "
            f"source currencies ({currencies}); their costs are not comparable without "
            "an FX rate. Paths are ranked within each currency only and no single "
            "cheapest path is recommended — see cheapest_per_currency."
        )

    return {
        "paths": paths,
        "recommended_path": recommended,
        "cost_comparable": comparable,
        "cheapest_per_currency": cheapest_per_currency,
        "ranking_note": note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DEX order book (get_orderbook)
#
# Convention: for the pair BASE/QUOTE, `price` is QUOTE units per 1 BASE unit
# and `size` is a BASE quantity — on both sides of the book.
#
# XRPL offers name their sides from the taker's point of view: an offer with
# TakerGets=X, TakerPays=Y sells X for Y. So
#   asks (someone sells BASE for QUOTE): TakerGets=BASE, TakerPays=QUOTE
#   bids (someone sells QUOTE for BASE): TakerGets=QUOTE, TakerPays=BASE
# ─────────────────────────────────────────────────────────────────────────────


def _offer_leg(leg: Any) -> tuple[float, str]:
    """Normalize an offer leg (drops string or token object) to (amount, currency)."""
    if isinstance(leg, str):
        return drops_to_xrp_float(leg), "XRP"
    try:
        amount = float(leg.get("value", "0"))
    except (TypeError, ValueError):
        amount = 0.0
    return amount, leg.get("currency", "?")


def format_book_offers(offers: list[dict], side: str, limit: int) -> list[dict]:
    """
    Format raw BookOffers entries into BASE/QUOTE price levels.

    Args:
        offers: raw ``offers`` array from a BookOffers response
        side: "asks" (offer's TakerGets is BASE) or "bids" (TakerPays is BASE)
        limit: max levels to return

    Offers with a zero-sized base leg are skipped rather than divided by.
    """
    if side not in ("asks", "bids"):
        raise ValueError(f"side must be 'asks' or 'bids', got {side!r}")

    levels = []
    for offer in offers[:limit]:
        gets_amount, gets_currency = _offer_leg(offer.get("TakerGets", {}))
        pays_amount, pays_currency = _offer_leg(offer.get("TakerPays", {}))

        if side == "asks":
            base_amount, base_currency = gets_amount, gets_currency
            quote_amount, quote_currency = pays_amount, pays_currency
        else:
            base_amount, base_currency = pays_amount, pays_currency
            quote_amount, quote_currency = gets_amount, gets_currency

        if base_amount <= 0:
            continue  # nothing to price against; skip instead of dividing by zero

        levels.append({
            "price": round(quote_amount / base_amount, 6),  # QUOTE per 1 BASE
            "size": base_amount,                            # BASE quantity
            "total": quote_amount,                          # QUOTE quantity
            "base_currency": base_currency,
            "quote_currency": quote_currency,
            # Retained for callers that read the raw offer legs.
            "gets_currency": gets_currency,
            "pays_currency": pays_currency,
        })

    return levels


def orderbook_summary(asks: list[dict], bids: list[dict]) -> dict[str, Any]:
    """
    Best bid/ask, spread and mid-price for a formatted book.

    Any of these is None when the corresponding side of the book is empty, so an
    empty or one-sided book never divides by zero.
    """
    best_ask = min((a["price"] for a in asks), default=None)
    best_bid = max((b["price"] for b in bids), default=None)

    spread = mid = spread_pct = None
    if best_ask is not None and best_bid is not None:
        spread = round(best_ask - best_bid, 6)
        mid = round((best_ask + best_bid) / 2, 6)
        if mid > 0:
            spread_pct = round(spread / mid * 100, 4)

    return {
        "best_ask": best_ask,
        "best_bid": best_bid,
        "spread": spread,
        "spread_pct": spread_pct,
        "mid": mid,
    }


# ─────────────────────────────────────────────────────────────────────────────
# AccountLines pagination
# ─────────────────────────────────────────────────────────────────────────────

# rippled returns at most a few hundred trust lines per page. Without following
# the marker, accounts with many trust lines silently report a truncated set —
# i.e. a balance query that omits balances. The page cap keeps a pathological
# account from hanging the tool: 25 pages x 400 lines = 10,000 trust lines.
ACCOUNT_LINES_PAGE_LIMIT = 400
ACCOUNT_LINES_MAX_PAGES = 25


def fetch_account_lines(
    client: Any,
    account: str,
    page_limit: int = ACCOUNT_LINES_PAGE_LIMIT,
    max_pages: int = ACCOUNT_LINES_MAX_PAGES,
) -> dict[str, Any]:
    """
    Fetch all trust lines for an account, following the ``marker`` field.

    Returns:
        {"lines": [...], "truncated": bool, "error": str|None}
        ``truncated`` is True when the page cap was hit and more lines exist.
        ``error`` is set (and lines holds whatever was retrieved) if a page
        request fails.
    """
    lines: list[dict] = []
    marker: Optional[Any] = None
    truncated = False

    for page in range(max_pages):
        req = AccountLines(account=account, limit=page_limit, marker=marker)
        response = client.request(req)
        if not response.is_successful():
            return {
                "lines": lines,
                "truncated": truncated,
                "error": response.result.get("error", "unknown"),
            }

        lines.extend(response.result.get("lines", []))
        marker = response.result.get("marker")
        if not marker:
            break
    else:
        # Loop finished without breaking and a marker is still pending.
        truncated = marker is not None

    return {"lines": lines, "truncated": truncated, "error": None}

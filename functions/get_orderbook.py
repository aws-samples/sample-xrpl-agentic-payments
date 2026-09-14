# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments Tool: get_orderbook

Query the XRPL DEX order book for a trading pair.
Read-only, no ledger writes.

Convention: for the pair BASE/QUOTE, `price` is QUOTE per 1 BASE and `size` is a
BASE quantity, on both sides of the book. Both sides are queried so the spread
and mid-price are real numbers rather than an inference from one side.
"""

from shared import xrpl_client, RLUSD_ISSUER, logger
from xrpl.models import BookOffers, IssuedCurrency
from xrpl.models.currencies import XRP as XRPCurrency

from xrpl_core import format_book_offers, orderbook_summary


def _currency(code: str, issuer: str):
    """Build a BookOffers currency object (XRP has no issuer)."""
    if code.upper() == "XRP":
        return XRPCurrency()
    return IssuedCurrency(currency=code, issuer=issuer)


def _fetch_offers(taker_gets, taker_pays, limit: int):
    """Request one side of the book. Returns (offers, error)."""
    response = xrpl_client.request(
        BookOffers(taker_gets=taker_gets, taker_pays=taker_pays, limit=limit)
    )
    if not response.is_successful():
        return [], response.result.get("error", "unknown")
    return response.result.get("offers", []), None


def lambda_handler(event, context):
    base_currency = event.get("base_currency", "USD")
    base_issuer = event.get("base_issuer", "")
    quote_currency = event.get("quote_currency", "XRP")
    quote_issuer = event.get("quote_issuer", "")
    limit = int(event.get("limit", 10))

    logger.info(f"get_orderbook: {base_currency}/{quote_currency} limit={limit}")

    effective_base_issuer = base_issuer if base_issuer else RLUSD_ISSUER
    effective_quote_issuer = quote_issuer if quote_issuer else RLUSD_ISSUER

    base = _currency(base_currency, effective_base_issuer)
    quote = _currency(quote_currency, effective_quote_issuer)

    # Asks: offers selling BASE for QUOTE (TakerGets=BASE, TakerPays=QUOTE).
    ask_offers, error = _fetch_offers(taker_gets=base, taker_pays=quote, limit=limit)
    if error:
        return {"error": f"Order book query failed: {error}"}

    # Bids: the mirror book — offers selling QUOTE for BASE.
    bid_offers, error = _fetch_offers(taker_gets=quote, taker_pays=base, limit=limit)
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
    }

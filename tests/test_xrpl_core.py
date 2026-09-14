# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for functions/xrpl_core.py — the logic shared by the Gateway Lambda
tools and the MCP server.

These cover the numeric defects that were silent in production: float drops
conversion, cross-currency path ranking, and the inverted order book price.
Only pure functions are tested; nothing here talks to XRPL.
"""

import ast
import sys
from pathlib import Path

import pytest

# xrpl_core lives in functions/ because that directory is the Lambda code asset.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from functions.xrpl_core import (  # noqa: E402
    ACCOUNT_LINES_PAGE_LIMIT,
    ATTRIBUTION_MEMO_TYPE,
    XRPL_AGENTIC_PAYMENTS_SOURCE_TAG,
    XrpAmountError,
    build_attribution_memo,
    fetch_account_lines,
    format_book_offers,
    orderbook_summary,
    rank_path_alternatives,
    xrp_to_drops_str,
)


# ─────────────────────────────────────────────────────────────────────────────
# Drops conversion (F12)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("8.29", "8290000"),      # int(float("8.29") * 1_000_000) == 8289999
        ("20.87", "20870000"),
        ("0.000001", "1"),
        ("100", "100000000"),
        ("0", "0"),
        (8.29, "8290000"),
        (5, "5000000"),
    ],
)
def test_xrp_to_drops_str_is_exact(amount, expected):
    assert xrp_to_drops_str(amount) == expected


def test_xrp_to_drops_str_fixes_float_truncation():
    """The old conversion truncated binary float error toward zero."""
    assert int(float("8.29") * 1_000_000) == 8289999  # the bug
    assert xrp_to_drops_str("8.29") == "8290000"      # the fix


@pytest.mark.parametrize(
    "amount",
    ["abc", "", "-1", "1e20", "0.0000001", None],
)
def test_xrp_to_drops_str_rejects_bad_amounts(amount):
    """Out-of-range/garbage input raises a catchable error, not a stray exception."""
    with pytest.raises(XrpAmountError):
        xrp_to_drops_str(amount)


# ─────────────────────────────────────────────────────────────────────────────
# On-ledger attribution (F10)
# ─────────────────────────────────────────────────────────────────────────────


def test_attribution_contract_is_unchanged():
    assert XRPL_AGENTIC_PAYMENTS_SOURCE_TAG == 20260530
    assert ATTRIBUTION_MEMO_TYPE == "746578742F6A736F6E"

    memo = build_attribution_memo(session_id="s1", action="submit_payment", task_id="t1")
    assert memo.memo_type == ATTRIBUTION_MEMO_TYPE
    payload = bytes.fromhex(memo.memo_data).decode()
    assert payload == (
        '{"agent_id":"xrpl-agentic-payments-payment-agent",'
        '"session_id":"s1","action":"submit_payment","task_id":"t1"}'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Path ranking (F11)
# ─────────────────────────────────────────────────────────────────────────────


def _alt(source_amount, hops=1):
    return {"source_amount": source_amount, "paths_computed": [[{}] * hops]}


def test_rank_single_currency_sorts_and_renumbers():
    ranking = rank_path_alternatives([
        _alt("30000000"),  # 30 XRP
        _alt("10000000"),  # 10 XRP
        _alt("20000000"),  # 20 XRP
    ])

    assert [p["cost_value"] for p in ranking["paths"]] == [10.0, 20.0, 30.0]
    # rank is assigned AFTER sorting (it used to be 2, 1, 3)
    assert [p["rank"] for p in ranking["paths"]] == [1, 2, 3]
    assert ranking["cost_comparable"] is True
    assert ranking["recommended_path"]["source_cost"] == "10.0 XRP"
    assert ranking["ranking_note"] is None


def test_rank_does_not_compare_across_currencies():
    """10 XRP vs 5 USD: 5 < 10 does not make the USD route cheaper."""
    ranking = rank_path_alternatives([
        _alt("10000000"),                                   # 10 XRP
        _alt({"currency": "USD", "value": "5"}),            # 5 USD
        _alt({"currency": "USD", "value": "4"}),            # 4 USD
    ])

    assert ranking["cost_comparable"] is False
    assert ranking["recommended_path"] is None              # no silent guess
    assert "not comparable" in ranking["ranking_note"]
    assert [p["rank"] for p in ranking["paths"]] == [1, 2, 3]

    # Ranked within a currency only, and the cheapest of each is reported.
    by_currency = ranking["cheapest_per_currency"]
    assert by_currency["USD"]["source_cost"] == "4 USD"
    assert by_currency["XRP"]["source_cost"] == "10.0 XRP"
    for currency in ("USD", "XRP"):
        costs = [p["cost_value"] for p in ranking["paths"] if p["cost_currency"] == currency]
        assert costs == sorted(costs)


def test_rank_empty_alternatives():
    ranking = rank_path_alternatives([])
    assert ranking["paths"] == []
    assert ranking["recommended_path"] is None
    assert ranking["cost_comparable"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Order book math (F13) — pair USD/XRP: price is XRP per 1 USD, size is USD
# ─────────────────────────────────────────────────────────────────────────────

# Offer selling 100 USD for 250 XRP → 2.5 XRP per USD.
ASK_OFFER = {
    "TakerGets": {"currency": "USD", "issuer": "rIssuer", "value": "100"},
    "TakerPays": "250000000",
}
# Offer selling 240 XRP for 100 USD → 2.4 XRP per USD.
BID_OFFER = {
    "TakerGets": "240000000",
    "TakerPays": {"currency": "USD", "issuer": "rIssuer", "value": "100"},
}


def test_ask_price_is_quote_per_base():
    (ask,) = format_book_offers([ASK_OFFER], "asks", 10)
    assert ask["price"] == 2.5      # XRP per USD, not the 0.4 reciprocal
    assert ask["size"] == 100.0     # USD (base) quantity, not the XRP amount
    assert ask["total"] == 250.0
    assert ask["base_currency"] == "USD"
    assert ask["quote_currency"] == "XRP"


def test_bid_price_uses_the_same_convention():
    (bid,) = format_book_offers([BID_OFFER], "bids", 10)
    assert bid["price"] == 2.4
    assert bid["size"] == 100.0
    assert bid["base_currency"] == "USD"


def test_offers_with_zero_base_are_skipped_not_divided_by():
    zero = {"TakerGets": {"currency": "USD", "value": "0"}, "TakerPays": "250000000"}
    assert format_book_offers([zero], "asks", 10) == []


def test_format_book_offers_respects_limit():
    assert len(format_book_offers([ASK_OFFER] * 5, "asks", 2)) == 2


def test_orderbook_summary_spread_and_mid():
    asks = format_book_offers([ASK_OFFER], "asks", 10)
    bids = format_book_offers([BID_OFFER], "bids", 10)
    summary = orderbook_summary(asks, bids)

    assert summary["best_ask"] == 2.5
    assert summary["best_bid"] == 2.4
    assert summary["spread"] == pytest.approx(0.1)
    assert summary["mid"] == pytest.approx(2.45)
    assert summary["spread_pct"] == pytest.approx(4.0816, abs=1e-4)


@pytest.mark.parametrize("asks,bids", [([], []), ([ASK_OFFER], []), ([], [BID_OFFER])])
def test_orderbook_summary_handles_one_sided_book(asks, bids):
    """An empty side must yield None, never a ZeroDivisionError."""
    summary = orderbook_summary(
        format_book_offers(asks, "asks", 10),
        format_book_offers(bids, "bids", 10),
    )
    assert summary["spread"] is None
    assert summary["mid"] is None


# ─────────────────────────────────────────────────────────────────────────────
# AccountLines pagination
# ─────────────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, result, successful=True):
        self.result = result
        self._successful = successful

    def is_successful(self):
        return self._successful


class _FakeClient:
    """Returns one AccountLines page per request, echoing the marker it got."""

    def __init__(self, pages, fail_after=None):
        self.pages = pages
        self.fail_after = fail_after
        self.requests = []

    def request(self, req):
        self.requests.append(req)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            return _FakeResponse({"error": "actNotFound"}, successful=False)
        return _FakeResponse(self.pages[len(self.requests) - 1])


def test_fetch_account_lines_follows_marker():
    client = _FakeClient([
        {"lines": [{"currency": "USD"}], "marker": "m1"},
        {"lines": [{"currency": "EUR"}], "marker": "m2"},
        {"lines": [{"currency": "GBP"}]},
    ])

    result = fetch_account_lines(client, "rAccount")

    assert [line["currency"] for line in result["lines"]] == ["USD", "EUR", "GBP"]
    assert result["truncated"] is False
    assert result["error"] is None
    assert [req.marker for req in client.requests] == [None, "m1", "m2"]
    assert client.requests[0].limit == ACCOUNT_LINES_PAGE_LIMIT


def test_fetch_account_lines_caps_pages_and_flags_truncation():
    endless = [{"lines": [{"currency": "USD"}], "marker": f"m{i}"} for i in range(10)]
    client = _FakeClient(endless)

    result = fetch_account_lines(client, "rAccount", max_pages=3)

    assert len(client.requests) == 3
    assert len(result["lines"]) == 3
    assert result["truncated"] is True


def test_fetch_account_lines_reports_page_error_with_partial_data():
    client = _FakeClient(
        [{"lines": [{"currency": "USD"}], "marker": "m1"}, {}],
        fail_after=1,
    )

    result = fetch_account_lines(client, "rAccount")

    assert result["error"] == "actNotFound"
    assert len(result["lines"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# The Lambda tools must not patch asyncio
# ─────────────────────────────────────────────────────────────────────────────
# functions/shared.py cannot be imported here: it reads the wallets secret from
# Secrets Manager at module scope. So this parses it instead.
#
# Parsed, not grepped: shared.py explains this hazard in a comment that names
# the call it must not make, and a substring check matches its own
# documentation.


def test_lambda_tools_do_not_apply_nest_asyncio():
    """nest_asyncio.apply() in functions/shared.py breaks every tool on py3.14.

    xrpl-py's sync wrappers call asyncio.run(), and every handler in functions/
    is a plain `def`, so there is no already-running loop for a nested one to sit
    inside — the only situation nest_asyncio addresses. Applying it anyway
    monkeypatches asyncio's task bookkeeping; current_task() then returns None
    inside httpx's AsyncClient.__aexit__ and anyio raises

        TypeError: cannot create weak reference to 'NoneType' object

    during teardown, *after* the ledger request has already succeeded. Every
    tool call returns "An internal error occurred" while the XRPL data it
    fetched is discarded.

    Nothing in a test run reproduces this — it needs the deployed runtime — so
    the guard is here.
    """
    shared = Path(__file__).resolve().parent.parent / "functions" / "shared.py"
    tree = ast.parse(shared.read_text())

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "nest_asyncio" not in imported

    applied = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "apply"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "nest_asyncio"
    ]
    assert applied == []


def test_every_lambda_handler_is_sync():
    """The premise of the test above: no handler in functions/ is async.

    If one ever becomes `async def`, asyncio.run() inside xrpl-py would raise
    "cannot be called from a running event loop" and the reasoning for dropping
    nest_asyncio no longer holds.
    """
    functions_dir = Path(__file__).resolve().parent.parent / "functions"
    offenders = [
        path.name
        for path in sorted(functions_dir.glob("*.py"))
        if "async def lambda_handler" in path.read_text()
    ]

    assert offenders == []

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Sanctions screening regression tests.

Both screening layers — the screen_sanctions Lambda behind the AgentCore Gateway
and the screen_sanctions MCP tool — must return the *same* verdict for the same
entity, because they are the same control implemented twice. These tests pin the
verdicts that previously diverged, and they run against the real OFAC SDN CSV in
data/ofac_sdn.csv.

The Lambda module is imported with a stubbed boto3 so no AWS credentials (and no
network) are needed; the matcher itself is stdlib-only.
"""

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OFAC_CSV = REPO_ROOT / "data" / "ofac_sdn.csv"

# functions/ is the Lambda bundle root, so its modules import flat there — put it
# on sys.path so the handler resolves `sanctions_matcher` exactly as in Lambda.
for _path in (str(REPO_ROOT), str(REPO_ROOT / "functions")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

pytestmark = pytest.mark.skipif(
    not OFAC_CSV.exists(),
    reason=f"OFAC SDN CSV not present at {OFAC_CSV}",
)

# entity_name → expected verdict. Every one of these returned opposite verdicts
# from the two layers before the shared matcher was introduced.
VERDICT_CASES = [
    # "ABBAS, Abu" (SDGT) — comma-swapped individual, must not be missed
    ("Abu Abbas", "BLOCKED"),
    # 4-char organisation name; a >5-char floor silently skipped it
    ("ISIS", "BLOCKED"),
    # Full SDN name inside a long free-text payment reference (25/59 chars)
    ("Invoice payment to ANGLO-CARIBBEAN CO., LTD. for consulting", "BLOCKED"),
    # The 1-token SDN entry "MARIA" must not block an ordinary person
    ("Maria Garcia", "CLEAR"),
    # 2-char token that appears in 700+ SDN names
    ("Al", "CLEAR"),
    # "AL ZAWAHIRI, Dr. Ayman" (SDGT) in natural order
    ("Ayman Al Zawahiri", "BLOCKED"),
    ("Acme Corp LLC", "CLEAR"),
]


@pytest.fixture(scope="module")
def matcher():
    import sanctions_matcher

    return sanctions_matcher


@pytest.fixture(scope="module")
def index(matcher):
    return matcher.load_index_from_path(OFAC_CSV)


@pytest.fixture(scope="module")
def lambda_handler():
    """The real Lambda handler, with boto3 stubbed to serve the local CSV."""
    csv_bytes = OFAC_CSV.read_bytes()

    class _Body:
        def read(self):
            return csv_bytes

    class _S3:
        def get_object(self, Bucket, Key):  # noqa: N803 - boto3 kwarg names
            return {"Body": _Body()}

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda *args, **kwargs: _S3()

    saved_boto3 = sys.modules.get("boto3")
    saved_module = sys.modules.pop("screen_sanctions", None)
    sys.modules["boto3"] = fake_boto3
    try:
        import screen_sanctions

        yield screen_sanctions.lambda_handler
    finally:
        sys.modules.pop("screen_sanctions", None)
        if saved_boto3 is not None:
            sys.modules["boto3"] = saved_boto3
        else:
            sys.modules.pop("boto3", None)
        if saved_module is not None:
            sys.modules["screen_sanctions"] = saved_module


@pytest.fixture(scope="module")
def mcp_screen():
    """The screen_sanctions MCP tool function."""
    pytest.importorskip("mcp.server.fastmcp", reason="MCP server deps not installed")
    pytest.importorskip("xrpl", reason="MCP server deps not installed")
    from src.mcp_server.server import screen_sanctions

    return screen_sanctions


@pytest.mark.parametrize("entity_name,expected", VERDICT_CASES)
def test_both_layers_agree_on_verdict(entity_name, expected, lambda_handler, mcp_screen):
    lambda_result = lambda_handler({"entity_name": entity_name}, None)
    mcp_result = mcp_screen(entity_name)

    assert lambda_result["status"] == expected
    assert mcp_result["status"] == expected

    # Identical payloads apart from the MCP-only x402 receipt field.
    assert {k: v for k, v in mcp_result.items() if k != "x402_fee_charged"} == lambda_result


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_entity_name_errors_instead_of_blocking(blank, lambda_handler, mcp_screen):
    assert lambda_handler({"entity_name": blank}, None) == {
        "error": "Missing required parameter: entity_name"
    }
    assert mcp_screen(blank) == {"error": "Missing required parameter: entity_name"}


def test_comma_swapped_individual_matches_natural_order(matcher, index):
    result = matcher.screen_entity("Abu Abbas", index=index)
    assert result["status"] == "BLOCKED"
    assert result["matches"][0]["name"] == "ABBAS, ABU"
    assert result["matches"][0]["match_type"] == "exact"


def test_full_sdn_name_in_long_free_text_is_blocked(matcher, index):
    result = matcher.screen_entity(
        "Invoice payment to ANGLO-CARIBBEAN CO., LTD. for consulting", index=index
    )
    assert result["status"] == "BLOCKED"
    assert result["matches"][0]["name"] == "ANGLO-CARIBBEAN CO., LTD."


def test_matches_report_program_not_country(matcher, index):
    """Column 3 of SDN.csv is the OFAC program, so it must not be labelled country."""
    match = matcher.screen_entity("Abu Abbas", index=index)["matches"][0]
    assert match["program"] == "SDGT"
    assert "country" not in match


def test_jurisdiction_check_is_separate_and_labelled(matcher, index):
    result = matcher.screen_entity("Some Trading Co", "Iran", index=index)
    assert result["status"] == "BLOCKED"
    assert result["matches"][0]["match_type"] == "jurisdiction"
    # Word boundaries, not substrings: MIRANDA must not read as IRAN.
    assert matcher.screen_entity("Miranda Rodriguez", index=index)["status"] == "CLEAR"


def test_matches_are_dicts_with_name_score_program(index, lambda_handler, mcp_screen):
    for result in (
        lambda_handler({"entity_name": "Abu Abbas"}, None),
        mcp_screen("Abu Abbas"),
    ):
        assert result["matches"], "expected at least one match"
        for match in result["matches"]:
            assert {"name", "score", "program"} <= set(match)


def test_placeholder_and_header_rows_are_skipped(matcher, index):
    names = {entry["name"] for entry in index.entries}
    assert "-0-" not in names
    assert "SDN_NAME" not in names

    with_header = matcher.parse_sdn_csv(
        'ent_num,SDN_Name,SDN_Type,Program\n'
        '1,"REAL ENTITY, Some",individual,"SDGT"\n'
        '2,-0- ,-0- ,-0- \n'
    )
    assert [entry["name"] for entry in with_header.entries] == ["REAL ENTITY, SOME"]


def test_csv_is_parsed_once_and_cached(matcher):
    assert matcher.load_index_from_path(OFAC_CSV) is matcher.load_index_from_path(OFAC_CSV)

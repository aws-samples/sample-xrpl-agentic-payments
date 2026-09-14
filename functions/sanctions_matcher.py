# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shared OFAC SDN matcher for XRPL Agentic Payments.

This is the single implementation of sanctions matching. Both screening layers
call it so they cannot return different verdicts for the same input:

  * functions/screen_sanctions.py — the Gateway Lambda (loads the CSV from S3)
  * src/mcp_server/server.py      — the screen_sanctions MCP tool (loads the
                                    CSV from the local data/ directory)

The module lives in functions/ because that directory *is* the Lambda code
asset (infra/lib/tools-stack.ts uses lambda.Code.fromAsset("functions")), so
the Lambda gets it with no packaging change. The MCP server's container and
AgentCore zip copy this one file next to src/ (see Dockerfile and
deploy/package.sh) and put it on sys.path.

Deliberately stdlib-only: no boto3, no xrpl-py, no AWS credentials needed, so
the matching logic is importable and testable on its own.

SDN.csv column layout (no header row in the Treasury file):
    0 ent_num, 1 SDN_Name, 2 SDN_Type, 3 Program, 4 Title, ... 11 Remarks
Column 3 is the OFAC *program* (SDGT, CUBA, RUSSIA-EO14024, ...), not a
country — see the program/jurisdiction split in screen_entity().
"""

import csv
import io
import re
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

# Floor for whole-token (word-boundary) matching. Must stay low enough that
# genuinely short SDN names still screen — e.g. "ISIS", which is a whole token
# of ISIS-EGYPT / ISIS-SOMALIA — but high enough to reject noise like "AL",
# which is a standalone token in 700+ SDN names.
MIN_TOKEN_MATCH_CHARS = 4

# Floor for loose (non-word-boundary) substring matching, which is much easier
# to trip accidentally, so it gets a higher floor.
MIN_LOOSE_MATCH_CHARS = 6

# An SDN name only counts as "contained in the query" if it is substantial:
# 6+ characters or 2+ tokens. Without this, the 1-token 5-char SDN entry
# "MARIA" blocks every payment to a "Maria Garcia".
MIN_SDN_IN_QUERY_CHARS = 6

MAX_MATCHES = 5

DATA_SOURCE = "U.S. Treasury OFAC SDN List (official)"

# Comprehensively sanctioned jurisdictions. Checked against the entity's
# country and name as a separate, clearly labelled signal — this is a
# jurisdiction test, not an OFAC program comparison.
BLOCKED_JURISDICTIONS = [
    "NORTH KOREA", "DPRK", "IRAN", "SYRIA", "CUBA", "CRIMEA",
    "DONETSK", "LUHANSK",
]

_PLACEHOLDER = "-0-"
_HEADER_NAMES = {"SDN_NAME", "SDN NAME", "NAME"}


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────────────────────


def normalize_tokens(name: str) -> list[str]:
    """Uppercase and split on anything that is not alphanumeric.

    Punctuation must be stripped *before* tokenising: OFAC stores individuals
    as "LASTNAME, First", so a bare .split() leaves "ABBAS," glued together
    and "Abu Abbas" never matches "ABBAS, Abu".
    """
    return re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()


def _comma_swapped_tokens(name: str) -> Optional[list[str]]:
    """Token list for the natural-order reading of "LASTNAME, First"."""
    if "," not in name:
        return None
    surname, rest = name.split(",", 1)
    if not rest.strip() or not surname.strip():
        return None
    return normalize_tokens(f"{rest} {surname}")


def _token_run(needle: list[str], haystack: list[str]) -> bool:
    """True if `needle` appears in `haystack` as a contiguous run of whole tokens."""
    n = len(needle)
    if not n or n > len(haystack):
        return False
    first = needle[0]
    for i in range(len(haystack) - n + 1):
        if haystack[i] == first and haystack[i:i + n] == needle:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# SDN index (parsed once, cached at module level)
# ─────────────────────────────────────────────────────────────────────────────


class SdnIndex:
    """Parsed SDN entries plus an exact-match index over their name forms."""

    __slots__ = ("entries", "by_form")

    def __init__(self, entries: list[dict[str, Any]]):
        self.entries = entries
        self.by_form: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            for form in entry["forms"]:
                self.by_form.setdefault(" ".join(form), []).append(entry)

    def __len__(self) -> int:
        return len(self.entries)


def parse_sdn_csv(csv_text: str) -> SdnIndex:
    """Parse the OFAC SDN CSV into an index. Skips placeholders and headers."""
    entries: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(csv_text)):
        if len(row) < 4:
            continue
        name = row[1].strip().strip('"').strip()
        if not name or name == _PLACEHOLDER or name.upper() in _HEADER_NAMES:
            continue
        program = row[3].strip().strip('"').strip()
        if program == _PLACEHOLDER:
            program = ""
        name_upper = name.upper()
        forms = [normalize_tokens(name_upper)]
        swapped = _comma_swapped_tokens(name_upper)
        # Index the comma-swapped form too so "Abu Abbas" hits "ABBAS, Abu".
        if swapped and swapped != forms[0]:
            forms.append(swapped)
        if not forms[0]:
            continue
        entries.append({"name": name_upper, "program": program.upper(), "forms": forms})
    return SdnIndex(entries)


_INDEX_CACHE: dict[str, SdnIndex] = {}


def load_index_from_text(csv_text: str, cache_key: str = "default") -> SdnIndex:
    """Parse and cache an index keyed by `cache_key` (parsed once per process)."""
    index = _INDEX_CACHE.get(cache_key)
    if index is None:
        index = parse_sdn_csv(csv_text)
        _INDEX_CACHE[cache_key] = index
    return index


def load_index_from_path(path) -> SdnIndex:
    """Parse and cache an index for a local CSV file (parsed once per process)."""
    key = f"path:{path}"
    index = _INDEX_CACHE.get(key)
    if index is None:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            index = parse_sdn_csv(handle.read())
        _INDEX_CACHE[key] = index
    return index


# ─────────────────────────────────────────────────────────────────────────────
# Matching
# ─────────────────────────────────────────────────────────────────────────────


def _hit(entry: dict[str, Any], match_type: str, score: float) -> dict[str, Any]:
    return {
        "name": entry["name"],
        "program": entry["program"],
        "match_type": match_type,
        "score": score,
    }


def _match_entry(
    query_tokens: list[str],
    query_norm: str,
    query_token_set: set,
    entry: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Non-exact match of one entry, or None. Exact hits come from the index."""
    for form in entry["forms"]:
        form_norm = " ".join(form)
        # Direction 1: the SDN name appears inside the query, on word
        # boundaries. No ratio gate — a full sanctioned name buried in a long
        # free-text payment reference is still a hit — but the name has to be
        # substantial so single short tokens do not fire.
        if len(form_norm) >= MIN_SDN_IN_QUERY_CHARS or len(form) >= 2:
            if form[0] in query_token_set and _token_run(form, query_tokens):
                return _hit(entry, "sdn_in_query", 0.95)

    for form in entry["forms"]:
        form_norm = " ".join(form)
        # Direction 2: the query appears inside the SDN name. Whole-token, so
        # the floor can stay low enough for real short names ("ISIS").
        if len(query_norm) >= MIN_TOKEN_MATCH_CHARS and _token_run(query_tokens, form):
            return _hit(entry, "query_in_sdn", 0.90)
        # Direction 3: loose substring, which can cut across token boundaries
        # and therefore carries the higher floor.
        if len(query_norm) >= MIN_LOOSE_MATCH_CHARS and query_norm in form_norm:
            return _hit(entry, "partial", 0.85)

    return None


def find_matches(
    entity_name: str,
    entity_country: str,
    index: SdnIndex,
) -> tuple[list[dict[str, Any]], float]:
    """Return (matches, top_score) for an entity. Matches are ordered strongest first."""
    query_tokens = normalize_tokens(entity_name)
    query_norm = " ".join(query_tokens)
    query_token_set = set(query_tokens)
    country_tokens = normalize_tokens(entity_country) if entity_country else []

    # 1. Jurisdiction screen. Token runs, not substrings: "IRAN" as a substring
    # also matches MIRANDA.
    jurisdiction_hits = []
    for jurisdiction in BLOCKED_JURISDICTIONS:
        tokens = normalize_tokens(jurisdiction)
        if _token_run(tokens, query_tokens) or _token_run(tokens, country_tokens):
            jurisdiction_hits.append({
                "name": jurisdiction,
                "program": "COMPREHENSIVE_JURISDICTION",
                "match_type": "jurisdiction",
                "score": 1.0,
            })
    if jurisdiction_hits:
        return jurisdiction_hits[:MAX_MATCHES], 1.0

    if not query_tokens:
        return [], 0.0

    # 2. Exact match on any indexed name form (original or comma-swapped).
    exact = index.by_form.get(query_norm)
    if exact:
        return [_hit(e, "exact", 1.0) for e in exact[:MAX_MATCHES]], 1.0

    # 3. Substring / containment directions.
    matches: list[dict[str, Any]] = []
    top_score = 0.0
    for entry in index.entries:
        hit = _match_entry(query_tokens, query_norm, query_token_set, entry)
        if hit:
            matches.append(hit)
            top_score = max(top_score, hit["score"])
            if len(matches) >= MAX_MATCHES:
                break
    if matches:
        return matches, top_score

    # 4. Word-overlap fuzzy match for reordered / partial personal names, e.g.
    # "Ayman Al Zawahiri" vs "AL ZAWAHIRI, Dr. Ayman".
    if len(query_tokens) >= 2:
        for entry in index.entries:
            for form in entry["forms"]:
                overlap = query_token_set & set(form)
                if len(overlap) >= len(query_tokens) - 1 and len(overlap) >= 2:
                    score = len(overlap) / max(len(query_tokens), len(form))
                    if score > 0.6:
                        matches.append(_hit(entry, "word_overlap", round(score, 3)))
                        top_score = max(top_score, score)
                        break
            if len(matches) >= MAX_MATCHES:
                break

    return matches, top_score


def screen_entity(
    entity_name: str,
    entity_country: str = "",
    entity_address: str = "",
    index: Optional[SdnIndex] = None,
) -> dict[str, Any]:
    """Screen an entity against the OFAC SDN list.

    Returns the screening result shared by both layers, or {"error": ...} for a
    blank entity_name (a missing argument is a caller bug, not a sanctions hit).
    """
    if index is None:
        raise ValueError("screen_entity requires a parsed SdnIndex")
    if not entity_name or not entity_name.strip():
        return {"error": "Missing required parameter: entity_name"}

    matches, top_score = find_matches(entity_name, entity_country, index)
    status = "BLOCKED" if matches else "CLEAR"

    return {
        "status": status,
        "entity_name": entity_name,
        "entity_country": entity_country,
        "entity_address": entity_address,
        "matches": matches[:MAX_MATCHES],
        "reason": (
            f"Matched OFAC SDN entry: {matches[0]['name']} "
            f"(program: {matches[0]['program'] or 'n/a'}, score: {matches[0]['score']})"
            if matches
            else "No matches found in OFAC SDN list"
        ),
        "confidence": top_score if matches else 0.01,
        "data_source": f"{DATA_SOURCE} ({len(index):,} entries)",
        "sdn_entries_searched": len(index),
    }

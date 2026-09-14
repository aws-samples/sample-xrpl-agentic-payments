# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
XRPL Agentic Payments Tool: screen_sanctions

Screen an entity against the official OFAC SDN (Specially Designated Nationals)
list from the U.S. Department of the Treasury.

Data source: https://www.treasury.gov/ofac/downloads/sdn.csv
The SDN CSV is loaded from S3 at Lambda init time and cached across invocations.

All matching lives in sanctions_matcher (same directory, so it ships in this
Lambda's code asset). The screen_sanctions MCP tool in src/mcp_server/server.py
calls the same module, so the two layers cannot drift apart.

Prototype rationale: The prototype screens against the official OFAC SDN list
downloaded from the U.S. Treasury, providing authoritative sanctions data without
runtime dependency on external government web services.

Production path: In production, this would be replaced by a commercial compliance
API (Chainalysis, Dow Jones, or Refinitiv) that provides real-time global sanctions
coverage, fuzzy matching with ML scoring, PEP screening, and audit-grade reporting.
"""

import logging
import os

import boto3

import sanctions_matcher

logger = logging.getLogger("xrpl_agentic_payments")
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────────────────────────
# Load OFAC SDN list from S3 at init time (cached across invocations)
# ─────────────────────────────────────────────────────────────────────────────

AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
S3_BUCKET = os.environ.get("ARTIFACTS_BUCKET", f"xrpl-agentic-payments-artifacts-{os.environ.get('AWS_ACCOUNT_ID', '')}")
S3_KEY = "data/ofac_sdn.csv"

s3 = boto3.client("s3", region_name=AWS_REGION)

logger.info("Loading OFAC SDN list from S3...")
_obj = s3.get_object(Bucket=S3_BUCKET, Key=S3_KEY)
_csv_content = _obj["Body"].read().decode("utf-8", errors="ignore")

# Parsed once at init and reused across invocations. Column 3 of SDN.csv is the
# OFAC program (SDGT, CUBA, RUSSIA-EO14024, ...), not a country.
SDN_INDEX = sanctions_matcher.load_index_from_text(_csv_content, cache_key=S3_KEY)

logger.info(f"Loaded {len(SDN_INDEX)} OFAC SDN entries")


def lambda_handler(event, context):
    entity_name = event.get("entity_name", "")
    entity_country = event.get("entity_country", "")
    entity_address = event.get("entity_address", "")

    if not entity_name or not entity_name.strip():
        return {"error": "Missing required parameter: entity_name"}

    logger.info(f"screen_sanctions: name={entity_name}, country={entity_country}")

    try:
        return sanctions_matcher.screen_entity(
            entity_name,
            entity_country,
            entity_address,
            index=SDN_INDEX,
        )
    except Exception as exc:  # fail closed: a screening failure must not clear a payment
        logger.exception("screen_sanctions failed")
        return {
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

#!/bin/bash

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# XRPL Agentic Payments MCP Server - Package for AgentCore Runtime (S3 Code Deploy)
#
# Creates a zip file that AgentCore Runtime will decompress to /var/task/
# Structure:
#   /var/task/
#     src/
#       __init__.py
#       mcp_server/
#         __init__.py
#         server.py
#     functions/
#       xrpl_core.py       (shared tool logic, also in the Lambda bundle)
#       sanctions_matcher.py
#     requirements.txt  (for reference only, deps are pre-installed in zip)
#     <installed packages from pip>
#
# NOTE: config/wallets.json is deliberately NOT in that list any more. See the
# "wallet material" section below.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$SCRIPT_DIR/build"
OUTPUT="$SCRIPT_DIR/mcp-server-v1.zip"

echo "=== XRPL Agentic Payments MCP Server Packaging ==="
echo "Project root: $PROJECT_ROOT"
echo "Build dir: $BUILD_DIR"
echo "Output: $OUTPUT"

# Clean previous build
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# 1. Install dependencies for linux/aarch64 (Graviton) into the build directory
# Using uv for fast, reliable cross-platform installs
echo ""
echo "--- Installing dependencies (target: aarch64-unknown-linux-gnu, Python 3.13) ---"
uv pip install \
    --python-platform aarch64-unknown-linux-gnu \
    --python-version 3.13 \
    --target "$BUILD_DIR" \
    -r "$SCRIPT_DIR/requirements.txt" 2>&1 | tail -20

# 2. Copy source code
echo ""
echo "--- Copying source code ---"
mkdir -p "$BUILD_DIR/src/mcp_server"
cp "$PROJECT_ROOT/src/__init__.py" "$BUILD_DIR/src/__init__.py"
cp "$PROJECT_ROOT/src/mcp_server/__init__.py" "$BUILD_DIR/src/mcp_server/__init__.py"
cp "$PROJECT_ROOT/src/mcp_server/server.py" "$BUILD_DIR/src/mcp_server/server.py"

# Shared tool logic (drops conversion, attribution memo, path ranking, order
# book math, AccountLines pagination) imported by both the MCP server and the
# Gateway Lambdas. Like the matcher below it lives in functions/ because that
# directory is the Lambda code asset, so it lands at <zip root>/functions/.
mkdir -p "$BUILD_DIR/functions"
cp "$PROJECT_ROOT/functions/xrpl_core.py" "$BUILD_DIR/functions/xrpl_core.py"

# Sanctions matcher shared with the screen_sanctions Gateway Lambda (same deal).
cp "$PROJECT_ROOT/functions/sanctions_matcher.py" "$BUILD_DIR/functions/sanctions_matcher.py"

# Copy the entrypoint script to zip root
cp "$SCRIPT_DIR/main.py" "$BUILD_DIR/main.py"

# 3. Wallet material — deliberately NOT packaged
#
# config/wallets.json holds private XRPL wallet seeds. This step used to copy it
# into the archive, which put the seeds into an S3 deployment artifact readable
# by anyone with s3:GetObject on the bucket, kept them in every retained
# artifact version, and made a fresh clone fail the build outright (the file is
# git-ignored, so it is absent until provision_wallets.py runs).
#
# The runtime source of truth is the Secrets Manager secret
# xrpl-agentic-payments/wallets — the same secret the Gateway Lambdas already
# read (functions/shared.py). infra/lib/infra-stack.ts grants the AgentCore
# runtime role read access to it, and WALLETS_SECRET_ID below names it.
#
# ⚠️  ROTATE: any seed that was ever included in a previously built archive (or
# in a container image built before this change) must be treated as disclosed.
# Rotate those wallets — re-run scripts/provision_wallets.py and update the
# secret — and drain the old ones. Deleting the artifact is not sufficient.
echo ""
echo "--- Wallet material: NOT bundled (read from Secrets Manager at runtime) ---"
echo "    Secret: xrpl-agentic-payments/wallets"

# 4. Copy data directory (OFAC file if present)
if [ -d "$PROJECT_ROOT/data" ]; then
    echo "--- Copying data directory ---"
    cp -r "$PROJECT_ROOT/data" "$BUILD_DIR/data"
fi

# 5. Remove __pycache__ directories (not compatible across architectures)
echo ""
echo "--- Cleaning __pycache__ ---"
find "$BUILD_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "*.pyc" -delete 2>/dev/null || true

# 6. Remove unnecessary files to reduce size (keep .dist-info for importlib.metadata!)
echo "--- Removing unnecessary files ---"
find "$BUILD_DIR" -name "tests" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "test" -type d -exec rm -rf {} + 2>/dev/null || true

# 7. Create zip
echo ""
echo "--- Creating zip archive ---"
cd "$BUILD_DIR"
rm -f "$OUTPUT"
zip -r "$OUTPUT" . -x "*.pyc" -x "__pycache__/*" > /dev/null

# 8. Refuse to ship secret material
#
# This gate is on the ARCHIVE, not on the copy steps above, so it also catches
# anything a future step (or a stray file left in build/) drops into the bundle.
echo ""
echo "--- Verifying no secret material in the archive ---"
LEAKED=$(zipinfo -1 "$OUTPUT" | grep -E '(^|/)(wallets\.json|\.env(\..+)?|[^/]+\.secret)$' || true)
if [ -n "$LEAKED" ]; then
    echo "ERROR: refusing to ship an archive containing secret material:" >&2
    echo "$LEAKED" >&2
    echo "Wallet seeds must come from Secrets Manager at runtime, not from the zip." >&2
    rm -f "$OUTPUT"
    exit 1
fi
echo "OK: no wallet seeds, .env files or *.secret entries"

# 9. Report
ZIP_SIZE=$(du -h "$OUTPUT" | cut -f1)
echo ""
echo "=== Package complete ==="
echo "Output: $OUTPUT"
echo "Size: $ZIP_SIZE"
echo ""
echo "Contents (top-level):"
zipinfo -1 "$OUTPUT" | grep -v "/" | head -20
echo ""
echo "Source files:"
zipinfo -1 "$OUTPUT" | grep "^src/" | head -20
echo ""
echo "Runtime configuration this artifact expects (nothing is bundled):"
echo "  WALLETS_SECRET_ID=xrpl-agentic-payments/wallets   (Secrets Manager)"

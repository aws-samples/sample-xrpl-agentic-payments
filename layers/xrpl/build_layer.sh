#!/bin/bash

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Build Lambda Layer for xrpl-py + shared deps
# Target: ARM64 (Graviton2), Python 3.14
#
# Lambda Layer structure:
#   layer.zip/
#     python/
#       xrpl/
#       httpx/            (transitive: xrpl-py's HTTP client)
#       nest_asyncio.py
#       ... (all deps)
#
# The zip is consumed directly as a CDK asset by infra/lib/tools-stack.ts
# (lambda.Code.fromAsset), so `cdk deploy` uploads and versions it — there is no
# manual `aws s3 cp` step to forget. That also means the file NAME below is a
# contract: it must stay equal to XRPL_LAYER_ZIP_FILENAME in tools-stack.ts, and
# infra/test/infra.test.ts fails if the two ever diverge.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/build"
OUTPUT="$SCRIPT_DIR/xrpl-layer-v1.zip"

echo "=== Building Lambda Layer: xrpl-py ==="
echo "Target: aarch64-unknown-linux-gnu (ARM64 Graviton2)"
echo "Python: 3.14"

# Clean
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/python"

# Install deps into python/ directory (Lambda layer convention)
echo ""
echo "--- Installing dependencies ---"
uv pip install \
    --python-platform aarch64-unknown-linux-gnu \
    --python-version 3.14 \
    --target "$BUILD_DIR/python" \
    -r "$SCRIPT_DIR/requirements.txt" 2>&1 | tail -20

# Remove __pycache__ (not compatible across architectures)
echo ""
echo "--- Cleaning ---"
find "$BUILD_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "*.pyc" -delete 2>/dev/null || true

# Remove test directories to save space
find "$BUILD_DIR" -name "tests" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "test" -type d -exec rm -rf {} + 2>/dev/null || true

# Create zip
echo ""
echo "--- Creating layer zip ---"
cd "$BUILD_DIR"
rm -f "$OUTPUT"
zip -r "$OUTPUT" python/ -x "*.pyc" > /dev/null

# Report
ZIP_SIZE=$(du -h "$OUTPUT" | cut -f1)
echo ""
echo "=== Layer build complete ==="
echo "Output: $OUTPUT"
echo "Size: $ZIP_SIZE"
echo "Consumed as a CDK asset — deploy with: cd infra && npx cdk deploy XrplAgenticPaymentsToolsStack"
echo ""
echo "Key packages:"
ls "$BUILD_DIR/python/" | grep -E "^(xrpl|httpx|nest_asyncio|cffi|cryptography)" || true
echo ""
echo "Architecture check (native .so files):"
find "$BUILD_DIR/python" -name "*.so" | head -3 | while read f; do
    echo "  $(basename $f)"
done

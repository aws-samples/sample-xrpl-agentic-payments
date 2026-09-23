#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
layer_root="${repository_root}/dist/layer"

mkdir -p "${layer_root}/python"
find "${layer_root}/python" -mindepth 1 ! -name .asset-stub -delete

uv pip install \
  --python-platform aarch64-manylinux2014 \
  --python-version 3.13 \
  --target "${layer_root}/python" \
  --requirements "${repository_root}/requirements-lambda.txt"

echo "Built ARM64 Python 3.13 Lambda layer at ${layer_root}"

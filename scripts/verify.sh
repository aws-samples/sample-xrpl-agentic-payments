#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repository_root}"

uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
npm run lint
npm run test
npm run build
npm audit --omit=dev
npm run synth --workspace infra -- --quiet

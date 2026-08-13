#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Tempest daily capture $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

PYTHONPATH=src python3 scripts/screen_market.py --fetch-bars
PYTHONPATH=src python3 scripts/run_backtest.py

echo "=== commit results ==="
bash scripts/commit_evidence.sh "daily screen + backtest results"

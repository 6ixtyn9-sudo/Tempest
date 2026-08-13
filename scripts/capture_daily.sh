#!/usr/bin/env bash
# Tempest daily capture loop for GitHub Actions.
# Screens the market (TradingView), logs qualifiers, fetches their 1m bars,
# runs the same-day backtest over the accumulated warehouse, and commits the
# results. Idempotent: safe to run on any cadence (screen is cached 15 min).
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Tempest daily capture $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

PYTHONPATH=src python3 scripts/screen_market.py --fetch-bars
PYTHONPATH=src python3 scripts/run_backtest.py

echo "=== commit results ==="
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
for evidence in localdata/screen_log.csv localdata/trade_journal.csv; do
    [ -f "$evidence" ] && git add -f "$evidence"
done
if git diff --cached --quiet; then
    echo "No Tempest data changes to commit."
else
    git commit -m "chore(capture): daily screen + backtest results [skip ci]"
    for attempt in 1 2 3; do
        git pull --rebase --autostash origin main && git push && break
        if [ "$attempt" = 3 ]; then
            echo "::error::Tempest push failed after 3 attempts"
            exit 1
        fi
        sleep $((attempt * 5))
    done
fi

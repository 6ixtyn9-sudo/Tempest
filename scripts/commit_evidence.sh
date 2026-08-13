#!/usr/bin/env bash
# Commit the accumulated evidence CSVs back to the repo.
#
# Called TWICE per run, and that is deliberate: once after the screen/backtest
# (which writes screen_log.csv) and again after the paper trade (which writes
# trade_journal.csv). Committing only after the capture would push the screen
# log while the journal was still unwritten, so every paper trade would be
# lost the moment the Actions cache expired.
#
# Idempotent: with nothing staged it prints and exits 0.
set -euo pipefail

cd "$(dirname "$0")/.."

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

for evidence in localdata/screen_log.csv localdata/trade_journal.csv; do
    [ -f "$evidence" ] && git add -f "$evidence"
done

if git diff --cached --quiet; then
    echo "No Tempest data changes to commit."
    exit 0
fi

git commit -m "chore(capture): ${1:-daily screen + backtest results} [skip ci]"
for attempt in 1 2 3; do
    git pull --rebase --autostash origin main && git push && break
    if [ "$attempt" = 3 ]; then
        echo "::error::Tempest push failed after 3 attempts"
        exit 1
    fi
    sleep $((attempt * 5))
done

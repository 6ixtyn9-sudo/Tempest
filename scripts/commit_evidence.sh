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

# Keep paper_status bounded: header + last 250 rows. A 5-minute poll
# otherwise grows the file without bound and the git history with it.
if [ -f localdata/paper_status.csv ]; then
    python3 - <<'PY'
from pathlib import Path
p = Path("localdata/paper_status.csv")
lines = p.read_text().splitlines()
if lines:
    head, body = lines[0], lines[1:]
    p.write_text("\n".join([head, *body[-250:]]) + "\n")
PY
fi

evidence_files=(
    localdata/screen_log.csv
    localdata/trade_journal.csv
    localdata/paper_status.csv
    localdata/gale_screen_log.csv
    localdata/gale_shadow_signals.csv
    localdata/gale_status.csv
)
for report in localdata/backtest_report_*.json localdata/gale_backtest_report_*.json; do
    [ -f "$report" ] && evidence_files+=("$report")
done
for evidence in "${evidence_files[@]}"; do
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

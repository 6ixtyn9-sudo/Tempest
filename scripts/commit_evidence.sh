#!/usr/bin/env bash
# Persist bounded operational records and dated reports on main.
# Idempotent: with nothing staged it prints and exits 0.
#
# Concurrency contract
# --------------------
# Two workflow runs (daily_capture + paper_poll) can be dispatched at the
# same minute and both append rows to the same localdata CSVs. Handling:
#
#   1. .gitattributes marks localdata/*.csv as `merge=union`, so a rebase
#      keeps BOTH sides' appended rows instead of raising a conflict.
#   2. normalise_csv() then de-duplicates and re-sorts by timestamp, so a
#      union merge cannot leave interleaved or repeated rows.
#   3. Any rebase that still fails is aborted before retrying, so the tree
#      is never left with unmerged files (the old code retried into
#      "Pulling is not possible because you have unmerged files" and burned
#      all 3 attempts in 15s).
set -euo pipefail

cd "$(dirname "$0")/.."

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

# NOTE: `union` is a git BUILT-IN merge driver. Do NOT define merge.union.*
# in git config -- that reclassifies it as a custom driver and git then fails
# with "custom merge driver union lacks command line".

EVIDENCE_CSVS=(
    localdata/screen_log.csv
    localdata/trade_journal.csv
    localdata/paper_status.csv
    localdata/gale_screen_log.csv
    localdata/gale_shadow_signals.csv
    localdata/gale_status.csv
)

# De-duplicate, sort by leading ISO timestamp, and bound the row count.
# Idempotent and safe to run on an already-clean file.
normalise_csv() {
    local path="$1" keep="$2"
    [ -f "$path" ] || return 0
    KEEP="$keep" python3 - "$path" <<'PY'
import os, sys
from pathlib import Path

path = Path(sys.argv[1])
keep = int(os.environ["KEEP"])
lines = path.read_text().splitlines()
if not lines:
    sys.exit(0)

head, body = lines[0], [ln for ln in lines[1:] if ln.strip()]

# Drop duplicates a union merge may have produced, preserving first sighting.
seen, unique = set(), []
for line in body:
    if line not in seen:
        seen.add(line)
        unique.append(line)

# Stable sort on the leading ISO-8601 timestamp so interleaved appends from
# two concurrent runs end up in true chronological order. Rows without a
# parseable timestamp keep their relative position at the end.
def key(item):
    idx, line = item
    stamp = line.split(",", 1)[0]
    return (0, stamp, idx) if stamp[:4].isdigit() else (1, "", idx)

ordered = [ln for _, ln in sorted(enumerate(unique), key=key)]
path.write_text("\n".join([head, *ordered[-keep:]]) + "\n")
PY
}

# paper_status/gale_status are high-frequency: a 5-minute poll appends ~8
# rows per pass, so bound them. Journals and screen logs are low-volume and
# are the actual trade record -- keep those long.
for csv in localdata/paper_status.csv localdata/gale_status.csv; do
    normalise_csv "$csv" 250
done
for csv in localdata/screen_log.csv localdata/trade_journal.csv \
           localdata/gale_screen_log.csv localdata/gale_shadow_signals.csv; do
    normalise_csv "$csv" 5000
done

evidence_files=("${EVIDENCE_CSVS[@]}")
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

for attempt in 1 2 3 4 5; do
    if git pull --rebase --autostash origin main; then
        # Re-normalise: a union merge just concatenated both sides' rows.
        for csv in localdata/paper_status.csv localdata/gale_status.csv; do
            normalise_csv "$csv" 250
        done
        for csv in localdata/screen_log.csv localdata/trade_journal.csv \
                   localdata/gale_screen_log.csv localdata/gale_shadow_signals.csv; do
            normalise_csv "$csv" 5000
        done
        if ! git diff --quiet; then
            for evidence in "${evidence_files[@]}"; do
                [ -f "$evidence" ] && git add -f "$evidence"
            done
            git commit --amend --no-edit
        fi

        if git push; then
            echo "Evidence pushed on attempt ${attempt}."
            exit 0
        fi
        echo "Push rejected on attempt ${attempt} (concurrent run won the race)."
    else
        # CRITICAL: never leave an in-progress rebase behind. The previous
        # version retried straight into "unmerged files" and failed the run.
        echo "Rebase failed on attempt ${attempt} - aborting cleanly and retrying."
        git rebase --abort 2>/dev/null || true
    fi

    if [ "$attempt" = 5 ]; then
        echo "::error::Tempest push failed after 5 attempts"
        exit 1
    fi
    # Jittered backoff so two racing runs do not retry in lockstep.
    sleep $(( attempt * 5 + RANDOM % 5 ))
done

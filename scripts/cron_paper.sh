#!/usr/bin/env bash
# Local 5-minute paper-trade poll. Runs from cron on a machine that is awake
# during US market hours. Unlike GitHub's scheduler this fires on time.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

mkdir -p localdata/logs
LOG="localdata/logs/cron_$(date -u +%Y%m%d).log"
exec >>"$LOG" 2>&1

echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) poll start ---"

ET_HHMM=$(TZ=America/New_York date +%H%M)
ET_DOW=$(TZ=America/New_York date +%u)
if [ "$ET_DOW" -gt 5 ]; then
  echo "weekend in ET - skip"; exit 0
fi
if [ "$((10#$ET_HHMM))" -lt 930 ] || [ "$((10#$ET_HHMM))" -gt 1600 ]; then
  echo "outside 09:30-16:00 ET (now $ET_HHMM) - skip"; exit 0
fi

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi
export TEMPEST_PAPER=1

PY=$(command -v python3)
PYTHONPATH=src "$PY" scripts/paper_trade.py
code=$?
echo "paper_trade exit=$code"

if [ "$code" -ne 0 ]; then
  echo "NON-ZERO EXIT - see localdata/paper_status.csv"
fi

if [ "${TEMPEST_CRON_PUSH:-0}" = "1" ]; then
  bash scripts/commit_evidence.sh "local cron paper poll" || echo "commit_evidence failed"
fi

echo "--- poll end ---"
exit 0

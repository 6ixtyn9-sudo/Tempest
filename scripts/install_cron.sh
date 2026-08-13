#!/usr/bin/env bash
# Install (or refresh) the local 5-minute paper-trade cron entry.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARK="# tempest-paper-poll"
LINE="*/5 * * * * /bin/bash ${REPO}/scripts/cron_paper.sh ${MARK}"

current=$(crontab -l 2>/dev/null || true)
cleaned=$(printf '%s\n' "$current" | grep -v "$MARK" || true)
printf '%s\n%s\n' "$cleaned" "$LINE" | grep -v '^$' | crontab -

echo "installed:"
crontab -l | grep "$MARK"
echo
echo "logs: ${REPO}/localdata/logs/"

#!/usr/bin/env bash
# Weekly journal review using the Claude Code harness. Reads trades.jsonl,
# writes agent/lessons.md. No API key needed.
#
# Usage:   ./agent/run_journal_review.sh [days]
# Cron (Sunday 20:00 UTC):
#   0 20 * * 0  cd <repo> && ./agent/run_journal_review.sh 7 >> agent/review.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

DAYS="${1:-7}"
PROMPT="$(sed -e "s|{{DAYS}}|${DAYS}|g" agent/journal_review_prompt.md)"

echo "[$(date -u +%FT%TZ)] reviewing last ${DAYS} days"
claude -p "${PROMPT}" \
  --allowed-tools Read Write \
  --permission-mode acceptEdits \
  --output-format text
echo "[$(date -u +%FT%TZ)] done — see agent/lessons.md"
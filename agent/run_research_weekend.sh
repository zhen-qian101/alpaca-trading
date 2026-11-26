#!/usr/bin/env bash
# WEEKEND RECAP → flagship cartoon video. Runs Sunday night (US). Claude
# summarises the WEEK that was + what to watch next week into
# decisions/weekend_<date>.json (a COSMETIC file), then the offline renderer
# turns it into the dashboard's weekend clip via the cartoon story pipeline
# (`--renderer wan`, whose shots MiniMax H3 renders: config story.video_engine).
#
# OUT-OF-BAND from trading: writes ONLY the weekend recap file and renders video;
# never rotates, validates, or sets guardrails. A hard guard restores
# decisions/decisions.json if the agent ever touches it.
#
# Cron (Sunday 8pm ET = 10:00 AEST Monday):
#   0 10 * * 1  cd <repo> && ./agent/run_research_weekend.sh >> agent/recap.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

WATCHLIST="${1:-GOOG,NVDA,MSFT,AMZN,TSLA,AAPL,META,SOXL,TQQQ,ORCL}"
DATE="$(TZ=America/New_York date +%F)"        # Sunday (ET) when the cron fires
RECAP="decisions/weekend_${DATE}.json"
LIVE="decisions/decisions.json"

GUARD=""
if [[ -f "$LIVE" ]]; then
  GUARD="$(mktemp)"; cp "$LIVE" "$GUARD"
fi
restore_live() {
  if [[ -n "$GUARD" && -f "$GUARD" ]]; then
    if ! cmp -s "$GUARD" "$LIVE"; then
      echo "WARN: weekend recap run modified ${LIVE} — restoring it (recap must be cosmetic)"
      cp "$GUARD" "$LIVE"
    fi
    rm -f "$GUARD"
  fi
}
trap restore_live EXIT

PROMPT="$(sed -e "s|{{WATCHLIST}}|${WATCHLIST}|g" -e "s|{{DATE}}|${DATE}|g" agent/research_prompt_weekend.md)"

echo "[$(date -u +%FT%TZ)] weekend recap research for ${WATCHLIST} (week ending ${DATE})"
# Authoritative prices before the agent runs — same reason as the daily recap
# (agent/prices.py). Non-fatal.
.venv/bin/python -m agent.prices --symbols "${WATCHLIST}" --date "${DATE}" \
  || echo "WARN: price precompute failed — agent must source prices itself"

claude -p "${PROMPT}" \
  --allowed-tools WebSearch WebFetch Read Write \
    mcp__alpaca__get_stock_snapshot \
    mcp__alpaca__get_stock_bars \
    mcp__alpaca__get_news \
    mcp__alpaca__get_market_movers \
    mcp__alpaca__get_most_active_stocks \
  --permission-mode acceptEdits \
  --output-format text

restore_live; trap - EXIT

if [[ ! -f "$RECAP" ]]; then
  echo "ERROR: ${RECAP} not written — skipping video render"; exit 1
fi

echo "[$(date -u +%FT%TZ)] rendering weekend recap video from ${RECAP}"
# Publish to YouTube like the post-market recap; BRIEFING_PUBLISH=0 renders only.
BRIEFING_PUBLISH="${BRIEFING_PUBLISH:-1}" BRIEFING_PUBLISH_KIND=video \
  ./briefing/run_briefing.sh --decisions "$RECAP" --renderer wan --label "Weekend recap"
echo "[$(date -u +%FT%TZ)] done"

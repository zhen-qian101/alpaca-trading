#!/usr/bin/env bash
# POST-MARKET RECAP → the hosted daily video. Runs after the US close every
# trading day. Claude summarises WHAT HAPPENED TODAY into decisions/recap_<date>.json
# (a COSMETIC file), then the offline renderer turns it into the end-of-day recap:
# the presenter's hosted slideshow (see the HOST ERA note at the render below).
#
# OUT-OF-BAND from trading: this run writes ONLY the recap file and renders video.
# It does NOT rotate positions, validate decisions, or set future guardrails, and a
# hard guard below restores decisions/decisions.json if the agent ever touches it.
#
# Cron (8pm ET every trading day = 10:00 AEST the next morning, Tue–Sat):
#   0 10 * * 2-6  cd <repo> && ./agent/run_research_postmarket.sh >> agent/recap.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

WATCHLIST="${1:-GOOG,NVDA,MSFT,AMZN,TSLA,AAPL,META,SOXL,TQQQ,ORCL}"
# The US trading session that just closed (ET), not the AEST wall-clock date.
DATE="$(TZ=America/New_York date +%F)"
RECAP="decisions/recap_${DATE}.json"
LIVE="decisions/decisions.json"

# --- hard guard: the live trade file must survive this run byte-for-byte. ---
GUARD=""
if [[ -f "$LIVE" ]]; then
  GUARD="$(mktemp)"; cp "$LIVE" "$GUARD"
fi
restore_live() {
  if [[ -n "$GUARD" && -f "$GUARD" ]]; then
    if ! cmp -s "$GUARD" "$LIVE"; then
      echo "WARN: recap run modified ${LIVE} — restoring it (recap must be cosmetic)"
      cp "$GUARD" "$LIVE"
    fi
    rm -f "$GUARD"
  fi
}
trap restore_live EXIT

PROMPT="$(sed -e "s|{{WATCHLIST}}|${WATCHLIST}|g" -e "s|{{DATE}}|${DATE}|g" agent/research_prompt_postmarket.md)"

# Authoritative prices, computed BEFORE the agent runs (see agent/prices.py). The
# agent is not trusted to pick the right feed: on 2026-07-29 it took MSFT's
# after-hours move from IEX's last print (40 shares, 2.6% spread, 16:55 ET) and
# reported +2.9% when the overnight book said +7.5%. Non-fatal by design — without
# it the prompt falls back to telling the agent to gather prices itself.
echo "[$(date -u +%FT%TZ)] precomputing session + extended-hours prices"
.venv/bin/python -m agent.prices --symbols "${WATCHLIST}" --date "${DATE}" \
  || echo "WARN: price precompute failed — agent must source prices itself, and the extended-hours number is the one it gets wrong"

echo "[$(date -u +%FT%TZ)] post-market recap research for ${WATCHLIST} (session ${DATE})"
claude -p "${PROMPT}" \
  --allowed-tools WebSearch WebFetch Read Write \
    mcp__alpaca__get_stock_snapshot \
    mcp__alpaca__get_stock_bars \
    mcp__alpaca__get_news \
    mcp__alpaca__get_stock_latest_trade \
    mcp__alpaca__get_stock_latest_quote \
    mcp__alpaca__get_market_movers \
    mcp__alpaca__get_most_active_stocks \
  --permission-mode acceptEdits \
  --output-format text

restore_live; trap - EXIT   # trade file is safe; render below is pure video

if [[ ! -f "$RECAP" ]]; then
  echo "ERROR: ${RECAP} not written — skipping video render"; exit 1
fi

echo "[$(date -u +%FT%TZ)] rendering post-market recap video from ${RECAP}"
# Publish the finished recap to YouTube (English audio only, poster as thumbnail).
# Set BRIEFING_PUBLISH=0 to render without uploading. The upload is guarded by youtube.preflight_qc — the
# anatomy reviewer samples the assembled video and REFUSES the upload on a limb
# defect, because unlike the render a public post is not cheaply redone.
# HOST ERA (since 2026-08-10): the daily video is the configured presenter's
# hosted slideshow — 陈谷雨 since 2026-08-12 (briefing/config.json host.host_json)
# — with the presenter's narration (EN+中文), the live dashboard, and one day
# keyframe for every slide. It replaced the cartoon recap and the intraday
# slideshows. BRIEFING_HOST forces factual mode inside generate.py; the weekend
# recap keeps the cartoon path in run_research_weekend.sh. BRIEFING_HOST=0 here
# falls back to the H3 cartoon recap.
BRIEFING_HOST="${BRIEFING_HOST:-1}" \
BRIEFING_PUBLISH="${BRIEFING_PUBLISH:-1}" BRIEFING_PUBLISH_KIND=video \
  ./briefing/run_briefing.sh --decisions "$RECAP" --label "Post-market recap"
echo "[$(date -u +%FT%TZ)] done"

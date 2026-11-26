#!/usr/bin/env bash
# Run the daily research/decision step using the Claude Code HARNESS (no API key
# needed — uses your `claude` CLI auth). Claude web-searches the watchlist and
# writes decisions/decisions.json; then we clamp it to hard ceilings.
#
# Usage:
#   ./agent/run_research.sh GOOG,NVDA,MSFT,AMZN,TSLA,AAPL,META,SOXL,TQQQ
#   ./agent/run_research.sh            # default 9-symbol watchlist
#
# Cron (daily, ~15 min before US market open = 09:30 ET):
#   14 13 * * 1-5  cd <repo> && ./agent/run_research.sh >> agent/research.log 2>&1
#   (13:14 UTC == 09:14 ET during EDT)
set -euo pipefail
cd "$(dirname "$0")/.."

WATCHLIST="${1:-GOOG,NVDA,MSFT,AMZN,TSLA,AAPL,META,SOXL,TQQQ,ORCL}"
DATE="$(date -u +%F)"
# The price file is keyed by US session date in ET, which is not the UTC date above.
PRICE_DATE="$(TZ=America/New_York date +%F)"
PRICE_FILE="decisions/_prices_${PRICE_DATE}.json"

PROMPT="$(sed -e "s|{{WATCHLIST}}|${WATCHLIST}|g" -e "s|{{DATE}}|${DATE}|g" \
              -e "s|{{PRICE_FILE}}|${PRICE_FILE}|g" agent/research_prompt.md)"

# Precompute the live trigger state + current positions BEFORE handing off. The
# agent has no Bash (see --allowed-tools below) so it cannot compute indicators
# itself, and the Alpaca MCP has no positions tool at all — without this it ranks
# "which fires next" by eyeballing charts against textbook indicator defaults this
# book does not trade. See agent/live_state.py. Non-fatal: a stale/missing file is
# better than no research run, and the prompt tells the agent to say so.
.venv/bin/python -m agent.live_state || echo "WARN: live_state precompute failed — agent will be flying blind"

# Authoritative session + extended-hours prices, for the same reason live_state
# exists: the agent should not be left to pick a feed. See agent/prices.py — the
# default IEX feed reports a few shares inside a huge spread as a price once the
# regular session is over. Non-fatal.
.venv/bin/python -m agent.prices --symbols "${WATCHLIST}" --date "${PRICE_DATE}" \
  || echo "WARN: price precompute failed — agent must source prices itself; keep extended-hours numbers qualitative"

echo "[$(date -u +%FT%TZ)] running research for ${WATCHLIST}"
# Headless Claude Code: allow research + write tools; acceptEdits auto-approves
# the Write of decisions/decisions.json within the project. The Alpaca MCP tools
# (registered at user scope) are listed so Claude can pull structured market
# data; if the MCP isn't loaded, Claude falls back to WebSearch.
#
# Bash is deliberately NOT allowed: this agent runs unattended on cron and sets
# the priorities that govern rotation, i.e. what the account holds — arbitrary
# shell is too large a blast radius for that. agent/live_state.py exists so it
# does not need shell to get the numbers.
DECISION_FILE="decisions/decisions.json"
BEFORE_MTIME="$(stat -c %Y "$DECISION_FILE" 2>/dev/null || echo 0)"

# `|| true` via AGENT_STATUS is load-bearing: `set -e` would otherwise abort the
# script the instant the agent errors, skipping the post-condition check below —
# which is exactly the silent failure it exists to catch.
AGENT_STATUS=0
claude -p "${PROMPT}" \
  --allowed-tools WebSearch WebFetch Read Write \
    mcp__alpaca__get_stock_snapshot \
    mcp__alpaca__get_stock_bars \
    mcp__alpaca__get_crypto_snapshot \
    mcp__alpaca__get_crypto_bars \
    mcp__alpaca__get_news \
    mcp__alpaca__get_market_movers \
    mcp__alpaca__get_most_active_stocks \
  --permission-mode acceptEdits \
  --output-format text || AGENT_STATUS=$?

# --- post-condition: did the agent actually refresh the live decision file? ---
# This run sets the day's bias/priority/contingencies, and relay/risk_filter.py
# reads them on every signal to decide whether a live buy is allowed. Before
# 2026-08-19 a failed `claude -p` just aborted the script, leaving decisions.json
# holding YESTERDAY's permissions with nothing to say so.
AFTER_MTIME="$(stat -c %Y "$DECISION_FILE" 2>/dev/null || echo 0)"
if [ "$AFTER_MTIME" = "$BEFORE_MTIME" ]; then
  echo "WARN: ${DECISION_FILE} was not rewritten (agent exited ${AGENT_STATUS}) — retrying once"
  claude -p "${PROMPT}" \
    --allowed-tools WebSearch WebFetch Read Write \
    --permission-mode acceptEdits \
    --output-format text || true
  AFTER_MTIME="$(stat -c %Y "$DECISION_FILE" 2>/dev/null || echo 0)"
fi
if [ "$AFTER_MTIME" = "$BEFORE_MTIME" ]; then
  echo "ERROR: ${DECISION_FILE} still not refreshed after a retry — the book is"
  echo "       running on STALE daily research. Journaling and exiting non-zero."
  .venv/bin/python - <<'PYEOF' || true
from relay.config import JOURNAL_FILE
from relay.logger import append_event
append_event(JOURNAL_FILE, {
    "event": "research_stale",
    "reason": "daily research did not rewrite decisions.json; bias/priority/"
              "contingencies are from a previous run and the risk gate is "
              "enforcing them",
})
PYEOF
  exit 1
fi

# Clamp any over-limit values the model may have emitted.
.venv/bin/python -m agent.validate_decisions --watchlist "${WATCHLIST}"

# Rotate: close any held symbol that's no longer today's single pick. The new
# pick only enters on its own EMA cross-up (a TradingView alert), not here.
.venv/bin/python -m agent.rotate || true
echo "[$(date -u +%FT%TZ)] done"
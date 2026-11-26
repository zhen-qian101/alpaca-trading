#!/usr/bin/env bash
# Lightweight INTRADAY refresh of decisions.json using the Claude Code harness.
# Re-picks the single best long opportunity from the existing watchlist (does
# NOT mine/prune the list — that's the daily job). Pair with a 5-min strategy so
# the bias stays fresh intraday.
#
# Usage:
#   ./agent/run_research_intraday.sh                    # uses current watchlist
#   ./agent/run_research_intraday.sh GOOG,NVDA,MSFT     # explicit list
#
# Cron (every 1 hour during the US session, 09:30-16:00 ET -> 14:30-21:00 UTC EDT):
#   30 14-20 * * 1-5  cd <repo> && ./agent/run_research_intraday.sh >> agent/research_intraday.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

# Default to the watchlist currently in decisions.json; fall back to the core 9.
if [ -z "${1:-}" ]; then
  WL="$(.venv/bin/python -c "import json;d=json.load(open('decisions/decisions.json'));print(','.join(d.get('watchlist',[])))" 2>/dev/null || true)"
  WATCHLIST="${WL:-GOOG,NVDA,MSFT,AMZN,TSLA,AAPL,META,SOXL,TQQQ}"
else
  WATCHLIST="$1"
fi
DATE="$(date -u +%FT%TZ)"
# Which session is this run setting the bias FOR? The engine fires on any 1h bar,
# including pre-market, after-hours and the overnight book, so a run that lands
# outside 09:30-16:00 ET is writing permissions for hours nobody will revisit —
# see the prompt's session section. 10# forces base 10: "0930" is not valid octal.
ET_HM="$((10#$(TZ=America/New_York date +%H%M)))"
if [ "$ET_HM" -ge 930 ] && [ "$ET_HM" -lt 1600 ]; then
  SESSION="REGULAR SESSION (09:30-16:00 ET) — another refresh follows within ~3h"
else
  SESSION="EXTENDED HOURS (pre-market / after-hours / overnight) — the next scheduled refresh is hours away"
fi
# {{DATE}} above is a UTC *timestamp* for the prompt header, so the price file needs
# its own ET session date — substituted as a full path so the agent never has to
# work out which date file to open.
PRICE_DATE="$(TZ=America/New_York date +%F)"
PRICE_FILE="decisions/_prices_${PRICE_DATE}.json"

PROMPT="$(sed -e "s|{{WATCHLIST}}|${WATCHLIST}|g" -e "s|{{DATE}}|${DATE}|g" \
              -e "s|{{PRICE_FILE}}|${PRICE_FILE}|g" -e "s|{{SESSION}}|${SESSION}|g" \
              agent/research_prompt_intraday.md)"

# Precompute the live trigger state + current positions BEFORE handing off. The
# agent has no Bash (see --allowed-tools below) so it cannot compute indicators
# itself, and the Alpaca MCP has no positions tool at all — without this it ranks
# "which fires next" by eyeballing charts against textbook indicator defaults this
# book does not trade. See agent/live_state.py. Non-fatal: a stale/missing file is
# better than no research run, and the prompt tells the agent to say so.
.venv/bin/python -m agent.live_state || echo "WARN: live_state precompute failed — agent will be flying blind"

# Authoritative session + extended-hours prices (see agent/prices.py). This matters
# more here than in the recap: the 18:00 ET run fires OUTSIDE regular hours, which
# is exactly when the default IEX feed the prompt
# used to read prices from is a few shares inside a huge spread — and these numbers
# set the contingency LEVELS that reach the live trade path, not just a video.
# Non-fatal: the prompt tells the agent to stay qualitative if the file is absent.
.venv/bin/python -m agent.prices --symbols "${WATCHLIST}" --date "${PRICE_DATE}" \
  || echo "WARN: price precompute failed — agent must source prices itself; extended-hours numbers are the ones it gets wrong"

echo "[$(date -u +%FT%TZ)] intraday refresh for ${WATCHLIST}"
DECISION_FILE="decisions/decisions.json"
BEFORE_MTIME="$(stat -c %Y "$DECISION_FILE" 2>/dev/null || echo 0)"

# `|| true` is load-bearing: `set -e` would otherwise abort the script the
# instant the agent errors, skipping the post-condition check below — which is
# exactly the silent failure this is here to catch. The status is kept for the
# log; the check that actually matters is whether the file moved.
AGENT_STATUS=0
claude -p "${PROMPT}" \
  --allowed-tools WebSearch WebFetch Read Write \
    mcp__alpaca__get_stock_snapshot \
    mcp__alpaca__get_stock_bars \
    mcp__alpaca__get_crypto_snapshot \
    mcp__alpaca__get_crypto_bars \
    mcp__alpaca__get_news \
  --permission-mode acceptEdits \
  --output-format text || AGENT_STATUS=$?
[ "$AGENT_STATUS" -eq 0 ] || echo "WARN: agent exited ${AGENT_STATUS}"

# --- post-condition: did the agent actually refresh the live decision file? ---
# Until 2026-08-19 this script ran `claude -p` and moved on. When that call
# failed — an API error, a timeout, the DNS outage that killed the 20:00 ET run
# on 2026-08-18 — `set -e` aborted BEFORE validate_decisions and rotate, and
# nothing recorded it anywhere a human or the dashboard would look.
#
# This run is NOT cosmetic: it writes `bias`, `priority` and `contingencies`,
# and relay/risk_filter.py reads them on every signal to decide whether a live
# buy is allowed. A silent failure leaves the book trading on the PREVIOUS run's
# permissions — up to 5.5h between intraday slots — while looking healthy. The
# log already shows it happening (more "refresh for" starts than "done"s).
AFTER_MTIME="$(stat -c %Y "$DECISION_FILE" 2>/dev/null || echo 0)"
if [ "$AFTER_MTIME" = "$BEFORE_MTIME" ]; then
  echo "WARN: ${DECISION_FILE} was not rewritten — retrying the agent once"
  claude -p "${PROMPT}" \
    --allowed-tools WebSearch WebFetch Read Write \
    --permission-mode acceptEdits \
    --output-format text || true
  AFTER_MTIME="$(stat -c %Y "$DECISION_FILE" 2>/dev/null || echo 0)"
fi
if [ "$AFTER_MTIME" = "$BEFORE_MTIME" ]; then
  echo "ERROR: ${DECISION_FILE} still not refreshed after a retry — the book is "
  echo "       running on STALE intraday refresh. Journaling and exiting non-zero."
  .venv/bin/python - <<'PYEOF' || true
from relay.config import JOURNAL_FILE
from relay.logger import append_event
append_event(JOURNAL_FILE, {
    "event": "research_stale",
    "reason": "intraday refresh did not rewrite decisions.json; bias/priority/contingencies "
              "are from a previous run and the risk gate is enforcing them",
})
PYEOF
  exit 1
fi

# Clamp any over-limit values.
.venv/bin/python -m agent.validate_decisions --watchlist "${WATCHLIST}"

# Rotate: close any held symbol that's no longer today's single pick. The new
# pick only enters on its own EMA cross-up (a TradingView alert), not here.
.venv/bin/python -m agent.rotate || true
echo "[$(date -u +%FT%TZ)] done"
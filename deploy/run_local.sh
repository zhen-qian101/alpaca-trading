#!/usr/bin/env bash
# Run the signal engine in a detached tmux session so it survives this Claude
# Code session. Re-runnable; kills stale instances.
#
# The relay is now a pure in-process library (no FastAPI/uvicorn, no cloudflared
# tunnel — TradingView and the HTTP webhook were removed). The signal engine
# generates every trade signal and routes it through relay.order_manager.handle()
# in-process. IB Gateway (IBC + Watchdog) is started separately by
# deploy/run_ibgateway.sh (Part 2) before this if you want IBKR as the data source;
# without it the engine falls back to yfinance.
#
#   ./deploy/run_local.sh             # session name "alpaca"
#   ./deploy/run_local.sh mysession
#
# Attach:    tmux attach -t alpaca        (detach: Ctrl-b d)
# Stop:      tmux kill-session -t alpaca
#            cron (deploy/ensure_up.sh) starts it again within 5 min unless held:
#            mkdir -p ~/.config/alpaca/hold && touch ~/.config/alpaca/hold/engine
set -euo pipefail
cd "$(dirname "$0")/.."
SESSION="${1:-alpaca}"
# .env is read INSIDE the tmux command, never exported from this shell. When no
# tmux server is running yet (after a reboot cron's deploy/ensure_up.sh is the
# first caller), the server copies THIS shell's environment into its global
# environment and hands that copy to every pane it starts from then on. The keys
# of the day the server was born would then outlive any later .env edit, because
# relay.config's load_dotenv() never overrides a variable that is already set.
DATADIR="$(set -a; [ -f .env ] && . ./.env; echo "${ALPACA_DATA_DIR:-/mnt/sandisk/alpaca}")"
mkdir -p "$DATADIR/logs"

# "=": exact name. A bare -t also matches by prefix, so with no "alpaca" session
# it would kill some other session whose name merely starts with it.
tmux kill-session -t "=$SESSION" 2>/dev/null || true
# Anchored, and matching the real argv rather than a loose substring. `pkill -f`
# tests the WHOLE command line, so the old pattern also killed anything that
# merely MENTIONED the engine — a grep, a tail, an editor, a monitoring loop
# (one was killed by it on 2026-08-19). Anchoring to the actual interpreter path
# means only the engine itself matches.
pkill -f "^\.venv/bin/python -m sigengine\.engine" 2>/dev/null || true
sleep 1

# Window 1: the signal engine (the system's signal source; TV was removed).
# Polls each enabled symbol in sigengine/config.json, runs the rolling EMA-cross
# backtest loop, and routes fresh signals through the relay's OrderManager
# in-process (risk gate + signal-driven rotation + journal).
#
# Wrapped in a SUPERVISOR loop, not run bare. The engine is the only thing that
# enforces per-position stops and emits exit/rotation signals, so while it is
# down the account is unmanaged — yet nothing used to notice it had stopped. On
# 2026-08-19 a SystemExit from the yfinance fallback escaped the poll loop (see
# sigengine.engine.DataUnavailable) and the process exited at 09:58; the tmux
# window closed with it and the engine stayed dead for 94 minutes, over an
# extended-hours session, until a human happened to look. That specific bug is
# fixed, but "the engine can exit and no one finds out" is the class of failure
# worth removing, not just the one instance.
#
# RESTART_DELAY is a small backoff so a crash-on-startup (bad config, missing
# .env) becomes a slow, greppable "[supervisor] engine exited" drumbeat in the
# log rather than a spin that fills the disk. Ctrl-C / tmux kill-session still
# stops it: bash traps SIGINT and SIGTERM here and leaves the loop.
RESTART_DELAY="${ENGINE_RESTART_DELAY:-10}"
tmux new-session -d -s "$SESSION" -n engine -c "$PWD" \
  "trap 'echo \"[supervisor] stopping on signal\"; exit 0' INT TERM; \
   while true; do \
     set -a; [ -f .env ] && . ./.env; set +a; \
     PYTHONPATH=. .venv/bin/python -m sigengine.engine --log-level INFO; \
     status=\$?; \
     echo \"[supervisor] \$(date -u +%FT%TZ) engine exited (status \$status) — restarting in ${RESTART_DELAY}s\"; \
     sleep ${RESTART_DELAY}; \
   done 2>&1 | tee -a $DATADIR/logs/sigengine.log"

echo "tmux session: $SESSION  (window: engine)"
echo "engine logs:  $DATADIR/logs/sigengine.log"
echo "data dir:      $DATADIR  (bar cache, journal, state)"
echo "attach:       tmux attach -t $SESSION   (detach: Ctrl-b d)"
echo "stop:         tmux kill-session -t $SESSION   (the supervisor restarts the engine on any other exit)"
echo "keep down:    mkdir -p ~/.config/alpaca/hold && touch ~/.config/alpaca/hold/engine   (else cron restarts it within 5 min)"
echo "dry-run:      PYTHONPATH=. .venv/bin/python -m sigengine.engine --once --dry-run"
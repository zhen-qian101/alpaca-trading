#!/usr/bin/env bash
# Launch the read-only web dashboard (web/server.py) in a detached tmux session.
#
# The dashboard is a VIEWER over the trading path: it reads the Alpaca account,
# decisions.json, the journal, the engine state file and the bar cache. It never
# imports relay.order_manager and no route reaches the broker — it cannot place,
# size or cancel an order (locked by tests/test_web_server.py).
#
# It does have ONE write route as of 2026-08-20: POST /api/look/approve signs
# off the presenter's daily outfit before the 20:00 ET render bakes it into
# video (/look.html). Its whole reach is one boolean in one cosmetic JSON file
# under briefing/assets; PUT/DELETE/PATCH remain absent and POST 404s elsewhere.
#
# RESTART AFTER MERGING web/server.py. Static files are read from disk per
# request, so a new page appears immediately — but the Python module is already
# imported into this process, so its new routes are NOT live until the session
# is relaunched. The failure looks like a page that loads and then 404s its own
# API, which is exactly what happened on 2026-08-20.
#
#   ./deploy/run_dashboard.sh                  # localhost:8787, no token needed
#   ./deploy/run_dashboard.sh --lan            # bind 0.0.0.0 for other machines
#   ./deploy/run_dashboard.sh --lan --port 9000
#
# Attach:  tmux attach -t dashboard   (detach: Ctrl-b d)
# Stop:    tmux kill-session -t dashboard
#          cron (deploy/ensure_up.sh) starts it again within 5 min unless held:
#          mkdir -p ~/.config/alpaca/hold && touch ~/.config/alpaca/hold/dashboard
#
# --- reaching it from another machine ---------------------------------------
# This box has a PRIVATE address (192.168.x) and no reverse proxy, so there is
# no public URL to hang it on out of the box. Pick one:
#
#  1. SSH tunnel (recommended — nothing is exposed, works from anywhere you can
#     ssh, and the dashboard stays bound to loopback). On your laptop:
#         ssh -N -L 8787:127.0.0.1:8787 <user>@<this-box>
#     then open http://localhost:8787 there. Run this script with NO flags.
#
#  2. Same LAN: run with --lan and open http://<lan-ip>:8787/?token=<token>
#     from any machine on the network. A token is REQUIRED (see below).
#
#  3. From anywhere: use the cloud dashboard (docs/cloud-dashboard.md), which
#     the publisher feeds by push, behind Cloudflare Access. Do NOT
#     port-forward this page to the internet: it is your live brokerage
#     account, and the token is all that would stand in front of it.
#
# --- token -------------------------------------------------------------------
# Binding anywhere other than loopback REQUIRES a shared token. Set DASHBOARD_TOKEN
# in .env to keep it stable; otherwise one is generated per run and saved to
# ~/.config/alpaca/dashboard_token (mode 0600; DASHBOARD_TOKEN_FILE moves it).
# The token is never printed: the server's output is teed into dashboard.log,
# and this script shows only which token is in force and where to find it.
set -euo pipefail
cd "$(dirname "$0")/.."
SESSION="dashboard"
HOST="127.0.0.1"
PORT="${DASHBOARD_PORT:-8787}"

while [ $# -gt 0 ]; do
  case "$1" in
    --lan)     HOST="0.0.0.0"; shift ;;
    --host)    HOST="$2"; shift 2 ;;
    --port)    PORT="$2"; shift 2 ;;
    --session) SESSION="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# .env is read INSIDE the tmux command, never exported from this shell. When no
# tmux server is running yet (after a reboot cron's deploy/ensure_up.sh is the
# first caller), the server copies THIS shell's environment into its global
# environment and hands that copy to every pane it starts from then on. The keys
# of the day the server was born would then outlive any later .env edit, because
# relay.config's load_dotenv() never overrides a variable that is already set.
DATADIR="$(set -a; [ -f .env ] && . ./.env; echo "${ALPACA_DATA_DIR:-/mnt/sandisk/alpaca}")"
mkdir -p "$DATADIR/logs"
LOG="$DATADIR/logs/dashboard.log"
# The log holds every request line; keep it to this user. (A filesystem that
# cannot hold modes refuses the chmod; that is no reason not to start.)
touch "$LOG"
chmod 600 "$LOG" 2>/dev/null || true
# Where this run's output starts, so the banner read back below is this run's
# and not the first one ever appended to the log.
LOG_START="$(stat -c %s "$LOG" 2>/dev/null || echo 0)"

tmux kill-session -t "=$SESSION" 2>/dev/null || true   # "=": exact name, not a prefix
sleep 1

tmux new-session -d -s "$SESSION" -n web -c "$PWD" \
  "set -a; [ -f .env ] && . ./.env; set +a; PYTHONPATH=. .venv/bin/python -m web.server --host $HOST --port $PORT 2>&1 | tee -a $LOG"

# web.server prints "  token:     ..." naming the token's SOURCE (.env, or the
# 0600 file a generated one was saved to), never the token itself.
TOKEN_LINE="  token:     (no startup banner yet: tmux attach -t $SESSION)"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  LINE="$(tail -c +"$((LOG_START + 1))" "$LOG" 2>/dev/null | grep -m1 "^  token:" || true)"
  [ -n "$LINE" ] && TOKEN_LINE="$LINE" && break
done
echo "tmux session: $SESSION  (window: web)"
echo "logs:         $LOG"
if [ "$HOST" = "0.0.0.0" ]; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "url:          http://${LAN_IP}:${PORT}/?token=<token>   (token required)"
else
  echo "url:          http://localhost:${PORT}/"
  echo "remote:       ssh -N -L ${PORT}:127.0.0.1:${PORT} ${USER:-$(id -un)}@$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "              ...then open http://localhost:${PORT} on that machine"
fi
echo "token:        ${TOKEN_LINE#  token:     }"
echo "attach:       tmux attach -t $SESSION   (detach: Ctrl-b d)"
echo "stop:         tmux kill-session -t $SESSION"
echo "keep down:    mkdir -p ~/.config/alpaca/hold && touch ~/.config/alpaca/hold/dashboard   (else cron restarts it within 5 min)"

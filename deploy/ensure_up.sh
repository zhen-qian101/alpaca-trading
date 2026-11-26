#!/usr/bin/env bash
# Bring the always-on trading stack back after a reboot, and keep it up.
#
# Run by cron: once at boot (--boot) and every 5 minutes. For each long-lived
# piece it asks "is this running?" and only if the answer is no does it start it,
# with the SAME launcher a human uses. So each thing still has one way to start
# and one place where it runs (its tmux session or the IBC chain). It never
# restarts anything that is up; on a healthy box every run is a silent no-op.
#
#   gateway    IB Gateway via IBC + Xvfb         deploy/restart_gateway.sh
#   engine     sigengine, tmux session "alpaca"   deploy/run_local.sh
#   dashboard  web/server.py, tmux "dashboard"    deploy/run_dashboard.sh
#
# Research, the recap render and the CSP job are ordinary cron jobs and already
# come back with the box. They are not handled here.
#
# WHY. On 2026-10-28 the GPU took the host down at 17:22. It came back at 22:20
# only because a human rebooted it, and none of the three pieces above came back
# until a human ran their launchers. The engine is the only thing that enforces
# per-position stops, so every minute it is down the account is unmanaged. Its
# tmux supervisor loop covers a crash; nothing covered a reboot or a dead tmux.
#
# HOLD. Anything stopped on purpose comes back within 5 minutes unless held:
#   mkdir -p ~/.config/alpaca/hold && touch ~/.config/alpaca/hold/engine
#   (or gateway, dashboard, all)    release: rm ~/.config/alpaca/hold/engine
# A hold also survives reboots, so a forgotten one keeps that piece down for
# good; the log says so at boot and once an hour, and --check lists holds.
#
#   ./deploy/ensure_up.sh           # start whatever is down
#   ./deploy/ensure_up.sh --check   # report only, start nothing
#   ./deploy/ensure_up.sh --boot    # @reboot: wait for the data disk + network first
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

MODE="run"
case "${1:-}" in
  --check) MODE="check" ;;
  --boot)  MODE="boot" ;;
  "")      ;;
  *) echo "usage: $0 [--check|--boot]" >&2; exit 2 ;;
esac

# Read ALPACA_DATA_DIR without exporting .env into this shell: the launchers
# read .env themselves, and the gateway's credentials have no business here.
DATADIR="$(grep -m1 -oP '^ALPACA_DATA_DIR=\K.*' .env 2>/dev/null | tr -d "\"'")"
DATADIR="${DATADIR:-/mnt/sandisk/alpaca}"
HOLD_DIR="${ALPACA_HOLD_DIR:-$HOME/.config/alpaca/hold}"
STATE_DIR="${ALPACA_ENSURE_STATE:-$HOME/.cache/alpaca-ensure}"
ENGINE_SESSION="alpaca"
DASH_SESSION="dashboard"
DASH_PORT="${DASHBOARD_PORT:-8787}"
GW_PORT=4002
GW_MIN_GAP=1800      # 30 min between gateway (re)starts, doubled per failed one
GW_MAX_GAP=14400     # ...up to 4 h: a login that needs a human must not be retried forever
GW_WEDGE_AFTER=900   # chain alive but API port shut this long -> restart it
mkdir -p "$STATE_DIR"

log() { echo "[$(date -u +%FT%TZ)] ensure_up: $*"; }
held() { [[ -e "$HOLD_DIR/$1" || -e "$HOLD_DIR/all" ]]; }
listening() { ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }
# "=" makes tmux match the name exactly; a bare -t also accepts a prefix.
has_session() { tmux has-session -t "=$1" 2>/dev/null; }
now() { date +%s; }
boot_epoch() { date -d "$(uptime -s)" +%s; }
# Once an hour (the :00 pass), or on every boot pass: for states a human must fix.
nag_now() { [[ "$MODE" == "boot" ]] || (( 10#$(date +%M) < 5 )); }

# The data disk is mounted with nofail. If it is missing, the launchers' own
# `mkdir -p $DATADIR/logs` would build an empty data dir on the ROOT disk and
# the engine would start with no state, no journal and no record of its open
# positions. Starting nothing is the only safe answer.
data_disk_ok() {
  [[ -d "$DATADIR" ]] && [[ "$(findmnt -n -o TARGET -T "$DATADIR" 2>/dev/null)" != "/" ]]
}

# Real engine processes, wherever they run. Anchored like run_local.sh's pkill,
# so a grep, tail or editor that mentions the engine does not count. Dry runs
# never trade and do not count.
engine_pids() {
  pgrep -af '^([^ ]*/)?python[0-9.]* -m sigengine\.engine( |$)' 2>/dev/null \
    | grep -v -- ' --dry-run' | cut -d' ' -f1
}
# A run_local.sh supervisor loop in ANY tmux session, including the 10 s gap
# between an engine exit and its restart, when no python process exists.
supervisor_pids() {
  pgrep -f '^([^ ]*/)?[a-z]*sh -c .*while true; do .*python -m sigengine\.engine' 2>/dev/null
}

# restart_gateway.sh owns the pattern that finds the IBC chain (a test pins it
# to its launch line), so ask it instead of keeping a second copy here. Sets
# GW_CHAINS (count) and GW_PGIDS (one process group per chain).
gateway_detect() {
  local line
  line="$(./deploy/restart_gateway.sh --detect-only 2>/dev/null | grep -m1 '^found ')"
  GW_CHAINS="$(sed -n 's/.* in \([0-9]*\) chain(s).*/\1/p' <<<"$line")"
  GW_CHAINS="${GW_CHAINS:-0}"
  GW_PGIDS="$(sed -n 's/.*chain(s): //p' <<<"$line")"
}

# Each auto-start that never got port 4002 open by the time of the next one
# doubles the gap: 30 min, 1 h, 2 h, 4 h. Stamps from before this boot do not
# count. The first pass that sees the port open resets the run of failures.
gateway_gap() {
  local fails gap
  fails="$(cat "$STATE_DIR/gateway.fails" 2>/dev/null || echo 0)"
  gap=$(( GW_MIN_GAP << (fails > 3 ? 3 : fails) ))
  echo $(( gap > GW_MAX_GAP ? GW_MAX_GAP : gap ))
}

start_gateway() {
  local why="$1" last opened gap
  last="$(cat "$STATE_DIR/gateway.last_start" 2>/dev/null || echo 0)"
  (( last > $(boot_epoch) )) || last=0
  gap="$(gateway_gap)"
  if (( last && $(now) - last < gap )); then
    nag_now && log "gateway $why, but it was auto-started $(( ($(now) - last) / 60 )) min ago; next try after $((gap / 60)) min"
    return 1
  fi
  opened="$(cat "$STATE_DIR/gateway.last_open" 2>/dev/null || echo 0)"
  if (( last && opened < last )); then
    echo $(( $(cat "$STATE_DIR/gateway.fails" 2>/dev/null || echo 0) + 1 )) > "$STATE_DIR/gateway.fails"
    log "the previous auto-start never opened port $GW_PORT (2FA or login? see /mnt/sandisk/alpaca/logs/ibc); retry gap now $(( $(gateway_gap) / 60 )) min"
  fi
  log "gateway $why: starting it"
  now > "$STATE_DIR/gateway.last_start"
  rm -f "$STATE_DIR/gateway.port_shut"
  # 9>&-: the chain lives until the next reboot, and must not take the lock with it.
  ./deploy/restart_gateway.sh 9>&- 2>&1 | sed 's/^/    /'
}

status() {
  local name="$1" state="$2"
  held "$name" && state="$state (HELD)"
  printf '  %-10s %s\n' "$name" "$state"
}

if [[ "$MODE" == "check" ]]; then
  echo "data disk: $DATADIR $(data_disk_ok && echo mounted || echo 'NOT MOUNTED')"
  gateway_detect
  if (( GW_CHAINS == 0 )); then
    status gateway "DOWN"
  else
    status gateway "up, API port $GW_PORT $(listening $GW_PORT && echo open || echo SHUT)$( (( GW_CHAINS > 1 )) && echo ", $GW_CHAINS CHAINS racing for the port: run deploy/restart_gateway.sh")"
  fi
  pids="$(engine_pids | paste -sd, -)"
  if has_session "$ENGINE_SESSION"; then
    status engine "$([[ -n "$pids" ]] && echo "up (tmux $ENGINE_SESSION, pid $pids)" || echo "tmux $ENGINE_SESSION present but NO engine process (restart gap or crash loop? tail $DATADIR/logs/sigengine.log)")"
  elif [[ -n "$pids$(supervisor_pids)" ]]; then
    status engine "running OUTSIDE tmux $ENGINE_SESSION (pid ${pids:-supervisor only})"
  else
    status engine "DOWN"
  fi
  if has_session "$DASH_SESSION" || listening "$DASH_PORT"; then
    status dashboard "up (port $DASH_PORT $(listening "$DASH_PORT" && echo open || echo SHUT))"
  else
    status dashboard "DOWN"
  fi
  if [[ -e "$STATE_DIR/last_pass" ]]; then
    age=$(( ($(now) - $(stat -c %Y "$STATE_DIR/last_pass")) / 60 ))
    echo "watchdog: last pass $age min ago$( (( age > 10 )) && echo ' -- STALE: is the cron line installed? is a pass stuck holding the lock?')"
  else
    echo "watchdog: has never run a pass"
  fi
  echo "gpu: $(timeout 20 nvidia-smi --query-gpu=name,power.limit --format=csv,noheader 2>/dev/null || echo 'NOT VISIBLE (the recap render would fail)')"
  exp="$(python3 -c 'import json,os,time; d=json.load(open(os.path.expanduser("~/.claude/.credentials.json")))["claudeAiOauth"]["refreshTokenExpiresAt"]; print(f"{(d/1000-time.time())/86400:.1f} days")' 2>/dev/null)"
  echo "claude login (research + recap): ${exp:-unknown} left"
  ls "$HOLD_DIR" 2>/dev/null | sed 's/^/hold: /'
  exit 0
fi

# One pass at a time: @reboot waits on the disk and network for minutes, and
# the 5-minute pass must not start a second copy of anything meanwhile. Every
# launcher below gets 9>&- so nothing it leaves running inherits the lock.
exec 9>"$STATE_DIR/lock"
if ! flock -n 9; then
  [[ -e "$STATE_DIR/last_pass" ]] && (( $(now) - $(stat -c %Y "$STATE_DIR/last_pass") > 900 )) \
    && log "WARNING: another pass has held the lock for over 15 min (see: fuser $STATE_DIR/lock)"
  exit 0
fi
touch "$STATE_DIR/last_pass"

if [[ "$MODE" == "boot" ]]; then
  log "boot pass (up since $(uptime -s))"
  for _ in $(seq 1 60); do data_disk_ok && break; sleep 10; done
  for _ in $(seq 1 30); do getent ahosts paper-api.alpaca.markets >/dev/null && break; sleep 10; done
  timeout 20 nvidia-smi --query-gpu=power.limit --format=csv,noheader >/dev/null 2>&1 \
    || log "WARNING: GPU not visible; the recap render will fail until the driver is back"
  touch "$STATE_DIR/last_pass"
fi

if ! data_disk_ok; then
  log "data disk $DATADIR is not mounted; starting nothing"
  exit 1
fi

# --- gateway ---------------------------------------------------------------
# Started first so the engine's first polls get IBKR bars. A chain that is
# alive but has kept its API port shut for 15 min is wedged (IBC parked on a
# login dialog). The port also closes for a minute or two during IBC's own
# 03:00 restart, and stays shut for the ~90 s of any fresh login, so the timer
# is tied to one chain (its process group) and the chain itself must be older
# than the threshold: a gateway someone just restarted by hand is left alone.
gw_started=0
gateway_detect
if listening "$GW_PORT"; then
  now > "$STATE_DIR/gateway.last_open"
  rm -f "$STATE_DIR/gateway.fails"
fi
if (( GW_CHAINS > 1 )) && nag_now; then
  log "WARNING: $GW_CHAINS gateway chains are racing for port $GW_PORT; run ./deploy/restart_gateway.sh to collapse them"
fi
if held gateway; then
  rm -f "$STATE_DIR/gateway.port_shut"
  (( GW_CHAINS == 0 )) && nag_now && log "gateway is DOWN and HELD ($HOLD_DIR); not starting it"
elif (( GW_CHAINS == 0 )); then
  start_gateway "is down" && gw_started=1
elif listening "$GW_PORT"; then
  rm -f "$STATE_DIR/gateway.port_shut"
else
  pgid="${GW_PGIDS%% *}"
  since=0 shut_pgid=""
  [[ -s "$STATE_DIR/gateway.port_shut" ]] && read -r since shut_pgid < "$STATE_DIR/gateway.port_shut"
  if [[ "${shut_pgid:-}" != "$pgid" ]]; then
    since="$(now)"
    echo "$since $pgid" > "$STATE_DIR/gateway.port_shut"
  fi
  age="$(ps -o etimes= -p "$pgid" 2>/dev/null | tr -d ' ')"
  if (( $(now) - since >= GW_WEDGE_AFTER && ${age:-0} >= GW_WEDGE_AFTER )); then
    start_gateway "has had port $GW_PORT shut for $(( ($(now) - since) / 60 )) min" && gw_started=1
  fi
fi

# --- engine ----------------------------------------------------------------
# An engine or supervisor anywhere (an orphan whose tmux died, one in another
# session, one started by hand) counts as up: a second engine means double
# orders, so this never starts one beside it. The supervisor loop inside the
# session restarts crashes.
if held engine; then
  ! has_session "$ENGINE_SESSION" && [[ -z "$(engine_pids)$(supervisor_pids)" ]] && nag_now \
    && log "engine is DOWN and HELD ($HOLD_DIR); not starting it"
elif ! has_session "$ENGINE_SESSION" && [[ -z "$(engine_pids)$(supervisor_pids)" ]]; then
  if (( gw_started )); then
    for _ in $(seq 1 30); do listening "$GW_PORT" && break; sleep 5; done
  fi
  log "engine is down: starting it"
  ./deploy/run_local.sh 9>&- 2>&1 | sed 's/^/    /'
fi

# --- dashboard -------------------------------------------------------------
# The recap render screenshots it at 20:00 ET for the DESK slide.
if held dashboard; then
  ! has_session "$DASH_SESSION" && ! listening "$DASH_PORT" && nag_now \
    && log "dashboard is DOWN and HELD ($HOLD_DIR); not starting it"
elif ! has_session "$DASH_SESSION" && ! listening "$DASH_PORT"; then
  log "dashboard is down: starting it"
  ./deploy/run_dashboard.sh 9>&- 2>&1 | sed 's/^/    /'
fi

[[ "$MODE" == "boot" ]] && log "boot pass done"
exit 0

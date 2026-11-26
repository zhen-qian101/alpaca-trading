#!/usr/bin/env bash
# Restart the headless IB Gateway (IBC) chain — the lever for a wedged data farm.
#
# WHEN TO REACH FOR THIS. The Gateway logs in fine, the engine connects in
# ~0.25s, and every reqHistoricalData comes back empty, with
# `Warning 2105 ... HMDS data farm connection is broken:ushmds` on each
# connection banner. Farm connections belong to the GATEWAY process, so a fresh
# API connect cannot heal one — only restarting the Gateway can (or IBKR fixing
# their end; if this script does not clear it, that is which one it was).
#
# Distinguish that from the other two IBKR failure modes before running it:
#   * `Connect call failed (111)` with a live java process -> the API port never
#     opened, usually IBC parked on a login dialog. This script fixes that too.
#   * `global call lock busy` -> not a Gateway problem at all; that is queueing
#     inside sigengine.ibkr (see _global_acquire_timeout).
#
# Usage:
#   ./deploy/restart_gateway.sh                # stop any chain, relaunch
#   ./deploy/restart_gateway.sh --detect-only  # report what it WOULD stop
#   ./deploy/restart_gateway.sh --stop-only    # stop, do not relaunch
#     (cron's deploy/ensure_up.sh relaunches it within 5 min unless held:
#      mkdir -p ~/.config/alpaca/hold && touch ~/.config/alpaca/hold/gateway)
#
# Credentials come from .env (TWS_USERID / TWS_PASSWORD), exactly as
# deploy/run_ibgateway.sh maps them. They are NOT written into this file: it
# lives in git, and the copy this replaced carried the paper password in clear
# text at ~/IBC/restart_gateway.sh.
set -uo pipefail
# readlink -f, not plain dirname: ~/IBC/restart_gateway.sh is a symlink to this
# file (that path is in the runbooks and in a year of notes), and a symlink
# makes $0 the LINK. dirname of the link is ~/IBC, so ".." would be the home
# directory — and .env would silently not be found, leaving the credential
# check to fail on a box that is configured perfectly well.
cd "$(dirname "$(readlink -f "$0")")/.."

MODE="run"
case "${1:-}" in
  --detect-only) MODE="detect" ;;
  --stop-only)   MODE="stop" ;;
  "")            ;;
  *) echo "usage: $0 [--detect-only|--stop-only]" >&2; exit 2 ;;
esac

# --------------------------------------------------------------------------
# THE DETECTION PATTERN MUST MATCH HOW THIS SCRIPT LAUNCHES. That sentence is
# the entire bug this file exists to prevent.
#
# The original pattern was "xvfb-run -a env.*IBC_PATH", written when the launch
# used `xvfb-run -a`. The launch later moved to `-n <display>` (stale Xvfb
# servers squat :99-:109 and -a hands out a squatted one), and the pattern was
# not moved with it. From its second run onward the script could no longer find
# the chain it had started itself: it printed "no running gateway chain found,
# just launching" and started a SECOND Gateway beside the wedged one — two java
# processes on one TWS_SETTINGS_PATH, racing for port 4002. Seen live
# 2026-08-20 with :130 and :131 both up, and it had to be untangled by hand.
#
# tests/test_restart_gateway.py derives BOTH the pattern and the launch line
# from this file and asserts they still agree, so the two cannot drift again.
#
# IBC_MATCH is overridable ONLY so those tests can aim the stop logic at
# throwaway processes instead of the real Gateway. Nothing in production sets it.
IBC_MATCH="${IBC_MATCH:-xvfb-run -[an][^|]*IBC_PATH}"

mapfile -t CHAIN_PIDS < <(pgrep -f "$IBC_MATCH" || true)
if [[ ${#CHAIN_PIDS[@]} -gt 0 ]]; then
  # One chain spans several PIDs (bash -c, /bin/sh xvfb-run, Xvfb, java) sharing
  # a process group. Collapse to distinct groups so each is signalled once — and
  # signal ALL of them, because a run under the old script may have left two.
  #
  # NEVER signal our own group. pgrep -f matches COMMAND LINES, so a shell whose
  # command line happens to contain this pattern matches too — hit accidentally
  # on 2026-08-29, where running the pattern inline from an interactive shell
  # matched that shell and turned 1 real chain into 3. This script's own command
  # line cannot match, so this is belt-and-braces; what it guards against is the
  # script killing the terminal that launched it.
  SELF_PGID=$(ps -o pgid= -p $$ | tr -d ' ')
  mapfile -t PGIDS < <(ps -o pgid= -p "${CHAIN_PIDS[*]// /,}" 2>/dev/null \
                        | tr -d ' ' | sort -u | grep -v "^${SELF_PGID}$")
  echo "found ${#CHAIN_PIDS[@]} gateway process(es) in ${#PGIDS[@]} chain(s): ${PGIDS[*]:-none}"
  if [[ "$MODE" == "detect" ]]; then
    exit 0
  fi
  for PGID in "${PGIDS[@]}"; do
    [[ -n "$PGID" ]] || continue
    echo "stopping gateway chain (pgid ${PGID})"
    kill -TERM -"${PGID}" 2>/dev/null || true
  done
  for _ in $(seq 1 20); do
    pgrep -f "$IBC_MATCH" >/dev/null 2>&1 || break
    sleep 1
  done
  if pgrep -f "$IBC_MATCH" >/dev/null 2>&1; then
    echo "still up after 20s, sending KILL"
    for PGID in "${PGIDS[@]}"; do
      [[ -n "$PGID" ]] || continue
      kill -KILL -"${PGID}" 2>/dev/null || true
    done
    sleep 2
  fi
  echo "gateway processes still running after stop: $(pgrep -cf "$IBC_MATCH" || true)"
else
  echo "no running gateway chain found"
  [[ "$MODE" == "detect" ]] && exit 0
fi

[[ "$MODE" == "stop" ]] && exit 0

# --------------------------------------------------------------------------
set -a; [ -f .env ] && source .env; set +a
if [[ -z "${TWS_USERID:-}" || -z "${TWS_PASSWORD:-}" ]]; then
  echo "ERROR: set TWS_USERID and TWS_PASSWORD in .env (your IBKR paper" \
       "credentials) — deploy/run_ibgateway.sh documents the same two keys." >&2
  exit 1
fi

# xvfb-run -a is UNSAFE here. ~15 stale Xvfb servers from July still hold
# :99-:109, but tmpfiles aged their /tmp/.X*-lock files away — so -a hands out a
# display whose old server rejects the new auth cookie ("Can't connect to X11
# ... ':109'", IBC exits 1100/76). Pin the first genuinely free display >= 130,
# probing BOTH the lock file and a live server.
XVFB_DISPLAY="${XVFB_DISPLAY:-130}"
while [ -e "/tmp/.X${XVFB_DISPLAY}-lock" ] || pgrep -f "Xvfb :${XVFB_DISPLAY} " >/dev/null 2>&1; do
  XVFB_DISPLAY=$((XVFB_DISPLAY + 1))
done

IBC_PATH="${IBC_PATH:-$HOME/IBC}"
TWS_PATH="${TWS_PATH:-$HOME/Jts}"
TWS_MAJOR_VRSN="${TWS_MAJOR_VRSN:-1045}"
IBC_INI="${IBC_INI:-$HOME/.config/ibc/ibc.ini}"
LOG_PATH="${LOG_PATH:-/mnt/sandisk/alpaca/logs/ibc}"
TRADING_MODE="${IBKR_TRADING_MODE:-paper}"
mkdir -p "$LOG_PATH"

echo "relaunching IB Gateway via IBC on display :${XVFB_DISPLAY} (mode ${TRADING_MODE})"
# setsid so the chain outlives this shell and owns its own process group — which
# is exactly the group the stop logic above signals next time.
setsid bash -c "xvfb-run -n ${XVFB_DISPLAY} env \
  IBC_PATH='${IBC_PATH}' \
  TWS_PATH='${TWS_PATH}' \
  TWS_MAJOR_VRSN='${TWS_MAJOR_VRSN}' \
  TWS_SETTINGS_PATH='${TWS_PATH}/ibgateway/${TWS_MAJOR_VRSN}' \
  IBC_INI='${IBC_INI}' \
  TRADING_MODE='${TRADING_MODE}' \
  TWOFA_TIMEOUT_ACTION='exit' \
  LOG_PATH='${LOG_PATH}' \
  APP='GATEWAY' \
  TWSUSERID='${TWS_USERID}' \
  TWSPASSWORD='${TWS_PASSWORD}' \
  FIXUSERID='' \
  FIXPASSWORD='' \
  _JAVA_OPTIONS='-Djava.net.preferIPv4Stack=true' \
  JAVA_PATH='' \
  '${IBC_PATH}/scripts/displaybannerandlaunch.sh' 2>&1 \
  | tee -a '${LOG_PATH}/gateway.log'" >/dev/null 2>&1 &
disown

echo "launched; the API port takes ~90s to open. Watch:"
echo "  ss -ltn | grep 4002"
echo "  tail -f ${LOG_PATH}/ibc-*_$(date +%A).txt"
echo "Then confirm the farm is healthy — the engine log should show OK:ushmds,"
echo "not broken:ushmds. If it still says broken, the wedge is IBKR-side and a"
echo "restart cannot clear it."

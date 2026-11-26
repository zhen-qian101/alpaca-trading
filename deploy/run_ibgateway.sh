#!/usr/bin/env bash
# Start IB Gateway (paper) headlessly via IBC, kept alive by ib_async's
# Watchdog from inside the signal engine. This script just launches the Gateway;
# the engine (sigengine/ibkr.py) connects to 127.0.0.1:4002 and uses Watchdog to
# reconnect if it drops. If the Gateway is down, the engine falls back to
# yfinance automatically — so this is only needed for IBKR-primary data.
#
# Layout this targets (installed 2026-07-02): IBC 3.24.0 in a FLAT layout at
# ~/IBC — i.e. ~/IBC/gatewaystart.sh (Gateway launcher, APP=GATEWAY) and
# ~/IBC/scripts/ibcstart.sh. gatewaystart.sh takes NO -t/-g/-c flags; it reads
# env vars (IBC_PATH, TWS_PATH, TWS_MAJOR_VRSN, TRADING_MODE, IBC_INI,
# TWSUSERID, TWSPASSWORD, ...) and execs scripts/displaybannerandlaunch.sh, which
# calls ibcstart.sh with --user/--pw from TWSUSERID/TWSPASSWORD (overriding the
# ini's IbLoginId/IbPassword). The previous version of this script assumed the
# ~/IBC/InvestmentStudio/IBC/twsstart.sh layout + -t paper -g -c flags, which do
# not exist in this install.
#
# Java: no system JRE required. IB Gateway's installer bundles a JRE and writes
# install4j/pref_jre.cfg; ibcstart.sh reads that to find java automatically.
#
# PREREQUISITES (one-time, the user must do these):
#   1. Install IB Gateway (paper):  ./ibgateway-installer.sh   (run once)
#      Installs to ~/Jts/ibgateway/<MAJOR>. This script auto-detects the major.
#   2. Install IBC 3.24.0 at ~/IBC: https://github.com/IbcAlpha/IBC
#   3. Install xvfb (headless X server):  sudo apt install -y xvfb
#   4. Sign the Market Data API Acknowledgement in Client Portal and confirm
#      equity network subscriptions (Network A/B/C). Without it IBKR returns
#      "not subscribed" and the engine uses yfinance.
#   5. Set TWS_USERID (IBKR paper username) + TWS_PASSWORD in .env (never commit).
#
#   ./deploy/run_ibgateway.sh            # session "ibgateway"
#   ./deploy/run_ibgateway.sh mysession
#
# Attach:  tmux attach -t ibgateway   (Ctrl-b d to detach)
# Stop:    tmux kill-session -t ibgateway
set -euo pipefail
cd "$(dirname "$0")/.."
SESSION="${1:-ibgateway}"
set -a; [ -f .env ] && source .env; set +a

if [ -z "${TWS_USERID:-}" ] || [ -z "${TWS_PASSWORD:-}" ]; then
  echo "ERROR: set TWS_USERID and TWS_PASSWORD in .env (your IBKR paper credentials)."
  exit 1
fi

# --- IBC / Gateway paths (override via env if non-default) -------------------
IBC_PATH="${IBC_PATH:-$HOME/IBC}"
GATEWAY_START="$IBC_PATH/gatewaystart.sh"
IBC_INI="${IBC_INI:-$HOME/.config/ibc/ibc.ini}"
# Allow using the repo's template if the user hasn't copied it yet.
[ -f "$IBC_INI" ] || IBC_INI="$(pwd)/deploy/ibc.ini"
TWS_PATH="${TWS_PATH:-$HOME/Jts}"

if [ ! -x "$GATEWAY_START" ]; then
  echo "ERROR: IBC gatewaystart.sh not found/executable at $GATEWAY_START."
  echo "  Install IBC 3.24.0 at ~/IBC: https://github.com/IbcAlpha/IBC"
  echo "  (ensure ~/IBC/gatewaystart.sh and ~/IBC/scripts/*.sh are executable)."
  exit 1
fi
if [ ! -f "$IBC_INI" ]; then
  echo "ERROR: IBC ini not found at $IBC_INI."
  echo "  Copy deploy/ibc.ini to ~/.config/ibc/ibc.ini or set IBC_INI."
  exit 1
fi

# --- Detect installed IB Gateway major version (e.g. 10.47 -> 1047) ---------
# IB Gateway installs to ~/Jts/ibgateway/<MAJOR>/ where MAJOR is the version with
# the dot stripped (the TWS_MAJOR_VRSN format IBC expects). Auto-detect so this
# script survives Gateway upgrades without editing.
if [ -z "${TWS_MAJOR_VRSN:-}" ]; then
  GW_DIR="$TWS_PATH/ibgateway"
  if [ -d "$GW_DIR" ]; then
    # Pick the highest numeric subdirectory (e.g. 1045 for Gateway 10.45).
    # -name (shell glob) is used instead of -regex because GNU find's default
    # emacs regex flavor mishandles the [0-9]+ pattern on some builds.
    TWS_MAJOR_VRSN="$(find "$GW_DIR" -maxdepth 1 -mindepth 1 -type d \
      -name '[0-9]*' -printf '%f\n' 2>/dev/null | sort -n | tail -1)"
  fi
fi
if [ -z "${TWS_MAJOR_VRSN:-}" ]; then
  echo "ERROR: could not detect an installed IB Gateway under $GW_DIR."
  echo "  Run the IB Gateway installer first (./ibgateway-installer.sh)."
  echo "  Or set TWS_MAJOR_VRSN explicitly (e.g. 1047)."
  exit 1
fi

# --- Headless X server -------------------------------------------------------
if [ -z "${DISPLAY:-}" ]; then
  if command -v xvfb-run >/dev/null 2>&1; then
    LAUNCH="xvfb-run -a"
  else
    echo "ERROR: no DISPLAY and xvfb-run not found. Install: sudo apt install -y xvfb"
    exit 1
  fi
else
  LAUNCH=""
fi

DATADIR="${ALPACA_DATA_DIR:-/mnt/sandisk/alpaca}"
mkdir -p "$DATADIR/logs/ibc"

# --- Credentials: .env TWS_USERID/TWS_PASSWORD -> IBC's TWSUSERID/TWSPASSWORD --
export TWSUSERID="$TWS_USERID"
export TWSPASSWORD="$TWS_PASSWORD"
export FIXUSERID=""
export FIXPASSWORD=""

# --- IBC env vars ---
# NOTE: we do NOT use gatewaystart.sh. It sets IBC_PATH=/opt/ibc, TWS_MAJOR_VRSN=1019,
# IBC_INI=~/ibc/config.ini, TWS_PATH=~/Jts etc. as UNCONDITIONAL assignments at its
# top, which clobber any exported env vars — so the values below would be ignored.
# Instead we call displaybannerandlaunch.sh directly: it reads ALL of these from the
# environment (no clobbering) and forwards them to ibcstart.sh as --tws-path/--ibc-ini/
# --user/--pw/etc. APP=GATEWAY tells it to pass the -g flag to ibcstart.sh.
# IBKR_TRADING_MODE in .env controls this — "paper" (default, safe) or "live"
# to match live-account TWS_USERID/TWS_PASSWORD (see the note in ibc.ini: some
# live accounts with multiple linked paper sub-accounts CANNOT auto-resolve
# under "paper" mode and require "live" here instead). This is UNRELATED to
# PAPER_TRADING in .env, which governs Alpaca order placement, not this IBKR
# DATA-ONLY connection (ReadOnlyApi=yes in ibc.ini blocks all IBKR orders
# regardless of this setting).
export IBC_PATH
export TWS_PATH
export TWS_MAJOR_VRSN
export TRADING_MODE="${IBKR_TRADING_MODE:-paper}"
export IBC_INI
export TWS_SETTINGS_PATH="$TWS_PATH/ibgateway/$TWS_MAJOR_VRSN"
export LOG_PATH="$DATADIR/logs/ibc"
# If a 2FA prompt appears and times out unanswered, exit cleanly rather than
# hang — with AutoRestartTime/ColdRestartTime set correctly in ibc.ini, this
# should only ever happen on a missed Sunday cold-restart tap; the engine
# keeps running on the yfinance fallback until you next restart the Gateway.
export TWOFA_TIMEOUT_ACTION="exit"
export APP="GATEWAY"
LAUNCH_SCRIPT="$IBC_PATH/scripts/displaybannerandlaunch.sh"

if [ ! -x "$LAUNCH_SCRIPT" ]; then
  echo "ERROR: $LAUNCH_SCRIPT not found/executable."
  exit 1
fi

tmux kill-session -t "$SESSION" 2>/dev/null || true
sleep 1

# displaybannerandlaunch.sh runs ibcstart.sh in the foreground (when LOG_PATH is
# set it backgrounds ibcstart and waits on it) and forwards TERM/INT — ideal for
# staying alive inside tmux. The banner + IBC stdout go to gateway.log.
#
# IMPORTANT: `export` does NOT reach the tmux child. The tmux *server* is a
# long-running process started outside this script; new sessions inherit the
# server's environment (filtered by update-environment), not this shell's
# exports — so IBC_PATH etc. came up empty and displaybannerandlaunch.sh failed
# ("/version: No such file"). Pass every var explicitly via `env` so the child
# actually sees them.
# -Djava.net.preferIPv4Stack=true (2026-07-04): this machine has IPv6 privacy
# extensions enabled (temporary, periodically-rotating addresses — confirmed
# 6+ rotations already accumulated), so successive Gateway connections could
# genuinely originate from DIFFERENT outbound IPv6 addresses even with no
# other device involved. That's a plausible contributor to IBKR's error 162
# ("Trading TWS session is connected from a different IP address") on the
# shared paper/live market-data entitlement. Forcing IPv4 (this machine has a
# stable public IPv4 address, unlike the rotating IPv6 one) removes that
# variable entirely.
#
# IMPORTANT: use _JAVA_OPTIONS, NOT JAVA_TOOL_OPTIONS. First attempt used
# JAVA_TOOL_OPTIONS and silently failed — confirmed via /proc/<pid>/environ on
# the running Gateway process that it arrived empty. Root cause: IBC's own
# ~/IBC/scripts/ibcstart.sh unconditionally does `JAVA_TOOL_OPTIONS=` (line
# ~498) right before invoking java, wiping out whatever was inherited. Its
# other injection point — the ibgateway.vmoptions file — ALSO doesn't work for
# this flag: ibcstart.sh's vmoptions parser explicitly SKIPS any line starting
# with "-D" (it wants to own all -D system properties itself, appending its
# own via string concat). _JAVA_OPTIONS is a real, separate JVM env-var
# override (like JAVA_TOOL_OPTIONS but different) that neither of those
# scripts references at all, so it survives untouched all the way to the
# actual `java` invocation. Picked up automatically by any JVM (prints one
# harmless "Picked up _JAVA_OPTIONS" line to stderr).
ENV_VARS="env \
  IBC_PATH='$IBC_PATH' \
  TWS_PATH='$TWS_PATH' \
  TWS_MAJOR_VRSN='$TWS_MAJOR_VRSN' \
  TWS_SETTINGS_PATH='$TWS_SETTINGS_PATH' \
  IBC_INI='$IBC_INI' \
  TRADING_MODE='$TRADING_MODE' \
  TWOFA_TIMEOUT_ACTION='$TWOFA_TIMEOUT_ACTION' \
  LOG_PATH='$LOG_PATH' \
  APP='$APP' \
  TWSUSERID='$TWSUSERID' \
  TWSPASSWORD='$TWSPASSWORD' \
  FIXUSERID='$FIXUSERID' \
  FIXPASSWORD='$FIXPASSWORD' \
  _JAVA_OPTIONS='-Djava.net.preferIPv4Stack=true' \
  JAVA_PATH='${JAVA_PATH:-}'"

tmux new-session -d -s "$SESSION" -n gateway \
  "$LAUNCH $ENV_VARS '$LAUNCH_SCRIPT' 2>&1 | tee -a '$DATADIR/logs/ibc/gateway.log'"

echo "tmux session:  $SESSION  (window: gateway)"
echo "Gateway ver:   $TWS_MAJOR_VRSN  (from $TWS_PATH/ibgateway/$TWS_MAJOR_VRSN)"
echo "IBC ini:       $IBC_INI"
echo "API on:        127.0.0.1:4002  (the signal engine connects here)"
echo "logs:          $DATADIR/logs/ibc/gateway.log"
echo "attach:        tmux attach -t $SESSION   (Ctrl-b d)"
echo "stop:          tmux kill-session -t $SESSION"
echo
echo "Once the Gateway is up, verify with:"
echo "  PYTHONPATH=. .venv/bin/python -m sigengine.check_ibkr"
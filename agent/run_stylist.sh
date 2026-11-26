#!/usr/bin/env bash
# Build the presenter's outfit + keyframe for TODAY'S SESSION, hours before the
# render needs it, so a human can actually look at it first.
#
# WHY THIS EXISTS
# ---------------
# generate.py already builds the day's look, but it does it INSIDE the 20:00 ET
# recap run, moments before the clips that use it. That is too late to review:
# by the time anyone sees the outfit it is already in a rendered video, and the
# only lever left is a re-render. Nothing about the stylist needs to happen at
# render time — it depends on the weather and the wardrobe, both of which are
# settled by the opening bell.
#
# So this runs at the open, and the evening render finds the look already on
# disk and skips its own stylist step ("look already built" — the same
# HISTORY_DIR/<date>.json check generate.py makes). Same artefact, same code
# path, just hours earlier and reviewable.
#
# IDEMPOTENT. Re-running after the look exists is a no-op, so a retry, a manual
# run, or an overlap with the render costs nothing.
#
# Cron (09:35 ET on trading days, five minutes after the open):
#   35 * * * * cd <repo> && ./agent/run_stylist.sh >> agent/stylist.log 2>&1
# (wrapped by cron/at_et.sh in the crontab, same as every other schedule here)
set -euo pipefail
cd "$(dirname "$0")/.."

# The SESSION date, in ET — the same date generate.py keys the look by. At 09:35
# ET this is today; using the local Melbourne date would be tomorrow's and build
# a look nothing ever reads.
DATE="${1:-$(TZ=America/New_York date +%F)}"

# PER PRESENTER, resolved exactly as generate.py does it: config host.host_json,
# with BRIEFING_HOST_JSON overriding one run. Hardcoding a presenter here is the
# bug that serves one presenter's look to another the moment the presenter
# changes — a failure that shows up only in the finished video. See
# host_briefing.day_keyframe's docstring. There is no default presenter.
HOST_FILE="${BRIEFING_HOST_JSON:-$(.venv/bin/python -c "
import json; print(json.load(open('briefing/config.json')).get('host',{}).get('host_json') or '')")}"
if [ -z "$HOST_FILE" ]; then
  echo "[$(date -u +%FT%TZ)] ERROR: briefing/config.json host.host_json is not set — no presenter" >&2
  exit 1
fi
case "$HOST_FILE" in
  host2.json) MODULE="briefing.host2_stylist"; ASSETS="briefing/assets/host2" ;;
  *)
    echo "[$(date -u +%FT%TZ)] ERROR: no stylist module for ${HOST_FILE}" >&2
    exit 1 ;;
esac

LOOK="${ASSETS}/stylist/${DATE}.json"
if [ -f "$LOOK" ]; then
  echo "[$(date -u +%FT%TZ)] ${DATE}: look already built (${LOOK}) — nothing to do"
  exit 0
fi

# Honour the same two switches the render honours, so turning the stylist off
# turns it off everywhere rather than leaving this quietly building looks.
ENABLED="$(.venv/bin/python -c "
import json
cfg = json.load(open('briefing/config.json')).get('host', {})
host = json.load(open('briefing/${HOST_FILE}'))
print('1' if cfg.get('stylist', True) and host.get('stylist', True) else '0')")"
if [ "${BRIEFING_STYLIST:-1}" != "1" ] || [ "$ENABLED" != "1" ]; then
  echo "[$(date -u +%FT%TZ)] stylist disabled (BRIEFING_STYLIST/config) — locked look stands"
  exit 0
fi

# The comfy env, not alpaca-media: draw_plate needs cv2, which alpaca-media
# deliberately lacks. This is the same subprocess generate.py spawns, and
# $COMFY_PYTHON overrides the env's python here as it does there
# (briefing.comfy.comfy_python).
echo "[$(date -u +%FT%TZ)] building ${DATE} look for ${HOST_FILE} via ${MODULE}"
"${COMFY_PYTHON:-$HOME/anaconda3/envs/comfy/bin/python}" -u -m "$MODULE" --date "$DATE"

if [ -f "$LOOK" ]; then
  echo "[$(date -u +%FT%TZ)] done — review: $LOOK"
  echo "  keyframe: $(ls -t ${ASSETS}/keyframes/stylist*_stage16x9.png 2>/dev/null | head -1)"
else
  # build_look returns None when the day fails its identity gates and the module
  # exits non-zero; the locked look then stands and the render is unaffected.
  echo "[$(date -u +%FT%TZ)] no look written — the day failed its gates, locked look stands"
fi

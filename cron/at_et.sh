#!/usr/bin/env bash
# Run a command only when it is a given wall-clock time in NEW YORK.
#
# WHY THIS EXISTS
# ---------------
# Every schedule in this project is defined against the US trading session, but
# this machine's clock is Australia/Melbourne and Ubuntu's cron has no per-user
# timezone. `man 5 crontab`, LIMITATIONS: "It currently does not support per-user
# timezones. ... Even if a user specifies the TZ environment variable in his
# crontab this will affect only the commands executed in the crontab, not the
# execution of the crontab tasks themselves" — and it recommends exactly this
# workaround ("one may check the date in the child script").
#
# The Melbourne->New York offset is NOT constant. It is 14h, 15h or 16h depending
# on which side of each zone's DST switch we are on (Melbourne flips in Oct/Apr,
# New York in Mar/Nov), so a fixed local cron time drifts by up to two hours in ET
# across the year. Measured against the schedule this replaces:
#
#   job                      Jul(14h)  Oct(15h)  Nov-Mar(16h)
#   intraday "market open"     09:30     08:30      07:30      <- before the open
#   intraday "midday"          12:30     11:30      10:30
#   intraday "pre-close"       15:30     14:30      13:30      <- 2.5h before close
#   post-market recap          20:00     19:00      18:00
#
# So for roughly five months a year the "market open" research ran while the
# market was shut, and the 8pm recap ran at 6pm. Anchoring on ET fixes that
# permanently instead of needing a manual nudge at four DST boundaries a year.
#
# The WEEKDAY is evaluated in New York too, which removes the other trap the old
# crontab worked around by hand: a US session's midday and afternoon fall on the
# NEXT calendar day in Melbourne, so Friday's session rolled into Saturday and a
# plain "1-5" silently dropped two of every Friday's three checks. In ET the
# session's own weekday is simply 1-5.
#
# Usage:
#   cron/at_et.sh <HH:MM> <ET-weekday-spec> <command> [args...]
#     HH:MM             target New York wall-clock time, 24h, zero-padded
#     ET-weekday-spec   1=Mon .. 7=Sun; comma list and/or ranges ("1-5", "6", "7")
#
# Cron then fires this once an hour at the target MINUTE and the guard decides:
#   30 * * * * cd /repo && cron/at_et.sh 09:30 1-5 ./agent/run_research_intraday.sh
# The 23 no-op wakeups a day cost a `date` call each. That is deliberately dumber
# than computing which local hours could map to the target: the union changes with
# DST, which is the very thing being worked around.
#
# Exits 0 silently when it is not the moment (a no-op must not look like failure
# in cron mail). NOTE: US market holidays are not modelled here, exactly as they
# were not before — a research run on a closed day is harmless and visible.
set -uo pipefail

TARGET="${1:-}"; DOW_SPEC="${2:-}"
if [ -z "$TARGET" ] || [ -z "$DOW_SPEC" ] || [ "$#" -lt 3 ]; then
    echo "usage: $0 <HH:MM> <ET-weekday-spec> <command> [args...]" >&2
    exit 2
fi
shift 2

case "$TARGET" in
    [0-2][0-9]:[0-5][0-9]) ;;
    *) echo "$0: target '$TARGET' is not zero-padded HH:MM" >&2; exit 2 ;;
esac

now_hm="$(TZ=America/New_York date +%H:%M)"
now_dow="$(TZ=America/New_York date +%u)"

[ "$now_hm" = "$TARGET" ] || exit 0

matched=0
IFS=','
for part in $DOW_SPEC; do
    case "$part" in
        *-*)
            lo="${part%%-*}"; hi="${part##*-}"
            if [ "$now_dow" -ge "$lo" ] && [ "$now_dow" -le "$hi" ]; then matched=1; fi
            ;;
        *)
            if [ "$now_dow" = "$part" ]; then matched=1; fi
            ;;
    esac
done
unset IFS

[ "$matched" -eq 1 ] || exit 0

exec "$@"

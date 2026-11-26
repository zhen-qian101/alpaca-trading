#!/usr/bin/env bash
# WEEKLY CASH-SECURED PUT — one contract a week, tried on three days.
#
# Deliberately NOT inside the signal engine's 300s poll loop. The engine is a
# bar-driven equity machine; this fires on a wall clock, and mixing the two
# would put a weekly option decision behind an every-5-minutes retry.
#
# 15:45 ET is late enough that the day's move is settled and early enough that
# a limit order still has 15 minutes of liquidity to fill in. Options are
# DAY-only and do not trade outside regular hours, so there is no second chance
# after the bell.
#
# FRIDAY IS THE RUN THAT MATTERS; Wednesday and Thursday are retries. Since
# 2026-08-28 the sleeve sells from its OWN reserve every week regardless of what
# the share book holds (the old "gate 1" is gone), so on a normal week the
# Friday run does everything: sells last week's assigned shares if any, closes
# the expiring put for pennies, sells the new one. If that run fails — a
# holiday Friday, a broker outage — the next Wednesday/Thursday attempt sells a
# slightly shorter contract for the same expiry instead of losing the week.
#
# It is safe because all three days select the SAME contract, and because a sale
# makes the later attempts no-ops:
#   * dte_min 5 / dte_max 9 spans d+5..d+9, and for Wed, Thu and Fri each of
#     those windows contains exactly ONE Friday — the same one. Verified:
#     2026-09-02, -03 and -04 all select the 2026-09-11 expiry. Monday and
#     Tuesday would need the band widened to 10-11 days, changing the premium
#     profile the study validated, so they are deliberately excluded.
#   * gate 3 refuses when option_positions() OR open_option_orders() is
#     non-empty, so Wednesday's sale makes Thursday and Friday do nothing.
#
# Cron (wrapped by cron/at_et.sh like every other schedule here, so the
# Melbourne->New York DST drift is handled once, in one place; "3-5" is Wed-Fri
# in NEW YORK, which is the weekday at_et.sh evaluates):
#   45 * * * * ./cron/at_et.sh 15:45 3-5 ./agent/run_csp.sh >> agent/csp.log 2>&1
#
# THIS SENDS REAL ORDERS. decisions/csp.json has shadow=false as of 2026-09-01,
# so running this by hand outside the cron will sell a put if the gates allow it
# — and the gates are designed around ONE run a day at 15:45 ET, not around an
# operator poking it. Use `--dry-run` to inspect: it forces shadow on for that
# invocation whatever the file says, so it chooses and journals but never sends.
#   PYTHONPATH=. .venv/bin/python -m relay.csp_run --dry-run
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[$(date -u +%FT%TZ)] cash-secured put check"
PYTHONPATH=. .venv/bin/python -m relay.csp_run "$@"
echo "[$(date -u +%FT%TZ)] done"

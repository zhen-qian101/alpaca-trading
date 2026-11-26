"""Every stop must fire well INSIDE the account-level circuit breaker.

THE DEFECT THIS PINS, which lived in the config for weeks: SOXL and TQQQ carried
a 6% position stop while the book is all-in/all-out at deploy 0.97. A 6% loss on
97% of equity is 5.82% of the ACCOUNT — larger than the 5% daily_loss_limit it
sits under. So one ordinary stop-out on either symbol tripped the account
breaker and halted every new buy for the rest of the day: the per-trade control
firing OUTSIDE the account control it is supposed to sit inside.

That is a laddering bug, not a tuning preference, and no backtest is needed to
see it. This file is the arithmetic, kept next to the config so it cannot come
back silently — including via a future deploy increase, which multiplies every
stop at once.

    .venv/bin/pytest -q tests/test_stop_ladder.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pytest  # noqa: E402

from relay.risk_filter import HARD_DAILY_LOSS_LIMIT  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent
                  / "sigengine" / "config.json").read_text())
SYMBOLS = {s: v for s, v in CFG["symbols"].items() if v.get("enabled")}

# How much of the daily breaker a SINGLE stop-out may consume. Two-thirds leaves
# room for a second losing trade in one day before the book is halted; at 1.0 a
# single stop would arm the halt, which is the failure this file exists for.
MAX_SHARE_OF_BREAKER = 0.67


@pytest.mark.parametrize("sym", sorted(SYMBOLS))
def test_one_stop_out_cannot_trip_the_daily_halt(sym):
    v = SYMBOLS[sym]
    account_loss = v["stop"] * v["deploy"]

    assert account_loss < HARD_DAILY_LOSS_LIMIT, (
        f"{sym}: a {v['stop']:.0%} stop on a {v['deploy']:.0%} deploy is "
        f"{account_loss:.2%} of equity, which ALONE trips the "
        f"{HARD_DAILY_LOSS_LIMIT:.0%} daily halt and blocks every buy for the "
        f"rest of the day")
    assert account_loss <= HARD_DAILY_LOSS_LIMIT * MAX_SHARE_OF_BREAKER, (
        f"{sym}: one stop-out is {account_loss / HARD_DAILY_LOSS_LIMIT:.0%} of the "
        f"daily breaker — too close to arming it on a single trade")


def test_the_leveraged_pair_is_not_back_on_six_percent():
    """Named explicitly because this is the value that was wrong, and because a
    'restore the 3x carve-out' change would look reasonable in review."""
    # SOXL is retired from the share strategy (the put sleeve owns it), so read
    # the raw entry rather than the enabled set — the number must stay right for
    # the day it is switched back on.
    for sym in ("SOXL", "TQQQ"):
        assert CFG["symbols"][sym]["stop"] <= 0.03, (
            f"{sym} is back above 3% — see the _stop_note in sigengine/config.json")


def test_assigned_shares_have_no_price_stop_at_all():
    """Operator's choice 2026-08-28, from the wheel backtest: every price stop on
    assigned shares tested worse than "sold within a week", because a stop set
    where a trading stop goes is already breached the day the shares arrive in
    half of all assignments. What remains is a CALENDAR backstop for the weekly
    sale itself failing — long enough to outlive one missed Friday, short enough
    that a seven-day position cannot become a seventy-day one."""
    import sigengine.engine as eng

    assert not hasattr(eng, "_ADOPTED_STOP_PCT"), "no price stop exists"
    assert 7 < eng._ADOPTED_MAX_DAYS <= 14


def test_one_assignment_held_for_a_week_fits_inside_the_breaker():
    """One contract at today's ~$120 strike is $12,000 of SOXL for at most one
    week. The p99 one-week move against it (10.8% of collateral, measured over
    802 weeks) is $1,300 — 1.3% of a $100,000 account, well inside the 5% halt.
    The worst week in the sample (45%) would consume it, which is the honest
    cost of running this sleeve at all and why it is ONE contract."""
    p99 = 100 * 120.0 * 0.108
    assert p99 / 100_000 < HARD_DAILY_LOSS_LIMIT * 0.5

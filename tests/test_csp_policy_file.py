"""The SHIPPED decisions/csp.json is a live trading control — test it as one.

Every other csp test builds its own policy dict, so nothing checked the file
that actually decides whether real orders go out. That file is hand-edited, and
`config()`'s docstring records the exact hazard: read by truthiness, a
`"shadow": null` or a `"shadow": "false"` ARMS live orders. The coercions are
safe by default in both directions — but a typo in a KEY is silent, because an
unknown key is simply ignored and the default quietly stands in for the value
the operator thought they set.

These tests deliberately do NOT assert that the strategy is on. Whether to trade
is the operator's call, it changes without a deploy, and a test that pinned it
would fail the moment someone correctly disables the sleeve. What is pinned is
that the file MEANS WHAT IT LOOKS LIKE IT MEANS.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from relay.csp_manager import CSP_CONFIG_FILE, DEFAULTS, config, load_policy


@pytest.fixture(scope="module")
def raw() -> dict:
    """The policy file exactly as it sits on disk — flat, not wrapped."""
    return json.loads(CSP_CONFIG_FILE.read_text())


def test_the_policy_file_exists_and_is_json():
    assert CSP_CONFIG_FILE.exists(), f"{CSP_CONFIG_FILE} is missing"
    assert isinstance(json.loads(CSP_CONFIG_FILE.read_text()), dict)


@pytest.mark.parametrize("key", ["enabled", "shadow"])
def test_the_two_safety_flags_are_real_json_booleans(raw, key):
    # The whole point: "false", "true", null, 0 and 1 all LOOK like booleans in
    # a hand-edited file, and none of them behave like one here.
    assert key in raw, f"{key} is missing from {CSP_CONFIG_FILE.name}"
    assert isinstance(raw[key], bool), (
        f"{key} is {raw[key]!r} ({type(raw[key]).__name__}), not a JSON boolean — "
        f"see config()'s docstring: a non-boolean here is a silent arm or disarm")


def test_no_unknown_keys(raw):
    # A typo'd key is not an error, it is a DEFAULT standing in silently:
    # "max_contract": 3 leaves max_contracts at 1 and says nothing.
    unknown = {k for k in raw if not k.startswith("_")} - set(DEFAULTS)
    assert not unknown, (
        f"unknown key(s) {sorted(unknown)} — these are ignored, so the default is "
        f"what actually runs. Known keys: {sorted(DEFAULTS)}")


def test_the_file_is_what_the_strategy_actually_sees(raw):
    # load_policy() -> config() is the exact path csp_run takes. If these ever
    # disagree, the log and the orders describe two different strategies — which
    # is precisely the bug load_policy() was extracted to kill.
    cfg = config(load_policy())
    for key in ("enabled", "shadow", "underlying", "contracts"):
        assert cfg[key] == raw.get(key, DEFAULTS[key]), (
            f"{key}: file says {raw.get(key)!r}, strategy sees {cfg[key]!r}")


def test_the_rails_are_coherent():
    cfg = config(load_policy())
    assert 0 < cfg["min_otm"] < cfg["max_otm"] < 1, "OTM band is inverted or absurd"
    assert 0 < cfg["dte_min"] <= cfg["dte_max"], "DTE window is inverted"
    assert cfg["max_contracts"] >= 1, "max_contracts below 1 can never trade"
    assert 0 < cfg["reserve_frac"] <= 1, "reserve_frac is a fraction of equity"
    assert 0 <= cfg["min_credit_frac"] < 1
    assert 0 < cfg["max_spread_frac"] <= 1
    assert 0 <= cfg["close_reserve"] < 1
    assert cfg["contracts"] == "auto" or (
        isinstance(cfg["contracts"], int) and cfg["contracts"] >= 1), (
        "a pinned contract count below 1 is refused outright by run()")


def test_the_dte_window_still_picks_one_friday_from_wednesday_thursday_and_friday():
    """run_csp.sh's safety argument depends on this, so pin it to the FILE.

    The cron fires Wed/Thu/Fri and relies on all three days selecting the SAME
    contract, so that a sale on the first of them makes the rest no-ops via
    gate 3. If a run could see two candidate expiries, a "retry" could open a
    SECOND put on a different week instead of doing nothing.

    The dangerous edits are NOT about how wide the band is — 5-9, 5-11, 4-11 and
    5-13 are all fine, because the reachable Fridays stay one. Measured, the two
    ways to break it are:

      * dte_min <= 2 — Wednesday starts reaching THIS week's Friday as well as
        next week's (5-9 -> 2-9 breaks on every single week);
      * dte_max >= 14 — Friday starts reaching the Friday after next (5-13 is
        the last safe upper bound).

    So the band may be retuned for premium, but not through either edge.

    This pins the FRIDAYS specifically, not "how many expiries are reachable",
    because SOXL lists Monday, Wednesday and Friday weeklies and next_expiry
    resolves that by PREFERRING the Friday (falling back to the window's last
    expiry only when a holiday week lists none). From Wed/Thu/Fri the window
    always contains a Wednesday expiry too; it is the Friday preference that
    makes all three days agree.

    Measured live on Mon 2026-08-31 ET, which is why Mon/Tue are excluded: a
    Monday reaches no Friday at all (Sep 4 is +4, Sep 11 is +11), so the
    fallback correctly took Wed 2026-09-09 — and that midweek contract was
    untradeable, every in-band strike quoting 23-196% wide against a 25% limit.
    The Friday it should have had, 2026-09-11, quoted K85 at 15.4% in the same
    minute. The weekday guard is not tidiness; the midweek book is not there.
    """
    cfg = config(load_policy())
    lo, hi = cfg["dte_min"], cfg["dte_max"]
    start = date(2026, 9, 2)                       # a Wednesday
    for week in range(60):                         # a full year of weeks
        chosen = set()
        for offset in range(3):                    # Wed, Thu, Fri
            d = start + timedelta(days=7 * week + offset)
            fridays = {d + timedelta(days=n) for n in range(lo, hi + 1)
                       if (d + timedelta(days=n)).weekday() == 4}
            assert len(fridays) == 1, (
                f"{d:%Y-%m-%d (%a)} sees {len(fridays)} Fridays in a {lo}-{hi} "
                f"day DTE window: {sorted(fridays)}")
            chosen |= fridays
        assert len(chosen) == 1, (
            f"week of {start + timedelta(days=7 * week)}: Wed/Thu/Fri disagree on "
            f"the expiry {sorted(chosen)} — a retry would open a SECOND put")

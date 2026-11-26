"""Five repairs to the data guards and the engine's own memory (audit 2026-08-27).

Each one is a case where the engine believed it was protected and was not. They
share a shape: a guard that reads correctly in its docstring but, in the code,
looked at the wrong window, the wrong clock, the wrong file, or the wrong
question — and failed SILENTLY, which is why none of them showed up as an
incident until the journal was read line by line.

  E3   the outlier screen skipped the newest TEN bars, not the newest one.
       _events_from_cached drops exactly ONE still-forming bar, so bars 1..9
       back from the live edge were cached and traded on unscreened — a ten-hour
       unjudged window on 1h bars, and exactly the window a fabricated entry
       fires in. MSFT 2026-08-19: junk at 20:00, the buy on the 21:00 bar.
  DG-3 the screen only ever filtered the INCOMING frame. upsert_bars has no
       DELETE path, so a junk bar already cached stayed cached forever and the
       strategies replayed it every poll. delete_bars_at is the missing verb.
  DG-4 "the same weekday and hour" was measured in UTC. A session hour is a New
       York wall-clock fact; in UTC it moves an hour twice a year, and the
       acceptance window is only half a bar — so for the ~2 weeks after a DST
       changeover the gap guard answered its own question wrongly.
  S5   losing state.json was completely silent. Every symbol re-baselines and a
       warm-up fires NOTHING, so every signal formed since the last poll is
       dropped — and the daemon polls with quiet=True, so not even the per-symbol
       warm-up line was printed. The data dir is on a removable disk.
  S2   the reconciler's "did I open this?" test could only ever answer yes. It
       asked the SIMULATED book, which replays the entire cached history, so the
       branch protecting a holding the engine did not open was unreachable and
       the safety net would auto-liquidate an operator's own position.

    .venv/bin/pytest -q tests/test_guard_and_state_fixes.py
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import logging  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from relay import bars_db  # noqa: E402
from sigengine import engine as eng  # noqa: E402

STEP = pd.Timedelta("1h")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Nothing here may reach the live journal, the live state file or the live
    bar cache — all three live on /mnt/sandisk and all three are written by the
    running daemon. Also start each test with empty report-dedup sets, so one
    test's findings cannot silence another's."""
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(eng, "_OUTLIER_REPORTED", set())
    monkeypatch.setattr(eng, "_GAP_REPORTED", set())
    monkeypatch.setattr(bars_db, "BARS_DB_FILE", tmp_path / "bars.sqlite")
    monkeypatch.setattr(bars_db, "_CONN", None)
    eng._PENDING_EXIT_SEEN.clear()
    yield
    bars_db.close()


def _journal():
    if not eng.JOURNAL_FILE.exists():
        return []
    return [json.loads(x) for x in eng.JOURNAL_FILE.read_text().splitlines() if x.strip()]


# =============================================================== E3: the edge ===
#
# The screen needs an AFTER side to judge a bar, and `_OUTLIER_NEAR` (2) bars of
# it is what forms the plausible band. `_OUTLIER_LOCAL` (10) only sets the
# volatility scale and the before/after levels, all of which tolerate a
# truncated window — so the loop bound moved from `loc` to `near` and the local
# windows are clipped at the edges instead of the whole bar being skipped.


def _spiked(n: int, spike_at: int):
    """A flat 484 series with one bad print at `spike_at` — the real MSFT
    numbers (H 503.58 / L 450.97 / C 474.47 against a true 484.x)."""
    close = np.full(n, 484.0)
    high = np.full(n, 484.5)
    low = np.full(n, 483.5)
    close[spike_at], high[spike_at], low[spike_at] = 474.47, 503.58, 450.97
    idx = pd.date_range("2026-08-19 00:00", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": close, "high": high, "low": low,
                         "close": close, "volume": np.full(n, 1000.0)}, index=idx)


@pytest.mark.parametrize("offset", [2, 3, 4, 5, 6, 7, 8, 9])
def test_junk_just_behind_the_live_edge_is_now_rejected(offset):
    """THE E3 regression, at every offset it used to cover.

    The loop ran `range(loc, n - loc)` with loc=10, so a bad print 2 to 9 rows
    from the newest was never examined — it went straight into the cache and the
    strategies traded on it. That is not a rare corner: at 1h bars it is the
    whole of the last ten hours, and a fabricated crossing fires within one or
    two bars of the junk that caused it. Every one of these offsets is now
    judged, and the newest bars are still deferred (next test)."""
    n = 30
    df = _spiked(n, n - 1 - offset)
    bad_ts = df.index[n - 1 - offset]

    clean, dropped = eng._reject_outlier_bars("MSFT", df)

    assert dropped == [bad_ts], f"a bad print {offset} rows from the edge must be caught"
    assert bad_ts not in clean.index


@pytest.mark.parametrize("offset", [0, 1])
def test_the_two_newest_bars_are_still_deferred(offset):
    """The half of the old rule that was RIGHT, and must survive the widening.

    A bar needs `_OUTLIER_NEAR` bars of after-side before anything has confirmed
    or refuted it; the newest two have less than that, so judging them would be
    guessing. They are judged on the next poll instead, and no signal ever
    depends on the very last one — _events_from_cached drops the still-forming
    bar before the strategies see it."""
    n = 30
    df = _spiked(n, n - 1 - offset)

    _clean, dropped = eng._reject_outlier_bars("MSFT", df)

    assert dropped == [], "a bar with no after-side must not be condemned on a guess"


def test_the_same_spike_is_caught_once_two_more_bars_arrive():
    """The other half of the previous test: the deferral is a WAIT, not a pardon.
    Two more bars of tape and the offset-1 print above becomes offset-3, and the
    screen takes it."""
    df = _spiked(32, 30)                     # offset 1 in a 32-bar frame...
    assert eng._reject_outlier_bars("MSFT", df)[1] == []

    grown = _spiked(34, 30)                  # ...offset 3 two bars later
    assert eng._reject_outlier_bars("MSFT", grown)[1] == [grown.index[30]]


def test_a_genuine_repricing_near_the_edge_is_not_rejected():
    """The cost of judging the last ten bars is that a real move now gets judged
    there too — so the drift test has to hold up at the edge, on a CLIPPED
    window, not just in the middle of a frame.

    This bar is violent by any measure (a low 55 local ranges under its
    neighbours) but the price does not come back: 484 before, 455 after. That is
    news, and dropping it would delete the real prices at exactly the moment the
    series mattered most."""
    n = 30
    crash = n - 3                            # offset 2 — inside the new window
    close = np.full(n, 484.0)
    high = np.full(n, 484.5)
    low = np.full(n, 483.5)
    close[crash:], high[crash:], low[crash:] = 455.0, 455.5, 454.5
    high[crash], low[crash] = 484.5, 400.0   # the crash bar's own violent range
    idx = pd.date_range("2026-08-19 00:00", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": close, "high": high, "low": low,
                       "close": close, "volume": np.full(n, 1000.0)}, index=idx)

    clean, dropped = eng._reject_outlier_bars("SPY", df)

    assert dropped == [], "a move that STAYS is news; only a fully-retraced one is junk"
    assert len(clean) == n


def test_widening_the_window_did_not_start_condemning_ordinary_bars():
    """The whole screen is only tolerable because it almost never fires — it was
    measured at 3 rejections in 94,873 real cached bars. Judging ten more bars
    per frame must not change that, so an ordinary noisy series (SOXL moved 14%
    top-to-bottom on the day this guard was written) must still come through
    completely untouched, edge bars included."""
    rng = np.random.default_rng(7)
    closes = 120 + np.cumsum(rng.normal(0, 1.2, 60))
    idx = pd.date_range("2026-08-19 00:00", periods=60, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": closes, "high": closes + 1.5, "low": closes - 1.5,
                       "close": closes, "volume": np.full(60, 1000.0)}, index=idx)

    clean, dropped = eng._reject_outlier_bars("SOXL", df)

    assert dropped == [] and len(clean) == 60


# ================================================== DG-3: purging cached junk ===


def _seed(symbol, interval, idx, source="yfinance"):
    df = pd.DataFrame({"open": np.arange(len(idx), dtype=float),
                       "high": np.arange(len(idx), dtype=float) + 1,
                       "low": np.arange(len(idx), dtype=float) - 1,
                       "close": np.arange(len(idx), dtype=float),
                       "volume": np.full(len(idx), 10.0)}, index=idx)
    bars_db.upsert_bars(symbol, interval, df, source=source)
    return df


def test_delete_bars_at_removes_exactly_the_named_timestamps():
    """THE DG-3 regression. _reject_outlier_bars screened the incoming frame and
    threw the verdict away, and upsert_bars only ever TOUCHES timestamps present
    in an incoming frame — it has no DELETE path. So a junk bar that had already
    been cached on an earlier poll could never be retired: the strategies
    replayed it every poll until a higher-ranked source happened to overwrite
    that exact hour, which during a yfinance-only outage never happens.

    Surgical is the point. delete_bars() would drop the whole symbol and force a
    full refetch; what the guard condemns is two or three individual prints."""
    idx = pd.date_range("2026-08-19 00:00", periods=5, freq="1h", tz="UTC")
    _seed("MSFT", "1h", idx)

    removed = bars_db.delete_bars_at("MSFT", "1h", [idx[1], idx[3]])

    assert removed == 2
    assert list(bars_db.read_bars("MSFT", "1h").index) == [idx[0], idx[2], idx[4]]


def test_delete_bars_at_leaves_other_symbols_and_intervals_alone():
    """Timestamps are shared across the whole board — every symbol has a 20:00
    bar. A delete keyed on the timestamp alone would punch a hole in the entire
    cache for one symbol's bad print."""
    idx = pd.date_range("2026-08-19 00:00", periods=4, freq="1h", tz="UTC")
    _seed("MSFT", "1h", idx)
    _seed("AAPL", "1h", idx)
    _seed("MSFT", "30min", idx)

    assert bars_db.delete_bars_at("MSFT", "1h", [idx[0], idx[1]]) == 2

    assert len(bars_db.read_bars("MSFT", "1h")) == 2
    assert len(bars_db.read_bars("AAPL", "1h")) == 4
    assert len(bars_db.read_bars("MSFT", "30min")) == 4


def test_delete_bars_at_is_a_no_op_when_nothing_is_condemned():
    """The overwhelmingly common case — _fetch_and_recompute calls this on every
    poll for every symbol and the guard almost never fires. It must not open a
    connection's worth of work, and above all must not fall through to a
    DELETE with an empty IN () clause."""
    idx = pd.date_range("2026-08-19 00:00", periods=3, freq="1h", tz="UTC")
    _seed("MSFT", "1h", idx)

    assert bars_db.delete_bars_at("MSFT", "1h", []) == 0
    assert len(bars_db.read_bars("MSFT", "1h")) == 3


def test_deleting_a_timestamp_that_is_not_cached_removes_nothing():
    """Safe to be wrong in this direction: the condemned timestamps always lie
    inside the top-up window just fetched, so the next poll re-offers them. A
    bar that is not there costs nothing; a clean one simply comes straight back."""
    idx = pd.date_range("2026-08-19 00:00", periods=3, freq="1h", tz="UTC")
    _seed("MSFT", "1h", idx)

    absent = pd.Timestamp("2030-01-01 00:00", tz="UTC")
    assert bars_db.delete_bars_at("MSFT", "1h", [absent]) == 0
    assert len(bars_db.read_bars("MSFT", "1h")) == 3


def test_delete_bars_at_accepts_a_naive_timestamp():
    """Callers hand it whatever their frame's index holds, and a hand-built or
    vendor-supplied frame is not always tz-aware. Everything in this cache is
    UTC epoch-seconds, so a naive value must be read as UTC — the same coercion
    upsert_bars uses to write it in the first place."""
    idx = pd.date_range("2026-08-19 00:00", periods=3, freq="1h", tz="UTC")
    _seed("MSFT", "1h", idx)

    assert bars_db.delete_bars_at("MSFT", "1h", [pd.Timestamp("2026-08-19 01:00")]) == 1
    assert list(bars_db.read_bars("MSFT", "1h").index) == [idx[0], idx[2]]


# ============================================== DG-4: probing in session time ===


def _et_index(start, end, hours):
    """Weekday bars at the given EXCHANGE-LOCAL hours, returned in UTC — the
    shape the bar cache actually holds. Building it in ET is the point: a
    session hour does not move when the clocks do, but its UTC label does."""
    days = []
    for d in pd.date_range(start, end, freq="D"):
        if d.weekday() >= 5:
            continue
        base = pd.Timestamp(d.date())
        days.append(pd.DatetimeIndex([base + pd.Timedelta(hours=h) for h in hours])
                    .tz_localize("America/New_York"))
    return days[0].append(days[1:]).tz_convert("UTC")


def _rth_index():
    """An RTH-only symbol either side of the 2026-11-01 US DST changeover. Its
    16:00 ET close is 20:00 UTC in October and 21:00 UTC in November."""
    return _et_index("2026-10-05", "2026-11-13", range(10, 16))


@pytest.mark.parametrize("day", ["2026-11-04", "2026-11-11"])
def test_a_session_slot_stays_normal_across_the_dst_changeover(day):
    """THE DG-4 regression. The probe reaches back one, two and three weeks and
    asks whether the SAME session slot was populated — but it subtracted seven
    days in UTC, where a session hour shifts by 60 minutes across a changeover,
    while the acceptance window is only step/2 = 30 minutes on 1h bars. So every
    probe that crossed 2026-11-01 landed an hour off and found nothing, the slot
    read as abnormal, and for the ~2 weeks it takes all three probes to clear the
    boundary the gap guard suppressed entries on perfectly ordinary sessions.

    Converting to ET and dropping the offset makes the subtraction a WALL-CLOCK
    one, so the probe lands on the same session hour by construction."""
    idx = _rth_index()
    missing = pd.Timestamp(f"{day} 15:00", tz="America/New_York")
    idx = idx[idx != missing]                # a genuine hole at that slot

    assert eng._slot_is_normally_populated(idx, missing, STEP) is True


@pytest.mark.parametrize("day", ["2026-10-21", "2026-10-28"])
def test_a_session_slot_before_the_changeover_is_unaffected(day):
    """The other side of the boundary, where UTC and ET arithmetic agree — this
    is the case that always worked, and the fix must not have moved it."""
    idx = _rth_index()
    missing = pd.Timestamp(f"{day} 15:00", tz="America/New_York")
    idx = idx[idx != missing]

    assert eng._slot_is_normally_populated(idx, missing, STEP) is True


def test_utc_clock_arithmetic_gets_the_same_slot_wrong():
    """Pins the actual defect rather than the symptom. Handed the identical data
    with the timezone stripped — which is the pre-2026-08-27 arithmetic, and
    still the documented fallback for a naive index — the probe walks UTC hours
    and misses: 2026-11-04 20:00 UTC minus seven days is 20:00 UTC on 10-28,
    which is 16:00 EDT, an hour after that symbol's last bar of the day.

    Note what this rules out: widening the window to +/-1h would paper over it
    and is wrong for a different reason — it leaks across midnight into the
    adjacent trading day, so a Saturday hole starts matching Friday 23:00 and the
    WEEKEND begins to look normally populated, which would suppress every Monday
    entry."""
    idx = _rth_index()
    missing = pd.Timestamp("2026-11-04 15:00", tz="America/New_York")
    idx = idx[idx != missing]

    naive_idx = idx.tz_localize(None)                       # raw UTC labels
    naive_missing = missing.tz_convert("UTC").tz_localize(None)

    assert eng._slot_is_normally_populated(naive_idx, naive_missing, STEP) is False
    assert eng._slot_is_normally_populated(idx, missing, STEP) is True


def test_the_weekend_is_still_not_a_normally_populated_slot():
    """The case the whole guard must never get wrong, re-asserted against the new
    clock. Equities gap every weekend; if a weekend slot ever read as "normally
    populated" the gap guard would suppress every Monday-morning entry, forever.
    (This is the direct-to-the-predicate form of
    test_the_weekend_does_not_block_trading in test_bar_sanity_guards.py.)"""
    idx = _et_index("2026-07-06", "2026-08-21", range(0, 24))
    saturday = pd.Timestamp("2026-08-15 12:00", tz="America/New_York")

    assert eng._slot_is_normally_populated(idx, saturday, STEP) is False


def test_an_rth_only_symbol_has_no_normal_overnight_slot():
    """If a slot was never populated in prior weeks, its absence is the symbol's
    normal shape, not damage — a name that only trades 09:30-16:00 must not have
    its every-morning first bar treated as a data hole. (The predicate behind
    test_a_symbol_that_never_trades_overnight_is_not_blocked.)"""
    idx = _et_index("2026-07-06", "2026-08-21", range(10, 16))
    overnight = pd.Timestamp("2026-08-20 02:00", tz="America/New_York")

    assert eng._slot_is_normally_populated(idx, overnight, STEP) is False


def test_a_slot_older_than_the_series_has_no_opinion():
    """No probe can reach back before the first cached bar, so there is no
    evidence either way — and 'no evidence' must read as 'not normal', i.e. do
    not claim a gap is routine on a cold cache."""
    idx = _et_index("2026-08-17", "2026-08-21", range(10, 16))
    missing = pd.Timestamp("2026-08-19 15:00", tz="America/New_York")

    assert eng._slot_is_normally_populated(idx, missing, STEP) is False


# ================================================ S5: losing the state cursor ===


def test_a_missing_state_file_is_announced(caplog):
    """Losing state.json is not cosmetic: every symbol re-baselines, a warm-up
    deliberately fires NOTHING, and so every signal that formed since the last
    poll is dropped. It used to happen in total silence — the daemon calls
    _diff_and_fire with quiet=True, so not even the per-symbol warm-up line was
    printed. The trigger is mundane: the data dir is on a removable disk, so a
    supervisor restart while it is unmounted loses the session's pending signals.

    A missing file IS normal on a genuinely first run, so this is a warning
    rather than an error, and it is not journaled — see the corrupt case."""
    with caplog.at_level(logging.WARNING, logger="sigengine.engine"):
        assert eng.load_state() == {}

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a lost state cursor must not be silent"
    assert "state" in warnings[0].getMessage().lower()
    assert not [e for e in _journal() if e["event"] == "state_lost"]


def test_a_corrupt_state_file_is_journaled_as_state_lost(caplog):
    """Corruption is the case that is never normal — the file is written with a
    temp-file + os.replace so a torn write means the disk or the mount is in
    trouble. That earns an ERROR *and* a journal row, because the journal is what
    gets read after the fact when a day's signals turn out to be missing."""
    eng.STATE_FILE.write_text('{"MSFT": "2026-08-19T20:00:00+00:00"')   # truncated

    with caplog.at_level(logging.ERROR, logger="sigengine.engine"):
        assert eng.load_state() == {}

    lost = [e for e in _journal() if e["event"] == "state_lost"]
    assert len(lost) == 1
    assert lost[0]["path"] == str(eng.STATE_FILE)
    assert lost[0]["reason"]                     # the decoder's own complaint
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_readable_state_file_is_returned_untouched():
    """The path that must stay boring — the new error handling only wraps the
    failures."""
    eng.STATE_FILE.write_text(json.dumps({"MSFT": "2026-08-19T20:00:00+00:00"}))
    assert eng.load_state() == {"MSFT": "2026-08-19T20:00:00+00:00"}


# =================================== S2: did the ENGINE actually open this? ===


def _ev(action, reason, ts="2026-07-15 11:00:00+00:00", price=660.9):
    return {"ts": pd.Timestamp(ts), "action": action, "reason": reason,
            "price": price, "strategy": "macd_sma"}


def _pos(sym, qty):
    return type("Pos", (), {"symbol": sym, "qty": str(qty)})()


class FakeClient:
    def __init__(self, positions=None):
        self.positions = positions or []

    def get_all_positions(self):
        return list(self.positions)

    def get_open_orders(self, symbol=None):
        return []

    def cancel_order(self, order_id):  # pragma: no cover - not reached here
        raise AssertionError("the reconciler must not cancel anything here")


class FakeOM:
    def __init__(self):
        self.handled: list = []

    def handle(self, payload):
        self.handled.append((payload.ticker, payload.action, payload.strategy))
        return {"status": "closed"}


def _fetched(**per_symbol):
    return {sym: (evs, pd.Timestamp("2026-07-15 12:00:00+00:00"), "ibkr")
            for sym, evs in per_symbol.items()}


def _aux(opened=None, pending=None):
    return {eng._OPENED_KEY: dict(opened or {}), eng._PENDING_KEY: dict(pending or {})}


# A window in which the strategy bought and then exited: sim flat, and the
# SIMULATED book claims it opened the position. The broker still shows 7 shares.
_ROUND_TRIP = [_ev("buy", "macd_sma_bullish", ts="2026-07-14 08:00:00+00:00"),
               _ev("close", "stop", ts="2026-07-15 11:00:00+00:00")]


def test_the_simulated_book_always_claims_it_opened_the_position():
    """The premise of the whole fix, asserted so it cannot rot. sim_bought was
    meant to mean "the engine opened this holding", and the reconciler gates its
    only destructive action on it — but the window it replays is the ENTIRE
    cached history, thousands of bars, so a simulated buy is essentially always
    in there. The answer is yes by construction, and the protective branch was
    unreachable."""
    assert eng._simulated_position(_ROUND_TRIP) == (False, True)


def test_an_unrecorded_long_is_alerted_on_instead_of_liquidated():
    """THE S2 regression. The sidecar's `opened` map is EMPTY, i.e. this engine
    never submitted a buy for META — so the 7 shares are somebody else's: an
    operator's manual position, a leftover from a previous config, a fill from an
    install that predates this state. The strategy is flat in its own simulation
    (it always eventually is), and the old code would have read that as a missed
    exit and flattened the holding.

    Reducing exposure is normally the safe direction, which is exactly why this
    one is dangerous: it is the one automatic action the reconciler can take, and
    here it takes it against a position it has no claim on. Alert, do not act."""
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=_ROUND_TRIP),
                             FakeClient([_pos("META", 7)]), om, dry_run=False,
                             aux=_aux(opened={}))

    assert om.handled == [], "never liquidate a position the engine did not open"
    alerts = [e for e in _journal() if e["event"] == "reconcile_alert"]
    assert len(alerts) == 1 and "never opened it" in alerts[0]["reason"]


def test_a_recorded_long_is_still_closed_on_a_missed_exit():
    """The other half, and the reason the sidecar had to exist rather than the
    branch simply being deleted. When `opened` DOES name the symbol, the engine
    really did buy it and the strategy really has exited — a real position riding
    with no stop, never re-emitted, blocking every future entry in the name. That
    is the failure this whole pass was written for and it must still fire."""
    om = FakeOM()
    aux = _aux(opened={"META": "2026-07-14T08:00:00+00:00"})

    eng._reconcile_positions({"META": {}}, _fetched(META=_ROUND_TRIP),
                             FakeClient([_pos("META", 7)]), om, dry_run=False,
                             aux=aux)

    assert om.handled == [("META", "close", "reconcile")]
    closed = [e for e in _journal() if e["event"] == "reconcile_close"]
    assert len(closed) == 1 and closed[0]["qty"] == 7.0


def test_aux_none_keeps_the_old_simulated_answer():
    """`aux=None` is the debug/standalone caller and the older tests, and it must
    behave EXACTLY as before the change — the sidecar makes the reconciler more
    cautious only where it has real information, and its absence must not quietly
    alter what a caller that never heard of it does."""
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=_ROUND_TRIP),
                             FakeClient([_pos("META", 7)]), om, dry_run=False,
                             aux=None)

    assert om.handled == [("META", "close", "reconcile")]


def test_going_flat_at_the_broker_releases_the_ownership_record():
    """The record has to be retired or it becomes a lie that grows: a symbol
    opened once and closed by hand would stay in `opened` forever, and the very
    next divergence — a position the engine again did not open — would be
    liquidated on the strength of a stale claim."""
    om = FakeOM()
    aux = _aux(opened={"META": "2026-07-14T08:00:00+00:00"})

    eng._reconcile_positions({"META": {}}, _fetched(META=_ROUND_TRIP),
                             FakeClient([]), om, dry_run=False, aux=aux)

    assert om.handled == []
    assert "META" not in aux[eng._OPENED_KEY]


def test_an_unrecorded_short_is_still_never_covered(caplog):
    """Ordering check: the short guard runs BEFORE the ownership question, and
    must keep doing so. A short in a long-only book is an anomaly this account
    has actually seen (the 2026-07-08 phantom), and it is reported whoever opened
    it — but never auto-covered, because covering a phantom is how you turn a
    reporting glitch into a real trade."""
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=_ROUND_TRIP),
                             FakeClient([_pos("META", -7)]), om, dry_run=False,
                             aux=_aux(opened={}))

    assert om.handled == []
    alerts = [e for e in _journal() if e["event"] == "reconcile_alert"]
    assert len(alerts) == 1 and "short position" in alerts[0]["reason"]


# --------------------------------------------------------------------------
# Regressions found by the adversarial review of the fixes themselves
# (2026-08-27). Both were introduced BY the S2/EXIT-6 work and would have
# silently disarmed the very safety nets it was meant to repair.
# --------------------------------------------------------------------------
class _OrderClient(FakeClient):
    """A broker that also reports working orders, which is what distinguishes
    'the entry never filled' from 'the position is gone'."""

    def __init__(self, positions=None, open_orders=None):
        super().__init__(positions)
        self._open = open_orders or []

    def get_open_orders(self, symbol=None):
        return list(self._open)


def _order(sym, side="buy"):
    return type("Ord", (), {"symbol": sym, "side": side, "id": "o-1"})()


def test_an_unfilled_entry_keeps_its_ownership_record():
    """THE regression. `opened` is written when a buy is SUBMITTED, but outside
    regular hours that order is a resting limit — so the broker still reports
    qty 0 for hours. Retiring the record on 'flat' alone therefore erased it at
    the end of the very poll that wrote it, in the ordinary case. With the
    record gone, sim_bought is False and _reconcile_positions ALERTS instead of
    closing, so the missed-exit backstop stands down for as long as the
    divergence lasts — the exact failure S2 was meant to end.
    """
    aux = _aux(opened={"META": "2026-07-14T08:00:00+00:00"})

    eng._reconcile_positions({"META": {}}, _fetched(META=_ROUND_TRIP),
                             _OrderClient([], [_order("META")]), FakeOM(),
                             dry_run=False, aux=aux)

    assert "META" in aux[eng._OPENED_KEY], \
        "an entry that has not filled yet must keep its ownership record"


def test_nothing_is_retired_on_a_poll_where_the_broker_cannot_be_asked():
    """If the working-order fetch fails we do not know which case we are in, and
    the costly mistake is erasing a live claim. Not knowing means not retiring."""
    class _Broken(FakeClient):
        def get_open_orders(self, symbol=None):
            raise RuntimeError("alpaca 500")

    aux = _aux(opened={"META": "2026-07-14T08:00:00+00:00"})

    eng._reconcile_positions({"META": {}}, _fetched(META=_ROUND_TRIP),
                             _Broken([]), FakeOM(), dry_run=False, aux=aux)

    assert "META" in aux[eng._OPENED_KEY]


def test_a_junk_bar_never_deletes_a_better_cached_bar(tmp_path, monkeypatch):
    """The purge added for DG-3 runs on ONE vendor's frame, and the engine only
    falls back to yfinance when IBKR is down — precisely when the cache is most
    likely to hold a better IBKR bar at the same timestamp. Deleting
    unconditionally would let a junk yfinance print destroy it, with nothing
    able to bring it back while IBKR stays down.
    """
    from relay import bars_db
    monkeypatch.setattr(bars_db, "BARS_DB_FILE", tmp_path / "bars.sqlite")
    monkeypatch.setattr(bars_db, "_CONN", None)
    monkeypatch.setattr(bars_db, "ensure_data_dirs", lambda: None)

    idx = pd.date_range("2026-08-19 20:00", periods=2, freq="1h", tz="UTC")
    frame = pd.DataFrame({"open": [484.0, 484.0], "high": [485.0, 485.0],
                          "low": [483.0, 483.0], "close": [484.0, 484.0],
                          "volume": [1e6, 1e6]}, index=idx)
    bars_db.upsert_bars("MSFT", "1h", frame, "ibkr")

    removed = bars_db.delete_bars_at("MSFT", "1h", [idx[0]], source="yfinance")
    assert removed == 0, "yfinance must not be able to delete an IBKR bar"
    assert len(bars_db.read_bars("MSFT", "1h")) == 2

    removed = bars_db.delete_bars_at("MSFT", "1h", [idx[0]], source="ibkr")
    assert removed == 1, "the same source may retire its own bad print"
    assert len(bars_db.read_bars("MSFT", "1h")) == 1

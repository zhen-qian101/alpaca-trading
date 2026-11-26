"""Two guards against trading on damaged data, both from the same incident.

On 2026-08-19 MSFT lost $30.51 on a round trip in which NEITHER side was a
market signal. IBKR was down and the engine was on the yfinance fallback, and
the two sides broke in different ways:

  ENTRY  one corrupt bar. 2026-08-19 20:00 UTC came back H 503.58 / L 450.97 /
         C 474.47 against a true 485.29 / 483.34 / 484.26. The fake plunge put
         price under the lower Bollinger band with RSI at 24; the next bar's
         "recovery" — the price simply being where it had always been —
         completed a mean-reversion buy.
  EXIT   the data hole. With no overnight bars the series jumped 08-19 23:00 ->
         08-20 08:00 UTC, and nine hours of ordinary drift was read as a
         single-bar crossunder because indicators cross on bar ADJACENCY, not on
         elapsed time.

The same junk also reached the ORDER path as a 504.45 quote, where
MAX_QUOTE_DEVIATION rejected it instantly. These tests are the bar-side
equivalent that did not exist.

Both guards are shaped so that being wrong is cheap: rejecting a bar and
suppressing a signal both make the engine do LESS, never something new.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sigengine import engine


@pytest.fixture(autouse=True)
def _quiet_journal(tmp_path, monkeypatch):
    """Never write the live trades.jsonl from a test, and start each test with
    empty report-dedup sets so one test's findings cannot silence another's."""
    monkeypatch.setattr(engine, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(engine, "_OUTLIER_REPORTED", set())
    monkeypatch.setattr(engine, "_GAP_REPORTED", set())


def _frame(closes, highs=None, lows=None, start="2026-08-19 00:00", freq="1h"):
    idx = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 0.5 if highs is None else np.asarray(highs, float),
         "low": c - 0.5 if lows is None else np.asarray(lows, float),
         "close": c, "volume": np.full(len(c), 1000.0)}, index=idx)


# ------------------------------------------------------- guard 1: bad bars ---
def test_the_real_msft_junk_bar_is_rejected():
    """THE bar that cost $30.51, with its real numbers."""
    closes = [484.2] * 12 + [474.47] + [484.3] * 12
    highs = [484.7] * 12 + [503.58] + [484.8] * 12
    lows = [483.7] * 12 + [450.97] + [483.8] * 12
    df = _frame(closes, highs, lows)
    bad_ts = df.index[12]

    clean, dropped = engine._reject_outlier_bars("MSFT", df)

    assert dropped == [bad_ts]
    assert bad_ts not in clean.index
    assert len(clean) == len(df) - 1


def test_an_ordinary_volatile_symbol_is_left_alone():
    """SOXL moved 14% top-to-bottom on the same day. A percent threshold tight
    enough to catch MSFT's junk would shred it, which is why the test is scaled
    by the symbol's OWN range."""
    rng = np.random.default_rng(7)
    closes = 120 + np.cumsum(rng.normal(0, 1.2, 60))
    df = _frame(closes, closes + 1.5, closes - 1.5)

    clean, dropped = engine._reject_outlier_bars("SOXL", df)

    assert dropped == []
    assert len(clean) == len(df)


def test_a_real_sustained_move_is_not_junk():
    """A genuine repricing — a gap that STAYS — must survive. Only a spike that
    no neighbour confirms is a bad print."""
    closes = [484.0] * 15 + [455.0] * 15          # crashes and stays down
    df = _frame(closes)

    clean, dropped = engine._reject_outlier_bars("MSFT", df)

    assert dropped == [], "a sustained repricing is news, not a bad print"


def test_a_flat_series_cannot_reject_everything():
    """A barely-traded or halted name has a median range of ~0. Without the
    floor the tolerance collapses to zero and every bar looks like an outlier."""
    df = _frame([100.0] * 30, [100.0] * 30, [100.0] * 30)

    clean, dropped = engine._reject_outlier_bars("QUIET", df)

    assert dropped == [] and len(clean) == 30


def test_a_short_frame_is_never_judged():
    """On a cold cache there is no neighbourhood to compare against, and a guess
    would be worse than nothing."""
    df = _frame([484.0, 474.0, 484.0])
    clean, dropped = engine._reject_outlier_bars("MSFT", df)
    assert dropped == [] and len(clean) == 3


def test_the_newest_bar_is_not_judged_yet():
    """A spike at the very end has no after-side, so nothing has confirmed or
    refuted it. Judging it would mean guessing; it is judged next poll instead,
    and signals never depend on it because _events_from_cached drops the last,
    still-forming bar."""
    closes = [484.0] * 20 + [474.47]
    highs = [484.5] * 20 + [503.58]
    lows = [483.5] * 20 + [450.97]

    _, dropped = engine._reject_outlier_bars("MSFT", _frame(closes, highs, lows))

    assert dropped == []


def test_the_same_spike_is_rejected_once_confirmed():
    """The other half of the previous test: enough bars arrive to show the price
    never left 484, and now the spike is refutable. The guard needs a full local
    window on BOTH sides before it will judge a bar — that is the price of not
    guessing."""
    closes = [484.0] * 20 + [474.47] + [484.1] * 14
    highs = [484.5] * 20 + [503.58] + [484.6] * 14
    lows = [483.5] * 20 + [450.97] + [483.6] * 14
    df = _frame(closes, highs, lows)

    _, dropped = engine._reject_outlier_bars("MSFT", df)

    assert dropped == [df.index[20]]


def test_rejection_is_journaled():
    closes = [484.2] * 12 + [474.47] + [484.3] * 12
    highs = [484.7] * 12 + [503.58] + [484.8] * 12
    lows = [483.7] * 12 + [450.97] + [483.8] * 12
    engine._reject_outlier_bars("MSFT", _frame(closes, highs, lows))

    rows = [r for r in engine.JOURNAL_FILE.read_text().splitlines() if r.strip()]
    assert any("bar_rejected_outlier" in r for r in rows)


# ----------------------------------------------------------- guard 2: gaps ---
def _cfg():
    return {"interval": "1h"}


def _gapped_index():
    """Three ordinary weeks of 24h bars, then a day whose overnight is missing —
    the exact shape of the MSFT exit."""
    full = pd.date_range("2026-07-29 00:00", "2026-08-19 23:00", freq="1h", tz="UTC")
    after = pd.date_range("2026-08-20 08:00", periods=6, freq="1h", tz="UTC")
    return full.append(after)


def test_an_entry_across_a_data_hole_is_suppressed():
    """THE regression. The bar before this one is nine hours earlier, so the
    'crossover' is the gap, not a price move — and an entry commits new capital
    at a price that never traded."""
    idx = _gapped_index()
    df = pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)
    ev = [{"ts": pd.Timestamp("2026-08-20 08:00", tz="UTC"), "action": "buy",
           "strategy": "bollinger_rsi", "reason": "rsi_bb_long"}]

    assert engine._gap_blocked_events("MSFT", _cfg(), df, ev) == []


def test_an_exit_across_a_data_hole_is_never_suppressed():
    """The other half, and the reason this guard was nearly fatal (audit
    EXIT-1). A stop is a LEVEL test — close <= entry*(1-stop_pct) — so a hole
    cannot corrupt it, and for an equity the first bar of a session ALWAYS has
    a missing predecessor. Suppressing it left a real position with no stop,
    because the strategy's simulated book goes flat at that bar regardless and
    so never re-emits the exit."""
    idx = _gapped_index()
    df = pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)
    for action, reason in (("close", "stop"), ("close", "rsi_bb_short"),
                           ("close", "macd_sma_bearish"), ("sell", "half")):
        ev = [{"ts": pd.Timestamp("2026-08-20 08:00", tz="UTC"), "action": action,
               "strategy": "bollinger_rsi", "reason": reason}]
        assert engine._gap_blocked_events("MSFT", _cfg(), df, ev) == ev, \
            f"an exit ({action}/{reason}) must never be suppressed by a data gap"


def test_the_reconciler_can_see_a_gapped_exit():
    """The compounding half of EXIT-1: _events_from_cached hands this same list
    to _reconcile_positions, so a suppressed close left sim_long True and the
    missed-exit backstop silently stood down. With exits kept, the simulated
    book goes flat and the safety net can act on it."""
    idx = _gapped_index()
    df = pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)
    ev = [{"ts": pd.Timestamp("2026-08-19 12:00", tz="UTC"), "action": "buy",
           "strategy": "bollinger_rsi", "reason": "rsi_bb_long"},
          {"ts": pd.Timestamp("2026-08-20 08:00", tz="UTC"), "action": "close",
           "strategy": "bollinger_rsi", "reason": "stop"}]

    kept = engine._gap_blocked_events("MSFT", _cfg(), df, ev)
    sim_long, _bought = engine._simulated_position(kept)
    assert sim_long is False, "the reconciler must see the strategy go flat"


def test_a_signal_on_consecutive_bars_passes():
    idx = _gapped_index()
    df = pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)
    ev = [{"ts": pd.Timestamp("2026-08-20 10:00", tz="UTC"), "action": "buy",
           "strategy": "bollinger_rsi", "reason": "rsi_bb_long"}]

    assert engine._gap_blocked_events("MSFT", _cfg(), df, ev) == ev


def test_the_weekend_does_not_block_trading():
    """The gap that must NOT trip this: equities gap every weekend, and blocking
    on it would silence the engine every Monday forever."""
    days = []
    for d in pd.date_range("2026-07-06", "2026-08-21", freq="D"):
        if d.weekday() < 5:                        # weekdays only, like the tape
            days.append(pd.date_range(d, periods=24, freq="1h", tz="UTC"))
    idx = days[0].append(days[1:]) if len(days) > 1 else days[0]
    df = pd.DataFrame({"close": np.linspace(100, 110, len(idx))}, index=idx)
    monday = pd.Timestamp("2026-08-17 00:00", tz="UTC")
    ev = [{"ts": monday, "action": "buy", "strategy": "macd_sma",
           "reason": "macd_sma_bullish"}]

    assert engine._gap_blocked_events("X", _cfg(), df, ev) == ev, \
        "a Monday-morning signal must survive the weekend gap"


def test_a_symbol_that_never_trades_overnight_is_not_blocked():
    """If the slot was never populated in prior weeks, its absence is the
    symbol's normal shape, not damage."""
    days = []
    for d in pd.date_range("2026-07-06", "2026-08-21", freq="D"):
        if d.weekday() < 5:
            days.append(pd.date_range(d.replace(hour=14), periods=7,
                                      freq="1h", tz="UTC"))
    idx = days[0].append(days[1:])
    df = pd.DataFrame({"close": np.linspace(100, 110, len(idx))}, index=idx)
    ev = [{"ts": pd.Timestamp("2026-08-20 14:00", tz="UTC"), "action": "buy",
           "strategy": "macd_sma", "reason": "macd_sma_bullish"}]

    assert engine._gap_blocked_events("RTHONLY", _cfg(), df, ev) == ev


def test_suppression_is_journaled():
    idx = _gapped_index()
    df = pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)
    ev = [{"ts": pd.Timestamp("2026-08-20 08:00", tz="UTC"), "action": "buy",
           "strategy": "bollinger_rsi", "reason": "rsi_bb_long"}]
    engine._gap_blocked_events("MSFT", _cfg(), df, ev)

    rows = [r for r in engine.JOURNAL_FILE.read_text().splitlines() if r.strip()]
    assert any("signal_gap_blocked" in r for r in rows)


# --------------------------------------------------- reporting, not spamming ---
def test_a_repeated_finding_is_reported_once_but_acted_on_every_time():
    """Both guards re-examine the whole cached history every poll, so a finding
    is re-found every 300s for as long as the bar is cached — for a 3-year series
    that is forever. The first live poll wrote 33 journal rows, i.e. ~9,500/day,
    all re-statements of the same handful of facts including a 2025 holiday gap.

    The distinction that matters: the SUPPRESSION must be unconditional, the
    REPORT must not be.
    """
    idx = _gapped_index()
    df = pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)
    ev = [{"ts": pd.Timestamp("2026-08-20 08:00", tz="UTC"), "action": "buy",
           "strategy": "bollinger_rsi", "reason": "rsi_bb_long"}]

    for _ in range(5):
        assert engine._gap_blocked_events("MSFT", _cfg(), df, ev) == [], \
            "the signal must stay suppressed on every poll"

    rows = [r for r in engine.JOURNAL_FILE.read_text().splitlines() if r.strip()]
    assert sum("signal_gap_blocked" in r for r in rows) == 1


def test_a_repeated_bad_bar_is_dropped_every_time_but_reported_once():
    closes = [484.0] * 20 + [474.47] + [484.1] * 14
    highs = [484.5] * 20 + [503.58] + [484.6] * 14
    lows = [483.5] * 20 + [450.97] + [483.6] * 14
    df = _frame(closes, highs, lows)

    for _ in range(5):
        clean, dropped = engine._reject_outlier_bars("MSFT", df)
        assert dropped == [df.index[20]], "the bar must stay out of the cache"
        assert df.index[20] not in clean.index

    rows = [r for r in engine.JOURNAL_FILE.read_text().splitlines() if r.strip()]
    assert sum("bar_rejected_outlier" in r for r in rows) == 1


def test_a_second_distinct_finding_is_still_reported():
    """Dedup must be per finding, not a one-shot latch — a NEW bad bar after an
    earlier one must still be announced."""
    def frame(spike_at):
        closes = [484.0] * 35
        highs = [484.5] * 35
        lows = [483.5] * 35
        closes[spike_at], highs[spike_at], lows[spike_at] = 474.47, 503.58, 450.97
        return _frame(closes, highs, lows)

    engine._reject_outlier_bars("MSFT", frame(15))
    engine._reject_outlier_bars("MSFT", frame(20))

    rows = [r for r in engine.JOURNAL_FILE.read_text().splitlines() if r.strip()]
    assert sum("bar_rejected_outlier" in r for r in rows) == 2


def test_no_events_is_not_an_error():
    idx = _gapped_index()
    df = pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)
    assert engine._gap_blocked_events("MSFT", _cfg(), df, []) == []


def test_an_event_not_on_a_bar_is_left_alone():
    """Defensive: an event whose ts is not in the index cannot be judged, and
    dropping it would be a silent behaviour change."""
    idx = _gapped_index()
    df = pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)
    ev = [{"ts": pd.Timestamp("2030-01-01 00:00", tz="UTC"), "action": "buy",
           "strategy": "x", "reason": "y"}]
    assert engine._gap_blocked_events("MSFT", _cfg(), df, ev) == ev

"""Tests for backtest/bollinger_rsi.py (translated from the original Pine
Script, retired with TradingView), covering the crossover/crossunder helpers and
that a dip-then-recovery bar sequence where RSI and the lower Bollinger Band
are crossed on the SAME bar produces exactly the expected buy/close pair.

Note: RSI(6)/BB(200), the strategy's real tuned parameters, rarely cross on
the exact same bar by design (the tuned strategy was validated on 2yr of real
data by a since-retired comparison study) — this test uses small
deliberately-chosen periods (rsi_length=2, bb_length=5) purely to make a
same-bar coincidence constructible in a short, deterministic fixture.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.bollinger_rsi import _crossover, _crossunder, run_with_events


def test_crossover_and_crossunder_basic():
    a = np.array([1.0, 1.0, 2.0, 3.0, 2.0, 1.0])
    b = np.array([1.5, 1.5, 1.5, 1.5, 1.5, 1.5])
    assert list(_crossover(a, b)) == [False, False, True, False, False, False]
    assert list(_crossunder(a, b)) == [False, False, False, False, False, True]


def _dip_recovery_df():
    base = [100.0] * 20
    dip = [100 - d for d in (2, 4, 6, 8, 10)]
    recover = [88 + 1.75 * x for x in (1, 2, 4, 7, 11, 16, 22)]
    close = np.array(base + dip + recover + [108.0] * 10)
    idx = pd.date_range("2024-01-01", periods=len(close), freq="5min", tz="UTC")
    return pd.DataFrame({"high": close + 0.1, "low": close - 0.1, "close": close}, index=idx)


def test_buy_fires_when_rsi_and_band_cross_same_bar_then_exits_on_crossunder():
    df = _dip_recovery_df()
    stats, _eq, events = run_with_events(df, 2, 5, 1.0, deploy=0.97, stop_pct=0.5)
    assert stats["n_trades"] == 1
    assert [e["action"] for e in events] == ["buy", "close"]
    assert events[0]["reason"] == "rsi_bb_long"
    assert events[1]["reason"] == "rsi_bb_short"


def test_no_signal_when_only_one_condition_is_true():
    """A pure price dip/recovery with RSI never crossing 50 on the same bar
    (short rsi window relative to bb window skewed the other way) must not
    fire — both conditions are required, not either."""
    df = _dip_recovery_df()
    # a much longer RSI length won't cross 50 in sync with the short, sharp
    # price move above, so this must produce no trade at all.
    stats, _eq, events = run_with_events(df, 14, 5, 1.0, deploy=0.97, stop_pct=0.5)
    assert stats["n_trades"] == 0
    assert events == []

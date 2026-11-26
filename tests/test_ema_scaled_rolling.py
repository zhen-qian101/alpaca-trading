"""Regression tests for backtest/ema_scaled_rolling.py:

1. exec_next_open — defers EXIT fills (stop / trend-exit / half-target) to the
   NEXT bar's open, the same way entries already work, instead of filling at
   the same bar's close that triggered the decision (2026-07-03, requested by
   the user after asking whether close-decision/close-fill was unrealistic).
2. yfinance MultiIndex columns — yfinance now returns MultiIndex (field,
   ticker) columns even for a single-ticker download, which used to crash
   load_ohlcv's yfinance branch outright. Since crypto (BTC/USD) always routes
   through yfinance (never IBKR — Paxos-limited), this had silently broken
   crypto trading in the live engine before the fix.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.ema_scaled_rolling import load_ohlcv, run_with_events


def _synthetic_df():
    n = 40
    idx = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    close = np.full(n, 100.0)
    close[10:] = 110.0  # jump up -> cross_up entry
    close[25:] = 90.0   # jump down -> breaches rolling-close-min stop reference
    open_ = close.copy()
    # A real open, one bar after the exit-decision bar, distinct from that
    # bar's close — proves the fill uses the NEXT bar's open, not the
    # decision bar's close.
    open_[26] = 999.0
    return pd.DataFrame({"close": close, "open": open_, "high": close, "low": close}, index=idx)


def test_exec_next_open_defers_exit_fill_by_one_bar():
    df = _synthetic_df()
    kw = dict(fast=3, slow=5, window=5, stop_pct=0.5, deploy=0.97, use_real_open=True)

    _, _, ev_close = run_with_events(df, **kw, exec_next_open=False)
    _, _, ev_next = run_with_events(df, **kw, exec_next_open=True)

    buy_close = [e for e in ev_close if e["action"] == "buy"][0]
    buy_next = [e for e in ev_next if e["action"] == "buy"][0]
    # Entries are unaffected — they already fill at the following bar's open.
    assert buy_close == buy_next

    exit_close = [e for e in ev_close if e["action"] == "close"][0]
    exit_next = [e for e in ev_next if e["action"] == "close"][0]
    assert exit_close["reason"] == exit_next["reason"] == "stop"
    # Same-bar-close fill: exits at the decision bar's own close.
    assert exit_close["price"] == 90.0
    # Next-open fill: exits one bar later, at that bar's real open.
    assert exit_next["ts"] == exit_close["ts"] + pd.Timedelta(minutes=5)
    assert exit_next["price"] == 999.0


def test_exec_next_open_default_is_close_fill_unchanged():
    """Default (exec_next_open unset) must stay byte-identical to the
    pre-existing behavior — this is an opt-in, not a behavior change."""
    df = _synthetic_df()
    kw = dict(fast=3, slow=5, window=5, stop_pct=0.5, deploy=0.97, use_real_open=True)
    _, _, ev_default = run_with_events(df, **kw)
    _, _, ev_explicit_false = run_with_events(df, **kw, exec_next_open=False)
    assert ev_default == ev_explicit_false


def test_load_ohlcv_yfinance_handles_multiindex_columns(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=5, freq="5min", tz="UTC")
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["AAPL"]])
    fake = pd.DataFrame(
        np.tile(np.arange(5.0).reshape(-1, 1), (1, 5)), index=idx, columns=cols
    )

    def fake_download(*args, **kwargs):
        return fake

    monkeypatch.setattr("yfinance.download", fake_download)
    df = load_ohlcv("AAPL", "5min", source="yfinance")
    assert list(df.columns) == ["open", "high", "low", "close"]
    assert len(df) == 5


def test_load_ohlcv_alpaca_branch_keeps_volume(monkeypatch):
    """Regression guard: the alpaca branch used to drop the volume column
    outright even though Alpaca's response carries it — the root cause of the
    Flow Terminal's "insufficient volume data" gap."""
    from alpaca.data.historical import StockHistoricalDataClient

    idx = pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC")
    fake_df = pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": [100.0, 200.0, 300.0, 400.0, 500.0]},
        index=idx,
    )

    class _FakeBarSet:
        df = fake_df

    captured = {}

    def fake_get_stock_bars(self, request):
        captured["feed"] = getattr(request, "feed", None)
        return _FakeBarSet()

    monkeypatch.setattr(StockHistoricalDataClient, "get_stock_bars", fake_get_stock_bars)
    df = load_ohlcv("AAPL", "1h", source="alpaca", feed="sip")
    assert "volume" in df.columns
    assert list(df["volume"]) == [100.0, 200.0, 300.0, 400.0, 500.0]
    assert captured["feed"] is not None and str(captured["feed"]).lower().endswith("sip")

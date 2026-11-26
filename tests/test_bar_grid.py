"""Tests for the 1h bar-grid repair (2026-07-14).

Background: `bars.sqlite` held two bar-open conventions under one
(symbol, interval='1h') key — yfinance's 09:30-anchored :30 stamps and IBKR's
:00 stamps. They can never collide on the primary key, so both survived and the
engine's warm-up window came back interleaved at ~2 bars/hour on all 11 symbols.
A one-off migration purged the existing off-grid duplicates (applied
2026-07-14, since retired from the repo).

Three defences are locked here, each of which the bug walked straight through:

  1. relay.bars_db.upsert_bars resolves same-ts collisions by SOURCE, so IBKR
     (the configured primary, with real volume + 24h coverage) upgrades a
     yfinance bar and yfinance can never downgrade an IBKR one. The old
     INSERT OR IGNORE gave the row to whoever wrote FIRST — meaning a transient
     IBKR outage let the degraded source win permanently.
  2. backtest.ema_scaled_rolling._yf_hourly_aligned rebuilds hourly bars from
     the 30m series so yfinance lands on :00 — rather than re-labelling a
     09:30-10:30 bar as "09:00", which would misstate the window it covers.
  3. sigengine.ibkr._snap_to_grid moves near-grid stamps (IBKR's 20:01 UTC
     crypto session bar) onto the boundary, while leaving genuinely off-grid
     bars alone.

    .venv/bin/python -m pytest -q tests/test_bar_grid.py
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from relay import bars_db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(bars_db, "BARS_DB_FILE", tmp_path / "grid_test.sqlite")
    monkeypatch.setattr(bars_db, "_CONN", None)
    yield
    bars_db.close()


def _bar(ts, close, volume=None):
    d = {"open": close, "high": close, "low": close, "close": close}
    if volume is not None:
        d["volume"] = volume
    return pd.DataFrame([d], index=pd.DatetimeIndex([ts], tz="UTC"))


# --- 1. source precedence -------------------------------------------------
def test_ibkr_upgrades_a_yfinance_bar_at_the_same_ts():
    """The regression: yfinance got there first, IBKR must still win."""
    ts = dt.datetime(2026, 1, 2, 10, tzinfo=dt.UTC)
    bars_db.upsert_bars("AAPL", "1h", _bar(ts, 100.0), source="yfinance")
    n = bars_db.upsert_bars("AAPL", "1h", _bar(ts, 101.5, volume=42.0), source="ibkr")

    assert n == 1
    df = bars_db.read_bars("AAPL", "1h")
    assert len(df) == 1
    assert df["close"].iloc[0] == 101.5
    assert df["volume"].iloc[0] == 42.0


def test_yfinance_can_never_downgrade_an_ibkr_bar():
    """A fallback poll during an IBKR blip must not overwrite good data."""
    ts = dt.datetime(2026, 1, 2, 10, tzinfo=dt.UTC)
    bars_db.upsert_bars("AAPL", "1h", _bar(ts, 101.5, volume=42.0), source="ibkr")
    n = bars_db.upsert_bars("AAPL", "1h", _bar(ts, 100.0), source="yfinance")

    assert n == 0
    df = bars_db.read_bars("AAPL", "1h")
    assert df["close"].iloc[0] == 101.5
    assert df["volume"].iloc[0] == 42.0


def test_same_source_refetch_now_corrects_the_row():
    """CHANGED 2026-07-31, and the old assertion here was the bug.

    This used to assert that a same-source re-fetch left the cached row alone —
    "preserving the original INSERT OR IGNORE behaviour for the common case".
    The common case turns out to be a bar being cached WHILE IT IS STILL
    FORMING, because the engine polls every 5 minutes, so that rule froze every
    hourly close at the price a few minutes into the hour. 148 of 151 cached
    MSFT bars disagreed with IBKR's own values, one by $11.09.

    A later fetch of the same bar from the same source is more complete, so it
    now wins.
    """
    ts = dt.datetime(2026, 1, 2, 10, tzinfo=dt.UTC)
    bars_db.upsert_bars("AAPL", "1h", _bar(ts, 100.0, volume=1_000.0), source="yfinance")
    assert bars_db.upsert_bars("AAPL", "1h", _bar(ts, 999.0, volume=50_000.0),
                               source="yfinance") == 1
    assert bars_db.read_bars("AAPL", "1h")["close"].iloc[0] == 999.0


def test_same_source_refetch_with_less_volume_is_still_refused():
    """The guard that keeps the correction from becoming last-writer-wins: a
    forming bar only ever gains volume, so less volume means a worse print."""
    ts = dt.datetime(2026, 1, 2, 10, tzinfo=dt.UTC)
    bars_db.upsert_bars("AAPL", "1h", _bar(ts, 100.0, volume=50_000.0), source="yfinance")
    assert bars_db.upsert_bars("AAPL", "1h", _bar(ts, 999.0, volume=7.0),
                               source="yfinance") == 0
    assert bars_db.read_bars("AAPL", "1h")["close"].iloc[0] == 100.0


def test_source_rank_order():
    assert bars_db.source_rank("ibkr") > bars_db.source_rank("alpaca") > bars_db.source_rank("yfinance")
    assert bars_db.source_rank("something-new") == 0
    assert bars_db.source_rank("IBKR") == bars_db.source_rank("ibkr")


def test_a_new_ts_is_still_a_plain_insert():
    base = dt.datetime(2026, 1, 2, 10, tzinfo=dt.UTC)
    df = pd.DataFrame(
        {"close": [1.0, 2.0, 3.0]},
        index=pd.date_range(base, periods=3, freq="1h", tz="UTC"),
    )
    assert bars_db.upsert_bars("MSFT", "1h", df, source="yfinance") == 3
    assert len(bars_db.read_bars("MSFT", "1h")) == 3


# --- 2. yfinance hourly alignment ----------------------------------------
def test_yf_hourly_aligned_buckets_30m_bars_onto_the_hour(monkeypatch):
    """A 09:30-anchored 30m series must come back stamped :00, with OHLCV
    aggregated across each pair of half-hours (not re-labelled)."""
    from backtest import ema_scaled_rolling as esr

    # One RTH day of 30m bars: 09:30 .. 11:30 ET, i.e. 13:30 .. 15:30 UTC.
    idx = pd.date_range("2026-01-02 13:30", periods=5, freq="30min", tz="UTC")
    fake = pd.DataFrame(
        {"Open": [10, 11, 12, 13, 14], "High": [20, 21, 22, 23, 24],
         "Low": [1, 2, 3, 4, 5], "Close": [15, 16, 17, 18, 19],
         "Volume": [100, 200, 300, 400, 500]},
        index=idx,
    )
    monkeypatch.setattr(esr, "yf", type("_yf", (), {"download": staticmethod(lambda *a, **k: fake)}),
                        raising=False)
    import sys
    sys.modules["yfinance"] = type("_m", (), {"download": staticmethod(lambda *a, **k: fake)})

    out = esr._yf_hourly_aligned("AAPL", "60d")

    assert list(pd.DatetimeIndex(out.index).minute) == [0, 0, 0]        # every bar on :00
    assert [t.hour for t in pd.DatetimeIndex(out.index)] == [13, 14, 15]

    # 13:00 bucket holds ONLY the 13:30 half-hour (RTH starts mid-hour).
    assert out.loc[out.index[0], "open"] == 10
    assert out.loc[out.index[0], "volume"] == 100
    # 14:00 bucket aggregates 14:00 + 14:30 -> open of first, close of last.
    assert out.loc[out.index[1], "open"] == 11
    assert out.loc[out.index[1], "close"] == 17
    assert out.loc[out.index[1], "high"] == 22
    assert out.loc[out.index[1], "low"] == 2
    assert out.loc[out.index[1], "volume"] == 500


def test_clamp_30m_period_respects_yfinances_60d_cap():
    """yfinance answers an over-long 30m request with an EMPTY frame, so an
    unclamped "730d" would silently yield no data at all."""
    from backtest.ema_scaled_rolling import _clamp_30m_period

    assert _clamp_30m_period("730d") == "60d"
    assert _clamp_30m_period("15d") == "15d"
    assert _clamp_30m_period("60d") == "60d"
    assert _clamp_30m_period("2y") == "60d"
    assert _clamp_30m_period("max") == "60d"


def test_crypto_is_exempt_from_realignment():
    """A 24/7 venue has no 09:30 open to anchor to, so yfinance crypto hourly
    bars are already :00 — realigning them would only cost history depth."""
    from backtest.ema_scaled_rolling import _is_crypto_ticker

    assert _is_crypto_ticker("BTC/USD")
    assert _is_crypto_ticker("BTC-USD")
    assert _is_crypto_ticker("ETH/USD")
    assert not _is_crypto_ticker("AAPL")
    assert not _is_crypto_ticker("SOXL")


def test_align_hourly_is_opt_in(monkeypatch):
    """Default False: the backtest CLIs call load_ohlcv(sym,'1h','yfinance')
    and need the deep 730d window that only the native (:30) endpoint serves."""
    import inspect
    from backtest.ema_scaled_rolling import load_ohlcv

    assert inspect.signature(load_ohlcv).parameters["align_hourly"].default is False


# --- 3. extended-hours (prepost) fallback coverage -------------------------
def test_prepost_is_opt_in():
    """Default False: backtest CLIs keep the RTH-only series their params were
    validated on; only the live cache writer opts in."""
    import inspect
    from backtest.ema_scaled_rolling import load_ohlcv, _yf_hourly_aligned

    assert inspect.signature(load_ohlcv).parameters["prepost"].default is False
    assert inspect.signature(_yf_hourly_aligned).parameters["prepost"].default is False


def test_yf_hourly_aligned_forwards_prepost_to_yfinance(monkeypatch):
    """prepost must reach yf.download, or an IBKR outage outside RTH freezes
    the equity cache (2026-08-05: 15h ushmds farm wedge, zero new bars)."""
    import sys
    from backtest import ema_scaled_rolling as esr

    idx = pd.date_range("2026-01-02 13:30", periods=2, freq="30min", tz="UTC")
    fake = pd.DataFrame(
        {"Open": [1, 2], "High": [3, 4], "Low": [0, 1], "Close": [2, 3],
         "Volume": [10, 20]},
        index=idx,
    )
    seen = {}

    def _download(*a, **k):
        seen.update(k)
        return fake

    sys.modules["yfinance"] = type("_m", (), {"download": staticmethod(_download)})
    esr._yf_hourly_aligned("AAPL", "60d", prepost=True)
    assert seen.get("prepost") is True


def test_engine_cache_writer_requests_prepost(monkeypatch):
    """sigengine.engine._load_with_retry is the live cache writer: it must ask
    for extended-hours bars so the yfinance fallback matches the IBKR
    primary's coverage."""
    from sigengine import engine as eng

    seen = {}

    def _fake_load_ohlcv(sym, interval, source, **kwargs):
        seen.update(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(eng, "load_ohlcv", _fake_load_ohlcv)
    eng._load_with_retry("AAPL", "1h", "yfinance")
    assert seen.get("prepost") is True
    assert seen.get("align_hourly") is True


# --- 4. IBKR grid snapping ------------------------------------------------
def test_snap_moves_the_2001_crypto_session_bar_onto_the_hour():
    from sigengine.ibkr import _snap_to_grid

    idx = pd.DatetimeIndex(["2026-07-13 19:00", "2026-07-13 20:01"], tz="UTC")
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    out = _snap_to_grid(df, "1h")

    assert [t.minute for t in pd.DatetimeIndex(out.index)] == [0, 0]
    assert [t.hour for t in pd.DatetimeIndex(out.index)] == [19, 20]
    assert out["close"].tolist() == [1.0, 2.0]


def test_snap_leaves_genuinely_off_grid_bars_alone():
    """A :30 bar is a real half-hour off — snapping it would misstate the
    window it covers, so it must be left for the source-side fix instead."""
    from sigengine.ibkr import _snap_to_grid

    idx = pd.DatetimeIndex(["2026-07-13 13:30", "2026-07-13 14:30"], tz="UTC")
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    out = _snap_to_grid(df, "1h")

    assert [t.minute for t in pd.DatetimeIndex(out.index)] == [30, 30]


def test_snap_is_a_noop_on_an_already_aligned_series():
    from sigengine.ibkr import _snap_to_grid

    idx = pd.date_range("2026-07-13 00:00", periods=4, freq="1h", tz="UTC")
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    out = _snap_to_grid(df, "1h")

    assert out.index.equals(idx)
    assert out["close"].tolist() == [1.0, 2.0, 3.0, 4.0]

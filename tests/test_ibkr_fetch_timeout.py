"""Regression test for the 2026-07-06 incident: IBKR's crypto/PAXOS
historical-data service silently hung (no exception, no data) for BTC/USD on
every poll cycle for 15+ hours straight — confirmed via isolated testing to be
IBKR/Gateway-side (a single-session-per-account crypto market-data
entitlement getting stuck, `Error 162: Trading TWS session is connected from
a different IP address` observed once), not a bug in this fetch/threading
code. Because reqHistoricalData's own advertised timeout doesn't reliably
fire for this contract, and _fetch_bars previously called ibkr.fetch_bars
directly with no bound of its own, a stuck call consumed the ENTIRE
_FETCH_PHASE_TIMEOUT with zero data — the yfinance fallback branch was never
even reached. sigengine.engine._ibkr_fetch_with_timeout now bounds every
ibkr.fetch_bars() call to _IBKR_CALL_TIMEOUT, so _fetch_bars falls back to
yfinance within that same poll cycle instead of getting nothing.

    .venv/bin/pytest -q tests/test_ibkr_fetch_timeout.py
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pandas as pd

import sigengine.engine as eng  # noqa: E402


def _df(n=3):
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                          "volume": 1.0}, index=idx)


def test_ibkr_fetch_with_timeout_returns_result_when_fast(monkeypatch):
    monkeypatch.setattr(eng, "_IBKR_CALL_TIMEOUT", 5.0)
    sentinel = _df()
    import sigengine.ibkr as ibkr
    monkeypatch.setattr(ibkr, "fetch_bars", lambda sym, iv: sentinel)

    out = eng._ibkr_fetch_with_timeout("AAPL", "1h")
    assert out is sentinel


def test_ibkr_fetch_with_timeout_abandons_a_stuck_call(monkeypatch):
    monkeypatch.setattr(eng, "_IBKR_CALL_TIMEOUT", 0.2)
    import sigengine.ibkr as ibkr

    def _stuck(sym, iv):
        time.sleep(0.6)  # longer than the timeout, short enough not to hang the test
        return _df()

    monkeypatch.setattr(ibkr, "fetch_bars", _stuck)

    start = time.monotonic()
    out = eng._ibkr_fetch_with_timeout("BTC/USD", "1h")
    elapsed = time.monotonic() - start

    assert out is None
    assert elapsed < 0.5, f"did not abandon the stuck call promptly ({elapsed:.2f}s)"


def test_ibkr_fetch_with_timeout_returns_none_on_exception(monkeypatch):
    monkeypatch.setattr(eng, "_IBKR_CALL_TIMEOUT", 5.0)
    import sigengine.ibkr as ibkr

    def _raises(sym, iv):
        raise RuntimeError("boom")

    monkeypatch.setattr(ibkr, "fetch_bars", _raises)
    assert eng._ibkr_fetch_with_timeout("AAPL", "1h") is None


def test_fetch_bars_crypto_falls_back_to_yfinance_when_ibkr_stuck(monkeypatch):
    """A stuck IBKR crypto call must not prevent the SAME poll cycle from
    still getting real (yfinance) data for that symbol."""
    monkeypatch.setattr(eng, "_IBKR_CALL_TIMEOUT", 0.2)
    monkeypatch.setattr(eng, "bar_count", lambda sym, iv: 10)  # warm cache -> topup path

    def _stuck_ibkr(sym, interval):
        assert eng._ibkr_fetch_with_timeout is not None
        time.sleep(0.6)
        return _df()

    import sigengine.ibkr as ibkr
    monkeypatch.setattr(ibkr, "fetch_bars", _stuck_ibkr)

    yf_sentinel = _df(n=5)
    monkeypatch.setattr(eng, "_load_with_retry",
                        lambda sym, interval, source, yf_period_override=None: yf_sentinel)

    start = time.monotonic()
    df, source = eng._fetch_bars("BTC/USD", {"interval": "1h"}, {"crypto_source": "ibkr"})
    elapsed = time.monotonic() - start

    assert source == "yfinance"
    assert df is yf_sentinel
    assert elapsed < 0.5, f"_fetch_bars blocked on the stuck IBKR call ({elapsed:.2f}s)"


def test_fetch_bars_equity_falls_back_to_yfinance_when_ibkr_stuck(monkeypatch):
    monkeypatch.setattr(eng, "_IBKR_CALL_TIMEOUT", 0.2)
    monkeypatch.setattr(eng, "bar_count", lambda sym, iv: 10)
    monkeypatch.setattr(eng, "equity_market_closed_for_weekend", lambda: False)
    monkeypatch.setattr(eng, "is_crypto_symbol", lambda sym: False)

    def _stuck_ibkr(sym, interval):
        time.sleep(0.6)
        return _df()

    import sigengine.ibkr as ibkr
    monkeypatch.setattr(ibkr, "fetch_bars", _stuck_ibkr)

    yf_sentinel = _df(n=5)
    monkeypatch.setattr(eng, "_load_with_retry",
                        lambda sym, interval, source, yf_period_override=None: yf_sentinel)

    start = time.monotonic()
    df, source = eng._fetch_bars("AAPL", {"interval": "1h"}, {"primary": "ibkr", "fallback": "yfinance"})
    elapsed = time.monotonic() - start

    assert source == "yfinance"
    assert df is yf_sentinel
    assert elapsed < 0.5, f"_fetch_bars blocked on the stuck IBKR call ({elapsed:.2f}s)"

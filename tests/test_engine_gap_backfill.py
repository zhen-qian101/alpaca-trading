"""Tests for sigengine.engine._backfill_gaps — the end-of-poll safety net that
re-pulls from IBKR any equity left >1 bar behind its peers by a transient
yfinance fallback, so the bar cache / Signal Terminal chart stay complete.

    .venv/bin/pytest -q tests/test_engine_gap_backfill.py
"""
from __future__ import annotations

import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pandas as pd  # noqa: E402

import sigengine.engine as eng  # noqa: E402

ENABLED = {
    "AAPL": {"interval": "1h"},
    "MSFT": {"interval": "1h"},
    "TQQQ": {"interval": "1h"},
    "BTC/USD": {"interval": "1h"},
}
DATA_CFG = {"primary": "ibkr"}
T13 = pd.Timestamp("2026-07-13 13:00", tz="UTC")
T12 = pd.Timestamp("2026-07-13 12:00", tz="UTC")


def _wire(monkeypatch, latest_map, *, weekend=False, ibkr_has_bar=True):
    """Patch the cache/IBKR/calendar seams _backfill_gaps depends on. Returns
    (repulled, upserted) lists that record which symbols were re-fetched/written."""
    repulled: list[str] = []
    upserted: list[str] = []

    monkeypatch.setattr(eng, "latest_ts", lambda sym, iv: latest_map.get(sym))
    monkeypatch.setattr(eng, "equity_market_closed_for_weekend", lambda: weekend)

    def fake_ibkr(sym, iv):
        repulled.append(sym)
        if not ibkr_has_bar:
            return None
        return pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0],
                             "close": [1.0], "volume": [1.0]}, index=[T13])

    monkeypatch.setattr(eng, "_ibkr_fetch_with_timeout", fake_ibkr)

    def fake_upsert(sym, iv, df, source):
        upserted.append(sym)
        return len(df)

    monkeypatch.setattr(eng, "upsert_bars", fake_upsert)
    return repulled, upserted


def test_lagging_equity_is_backfilled(monkeypatch):
    # TQQQ one bar behind its peers -> re-pulled from IBKR and upserted.
    latest = {"AAPL": T13, "MSFT": T13, "TQQQ": T12, "BTC/USD": T13}
    repulled, upserted = _wire(monkeypatch, latest)
    eng._backfill_gaps(ENABLED, DATA_CFG)
    assert repulled == ["TQQQ"]
    assert upserted == ["TQQQ"]


def test_all_current_no_backfill(monkeypatch):
    # Every equity on the same bar -> nothing re-pulled.
    latest = {"AAPL": T13, "MSFT": T13, "TQQQ": T13, "BTC/USD": T13}
    repulled, upserted = _wire(monkeypatch, latest)
    eng._backfill_gaps(ENABLED, DATA_CFG)
    assert repulled == []
    assert upserted == []


def test_weekend_skips_entirely(monkeypatch):
    # Market closed for the weekend: every equity is legitimately frozen, so the
    # pass must not fire even if timestamps differ.
    latest = {"AAPL": T13, "MSFT": T13, "TQQQ": T12, "BTC/USD": T13}
    repulled, _ = _wire(monkeypatch, latest, weekend=True)
    eng._backfill_gaps(ENABLED, DATA_CFG)
    assert repulled == []


def test_crypto_lag_ignored_and_not_a_reference(monkeypatch):
    # BTC is excluded both ways: a lagging BTC is never backfilled, AND a fresh
    # BTC must not become the peer reference that mis-flags in-sync equities.
    # Here all equities share T12 while BTC is at T13; no equity should be pulled.
    latest = {"AAPL": T12, "MSFT": T12, "TQQQ": T12, "BTC/USD": T13}
    repulled, _ = _wire(monkeypatch, latest)
    eng._backfill_gaps(ENABLED, DATA_CFG)
    assert repulled == []


def test_ibkr_still_down_no_upsert(monkeypatch):
    # Laggard flagged but IBKR returns nothing -> attempted, no upsert, no raise.
    latest = {"AAPL": T13, "MSFT": T13, "TQQQ": T12, "BTC/USD": T13}
    repulled, upserted = _wire(monkeypatch, latest, ibkr_has_bar=False)
    eng._backfill_gaps(ENABLED, DATA_CFG)
    assert repulled == ["TQQQ"]
    assert upserted == []


def test_skipped_when_primary_not_ibkr(monkeypatch):
    latest = {"AAPL": T13, "MSFT": T13, "TQQQ": T12, "BTC/USD": T13}
    repulled, _ = _wire(monkeypatch, latest)
    eng._backfill_gaps(ENABLED, {"primary": "yfinance"})
    assert repulled == []

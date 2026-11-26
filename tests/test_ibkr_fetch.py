"""Regression test for a real bug found 2026-07-03 against a live IBKR
connection: sigengine.ibkr._fetch_one built its DatetimeIndex from
df["date"] AFTER already dropping that column via a
df[["open","high","low","close","volume"]] selection, so `"date" in df`
was always False and it silently fell through to df's meaningless default
RangeIndex — which pd.DatetimeIndex(..., tz="UTC") then misinterpreted as
nanoseconds-since-epoch, dating every bar ~1970-01-01. This corrupts the
signal engine's diff logic (last_closed_ts comparisons) and the SQLite bar
cache if it reaches upsert_bars.

Uses a fake `ib` object (no real IBKR connection) returning real
ib_async.BarData objects, matching the exact shape sigengine.ibkr._fetch_one
consumes.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from ib_async import BarData

import sigengine.ibkr as ibkr_mod


class FakeIB:
    def __init__(self, bars):
        self._bars = bars

    def qualifyContracts(self, contract):
        return [contract]

    def reqHistoricalData(self, *args, **kwargs):
        return self._bars


def _bars():
    base = dt.datetime(2026, 7, 2, 8, 0, tzinfo=dt.timezone.utc)
    return [
        BarData(date=base + dt.timedelta(minutes=5 * i), open=100.0 + i, high=101.0 + i,
                low=99.0 + i, close=100.5 + i, volume=1000.0)
        for i in range(5)
    ]


def test_fetch_one_index_is_real_dates_not_epoch_garbage():
    fake_ib = FakeIB(_bars())
    df = ibkr_mod._fetch_one(fake_ib, contract=object(), bar_size="5 mins", duration="1 D")
    assert df is not None
    assert isinstance(df.index, pd.DatetimeIndex)
    # The regression: a broken index lands near 1970-01-01. Real bars here
    # are dated 2026-07-02 — assert we're nowhere near the epoch.
    assert df.index.min().year >= 2020
    assert list(df.index) == [b.date for b in _bars()]


def test_fetch_one_index_is_utc_and_sorted():
    fake_ib = FakeIB(_bars())
    df = ibkr_mod._fetch_one(fake_ib, contract=object(), bar_size="5 mins", duration="1 D")
    assert str(df.index.tz) == "UTC"
    assert df.index.is_monotonic_increasing


def test_fetch_one_returns_none_on_empty_bars():
    fake_ib = FakeIB([])
    df = ibkr_mod._fetch_one(fake_ib, contract=object(), bar_size="5 mins", duration="1 D")
    assert df is None


def test_crypto_contract_for_uses_paxos_exchange():
    """IBKR crypto trades on a single 24/7 venue: PAXOS. Added 2026-07-03 —
    fetch_bars used to hard-skip crypto symbols entirely; this is the contract
    that now lets BTC/USD route through IBKR instead of always yfinance."""
    c = ibkr_mod._crypto_contract_for("BTC/USD")
    assert c.symbol == "BTC"
    assert c.exchange == "PAXOS"
    assert c.currency == "USD"
    assert c.secType == "CRYPTO"


class RecordingFakeIB(FakeIB):
    """Like FakeIB, but records the contract/whatToShow of every historical
    data request, so tests can assert routing without a live connection."""
    def __init__(self, bars):
        super().__init__(bars)
        self.calls = []

    def reqHistoricalData(self, contract, **kwargs):
        self.calls.append({"contract": contract, "what_to_show": kwargs.get("whatToShow")})
        return self._bars


def test_fetch_bars_routes_btc_through_single_crypto_contract(monkeypatch):
    """Crypto must NOT go through the equity SMART+OVERNIGHT merge path (that's
    an equity-only concept — Paxos is already 24/7) and must request
    whatToShow="AGGTRADES", the value IBKR added specifically for Crypto
    contracts (TRADES is documented for equities, not guaranteed for crypto)."""
    fake_ib = RecordingFakeIB(_bars())
    monkeypatch.setattr(ibkr_mod, "connect", lambda symbol: fake_ib)
    df = ibkr_mod.fetch_bars("BTC/USD", "1h")
    assert df is not None
    assert len(fake_ib.calls) == 1  # single contract, no SMART/OVERNIGHT merge
    assert fake_ib.calls[0]["contract"].secType == "CRYPTO"
    assert fake_ib.calls[0]["contract"].exchange == "PAXOS"
    assert fake_ib.calls[0]["what_to_show"] == "AGGTRADES"

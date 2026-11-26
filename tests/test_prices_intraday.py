"""agent/prices.py under the runs that are NOT post-session.

The module was written for the 20:00 ET recap, where the daily bar has settled
and the overnight book is live. It is now also called by the intraday research
runs, and two things went wrong there:

  1. The intraday runner passes the watchlist FROM decisions.json, which
     contains BTC/USD. A crypto pair sent to a stock endpoint does not merely
     return nothing for itself — Alpaca rejects the entire batch, so every
     equity lost its prices. On 2026-07-31 that shipped a briefing whose
     narrator told viewers "my price file came back completely empty".
  2. A daily bar read mid-session is still forming, and the statement called its
     last print a "close".
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import prices


class _Bar:
    def __init__(self, day, close, high, low, volume):
        self.timestamp = datetime(2026, 7, day, 20, 0, tzinfo=timezone.utc)
        self.close, self.high, self.low, self.volume = close, high, low, volume


class _FakeClient:
    """Mimics the one behaviour that matters: a batch containing an unusable
    symbol fails as a whole, exactly as the API does."""

    def __init__(self, bad=("BTC/USD",), fail_all=False):
        self.bad, self.fail_all = set(bad), fail_all
        self.calls = []

    def get_stock_bars(self, req):
        syms = req.symbol_or_symbols
        syms = [syms] if isinstance(syms, str) else list(syms)
        self.calls.append(tuple(syms))
        if self.fail_all:
            raise RuntimeError("feed unavailable")
        bad = self.bad.intersection(syms)
        if bad:
            raise RuntimeError(f'{{"message":"invalid symbol: {sorted(bad)[0]}"}}')

        class _R:
            data = {s: [_Bar(29, 100.0, 101.0, 99.0, 1000),
                        _Bar(30, 110.0, 112.0, 105.0, 2000)] for s in syms}
        return _R()


@pytest.fixture(autouse=True)
def _no_real_sdk(monkeypatch):
    """The request/enum objects are only carriers here; stub them so the test
    never needs alpaca-py's validation or the network."""
    import types

    class _Req:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    mods = {
        "alpaca.data.enums": types.SimpleNamespace(DataFeed=lambda f: f),
        "alpaca.data.requests": types.SimpleNamespace(
            StockBarsRequest=_Req, StockLatestQuoteRequest=_Req,
            StockLatestTradeRequest=_Req),
        "alpaca.data.timeframe": types.SimpleNamespace(
            TimeFrame=types.SimpleNamespace(Day="1Day")),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(__import__("sys").modules, name, mod)


START = datetime(2026, 7, 19, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# one bad symbol must not blank the file
# --------------------------------------------------------------------------
def test_crypto_is_never_sent_to_a_stock_endpoint():
    eq, other = prices._split_equities(["GOOG", "BTC/USD", "MSFT", "ETH/USD"])
    assert eq == ["GOOG", "MSFT"] and other == ["BTC/USD", "ETH/USD"]


def test_a_poisoned_batch_falls_back_to_per_symbol():
    """Even if something unusable slips through, the file must lose that one
    symbol — not all ten."""
    c = _FakeClient(bad=("TSLA",))
    bars, feed = prices._daily_bars(c, ["GOOG", "TSLA", "MSFT"], START)
    assert feed == "sip"
    assert sorted(bars) == ["GOOG", "MSFT"]          # TSLA dropped, the rest survive
    assert ("GOOG", "TSLA", "MSFT") in c.calls        # batch tried first
    assert ("GOOG",) in c.calls                       # then per symbol


def test_the_batch_is_tried_before_any_per_symbol_calls():
    c = _FakeClient(bad=())
    bars, feed = prices._daily_bars(c, ["GOOG", "MSFT"], START)
    assert sorted(bars) == ["GOOG", "MSFT"] and c.calls == [("GOOG", "MSFT")]


def test_a_genuinely_dead_feed_still_reports_none():
    c = _FakeClient(fail_all=True)
    bars, feed = prices._daily_bars(c, ["GOOG", "MSFT"], START)
    assert bars == {} and feed == "none"


def test_a_single_symbol_request_does_not_recurse():
    c = _FakeClient(bad=("GOOG",))
    bars, feed = prices._daily_bars(c, ["GOOG"], START)
    assert bars == {} and feed == "none"
    assert c.calls.count(("GOOG",)) == len(prices._DAILY_FEEDS)


# --------------------------------------------------------------------------
# a bar that is still forming is not a close
# --------------------------------------------------------------------------
SESSION = {"date": "2026-07-31", "close": 454.25, "high": 462.95, "low": 449.33,
           "volume": 14_532_057, "prev_close": 451.10, "pct": 0.70}


def test_mid_session_the_statement_says_trading_not_closed():
    st = prices._statement("MSFT", SESSION, {"quality": "unavailable"}, in_progress=True)
    assert "trading at $454.25" in st and "range so far" in st
    assert "closed" not in st.split("THE SESSION")[0]
    assert "STILL OPEN" in st


def test_after_the_bell_the_statement_says_closed():
    st = prices._statement("MSFT", SESSION,
                           {"quality": "unavailable"}, in_progress=False)
    assert st.startswith("MSFT: closed $454.25 (+0.70%)")
    assert "STILL OPEN" not in st


def test_mid_session_says_nothing_about_the_overnight_book():
    """It closed at 04:00 ET. Quoting it against a live price compares a stale
    thin print to the real market."""
    st = prices._statement("MSFT", SESSION, {"quality": "reliable", "mid": 400.0,
                                             "pct_vs_close": -12.0, "bid": 399.0,
                                             "ask": 401.0, "bid_size": 1, "ask_size": 1},
                           in_progress=True)
    assert "EXTENDED" not in st


def test_no_session_data_still_forbids_inventing_a_price():
    st = prices._statement("MSFT", None, {"quality": "unavailable"})
    assert "do not state a price" in st

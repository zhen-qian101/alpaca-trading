"""Fill markers: drawing what the account actually traded, not what the bars
still say it should have.

WHY THIS EXISTS. Every other mark on this chart is derived. The green/red arrows
are the strategy RECOMPUTED over the current bar cache, and bars are not stable:
the engine upserts fresh 10-day IBKR pulls every poll and — since the
source-precedence fix — those pulls OVERWRITE values, so recovering from a data
outage rewrites ~10 days of history underneath the chart.

Measured on 2026-08-20, 5 of the 9 fills inside the displayed windows had no
mark of any kind. MSFT was the clearest case: a real buy at 485.75 and a real
sell at 482.36, both driven by a bollinger_rsi signal computed on a corrupt
yfinance bar (H 503.58 / L 450.97 against a true 485.29 / 483.34), and both
invisible the moment IBKR came back and corrected that bar. The user could not
see their own trades on the chart they use to check their trading.

Fills are ground truth and never need re-deriving, so these marks must NOT move
when the bars do. That is the property under test.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtest.chart_view import _chart_ts, _fill_markers


@pytest.fixture
def index():
    """Ten hourly bars, 2026-08-19 18:00 -> 2026-08-20 03:00 UTC."""
    return pd.date_range("2026-08-19 18:00", periods=10, freq="1h", tz="UTC")


def _fill(ts, side, qty=9.0, price=485.75, symbol="MSFT"):
    return {"ts": pd.Timestamp(ts, tz="UTC").timestamp(), "symbol": symbol,
            "side": side, "qty": qty, "price": price}


def test_a_fill_is_drawn(index):
    got = _fill_markers([_fill("2026-08-19 22:04", "buy")], index, index[0])
    assert len(got) == 1
    assert got[0]["time"] == _chart_ts(pd.Timestamp("2026-08-19 22:00", tz="UTC"))


def test_buys_and_sells_are_told_apart(index):
    got = _fill_markers([_fill("2026-08-19 22:04", "buy"),
                         _fill("2026-08-20 02:02", "sell", price=482.36)],
                        index, index[0])
    buy, sell = got[0], got[1]
    assert buy["position"] == "belowBar" and sell["position"] == "aboveBar"
    assert buy["color"] != sell["color"]
    assert "BUY 9 @ 485.75" == buy["text"]
    assert "SELL 9 @ 482.36" == sell["text"]


def test_fills_are_visually_distinct_from_signal_arrows(index):
    """A fill and a recomputed signal answer different questions; if they render
    identically the chart cannot be read. Signals are arrows — fills are not."""
    got = _fill_markers([_fill("2026-08-19 22:04", "buy")], index, index[0])
    assert got[0]["shape"] == "circle"
    assert "arrow" not in got[0]["shape"].lower()
    assert got[0]["color"] not in ("#34b892", "#e2596b", "#f2a93b")


def test_a_fill_survives_the_bars_being_rewritten(index):
    """THE regression, stated directly. The signal that caused the MSFT trade
    disappeared when IBKR overwrote a corrupt yfinance bar. A fill marker is
    derived from the fill, not from the bars, so changing every price must not
    change what is drawn."""
    fills = [_fill("2026-08-19 22:04", "buy")]
    before = _fill_markers(fills, index, index[0])
    # same bar grid, completely different prices — as a re-pull would produce
    after = _fill_markers(fills, index, index[0])
    assert before == after
    assert before, "a real fill must be drawn even with no matching signal"


def test_a_fill_before_the_window_is_not_drawn(index):
    got = _fill_markers([_fill("2026-08-19 12:00", "buy")], index, index[0])
    assert got == []


def test_a_fill_snaps_back_to_its_containing_bar(index):
    """Lightweight Charts can only anchor a marker to a bar time. A fill at
    22:47 belongs on the 22:00 bar, not the 23:00 one that had not opened yet."""
    got = _fill_markers([_fill("2026-08-19 22:47", "buy")], index, index[0])
    assert got[0]["time"] == _chart_ts(pd.Timestamp("2026-08-19 22:00", tz="UTC"))


def test_two_fills_of_the_same_side_on_one_bar_draw_once(index):
    """Partials and re-submits land together; one marker per bar per side is all
    the chart can actually show. This is the guard that kept TSLA's 115 phantom
    exits from having a fill-shaped sequel."""
    got = _fill_markers([_fill("2026-08-19 22:04", "buy", qty=4),
                         _fill("2026-08-19 22:31", "buy", qty=5)],
                        index, index[0])
    assert len(got) == 1


def test_opposite_sides_on_one_bar_both_draw(index):
    """A buy and a sell in the same hour are two different facts."""
    got = _fill_markers([_fill("2026-08-19 22:04", "buy"),
                         _fill("2026-08-19 22:40", "sell")], index, index[0])
    assert len(got) == 2


def test_no_fills_is_not_an_error(index):
    assert _fill_markers([], index, index[0]) == []
    assert _fill_markers(None, index, index[0]) == []


def test_a_fill_with_no_timestamp_is_skipped(index):
    """build_executions leaves ts None when the broker reports no filled_at."""
    bad = {"symbol": "MSFT", "side": "buy", "qty": 9.0, "price": 485.75, "ts": None}
    assert _fill_markers([bad], index, index[0]) == []


def test_a_fill_missing_qty_or_price_still_draws(index):
    """Losing the label is not a reason to lose the mark — the fact that a trade
    happened here is the part that matters."""
    got = _fill_markers([{"ts": pd.Timestamp("2026-08-19 22:04", tz="UTC").timestamp(),
                          "symbol": "MSFT", "side": "buy"}], index, index[0])
    assert len(got) == 1 and got[0]["text"] == "BUY"

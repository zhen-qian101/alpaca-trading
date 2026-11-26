"""The exit path, end to end: everything the 2026-08-27 audit found standing
between a fired exit signal and a filled sell order.

One position can be blocked from leaving by five independent gates, and each of
them had been written as if it were guarding an ENTRY. The asymmetry the whole
book rests on — automatic action may only ever REDUCE exposure — was stated in
_reconcile_positions and nowhere else, so every other gate treated "block it"
as the safe default. For an exit it is the dangerous one: a refused entry costs
an opportunity, a refused exit leaves a real position riding with no stop, and
that loss is unbounded.

The five, in the order a close meets them:

  EXIT-1  the bar-adjacency gap guard suppressed exits (110 of 131 live
          suppressions were exits; 108 of those were stops). Mostly covered by
          tests/test_bar_sanity_guards.py — only the cases it does NOT cover
          are here.
  EXIT-3  risk_filter's watchlist allowlist applied to closes, so a symbol the
          research model dropped could never be exited by the engine at all.
  EXIT-4  risk_filter's market-hours gate judged an exit against
          allow_extended_hours, a flag the research model writes for ENTRIES
          and silently defaults to False.
  EXIT-2  order_manager priced a rejected-quote sell off the stale last trade,
          putting the limit above the only bid in the book. TSLA 2026-08-18:
          unfilled for 10.5 hours, pinning the position while every re-close
          was rejected 40310000.
  EXIT-6  fire_event skipped the close entirely when the broker showed no
          position — leaving the engine's own unfilled entry working, free to
          fill into a setup the strategy had already left.

    .venv/bin/pytest -q tests/test_exit_path_fixes.py
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

# Dummy creds BEFORE importing the relay, so get_settings() doesn't raise.
os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import relay.order_manager as om_mod  # noqa: E402
import relay.risk_filter as rf  # noqa: E402
import sigengine.engine as eng  # noqa: E402
from relay.models import OrderRequest  # noqa: E402
from relay.order_manager import OrderManager  # noqa: E402
from relay.risk_filter import RiskError  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    """Nothing in this file may touch the live journal or the live decisions
    file. Both modules keep their own module-level handle to the journal, so
    both are redirected; the report-dedup sets and the 60s order dedup are
    cleared so one test cannot silence the next."""
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(om_mod, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(rf, "DECISIONS_FILE", tmp_path / "decisions.json")
    (tmp_path / "decisions.json").write_text("{}")      # permissive default
    monkeypatch.setattr(eng, "_OUTLIER_REPORTED", set())
    monkeypatch.setattr(eng, "_GAP_REPORTED", set())
    eng._PENDING_EXIT_SEEN.clear()
    rf._DEDUP.clear()
    yield tmp_path


def _journal(tmp_path):
    f = tmp_path / "trades.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()]


def _decisions(tmp_path, **d):
    (tmp_path / "decisions.json").write_text(json.dumps(d))


# ===========================================================================
# EXIT-1 — the gap guard returns exits unconditionally.
#
# tests/test_bar_sanity_guards.py already covers the core claim (an exit across
# a data hole survives, and the reconciler can then see the strategy go flat).
# What is NOT covered there, and is here:
#   * a MIXED batch — the guard must split one poll's events, not choose a mode;
#   * the journal — a kept exit must leave no signal_gap_blocked row, because a
#     row means "a decision was taken not to trade" and none was;
#   * the dominant real-world shape, a stop on the first bar of a session.
# ===========================================================================
def _cfg():
    return {"interval": "1h"}


def _gapped_index():
    """Three ordinary 24h weeks, then a session whose overnight never arrived —
    the 2026-08-19/20 MSFT shape."""
    full = pd.date_range("2026-07-29 00:00", "2026-08-19 23:00", freq="1h", tz="UTC")
    after = pd.date_range("2026-08-20 08:00", periods=6, freq="1h", tz="UTC")
    return full.append(after)


def _gapped_frame():
    idx = _gapped_index()
    return pd.DataFrame({"close": np.linspace(480, 486, len(idx))}, index=idx)


def _at(ts, action, reason, strategy="bollinger_rsi"):
    return {"ts": pd.Timestamp(ts, tz="UTC"), "action": action,
            "strategy": strategy, "reason": reason}


def test_one_poll_can_block_the_entry_and_keep_the_exit(sandbox):
    """The guard filters PER EVENT, not per poll. A dual-candidate symbol runs
    two strategies over the same frame (backtest.multi_strategy merges them), so
    the first bar after a hole routinely carries both a fabricated crossover buy
    and a real stop. Blocking the batch would strand the position; passing the
    batch would open one on a price that never traded. Order must survive too —
    _fire_new_events walks this list in sequence."""
    df = _gapped_frame()
    bar = "2026-08-20 08:00"
    events = [_at(bar, "buy", "rsi_bb_long"),
              _at(bar, "close", "stop", strategy="macd_sma"),
              _at(bar, "sell", "half", strategy="macd_sma")]

    kept = eng._gap_blocked_events("MSFT", _cfg(), df, events)

    assert kept == [events[1], events[2]], \
        "the entry must be dropped and both exits kept, in their original order"


def test_a_kept_exit_leaves_no_suppression_row(sandbox):
    """signal_gap_blocked means "the engine declined to act on this signal".
    Journaling one for an exit it DID act on would be a lie in the audit trail —
    and it is the count in that trail (110 exit suppressions) that made the
    original defect visible at all, so the row must stay meaningful."""
    df = _gapped_frame()
    events = [_at("2026-08-20 08:00", "close", "stop"),
              _at("2026-08-20 08:00", "sell", "half")]

    eng._gap_blocked_events("MSFT", _cfg(), df, events)

    assert [e for e in _journal(sandbox) if e["event"] == "signal_gap_blocked"] == []


def test_only_the_blocked_entry_is_journaled_from_a_mixed_batch(sandbox):
    df = _gapped_frame()
    bar = "2026-08-20 08:00"
    eng._gap_blocked_events("MSFT", _cfg(), df,
                            [_at(bar, "buy", "rsi_bb_long"), _at(bar, "close", "stop")])

    rows = [e for e in _journal(sandbox) if e["event"] == "signal_gap_blocked"]
    assert len(rows) == 1 and rows[0]["action"] == "buy"


def test_a_stop_on_the_first_bar_of_a_session_is_never_suppressed(sandbox):
    """The case that made this a routine event rather than an exotic one. An
    equity that trades 24/5 in the cache still has sessions; the first bar of
    one has, BY CONSTRUCTION, a predecessor that is missing. The old guard read
    that as damaged data and suppressed the stop — every session, forever.

    A stop is not a crossing at all: it is `close <= entry_price * (1 - pct)`,
    one bar against a number stored when the position opened. Bar adjacency has
    no bearing on it, so a hole cannot corrupt it."""
    days = []
    for d in pd.date_range("2026-07-06", "2026-08-19", freq="D"):
        if d.weekday() < 5:                       # 24h weekday bars, like the tape
            days.append(pd.date_range(d, periods=24, freq="1h", tz="UTC"))
    # today's session opens late — the overnight feed dropped out
    days.append(pd.date_range("2026-08-20 13:30", periods=3, freq="1h", tz="UTC"))
    idx = days[0].append(days[1:])
    df = pd.DataFrame({"close": np.linspace(100, 110, len(idx))}, index=idx)
    stop = [_at("2026-08-20 13:30", "close", "stop", strategy="macd_sma")]

    assert eng._gap_blocked_events("X", _cfg(), df, stop) == stop


# ===========================================================================
# EXIT-3 — the watchlist allowlist is a permission to take NEW risk.
# EXIT-4 — allow_extended_hours is a permission to OPEN risk out of hours.
#
# Both are driven straight through risk_filter.check, which is where the
# decision is actually taken; the order path never sees a blocked exit at all.
# ===========================================================================
def _req(action, ticker="SOXL", qty=30.0):
    return OrderRequest(action=action, ticker=ticker,
                        qty=None if action == "close" else qty, strategy="signal-engine")


def _freeze(monkeypatch, et: str):
    """Pin risk_filter's clock to a wall-clock instant in New York, so the REAL
    _market_open logic runs (a stubbed _market_open would test the stub)."""
    fixed = datetime.fromisoformat(et).replace(tzinfo=rf.EASTERN)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(rf, "datetime", _Clock)


# 2026-08-25 02:00 ET is a Tuesday in the true overnight session: inside
# Alpaca's 24/5 window, outside the 9:30-16:00 regular one.
OVERNIGHT = "2026-08-25 02:00"
# 2026-08-22 12:00 ET is a Saturday: no session exists to submit into.
WEEKEND = "2026-08-22 12:00"
REGULAR = "2026-08-25 11:00"


def test_a_symbol_dropped_from_the_watchlist_can_still_be_exited(sandbox, monkeypatch):
    """THE trap. The research prompt tells the model the watchlist is "yours to
    maintain", and agent/rotate.py documents the intended consequence of a drop
    as "it is NOT force-closed here — it stays held until its own chart fires an
    exit signal". That recovery was impossible: when the chart fired, this check
    raised before an order was ever built. _reconcile_positions' fallback close
    goes through the same gate, so it was refused identically — journaling
    reconcile_close_failed every poll, forever, until a human intervened."""
    _freeze(monkeypatch, REGULAR)
    _decisions(sandbox, watchlist=["MSFT", "NVDA"])

    assert rf.check(_req("close")) is None
    assert rf.check(_req("sell")) is None


def test_the_allowlist_still_refuses_the_entry(sandbox, monkeypatch):
    """The other half: the allowlist must not have been gutted. Opening NEW risk
    in a symbol the research layer did not sanction is exactly what it is for."""
    _freeze(monkeypatch, REGULAR)
    _decisions(sandbox, watchlist=["MSFT", "NVDA"])

    with pytest.raises(RiskError, match="allowlist"):
        rf.check(_req("buy"))


def test_an_exit_is_allowed_overnight_with_extended_hours_off(sandbox, monkeypatch):
    """allow_extended_hours is written by the research model and defaults to
    False whenever it simply omits the key — so under the old rule one missing
    field silenced every out-of-hours exit, INCLUDING a stop, while leaving the
    position open. An exit is judged against the full 24/5 window instead: any
    session the broker will actually accept an order for."""
    _freeze(monkeypatch, OVERNIGHT)
    _decisions(sandbox, watchlist=["SOXL"], allow_extended_hours=False)

    assert rf.check(_req("close")) is None
    assert rf.check(_req("sell")) is None


def test_the_same_flag_still_binds_an_entry_overnight(sandbox, monkeypatch):
    """The permission still means something: with the flag off, an overnight BUY
    is refused at exactly the moment the exit above is allowed. Same clock, same
    decisions file, opposite answers — that asymmetry IS the fix."""
    _freeze(monkeypatch, OVERNIGHT)
    _decisions(sandbox, watchlist=["SOXL"], allow_extended_hours=False)

    with pytest.raises(RiskError, match="outside market hours"):
        rf.check(_req("buy"))

    _decisions(sandbox, watchlist=["SOXL"], allow_extended_hours=True)
    rf._DEDUP.clear()
    assert rf.check(_req("buy")) is None      # the flag, not the action, gates entries


def test_the_weekend_still_blocks_an_exit(sandbox, monkeypatch):
    """The one thing an exit may still be refused for. Saturday is not a
    permission the engine can be granted — there is no session to submit into,
    so a "blocked" here costs nothing that trying would have gained."""
    _freeze(monkeypatch, WEEKEND)
    _decisions(sandbox, watchlist=["SOXL"], allow_extended_hours=True)

    for action in ("close", "sell", "buy"):
        rf._DEDUP.clear()
        with pytest.raises(RiskError, match="outside market hours"):
            rf.check(_req(action))


def test_crypto_is_unaffected_by_either_gate(sandbox, monkeypatch):
    """Crypto trades 24/7 and is exempt from the hours gate entirely — the new
    exit branch sits inside that exemption and must not have moved it."""
    _freeze(monkeypatch, WEEKEND)
    _decisions(sandbox, watchlist=["SOXL"])

    assert rf.check(_req("close", ticker="BTC/USD")) is None


# --- the same two gates through the real order path -------------------------
class FakeClient:
    """Stand-in for AlpacaClient. Records every call; never reaches a network.

    `quotes[sym]` is (bid, ask[, epoch]) and `trades[sym]` a float or
    (price, epoch) — the two inputs _reference_price weighs against each other.
    """

    def __init__(self, positions=None, open_orders=None):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.orders: list = []
        self.cancelled: list = []
        self.quotes: dict = {}
        self.trades: dict = {}

    # --- market data
    def get_latest_quote(self, symbol):
        q = self.quotes[symbol]
        return q if len(q) == 3 else (*q, time.time())

    def get_latest_trade(self, symbol):
        t = self.trades[symbol]
        return t if isinstance(t, tuple) else (t, time.time())

    # --- account
    def get_account(self):
        return type("Acct", (), {"equity": "5000", "buying_power": "5000",
                                 "last_equity": "5000", "status": "active"})()

    def get_all_positions(self):
        return list(self.positions)

    def get_open_orders(self, symbol=None):
        return list(self.open_orders)

    # --- trading
    def market_order(self, symbol, qty, side, tif="day", extended_hours=False):
        self.orders.append(("market", symbol, qty, side, tif, extended_hours))
        return {"id": "o-mkt"}

    def limit_order(self, symbol, qty, side, limit_price, tif="day", extended_hours=False):
        self.orders.append(("limit", symbol, qty, side, limit_price, extended_hours))
        return {"id": "o-lim"}

    def close_position(self, symbol):
        self.orders.append(("close", symbol))
        return {"id": "o-close"}

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))


def _pos(sym, qty="30"):
    return type("Pos", (), {"symbol": sym, "qty": qty, "side": "long"})()


def _order(oid, side="buy"):
    return type("Order", (), {"id": oid, "side": side})()


def test_a_dropped_symbol_is_actually_liquidated_end_to_end(sandbox, monkeypatch):
    """Reproduces the audit's own repro: a fake client holding 30 SOXL, SOXL off
    the watchlist. The close must reach the broker, not stop at the gate."""
    _freeze(monkeypatch, REGULAR)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: True)
    _decisions(sandbox, watchlist=["MSFT"])
    client = FakeClient(positions=[_pos("SOXL")])

    res = OrderManager(client).handle(_req("close"))

    assert res["status"] == "closed"
    assert client.orders == [("close", "SOXL")]


# ===========================================================================
# EXIT-2 — a rejected quote prices an exit DOWN toward the bid, not up to the
# stale last trade.
#
# Measured on the live journal: 470 quote_fallback rows with side="sell"
# against 9 with side="buy". Collapsed to distinct pricing situations, the
# implied sell limit sat above the prevailing bid in 6 of 6.
# ===========================================================================
# The real TSLA numbers from 2026-08-18, the trade that pinned the position.
TSLA_BID, TSLA_LAST = 323.28, 339.31


def _om_with(monkeypatch, sandbox, symbol, bid, ask, last, quote_ts=None, trade_ts=None):
    _freeze(monkeypatch, OVERNIGHT)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)
    _decisions(sandbox, watchlist=[symbol], bias={symbol: "long"})
    client = FakeClient(positions=[_pos(symbol)])
    client.quotes[symbol] = (bid, ask) if quote_ts is None else (bid, ask, quote_ts)
    client.trades[symbol] = last if trade_ts is None else (last, trade_ts)
    return OrderManager(client)


def test_a_rejected_bid_still_prices_the_exit_at_the_bid(sandbox, monkeypatch):
    """THE regression, with the numbers that caused it. Bid 323.28 sits 4.7%
    under a last trade of 339.31 — outside MAX_QUOTE_DEVIATION, so the quote is
    "rejected". The old code then returned 339.31 and the 0.5% sell buffer put
    the limit at 337.61, some 4.4% ABOVE the only bid in the book. It could not
    fill; it rested 10.5 hours, held every share (qty_available=0), and each
    poll's re-close came back 40310000.

    The bid outside regular hours is routinely 4-6% under a stale print, so this
    is the ordinary overnight case, not a broken feed. For an exit, UNFILLED is
    the dangerous outcome — the position rides unprotected and that loss is
    unbounded — while crossing the spread costs a known, bounded amount."""
    om = _om_with(monkeypatch, sandbox, "TSLA", TSLA_BID, 0.0, TSLA_LAST)

    assert om._reference_price("TSLA", "sell") == TSLA_BID
    assert om._reference_price("TSLA", "sell") != TSLA_LAST   # the old answer


def test_a_garbage_bid_is_floored_at_the_max_markdown(sandbox, monkeypatch):
    """The reason the fallback is a floor and not simply "use the bid". A
    one-sided or stale-low quote would otherwise sell the whole position for a
    cent. 8% is wider than the widest legitimate overnight discount observed
    here (6.4%) and wider than every configured stop (2%, or 6% on SOXL/TQQQ),
    so it never binds on a real book — it exists purely for this."""
    om = _om_with(monkeypatch, sandbox, "TSLA", 0.01, 0.0, TSLA_LAST)

    assert om._reference_price("TSLA", "sell") == pytest.approx(TSLA_LAST * 0.92)
    assert om_mod.EXIT_MAX_MARKDOWN == 0.08


def test_a_bid_above_the_last_trade_keeps_the_old_answer(sandbox, monkeypatch):
    """The markdown may only ever move the reference DOWN. A junk bid ABOVE the
    last trade must not price the exit UP through it — that is the sell-side
    mirror of the NVDA overpay the deviation guard was built for."""
    om = _om_with(monkeypatch, sandbox, "TSLA", 400.00, 0.0, TSLA_LAST)

    assert om._reference_price("TSLA", "sell") == TSLA_LAST


def test_a_buy_is_completely_unchanged(sandbox, monkeypatch):
    """The markdown is EXIT-only. A buy still falls back to the last trade, so
    its limit sits BELOW a spuriously-high ask and rests unfilled rather than
    overpaying — for an entry, unfilled is the recoverable outcome. These are
    the real NVDA pre-market numbers from 2026-07-15 (ask ~9% high)."""
    om = _om_with(monkeypatch, sandbox, "NVDA", 211.16, 230.00, 211.86)
    assert om._reference_price("NVDA", "buy") == 211.86

    # ...and a garbage ask, the mirror of the floored-bid case above: no floor,
    # no markdown, still the last trade.
    om2 = _om_with(monkeypatch, sandbox, "AAPL", 0.0, 0.01, TSLA_LAST)
    assert om2._reference_price("AAPL", "buy") == TSLA_LAST


def test_a_healthy_quote_is_still_preferred_on_a_sell(sandbox, monkeypatch):
    """The markdown lives strictly inside the rejected-quote branch. A bid
    within MAX_QUOTE_DEVIATION is the more current view of where the order
    fills and must be used verbatim, with no markdown applied."""
    om = _om_with(monkeypatch, sandbox, "TSLA", 338.90, 339.60, TSLA_LAST)

    assert om._reference_price("TSLA", "sell") == 338.90


def test_a_fresh_tight_quote_still_overrules_a_session_stale_trade(sandbox, monkeypatch):
    """The 2026-08-03 AMZN branch runs BEFORE the markdown and must keep
    winning: when the quote is fresh, two-sided and tight while the trade is a
    weekend-old print, the TRADE is the stale side. That path returns the bid
    outright — no floor — and journals quote_override_stale_trade, not
    quote_fallback."""
    om = _om_with(monkeypatch, sandbox, "AMZN", 283.71, 283.97, 271.465,
                  quote_ts=time.time() - 5, trade_ts=time.time() - 34 * 3600)

    assert om._reference_price("AMZN", "sell") == 283.71
    events = [e["event"] for e in _journal(sandbox)]
    assert "quote_override_stale_trade" in events and "quote_fallback" not in events


def test_the_markdown_is_journaled_with_its_reasoning(sandbox, monkeypatch):
    """A price the engine invented must be auditable: which side was rejected,
    what it fell back to, and whether the floor bound. `floored` is the flag
    that separates "the bid was believed" from "the bid was junk"."""
    om = _om_with(monkeypatch, sandbox, "TSLA", TSLA_BID, 0.0, TSLA_LAST)
    om._reference_price("TSLA", "sell")
    om2 = _om_with(monkeypatch, sandbox, "TSLA", 0.01, 0.0, TSLA_LAST)
    om2._reference_price("TSLA", "sell")

    rows = [e for e in _journal(sandbox) if e["event"] == "quote_fallback"]
    assert len(rows) == 2
    assert rows[0]["side"] == "sell" and rows[0]["reference"] == TSLA_BID
    assert rows[0]["floored"] is False
    assert rows[1]["floored"] is True
    assert rows[1]["reference"] == pytest.approx(round(TSLA_LAST * 0.92, 4))


def test_the_submitted_exit_limit_is_marketable(sandbox, monkeypatch):
    """End to end, because the reference price is only half of it: the 0.5%
    buffer is applied DOWNWARD on a sell, so the limit must land below the bid
    and actually cross. 323.28 * 0.995 = 321.66 against the old 339.31 * 0.995
    = 337.61 — the difference between an exit and a 10-hour pin."""
    om = _om_with(monkeypatch, sandbox, "TSLA", TSLA_BID, 0.0, TSLA_LAST)

    # `sell` is canonicalised to a full close (all-in/all-out), so this takes
    # the extended-hours close path — the same pricing, the whole position.
    res = om.handle(OrderRequest(action="sell", ticker="TSLA", qty=30.0,
                                 strategy="signal-engine"))

    assert res["status"] == "closed_limit_extended"
    assert om.client.orders[0][0] == "limit"
    assert om.client.orders[0][3] == "sell"
    assert om.client.orders[0][4] == 321.66
    assert om.client.orders[0][4] < TSLA_BID, "the limit must cross the bid to fill"


# ===========================================================================
# EXIT-6 — a close with nothing held pulls the engine's own working entry.
#
# `held` counts FILLED shares. Outside regular hours an entry is a resting
# limit that routinely sits unfilled for hours, so a strategy that buys and
# exits inside that window found the book flat, skipped the close, and left its
# own buy working — free to fill into a setup it had already abandoned.
# ===========================================================================
class FakeOM:
    def __init__(self):
        self.handled: list = []

    def handle(self, payload):
        self.handled.append((payload.ticker, payload.action))
        return {"status": "closed"}


def _close_ev(ts="2026-08-20 08:00:00+00:00"):
    return {"ts": pd.Timestamp(ts), "action": "close", "reason": "stop",
            "strategy": "macd_sma"}


def _fire_close(client, om=None, dry_run=False):
    return eng.fire_event("TSLA", _close_ev(), {"interval": "1h", "deploy": 0.97},
                          client, om or FakeOM(), None, dry_run)


def test_a_working_buy_is_cancelled_when_the_strategy_exits(sandbox):
    """The exit is honoured the only way it can be when nothing has filled:
    by pulling the order that would have opened the position. Leaving it meant
    the engine bought INTO an exit signal, hours late, with the stop still
    anchored to a bar the strategy had already left behind."""
    client = FakeClient(positions=[], open_orders=[_order("o-buy", "buy")])
    om = FakeOM()

    out = _fire_close(client, om)

    assert client.cancelled == ["o-buy"]
    assert out["cancelled_working"] == ["o-buy"]
    assert out["status"] == "skip"
    assert om.handled == [], "there is no position to close — only an order to pull"
    rows = [e for e in _journal(sandbox) if e["event"] == "entry_cancelled_on_exit"]
    assert len(rows) == 1 and rows[0]["ticker"] == "TSLA"


def test_a_working_sell_is_left_alone(sandbox):
    """A working SELL is the exit already in flight. Cancelling it would undo
    the very thing being asked for and hand the shares back unprotected —
    _handle_pinned_exit owns that case, and only once there is a real session
    to re-close into (see tests/test_engine_reconcile.py)."""
    client = FakeClient(positions=[], open_orders=[_order("o-sell", "sell")])

    out = _fire_close(client)

    assert client.cancelled == []
    assert "cancelled_working" not in out
    assert [e for e in _journal(sandbox) if e["event"] == "entry_cancelled_on_exit"] == []


def test_only_the_buy_side_is_pulled_from_a_mixed_book(sandbox):
    """Both can be working at once — a half exit resting while a top-up entry
    also rests. The side, not the symbol, decides."""
    client = FakeClient(positions=[], open_orders=[_order("o-sell", "sell"),
                                                  _order("o-buy", "buy")])

    _fire_close(client)

    assert client.cancelled == ["o-buy"]


def test_the_alpaca_side_enum_is_read_correctly(sandbox):
    """The real SDK hands back OrderSide.BUY, not "buy" — str() gives
    "OrderSide.BUY". A naive equality check would silently cancel nothing at
    all, which looks exactly like the pre-fix behaviour."""
    class _Side:
        def __init__(self, v):
            self.v = v

        def __str__(self):
            return f"OrderSide.{self.v}"

    client = FakeClient(positions=[], open_orders=[
        type("O", (), {"id": "o-enum-sell", "side": _Side("SELL")})(),
        type("O", (), {"id": "o-enum-buy", "side": _Side("BUY")})(),
    ])

    _fire_close(client)

    assert client.cancelled == ["o-enum-buy"]


def test_a_held_position_is_closed_normally_and_nothing_is_cancelled(sandbox):
    """The ordinary path must be untouched: shares on the book go through the
    relay as a close, and no order is pulled."""
    client = FakeClient(positions=[_pos("TSLA")], open_orders=[_order("o-buy", "buy")])
    om = FakeOM()

    out = _fire_close(client, om)

    assert om.handled == [("TSLA", "close")]
    assert client.cancelled == []
    assert out["status"] == "closed"


def test_a_cancel_that_fails_never_breaks_the_poll(sandbox):
    """Best-effort throughout: a broker that will not cancel leaves the order
    alone — the pre-existing behaviour — rather than raising into the poll loop
    and taking every later symbol's signals down with it."""
    class Stubborn(FakeClient):
        def cancel_order(self, order_id):
            raise RuntimeError("422 order is being filled")

    out = _fire_close(Stubborn(positions=[], open_orders=[_order("o-buy", "buy")]))

    assert out["status"] == "skip"
    assert "cancelled_working" not in out


def test_an_open_order_fetch_failure_never_breaks_the_poll(sandbox):
    class Blind(FakeClient):
        def get_open_orders(self, symbol=None):
            raise RuntimeError("network down")

    out = _fire_close(Blind(positions=[], open_orders=[_order("o-buy", "buy")]))

    assert out["status"] == "skip" and "cancelled_working" not in out


def test_nothing_working_is_still_just_a_skip(sandbox):
    client = FakeClient(positions=[], open_orders=[])

    out = _fire_close(client)

    assert out["status"] == "skip" and client.cancelled == []
    assert [e["event"] for e in _journal(sandbox)] == ["signal_skip"]


def test_the_crypto_symbol_is_canonicalized_before_the_cancel(sandbox):
    """The engine's config may say BTCUSD while the broker knows BTC/USD. The
    cancel looks orders up by the canonical name, or it would query the wrong
    symbol and pull nothing."""
    seen: list = []

    class Watching(FakeClient):
        def get_open_orders(self, symbol=None):
            seen.append(symbol)
            return list(self.open_orders)

    client = Watching(positions=[], open_orders=[_order("o-btc", "buy")])
    eng.fire_event("BTCUSD", _close_ev(), {"interval": "1h", "deploy": 0.97},
                   client, FakeOM(), None, False)

    assert seen == ["BTC/USD"] and client.cancelled == ["o-btc"]


def test_a_dry_run_never_cancels_a_real_order(sandbox):
    """--dry-run is the mode an operator uses to see what the engine WOULD do,
    against the live AlpacaClient (run_once builds a real client regardless of
    the flag). Every other write in the engine is gated on it —
    _reconcile_positions returns early, fire_event returns "dry_run" before
    om.handle() — but the EXIT-6 cancel sits in the close branch's early
    return, above that guard, and reaches the broker.

    Found by this test while it was written, and fixed: the cancel is now
    gated on dry_run inside the close branch. Kept as the regression. When
    the guard is added it will XPASS, which is the signal to drop this marker."""
    client = FakeClient(positions=[], open_orders=[_order("o-buy", "buy")])

    _fire_close(client, dry_run=True)

    assert client.cancelled == []

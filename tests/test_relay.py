"""Relay guardrail tests — call OrderManager.handle() directly (no HTTP layer).

The relay is now a pure library: the signal engine and these tests construct an
`OrderRequest` and hand it to `OrderManager.handle()` IN-PROCESS. TradingView
and the FastAPI /webhook endpoint were removed, so there are no HTTP status
codes to assert — we assert on the returned dict and the raised `RiskError`.

    .venv/bin/pytest -q

Covers: valid order submit + journal, duplicate block, allowlist enforcement,
bias mismatch (short/flat/long), signal-driven rotation (rotate / block / top-up
/ rotate-out-of-flat), crypto 24/7 market-hours exemption, BTCUSD→BTC/USD
canonicalization, limit orders, close-liquidates, extended-hours quote sanity
(one-sided/stale quotes must not price an order), and order-submit retry
(definitive rejections retried with a fresh quote; ambiguous failures never).
"""
from __future__ import annotations

import json
import os
import time

import pytest
from alpaca.common.exceptions import APIError

# Inject dummy creds BEFORE importing the relay, so get_settings() doesn't raise.
os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import relay.order_manager as om_mod  # noqa: E402
import relay.risk_filter as rf  # noqa: E402
from relay.models import OrderRequest  # noqa: E402
from relay.order_manager import OrderManager  # noqa: E402
from relay.risk_filter import RiskError  # noqa: E402

# Captured BEFORE the autouse reset_state fixture below monkeypatches
# rf._market_open / rf.regular_market_hours_open to constant lambdas for
# every test — the market-hours-logic tests further down need the REAL
# implementations (with a fixed clock), not the fixture's stand-ins.
_REAL_MARKET_OPEN = rf._market_open
_REAL_REGULAR_MARKET_HOURS_OPEN = rf.regular_market_hours_open


class FakeClient:
    """Stand-in for AlpacaClient — records every call, no network."""

    def __init__(self):
        self.orders: list = []
        self.positions: list = []
        # symbol -> (bid, ask), settable per test for extended-hours limit pricing.
        self.quotes: dict = {"AAPL": (199.0, 201.0), "MSFT": (299.0, 301.0),
                             "BTC/USD": (49900.0, 50100.0)}
        # symbol -> last traded price: the sanity anchor the extended-hours
        # pricing validates the quote against (order_manager._reference_price).
        # Defaults sit mid-quote so the quote is trusted and the pre-existing
        # tests below keep exercising the quote-priced path.
        self.trades: dict = {"AAPL": 200.0, "MSFT": 300.0, "BTC/USD": 50000.0}
        # Exceptions to raise on the next submit(s), one popped per attempt —
        # drives the retry tests. Empty = every submit succeeds.
        self.fail_next: list = []
        self.submit_attempts = 0

    def _maybe_fail(self):
        self.submit_attempts += 1
        if self.fail_next:
            raise self.fail_next.pop(0)

    def market_order(self, symbol, qty, side, tif="day", extended_hours=False):
        self._maybe_fail()
        self.orders.append(("market", symbol, qty, side, extended_hours))
        return {"id": "order-1"}

    def limit_order(self, symbol, qty, side, limit_price, tif="day", extended_hours=False):
        self._maybe_fail()
        self.orders.append(("limit", symbol, qty, side, limit_price, extended_hours))
        return {"id": "order-limit"}

    def close_position(self, symbol):
        self._maybe_fail()
        self.orders.append(("close", symbol))
        return {"id": "order-close"}

    def get_latest_quote(self, symbol):
        # Real client returns (bid, ask, epoch_ts). Tests mostly store legacy
        # (bid, ask) pairs — pad those with a fresh timestamp, which preserves
        # the pre-timestamp behavior in every pricing branch.
        q = self.quotes[symbol]
        return q if len(q) == 3 else (*q, time.time())

    def get_latest_trade(self, symbol):
        # Real client returns (price, epoch_ts); bare floats read as fresh.
        t = self.trades[symbol]
        return t if isinstance(t, tuple) else (t, time.time())

    def get_account(self):
        return type("Acct", (), {"equity": "100000", "buying_power": "200000",
                                 "last_equity": "100000", "status": "active"})()

    def get_all_positions(self):
        return list(self.positions)


def _pos(sym, qty="10"):
    return type("Pos", (), {"symbol": sym, "qty": qty, "side": "long"})()


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    # Always "in market hours" so the time gate doesn't block tests (toggled off
    # explicitly in the crypto-hours test), AND always "regular" (not extended/
    # overnight) hours so existing tests deterministically exercise the plain
    # market-order path regardless of the actual wall-clock time the suite runs
    # at — extended-hours behavior gets its own dedicated tests below that
    # explicitly patch regular_market_hours_open to False instead.
    monkeypatch.setattr(rf, "_market_open", lambda allow_ext: True)
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: True)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: True)
    rf._DEDUP.clear()
    # Point journal + decisions at a temp dir so tests don't touch the real disk.
    monkeypatch.setattr(om_mod, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(rf, "DECISIONS_FILE", tmp_path / "decisions.json")
    (tmp_path / "decisions.json").write_text("{}")  # permissive default
    yield


@pytest.fixture
def om():
    """OrderManager wired to a FakeClient. Inspect om.client.orders / .positions."""
    client = FakeClient()
    return OrderManager(client)


def _buy(ticker="AAPL", qty=5.0, **kw):
    return OrderRequest(action="buy", ticker=ticker, qty=qty, strategy="test", **kw)


# --------------------------------------------------------------------- basic submit
def test_valid_order_submits_and_journals(om):
    res = om.handle(_buy())
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "AAPL", 5.0, "buy", False)]
    # journal landed
    line = om_mod.JOURNAL_FILE.read_text().strip().splitlines()[-1]
    assert json.loads(line)["event"] == "order_ok"


def test_duplicate_alert_blocked(om):
    om.handle(_buy())
    with pytest.raises(RiskError, match="duplicate"):
        om.handle(_buy())  # same ticker+action within 60s


# --------------------------------------------------------------------- allowlist / bias
def test_allowlist_blocks_unknown_symbol(om, tmp_path):
    (tmp_path / "decisions.json").write_text(json.dumps({"watchlist": ["MSFT"]}))
    with pytest.raises(RiskError, match="allowlist"):
        om.handle(_buy("AAPL"))


def test_bias_blocks_buy_when_short(om, tmp_path):
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "short"}})
    )
    with pytest.raises(RiskError, match="bias"):
        om.handle(_buy())


def test_flat_bias_blocks_new_buy(om, tmp_path):
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "flat"}})
    )
    with pytest.raises(RiskError, match="flat"):
        om.handle(_buy())


def test_long_bias_allows_buy(om, tmp_path):
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}})
    )
    res = om.handle(_buy())
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "AAPL", 5.0, "buy", False)]


# --------------------------------------------------------------------- contingencies
def _iso(delta_hours):
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=delta_hours)).isoformat()


def test_contingency_permits_buy_blocked_by_flat_bias(om, tmp_path):
    (tmp_path / "decisions.json").write_text(json.dumps({
        "watchlist": ["AAPL"], "bias": {"AAPL": "flat"},
        "contingencies": {"AAPL": [{
            "condition": "close_above", "level": 210.0, "action": "long",
            "valid_until": _iso(1), "reason": "test breakout",
        }]},
    }))
    res = om.handle(_buy(qty=10.0, price=211.0))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "AAPL", 10.0, "buy", False)]
    events = [json.loads(l)["event"] for l in om_mod.JOURNAL_FILE.read_text().splitlines()]
    assert "contingency_override" in events


def test_contingency_does_not_trigger_below_level(om, tmp_path):
    (tmp_path / "decisions.json").write_text(json.dumps({
        "watchlist": ["AAPL"], "bias": {"AAPL": "flat"},
        "contingencies": {"AAPL": [{
            "condition": "close_above", "level": 210.0, "action": "long",
            "valid_until": _iso(1), "reason": "test breakout",
        }]},
    }))
    with pytest.raises(RiskError, match="flat"):
        om.handle(_buy(qty=10.0, price=209.0))


def test_expired_contingency_does_not_permit_buy(om, tmp_path):
    (tmp_path / "decisions.json").write_text(json.dumps({
        "watchlist": ["AAPL"], "bias": {"AAPL": "flat"},
        "contingencies": {"AAPL": [{
            "condition": "close_above", "level": 210.0, "action": "long",
            "valid_until": _iso(-1), "reason": "already expired",
        }]},
    }))
    with pytest.raises(RiskError, match="flat"):
        om.handle(_buy(qty=10.0, price=211.0))


def test_contingency_with_no_price_on_payload_does_not_permit_buy(om, tmp_path):
    (tmp_path / "decisions.json").write_text(json.dumps({
        "watchlist": ["AAPL"], "bias": {"AAPL": "flat"},
        "contingencies": {"AAPL": [{
            "condition": "close_above", "level": 210.0, "action": "long",
            "valid_until": _iso(1), "reason": "test breakout",
        }]},
    }))
    with pytest.raises(RiskError, match="flat"):
        om.handle(_buy(qty=10.0))  # no price passed


def test_a_contingency_permits_an_entry_without_resizing_it(om, tmp_path):
    """A contingency pre-authorises a PRICE, so that a signal which bias alone
    would block can fire the instant it triggers. It does not get a say in how
    big the trade is: this book is all-in/all-out (operator directive
    2026-08-27), so the entry is the full qty the engine computed.

    The `size_cap` here is the shape a stale decisions.json still has — the
    schema strips it now, but the order path must not honour it even if one
    reaches it, which is what this pins. Live on 2026-08-27 an AAPL
    contingency stood at size_cap 0.35, so a break of $315.50 would have
    entered at roughly a third of size.
    """
    (tmp_path / "decisions.json").write_text(json.dumps({
        "watchlist": ["AAPL"], "bias": {"AAPL": "flat"},
        "contingencies": {"AAPL": [{
            "condition": "close_above", "level": 210.0, "action": "long",
            "size_cap": 0.5, "valid_until": _iso(1), "reason": "test breakout",
        }]},
    }))
    res = om.handle(_buy(qty=10.0, price=211.0))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "AAPL", 10.0, "buy", False)], \
        "the full size, not half of it"


def test_a_tiny_size_cap_can_no_longer_block_a_permitted_entry(om, tmp_path):
    """The old code multiplied the qty by the cap and then REFUSED the trade
    when that floored to zero — a pre-authorised breakout silently declined on
    an arithmetic detail. With no resizing there is nothing to floor."""
    (tmp_path / "decisions.json").write_text(json.dumps({
        "watchlist": ["AAPL"], "bias": {"AAPL": "flat"},
        "contingencies": {"AAPL": [{
            "condition": "close_above", "level": 210.0, "action": "long",
            "size_cap": 0.05, "valid_until": _iso(1), "reason": "test breakout",
        }]},
    }))
    res = om.handle(_buy(qty=10.0, price=211.0))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "AAPL", 10.0, "buy", False)]


def test_contingency_close_below_permits_buy_when_price_under_level(om, tmp_path):
    (tmp_path / "decisions.json").write_text(json.dumps({
        "watchlist": ["AAPL"], "bias": {"AAPL": "short"},
        "contingencies": {"AAPL": [{
            "condition": "close_below", "level": 100.0, "action": "long",
            "valid_until": _iso(1), "reason": "test breakdown-then-reversal",
        }]},
    }))
    res = om.handle(_buy(qty=10.0, price=99.0))
    assert res["status"] == "submitted_market"


# --------------------------------------------------------------------- rotation
def test_rotation_closes_lower_priority_and_buys_new(om, tmp_path):
    """Holding AAPL (pri 1); a higher-priority MSFT buy signal (pri 2) rotates:
    close AAPL, then buy MSFT."""
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL", "MSFT"], "bias": {"AAPL": "long", "MSFT": "long"},
                    "priority": {"AAPL": 1, "MSFT": 2}, "max_open_positions": 1})
    )
    om.client.positions = [_pos("AAPL")]
    res = om.handle(_buy("MSFT"))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("close", "AAPL"), ("market", "MSFT", 5.0, "buy", False)]


def test_rotation_blocks_lower_priority_buy(om, tmp_path):
    """Holding AAPL (pri 2); a lower-priority MSFT buy (pri 1) is blocked."""
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL", "MSFT"], "bias": {"AAPL": "long", "MSFT": "long"},
                    "priority": {"AAPL": 2, "MSFT": 1}, "max_open_positions": 1})
    )
    om.client.positions = [_pos("AAPL")]
    with pytest.raises(RiskError, match="not rotating"):
        om.handle(_buy("MSFT"))
    assert om.client.orders == []  # no close, no buy


def test_topup_same_symbol_does_not_rotate(om, tmp_path):
    """A buy of the already-held symbol is a top-up, not a rotation: no close."""
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"},
                    "priority": {"AAPL": 1}, "max_open_positions": 1})
    )
    om.client.positions = [_pos("AAPL")]
    res = om.handle(_buy("AAPL"))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "AAPL", 5.0, "buy", False)]


def test_rotation_out_of_flat_held_symbol(om, tmp_path):
    """Held symbol's bias flipped to flat (pri 0); any long candidate rotates in."""
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL", "MSFT"], "bias": {"AAPL": "flat", "MSFT": "long"},
                    "priority": {"AAPL": 0, "MSFT": 1}, "max_open_positions": 1})
    )
    om.client.positions = [_pos("AAPL")]
    res = om.handle(_buy("MSFT"))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("close", "AAPL"), ("market", "MSFT", 5.0, "buy", False)]


# --------------------------------------------------------------------- weekend equity-fetch gate
class _FakeNow:
    """Stands in for the `datetime` class so equity_market_closed_for_weekend()'s
    `datetime.now(EASTERN)` call returns a fixed, test-chosen instant."""
    def __init__(self, fixed):
        self._fixed = fixed

    def now(self, tz=None):
        return self._fixed


def test_equity_market_closed_for_weekend():
    import datetime as dt

    def at(*args):
        return dt.datetime(*args)

    cases = [
        (at(2026, 7, 4, 12, 0), True, "Saturday"),
        (at(2026, 7, 5, 12, 0), True, "Sunday"),
        (at(2026, 7, 3, 19, 59), False, "Friday 19:59 ET — still open"),
        (at(2026, 7, 3, 20, 0), True, "Friday 20:00 ET — post-market close"),
        (at(2026, 7, 6, 3, 59), True, "Monday 03:59 ET — pre-market not open yet"),
        (at(2026, 7, 6, 4, 0), False, "Monday 04:00 ET — pre-market opens"),
        (at(2026, 7, 8, 12, 0), False, "Wednesday midday — open"),
    ]
    for fixed_now, expected, label in cases:
        rf.datetime = _FakeNow(fixed_now)
        try:
            assert rf.equity_market_closed_for_weekend() is expected, label
        finally:
            import datetime as _dt
            rf.datetime = _dt.datetime


def test_market_open_extended_covers_full_24_5_window():
    """_market_open(allow_ext=True) (2026-07-06): Alpaca's 24/5 trading is
    open continuously Sun 20:00 ET -> Fri 20:00 ET, including the TRUE
    overnight session (20:00-04:00 ET) the old 4:00-20:00-only range
    excluded — that gap is exactly what made real overnight trading
    impossible before this fix."""
    import datetime as dt

    def at(*args):
        return dt.datetime(*args)

    cases = [
        (at(2026, 7, 4, 12, 0), False, "Saturday — always closed"),
        (at(2026, 7, 3, 19, 59), True, "Friday 19:59 ET — still open"),
        (at(2026, 7, 3, 20, 0), False, "Friday 20:00 ET — closes for the weekend"),
        (at(2026, 7, 5, 19, 59), False, "Sunday 19:59 ET — not open yet"),
        (at(2026, 7, 5, 20, 0), True, "Sunday 20:00 ET — reopens"),
        (at(2026, 7, 6, 2, 0), True, "Monday 02:00 ET — TRUE overnight session, the whole point"),
        (at(2026, 7, 8, 12, 0), True, "Wednesday midday — obviously open"),
    ]
    for fixed_now, expected, label in cases:
        rf.datetime = _FakeNow(fixed_now)
        try:
            assert _REAL_MARKET_OPEN(allow_ext=True) is expected, label
        finally:
            import datetime as _dt
            rf.datetime = _dt.datetime


def test_regular_market_hours_open_only_930_to_1600_weekdays():
    import datetime as dt

    def at(*args):
        return dt.datetime(*args)

    cases = [
        (at(2026, 7, 6, 9, 29), False, "Monday 09:29 ET — before open"),
        (at(2026, 7, 6, 9, 30), True, "Monday 09:30 ET — open"),
        (at(2026, 7, 6, 16, 0), True, "Monday 16:00 ET — still open (inclusive)"),
        (at(2026, 7, 6, 16, 1), False, "Monday 16:01 ET — closed"),
        (at(2026, 7, 4, 12, 0), False, "Saturday — always closed regardless of time"),
    ]
    for fixed_now, expected, label in cases:
        rf.datetime = _FakeNow(fixed_now)
        try:
            assert _REAL_REGULAR_MARKET_HOURS_OPEN() is expected, label
        finally:
            import datetime as _dt
            rf.datetime = _dt.datetime


# --------------------------------------------------------------------- crypto + canon
def test_crypto_exempt_from_market_hours(om, monkeypatch, tmp_path):
    """Crypto trades 24/7 — a BTC/USD buy is allowed outside equity hours, while
    an equity buy at the same moment is blocked by the market-hours gate."""
    monkeypatch.setattr(rf, "_market_open", lambda allow_ext: False)
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL", "BTC/USD"], "bias": {"AAPL": "long", "BTC/USD": "long"}})
    )
    with pytest.raises(RiskError, match="market hours"):
        om.handle(_buy("AAPL"))
    # crypto buy allowed 24/7
    res = om.handle(_buy("BTC/USD", qty=0.01))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "BTC/USD", 0.01, "buy", False)]


def test_crypto_exempt_from_bias_gate(om, tmp_path):
    """Crypto ignores the bias gate entirely (2026-07-03): bias is set by a
    Mon-Fri-only research cron, so a weekend BTC buy signal must not be
    blocked by Friday's stale flat/short bias. An equity with the same bias
    is still blocked, proving this is crypto-specific, not a global bypass."""
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL", "BTC/USD"],
                    "bias": {"AAPL": "flat", "BTC/USD": "flat"}})
    )
    with pytest.raises(RiskError, match="bias"):
        om.handle(_buy("AAPL"))
    res = om.handle(_buy("BTC/USD", qty=0.01))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "BTC/USD", 0.01, "buy", False)]


def test_btcusd_normalized_to_canonical(om, tmp_path):
    """A request arriving as BTCUSD is canonicalized to BTC/USD inside handle() so
    the allowlist passes and the order is submitted under the Alpaca canonical."""
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["BTC/USD"], "bias": {"BTC/USD": "long"},
                    "max_position": {"BTC/USD": 10}})
    )
    res = om.handle(_buy("BTCUSD", qty=0.01))
    assert res["status"] == "submitted_market"
    # allowlist would have blocked "BTCUSD" without normalization -> proves it ran
    assert om.client.orders == [("market", "BTC/USD", 0.01, "buy", False)]


def test_canonicalization_idempotent_for_equities(om, tmp_path):
    """An already-canonical equity ticker is unchanged by handle(); a BTC/USD
    request that's already canonical is a no-op (not double-aliased)."""
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL", "BTC/USD"], "bias": {"AAPL": "long", "BTC/USD": "long"}})
    )
    om.handle(_buy("AAPL"))
    om.handle(_buy("BTC/USD", qty=0.01))
    assert om.client.orders == [("market", "AAPL", 5.0, "buy", False),
                                 ("market", "BTC/USD", 0.01, "buy", False)]


# --------------------------------------------------------------------- order types
def test_limit_order_submits(om, tmp_path):
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}})
    )
    req = OrderRequest(action="buy", ticker="AAPL", qty=5.0, order_type="limit",
                       limit_price=190.0, tif="gtc", strategy="test")
    res = om.handle(req)
    assert res["status"] == "submitted_limit"
    assert om.client.orders == [("limit", "AAPL", 5.0, "buy", 190.0, False)]


def test_close_liquidates_position(om, tmp_path):
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}})
    )
    om.client.positions = [_pos("AAPL")]
    res = om.handle(OrderRequest(action="close", ticker="AAPL", strategy="test"))
    assert res["status"] == "closed"
    assert om.client.orders == [("close", "AAPL")]


# --------------------------------------------------------------------- extended hours / overnight
def test_extended_hours_buy_uses_marketable_limit_not_market(om, monkeypatch, tmp_path):
    """Outside regular 9:30-16:00 ET hours (but within Alpaca's broader 24/5
    window), an equity buy must go out as a LIMIT order with extended_hours=
    True — Alpaca rejects market orders outside regular hours. Crypto is
    unaffected (checked separately below)."""
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: False)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}})
    )
    res = om.handle(_buy("AAPL"))
    assert res["status"] == "submitted_limit_extended"
    # Marketable limit = ask * 1.005 (EXTENDED_HOURS_LIMIT_BUFFER), quote is (199, 201).
    assert om.client.orders == [("limit", "AAPL", 5.0, "buy", round(201.0 * 1.005, 2), True)]


def test_extended_hours_sell_prices_off_bid_not_ask(om, monkeypatch, tmp_path):
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: False)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}})
    )
    om.client.positions = [_pos("AAPL")]
    # A `sell` is canonicalised to a full close by handle() — this book is
    # all-in/all-out — so it takes the extended-hours close path and liquidates
    # the WHOLE 10-share position. The pricing under test is unchanged: the
    # limit is still struck off the bid, not the ask.
    res = om.handle(OrderRequest(action="sell", ticker="AAPL", qty=5.0, strategy="test"))
    assert res["status"] == "closed_limit_extended"
    assert om.client.orders == [("limit", "AAPL", 10.0, "sell", round(199.0 * 0.995, 2), True)]


def test_extended_hours_close_uses_manual_limit_not_close_position(om, monkeypatch, tmp_path):
    """close_position() always submits a market order internally with no way
    to request extended_hours/limit_price — outside regular hours the close
    must go through a manual limit order sized off the ACTUAL held qty/side
    instead."""
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: False)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}})
    )
    om.client.positions = [_pos("AAPL", qty="7")]
    res = om.handle(OrderRequest(action="close", ticker="AAPL", strategy="test"))
    assert res["status"] == "closed_limit_extended"
    # Long position closes via SELL, priced off the bid (199.0).
    assert om.client.orders == [("limit", "AAPL", 7.0, "sell", round(199.0 * 0.995, 2), True)]
    assert "close" not in [o[0] for o in om.client.orders]


def test_extended_hours_close_with_nothing_held_is_a_noop(om, monkeypatch, tmp_path):
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: False)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}})
    )
    res = om.handle(OrderRequest(action="close", ticker="AAPL", strategy="test"))
    assert res["status"] == "closed"
    assert om.client.orders == []


def test_extended_hours_crypto_unaffected_still_plain_market(om, monkeypatch, tmp_path):
    """Crypto trades 24/7 on Alpaca with no extended-hours distinction — even
    with regular_market_hours_open() False, a crypto order stays a plain
    market order, not a limit."""
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: False)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["BTC/USD"], "bias": {"BTC/USD": "long"}})
    )
    res = om.handle(_buy("BTC/USD", qty=0.01))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "BTC/USD", 0.01, "buy", False)]


def test_extended_hours_explicit_limit_order_still_gets_extended_hours_flag(om, monkeypatch, tmp_path):
    """An explicit limit order (order_type="limit") outside regular hours
    still needs extended_hours=True passed through, even though the caller
    already chose their own limit_price."""
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: False)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}})
    )
    req = OrderRequest(action="buy", ticker="AAPL", qty=5.0, order_type="limit",
                       limit_price=190.0, tif="gtc", strategy="test")
    res = om.handle(req)
    assert res["status"] == "submitted_limit"
    assert om.client.orders == [("limit", "AAPL", 5.0, "buy", 190.0, True)]

# ------------------------------------------- extended-hours quote sanity + retry
# Regression tests for the 2026-07-15 08:04:53 ET META failure: a pre-market IEX
# quote came back one-sided (bid=632.49, ask=0.00), _extended_hours_limit_price
# used that ask verbatim, and the resulting $0.00 limit was rejected by Alpaca
# — {"code":40010001,"message":"limit price must be > 0"} — losing the signal
# for good (the engine advances state whether or not the order lands). The
# quotes below are the REAL ones observed that morning.
# See order_manager.MAX_QUOTE_DEVIATION / _reference_price / _submit_with_retry.
def _api_error(status: int, code: int = 40010001,
               message: str = "limit price must be > 0") -> APIError:
    """A real alpaca APIError carrying an HTTP status. is_definitive_rejection
    reads .status_code off the wrapped HTTPError, so a bare Exception (or an
    APIError built without one) would not exercise the real predicate."""
    http_error = type("HTTPError", (), {
        "response": type("Resp", (), {"status_code": status})()})()
    return APIError(json.dumps({"code": code, "message": message}), http_error)


def _extended(monkeypatch, tmp_path, symbol):
    """Put the relay outside regular hours with `symbol` long-biased."""
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: False)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": [symbol], "bias": {symbol: "long"}})
    )


def test_zero_ask_falls_back_to_last_trade_not_zero_limit(om, monkeypatch, tmp_path):
    """THE META BUG: a one-sided quote (ask=0) must never become a $0 limit."""
    _extended(monkeypatch, tmp_path, "META")
    om.client.quotes["META"] = (632.49, 0.0)  # exactly as observed pre-market
    om.client.trades["META"] = 661.04
    res = om.handle(_buy("META", qty=7.0))
    assert res["status"] == "submitted_limit_extended"
    assert om.client.orders == [("limit", "META", 7.0, "buy", round(661.04 * 1.005, 2), True)]
    assert om.client.orders[0][4] > 0  # the regression itself


def test_stale_wide_ask_falls_back_to_last_trade(om, monkeypatch, tmp_path):
    """NVDA pre-market 2026-07-15: ask=230.00 against a 211.86 last trade (~9%
    high). It passes a naive "> 0" check and WOULD fill — silently overpaying,
    which is strictly worse than the loud META rejection."""
    _extended(monkeypatch, tmp_path, "NVDA")
    om.client.quotes["NVDA"] = (211.16, 230.00)
    om.client.trades["NVDA"] = 211.86
    om.handle(_buy("NVDA", qty=1.0))
    limit = om.client.orders[0][4]
    assert limit == round(211.86 * 1.005, 2)
    assert limit < 230.00  # never prices through the junk ask


def test_zero_bid_falls_back_to_last_trade_on_sell(om, monkeypatch, tmp_path):
    """The sell side has the mirror failure: bid=0 -> $0 limit."""
    _extended(monkeypatch, tmp_path, "META")
    om.client.quotes["META"] = (0.0, 661.50)
    om.client.trades["META"] = 661.04
    om.client.positions = [_pos("META")]
    om.handle(OrderRequest(action="sell", ticker="META", qty=5.0, strategy="test"))
    # full position, because a sell is a full close now (all-in/all-out)
    assert om.client.orders == [("limit", "META", 10.0, "sell", round(661.04 * 0.995, 2), True)]


def test_quote_within_band_is_still_preferred_over_last_trade(om, monkeypatch, tmp_path):
    """The fallback must not fire on a healthy quote — a tight ask is the more
    current view of where the order actually fills."""
    _extended(monkeypatch, tmp_path, "META")
    om.client.quotes["META"] = (660.90, 661.50)
    om.client.trades["META"] = 661.04
    om.handle(_buy("META", qty=7.0))
    assert om.client.orders == [("limit", "META", 7.0, "buy", round(661.50 * 1.005, 2), True)]


def test_no_usable_quote_or_trade_blocks_instead_of_sending_garbage(om, monkeypatch, tmp_path):
    _extended(monkeypatch, tmp_path, "META")
    om.client.quotes["META"] = (0.0, 0.0)
    om.client.trades["META"] = 0.0
    with pytest.raises(RiskError, match="cannot price"):
        om.handle(_buy("META", qty=7.0))
    assert om.client.orders == []


def test_quote_fallback_is_journaled(om, monkeypatch, tmp_path):
    """The fallback must leave an audit trail — this is how the original bug
    was found, and a silent fallback would hide the next one."""
    _extended(monkeypatch, tmp_path, "META")
    om.client.quotes["META"] = (632.49, 0.0)
    om.client.trades["META"] = 661.04
    om.handle(_buy("META", qty=7.0))
    events = [json.loads(x) for x in (tmp_path / "trades.jsonl").read_text().splitlines()]
    fallbacks = [e for e in events if e["event"] == "quote_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["ticker"] == "META" and fallbacks[0]["last_trade"] == 661.04


# --- 2026-08-03 AMZN weekend gap: the trade, not the quote, was the stale side.
# The overnight quote (283.97, real) sat 4.6% over Friday's close (271.465, the
# "last trade"), the deviation guard called the quote junk, and the buy limit
# priced off the stale close rested ~4.4% below the market — never filled, book
# flat while the engine believed it was long. A fresh tight two-sided quote
# must overrule a session-gap-old trade. See order_manager.STALE_TRADE_*.

def test_fresh_tight_quote_overrules_weekend_stale_trade(om, monkeypatch, tmp_path):
    _extended(monkeypatch, tmp_path, "AMZN")
    om.client.quotes["AMZN"] = (283.71, 283.97, time.time() - 5)   # live overnight book
    om.client.trades["AMZN"] = (271.465, time.time() - 34 * 3600)  # Friday's close
    res = om.handle(_buy("AMZN", qty=18.0))
    assert res["status"] == "submitted_limit_extended"
    # Priced off the fresh ask, not the weekend-stale trade.
    assert om.client.orders == [("limit", "AMZN", 18.0, "buy", round(283.97 * 1.005, 2), True)]
    events = [json.loads(x) for x in (tmp_path / "trades.jsonl").read_text().splitlines()]
    overrides = [e for e in events if e["event"] == "quote_override_stale_trade"]
    assert len(overrides) == 1 and overrides[0]["ticker"] == "AMZN"


def test_wide_quote_never_overrules_even_when_trade_is_stale(om, monkeypatch, tmp_path):
    """A stale trade doesn't make a junk quote trustworthy: the NVDA-style
    wide ask must still lose to the last trade, stale or not."""
    _extended(monkeypatch, tmp_path, "NVDA")
    om.client.quotes["NVDA"] = (211.16, 230.00, time.time() - 5)   # ~8.6% spread
    om.client.trades["NVDA"] = (211.86, time.time() - 34 * 3600)
    om.handle(_buy("NVDA", qty=1.0))
    assert om.client.orders[0][4] == round(211.86 * 1.005, 2)


def test_stale_quote_never_overrules_stale_trade(om, monkeypatch, tmp_path):
    """Both sides stale → keep the conservative fallback (rest unfilled)."""
    _extended(monkeypatch, tmp_path, "AMZN")
    om.client.quotes["AMZN"] = (283.71, 283.97, time.time() - 3600)
    om.client.trades["AMZN"] = (271.465, time.time() - 34 * 3600)
    om.handle(_buy("AMZN", qty=18.0))
    assert om.client.orders[0][4] == round(271.465 * 1.005, 2)


def test_fresh_trade_still_wins_deviation_disputes(om, monkeypatch, tmp_path):
    """The original junk-quote case is unchanged: when the last trade is
    RECENT, a deviant quote (even tight and fresh) still loses — that
    disagreement is the quote's fault, not a session gap."""
    _extended(monkeypatch, tmp_path, "NVDA")
    om.client.quotes["NVDA"] = (229.50, 230.00, time.time() - 5)
    om.client.trades["NVDA"] = (211.86, time.time() - 60)
    om.handle(_buy("NVDA", qty=1.0))
    assert om.client.orders[0][4] == round(211.86 * 1.005, 2)


def test_missing_timestamps_keep_conservative_fallback(om, monkeypatch, tmp_path):
    """No timestamps (older client/fake) → exactly the pre-timestamp behavior."""
    _extended(monkeypatch, tmp_path, "AMZN")
    monkeypatch.setattr(om.client, "get_latest_quote", lambda s: (283.71, 283.97, None))
    monkeypatch.setattr(om.client, "get_latest_trade", lambda s: (271.465, None))
    om.handle(_buy("AMZN", qty=18.0))
    assert om.client.orders[0][4] == round(271.465 * 1.005, 2)


def test_definitive_rejection_is_retried_and_can_succeed(om, monkeypatch, tmp_path):
    _extended(monkeypatch, tmp_path, "META")
    monkeypatch.setattr(om_mod.time, "sleep", lambda _s: None)
    om.client.quotes["META"] = (660.90, 661.50)
    om.client.trades["META"] = 661.04
    om.client.fail_next = [_api_error(422)]  # first attempt rejected
    res = om.handle(_buy("META", qty=7.0))
    assert res["status"] == "submitted_limit_extended"
    assert om.client.submit_attempts == 2
    assert len(om.client.orders) == 1  # exactly one order exists


def test_ambiguous_failure_is_never_retried(om, monkeypatch, tmp_path):
    """A timeout may mean the order DID land and fill — retrying it without an
    idempotency key could open a second position. Must fail after one attempt."""
    _extended(monkeypatch, tmp_path, "META")
    monkeypatch.setattr(om_mod.time, "sleep", lambda _s: None)
    om.client.quotes["META"] = (660.90, 661.50)
    om.client.trades["META"] = 661.04
    om.client.fail_next = [TimeoutError("connection timed out")]
    with pytest.raises(TimeoutError):
        om.handle(_buy("META", qty=7.0))
    assert om.client.submit_attempts == 1


def test_server_error_is_never_retried(om, monkeypatch, tmp_path):
    """A 5xx is equally ambiguous — the order may exist broker-side."""
    _extended(monkeypatch, tmp_path, "META")
    monkeypatch.setattr(om_mod.time, "sleep", lambda _s: None)
    om.client.quotes["META"] = (660.90, 661.50)
    om.client.trades["META"] = 661.04
    om.client.fail_next = [_api_error(503, code=50010000, message="internal error")]
    with pytest.raises(APIError):
        om.handle(_buy("META", qty=7.0))
    assert om.client.submit_attempts == 1


def test_retry_gives_up_after_max_attempts_and_journals_error(om, monkeypatch, tmp_path):
    _extended(monkeypatch, tmp_path, "META")
    monkeypatch.setattr(om_mod.time, "sleep", lambda _s: None)
    om.client.quotes["META"] = (660.90, 661.50)
    om.client.trades["META"] = 661.04
    om.client.fail_next = [_api_error(422) for _ in range(om_mod.ORDER_SUBMIT_ATTEMPTS)]
    with pytest.raises(APIError):
        om.handle(_buy("META", qty=7.0))
    assert om.client.submit_attempts == om_mod.ORDER_SUBMIT_ATTEMPTS
    assert om.client.orders == []
    events = [json.loads(x) for x in (tmp_path / "trades.jsonl").read_text().splitlines()]
    assert len([e for e in events if e["event"] == "order_retry"]) == \
        om_mod.ORDER_SUBMIT_ATTEMPTS - 1
    assert [e for e in events if e["event"] == "order_error"]


def test_retry_reprices_from_a_fresh_quote(om, monkeypatch, tmp_path):
    """The point of retrying: each attempt must RE-QUOTE. A transiently
    one-sided quote is usually two-sided moments later, so attempt 2 should
    price off the healed quote rather than repeat attempt 1's number."""
    _extended(monkeypatch, tmp_path, "META")
    monkeypatch.setattr(om_mod.time, "sleep", lambda _s: None)
    om.client.trades["META"] = 661.04
    om.client.quotes["META"] = (632.49, 0.0)  # attempt 1: junk -> priced off last trade
    om.client.fail_next = [_api_error(422)]

    def recovering(symbol):
        # Quote heals once the first attempt has been made.
        if om.client.submit_attempts >= 1:
            return (660.90, 661.50, time.time())
        q = om.client.quotes[symbol]
        return q if len(q) == 3 else (*q, time.time())
    monkeypatch.setattr(om.client, "get_latest_quote", recovering)

    res = om.handle(_buy("META", qty=7.0))
    assert res["status"] == "submitted_limit_extended"
    # Priced off the HEALED ask (661.50), not the fallback last trade (661.04).
    assert om.client.orders == [("limit", "META", 7.0, "buy", round(661.50 * 1.005, 2), True)]


# ---------------------------------------------------------------------------
# ALL-IN / ALL-OUT is enforced by the RELAY, not just observed by the engine
# (operator directive 2026-08-27; found by the adversarial review of that
# change, which pointed out the invariant rested on one caller's restraint).
# ---------------------------------------------------------------------------
def test_a_partial_sell_is_canonicalised_to_a_full_close(om, tmp_path):
    """OrderRequest still permits action='sell' with an arbitrary qty, and
    before this the relay would happily submit it — a partial exit, from the
    component whose job is enforcing risk rules. The engine no longer asks for
    one, but the invariant must not depend on that."""
    (tmp_path / "decisions.json").write_text(
        json.dumps({"watchlist": ["AAPL"], "bias": {"AAPL": "long"}}))
    om.client.positions = [_pos("AAPL")]          # 10 shares held

    res = om.handle(OrderRequest(action="sell", ticker="AAPL", qty=5.0, strategy="test"))

    assert res["status"] == "closed"
    assert res["action"] == "close" and res["qty"] is None
    assert om.client.orders == [("close", "AAPL")], \
        "one whole-position close, not a 5-share sell"


def test_a_contingency_never_shrinks_the_entry_even_with_a_stale_cap(om, tmp_path):
    """The schema strips size_cap now, but a decisions.json written before that
    still carries one. The order path must ignore it rather than half-size a
    live breakout — which is what the AAPL rule standing at 0.35 would have done
    on 2026-08-27."""
    (tmp_path / "decisions.json").write_text(json.dumps({
        "watchlist": ["AAPL"], "bias": {"AAPL": "flat"},
        "contingencies": {"AAPL": [{
            "condition": "close_above", "level": 315.5, "action": "long",
            "size_cap": 0.35, "valid_until": _iso(1), "reason": "stale cap",
        }]},
    }))
    res = om.handle(_buy(qty=15.0, price=316.0))
    assert res["status"] == "submitted_market"
    assert om.client.orders == [("market", "AAPL", 15.0, "buy", False)]

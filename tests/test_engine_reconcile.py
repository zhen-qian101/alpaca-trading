"""Tests for sigengine.engine._reconcile_positions — the end-of-poll safety net
that closes a position the strategy has already exited but that is still open at
the broker.

Background: state[sym] advances whether or not an order landed, and a strategy's
stop is only evaluated against its SIMULATED position — so a failed close leaves
a real position riding with no stop, never re-emitted, while also blocking every
future entry in that symbol (fire_event skips a buy when held > 0).

The pass is deliberately asymmetric: it may only ever REDUCE exposure. It closes
missed exits; it only alerts on missed entries, on holdings that predate the
rolling window, and on shorts.

    .venv/bin/pytest -q tests/test_engine_reconcile.py
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import sigengine.engine as eng  # noqa: E402

ENABLED = {"META": {"interval": "1h"}, "AAPL": {"interval": "1h"}}


def _ev(action, reason, ts="2026-07-15 11:00:00+00:00", price=660.9):
    return {"ts": pd.Timestamp(ts), "action": action, "reason": reason,
            "price": price, "strategy": "macd_sma"}


def _pos(sym, qty, available=None):
    """`available` is Alpaca's qty_available (qty minus whatever a working
    order has pinned). Left off, the attribute is absent entirely — which is
    also what an older SDK returns, and the pass must treat that as "nothing
    pinned"."""
    attrs = {"symbol": sym, "qty": str(qty)}
    if available is not None:
        attrs["qty_available"] = str(available)
    return type("Pos", (), attrs)()


def _order(oid="o1"):
    return type("Order", (), {"id": oid})()


class FakeClient:
    def __init__(self, positions=None, open_orders=None):
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.cancelled: list = []

    def get_all_positions(self):
        return list(self.positions)

    def get_open_orders(self, symbol=None):
        return list(self.open_orders)

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))


class FakeOM:
    """Records handle() calls instead of trading. `raises` makes a close fail."""

    def __init__(self, raises=None):
        self.handled: list = []
        self.raises = raises

    def handle(self, payload):
        self.handled.append((payload.ticker, payload.action, payload.strategy))
        if self.raises:
            raise self.raises
        return {"status": "closed"}


@pytest.fixture(autouse=True)
def journal(monkeypatch, tmp_path):
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    eng._PENDING_EXIT_SEEN.clear()  # module-level "already reported" damper
    return tmp_path / "trades.jsonl"


@pytest.fixture
def closed_session(monkeypatch):
    """Outside regular hours — an unfilled exit can only be waited on."""
    monkeypatch.setattr(eng, "regular_market_hours_open", lambda: False)


@pytest.fixture
def open_session(monkeypatch):
    """Regular hours — an unfilled exit can be cancelled and re-closed at market."""
    monkeypatch.setattr(eng, "regular_market_hours_open", lambda: True)


def _events(journal_file):
    if not journal_file.exists():
        return []
    return [json.loads(x) for x in journal_file.read_text().splitlines()]


def _fetched(**per_symbol):
    """symbol -> events list, packed as _fetch_and_recompute's (events, ts, source)."""
    return {sym: (evs, pd.Timestamp("2026-07-15 12:00:00+00:00"), "ibkr")
            for sym, evs in per_symbol.items()}


# ------------------------------------------------------- the eod artifact trap
def test_eod_close_artifact_does_not_look_like_an_exit(journal):
    """THE trap this pass must not fall into. The backtest closes any open
    position at the end of its data window with reason "eod" — an artifact, not
    a signal (_diff_and_fire filters it too). Counting it would make EVERY
    symbol look flat every poll and liquidate the whole account."""
    events = [_ev("buy", "macd_sma_bullish"),
              _ev("close", "eod", ts="2026-07-15 12:00:00+00:00")]
    assert eng._simulated_position(events) == (True, True)  # still LONG

    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=events),
                             FakeClient([_pos("META", 7)]), om, dry_run=False)
    assert om.handled == []  # must NOT close a legitimately held position
    assert _events(journal) == []


def test_all_symbols_holding_are_untouched_when_only_eod_closes_them(journal):
    """The account-liquidation scenario, end to end."""
    ev = [_ev("buy", "macd_sma_bullish"), _ev("close", "eod")]
    om = FakeOM()
    eng._reconcile_positions(ENABLED, _fetched(META=ev, AAPL=ev),
                             FakeClient([_pos("META", 7), _pos("AAPL", 3)]),
                             om, dry_run=False)
    assert om.handled == []


# --------------------------------------------------------------- missed exit
def test_missed_exit_is_closed(journal):
    """Real long + strategy flat after a genuine exit = our close never landed."""
    events = [_ev("buy", "macd_sma_bullish", ts="2026-07-14 08:00:00+00:00"),
              _ev("close", "stop", ts="2026-07-15 11:00:00+00:00")]
    assert eng._simulated_position(events) == (False, True)

    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=events),
                             FakeClient([_pos("META", 7)]), om, dry_run=False)
    assert om.handled == [("META", "close", "reconcile")]
    ev = [e for e in _events(journal) if e["event"] == "reconcile_close"]
    assert len(ev) == 1 and ev[0]["ticker"] == "META" and ev[0]["qty"] == 7.0


def test_close_failure_is_journaled_and_does_not_raise(journal):
    """A reconcile close that fails (e.g. risk_filter's 60s dedup rejecting a
    close the fire phase just attempted) must not break the poll — the next
    poll retries."""
    events = [_ev("buy", "macd_sma_bullish"), _ev("close", "stop")]
    om = FakeOM(raises=RuntimeError("duplicate META close within 60s"))
    eng._reconcile_positions({"META": {}}, _fetched(META=events),
                             FakeClient([_pos("META", 7)]), om, dry_run=False)
    assert om.handled == [("META", "close", "reconcile")]


# ------------------------------------------- things it must NOT act on
def test_position_predating_the_window_is_not_closed(journal):
    """The backtest re-runs from a flat book each window, so a position opened
    before the window looks 'flat' to the strategy. Closing it would flatten a
    legitimate holding — alert instead."""
    events = [_ev("sell", "half")]  # no buy in this window
    assert eng._simulated_position(events) == (False, False)

    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=events),
                             FakeClient([_pos("META", 7)]), om, dry_run=False)
    assert om.handled == []
    alerts = [e for e in _events(journal) if e["event"] == "reconcile_alert"]
    assert len(alerts) == 1 and "predates window" in alerts[0]["reason"]


def test_strategy_long_while_flat_is_silent_and_never_auto_bought(journal):
    """Sim long + broker flat is the NORMAL resting state here, not a fault: the
    relay holds one position at a time and rotates by priority, so every
    unselected candidate is long in its own simulation while flat at the broker.
    Reporting it would fire for most symbols every poll and drown the real
    signals. (Verified against live data 2026-07-15: 7 of 11 enabled symbols sat
    in exactly this state with a flat account.)"""
    events = [_ev("buy", "macd_sma_bullish")]
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=events),
                             FakeClient([]), om, dry_run=False)
    assert om.handled == []       # never auto-buys — that would chase
    assert _events(journal) == []  # and never alerts — it is not a divergence


def test_short_position_is_never_covered(journal):
    """The book is long-only; a short is an anomaly, and this account has seen a
    broker-side phantom short before. Never trade against a glitch."""
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=[]),
                             FakeClient([_pos("META", -5)]), om, dry_run=False)
    assert om.handled == []
    alerts = [e for e in _events(journal) if e["event"] == "reconcile_alert"]
    assert len(alerts) == 1 and "short position" in alerts[0]["reason"]


def test_a_sell_is_a_full_exit_like_any_other(journal):
    """A "sell" used to be a HALF exit that left the simulated book long. This
    book is all-in/all-out (operator directive 2026-08-27) and fire_event routes
    both exit actions to the same full close, so the simulation must agree —
    otherwise the reconciler would believe a closed position was still open and
    stand down from the divergence it exists to catch."""
    events = [_ev("buy", "macd_sma_bullish"), _ev("sell", "half")]
    assert eng._simulated_position(events) == (False, True)


def test_agreement_is_silent(journal):
    """Strategy long + position held = nothing to say."""
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "macd_sma_bullish")]),
                             FakeClient([_pos("META", 7)]), om, dry_run=False)
    assert om.handled == []
    assert _events(journal) == []


def test_flat_and_flat_is_silent(journal):
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=[]), FakeClient([]),
                             om, dry_run=False)
    assert om.handled == []
    assert _events(journal) == []


def test_dry_run_never_acts(journal):
    events = [_ev("buy", "macd_sma_bullish"), _ev("close", "stop")]
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=events),
                             FakeClient([_pos("META", 7)]), om, dry_run=True)
    assert om.handled == []
    assert _events(journal) == []


def test_failed_fetch_is_ignored(journal):
    """A symbol whose fetch failed this cycle yields no strategy opinion — the
    pass must stay quiet rather than assume flat and close the position."""
    om = FakeOM()
    fetched = {"META": RuntimeError("ibkr timeout")}
    eng._reconcile_positions({"META": {}}, fetched, FakeClient([_pos("META", 7)]),
                             om, dry_run=False)
    assert om.handled == []
    assert _events(journal) == []


def test_positions_fetch_failure_is_non_fatal(journal):
    class Boom:
        def get_all_positions(self):
            raise RuntimeError("network down")

    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=[]), Boom(), om, dry_run=False)
    assert om.handled == []


def test_crypto_symbol_is_canonicalized(journal):
    """Broker reports BTC/USD; config may say BTCUSD. They must match, or every
    poll would see a phantom divergence."""
    events = [_ev("buy", "ema_bullish"), _ev("close", "stop")]
    om = FakeOM()
    eng._reconcile_positions({"BTCUSD": {}}, {"BTCUSD": (events, pd.Timestamp(
        "2026-07-15 12:00:00+00:00"), "ibkr")},
        FakeClient([_pos("BTC/USD", 0.5)]), om, dry_run=False)
    assert om.handled == [("BTC/USD", "close", "reconcile")]


# ------------------------------------------------- the pinned-exit trap (08-18)
# THE failure this guard exists for. TSLA 2026-08-18: a REAL exit signal fired
# and its order was accepted, but priced off a frozen overnight last trade it
# rested ~4.4% above the bid and did not fill for 10.5 hours. The position stayed
# open, so this pass called it a missed exit on every poll and submitted another
# close — each rejected with code 40310000 because the FIRST order held all the
# shares. 115 rejections, 115 spurious exit arrows. AAPL 2026-08-11: same, 109x.
def test_pinned_exit_is_not_resubmitted(journal, closed_session):
    """qty_available == 0 means a working order already holds the shares: the
    exit is unfilled, NOT missed, and a second close cannot do anything but be
    rejected."""
    om = FakeOM()
    client = FakeClient([_pos("META", 7, available=0)], open_orders=[_order()])
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             client, om, dry_run=False)
    assert om.handled == []           # no doomed second close
    assert client.cancelled == []     # and the working exit is left alone
    evs = _events(journal)
    assert [e["event"] for e in evs] == ["reconcile_exit_pending"]
    assert evs[0]["ticker"] == "META" and evs[0]["qty"] == 7.0


def test_pinned_exit_is_reported_once_not_every_poll(journal, closed_session):
    """The noise, not just the orders: 115 journal lines is what made this
    invisible on the chart. One resting order -> one line, however long it rests."""
    om = FakeOM()
    client = FakeClient([_pos("META", 7, available=0)], open_orders=[_order()])
    fetched = _fetched(META=[_ev("buy", "cross"), _ev("close", "stop")])
    for _ in range(5):
        eng._reconcile_positions({"META": {}}, fetched, client, om, dry_run=False)
    assert [e["event"] for e in _events(journal)] == ["reconcile_exit_pending"]


def test_a_different_resting_order_is_reported_again(journal, closed_session):
    """The damper is keyed by order id, so a genuinely new pinning order is not
    swallowed by the previous one's notice."""
    om = FakeOM()
    client = FakeClient([_pos("META", 7, available=0)], open_orders=[_order("o1")])
    fetched = _fetched(META=[_ev("buy", "cross"), _ev("close", "stop")])
    eng._reconcile_positions({"META": {}}, fetched, client, om, dry_run=False)
    client.open_orders = [_order("o2")]
    eng._reconcile_positions({"META": {}}, fetched, client, om, dry_run=False)
    assert [e["event"] for e in _events(journal)] == ["reconcile_exit_pending"] * 2


def test_pinned_exit_is_cancelled_once_regular_hours_open(journal, open_session):
    """Waiting only pays while there is no session to trade in. At the open,
    release the shares — but do NOT also close in the same poll, which would
    just race the cancel and earn one more rejection."""
    om = FakeOM()
    client = FakeClient([_pos("META", 7, available=0)], open_orders=[_order("o9")])
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             client, om, dry_run=False)
    assert client.cancelled == ["o9"]
    assert om.handled == []
    evs = _events(journal)
    assert [e["event"] for e in evs] == ["reconcile_cancel_pinned"]
    assert evs[0]["order_ids"] == "o9"


def test_freed_shares_are_closed_on_the_next_poll(journal, open_session):
    """The poll after the cancel: nothing pinned any more, so the ordinary
    missed-exit close runs and the position is actually flattened."""
    om = FakeOM()
    client = FakeClient([_pos("META", 7, available=7)])
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             client, om, dry_run=False)
    assert om.handled == [("META", "close", "reconcile")]
    assert [e["event"] for e in _events(journal)] == ["reconcile_close"]


def test_no_open_order_found_waits_rather_than_guessing(journal, closed_session):
    """qty_available == 0 with nothing working is a race with a fill or a
    cancel — re-read next poll instead of firing at a stale picture."""
    om = FakeOM()
    client = FakeClient([_pos("META", 7, available=0)], open_orders=[])
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             client, om, dry_run=False)
    assert om.handled == [] and _events(journal) == []


def test_open_order_fetch_failure_is_non_fatal_and_waits(journal, closed_session):
    class Boom(FakeClient):
        def get_open_orders(self, symbol=None):
            raise RuntimeError("network")

    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             Boom([_pos("META", 7, available=0)]), om, dry_run=False)
    assert om.handled == [] and _events(journal) == []


def test_missing_qty_available_is_treated_as_nothing_pinned(journal, closed_session):
    """Older SDKs and every pre-existing test double omit the field; that must
    keep the original behavior rather than silently disabling the close."""
    om = FakeOM()
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             FakeClient([_pos("META", 7)]), om, dry_run=False)
    assert om.handled == [("META", "close", "reconcile")]


# ------------------------------------------- reconcile_close means SUBMITTED
def test_reconcile_close_is_journaled_only_after_a_successful_submit(journal):
    """The chart draws an exit arrow per reconcile_close. Journaling it before
    the attempt drew one for every rejection too — 115 of them for TSLA."""
    om = FakeOM(raises=RuntimeError("insufficient qty available"))
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             FakeClient([_pos("META", 7)]), om, dry_run=False)
    evs = _events(journal)
    assert [e["event"] for e in evs] == ["reconcile_close_failed"]
    assert "insufficient qty" in evs[0]["reason"]


# ------------------------------------------------- PARTIAL pins count too
# The first version of this guard tested `avail <= 0`, which only caught a
# position whose shares were ENTIRELY committed. But a close is always sized to
# the whole position (order_manager._close_position / _position_qty_and_side),
# so the broker rejects it whenever ANY shares are held_for_orders. The
# strategy's `sell` is a HALF exit, so a resting half-exit is the ordinary way
# to land in that state — and it brought the whole reject-every-poll loop back.
def test_a_partially_pinned_position_is_not_closed_either(journal, closed_session):
    om = FakeOM()
    client = FakeClient([_pos("META", 13, available=7)], open_orders=[_order()])
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             client, om, dry_run=False)
    assert om.handled == []       # a full-size close here is rejected 40310000
    assert [e["event"] for e in _events(journal)] == ["reconcile_exit_pending"]


def test_a_partial_pin_is_cancelled_once_regular_hours_open(journal, open_session):
    om = FakeOM()
    client = FakeClient([_pos("META", 13, available=7)], open_orders=[_order("o3")])
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             client, om, dry_run=False)
    assert client.cancelled == ["o3"]
    assert om.handled == []
    assert [e["event"] for e in _events(journal)] == ["reconcile_cancel_pinned"]


def test_a_fully_available_position_is_still_closed_normally(journal, closed_session):
    """The guard must not swallow the ordinary missed exit it was built around."""
    om = FakeOM()
    client = FakeClient([_pos("META", 13, available=13)])
    eng._reconcile_positions({"META": {}}, _fetched(META=[_ev("buy", "cross"),
                                                         _ev("close", "stop")]),
                             client, om, dry_run=False)
    assert om.handled == [("META", "close", "reconcile")]
    assert [e["event"] for e in _events(journal)] == ["reconcile_close"]

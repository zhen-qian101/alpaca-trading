"""The order path must FAIL CLOSED when the account state is unknown.

From the 2026-08-19 audit. Four ways the "one position at a time" invariant
could be bypassed or an exit silently lost:

* a failed positions fetch SKIPPED the rotation check entirely (it was gated on
  `positions is not None`), so a second full-size position could open;
* that same None reached _close_position(), where the extended-hours branch
  found nothing in `positions or []`, returned "closed" without submitting an
  order, and journaled it as a successful exit;
* a submitted-but-unfilled order creates no position, so a positions-only check
  is blind to it — and outside regular hours every order is a resting limit that
  routinely sits for hours;
* rotation treated an extended-hours close as done the moment it was accepted,
  then immediately bought the new name while still holding the old one.

    .venv/bin/pytest -q tests/test_order_manager_fail_closed.py
"""
from __future__ import annotations

import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pytest  # noqa: E402

import relay.order_manager as om_mod  # noqa: E402
from relay.models import OrderRequest  # noqa: E402
from relay.risk_filter import RiskError  # noqa: E402


def _pos(sym, qty):
    return type("Pos", (), {"symbol": sym, "qty": str(qty), "qty_available": str(qty)})()


def _order(sym):
    return type("Order", (), {"id": f"o-{sym}", "symbol": sym})()


class FakeClient:
    def __init__(self, positions=None, open_orders=None, positions_raise=False):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.positions_raise = positions_raise
        self.submitted: list = []
        self.closed: list = []

    def get_account(self):
        return type("Acct", (), {"equity": "10000", "cash": "10000",
                                 "pattern_day_trader": False})()

    def get_all_positions(self):
        if self.positions_raise:
            raise RuntimeError("alpaca 500")
        return list(self._positions)

    def get_open_orders(self, symbol=None):
        return list(self._open_orders)

    def get_latest_quote(self, sym):
        return (100.0, 100.5, None)

    def get_latest_trade(self, sym):
        return (100.2, None)

    def limit_order(self, sym, qty, side, price, tif, extended_hours=False):
        self.submitted.append((sym, qty, side, price))
        return {"id": "x"}

    def market_order(self, sym, qty, side, tif="day"):
        self.submitted.append((sym, qty, side, None))
        return {"id": "x"}

    def close_position(self, sym):
        self.closed.append(sym)
        return {"id": "c"}


@pytest.fixture(autouse=True)
def journal(monkeypatch, tmp_path):
    monkeypatch.setattr(om_mod, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    return tmp_path / "trades.jsonl"


@pytest.fixture(autouse=True)
def allow_everything(monkeypatch):
    """Neutralise the risk gate and rotation priorities — this module is about
    the position/order bookkeeping around them, not about them."""
    monkeypatch.setattr(om_mod, "check", lambda *a, **k: None)
    monkeypatch.setattr(om_mod, "decisions_for_rotation",
                        lambda: ({"AAPL": 5, "MSFT": 1}, {}))


@pytest.fixture
def rth(monkeypatch):
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: True)


@pytest.fixture
def after_hours(monkeypatch):
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)


def _buy(ticker="AAPL", qty=10):
    return OrderRequest(ticker=ticker, action="buy", qty=qty)


# ------------------------------------------- unknown account state blocks a buy
def test_a_failed_positions_fetch_blocks_the_buy(rth):
    """It used to SKIP the one-position check and submit anyway — the opposite
    of what an unknown account state should cause."""
    c = FakeClient(positions_raise=True)
    with pytest.raises(RiskError, match="position state unknown"):
        om_mod.OrderManager(c).handle(_buy())
    assert c.submitted == []


def test_a_normal_buy_on_a_flat_book_still_goes_through(rth):
    c = FakeClient(positions=[], open_orders=[])
    res = om_mod.OrderManager(c).handle(_buy())
    assert res["status"].startswith("submitted")
    assert len(c.submitted) == 1


# --------------------------------------------- working orders count as held
def test_a_working_order_in_the_same_symbol_blocks_a_second_entry(after_hours):
    c = FakeClient(positions=[], open_orders=[_order("AAPL")])
    with pytest.raises(RiskError, match="already working"):
        om_mod.OrderManager(c).handle(_buy("AAPL"))
    assert c.submitted == []


def test_a_working_order_in_another_symbol_blocks_rather_than_rotating(after_hours):
    """MSFT's unfilled entry is invisible to get_all_positions(), so a
    positions-only check would open AAPL full-size alongside it.

    It must BLOCK, not rotate: with no MSFT position, _close_position finds
    nothing and returns "closed" without sending anything, so a rotation would
    leave MSFT's order still resting AND submit AAPL on top — the exact
    stacking this is meant to prevent. The resting order is a DAY order and
    resolves by the session's end."""
    c = FakeClient(positions=[], open_orders=[_order("MSFT")])
    with pytest.raises(RiskError, match="still working"):
        om_mod.OrderManager(c).handle(_buy("AAPL"))
    assert c.submitted == [] and c.closed == []


def test_an_unreadable_open_orders_list_does_not_block_a_normal_buy(rth, monkeypatch):
    """This check can only ADD blocks; degrading it must not kill the entry
    path outright."""
    c = FakeClient(positions=[], open_orders=[])
    monkeypatch.setattr(c, "get_open_orders",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    res = om_mod.OrderManager(c).handle(_buy())
    assert res["status"].startswith("submitted")


# ------------------------------- an extended-hours close is not a completed exit
def test_rotation_defers_the_entry_while_the_exit_is_only_resting(after_hours):
    c = FakeClient(positions=[_pos("MSFT", 5)], open_orders=[])
    with pytest.raises(RiskError, match="resting unfilled"):
        om_mod.OrderManager(c).handle(_buy("AAPL"))
    sides = [s[2] for s in c.submitted]
    assert "sell" in sides           # the exit WAS sent
    assert "buy" not in sides        # the entry was NOT stacked on top of it


def test_rotation_in_regular_hours_still_closes_then_buys(rth):
    """During RTH the close is a market order that completes, so the original
    close-then-enter behavior is preserved."""
    c = FakeClient(positions=[_pos("MSFT", 5)], open_orders=[])
    res = om_mod.OrderManager(c).handle(_buy("AAPL"))
    assert c.closed == ["MSFT"]
    assert res["status"].startswith("submitted")


# ------------------------- a close must never be reported without being sent
def test_an_unknown_position_state_never_reports_a_phantom_exit(after_hours):
    """_close_position used to return "closed" — journaled as order_ok — when
    the positions fetch had failed, so a stop that fired during an Alpaca blip
    was recorded as filled while the position rode on."""
    c = FakeClient(positions_raise=True)
    with pytest.raises(RiskError, match="position state unknown"):
        om_mod.OrderManager(c)._close_position("AAPL", None)
    assert c.submitted == [] and c.closed == []


def test_a_genuinely_flat_book_is_still_a_no_op_close(after_hours):
    c = FakeClient(positions=[])
    assert om_mod.OrderManager(c)._close_position("AAPL", []) == "closed"
    assert c.submitted == []


def test_a_none_positions_list_is_refetched_before_believing_it(after_hours):
    """None means "handle() could not read them", not "the book is flat" — so
    re-read, and if the book really does hold something, SUBMIT the exit."""
    c = FakeClient(positions=[_pos("AAPL", 7)])
    assert om_mod.OrderManager(c)._close_position("AAPL", None) == "closed_limit_extended"
    assert [s[2] for s in c.submitted] == ["sell"]

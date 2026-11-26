"""The ENTRY half of the 2026-08-27 audit: five ways a legitimate buy was lost.

The exit findings (EXIT-1..6) get their own modules. These five share a single
shape — the engine decided to open a position, and then the position did not
open, silently, with the state cursor advancing past the signal so it could
never be re-emitted. Across the live journal that is 45 blocked-or-errored
entries against 13 fills ever, and zero crypto fills in the whole history.

  E1  every crypto buy handed Alpaca `time_in_force=day`, which it rejects
      outright (42210000). BTC/USD held a priority-1 rotation slot it could
      not use. Fixed by promoting a crypto DAY order to GTC at submit.
  E2  a buy blocked for a TRANSIENT reason — a rotation exit still resting, an
      order still working, the 60s dedup — was gone for good, because each
      strategy emits exactly one buy per cross-up cycle and the cursor had
      moved. The rotation path's own comment promised "the buy re-fires from
      the next poll's signal", and nothing kept that promise. Fixed with a
      pending-entry sidecar and a narrow, four-way-bounded retry.
  E8  events sharing a timestamp were ordered by Python's stable sort, i.e. by
      the order the strategies happen to appear in sigengine/config.json. A
      config edit could therefore turn a strategy handoff into an exit with no
      re-entry. Fixed by ranking exits ahead of entries at the same ts.
  S3  the same-poll double-buy guard was set but never reset, so a catch-up
      batch shaped buy -> close -> buy swallowed the final, legitimate buy.
  S4  and the opposite hazard: _diff_and_fire fires catch-up events of ANY age
      at their own bar's price, so a day-old buy after downtime opened a
      position the market had already left. Entries now age out at three bars.
      Exits never do.

Every test here fakes the broker and redirects the journal into tmp_path — no
order reaches Alpaca and the live trades.jsonl is never touched.

    .venv/bin/pytest -q tests/test_entry_path_fixes.py
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

import backtest.multi_strategy as ms  # noqa: E402
import relay.order_manager as om_mod  # noqa: E402
import sigengine.engine as eng  # noqa: E402
from relay.models import OrderRequest  # noqa: E402
from relay.risk_filter import RiskError  # noqa: E402

CFG = {"interval": "1h", "deploy": 0.97, "stop": 0.02}
T0 = pd.Timestamp("2026-08-27 14:00:00+00:00")
HOUR = pd.Timedelta("1h")


# --------------------------------------------------------------------- fakes
class FakeBroker:
    """Records what reached the broker. Deliberately records `tif` positionally
    the way AlpacaClient.market_order receives it, because tif is the whole
    subject of E1."""

    def __init__(self, positions=None, open_orders=None, equity=10_000.0):
        self._positions = list(positions or [])
        self._open_orders = list(open_orders or [])
        self.equity = equity
        self.market_orders: list = []
        self.limit_orders: list = []
        self.closed: list = []
        self.cancelled: list = []

    def get_account(self):
        return type("Acct", (), {"equity": str(self.equity), "cash": str(self.equity),
                                 "pattern_day_trader": False})()

    def get_all_positions(self):
        return list(self._positions)

    def get_open_orders(self, symbol=None):
        return list(self._open_orders)

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))

    def get_latest_quote(self, sym):
        return (100.0, 100.5, None)

    def get_latest_trade(self, sym):
        return (100.2, None)

    def market_order(self, symbol, qty, side, tif="day", extended_hours=False):
        self.market_orders.append({"symbol": symbol, "qty": qty, "side": side, "tif": tif})
        return {"id": "m1"}

    def limit_order(self, symbol, qty, side, limit_price, tif="day", extended_hours=False):
        self.limit_orders.append({"symbol": symbol, "qty": qty, "side": side,
                                  "limit_price": limit_price, "tif": tif,
                                  "extended_hours": extended_hours})
        return {"id": "l1"}

    def close_position(self, symbol):
        self.closed.append(symbol)
        return {"id": "c1"}


class FakeOM:
    """An OrderManager stand-in whose verdict each call is scripted.

    `outcomes` is consumed one entry per handle(): an Exception instance is
    raised, anything else is returned as the result dict. Running off the end
    means "accepted"."""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.handled: list = []

    def handle(self, payload: OrderRequest) -> dict:
        self.handled.append(payload)
        out = self.outcomes.pop(0) if self.outcomes else {"status": "submitted_market"}
        if isinstance(out, Exception):
            raise out
        return out


def _pos(sym, qty):
    return type("Pos", (), {"symbol": sym, "qty": str(qty), "qty_available": str(qty)})()


def _order(sym, side="buy", oid=None):
    return type("Order", (), {"id": oid or f"o-{sym}", "symbol": sym, "side": side})()


@pytest.fixture(autouse=True)
def journal(monkeypatch, tmp_path):
    """Both modules append to their OWN module-global JOURNAL_FILE, so both must
    be redirected or a test writes into the live /mnt/sandisk trades.jsonl."""
    path = tmp_path / "trades.jsonl"
    monkeypatch.setattr(eng, "JOURNAL_FILE", path)
    monkeypatch.setattr(om_mod, "JOURNAL_FILE", path)
    return path


def _rows(journal_file, event=None):
    if not journal_file.exists():
        return []
    rows = [json.loads(x) for x in journal_file.read_text().splitlines() if x.strip()]
    return [r for r in rows if event is None or r.get("event") == event]


@pytest.fixture
def no_risk_gate(monkeypatch):
    """E1 is about what the submit call looks like, not about the risk gate."""
    monkeypatch.setattr(om_mod, "check", lambda *a, **k: None)
    monkeypatch.setattr(om_mod, "decisions_for_rotation", lambda: ({}, {}))


@pytest.fixture
def rth(monkeypatch):
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: True)


@pytest.fixture
def after_hours(monkeypatch):
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: False)


def _aux():
    return {eng._PENDING_KEY: {}, eng._OPENED_KEY: {}}


def _ev(action, ts=T0, price=100.0, reason=None, strategy="macd_sma"):
    return {"ts": ts, "action": action, "price": price, "strategy": strategy,
            "reason": reason or {"buy": "macd_sma_bullish", "sell": "half",
                                 "close": "stop"}[action]}


# ======================================================================== E1
# Crypto cannot use time_in_force=day.
def test_a_crypto_buy_reaches_the_broker_as_gtc(no_risk_gate, rth):
    """THE regression. OrderRequest.tif defaults to "day" because that is right
    for equities, and crypto is deliberately never routed through the
    extended-hours branch — so every crypto buy fell into the plain market
    branch and handed "day" to TimeInForce, which Alpaca rejects with 42210000
    "invalid crypto time_in_force". A 4xx is a definitive rejection, so all
    three submit retries re-sent the identical bad order and the engine then
    advanced its cursor past the signal. Zero BTC/USD entries had EVER opened
    while it sat on the board as a priority-1 long-bias candidate."""
    c = FakeBroker()
    om_mod.OrderManager(c).handle(OrderRequest(ticker="BTC/USD", action="buy", qty=0.05))

    assert len(c.market_orders) == 1
    assert c.market_orders[0]["tif"] == "gtc", \
        "Alpaca rejects a crypto DAY order outright — it must be promoted to GTC"
    assert c.market_orders[0]["symbol"] == "BTC/USD"


def test_the_slashless_crypto_alias_is_promoted_too(no_risk_gate, rth):
    """A chart may emit BTCUSD; handle() canonicalizes at the library boundary.
    The tif fix must sit AFTER that, or the alias form still ships `day`."""
    c = FakeBroker()
    om_mod.OrderManager(c).handle(OrderRequest(ticker="BTCUSD", action="buy", qty=0.05))

    assert c.market_orders[0] == {"symbol": "BTC/USD", "qty": 0.05, "side": "buy", "tif": "gtc"}


def test_an_equity_buy_still_ships_day(no_risk_gate, rth):
    """The other half: `day` is correct for equities and is what every existing
    equity fill was submitted with. A blanket promotion to GTC would leave an
    unfilled entry resting across sessions instead of expiring — the working
    order that the one-position-at-a-time guard then blocks the book on."""
    c = FakeBroker()
    om_mod.OrderManager(c).handle(OrderRequest(ticker="AAPL", action="buy", qty=10))

    assert c.market_orders[0]["tif"] == "day"


def test_an_extended_hours_equity_entry_still_ships_day(no_risk_gate, after_hours):
    """Out of hours an equity entry becomes a marketable LIMIT, which is a
    different submit call. It must be untouched by an entry-only crypto fix."""
    c = FakeBroker()
    om_mod.OrderManager(c).handle(OrderRequest(ticker="AAPL", action="buy", qty=10))

    assert c.market_orders == []
    assert c.limit_orders[0]["tif"] == "day"
    assert c.limit_orders[0]["extended_hours"] is True


def test_a_crypto_close_goes_through_close_position_with_no_tif(no_risk_gate, after_hours):
    """Exits were never affected and must stay that way. close_position() sends
    no time_in_force at all, and crypto skips the extended-hours limit branch
    even outside RTH because it trades 24/7 — so a crypto exit never touches
    the enum the fix exists for. Asserted at after_hours because that is the
    branch that would misroute it."""
    c = FakeBroker(positions=[_pos("BTC/USD", 0.05)])
    status = om_mod.OrderManager(c).handle(OrderRequest(ticker="BTC/USD", action="close"))

    assert c.closed == ["BTC/USD"]
    assert c.market_orders == [] and c.limit_orders == []
    assert status["status"] == "closed"


def test_an_explicit_gtc_is_left_alone(no_risk_gate, rth):
    """The promotion is narrowly scoped to the DAY default, so a caller that
    already asked for GTC is not rewritten."""
    c = FakeBroker()
    om_mod.OrderManager(c).handle(
        OrderRequest(ticker="ETH/USD", action="buy", qty=1.0, tif="gtc"))

    assert c.market_orders[0]["tif"] == "gtc"


# ======================================================================== E2
# The pending-entry lifecycle.
def _fire(sym, cfg, state, events, last_closed_ts, client, om, aux, new_only=None):
    return eng._diff_and_fire(sym, cfg, state, events, last_closed_ts, "test",
                              client, om, None, dry_run=False, quiet=True, aux=aux)


def test_a_transiently_blocked_entry_is_held_for_a_retry(journal):
    """THE regression. state[sym] advances after every poll whatever the
    outcome, so a blocked buy was marked "already processed" — and each
    strategy emits exactly one buy per cross-up cycle, so the opportunity was
    simply gone. The rotation path's own comment promised otherwise: "the buy
    re-fires from the next poll's signal once the book is flat". The sidecar is
    what finally keeps that promise."""
    client = FakeBroker()
    om = FakeOM([RiskError("rotation exit for TSLA is resting unfilled", transient=True)])
    aux, state = _aux(), {"AAPL": (T0 - HOUR).isoformat()}

    fired = _fire("AAPL", CFG, state, [_ev("buy")], T0, client, om, aux)

    assert fired[0]["status"] == "blocked" and fired[0]["transient"] is True
    pend = aux[eng._PENDING_KEY]["AAPL"]
    assert pend["bar_ts"] == str(T0)
    assert pend["price"] == 100.0, "the retry must re-price from the ORIGINAL signal bar"
    assert pend["attempts"] == 1


def test_the_held_entry_is_retried_next_poll_and_cleared_on_success(journal):
    """The point of the whole mechanism: the next poll carries NO new event —
    the cursor has already passed the signal — yet the entry still gets placed.
    Success clears the pending record and writes `opened`, which is what
    _reconcile_positions later reads to know the engine owns this position."""
    client = FakeBroker()
    om = FakeOM()                                  # accepts
    aux = _aux()
    aux[eng._PENDING_KEY]["AAPL"] = {"bar_ts": str(T0), "price": 100.0,
                                     "reason": "macd_sma_bullish",
                                     "strategy": "macd_sma", "attempts": 1}
    state = {"AAPL": T0.isoformat()}               # cursor already past the signal

    fired = _fire("AAPL", CFG, state, [_ev("buy")], T0 + HOUR, client, om, aux)

    assert len(om.handled) == 1 and om.handled[0].action == "buy"
    assert fired[0]["retry_of"] == str(T0)
    assert aux[eng._PENDING_KEY] == {}, "a filled entry must not be retried again"
    assert aux[eng._OPENED_KEY]["AAPL"] == str(T0)


def test_a_still_blocked_retry_stays_pending_and_counts_attempts(journal):
    """A transient block that is still transient is not a reason to give up —
    a rotation exit can rest for hours. The attempt counter is what makes the
    retry auditable rather than an invisible loop."""
    client = FakeBroker()
    om = FakeOM([RiskError("an order for AAPL is already working", transient=True)])
    aux = _aux()
    aux[eng._PENDING_KEY]["AAPL"] = {"bar_ts": str(T0), "price": 100.0,
                                     "reason": "macd_sma_bullish",
                                     "strategy": "macd_sma", "attempts": 1}

    _fire("AAPL", CFG, {"AAPL": T0.isoformat()}, [_ev("buy")], T0 + HOUR, client, om, aux)

    assert aux[eng._PENDING_KEY]["AAPL"]["attempts"] == 2


def test_a_non_transient_block_is_never_held(journal):
    """The line the whole `transient` flag exists to draw. Bias flat, off the
    allowlist, over the size ceiling, daily loss limit tripped — those are
    DECISIONS, correctly taken. Re-attempting them next poll would be arguing
    with the risk gate on a timer, which is exactly the order-spam shape the
    115 rejected TSLA closes had."""
    client = FakeBroker()
    om = FakeOM([RiskError("AAPL bias=flat, blocking new buy entry")])
    aux, state = _aux(), {"AAPL": (T0 - HOUR).isoformat()}

    fired = _fire("AAPL", CFG, state, [_ev("buy")], T0, client, om, aux)

    assert fired[0]["status"] == "blocked" and fired[0]["transient"] is False
    assert aux[eng._PENDING_KEY] == {}


def test_a_refused_retry_stops_asking(journal):
    """The same line, applied one poll later: the block was transient when it
    was stored, but by the retry the bias has flipped to flat. A decision has
    now been taken, so the record is dropped rather than carried to expiry."""
    client = FakeBroker()
    om = FakeOM([RiskError("AAPL bias=flat, blocking new buy entry")])
    aux = _aux()
    aux[eng._PENDING_KEY]["AAPL"] = {"bar_ts": str(T0), "price": 100.0,
                                     "reason": "macd_sma_bullish",
                                     "strategy": "macd_sma", "attempts": 1}

    _fire("AAPL", CFG, {"AAPL": T0.isoformat()}, [_ev("buy")], T0 + HOUR, client, om, aux)

    assert aux[eng._PENDING_KEY] == {}
    assert _rows(journal, "pending_entry_dropped"), "a decision not to trade is still a decision"


def test_a_held_entry_expires_after_three_bars(journal):
    """The retry is priced from the ORIGINAL signal bar, so an unbounded window
    would size a trade off a price that is no longer the market — the same
    hazard S4 covers for catch-up events. Three bars is long enough to outlast
    a resting rotation exit and short enough not to chase."""
    client = FakeBroker()
    om = FakeOM()
    aux = _aux()
    aux[eng._PENDING_KEY]["AAPL"] = {"bar_ts": str(T0), "price": 100.0,
                                     "reason": "macd_sma_bullish",
                                     "strategy": "macd_sma", "attempts": 3}
    stale_by = eng._PENDING_ENTRY_MAX_BARS + 1

    _fire("AAPL", CFG, {"AAPL": (T0 + HOUR).isoformat()}, [_ev("buy")],
          T0 + stale_by * HOUR, client, om, aux)

    assert om.handled == [], "an expired entry must never reach the order path"
    assert aux[eng._PENDING_KEY] == {}
    assert _rows(journal, "pending_entry_dropped")


def test_a_held_entry_at_exactly_the_limit_still_retries(journal):
    """The boundary, pinned deliberately: the limit is inclusive, so a three-bar
    -old entry is still live. Without this an off-by-one would quietly halve the
    window the mechanism was sized for."""
    client = FakeBroker()
    om = FakeOM()
    aux = _aux()
    aux[eng._PENDING_KEY]["AAPL"] = {"bar_ts": str(T0), "price": 100.0,
                                     "reason": "macd_sma_bullish",
                                     "strategy": "macd_sma", "attempts": 1}

    _fire("AAPL", CFG, {"AAPL": (T0 + HOUR).isoformat()}, [_ev("buy")],
          T0 + eng._PENDING_ENTRY_MAX_BARS * HOUR, client, om, aux)

    assert len(om.handled) == 1


def test_a_held_entry_is_dropped_once_the_strategy_is_flat(journal):
    """The first and most important of the four stops. The strategy has moved
    on — its simulated book closed the position — so the setup the buy was
    reasoning about no longer exists. Chasing an abandoned setup is worse than
    missing it, because nothing downstream would ever exit it on that
    strategy's terms."""
    client = FakeBroker()
    om = FakeOM()
    aux = _aux()
    aux[eng._PENDING_KEY]["AAPL"] = {"bar_ts": str(T0), "price": 100.0,
                                     "reason": "macd_sma_bullish",
                                     "strategy": "macd_sma", "attempts": 1}
    events = [_ev("buy"), _ev("close", ts=T0 + HOUR)]

    _fire("AAPL", CFG, {"AAPL": (T0 + HOUR).isoformat()}, events,
          T0 + HOUR, client, om, aux)

    assert om.handled == []
    assert aux[eng._PENDING_KEY] == {}
    assert _rows(journal, "pending_entry_dropped")


def test_a_held_entry_is_dropped_when_this_poll_carries_an_exit(journal):
    """The third stop. The poll carries an exit for the same symbol, so firing
    the held buy would open into a bar the strategy is leaving — buying and
    selling the same symbol in the same poll. This stop is checked BEFORE the
    age test, so it fires even on an entry that is still perfectly fresh."""
    client = FakeBroker()
    om = FakeOM()
    aux = _aux()
    aux[eng._PENDING_KEY]["AAPL"] = {"bar_ts": str(T0), "price": 100.0,
                                     "reason": "macd_sma_bullish",
                                     "strategy": "macd_sma", "attempts": 1}
    events = [_ev("buy"), _ev("sell", ts=T0 + 2 * HOUR)]

    _fire("AAPL", CFG, {"AAPL": (T0 + HOUR).isoformat()}, events,
          T0 + 2 * HOUR, client, om, aux)

    assert not any(p.action == "buy" for p in om.handled)
    assert aux[eng._PENDING_KEY] == {}
    assert _rows(journal, "pending_entry_dropped")


def test_an_exit_clears_the_opened_record(journal):
    """`opened` is the reconciler's answer to "did the engine open this?", so a
    stale one would keep claiming ownership of a position that is gone and
    leave the next operator-opened holding exposed to auto-liquidation."""
    client = FakeBroker(positions=[_pos("AAPL", 97)])
    om = FakeOM([{"status": "closed"}])
    aux = _aux()
    aux[eng._OPENED_KEY]["AAPL"] = str(T0)

    _fire("AAPL", CFG, {"AAPL": T0.isoformat()}, [_ev("close", ts=T0 + HOUR)],
          T0 + HOUR, client, om, aux)

    assert aux[eng._OPENED_KEY] == {}


def test_without_the_sidecar_nothing_changes(journal):
    """aux=None is the documented pre-2026-08-27 path, kept for the debug and
    standalone callers. It must place no retry and record nothing — a new
    mechanism that also rewrites the old callers' behaviour is not a fix."""
    client = FakeBroker()
    om = FakeOM([RiskError("rotation exit resting", transient=True)])

    fired = _fire("AAPL", CFG, {"AAPL": (T0 - HOUR).isoformat()}, [_ev("buy")],
                  T0, client, om, aux=None)

    assert [f["status"] for f in fired] == ["blocked"], "no retry may be appended"
    assert len(om.handled) == 1, "the blocked buy is attempted exactly once"
    assert _rows(journal, "pending_entry_dropped") == []


def test_the_sidecar_never_pollutes_state_json(tmp_path):
    """state.json's contract is "symbol -> last processed bar ISO", and
    web/server.py parses it positionally to build the dashboard's per-symbol
    liveness panel — it would read a bookkeeping key as if it were a ticker.
    That is why this is a separate file, and the separation is worth a test."""
    p = eng._aux_path(dry_run=True)
    assert p != eng._state_path(dry_run=True)
    assert p.name.endswith("_entries.json")


# ======================================================================== E8
# An exit at a timestamp is processed before an entry at the same one.
def _stub_strategies(monkeypatch, per_type):
    """Replace the real runners with ones that replay canned events, so the
    ordering can be tested without hand-crafting bars that make two real
    strategies fire on the same bar."""
    runners = {}
    for name, evs in per_type.items():
        runners[name] = (lambda evs: (lambda df, p, deploy, stop:
                                      [dict(e) for e in evs]))(evs)
    monkeypatch.setattr(ms, "STRATEGY_RUNNERS", runners)


def _combo_cfg(order):
    return {"strategies": [{"type": t} for t in order], "deploy": 0.97, "stop": 0.02}


@pytest.mark.parametrize("order", [("macd_sma", "bollinger_rsi"),
                                   ("bollinger_rsi", "macd_sma")])
def test_a_same_bar_exit_is_ordered_before_the_entry(monkeypatch, order):
    """THE regression. Merging sorted on ts alone, so a same-bar tie was broken
    by Python's stable sort — i.e. by whichever strategy happens to be listed
    first in sigengine/config.json. Every symbol currently lists macd_sma
    first, which is safe BY LUCK; reversed, the bollinger_rsi buy at bar T
    would fire before the macd_sma close at the same T, fire_event would skip
    it on `held > 0`, and the close would then flatten the book — a legitimate
    strategy handoff turned into an exit with no re-entry. A config file's key
    order must not decide trading semantics."""
    _stub_strategies(monkeypatch, {
        "macd_sma": [{"ts": T0, "action": "close", "price": 100.0, "reason": "stop"}],
        "bollinger_rsi": [{"ts": T0, "action": "buy", "price": 100.0, "reason": "rsi_bb_long"}],
    })

    evs = ms.run_symbol_strategies(pd.DataFrame(), _combo_cfg(order), "AAPL")

    assert [e["action"] for e in evs] == ["close", "buy"]
    assert evs[0]["strategy"] == "macd_sma" and evs[1]["strategy"] == "bollinger_rsi"


def test_a_half_exit_also_outranks_a_same_bar_entry(monkeypatch):
    """`sell` is the half-target exit. It reduces exposure, so it takes the same
    precedence a close does — the book's standing rule is that getting smaller
    comes first."""
    _stub_strategies(monkeypatch, {
        "a": [{"ts": T0, "action": "buy", "price": 100.0, "reason": "x"}],
        "b": [{"ts": T0, "action": "sell", "price": 100.0, "reason": "half"}],
    })

    evs = ms.run_symbol_strategies(pd.DataFrame(), _combo_cfg(("a", "b")), "AAPL")

    assert [e["action"] for e in evs] == ["sell", "buy"]


def test_chronological_order_still_dominates(monkeypatch):
    """The tie-break is a SECONDARY key. An exit at a later bar must not be
    hoisted above an earlier entry, or the merged stream would stop being a
    replay of what happened."""
    _stub_strategies(monkeypatch, {
        "a": [{"ts": T0, "action": "buy", "price": 100.0, "reason": "x"}],
        "b": [{"ts": T0 + HOUR, "action": "close", "price": 101.0, "reason": "stop"}],
    })

    evs = ms.run_symbol_strategies(pd.DataFrame(), _combo_cfg(("b", "a")), "AAPL")

    assert [e["action"] for e in evs] == ["buy", "close"]
    assert [e["ts"] for e in evs] == [T0, T0 + HOUR]


def test_an_unknown_action_sorts_last_and_does_not_raise(monkeypatch):
    """Defensive: the rank table is a lookup with a default, so a future action
    verb degrades to "after the ones we know about" rather than a KeyError
    inside the live poll loop."""
    _stub_strategies(monkeypatch, {
        "a": [{"ts": T0, "action": "trim", "price": 100.0, "reason": "?"}],
        "b": [{"ts": T0, "action": "close", "price": 100.0, "reason": "stop"}],
    })

    evs = ms.run_symbol_strategies(pd.DataFrame(), _combo_cfg(("a", "b")), "AAPL")

    assert [e["action"] for e in evs] == ["close", "trim"]


# ======================================================================== S3
# The same-poll double-buy guard must be cleared by an exit.
@pytest.fixture
def spy_fire(monkeypatch):
    """Replace fire_event so this is a test of the batch guard alone — the
    order path has its own modules."""
    calls: list = []

    def fake(sym, ev, cfg, client, om, settings, dry_run, aux=None):
        calls.append(ev["action"])
        return {"ticker": sym, "action": ev["action"], "bar_ts": str(ev["ts"]),
                "status": "submitted_market", "price": ev.get("price")}

    monkeypatch.setattr(eng, "fire_event", fake)
    return calls


def test_a_buy_close_buy_batch_fires_the_final_buy(spy_fire, journal):
    """THE regression. The guard was SET on a successful buy and never reset, so
    a catch-up batch — the normal shape after any downtime — silently swallowed
    the last, legitimate re-entry: the book is flat again by the time it is
    reached, but the guard still said "already bought this poll". The guard
    exists because the positions API may lag the first fill; once a close has
    been sent for the symbol, that staleness argument no longer holds."""
    new = [_ev("buy"), _ev("close", ts=T0 + HOUR), _ev("buy", ts=T0 + 2 * HOUR)]

    fired = eng._fire_new_events("AAPL", new, CFG, FakeBroker(), FakeOM(), None, False)

    assert spy_fire == ["buy", "close", "buy"]
    assert [f["status"] for f in fired] == ["submitted_market"] * 3
    assert _rows(journal, "signal_skip") == []


def test_a_sell_clears_the_guard_because_it_is_a_full_exit(spy_fire, journal):
    """`sell` and `close` are the same act in this book — fire_event routes both
    to a full close — so a sell leaves the account flat and a later buy in the
    same batch is a legitimate re-entry, not a second stacked position."""
    new = [_ev("buy"), _ev("sell", ts=T0 + HOUR), _ev("buy", ts=T0 + 2 * HOUR)]

    eng._fire_new_events("AAPL", new, CFG, FakeBroker(), FakeOM(), None, False)

    assert spy_fire == ["buy", "sell", "buy"]


def test_a_close_that_did_not_reach_the_broker_does_not_clear_the_guard(monkeypatch, journal):
    """The guard exists because the positions API may not yet show the first
    fill. A close that was skipped, blocked or errored changed nothing at the
    broker, so that staleness argument still holds and a following buy would be
    a second full-size entry on a book that is still long."""
    calls: list = []

    def fake(sym, ev, cfg, client, om, settings, dry_run, aux=None):
        calls.append(ev["action"])
        status = "blocked" if ev["action"] == "close" else "submitted_market"
        return {"ticker": sym, "action": ev["action"], "status": status,
                "reason": "", "bar_ts": str(ev["ts"])}

    monkeypatch.setattr(eng, "fire_event", fake)
    new = [_ev("buy"), _ev("close", ts=T0 + HOUR), _ev("buy", ts=T0 + 2 * HOUR)]

    fired = eng._fire_new_events("AAPL", new, CFG, FakeBroker(), FakeOM(), None, False)

    assert calls == ["buy", "close"], "the trailing buy must not reach the order path"
    assert fired[2]["status"] == "skip"


def test_two_buys_with_no_exit_between_them_still_collapse(spy_fire, journal):
    """The guard itself must survive the fix. Two strategies signalling
    independently on the same symbol in one batch is the case it was written
    for, and a second full-size entry there is a double position."""
    new = [_ev("buy"), _ev("buy", ts=T0 + HOUR, strategy="bollinger_rsi")]

    fired = eng._fire_new_events("AAPL", new, CFG, FakeBroker(), FakeOM(), None, False)

    assert spy_fire == ["buy"], "the second buy must not reach the order path at all"
    assert fired[1]["status"] == "skip"
    assert "duplicate buy in same poll" in fired[1]["reason"]


def test_a_failed_buy_never_set_the_guard_in_the_first_place(monkeypatch, journal):
    """Boundary check on the OTHER side of the flag: a buy that was blocked
    opened nothing, so a later buy in the same batch is still legitimate."""
    calls: list = []

    def fake(sym, ev, cfg, client, om, settings, dry_run, aux=None):
        calls.append(ev["action"])
        status = "blocked" if len(calls) == 1 else "submitted_market"
        return {"ticker": sym, "action": ev["action"], "status": status,
                "bar_ts": str(ev["ts"])}

    monkeypatch.setattr(eng, "fire_event", fake)
    new = [_ev("buy"), _ev("buy", ts=T0 + HOUR, strategy="bollinger_rsi")]

    eng._fire_new_events("AAPL", new, CFG, FakeBroker(), FakeOM(), None, False)

    assert calls == ["buy", "buy"]


# ======================================================================== S4
# Catch-up entries age out; catch-up exits never do.
def test_a_stale_catchup_entry_is_refused_and_journaled(journal):
    """THE regression. _diff_and_fire fires every event newer than the state
    cursor regardless of age, and fire_event sizes it from the price on its OWN
    bar. After any downtime — a supervisor restart, a wedged IBKR lock, an
    overnight outage — that means executing a day-old signal at today's price
    with yesterday's sizing, with nothing but the share-count ceiling
    downstream to notice."""
    stale = _ev("buy", ts=T0)
    last_closed = T0 + (eng._CATCHUP_ENTRY_MAX_BARS + 1) * HOUR

    kept = eng._drop_stale_entries("AAPL", CFG, [stale], last_closed)

    assert kept == []
    rows = _rows(journal, "stale_entry_skipped")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL" and rows[0]["action"] == "buy"
    assert rows[0]["age_bars"] == float(eng._CATCHUP_ENTRY_MAX_BARS + 1)
    assert rows[0]["bar_ts"] == str(T0)


@pytest.mark.parametrize("action,reason", [("close", "stop"),
                                           ("close", "macd_sma_bearish"),
                                           ("sell", "half")])
def test_an_equally_old_exit_still_fires(journal, action, reason):
    """The asymmetry that makes the whole guard safe, and the same one
    _gap_blocked_events and _reconcile_positions state for themselves: an
    automatic refusal may only ever REDUCE what we do. A stale exit is still an
    exit — the position is real, the stop level it tripped is real, and ageing
    it out would leave a live position unprotected because the strategy's
    simulated book has already gone flat and will never re-emit it."""
    old = _ev(action, ts=T0, reason=reason)
    last_closed = T0 + 48 * HOUR                      # two days of downtime

    assert eng._drop_stale_entries("AAPL", CFG, [old], last_closed) == [old]
    assert _rows(journal, "stale_entry_skipped") == []


def test_an_entry_at_exactly_the_limit_still_fires(journal):
    """Pinning the boundary: three bars is inclusive. On 1h bars a poll that ran
    slightly late routinely produces a one- or two-bar-old signal, and refusing
    those would break normal operation, not just recovery from downtime."""
    ev = _ev("buy", ts=T0)
    last_closed = T0 + eng._CATCHUP_ENTRY_MAX_BARS * HOUR

    assert eng._drop_stale_entries("AAPL", CFG, [ev], last_closed) == [ev]
    assert _rows(journal, "stale_entry_skipped") == []


def test_a_stale_entry_is_dropped_but_its_batch_mates_survive(journal):
    """The filter is per event, not per batch. A catch-up batch after downtime
    is exactly where a stale buy and a live exit arrive together, and dropping
    the whole list would re-create the missed-exit failure this audit was
    chasing."""
    stale_buy = _ev("buy", ts=T0)
    fresh_exit = _ev("close", ts=T0 + 5 * HOUR)
    fresh_buy = _ev("buy", ts=T0 + 6 * HOUR)
    last_closed = T0 + 6 * HOUR

    kept = eng._drop_stale_entries("AAPL", CFG,
                                   [stale_buy, fresh_exit, fresh_buy], last_closed)

    assert kept == [fresh_exit, fresh_buy]


def test_a_stale_entry_never_reaches_the_order_path(journal):
    """End-to-end through _diff_and_fire, because the filter being called in the
    right place is the half a unit test of the filter cannot show."""
    client = FakeBroker()
    om = FakeOM()
    last_closed = T0 + 10 * HOUR

    fired = _fire("AAPL", CFG, {"AAPL": (T0 - HOUR).isoformat()}, [_ev("buy")],
                  last_closed, client, om, _aux())

    assert om.handled == [] and client.market_orders == []
    assert fired == []
    assert _rows(journal, "stale_entry_skipped")

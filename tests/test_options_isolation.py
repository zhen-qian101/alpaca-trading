"""The equity path must be OPTION-BLIND, and this is the file that proves it.

The weekly cash-secured put (relay/csp_manager.py) adds a second kind of row to
`get_all_positions()` and `get_open_orders()`, and BOTH are unfiltered
passthroughs to alpaca-py. Every equity helper in the relay iterates them and
reads `.symbol` as if it were a stock ticker. Nothing in the options code has to
misbehave for that to cost money — the damage is done by the EQUITY code
mistaking an option for a stock:

  ROTATION      _held_symbol_other_than returned the OCC string
                (SOXL260904P00090000) as "the other position held".
                _should_rotate then looked that string up in decisions.json's
                priority map, got the default 0, and any long-bias candidate
                beat it — so the very first equity buy signal after a put was
                opened called _close_position(<OCC>), a market BUY-TO-CLOSE of
                the option that never passed the risk gate (check() had run
                against the equity payload).
  QUOTES        outside regular hours the same path asked the STOCK quote
                endpoint for an OCC symbol, found nothing, and raised something
                that reads like a data outage — killing the equity entry.
  SLOT COUNTING a working option order counted as a working equity order, so
                one resting put quote would block every equity entry for the
                rest of the session.
  RECONCILE     a short put has qty -1. The reconciler's bookkeeping is
                equity-shaped ("qty" is CONTRACTS, not shares) and its
                long-only rule reads a negative qty as a phantom short.
  SIZING        Alpaca does NOT reduce `equity` when cash is pledged as put
                collateral, so fire_event kept sizing entries at 0.97 x the
                FULL equity and let a broker rejection decide which of the two
                books got the money.

That is the day-one hazard: it fires on the first equity signal after any
option is opened, by this strategy or by hand, with no options code executing at
all. These tests construct it directly.

Every fake here is local and nothing may reach a broker: no order of any kind,
equity or option, is ever submitted from this file, and the journal is
redirected to tmp_path in an autouse fixture in both halves.

    PYTHONPATH=. .venv/bin/pytest -q tests/test_options_isolation.py
"""
from __future__ import annotations

import json
import os
import time

import pytest

# Dummy creds BEFORE importing relay/sigengine, so get_settings() cannot raise
# and cannot pick up the operator's real keys from the environment.
os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pandas as pd  # noqa: E402
from alpaca.trading.enums import AssetClass  # noqa: E402

import relay.csp_manager as csp_mod  # noqa: E402
import relay.options_client as oc_mod  # noqa: E402
import relay.order_manager as om_mod  # noqa: E402
import relay.risk_filter as rf  # noqa: E402
import sigengine.engine as eng


@pytest.fixture(autouse=True)
def _isolate_sleeve_policy(monkeypatch, tmp_path):
    """The share book sizes off equity MINUS the put sleeve's reserve, read from
    decisions/csp.json. These tests are about the sizing arithmetic itself, so
    the LIVE policy file must not leak into an expected share count: point the
    sleeve at an empty file (= disabled = no reserve beyond any pledge)."""
    import relay.csp_manager as csp
    monkeypatch.setattr(csp, "CSP_CONFIG_FILE", tmp_path / "csp.json")  # noqa: E402
from relay.models import OrderRequest  # noqa: E402
from relay.order_manager import (  # noqa: E402
    OrderManager,
    _is_equity_position,
    is_option_symbol,
)
from relay.risk_filter import RiskError  # noqa: E402

# The real contract this strategy would have sold on 2026-08-27: SOXL, the
# 2026-09-04 weekly, the 90 strike. Used verbatim everywhere below so a test
# failure points at a string an operator can recognise in the account.
OCC_PUT = "SOXL260904P00090000"
OCC_CALL = "SOXL260904C00090000"


# =========================================================================== #
# is_option_symbol — the shape backstop
# =========================================================================== #
# It is a BACKSTOP, not the primary test: asset_class == US_OPTION is. It has to
# exist because a position or order object may carry no asset_class at all (an
# older SDK, a test double, a hand-built dict), and "no asset_class" must not
# mean "treat this option as a stock".

def test_a_real_occ_put_symbol_is_recognised():
    """THE symbol from the study. Root + YYMMDD + P + strike in thousandths."""
    assert is_option_symbol(OCC_PUT) is True


def test_a_call_is_an_option_too():
    """The strategy only sells puts, but assignment, a covered call written by
    hand, or a broker-side leg can all put a C contract in the account. The
    guard is about "is this a stock", not "is this our put"."""
    assert is_option_symbol(OCC_CALL) is True


def test_case_does_not_decide_it():
    """Alpaca returns OCC symbols upper-case, but nothing in the relay
    guarantees the caller did not lower-case one on the way through."""
    assert is_option_symbol(OCC_PUT.lower()) is True


@pytest.mark.parametrize("sym", ["SOXL", "MSFT", "AAPL", "TQQQ", "NVDA"])
def test_every_equity_this_book_trades_is_not_an_option(sym):
    """The false-positive direction is the expensive one: a stock misread as an
    option would make _guard_equity_only refuse a legitimate close, leaving a
    real position with no way out."""
    assert is_option_symbol(sym) is False


def test_a_crypto_pair_is_not_an_option():
    """BTC/USD is the longest symbol the equity path handles and the only one
    with punctuation. The slash alone disqualifies it — an OCC symbol has
    none — but the length check would not."""
    assert is_option_symbol("BTC/USD") is False


@pytest.mark.parametrize("sym,why", [
    ("SOXL260904X00090000", "X is not a valid contract type — only C or P"),
    ("SOXL2609040009000", "strike field is short, so the digits do not line up"),
    ("260904P00090000", "an OCC tail with no root is not a tradeable symbol"),
    ("SOXLXX0904P00090000", "the date field is not six digits"),
    ("SOXL260904P0009000A", "the strike field is not all digits"),
    ("SOXL/260904P00090000", "a slash means a crypto pair, never an option"),
    ("", "empty"),
    (None, "None — callers pass str(getattr(p, 'symbol', '')) and that can be junk"),
])
def test_near_misses_are_not_options(sym, why):
    """Near-misses matter because this guard's OUTPUT is a hard refusal
    (_guard_equity_only raises). Anything it wrongly claims is an option becomes
    a symbol the relay can no longer trade at all."""
    assert is_option_symbol(sym) is False, why


# =========================================================================== #
# _is_equity_position — the real test, and its fallback
# =========================================================================== #

class _EnumPos:
    """A position the way alpaca-py returns one: asset_class is an AssetClass
    enum member, so `.value` is what has to be compared."""

    def __init__(self, symbol, qty, asset_class):
        self.symbol = symbol
        self.qty = str(qty)
        self.asset_class = asset_class


class _BarePos:
    """A position object with NO asset_class attribute at all — an older SDK, a
    hand-rolled double, or anything constructed from a raw API dict. This is the
    case that forces is_option_symbol to exist."""

    def __init__(self, symbol, qty, **extra):
        self.symbol = symbol
        self.qty = str(qty)
        for k, v in extra.items():
            setattr(self, k, v)


def test_a_real_us_option_enum_is_not_an_equity_position():
    """The primary path, with the genuine alpaca-py enum member rather than a
    string — `str(ac)` on an enum is "AssetClass.US_OPTION", which is why the
    production code reaches for `.value` first."""
    p = _EnumPos(OCC_PUT, -1, AssetClass.US_OPTION)
    assert _is_equity_position(p) is False


def test_a_real_us_equity_enum_is_an_equity_position():
    p = _EnumPos("SOXL", 18, AssetClass.US_EQUITY)
    assert _is_equity_position(p) is True


def test_crypto_stays_on_the_equity_path():
    """Crypto is not an option and the relay does trade it, so it must remain
    visible to every helper this filter guards."""
    p = _EnumPos("BTC/USD", 0.5, AssetClass.CRYPTO)
    assert _is_equity_position(p) is True


def test_an_object_with_no_asset_class_falls_back_to_the_symbol_shape():
    """THE fallback path. With no asset_class the only evidence left is the
    symbol, and getting this wrong is what produced the rotation bug."""
    assert _is_equity_position(_BarePos(OCC_PUT, -1)) is False
    assert _is_equity_position(_BarePos("MSFT", 10)) is True


def test_an_explicit_none_asset_class_also_falls_back():
    """`getattr(p, "asset_class", None)` cannot tell "attribute missing" from
    "attribute present and None", and a JSON-shaped double routinely produces
    the second. Both must land on the shape check rather than on "not an
    option"."""
    assert _is_equity_position(_BarePos(OCC_PUT, -1, asset_class=None)) is False


def test_a_plain_string_asset_class_is_understood():
    """Some doubles (and older SDK builds) set asset_class to the bare string.
    `.value` is absent there, so the code falls through to str(ac)."""
    assert _is_equity_position(_BarePos(OCC_PUT, -1, asset_class="us_option")) is False
    assert _is_equity_position(_BarePos("SOXL", 18, asset_class="us_equity")) is True


# =========================================================================== #
# The relay, end to end: an open short put plus an equity buy signal
# =========================================================================== #

class FakeClient:
    """Stand-in for AlpacaClient. Records every order; reaches no network.

    Deliberately minimal and local: nothing in this file may construct a real
    TradingClient, and no order — equity or option — may leave the process.
    """

    def __init__(self, positions=None, open_orders=None):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.orders: list = []
        self.quotes = {"MSFT": (299.0, 301.0), "SOXL": (24.9, 25.1)}
        self.trades = {"MSFT": 300.0, "SOXL": 25.0}

    # ---- writes (recorded, never sent) ----
    def market_order(self, symbol, qty, side, tif="day", extended_hours=False):
        self.orders.append(("market", symbol, qty, side, extended_hours))
        return {"id": "order-1"}

    def limit_order(self, symbol, qty, side, limit_price, tif="day", extended_hours=False):
        self.orders.append(("limit", symbol, qty, side, limit_price, extended_hours))
        return {"id": "order-limit"}

    def close_position(self, symbol):
        self.orders.append(("close", symbol))
        return {"id": "order-close"}

    # ---- reads ----
    def get_latest_quote(self, symbol):
        return (*self.quotes[symbol], time.time())

    def get_latest_trade(self, symbol):
        return (self.trades[symbol], time.time())

    def get_account(self):
        return type("Acct", (), {"equity": "100000", "buying_power": "200000",
                                 "last_equity": "100000", "status": "active"})()

    def get_all_positions(self):
        return list(self.positions)

    def get_open_orders(self, symbol=None):
        return list(self.open_orders)


def _order(symbol, asset_class=None, side="sell"):
    attrs = {"id": "o-1", "symbol": symbol, "side": side}
    if asset_class is not None:
        attrs["asset_class"] = asset_class
    return type("Order", (), attrs)()


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """No live journal, no live decisions.json, and a deterministic clock.

    The journal redirect is not hygiene, it is a correctness requirement: this
    file exercises the ORDER path, which appends to JOURNAL_FILE on every
    decision it takes.
    """
    monkeypatch.setattr(rf, "_market_open", lambda allow_ext: True)
    monkeypatch.setattr(rf, "regular_market_hours_open", lambda: True)
    monkeypatch.setattr(om_mod, "regular_market_hours_open", lambda: True)
    rf._DEDUP.clear()
    monkeypatch.setattr(om_mod, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(rf, "DECISIONS_FILE", tmp_path / "decisions.json")
    (tmp_path / "decisions.json").write_text("{}")
    yield


def _buy(ticker="MSFT", qty=5.0, **kw):
    return OrderRequest(action="buy", ticker=ticker, qty=qty, strategy="test", **kw)


# ------------------------------------------------ _held_symbol_other_than ----
def test_an_open_short_put_is_not_the_other_position_held():
    """The single assertion the rotation bug turned on. A short put is a
    commitment of CASH, not a rotatable position, and there is no sane answer to
    "should I rotate out of SOXL260904P00090000 into MSFT?" — the question must
    never be asked."""
    positions = [_EnumPos(OCC_PUT, -1, AssetClass.US_OPTION)]
    assert OrderManager._held_symbol_other_than(positions, "MSFT") is None


def test_an_open_short_put_with_no_asset_class_is_still_invisible():
    """Same claim through the fallback path — the filter must not depend on the
    SDK volunteering an asset_class."""
    assert OrderManager._held_symbol_other_than([_BarePos(OCC_PUT, -1)], "MSFT") is None


def test_a_real_equity_beside_an_option_is_still_found():
    """The filter must remove the option and NOTHING else: an equity holding
    alongside a put still has to be seen, or the one-position-at-a-time rule
    quietly stops being enforced and the engine doubles up."""
    positions = [_EnumPos(OCC_PUT, -1, AssetClass.US_OPTION),
                 _EnumPos("SOXL", 18, AssetClass.US_EQUITY)]
    assert OrderManager._held_symbol_other_than(positions, "MSFT") == "SOXL"


# ------------------------------------------------------- THE day-one hazard --
def test_an_equity_buy_beside_an_open_short_put_never_closes_the_option():
    """DAY ONE, EXACTLY AS IT WOULD HAVE HAPPENED.

    The account holds one short SOXL put and nothing else. MSFT fires a buy.
    Before the filter, handle() read the OCC string as the held symbol, found
    priority 0 for it in decisions.json, rotated, and sent a market
    BUY-TO-CLOSE of the put — an option order placed by the equity relay, at
    market, on a book that quotes as wide as 52% of mid, with the risk gate
    having validated a completely different payload.

    Two assertions, and the first is the one that matters: NO order of any kind
    is submitted against the OCC symbol. The second is that the equity signal
    still goes through — a fix that blocked the buy instead would trade one
    silent failure for another.
    """
    client = FakeClient(positions=[_EnumPos(OCC_PUT, -1, AssetClass.US_OPTION)])
    om = OrderManager(client)

    res = om.handle(_buy("MSFT"))

    assert not any(OCC_PUT in str(o) for o in client.orders), \
        "the equity relay must never send an order against an option symbol"
    assert client.orders == [("market", "MSFT", 5.0, "buy", False)]
    assert res["status"] == "submitted_market"


def test_the_hazard_is_blocked_on_the_fallback_path_too():
    """The same scenario with a position object carrying no asset_class. This is
    the shape a raw-dict or older-SDK position takes, and the guard has to hold
    on the evidence it actually has."""
    client = FakeClient(positions=[_BarePos(OCC_PUT, -1)])
    om = OrderManager(client)

    res = om.handle(_buy("MSFT"))

    assert client.orders == [("market", "MSFT", 5.0, "buy", False)]
    assert res["status"] == "submitted_market"


def test_a_real_rotation_still_happens_when_an_option_is_also_open():
    """The guard must not disarm rotation itself. Holding SOXL shares (priority
    1) plus a short put, a higher-priority MSFT buy still closes SOXL — the
    equity close, and only that."""
    client = FakeClient(positions=[_EnumPos(OCC_PUT, -1, AssetClass.US_OPTION),
                                   _EnumPos("SOXL", 18, AssetClass.US_EQUITY)])
    om = OrderManager(client)
    rf.DECISIONS_FILE.write_text(json.dumps({
        "watchlist": ["SOXL", "MSFT"], "bias": {"SOXL": "long", "MSFT": "long"},
        "priority": {"SOXL": 1, "MSFT": 2}, "max_open_positions": 1}))

    res = om.handle(_buy("MSFT"))

    assert client.orders == [("close", "SOXL"), ("market", "MSFT", 5.0, "buy", False)]
    assert res["status"] == "submitted_market"


# ------------------------------------------------------ _open_order_symbols --
def test_a_working_option_order_is_not_a_working_equity_order():
    """A resting put quote must not consume the account's one equity slot.

    The put is a DAY limit at the bid that may sit unfilled until the close, and
    handle() blocks any entry while some OTHER symbol has a working order. Left
    unfiltered, one unfilled Friday put quote would refuse every equity entry
    for the rest of the session — a whole-session outage from an order that is
    not even competing for the same thing.
    """
    client = FakeClient(open_orders=[_order(OCC_PUT, AssetClass.US_OPTION)])
    assert OrderManager(client)._open_order_symbols() == set()


def test_a_working_option_order_with_no_asset_class_is_also_ignored():
    assert OrderManager(FakeClient(open_orders=[_order(OCC_PUT)]))._open_order_symbols() == set()


def test_a_working_equity_order_is_still_counted():
    """The other half: the slot guard must keep working for stocks, or two
    entries an hour apart both go full size (the failure it was written for)."""
    client = FakeClient(open_orders=[_order("SOXL", AssetClass.US_EQUITY),
                                     _order(OCC_PUT, AssetClass.US_OPTION)])
    assert OrderManager(client)._open_order_symbols() == {"SOXL"}


def test_a_working_option_order_does_not_block_an_equity_entry():
    """The end-to-end statement of the same thing, through handle(): flat book,
    one resting put quote, an MSFT buy — the buy must go."""
    client = FakeClient(positions=[],
                        open_orders=[_order(OCC_PUT, AssetClass.US_OPTION)])
    om = OrderManager(client)

    res = om.handle(_buy("MSFT"))

    assert res["status"] == "submitted_market"
    assert client.orders == [("market", "MSFT", 5.0, "buy", False)]


def test_a_short_put_plus_its_working_quote_still_lets_equity_trade():
    """Both halves of the hazard at once — the position AND the order — which is
    the real Friday-afternoon state of the account while a put quote rests."""
    client = FakeClient(positions=[_EnumPos(OCC_PUT, -1, AssetClass.US_OPTION)],
                        open_orders=[_order(OCC_PUT, AssetClass.US_OPTION)])

    res = OrderManager(client).handle(_buy("MSFT"))

    assert res["status"] == "submitted_market"
    assert client.orders == [("market", "MSFT", 5.0, "buy", False)]


# ------------------------------------------------------- _guard_equity_only --
def test_close_position_refuses_an_occ_symbol_and_names_itself():
    """A LOUD failure replacing a silent one. If an OCC symbol ever reaches here
    again the caller has a bug, and the message has to say WHICH caller —
    otherwise the next person sees a RiskError from somewhere in the order path
    and reads it as a market-data problem, which is exactly how the original
    version hid."""
    client = FakeClient(positions=[_EnumPos(OCC_PUT, -1, AssetClass.US_OPTION)])
    om = OrderManager(client)

    with pytest.raises(RiskError, match="_close_position") as e:
        om._close_position(OCC_PUT, client.positions)

    assert OCC_PUT in str(e.value)
    assert client.orders == [], "the guard must fire BEFORE any broker call"


def test_reference_price_refuses_an_occ_symbol_and_names_itself():
    """Outside regular hours this is the path that used to ask the STOCK quote
    endpoint for an option symbol. Failing here, by name, beats failing there
    with something that reads like a data outage."""
    om = OrderManager(FakeClient())

    with pytest.raises(RiskError, match="_reference_price") as e:
        om._reference_price(OCC_PUT, "buy")

    assert OCC_PUT in str(e.value)


@pytest.mark.parametrize("sym", ["MSFT", "SOXL"])
def test_the_guard_does_not_fire_on_an_equity(sym):
    """The guard's own false-positive check: a stock must still price and close
    normally, or the fix is worse than the bug."""
    client = FakeClient(positions=[_EnumPos(sym, 10, AssetClass.US_EQUITY)])
    om = OrderManager(client)

    assert om._reference_price(sym, "buy") > 0
    assert om._close_position(sym, client.positions) == "closed"


# =========================================================================== #
# sigengine.engine — the reconciler and the sizing arbitration
# =========================================================================== #

class _BoobyTrappedOption:
    """An option position whose equity-shaped fields EXPLODE when read.

    This is how the test observes a local variable it cannot see: `held` is
    built from `p.qty` and `p.symbol`, so a position that never has those read
    is a position that never entered `held`. Asserting on the absence of an
    alert alone would pass even if the filter were deleted, because the OCC
    string simply never matches an `enabled` key — a test that cannot fail is
    not a test.
    """

    symbol = OCC_PUT
    asset_class = AssetClass.US_OPTION

    @property
    def qty(self):
        raise RuntimeError("an option's qty is CONTRACTS — the reconciler must "
                           "never read it as a share count")


def _rev(action, reason, ts="2026-08-27 15:00:00+00:00", price=25.0):
    return {"ts": pd.Timestamp(ts), "action": action, "reason": reason,
            "price": price, "strategy": "macd_sma"}


def _fetched(**per_symbol):
    return {sym: (evs, pd.Timestamp("2026-08-27 16:00:00+00:00"), "ibkr")
            for sym, evs in per_symbol.items()}


class ReconClient:
    def __init__(self, positions=None, open_orders=None):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])

    def get_all_positions(self):
        return list(self.positions)

    def get_open_orders(self, symbol=None):
        return list(self.open_orders)


class ReconOM:
    def __init__(self):
        self.handled: list = []

    def handle(self, payload):
        self.handled.append((payload.ticker, payload.action, payload.strategy))
        return {"status": "closed"}


@pytest.fixture
def recon_journal(tmp_path):
    return tmp_path / "trades.jsonl"


def _journal_events(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_the_reconciler_never_reads_an_option_position(recon_journal):
    """It must skip the row BEFORE touching any equity-shaped field. A short put
    reports qty -1 and the pass's long-only rule reads a negative qty as a
    phantom short — the one condition this account has actually seen and the one
    the operator is told never to trade against."""
    eng._PENDING_EXIT_SEEN.clear()
    om = ReconOM()

    eng._reconcile_positions({"SOXL": {}}, _fetched(SOXL=[]),
                             ReconClient([_BoobyTrappedOption()]), om, dry_run=False)

    assert om.handled == []
    assert _journal_events(recon_journal) == [], \
        "an option position is not a divergence and must not alert"


def test_an_equity_beside_an_option_is_still_reconciled(recon_journal):
    """The filter removes the option and nothing else. SOXL was bought and then
    exited in the simulation while the broker still shows 18 shares — that is a
    missed exit, and it must still be closed with a put sitting in the same
    positions list."""
    eng._PENDING_EXIT_SEEN.clear()
    events = [_rev("buy", "macd_sma_bullish", ts="2026-08-26 14:00:00+00:00"),
              _rev("close", "stop", ts="2026-08-27 15:00:00+00:00")]
    om = ReconOM()

    eng._reconcile_positions(
        {"SOXL": {}}, _fetched(SOXL=events),
        ReconClient([_BoobyTrappedOption(), _BarePos("SOXL", 18)]), om, dry_run=False)

    assert om.handled == [("SOXL", "close", "reconcile")]


# --------------------------------------------------- fire_event vs. collateral

class SizingClient:
    def __init__(self, equity=100000.0, positions=None):
        self._equity = equity
        self._positions = list(positions or [])

    def get_account(self):
        return type("Acct", (), {"equity": str(self._equity),
                                 "last_equity": str(self._equity)})()

    def get_all_positions(self):
        return list(self._positions)


class SizingOM:
    def __init__(self):
        self.calls: list = []

    def handle(self, payload):
        self.calls.append({"ticker": payload.ticker, "action": payload.action,
                           "qty": payload.qty})
        return {"status": "submitted_market"}


EQ_CFG = {"fast": 9, "slow": 65, "window": 300, "interval": "1h",
          "source": "yfinance", "deploy": 0.97, "stop": 0.06, "enabled": True}


def _buy_ev(price=250.0):
    return {"ts": pd.Timestamp("2026-08-27 15:00:00-04:00"), "action": "buy",
            "price": price, "reason": "macd_sma_bullish", "strategy": "macd_sma"}


class _Settings:
    webhook_secret = "testsecret"


def _fire(monkeypatch, pledge, equity=100000.0, price=250.0):
    monkeypatch.setattr(eng, "_pledged_collateral", lambda: pledge)
    om = SizingOM()
    out = eng.fire_event("MSFT", _buy_ev(price), EQ_CFG, SizingClient(equity),
                         om, _Settings(), dry_run=False)
    return out, om


def test_an_unpledged_account_sizes_off_full_equity(monkeypatch):
    """The baseline the subtraction must not disturb: with no put open the
    engine sizes exactly as it did before options existed."""
    out, om = _fire(monkeypatch, 0.0)
    assert out["status"] == "submitted_market"
    assert om.calls == [{"ticker": "MSFT", "action": "buy",
                         "qty": float(int(100000 * 0.97 / 250))}]


def test_pledged_collateral_is_subtracted_before_sizing(monkeypatch):
    """THE arbitration point. Alpaca leaves `equity` unchanged when cash is
    pledged behind a short put, so without this subtraction the two books claim
    the same dollars and a broker rejection — not a decision — picks the winner.

    $9,000 of collateral against $100,000 of equity must size the entry off
    $91,000, not $100,000.
    """
    out, om = _fire(monkeypatch, 9000.0)
    assert out["status"] == "submitted_market"
    assert om.calls[0]["qty"] == float(int(91000 * 0.97 / 250))


def test_the_sleeves_reserve_is_subtracted_even_with_no_put_open(monkeypatch, tmp_path):
    """THE CAPITAL SPLIT, from the share book's side. With the sleeve enabled at
    a 20% reserve and NOTHING pledged yet, the share book still sizes off
    $80,000 — the reserve is a policy number, not a fact that only appears once
    a put is open. And it never sizes off less than the live pledge."""
    import json
    import relay.csp_manager as csp
    (tmp_path / "csp.json").write_text(json.dumps({"enabled": True, "reserve_frac": 0.20}))
    monkeypatch.setattr(csp, "CSP_CONFIG_FILE", tmp_path / "csp.json")

    out, om = _fire(monkeypatch, 0.0)
    assert om.calls[0]["qty"] == float(int(80000 * 0.97 / 250))

    out, om = _fire(monkeypatch, 30000.0)           # a pledge bigger than the reserve
    assert om.calls[0]["qty"] == float(int(70000 * 0.97 / 250)), "the larger of the two wins"


def test_a_bigger_pledge_shrinks_the_entry_further(monkeypatch):
    """Monotonic in the pledge — the relationship, not a single arithmetic
    coincidence."""
    small, _ = _fire(monkeypatch, 9000.0)
    big, _ = _fire(monkeypatch, 40000.0)
    assert big["qty"] < small["qty"] < float(int(100000 * 0.97 / 250))


def test_a_pledge_larger_than_equity_skips_instead_of_going_negative(monkeypatch):
    """The clamp, and why it is `max(0.0, ...)` rather than bare arithmetic.

    A negative equity yields a NEGATIVE qty, and a negative qty on a `buy`
    OrderRequest is not a smaller trade — it is a nonsense order the relay would
    hand to the broker. The honest outcome when the option book has taken all
    the cash is no equity trade at all, journaled as a skip.
    """
    out, om = _fire(monkeypatch, 150000.0)

    assert out["status"] == "skip"
    assert "computed qty" in out["reason"]
    assert om.calls == [], "no order may be submitted when there is no free cash"
    assert out.get("qty", 0.0) >= 0.0


def test_a_pledge_exactly_equal_to_equity_also_skips(monkeypatch):
    """The boundary. equity - pledge == 0 is the case the clamp and the
    `qty <= 0` guard have to agree about; disagreeing would send qty 0.0."""
    out, om = _fire(monkeypatch, 100000.0)
    assert out["status"] == "skip"
    assert om.calls == []


# ------------------------------------------- _pledged_collateral must not raise

def test_pledged_collateral_is_zero_when_the_options_client_cannot_be_built(monkeypatch):
    """FAIL OPEN, AND ONLY HERE. Everywhere else in this system an unknown
    account state fails closed, but this one number is different: erring high
    would wrongly starve the equity book of cash it actually has, and returning
    0.0 restores precisely the pre-options behaviour, with the broker as the
    backstop — which is where it was before any of this was written.

    Missing keys, a network refusal, an alpaca-py version without the options
    surface: all of them arrive here as an exception from the constructor.
    """
    monkeypatch.setattr(csp_mod, "config", lambda *a, **k: {"enabled": True})

    def _boom(*a, **k):
        raise RuntimeError("no options credentials on this account")

    monkeypatch.setattr(oc_mod, "OptionsClient", _boom)

    assert eng._pledged_collateral() == 0.0


def test_pledged_collateral_is_zero_when_the_strategy_is_switched_off(monkeypatch):
    """With csp.enabled false the options client must not even be constructed —
    the equity engine polls every 300s and has no business making a broker call
    for a strategy nobody turned on."""
    monkeypatch.setattr(csp_mod, "config", lambda *a, **k: {"enabled": False})

    def _never(*a, **k):
        raise AssertionError("OptionsClient must not be constructed when csp is off")

    monkeypatch.setattr(oc_mod, "OptionsClient", _never)

    assert eng._pledged_collateral() == 0.0


def test_pledged_collateral_is_zero_when_the_config_itself_fails(monkeypatch):
    """A corrupt or unreadable decisions.json must not stop the equity engine
    trading. It is the same fail-open rule one layer up."""
    def _boom(*a, **k):
        raise RuntimeError("decisions.json is not readable")

    monkeypatch.setattr(csp_mod, "config", _boom)

    assert eng._pledged_collateral() == 0.0


def test_a_pledge_reading_of_zero_leaves_the_engine_exactly_as_it_was(monkeypatch):
    """The consequence of the previous three, stated as behaviour: when the
    pledge cannot be read, sizing is identical to an account with no options at
    all — no silent shrinkage, no silent block."""
    monkeypatch.setattr(csp_mod, "config", lambda *a, **k: {"enabled": True})
    monkeypatch.setattr(oc_mod, "OptionsClient",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    om = SizingOM()

    out = eng.fire_event("MSFT", _buy_ev(250.0), EQ_CFG, SizingClient(100000.0),
                         om, _Settings(), dry_run=False)

    assert out["status"] == "submitted_market"
    assert om.calls[0]["qty"] == float(int(100000 * 0.97 / 250))

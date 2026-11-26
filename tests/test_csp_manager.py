"""The weekly SOXL cash-secured put: its arithmetic, its gates, and its refusals.

This strategy is unusual in that almost all of its value is in the weeks it does
NOT trade. A 22-28% OTM weekly put wins ~95% of the time by construction, so the
only ways to lose money are (a) selling a strike that is too close, (b) selling
into a regime that has stopped paying for the risk, and (c) crossing a spread so
wide that the edge is gone before the trade starts. Every one of those is a
REFUSAL path, and a refusal that silently turns into a sale is invisible until
the week it costs $9,700. So the refusals get as much coverage here as the sale.

Three design choices are deliberate and are pinned rather than questioned:

  FIXED DISTANCE, NOT DELTA. The strike is chosen at a fixed 22-28% OTM band and
      never from trailing vol. The vol-targeted variant backtests at -5.9% CAGR
      with an -82.7% drawdown because vol CLUSTERS: trailing vol lags a regime
      change, so the rule sells its closest strike immediately before the crash.
      Tests here assert the band is a constant, not that it adapts.
  A PREMIUM FLOOR. Breakeven is an IV of ~106-108% against SOXL's 99.8% median
      realized, so a cheap week is a week not to sell. Nothing else in the
      design stops the strategy selling into an uncompensated regime.
  SELL AT THE BID, ON A LIMIT. A market fill on a book quoting 52% of mid is
      unbounded in the wrong direction; an unfilled limit is merely a week off.

Everything runs against FAKES. No client is constructed, no order is ever
submitted, and the journal is redirected to tmp_path — a test that reached the
live trades.jsonl would corrupt the audit trail the review agent reads.
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest

# Dummy creds before importing the relay, mirroring tests/test_relay.py — the
# relay reads .env at import time and get_settings() raises without them.
os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import relay.csp_manager as csp  # noqa: E402
from relay.options_client import PutQuote, _strike_from_occ  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes. Nothing here talks to a broker.
# ---------------------------------------------------------------------------
def _occ(strike: float, root: str = "SOXL", yymmdd: str = "260904",
         right: str = "P") -> str:
    """Build an OCC symbol: root + YYMMDD + C/P + strike in thousandths."""
    return f"{root}{yymmdd}{right}{int(round(strike * 1000)):08d}"


def _quote(strike, bid, ask, underlying=100.0, expiry="2026-09-04") -> PutQuote:
    return PutQuote(symbol=_occ(strike), strike=float(strike), expiry=expiry,
                    bid=float(bid), ask=float(ask), underlying=float(underlying))


def _equity_pos(symbol, qty="100", **kw):
    attrs = {"symbol": symbol, "qty": qty, "side": "long",
             "asset_class": "us_equity", "avg_entry_price": "90.0",
             "unrealized_pl": "12.5", "unrealized_plpc": "0.0138"}
    attrs.update(kw)
    return type("Pos", (), attrs)()


def _option_pos(symbol=None, qty="-1", **kw):
    attrs = {"symbol": symbol or _occ(90), "qty": qty, "side": "short",
             "asset_class": "us_option", "unrealized_pl": "4.0",
             "market_value": "-36.0"}
    attrs.update(kw)
    return type("OptPos", (), attrs)()


class FakeOptions:
    """Stand-in for OptionsClient. Records every call; submits nothing.

    sell_put_to_open only appends to `self.orders`; buy_put_to_close raises,
    because no test in this file has any business closing a contract and a
    silent call would mean the manager grew a code path nobody asked for.
    """

    def __init__(self, *, options=(), expirations=("2026-09-04",), chain=(),
                 bp=100_000.0, spot=100.0, open_orders=(), equity=None):
        self._options = list(options)
        self._expirations = list(expirations)
        self._chain = list(chain)
        self._bp = bp
        self._equity = equity
        self.spot = spot                       # read by the patched csp._spot
        self.orders: list = []
        self.expiration_calls: list = []
        self.chain_calls: list = []
        self.option_positions_error: Exception | None = None
        # A working sell-to-open limit is a commitment with no position behind
        # it — the manager refuses to stack a second contract on top of one, and
        # fails CLOSED if it cannot find out. A double without this method makes
        # every run take the fail-closed branch.
        self._open_orders = list(open_orders)
        self.open_orders_error: Exception | None = None

    def open_option_orders(self):
        if self.open_orders_error is not None:
            raise self.open_orders_error
        return list(self._open_orders)

    def option_positions(self):
        if self.option_positions_error is not None:
            raise self.option_positions_error
        return list(self._options)

    def expirations(self, underlying, gte, lte):
        self.expiration_calls.append((underlying, gte, lte))
        return list(self._expirations)

    def put_chain(self, underlying, expiry, lo, hi, underlying_price):
        self.chain_calls.append((underlying, expiry, lo, hi, underlying_price))
        return list(self._chain)

    def account_snapshot(self):
        """Cash AND equity, because sizing needs both. `equity` defaults to the
        buying power: these doubles model a FLAT book, where the two are the
        same number. A test that wants the RISK BUDGET to bite rather than the
        cash gate sets them apart, or raises reserve_frac."""
        return {"options_buying_power": self._bp,
                "equity": self._bp if self._equity is None else self._equity}

    def options_buying_power(self):                       # pragma: no cover
        return self._bp

    def sell_put_to_open(self, occ_symbol, limit_price, qty=1):
        self.orders.append((occ_symbol, limit_price, qty))
        return {"id": "fake-option-order"}

    def buy_put_to_close(self, occ_symbol, limit_price, qty=1):   # pragma: no cover
        raise AssertionError("no test may close a contract")


def _cfg(**over) -> dict:
    """DEFAULTS with the strategy switched on, plus per-test overrides."""
    cfg = dict(csp.DEFAULTS)
    cfg["enabled"] = True
    cfg.update(over)
    return cfg


def _decisions(**over) -> dict:
    return {"csp": _cfg(**over)}


# A chain whose furthest-out strike is genuinely the best choice: 72 is 28% OTM
# (the far edge of the band), quotes tight, and clears the premium floor.
GOOD_CHAIN = [_quote(78, 0.60, 0.65), _quote(75, 0.50, 0.54), _quote(72, 0.40, 0.44)]


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Redirect BOTH files the module can touch and stub the only live call.

    JOURNAL_FILE and DECISIONS_FILE are module globals bound at import, so they
    are patched on relay.csp_manager itself. `_spot` is the one function that
    reaches the network (it builds a StockHistoricalDataClient out of the
    options client's credentials); it is replaced with a read of the fake.
    """
    monkeypatch.setattr(csp, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(csp, "DECISIONS_FILE", tmp_path / "decisions.json")
    # config() reads the operator POLICY file, which deliberately lives outside
    # decisions.json (the research harness rewrites that one wholesale)
    monkeypatch.setattr(csp, "CSP_CONFIG_FILE", tmp_path / "csp.json")
    monkeypatch.setattr(csp, "_spot", lambda opt, und: getattr(opt, "spot", None))
    yield


def _sold_a_put(und: str = "SOXL", ago_days: float = 3.0) -> None:
    """Lay down the evidence a real assignment would have left behind.

    detect_assignment requires a recent csp_sell for the underlying before it
    will call shares an assignment. That is not bureaucracy: SOXL is an ENABLED
    trading symbol in sigengine/config.json, so the equity engine buys and sells
    it in the ordinary course, and without this check the engine's own position
    would be relabelled as an assignment and described that way to the
    presenter in the recap.
    """
    import time as _t
    csp.JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with csp.JOURNAL_FILE.open("a") as f:
        f.write(json.dumps({"event": "csp_sell", "ticker": und,
                            "ts": _t.time() - ago_days * 86400}) + "\n")


def _journal_rows() -> list[dict]:
    p = csp.JOURNAL_FILE
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _one_csp_event(expected_event: str) -> dict:
    """The journal must carry EXACTLY ONE csp_* row, and it must be this one.

    Every outcome is journaled — that is what makes shadow mode informative —
    but a path that journals twice would double-count a week in the recap, and
    one that journals nothing makes the strategy unwatchable while it is unfunded.
    """
    rows = [r for r in _journal_rows() if str(r.get("event", "")).startswith("csp_")]
    assert len(rows) == 1, f"expected one csp_* row, got {[r['event'] for r in rows]}"
    assert rows[0]["event"] == expected_event
    return rows[0]


# ---------------------------------------------------------------------------
# PutQuote — the arithmetic every gate is built on
# ---------------------------------------------------------------------------
def test_mid_is_the_midpoint_of_a_two_sided_quote():
    assert _quote(90, 0.40, 0.60).mid == pytest.approx(0.50)


def test_mid_falls_back_to_the_bid_when_a_side_is_missing():
    """A one-sided quote has no midpoint. Returning the BID rather than half of
    a zero is the conservative direction: it is the price we could actually sell
    at, so nothing downstream can be told the contract is worth more than it is."""
    assert _quote(90, 0.40, 0.0).mid == pytest.approx(0.40)
    assert _quote(90, 0.0, 0.60).mid == pytest.approx(0.0)


def test_spread_frac_is_measured_against_mid_not_against_the_bid():
    """0.30/0.35 is the shape of a tradeable far-OTM weekly: 0.05 wide on a
    0.325 mid = 15%. Measuring against the bid instead would read 16.7% and the
    two diverge fast on the wide strikes the liquidity gate exists to reject."""
    assert _quote(90, 0.30, 0.35).spread_frac == pytest.approx(0.05 / 0.325)


def test_the_real_2026_08_26_liquidity_gap_is_visible_in_spread_frac():
    """The measurement that made liquidity a selection criterion: strike 95
    quoted ~7% wide while strike 97 quoted ~52%. The further-out strike was also
    the tighter one, which is why picking the furthest is not a trade-off here."""
    near = _quote(97, 0.30, 0.51)
    far = _quote(95, 0.28, 0.30)
    assert far.spread_frac < 0.10 < near.spread_frac
    assert far.strike < near.strike


def test_a_quote_with_no_mid_is_maximally_wide_not_a_zero_division():
    """mid == 0 must not raise and must not read as a perfect spread — 1.0 is
    above any sane max_spread_frac, so the strike is rejected rather than
    accidentally preferred."""
    assert _quote(90, 0.0, 0.0).spread_frac == 1.0


def test_collateral_is_strike_times_one_hundred():
    """Alpaca has NO naked-put tier at any approval level, so the full strike is
    pledged. The premium nets off it, but the design budgets to the gross —
    erring high is the direction that cannot cause a rejection."""
    assert _quote(90, 0.4, 0.5).collateral == 9000.0
    assert _quote(72.5, 0.4, 0.5).collateral == 7250.0


def test_collateral_is_why_this_account_cannot_trade_it_yet():
    """The whole reason for shadow mode, as arithmetic: a ~$97 strike needs
    $9,700 against $4,848 of options buying power."""
    assert _quote(97, 0.3, 0.4).collateral == 9700.0


def test_otm_frac_is_the_distance_below_spot():
    q = _quote(78, 0.6, 0.65, underlying=100.0)
    assert q.otm_frac == pytest.approx(0.22)


def test_an_in_the_money_strike_has_a_negative_otm_frac():
    """Signed, not absolute — an ITM put must fall out of the band rather than
    look like a deep-OTM one at the same distance."""
    assert _quote(110, 0.6, 0.65, underlying=100.0).otm_frac == pytest.approx(-0.10)


def test_otm_frac_is_zero_when_spot_is_unknown_rather_than_dividing_by_zero():
    assert _quote(90, 0.4, 0.5, underlying=0.0).otm_frac == 0.0


def test_strike_round_trips_through_the_occ_symbol():
    """The OCC tail is the strike in thousandths of a dollar. This is the only
    place a strike is recovered from a symbol — pledged_collateral and
    assignment_state both re-derive it the same way from a broker position,
    where no PutQuote exists to ask."""
    for strike in (90.0, 72.5, 5.0, 1234.5, 0.5):
        assert _strike_from_occ(_occ(strike)) == pytest.approx(strike)


def test_the_documented_occ_example_parses():
    assert _strike_from_occ("SOXL260904P00090000") == 90.0


# ---------------------------------------------------------------------------
# pick_strike — the selection, and the three ways it refuses
# ---------------------------------------------------------------------------
def test_the_furthest_out_strike_in_the_band_is_taken():
    """Further out is strictly safer, so within the band the tie-break is
    distance and nothing else."""
    q, why = csp.pick_strike(GOOD_CHAIN, _cfg())

    assert why == "ok"
    assert q.strike == 72.0
    assert q.otm_frac == pytest.approx(0.28)


def test_a_further_strike_beats_a_nearer_one_that_pays_more():
    """THE selection rule, stated as the case that makes it non-obvious. A
    yield-maximising picker takes the 78 at $110; this one takes the 72 at $40,
    because the extra 6% of distance is the entire reason the win rate is ~95%
    and premium is what is being traded away for it."""
    chain = [_quote(78, 1.10, 1.16), _quote(72, 0.40, 0.44)]

    q, _ = csp.pick_strike(chain, _cfg())

    assert q.strike == 72.0, "distance wins over premium inside the band"


def test_a_strike_beyond_the_far_edge_is_not_taken_either():
    """The band has two sides. Past max_otm the premium stops paying for the
    slot at all, so 65 (35% OTM) is not 'even safer', it is out of scope."""
    chain = [_quote(65, 0.20, 0.22), _quote(72, 0.40, 0.44)]

    q, _ = csp.pick_strike(chain, _cfg())

    assert q.strike == 72.0


def test_the_band_is_a_fixed_distance_and_ignores_how_the_quotes_are_priced():
    """Pinning the deliberate choice: identical strikes with wildly different
    premiums (a calm week vs a panic week) select the SAME contract. If a future
    change made the strike vol- or delta-derived this test is the one that
    breaks, and the -82.7%-drawdown backtest is the reason it must."""
    calm = [_quote(78, 0.40, 0.43), _quote(72, 0.30, 0.32)]
    panic = [_quote(78, 4.00, 4.20), _quote(72, 2.50, 2.60)]

    assert csp.pick_strike(calm, _cfg())[0].strike == \
        csp.pick_strike(panic, _cfg())[0].strike == 72.0


def test_an_empty_band_is_a_refusal_that_names_the_band():
    """SOXL does not list a strike at every dollar. A week with nothing between
    22% and 28% out is a week off, not an invitation to take the nearest thing."""
    chain = [_quote(90, 0.6, 0.65), _quote(60, 0.1, 0.12)]   # 10% and 40% OTM

    q, why = csp.pick_strike(chain, _cfg())

    assert q is None
    assert "22%" in why and "28%" in why


def test_an_empty_chain_refuses_rather_than_raising():
    q, why = csp.pick_strike([], _cfg())
    assert q is None and "no listed strike" in why


def test_an_untradeable_band_is_refused_and_the_tightest_is_named():
    """Every strike wider than max_spread_frac. The message must carry the
    TIGHTEST one because that is the number an operator retunes against — the
    refusal is only actionable if it says how close the book came."""
    chain = [_quote(78, 1.00, 2.00),      # 67% of mid
             _quote(74, 1.00, 1.60),      # 46% of mid — the tightest
             _quote(72, 0.50, 1.50)]      # 100% of mid

    q, why = csp.pick_strike(chain, _cfg())

    assert q is None
    assert "too wide" in why
    assert "74" in why, f"the tightest strike must be named: {why}"
    assert "46%" in why


def test_a_strike_exactly_at_the_spread_limit_is_still_tradeable():
    """The bound is inclusive: a strike sitting exactly on max_spread_frac is
    the last tradeable one, not the first rejected one. The limit is taken from
    the quote itself so the assertion is about the comparison, not about whether
    two independently-computed floats happen to land on the same bits."""
    quote = _quote(72, 0.40, 0.52)
    cfg = _cfg(min_credit_frac=0.0, max_spread_frac=quote.spread_frac)

    q, _ = csp.pick_strike([quote], cfg)

    assert q is not None and q.strike == 72.0


def test_a_cheap_week_is_refused_and_the_best_payer_is_named():
    """The premium floor. Both strikes quote tightly and both sit in the band —
    the only thing wrong is that the regime has stopped paying, which is exactly
    the condition nothing else in the design detects. The message names the best
    payer and both percentages so the refusal can be audited against the tape."""
    chain = [_quote(78, 0.10, 0.11),    # $10 on $7,800 = 0.128%
             _quote(72, 0.15, 0.17)]    # $15 on $7,200 = 0.208% — the best payer

    q, why = csp.pick_strike(chain, _cfg())

    assert q is None
    assert "premium below the floor" in why
    assert "72" in why, f"the best payer must be named: {why}"
    assert "0.208%" in why and "0.350%" in why


def test_the_floor_is_measured_per_dollar_of_collateral_not_per_contract():
    """$26 of premium clears the 0.35% floor on a $7,200 strike and misses it on
    a $9,000 one. A flat dollar floor would quietly loosen as strikes rose."""
    cheap_strike = _quote(72, 0.26, 0.27, underlying=100.0)
    rich_strike = _quote(90, 0.26, 0.27, underlying=125.0)   # also 28% OTM

    assert csp.pick_strike([cheap_strike], _cfg())[0] is cheap_strike
    assert csp.pick_strike([rich_strike], _cfg())[0] is None


def test_a_wide_strike_is_dropped_before_the_premium_floor_is_judged():
    """Filter order matters for the MESSAGE. The 78 pays plenty but is
    untradeable; the 72 is tradeable but cheap. The operator must be told about
    the premium — the strike that is actually reachable — not about the spread
    on a strike that was never a candidate."""
    chain = [_quote(78, 2.00, 4.00),     # rich, 67% wide
             _quote(72, 0.15, 0.17)]     # tight, below the floor

    q, why = csp.pick_strike(chain, _cfg())

    assert q is None
    assert "premium below the floor" in why and "72" in why


# ---------------------------------------------------------------------------
# next_expiry — a weekly strategy must land on the weekly
# ---------------------------------------------------------------------------
def test_the_friday_is_preferred_over_a_nearer_wednesday():
    """SOXL lists Monday, Wednesday AND Friday weeklies. Taking the first expiry
    in the 5-9 day window on a Thursday picks 2026-09-02 (Wed) — a 5-day
    contract at a strike distance calibrated for a full week."""
    opt = FakeOptions(expirations=["2026-09-02", "2026-09-04"])

    assert csp.next_expiry(opt, _cfg(), date(2026, 8, 27)) == "2026-09-04"


def test_a_holiday_week_with_no_friday_still_trades():
    """Preferring the Friday must not become requiring one, or a single holiday
    silently stands the strategy down for a week."""
    opt = FakeOptions(expirations=["2026-09-02"])

    assert csp.next_expiry(opt, _cfg(), date(2026, 8, 27)) == "2026-09-02"


def test_the_window_asked_for_is_dte_min_to_dte_max_inclusive():
    opt = FakeOptions()
    csp.next_expiry(opt, _cfg(), date(2026, 8, 27))

    assert opt.expiration_calls == [("SOXL", "2026-09-01", "2026-09-05")]


def test_no_listed_expiry_is_none_not_an_exception():
    assert csp.next_expiry(FakeOptions(expirations=[]), _cfg(), date(2026, 8, 27)) is None


# ---------------------------------------------------------------------------
# run() — the gates, in the order they fire
# ---------------------------------------------------------------------------
def _run(opt, positions=(), sell_shares=None, **over):
    return csp.run(opt, list(positions), decisions=_decisions(**over),
                   today=date(2026, 8, 27), log=lambda *a: None, sell_shares=sell_shares)


def test_disabled_does_nothing_and_says_so():
    """The opt-in switch is first, before any broker read — a disabled strategy
    must not even fetch a chain."""
    opt = FakeOptions(chain=GOOD_CHAIN)

    dec = _run(opt, enabled=False, shadow=False)

    assert dec.action == "skip" and "enabled is false" in dec.reason
    assert opt.orders == [] and opt.chain_calls == []
    _one_csp_event("csp_skip")


def test_a_held_share_book_no_longer_blocks_the_sale():
    """THE OLD "GATE 1" IS GONE, ON PURPOSE.

    The sleeve used to refuse while the share book held anything, and the
    all-in share book holds something ~79% of the time — so the put sold about
    11 weeks a year. Since 2026-08-28 the sleeve has its own reserve and sells
    from it every week; what the share book holds is the share book's business.
    """
    opt = FakeOptions(chain=GOOD_CHAIN)

    dec = _run(opt, positions=[_equity_pos("TQQQ")], shadow=False)

    assert dec.action == "sell", dec.reason
    assert opt.orders == [(_occ(72), 0.40, 1)]
    _one_csp_event("csp_sell")


def test_last_weeks_assigned_shares_are_sold_before_the_new_put():
    """"SOLD WITHIN A WEEK" IS STEP ONE OF EVERY RUN. The shares a put was
    assigned into are sold through the relay at the very next weekly run, then
    the new put goes out. The seller is called with the underlying and the
    exact share count, and the sale is journaled."""
    _sold_a_put()                                  # the corroborating csp_sell
    sold = []
    opt = FakeOptions(chain=GOOD_CHAIN)

    dec = _run(opt, positions=[_equity_pos("SOXL", qty="100")], shadow=False,
               sell_shares=lambda sym, qty: sold.append((sym, qty)) or "closed")

    assert sold == [("SOXL", 100.0)]
    assert dec.action == "sell" and opt.orders == [(_occ(72), 0.40, 1)]
    rows = [r for r in _journal_rows() if r["event"] == "csp_assignment_sold"]
    assert len(rows) == 1 and rows[0]["qty"] == 100.0


def test_shares_the_engine_bought_are_not_the_sleeves_to_sell():
    """The same 100 SOXL with NO put behind them are the share book's own
    position. The seller must not be called for them."""
    sold = []
    dec = _run(FakeOptions(chain=GOOD_CHAIN), positions=[_equity_pos("SOXL", qty="100")],
               shadow=False, sell_shares=lambda sym, qty: sold.append((sym, qty)) or "closed")

    assert sold == []
    assert dec.action == "sell"


def test_an_option_in_the_equity_list_is_not_mistaken_for_shares():
    """get_all_positions() is an unfiltered passthrough and includes OPTION rows
    with `symbol` set to the OCC ticker. If the share-book gate counted those,
    the strategy's own open put would permanently block its next sale — and the
    week it expired worthless it would still look 'held'."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0)

    dec = _run(opt, positions=[_option_pos()], shadow=False)

    assert dec.action == "sell", f"an OCC row is not the share book: {dec.reason}"


def test_an_already_open_short_put_blocks_a_second_one():
    """One contract at a time. Two open puts is $19,400 of collateral on an
    account that cannot secure one, and the manager has no concept of a ladder."""
    opt = FakeOptions(options=[_option_pos()], chain=GOOD_CHAIN)

    dec = _run(opt, shadow=False)

    assert dec.action == "skip" and "already open" in dec.reason
    assert opt.orders == []
    _one_csp_event("csp_skip")


def test_assigned_shares_still_held_shrink_the_sleeve():
    """With no seller (dry-run) or in shadow, the assigned shares stay put and
    keep occupying the reserve, so the new put is sized around them — and when
    they fill the reserve, the refusal SAYS that is why."""
    _sold_a_put()
    opt = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0, equity=100_000.0)

    dec = _run(opt, positions=[_equity_pos("SOXL", qty="100", market_value="8400")],
               shadow=False, reserve_frac=0.10, sell_shares=None)

    assert dec.action == "skip" and dec.contracts == 0
    assert "assigned shares still in it" in dec.reason
    assert opt.orders == []


def test_a_failed_share_sale_is_journaled_and_sized_around():
    """The seller raising must not take the run down: the shares are treated as
    still held, the failure is journaled, and the put is sized around them."""
    _sold_a_put()
    def boom(sym, qty):
        raise RuntimeError("broker 500")
    dec = _run(FakeOptions(chain=GOOD_CHAIN, bp=100_000.0, equity=100_000.0),
               positions=[_equity_pos("SOXL", qty="100", market_value="8400")],
               shadow=False, sell_shares=boom)

    assert [r for r in _journal_rows() if r["event"] == "csp_assignment_sell_failed"]
    assert dec.action == "sell", "one contract still fits beside the shares at a 20% reserve"


def test_no_expiry_in_the_window_is_a_skip():
    opt = FakeOptions(expirations=[], chain=GOOD_CHAIN)

    dec = _run(opt, shadow=False)

    assert dec.action == "skip" and "expiry" in dec.reason
    assert opt.orders == [] and opt.chain_calls == []
    _one_csp_event("csp_skip")


def test_no_spot_price_is_a_skip_before_any_chain_is_fetched():
    """Every downstream number — the band, the strike, the collateral — is
    measured against spot. Without it there is nothing to be 22% below."""
    opt = FakeOptions(spot=None, chain=GOOD_CHAIN)

    dec = _run(opt, shadow=False)

    assert dec.action == "skip" and "no usable SOXL price" in dec.reason
    assert opt.chain_calls == []
    _one_csp_event("csp_skip")


def test_the_chain_is_requested_around_the_band_with_a_margin():
    """The chain window is deliberately wider than the band on both sides, so
    that a refusal message can name the near-miss strike instead of reporting an
    empty band whenever the listed strikes fall just outside."""
    opt = FakeOptions(chain=GOOD_CHAIN)
    _run(opt, shadow=False)

    (und, exp, lo, hi, spot) = opt.chain_calls[0]
    assert und == "SOXL" and exp == "2026-09-04" and spot == 100.0
    assert lo == pytest.approx(67.0) and hi == pytest.approx(83.0)
    assert lo < 72.0 and hi > 78.0, "the window must contain the whole band"


def test_a_refused_strike_is_journaled_as_a_skip_with_the_reason():
    """A refusal is a result. The chain arrived, the band was empty, and that
    fact belongs in the audit trail with the spot it was measured against."""
    opt = FakeOptions(chain=[_quote(90, 0.6, 0.65)])          # 10% OTM only

    dec = _run(opt, shadow=False)

    assert dec.action == "skip" and "no listed strike" in dec.reason
    assert opt.orders == []
    row = _one_csp_event("csp_skip")
    assert row["spot"] == 100.0


# ------------------------------------------------- gate 4: can we pay for it ---
def test_an_unaffordable_week_in_shadow_mode_reports_the_whole_trade():
    """TODAY'S ACTUAL STATE. The trade is chosen and journaled in full even
    though it cannot be paid for — that is what makes an unfunded strategy
    watchable for weeks before it is ever turned on."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=4_848.0)

    dec = _run(opt, reserve_frac=1.0, shadow=True)

    assert dec.action == "shadow"
    assert opt.orders == []
    assert dec.symbol == _occ(72) and dec.strike == 72.0
    assert dec.limit == pytest.approx(0.40), "the intended price is still the bid"
    assert dec.collateral == 7_200.0
    _one_csp_event("csp_shadow")


def test_the_shortfall_counts_the_buy_back_reserve_not_just_the_collateral():
    """$7,200 of collateral is not the requirement. An account funded to exactly
    the strike cannot BUY THE PUT BACK — that also consumes options buying power
    — so its only exit from a losing contract is to accept assignment. The 15%
    reserve is what keeps a losing week a decision instead of a fact."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=4_848.0)

    dec = _run(opt, reserve_frac=1.0, shadow=True)

    need = 7_200.0 * 1.15
    assert dec.shortfall == pytest.approx(need - 4_848.0)
    assert dec.shortfall == pytest.approx(3_432.0)
    assert dec.options_bp == 4_848.0
    assert "8,280" in dec.reason and "4,848" in dec.reason


def test_an_account_funded_to_the_bare_collateral_still_cannot_sell():
    """The other half of the reserve: $7,200 covers the strike exactly and is
    still refused."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=7_200.0)

    dec = _run(opt, reserve_frac=1.0, shadow=False)

    assert dec.action == "skip"
    assert dec.shortfall == pytest.approx(1_080.0)
    assert opt.orders == []


def test_an_unaffordable_week_with_shadow_off_is_a_plain_skip():
    """Shadow is a REPORTING mode, not a funding one. With it off the same
    refusal is journaled as a skip — and it is still a refusal, never an order."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=4_848.0)

    dec = _run(opt, reserve_frac=1.0, shadow=False)

    assert dec.action == "skip"
    assert opt.orders == []
    assert dec.shortfall == pytest.approx(3_432.0), "the number is the same either way"
    _one_csp_event("csp_skip")


def test_the_collateral_gate_scales_with_the_contract_count():
    """Two contracts is two lots of collateral. An affordable single contract
    must not become an affordable pair by accident.

    The risk budget is lifted out of the way here — big equity, whole-account
    frac — because this test is about the CASH gate, and the budget is a
    separate ceiling with its own tests. An unaffordable pinned pair must be
    REFUSED, never quietly trimmed to an affordable single."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=9_000.0, equity=1_000_000.0)

    one = _run(opt, shadow=False, contracts=1, reserve_frac=1.0, max_contracts=2)
    assert one.action == "sell"

    opt2 = FakeOptions(chain=GOOD_CHAIN, bp=9_000.0, equity=1_000_000.0)
    two = _run(opt2, shadow=False, contracts=2, reserve_frac=1.0, max_contracts=2)
    assert two.action == "skip"
    assert two.collateral == 14_400.0
    assert opt2.orders == []


# ------------------------------------------------------- the affordable week ---
def test_shadow_mode_never_submits_an_order_even_when_it_could():
    """The affordable-and-shadowed case, which is what happens the moment the
    account is funded but `csp.shadow` has not been flipped. The full trade is
    described; nothing is sent."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0)

    dec = _run(opt, shadow=True)

    assert dec.action == "shadow"
    assert opt.orders == [], "shadow mode must not reach the broker"
    assert dec.shortfall is None, "affordable — there is nothing missing"
    assert dec.symbol == _occ(72)
    _one_csp_event("csp_shadow")


def test_a_live_sale_is_exactly_one_limit_at_the_bid():
    """The only path in this module that sends anything.

    Sell at the BID, not the mid: a limit that does not fill is a week with no
    trade, which is recoverable; chasing a book that quotes up to 52% of mid is
    not. Exactly one order, for exactly the configured contract count."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0)

    dec = _run(opt, shadow=False, contracts=1)

    assert dec.action == "sell" and dec.reason.startswith("sold 1 to open")
    assert opt.orders == [(_occ(72), 0.40, 1)]
    assert opt.orders[0][1] == pytest.approx(GOOD_CHAIN[-1].bid), "the bid, not the mid"


def test_the_live_sale_journals_the_economics_it_committed_to():
    opt = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0)
    _run(opt, shadow=False, contracts=1)

    row = _one_csp_event("csp_sell")
    assert row["ticker"] == "SOXL"
    assert row["symbol"] == _occ(72)
    assert row["strike"] == 72.0
    assert row["expiry"] == "2026-09-04"
    assert row["limit"] == pytest.approx(0.40)
    assert row["credit"] == pytest.approx(40.0)
    assert row["collateral"] == 7_200.0
    assert row["otm"] == pytest.approx(0.28)


def test_a_multi_contract_sale_sends_one_order_for_the_whole_size():
    """Sizing lives in the order, not in a loop — N separate contracts would be
    N crossings of the spread the liquidity gate exists to protect."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0)

    dec = _run(opt, shadow=False, contracts=3, reserve_frac=1.0, max_contracts=3)

    assert opt.orders == [(_occ(72), 0.40, 3)]
    assert dec.credit == pytest.approx(120.0)
    assert dec.collateral == 21_600.0


def test_defaults_are_off_so_an_absent_config_block_cannot_trade():
    """decisions.json is rewritten by the research layer every day. If the csp
    block were ever dropped, the fallback must be silence, not a live sale."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0)

    dec = csp.run(opt, [], decisions={}, today=date(2026, 8, 27), log=lambda *a: None)

    assert dec.action == "skip" and opt.orders == []
    assert csp.DEFAULTS["enabled"] is False and csp.DEFAULTS["shadow"] is True


def test_config_layers_the_decisions_block_over_the_defaults():
    cfg = csp.config({"csp": {"min_otm": 0.24, "enabled": True}})

    assert cfg["min_otm"] == 0.24 and cfg["enabled"] is True
    assert cfg["max_otm"] == csp.DEFAULTS["max_otm"], "untouched keys keep the default"


def test_the_policy_file_reaches_run_not_just_config():
    """THE BUG THAT MADE A LIVE, ENABLED STRATEGY REPORT ITSELF DISABLED.

    config() reads csp.json only when it is handed None, and relay/csp_run.py
    handed it the contents of decisions.json — which has no `csp` block by
    design, because the research harness rewrites that file wholesale. So the
    None branch never ran, every value came from DEFAULTS, and `enabled` was
    False no matter what the operator had written. The job printed "disabled"
    and exited 0, which is indistinguishable from working.

    load_policy() is the single way in, and this pins that run() honours it.
    """
    CSP_CONFIG_FILE_STATE = {"enabled": True, "shadow": True, "underlying": "SOXL"}
    csp.CSP_CONFIG_FILE.write_text(json.dumps(CSP_CONFIG_FILE_STATE))

    assert csp.config(csp.load_policy())["enabled"] is True
    assert csp.config(None)["enabled"] is True, "the None branch reads the same file"

    # and a run driven off it gets past the enabled gate rather than skipping
    dec = csp.run(FakeOptions(), [], csp.load_policy(), today=date(2026, 8, 28),
                  log=lambda *_: None)
    assert dec.reason != "csp.enabled is false"


def test_an_unreadable_policy_file_disarms_rather_than_running_the_defaults():
    """Half-written JSON is the shape a file has mid-save. The defaults have
    enabled False anyway, but saying so out loud beats inheriting it by luck."""
    csp.CSP_CONFIG_FILE.write_text("{ not json")

    assert csp.load_policy() == {}
    assert csp.config(None)["enabled"] is False


def test_an_inverted_band_falls_back_rather_than_selecting_nothing():
    """min_otm above max_otm selects an empty band, and an empty band is a
    refusal every week — a typo that silently retires the strategy."""
    cfg = csp.config({"csp": {"min_otm": 0.30, "enabled": True}})

    assert cfg["min_otm"] == csp.DEFAULTS["min_otm"]
    assert cfg["max_otm"] == csp.DEFAULTS["max_otm"]


def test_a_missing_decisions_file_falls_back_to_the_safe_defaults():
    """CSP_CONFIG_FILE points at tmp_path here and was never written, which is
    the same shape as a corrupt or half-written file mid-rewrite."""
    cfg = csp.config(None)

    assert cfg["enabled"] is False


# ---------------------------------------------------------------------------
# pledged_collateral — the one number the equity book also reads
# ---------------------------------------------------------------------------
def test_a_short_put_pledges_its_strike_times_one_hundred():
    """sigengine.engine.fire_event subtracts this before sizing an equity entry:
    Alpaca does NOT reduce `equity` when cash is pledged to a put, so without it
    the engine keeps trying to buy 0.97 x equity of stock and lets a broker
    rejection decide which strategy wins."""
    opt = FakeOptions(options=[_option_pos(_occ(90), qty="-1")])

    assert csp.pledged_collateral(opt) == 9_000.0


def test_several_contracts_pledge_several_lots():
    opt = FakeOptions(options=[_option_pos(_occ(90), qty="-3")])

    assert csp.pledged_collateral(opt) == 27_000.0


def test_a_long_put_pledges_nothing():
    """A long option is paid for in full at purchase; it reserves no further
    cash. Counting it would starve the equity book of capital that is free."""
    opt = FakeOptions(options=[_option_pos(_occ(90), qty="2")])

    assert csp.pledged_collateral(opt) == 0.0


def test_a_short_call_is_not_a_cash_secured_put():
    """This strategy never sells calls, but the reader is a generic position
    scan. A short call's collateral is the SHARES, not strike x 100 in cash, so
    counting it would double-report."""
    opt = FakeOptions(options=[_option_pos(_occ(90, right="C"), qty="-1")])

    assert csp.pledged_collateral(opt) == 0.0


def test_a_mixed_option_book_sums_only_the_short_puts():
    opt = FakeOptions(options=[
        _option_pos(_occ(90), qty="-1"),              # 9,000
        _option_pos(_occ(72.5), qty="-2"),            # 14,500
        _option_pos(_occ(80), qty="4"),               # long — nothing
        _option_pos(_occ(85, right="C"), qty="-1"),   # short call — nothing
    ])

    assert csp.pledged_collateral(opt) == 9_000.0 + 14_500.0


def test_no_options_pledges_nothing():
    assert csp.pledged_collateral(FakeOptions()) == 0.0


def test_a_broker_error_reports_zero_rather_than_starving_the_equity_book():
    """Best-effort on purpose, and the direction is chosen. Over-reporting a
    pledge would shrink every equity entry for as long as the outage lasted;
    under-reporting hands the decision to the broker, which will reject an
    order it cannot fund. A wrong answer here must be the recoverable one."""
    opt = FakeOptions()
    opt.option_positions_error = RuntimeError("alpaca 503")

    assert csp.pledged_collateral(opt) == 0.0


# ---------------------------------------------------------------------------
# detect_assignment / assignment_state
# ---------------------------------------------------------------------------
def test_a_flat_account_has_not_been_assigned():
    assert csp.detect_assignment(FakeOptions(), [], _decisions()) is None
    assert csp.detect_assignment(FakeOptions(), None, _decisions()) is None


def test_shares_of_the_underlying_are_the_assignment_signature():
    """Detection is by POLLING POSITIONS, not by reading activities: on paper
    Alpaca syncs OPASN/OPEXP only the NEXT day, while positions update
    immediately — a day late on a weekly strategy is most of the holding period."""
    _sold_a_put()
    pos = _equity_pos("SOXL", qty="100")

    assert csp.detect_assignment(FakeOptions(), [pos], _decisions()) is pos


def test_shares_with_no_put_behind_them_are_NOT_an_assignment():
    """THE SAFETY PROPERTY. SOXL is an enabled engine symbol, so a long SOXL
    position is far more likely to be the share strategy doing its job than an
    assignment. Calling it an assignment would relabel the operator's own trade
    and, through the recap, tell them they were put shares they chose to buy."""
    pos = _equity_pos("SOXL", qty="100")        # no csp_sell in the journal

    assert csp.detect_assignment(FakeOptions(), [pos], _decisions()) is None


def test_a_share_count_that_is_not_a_round_lot_is_NOT_an_assignment():
    """An assignment delivers exactly 100 shares per contract. The equity engine
    sizes off 0.97 x equity and lands on whatever whole number that gives."""
    _sold_a_put()

    assert csp.detect_assignment(FakeOptions(), [_equity_pos("SOXL", qty="47")],
                                 _decisions()) is None


def test_shares_of_something_else_are_the_equity_book_not_an_assignment():
    """A SOXL put can only ever deliver SOXL. TQQQ in the account is the share
    strategy doing its job."""
    assert csp.detect_assignment(FakeOptions(), [_equity_pos("TQQQ")], _decisions()) is None


def test_the_underlying_match_is_case_insensitive():
    _sold_a_put()
    assert csp.detect_assignment(
        FakeOptions(), [_equity_pos("soxl")], _decisions()) is not None


def test_an_open_short_put_is_never_read_as_an_assignment():
    """THE misread this whole filter exists to prevent. An OCC row's `symbol`
    starts with SOXL, so a naive prefix or containment test sees the strategy's
    own live contract as 100 delivered shares — and the recap would announce an
    assignment every week the put was working."""
    assert csp.detect_assignment(
        FakeOptions(), [_option_pos(_occ(90))], _decisions()) is None


def test_an_option_row_without_an_asset_class_is_still_not_an_assignment():
    """The shape backstop, for a test double or an older SDK that omits
    asset_class: the OCC symbol itself is recognisable."""
    row = type("Row", (), {"symbol": _occ(90), "qty": "-1"})()

    assert csp.detect_assignment(FakeOptions(), [row], _decisions()) is None


def test_a_zero_or_short_equity_position_is_not_an_assignment():
    """A put delivers LONG shares. A flat or short row is something else and
    must not be reported as delivered stock."""
    assert csp.detect_assignment(
        FakeOptions(), [_equity_pos("SOXL", qty="0")], _decisions()) is None
    assert csp.detect_assignment(
        FakeOptions(), [_equity_pos("SOXL", qty="-100")], _decisions()) is None


def test_an_unparseable_qty_is_skipped_not_raised():
    """A display path calls into this; it must not be able to break a render."""
    assert csp.detect_assignment(
        FakeOptions(), [_equity_pos("SOXL", qty="n/a")], _decisions()) is None


def test_assignment_state_on_a_flat_account_reports_the_config_and_nothing_else():
    st = csp.assignment_state(FakeOptions(), [], {}, _decisions())

    assert st == {"enabled": True, "shadow": True, "underlying": "SOXL",
                  "short_put": None, "assigned": None}


def test_assignment_state_describes_an_open_short_put():
    """Both the strike and the expiry are re-derived from the OCC symbol,
    because the broker position carries neither as a field."""
    opt = FakeOptions(options=[_option_pos(_occ(90), qty="-1")])

    st = csp.assignment_state(opt, [], {}, _decisions())

    assert st["short_put"]["symbol"] == _occ(90)
    assert st["short_put"]["contracts"] == 1.0
    assert st["short_put"]["strike"] == 90.0
    assert st["short_put"]["expiry"] == "2026-09-04"
    assert st["short_put"]["unrealized"] == 4.0


def test_assignment_state_ignores_a_long_option():
    opt = FakeOptions(options=[_option_pos(_occ(90), qty="2")])

    assert csp.assignment_state(opt, [], {}, _decisions())["short_put"] is None


def test_assigned_shares_the_engine_never_bought_are_flagged_unmanaged():
    """THE FLAG THAT MATTERS. managed_by_engine False means no strategy will
    ever emit an exit for these shares: the share strategies only close what
    they simulated opening, and the reconciler only closes what is in the entry
    sidecar. Assigned stock is in neither, so it sits forever unless a human
    decides. Reporting it as managed would hide that."""
    _sold_a_put()
    st = csp.assignment_state(FakeOptions(), [_equity_pos("SOXL", qty="100")],
                              {"opened": {"TQQQ": {}}}, _decisions())

    assert st["assigned"]["symbol"] == "SOXL"
    assert st["assigned"]["qty"] == 100.0
    assert st["assigned"]["avg_price"] == 90.0
    assert st["assigned"]["managed_by_engine"] is False


def test_shares_the_engine_did_open_are_flagged_managed():
    """The same 100 SOXL shares, with the engine's sidecar saying it opened
    them, are an ordinary equity position with a working exit — not orphaned
    stock. The distinction is the sidecar and nothing else."""
    _sold_a_put()
    st = csp.assignment_state(FakeOptions(), [_equity_pos("SOXL", qty="100")],
                              {"opened": {"SOXL": {"ts": 1}}}, _decisions())

    assert st["assigned"]["managed_by_engine"] is True


def test_adopted_shares_report_as_managed_and_carry_their_sell_date():
    """ADOPTION IS THE THIRD STATE, and the one the strategy actually produces.

    Assigned shares are in `adopted`, never in `opened` — putting them in
    `opened` would make the reconciler market-sell them on the next poll, since
    the strategy's simulation is flat right after an assignment. So the display
    has to read both maps, or every real assignment reports as orphaned stock
    the moment it is correctly handled.

    What the recap says about them is a DATE, not a level: there is no stop on
    assigned shares by design (operator 2026-08-28), the sleeve sells them at
    its next weekly run, and that run is the first Friday after adoption.
    """
    _sold_a_put()
    st = csp.assignment_state(FakeOptions(), [_equity_pos("SOXL", qty="100")],
                              {"opened": {},
                               "adopted": {"SOXL": {"basis": 90.0, "anchor": 84.0,
                                                    "since": "2026-08-22T20:05:00+00:00"}}},
                              _decisions())

    assert st["assigned"]["managed_by_engine"] is True
    assert st["assigned"]["adopted"] is True
    assert st["assigned"]["basis"] == 90.0, "the strike"
    assert st["assigned"]["anchor"] == 84.0, "the arrival price"
    assert st["assigned"]["sell_by"] == "2026-08-28", "the first Friday after a Saturday adoption"
    assert "stop_at" not in st["assigned"], "there is no stop to quote"


def test_an_adoption_with_no_date_does_not_invent_one():
    """A record without a usable `since` still means the shares are adopted, but
    there is no date to quote — and quoting one would put a fabricated fact in
    a video."""
    _sold_a_put()
    st = csp.assignment_state(FakeOptions(), [_equity_pos("SOXL", qty="100")],
                              {"adopted": {"SOXL": {"basis": None}}}, _decisions())

    assert st["assigned"]["adopted"] is True
    assert st["assigned"]["sell_by"] is None


def test_a_missing_sidecar_reads_as_unmanaged():
    """An absent or empty aux must fail toward 'nobody is watching these', which
    is the answer that prompts a human to look."""
    _sold_a_put()
    for aux in (None, {}, {"opened": None}, {"opened": {}, "adopted": {}}):
        st = csp.assignment_state(FakeOptions(), [_equity_pos("SOXL", qty="100")],
                                  aux, _decisions())
        assert st["assigned"]["managed_by_engine"] is False


def test_assignment_state_survives_a_broken_option_read():
    """A display path must never raise. With the option book unreachable the
    equity half is still reported."""
    _sold_a_put()
    opt = FakeOptions()
    opt.option_positions_error = RuntimeError("alpaca 503")

    st = csp.assignment_state(opt, [_equity_pos("SOXL", qty="100")], {}, _decisions())

    assert st["short_put"] is None
    assert st["assigned"]["symbol"] == "SOXL"


def test_assignment_state_reports_both_halves_at_once():
    """The week a put is rolled or partially assigned, both can be true. Neither
    reader may be shadowed by the other."""
    _sold_a_put()
    opt = FakeOptions(options=[_option_pos(_occ(72), qty="-1")])

    st = csp.assignment_state(opt, [_equity_pos("SOXL", qty="100")],
                              {"opened": {}}, _decisions())

    assert st["short_put"]["strike"] == 72.0
    assert st["assigned"]["qty"] == 100.0


def test_assignment_state_reads_nothing_and_writes_nothing_to_the_journal():
    """It is a display path. A dashboard refresh every few seconds must not
    append to the audit trail."""
    opt = FakeOptions(options=[_option_pos(_occ(90), qty="-1")])
    csp.assignment_state(opt, [_equity_pos("SOXL")], {}, _decisions())

    assert _journal_rows() == []


# ---------------------------------------------------------------------------
# Journaling — every outcome, exactly once
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs,positions,expected", [
    (dict(enabled=False), (), "csp_skip"),
    (dict(shadow=False), (_equity_pos("TQQQ"),), "csp_sell"),   # a held share book no longer blocks
    (dict(shadow=True), (), "csp_shadow"),
    (dict(shadow=False), (), "csp_sell"),
])
def test_every_outcome_writes_exactly_one_csp_event(kwargs, positions, expected):
    """Shadow mode's entire purpose is that the refusals are as legible as the
    sales, so 'journaled' cannot be conditional on trading. One row per decision:
    a duplicate double-counts the week in the recap, and a miss makes an unfunded
    strategy unwatchable."""
    opt = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0)

    dec = _run(opt, positions=positions, **kwargs)

    row = _one_csp_event(expected)
    assert row["ticker"] == "SOXL"
    assert row["reason"] == dec.reason
    assert "ts" in row


def test_a_refusal_never_journals_a_symbol_it_did_not_choose():
    """asdict() drops None fields, so a skip before a strike was picked must
    carry no symbol at all — a half-populated row would read as a real trade in
    any downstream scan of csp_* events."""
    _run(FakeOptions(expirations=[]), shadow=False)

    row = _one_csp_event("csp_skip")
    assert "symbol" not in row and "strike" not in row and "credit" not in row


def test_two_fridays_append_rather_than_overwrite():
    """The journal is append-only — it is the audit trail, and a week that
    replaced the previous week's row would erase the history the strategy is
    being judged on."""
    for _ in range(3):
        _run(FakeOptions(chain=GOOD_CHAIN, bp=100_000.0), shadow=True)

    rows = [r for r in _journal_rows() if r["event"] == "csp_shadow"]
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# size_position — the contract count IS the capital split
# ---------------------------------------------------------------------------
def _sz(collateral=9_000.0, equity=100_000.0, bp=100_000.0, shares_value=0.0, **over):
    return csp.size_position(collateral, csp.config(_decisions(**over)), equity, bp, shares_value)


# one contract = $9,000 collateral x 1.15 close reserve = $10,350 of the reserve
def test_the_count_scales_with_the_account_in_both_directions():
    """THE POINT OF AUTO SIZING. A hard-coded 1 was right for one account
    balance and wrong for every other one; the reserve never needs a manual
    edit. (max_contracts is lifted so the reserve, not the rail, is what scales.)"""
    assert _sz(equity=50_000.0, bp=50_000.0, max_contracts=10)[0] == 0, "a $10k reserve is short of one"
    assert _sz(equity=100_000.0, bp=100_000.0, max_contracts=10)[0] == 1
    assert _sz(equity=250_000.0, bp=250_000.0, max_contracts=10)[0] == 4
    assert _sz(equity=1_000_000.0, bp=1_000_000.0, max_contracts=10)[0] == 10, "the rail"


def test_the_reserve_binds_before_cash_does():
    """CASH IS THE LOOSER CEILING AND MUST NOT BE THE ONLY ONE.

    A $100,000 account can PAY for nine contracts (100,000 / 10,350). Nine would
    pledge $81,000, leave the share book $19,000, and convert 81% of the account
    into a 3x semiconductor ETF on one bad week. The reserve is what stops that.
    """
    n, why = _sz(equity=100_000.0, bp=100_000.0, max_contracts=10)

    assert n == 1 and why == "reserve"
    assert _sz(equity=100_000.0, bp=100_000.0, max_contracts=10, reserve_frac=1.0)[0] == 9, (
        "with the whole account as reserve, cash alone permits nine")


def test_a_cash_poor_but_equity_rich_book_is_sized_by_cash():
    """The two pools swap places depending on where the money IS. A book fully
    deployed in shares has equity but little cash, and cash is then the honest
    constraint — you cannot pledge what is already in stock."""
    n, why = _sz(equity=100_000.0, bp=9_000.0)

    assert n == 0 and why == "cash"


def test_an_account_too_small_for_its_reserve_refuses_rather_than_stretching():
    """At $30,000 the reserve is $6,000 — short of one $10,350 contract. The
    account could pay; the reserve says not to. That is a refusal with a
    different reason from a shortfall, and the journal must tell them apart."""
    n, why = _sz(equity=30_000.0, bp=30_000.0)

    assert n == 0 and why == "reserve"


def test_the_old_five_thousand_dollar_account_reads_as_a_reserve_refusal_now():
    """On the account this sleeve was built for the reason was always "cannot
    pay". Under a 20% reserve it is the RESERVE ($970) that is short first; give
    the sleeve the whole account and the honest answer is cash again."""
    assert _sz(equity=4_848.0, bp=4_848.0) == (0, "reserve")
    assert _sz(equity=4_848.0, bp=4_848.0, reserve_frac=1.0) == (0, "cash")


def test_assigned_shares_still_held_come_out_of_the_reserve():
    """Continuous weekly selling through an assignment needs the reserve to hold
    the shares AND the next contract. $20,000 does: $8,400 of shares leaves
    $11,600, which is one more contract. $16,000 does not."""
    assert _sz(equity=100_000.0, bp=100_000.0, shares_value=8_400.0)[0] == 1
    assert _sz(equity=80_000.0, bp=80_000.0, shares_value=8_400.0) == (0, "reserve")


def test_the_absurd_order_rail_caps_a_very_large_account():
    """The same idea as risk_filter.HARD_MAX_POSITION — and today the rail IS
    the operator's choice of one contract."""
    assert _sz(equity=10_000_000.0, bp=10_000_000.0) == (1, "rail")
    assert _sz(equity=10_000_000.0, bp=10_000_000.0, max_contracts=3) == (3, "rail")


def test_a_pinned_count_is_still_capped_by_the_reserve():
    """The reserve is the ONE knob that decides the capital split. It must not be
    bypassable by typing a bigger integer into the same file — that is the shape
    of an accident, not a decision."""
    f = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0, equity=100_000.0)

    dec = _run(f, shadow=False, contracts=8)

    # a 20% reserve on $100k is $20,000; at this chain's $72 strike a contract
    # costs $8,280 of it, so the reserve allows TWO — not the eight typed in
    assert dec.contracts == 2
    assert "capped by the reserve" in dec.sized_by
    assert f.orders == [(_occ(72), 0.40, 2)]


def test_a_zero_count_still_names_the_trade_it_did_not_make():
    """Shadow mode exists to describe the trade that did not happen. A refusal
    reported as "$0 of collateral on 0 contracts" names nothing and reads as a
    missing decision rather than a considered one — so the economics shown are
    one contract's worth, while `contracts` still tells the truth."""
    f = FakeOptions(chain=GOOD_CHAIN, bp=4_848.0, equity=4_848.0)

    dec = _run(f, shadow=True, reserve_frac=1.0)

    assert dec.contracts == 0
    assert dec.collateral == 7_200.0, "one contract's worth, not zero"
    assert dec.credit == pytest.approx(40.0)
    assert dec.shortfall and dec.shortfall > 0


def test_the_sizing_reason_reaches_the_journal():
    """The recap reads the last csp_* row. Which ceiling bound the count is the
    difference between "the sleeve is at its limit" and "the account is short
    of cash", and the presenter should not have to guess."""
    f = FakeOptions(chain=GOOD_CHAIN, bp=100_000.0, equity=100_000.0)
    _run(f, shadow=True)

    row = _one_csp_event("csp_shadow")
    assert row["contracts"] == 1
    assert row["sized_by"] == "rail", "one contract by the operator's cap"
    assert row["equity"] == 100_000.0


def test_a_broken_reserve_falls_back_rather_than_selling_the_account():
    """reserve_frac is the one number standing between a cash-sized sleeve and
    81% of the account in one leveraged ETF. A negative, a zero, a null or a
    value above 1 is a typo, and a typo must not be read as permission."""
    for bad in (-0.2, 0.0, 1.5, None, "lots"):
        cfg = csp.config(_decisions(reserve_frac=bad))
        assert cfg["reserve_frac"] == csp.DEFAULTS["reserve_frac"]



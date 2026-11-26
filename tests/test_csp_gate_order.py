"""The weekly put's GATES: their order, and what each one does when it is blind.

A refusal path is only worth what it costs to skip it. This file is a companion
to tests/test_csp_manager.py — that file proves each gate refuses the right
thing; this one attacks the SEQUENCE and the FAILURE MODES:

  * does a cheap, certain refusal really run before an expensive broker read?
  * when a read fails or returns nothing, does the Friday fail CLOSED (no sale)
    or OPEN (sell anyway on an assumption)?
  * does the expiry search actually land on next Friday when the job runs ON a
    Friday, and what does it do in a week that has no Friday expiry?
  * can a hand-edited or half-written `csp` block in decisions.json reach the
    gates as a string, a zero, or something that is not a mapping at all?

Several tests here are marked xfail: they state what the strategy should do and
record that today it does not. An xfail that starts PASSING is the signal that
the defect was fixed and the marker can go.

Everything runs against FAKES. No client is constructed, no order is submitted,
and both files the module can touch (JOURNAL_FILE, DECISIONS_FILE) are
redirected to tmp_path — a test that reached the live trades.jsonl would corrupt
the audit trail.
"""
from __future__ import annotations

import json
import os
from datetime import date

import pytest

# Dummy creds before importing the relay, mirroring tests/test_relay.py.
os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import relay.csp_manager as csp  # noqa: E402
from relay.options_client import PutQuote  # noqa: E402

FRIDAY = date(2026, 8, 28)          # the day the strategy is meant to run
THURSDAY = date(2026, 8, 27)        # the day it was written
NEXT_FRIDAY = "2026-09-04"          # the expiry it is meant to sell


def _occ(strike: float, yymmdd: str = "260904", right: str = "P") -> str:
    return f"SOXL{yymmdd}{right}{int(round(strike * 1000)):08d}"


def _quote(strike, bid, ask, underlying=100.0, expiry=NEXT_FRIDAY) -> PutQuote:
    return PutQuote(symbol=_occ(strike), strike=float(strike), expiry=expiry,
                    bid=float(bid), ask=float(ask), underlying=float(underlying))


def _equity_pos(symbol="SOXL", qty="100"):
    return type("Pos", (), {"symbol": symbol, "qty": qty, "side": "long",
                            "asset_class": "us_equity",
                            "avg_entry_price": "90.0"})()


def _option_pos(symbol=None, qty="-1"):
    return type("OptPos", (), {"symbol": symbol or _occ(90), "qty": qty,
                               "side": "short", "asset_class": "us_option"})()


class FakeOptions:
    """Stand-in for OptionsClient that records the ORDER of every broker read.

    `calls` is the whole point of this file: a gate that refuses cheaply must
    leave the expensive reads unmade.
    """

    def __init__(self, *, options=(), open_orders=(), expirations=(NEXT_FRIDAY,),
                 chain=(), bp=1_000_000.0, spot=100.0, equity=None):
        self._options = list(options)
        self._open_orders = list(open_orders)
        self._expirations = list(expirations)
        self._chain = list(chain)
        self._bp = bp
        self._equity = equity
        self.spot = spot                      # read by the patched csp._spot
        self.orders: list = []
        self.calls: list[str] = []
        self.expiration_windows: list[tuple[str, str]] = []
        self.option_positions_error: Exception | None = None
        self.open_orders_error: Exception | None = None

    def option_positions(self):
        self.calls.append("option_positions")
        if self.option_positions_error is not None:
            raise self.option_positions_error
        return list(self._options)

    def open_option_orders(self):
        self.calls.append("open_option_orders")
        if self.open_orders_error is not None:
            raise self.open_orders_error
        return list(self._open_orders)

    def expirations(self, underlying, gte, lte):
        self.calls.append("expirations")
        self.expiration_windows.append((gte, lte))
        return list(self._expirations)

    def put_chain(self, underlying, expiry, lo, hi, underlying_price):
        self.calls.append("put_chain")
        return list(self._chain)

    def account_snapshot(self):
        """Cash AND equity in ONE call — the sizing rule needs both, and the
        gate-order test below pins that it stays one round trip."""
        self.calls.append("account_snapshot")
        return {"options_buying_power": self._bp,
                "equity": self._bp if self._equity is None else self._equity}

    def options_buying_power(self):                       # pragma: no cover
        self.calls.append("options_buying_power")
        return self._bp

    def sell_put_to_open(self, occ_symbol, limit_price, qty=1):
        self.calls.append("sell_put_to_open")
        self.orders.append((occ_symbol, limit_price, qty))
        self._open_orders.append(type("Ord", (), {
            "symbol": occ_symbol, "asset_class": "us_option"})())
        return {"id": "fake-option-order"}

    def buy_put_to_close(self, occ_symbol, limit_price, qty=1):  # pragma: no cover
        raise AssertionError("no test may close a contract")


# 72 is exactly 28% OTM against a spot of 100 — the far edge of the band, tight,
# and comfortably over the premium floor. It is the strike every "it traded"
# test below expects to see chosen.
GOOD_CHAIN = [_quote(78, 0.60, 0.65), _quote(75, 0.50, 0.54), _quote(72, 0.40, 0.44)]


def _decisions(**over) -> dict:
    """decisions.json's csp block: the defaults, switched ON, plus overrides."""
    return {"csp": {**csp.DEFAULTS, "enabled": True, **over}}


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(csp, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(csp, "DECISIONS_FILE", tmp_path / "decisions.json")
    monkeypatch.setattr(csp, "CSP_CONFIG_FILE", tmp_path / "csp.json")
    monkeypatch.setattr(csp, "_spot", lambda opt, und: getattr(opt, "spot", None))
    yield


def _run(opt, positions=(), *, today=FRIDAY, **over):
    return csp.run(opt, positions, _decisions(**over), today=today, log=lambda *_: None)


def _csp_rows() -> list[dict]:
    p = csp.JOURNAL_FILE
    if not p.exists():
        return []
    rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    return [r for r in rows if str(r.get("event", "")).startswith("csp_")]


# ---------------------------------------------------------------------------
# Gate ORDER — a certain refusal must not cost four broker round trips
# ---------------------------------------------------------------------------
def test_a_disabled_strategy_asks_the_broker_nothing():
    """The enabled flag is checked before anything is fetched.

    This matters more than it looks: the cron wrapper fires this every Friday
    whether or not the sleeve is switched on, and an off strategy that still
    pulled a chain would be paying rate limit for a decision already made.
    """
    f = FakeOptions(chain=GOOD_CHAIN)
    dec = csp.run(f, [], {"csp": {"enabled": False}}, today=FRIDAY, log=lambda *_: None)
    assert dec.action == "skip"
    assert f.calls == []


def test_a_held_share_book_no_longer_stops_the_sleeve():
    """The old gate 1 — refuse while the share book holds anything — is gone.
    The sleeve sells from its own reserve; the share book's NVDA is not its
    concern, and the chain IS priced."""
    f = FakeOptions(chain=GOOD_CHAIN)
    dec = _run(f, [_equity_pos("NVDA")])
    assert dec.action == "shadow", dec.reason
    assert "put_chain" in f.calls


def test_an_open_contract_refuses_before_the_chain_is_priced():
    """Gate 3 costs one read and stops there — it never prices a second put."""
    f = FakeOptions(options=[_option_pos()], chain=GOOD_CHAIN)
    dec = _run(f)
    assert dec.action == "skip"
    assert f.calls == ["option_positions"]


def test_the_buying_power_read_is_the_last_broker_call():
    """PINS THE DELIBERATE ORDERING, INCLUDING WHAT IT COSTS.

    Affordability is the one gate that is certain to fail on today's account
    ($4,848 of options buying power against ~$9,700 of collateral), and it is
    checked LAST — after the option book, the expiry list, the spot price and
    the whole chain. That is on purpose: shadow mode's entire value is naming
    the trade it could not afford, and it cannot name a trade it never priced.
    Anyone tempted to "optimise" by moving the buying-power check earlier is
    deleting shadow mode; this test is here to say so out loud.
    """
    f = FakeOptions(chain=GOOD_CHAIN, bp=4_848.0)
    dec = _run(f, [], shadow=True, reserve_frac=1.0)
    assert dec.action == "shadow"
    assert f.calls == ["option_positions", "open_option_orders", "expirations",
                       "put_chain", "account_snapshot"]
    assert dec.strike == 72 and dec.shortfall > 0


# ---------------------------------------------------------------------------
# Blind gates — what happens when a read fails or says nothing
# ---------------------------------------------------------------------------
def test_a_broker_error_on_the_option_book_stops_the_week_without_selling():
    """FAILING CLOSED IS THE RIGHT CHOICE and this pins it.

    If the "is a put already open?" read raises, the manager must not fall
    through to a sale — an unreadable option book is exactly the state in which
    a second contract could be sold on top of a first.
    """
    f = FakeOptions(chain=GOOD_CHAIN)
    f.option_positions_error = RuntimeError("broker 500")
    dec = _run(f, [], shadow=False)
    assert dec.action == "skip"
    assert f.orders == []
    assert "put_chain" not in f.calls, "a blind gate stops, it does not price"


def test_a_working_order_that_cannot_be_read_also_stops_the_week():
    """The order half of gate 3 fails closed for the same reason as the
    position half: a resting sell-to-open IS a commitment, and not being able
    to see one is not permission to add another."""
    f = FakeOptions(chain=GOOD_CHAIN)
    f.open_orders_error = RuntimeError("broker 500")
    dec = _run(f, [], shadow=False)
    assert dec.action == "skip" and f.orders == []


def test_a_stopped_week_still_leaves_its_trace_in_the_journal():
    """A BROKER OUTAGE IS A DECISION AND IT GETS WRITTEN DOWN.

    Every outcome the manager reaches is journaled — that is what makes shadow
    mode watchable. If the outage alone wrote nothing, the journal could not
    tell "the cron never ran" from "it ran and the broker was down", and the
    reader of that journal (briefing/desk_probe.py _csp_state) takes the LAST
    csp_* row with no date filter — so the week after a crash the desk sheet
    would present the previous week's decision as this week's, on air.
    """
    f = FakeOptions(chain=GOOD_CHAIN)
    f.option_positions_error = RuntimeError("broker 500")
    _run(f, [], shadow=False)
    rows = _csp_rows()
    assert len(rows) == 1 and rows[0]["event"] == "csp_skip"
    assert "broker 500" in rows[0]["reason"]


def test_a_genuinely_flat_book_is_an_empty_list_and_may_sell():
    """The control for the test below: [] means the broker said "nothing held"."""
    f = FakeOptions(chain=GOOD_CHAIN)
    dec = _run(f, [], shadow=False, contracts=1)
    assert dec.action == "sell"
    assert f.orders == [(_occ(72), 0.40, 1)]


def test_an_unknown_position_state_is_not_a_flat_book():
    """None IS NOT FLAT. It is "I could not find out".

    `equity_positions` comes straight from AlpacaClient.get_all_positions() in
    relay/csp_run.py, and the rest of this repo already treats a missing
    position list as unknown rather than empty: OrderManager._close_position
    carries a comment saying positions "may be None because handle()'s own fetch
    FAILED, not because the book is flat", and the 2026-08-19 audit made the
    order path fail closed on unknown account state for exactly this reason.

    Gate 1 decides whether the account's cash is already committed to shares.
    Collapsing both cases to "flat" would make an unreadable book a live sale of
    a $9,700 obligation on top of a share position nobody could see.
    """
    f = FakeOptions(chain=GOOD_CHAIN)
    dec = _run(f, None, shadow=False)
    assert dec.action == "skip"
    assert f.orders == []


def test_a_second_run_in_one_session_cannot_stack_a_second_contract():
    """THE GATE THAT HAS TO LOOK AT ORDERS, NOT ONLY POSITIONS.

    The put is sold as a DAY LIMIT at the bid on a book that quotes as wide as
    52% of mid, so it very often does not fill immediately. Until it fills there
    is no option POSITION — so a gate that only reads positions would let a
    second invocation in the same session sell another contract, doubling an
    obligation the account cannot cover once. agent/run_csp.sh advertises itself
    as "safe to run by hand at any time"; this is what makes that true.

    The equity path solved this the same way: OrderManager consults
    _open_order_symbols() and refuses when "an order for X is already working".
    """
    f = FakeOptions(chain=GOOD_CHAIN)
    assert _run(f, [], shadow=False).action == "sell"
    # nothing FILLED, but the sell-to-open is still resting on the book
    second = _run(f, [], shadow=False)
    assert second.action == "skip" and "already working" in second.reason
    assert len(f.orders) == 1, "one contract, however many times the job runs"


# ---------------------------------------------------------------------------
# Which expiry a Friday run actually lands on
# ---------------------------------------------------------------------------
def test_a_friday_run_asks_for_the_window_it_means():
    """dte 5..9 from Friday 2026-08-28 spans Wed 09-02 to Sun 09-06, inclusive."""
    f = FakeOptions(expirations=["2026-09-02", NEXT_FRIDAY])
    csp.next_expiry(f, csp.config(_decisions()), FRIDAY)
    assert f.expiration_windows == [("2026-09-02", "2026-09-06")]


def test_a_friday_run_takes_the_friday_not_the_wednesday():
    """The whole point of the default window: 7 DTE, not 5.

    SOXL lists Monday, Wednesday and Friday weeklies. The strategy is specified,
    backtested and priced as a seven-day contract at a strike distance
    calibrated for a full week, so the nearest expiry in the window is the wrong
    one even though it is in range.
    """
    f = FakeOptions(expirations=["2026-09-02", NEXT_FRIDAY])
    assert csp.next_expiry(f, csp.config(_decisions()), FRIDAY) == NEXT_FRIDAY


def test_a_thursday_run_lands_on_the_same_friday():
    """A cron that slips a day (or an operator running it early) must not move
    the trade. From Thursday the window is 09-01..09-05 and still contains the
    same Friday."""
    f = FakeOptions(expirations=["2026-09-02", NEXT_FRIDAY])
    assert csp.next_expiry(f, csp.config(_decisions()), THURSDAY) == NEXT_FRIDAY
    assert f.expiration_windows == [("2026-09-01", "2026-09-05")]


def test_the_nearer_of_two_fridays_wins():
    """A widened dte_max must not silently sell a fortnightly contract."""
    f = FakeOptions(expirations=[NEXT_FRIDAY, "2026-09-11"])
    assert csp.next_expiry(f, csp.config(_decisions(dte_max=16)), FRIDAY) == NEXT_FRIDAY


def test_a_holiday_week_takes_the_last_trading_day_not_the_first():
    """GOOD FRIDAY IS THE CASE THAT BREAKS THE FALLBACK.

    In a week whose Friday is a holiday the exchange lists no Friday expiry; the
    week's last expiry is the Thursday. Falling back to the EARLIEST expiry in
    the window would hand back the Wednesday — a five-day contract priced and
    sized as a seven-day one, precisely the failure the Friday preference exists
    to prevent. The fallback takes the LAST expiry in the window instead.
    """
    f = FakeOptions(expirations=["2026-04-01", "2026-04-02"])   # Wed, Thu
    assert csp.next_expiry(f, csp.config(_decisions()), date(2026, 3, 27)) == "2026-04-02"


def test_no_expiry_in_the_window_is_a_refusal_not_a_guess():
    f = FakeOptions(expirations=[], chain=GOOD_CHAIN)
    dec = _run(f)
    assert dec.action == "skip" and "expiry" in dec.reason
    assert "put_chain" not in f.calls


# ---------------------------------------------------------------------------
# The config block — decisions.json is hand-edited AND machine-rewritten
# ---------------------------------------------------------------------------
def test_a_partial_block_keeps_every_other_default():
    cfg = csp.config({"csp": {"enabled": True, "min_otm": 0.24}})
    assert cfg["min_otm"] == 0.24
    assert cfg["max_otm"] == csp.DEFAULTS["max_otm"]
    assert cfg["contracts"] == "auto" and cfg["shadow"] is True


def test_a_null_block_is_the_safe_defaults():
    assert csp.config({"csp": None})["enabled"] is False


def test_a_number_written_as_a_string_never_reaches_the_gates_as_a_string():
    """EVERY OTHER FIELD IN decisions.json IS COERCED; THIS BLOCK IS TOO.

    agent/decisions_schema.validate_decision wraps each numeric field in
    try/except and falls back to a safe value. A bare dict.update would carry
    `"min_otm": "0.22"` — one quote character in a hand-edited file — all the
    way into `spot * (1 - cfg["min_otm"] + 0.05)` and raise TypeError, killing
    the Friday mid-decision AFTER the chain was fetched.

    A numeric string that parses is used as the number it plainly is; one that
    does not parse falls back to the default. Neither raises.
    """
    assert csp.config({"csp": {"min_otm": "0.24"}})["min_otm"] == 0.24
    assert csp.config({"csp": {"min_otm": "wide"}})["min_otm"] == csp.DEFAULTS["min_otm"]
    dec = _run(FakeOptions(chain=GOOD_CHAIN), [], min_otm="0.22")
    assert dec.action == "shadow" and dec.strike == 72


def test_a_junk_block_cannot_break_the_display_path():
    """assignment_state promises "Never raises — a display path must not be able
    to break a render", but its very first line calls config(), which is outside
    every try. A `csp` value that is not a mapping (a string, a list) makes
    dict.update raise ValueError straight through the promise. The desk slide's
    caller swallows it, so the symptom is a silently missing slide — the same
    2026-08-13 failure the _qty() helper in briefing/desk.py was written for.
    """
    assert isinstance(csp.assignment_state(FakeOptions(), [], decisions={"csp": "on"}), dict)


def test_enabled_written_as_the_string_false_does_not_arm_the_strategy():
    """The switch is read as a bare truthiness test. A hand-edited
    `"enabled": "false"` — the single most likely way an operator turns this off
    in a JSON file by hand — turns it ON, and with shadow off that sends a live
    order."""
    f = FakeOptions(chain=GOOD_CHAIN)
    dec = _run(f, [], enabled="false", shadow=False)
    assert dec.action == "skip"
    assert f.orders == []


def test_a_zero_contract_config_never_sends_an_order():
    """`contracts: 0` would make the collateral requirement zero, so gate 4
    approves it against any balance and a zero-lot order reaches the broker;
    `-1` submits a BUY dressed as a sell. Both are refused before any read, and
    refused rather than rounded up to 1 — someone who wrote 0 did not ask to
    trade."""
    for n in (0, -1):
        f = FakeOptions(chain=GOOD_CHAIN)
        dec = _run(f, [], contracts=n, shadow=False)
        assert dec.action == "skip" and "contract count" in dec.reason
        assert f.orders == [] and f.calls == []


# ---------------------------------------------------------------------------
# pick_strike edges the gates depend on
# ---------------------------------------------------------------------------
def test_a_chain_with_no_bid_is_refused_on_liquidity_not_on_arithmetic():
    """A zero bid must not divide by zero and must not read as a free premium.

    put_chain drops one-sided quotes upstream, but pick_strike is a public
    function and the two must not disagree: a zero-bid quote scores a spread of
    100% of mid and is refused as untradeable before the premium floor is ever
    consulted.
    """
    q, why = csp.pick_strike([_quote(75, 0.0, 0.10)], csp.config(_decisions()))
    assert q is None and "too wide" in why
    q2, why2 = csp.pick_strike([_quote(75, 0.0, 0.0)], csp.config(_decisions()))
    assert q2 is None and "too wide" in why2


def test_a_tie_on_distance_is_broken_by_chain_order_not_by_credit():
    """Two contracts at the same strike are separated by nothing.

    max(..., key=otm_frac) returns the FIRST maximum, so when a chain carries
    two quotes at one strike — a standard contract and an adjusted one, which a
    leveraged ETF like SOXL produces every time it splits — the pick is
    whichever the broker happened to list first, not the better bid. The credit
    is deliberately not a tie-break, but an adjusted contract also breaks the
    strike x 100 collateral assumption in PutQuote.collateral.
    """
    first, second = _quote(75, 0.50, 0.54), _quote(75, 0.90, 0.94)
    q, _ = csp.pick_strike([first, second], csp.config(_decisions()))
    assert q.bid == 0.50
    q2, _ = csp.pick_strike([second, first], csp.config(_decisions()))
    assert q2.bid == 0.90


# ---------------------------------------------------------------------------
# The pledge the equity book sizes against
# ---------------------------------------------------------------------------
def _opt_client(*positions):
    return type("OC", (), {"option_positions": lambda self: list(positions)})()


def test_a_short_put_pledges_its_collateral():
    assert csp.pledged_collateral(_opt_client(_option_pos(_occ(90), "-1"))) == 9000.0


def test_one_unreadable_row_does_not_erase_the_whole_pledge():
    """THE FAILURE DIRECTION IS WRONG WHEN THERE IS ANYTHING TO LOSE.

    pledged_collateral is what stops the equity book and the put claiming the
    same dollars (sigengine.engine.fire_event subtracts it before sizing). Its
    except-return-0.0 is documented as erring safe, and it is — for a total
    read failure. But the try wraps the whole LOOP, so a single row it cannot
    parse throws away every pledge already summed and reports zero, which is the
    unsafe direction: the engine then sizes an entry off cash that is committed.
    Reporting the partial sum, or the largest pledge seen, keeps the intent.

    (The guard is also off by one: `len(sym) >= 8` precedes `sym[-9]`, so an
    eight-character symbol raises IndexError rather than being skipped.)
    """
    got = csp.pledged_collateral(_opt_client(_option_pos(_occ(90), "-1"),
                                             _option_pos("SOXL2609", "-1")))
    assert got == 9000.0


# ---------------------------------------------------------------------------
# The sleeve has to reach the people it was built for
# ---------------------------------------------------------------------------
def test_the_desk_payload_carries_the_sleeve(tmp_path, monkeypatch):
    """EVERY READER OF THE SLEEVE IS FED BY THIS ONE KEY.

    briefing/desk.py digests `payload["csp"]` and briefing/host_briefing.py
    _options_truth reads `dg["csp"]` — both return silence when it is absent,
    and briefing/desk_probe.py builds the payload without ever calling the
    _csp_state() written to fill it. The result is that the fact sheet's option
    lines and the presenter's permission to mention the sleeve are unreachable,
    while DECK_PROMPT_VERSION was bumped to v4-options-sleeve, busting the deck
    cache for a prompt block that renders empty.
    """
    import briefing.desk_probe as probe

    monkeypatch.setattr(probe, "headless_data", lambda: {"equity": 4848.0, "positions": []})
    out = tmp_path / "desk.json"
    probe.main(["--png", str(tmp_path / "desk.png"), "--json", str(out), "--no-shot"])
    assert "csp" in json.loads(out.read_text())

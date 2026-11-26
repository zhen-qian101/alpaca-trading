"""What happens to 100 SOXL shares a put was assigned into.

THE RULE, in the operator's words: "if the shares are assigned next week, keep
it and use the shares trading strategy we already built to sell it (either exit
signal appear or stop loss triggerred)".

Nothing in the engine could do that. The strategies simulate from a FLAT book
and only emit an exit while their own simulated position is long, so shares they
never bought produce no exit ever; the reconciler only closes what is in its
`opened` sidecar; and rotation would liquidate them at an unrelated symbol's
signal. Wrong in three directions at once (2026-08-27 study).

`aux["adopted"]` is the record that fixes it. This file pins the four things it
has to do and the two it must NOT:

  MUST   adopt an assignment the moment it appears in the position list
  MUST   leave the shares alone until a signal or the stop says otherwise
  MUST   close them when price <= strike * (1 - stop)
  MUST   let the share strategy's own exit close them
  MUST   refuse to rotate the equity book out of them
  MUST NOT adopt shares the engine bought itself

    .venv/bin/pytest -q tests/test_csp_assignment_handoff.py
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

import relay.csp_manager as csp  # noqa: E402
import sigengine.engine as eng  # noqa: E402

# The symbol's SHARE stop, for the strategies' own positions. Assigned shares
# have NO price stop at all (operator 2026-08-28): the sleeve sells them at its
# next weekly run, and engine._ADOPTED_MAX_DAYS is the calendar backstop.
SOXL_CFG = {"interval": "1h", "stop": 0.03, "deploy": 0.97}
ENABLED = {"SOXL": SOXL_CFG}


def _ev(action, reason="macd_sma_bearish", ts="2026-07-15 11:00:00+00:00", price=90.0):
    return {"ts": pd.Timestamp(ts), "action": action, "reason": reason,
            "price": price, "strategy": "macd_sma"}


def _pos(sym="SOXL", qty="100", basis="90.0", price=None, available=None):
    attrs = {"symbol": sym, "qty": str(qty), "asset_class": "us_equity",
             "avg_entry_price": str(basis), "side": "long"}
    if price is not None:
        attrs["current_price"] = str(price)
    if available is not None:
        attrs["qty_available"] = str(available)
    return type("Pos", (), attrs)()


class FakeClient:
    def __init__(self, positions=None, open_orders=None):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])

    def get_all_positions(self):
        return list(self.positions)

    def get_open_orders(self, symbol=None):
        return list(self.open_orders)

    def get_account(self):
        return type("Acct", (), {"equity": "100000"})()


class FakeOM:
    def __init__(self):
        self.handled: list = []

    def handle(self, payload):
        self.handled.append((payload.ticker, payload.action, payload.strategy))
        return {"status": "closed"}


def _fetched(**per_symbol):
    return {sym: (evs, pd.Timestamp("2026-07-15 12:00:00+00:00"), "ibkr")
            for sym, evs in per_symbol.items()}


def _rows(journal_file):
    if not journal_file.exists():
        return []
    return [json.loads(x) for x in journal_file.read_text().splitlines() if x.strip()]


@pytest.fixture(autouse=True)
def sandbox(monkeypatch, tmp_path):
    """Both modules write to the LIVE trades.jsonl unless redirected — the
    journal is the audit trail and a test may not touch it."""
    j = tmp_path / "trades.jsonl"
    monkeypatch.setattr(eng, "JOURNAL_FILE", j)
    monkeypatch.setattr(csp, "JOURNAL_FILE", j)
    monkeypatch.setattr(csp, "CSP_CONFIG_FILE", tmp_path / "csp.json")
    monkeypatch.setattr(csp, "DECISIONS_FILE", tmp_path / "decisions.json")
    (tmp_path / "csp.json").write_text(json.dumps(
        {**csp.DEFAULTS, "enabled": True, "shadow": True}))
    eng._PENDING_EXIT_SEEN.clear()
    return j


def _sold_a_put(journal, und="SOXL", ago_days=3.0):
    """The corroborating evidence detect_assignment requires. SOXL is an
    ENABLED trading symbol, so a bare long SOXL position is far more likely to
    be the engine's own trade than an assignment; only a csp_sell in the journal
    separates the two."""
    import time as _t
    with journal.open("a") as f:
        f.write(json.dumps({"event": "csp_sell", "ticker": und,
                            "ts": _t.time() - ago_days * 86400}) + "\n")


def _aux():
    return {eng._PENDING_KEY: {}, eng._OPENED_KEY: {}, eng._ADOPTED_KEY: {}}


def _since(days_ago: float, naive: bool = False) -> str:
    ts = pd.Timestamp.utcnow() - pd.Timedelta(days=days_ago)
    return (ts.tz_localize(None) if naive else ts).isoformat(timespec="seconds")


def _adopt(aux, days_ago=1.0, **extra):
    aux[eng._ADOPTED_KEY]["SOXL"] = {"basis": 90.0, "anchor": 84.0, "reason": "csp_assignment",
                                     "since": _since(days_ago), **extra}


# --------------------------------------------------------------- adoption
def test_an_assignment_records_both_the_strike_and_the_arrival_price(sandbox):
    """TWO DIFFERENT NUMBERS DOING TWO DIFFERENT JOBS.

    `basis` is the STRIKE — what the shares cost, and what the recap quotes.
    `anchor` is what they were worth when they landed, and it is what the brake
    measures from. They are usually far apart: conditional on a 22-28% OTM put
    finishing in the money, spot on arrival is a median 5.3% below the strike
    (829 weekly windows of SOXL, 44 assignments).
    """
    _sold_a_put(sandbox)
    aux = _aux()
    eng._adopt_assignments([_pos(basis="90.0", price=84.0)], aux)

    rec = aux[eng._ADOPTED_KEY]["SOXL"]
    assert rec["basis"] == 90.0, "the strike"
    assert rec["anchor"] == 84.0, "the price on arrival"
    assert rec["qty"] == 100.0 and rec["reason"] == "csp_assignment"
    assert [r for r in _rows(sandbox) if r["event"] == "csp_assignment_adopted"]


def test_adoption_never_sells_on_arrival(sandbox):
    """THE BUG THE FIRST DESIGN HAD, stated as the thing that must not happen.

    A price stop under the strike was already breached the day the shares
    landed in half of all assignments and would have market-sold into the
    crash. There is no price stop now. Freshly adopted shares, at ANY price,
    are left for the sleeve's weekly run.
    """
    _sold_a_put(sandbox)
    aux = _aux(); om = FakeOM()
    arrival = _pos(basis="90.0", price=40.0)          # -56% under the strike
    eng._adopt_assignments([arrival], aux)

    assert eng._check_adopted_stale("SOXL", arrival, aux, SOXL_CFG, om, dry_run=False) is False
    assert om.handled == []


def test_shares_the_engine_bought_itself_are_never_adopted(sandbox):
    """No csp_sell behind them, so they are the equity book's own position.
    Adopting one would freeze rotation and hand it a stop measured from the
    wrong number."""
    aux = _aux()
    eng._adopt_assignments([_pos(qty="100")], aux)
    assert aux[eng._ADOPTED_KEY] == {}


def test_a_position_already_in_the_opened_record_is_not_re_adopted(sandbox):
    """`opened` means the engine submitted the buy. Even with a put sold that
    week, its own entry is not an assignment."""
    _sold_a_put(sandbox)
    aux = _aux()
    aux[eng._OPENED_KEY]["SOXL"] = "2026-07-15 10:00:00+00:00"
    eng._adopt_assignments([_pos()], aux)
    assert aux[eng._ADOPTED_KEY] == {}


def test_an_assignment_with_no_readable_basis_is_refused_loudly(sandbox):
    """Adopting without a basis would create a position whose stop can never be
    evaluated — managed on paper, unprotected in fact. Refusing leaves it as a
    visible orphan the reconciler already alerts about every poll."""
    _sold_a_put(sandbox)
    aux = _aux()
    eng._adopt_assignments([_pos(basis="0")], aux)
    assert aux[eng._ADOPTED_KEY] == {}


def test_adoption_never_takes_the_engine_down(sandbox, monkeypatch):
    """The option sleeve is optional machinery bolted to a trading loop that is
    not. A broken import or an unreadable journal must cost a log line."""
    def boom(*a, **k):
        raise RuntimeError("options module is gone")
    monkeypatch.setattr(csp, "detect_assignment", boom)
    aux = _aux()
    eng._adopt_assignments([_pos()], aux)          # must not raise
    assert aux[eng._ADOPTED_KEY] == {}


# ------------------------------------------------- what the reconciler does
def test_adopted_shares_are_not_liquidated_as_a_missed_exit(sandbox):
    """THE FAILURE THIS WHOLE DESIGN AVOIDS.

    Right after assignment the strategy's simulation is flat — it never bought
    these shares. If adoption had been recorded in `opened` instead, the
    reconciler would read "real long + sim flat + engine opened it" and
    market-sell 100 shares on the next poll, which is the exact opposite of
    holding them for the strategy's exit.
    """
    _sold_a_put(sandbox)
    aux = _aux()
    om = FakeOM()
    ev = [_ev("buy", "macd_sma_bullish"), _ev("close", "macd_sma_bearish")]
    eng._reconcile_positions(ENABLED, _fetched(SOXL=ev),
                             FakeClient([_pos(price=95.0)]), om,
                             dry_run=False, aux=aux)
    assert om.handled == []
    assert "SOXL" in aux[eng._ADOPTED_KEY]
    reasons = [r.get("reason", "") for r in _rows(sandbox)]
    assert any("sells them at its next weekly run" in r for r in reasons)


def test_shares_the_sleeve_failed_to_sell_are_sold_after_ten_days(sandbox):
    """THE BACKSTOP, which is a calendar and not a price. The sleeve's weekly run
    is the exit; if it has not happened for ten days — a cron that never fired,
    a broker outage on the Friday — the engine sells rather than let a position
    meant to live seven days quietly live seventy."""
    _sold_a_put(sandbox)
    aux = _aux(); _adopt(aux, days_ago=12)
    om = FakeOM()
    eng._reconcile_positions(ENABLED, _fetched(SOXL=[_ev("buy")]),
                             FakeClient([_pos(price=95.0)]), om, dry_run=False, aux=aux)

    assert om.handled == [("SOXL", "close", "csp-assignment-stale")]
    rows = [r for r in _rows(sandbox) if r["event"] == "csp_assignment_stale_sold"]
    assert len(rows) == 1 and rows[0]["age_days"] >= 12


def test_inside_the_window_no_price_move_sells_the_shares(sandbox):
    """No stop means NO STOP. Six days in and down 99%, the shares are still the
    sleeve's to sell on Friday."""
    _sold_a_put(sandbox)
    aux = _aux(); _adopt(aux, days_ago=6)
    om = FakeOM()
    eng._reconcile_positions(ENABLED, _fetched(SOXL=[_ev("buy")]),
                             FakeClient([_pos(price=1.0)]), om, dry_run=False, aux=aux)
    assert om.handled == []


def test_a_record_with_no_date_is_never_sold_as_stale(sandbox):
    """"Unknown" is not "overdue". A record without a usable `since` (an older
    install, a hand edit) must not be sold by a calendar test that cannot read
    the calendar — NaN compared to ten days was silently True once."""
    aux = _aux()
    aux[eng._ADOPTED_KEY]["SOXL"] = {"basis": 90.0, "reason": "csp_assignment"}
    om = FakeOM()
    assert eng._check_adopted_stale("SOXL", _pos(price=1.0), aux, SOXL_CFG, om, dry_run=False) is False
    aux[eng._ADOPTED_KEY]["SOXL"]["since"] = "not a date"
    assert eng._check_adopted_stale("SOXL", _pos(price=1.0), aux, SOXL_CFG, om, dry_run=False) is False
    assert om.handled == []


def test_a_dry_run_never_closes_adopted_shares(sandbox):
    """--dry-run is an inspection. Even a month-old adoption is left alone."""
    aux = _aux(); _adopt(aux, days_ago=30)
    om = FakeOM()
    assert eng._check_adopted_stale("SOXL", _pos(), aux, SOXL_CFG, om, dry_run=True) is False
    assert om.handled == []


def test_a_naive_timestamp_is_read_as_utc(sandbox):
    """The adoption writes a tz-aware ISO stamp, but the backstop must cope with
    a naive one rather than never firing on it."""
    aux = _aux()
    aux[eng._ADOPTED_KEY]["SOXL"] = {"basis": 90.0, "reason": "csp_assignment",
                                     "since": _since(12, naive=True)}
    om = FakeOM()
    assert eng._check_adopted_stale("SOXL", _pos(), aux, SOXL_CFG, om, dry_run=False) is True
    assert om.handled == [("SOXL", "close", "csp-assignment-stale")]


def test_the_adoption_is_retired_once_the_shares_are_gone(sandbox):
    """And it must be, or gate 1 of csp_manager.run would refuse to sell
    another put forever."""
    aux = _aux()
    aux[eng._ADOPTED_KEY]["SOXL"] = {"basis": 90.0, "reason": "csp_assignment"}
    om = FakeOM()
    eng._reconcile_positions(ENABLED, _fetched(SOXL=[_ev("buy")]),
                             FakeClient([]), om, dry_run=False, aux=aux)
    assert aux[eng._ADOPTED_KEY] == {}
    assert [r for r in _rows(sandbox) if r["event"] == "csp_assignment_cleared"]


# --------------------------------------------- what the entry path does
def test_a_buy_in_another_symbol_trades_around_the_sleeves_shares(sandbox):
    """ROTATION IS THE THIRD WAY THESE SHARES USED TO DISAPPEAR.

    OrderManager.handle closes whatever else is held before opening a
    higher-priority name — applied to an assignment that sells 100 shares
    because NVDA signalled. The first fix BLOCKED the NVDA entry, which froze
    the whole share book for the life of every assignment. Now the sleeve's
    shares are marked PROTECTED and the relay looks straight through them: the
    NVDA buy goes out, the SOXL shares stay.
    """
    aux = _aux(); _adopt(aux)
    om = FakeOM()
    out = eng.fire_event("NVDA", _ev("buy", "macd_sma_bullish", price=180.0),
                         {"interval": "1h", "deploy": 0.97, "stop": 0.02},
                         FakeClient([_pos()]), om, settings=None,
                         dry_run=False, aux=aux)
    assert out["status"] not in ("blocked", "skip"), out
    assert om.handled == [("NVDA", "buy", "signal-engine")]
    assert om.protected_symbols == {"SOXL"}, "the relay was told whose shares to ignore"


def test_the_strategys_own_exit_still_closes_assigned_shares(sandbox):
    """THE OTHER HALF OF THE OPERATOR'S RULE, and it needs no new code.

    fire_event's close branch closes whatever the broker holds; it does not
    consult `opened` or `adopted`. So once the strategy has cycled back to long
    in its own simulation and then signalled an exit, that exit flattens the
    assigned shares exactly as it would flatten the engine's own position.
    """
    aux = _aux()
    aux[eng._ADOPTED_KEY]["SOXL"] = {"basis": 90.0, "reason": "csp_assignment"}
    om = FakeOM()
    out = eng.fire_event("SOXL", _ev("close", "macd_sma_bearish"), SOXL_CFG,
                         FakeClient([_pos()]), om, settings=None,
                         dry_run=False, aux=aux)
    assert om.handled == [("SOXL", "close", "signal-engine")]
    assert out["status"] != "blocked"


def test_a_buy_in_the_adopted_symbol_itself_is_only_the_ordinary_skip(sandbox):
    """Not a rotation — there is nothing to rotate out of. It falls through to
    the plain "already long" skip, which is what keeps the strategy's simulation
    moving toward the exit that will close these shares."""
    aux = _aux()
    aux[eng._ADOPTED_KEY]["SOXL"] = {"basis": 90.0, "reason": "csp_assignment"}
    om = FakeOM()
    out = eng.fire_event("SOXL", _ev("buy", "macd_sma_bullish"), SOXL_CFG,
                         FakeClient([_pos()]), om, settings=None,
                         dry_run=False, aux=aux)
    assert out["status"] == "skip" and "already long" in out["reason"]
    assert om.handled == []


def test_the_sidecar_grows_the_key_on_an_older_install(tmp_path, monkeypatch):
    """load_aux must fill in `adopted` for a file written before it existed, or
    every read of it is a KeyError in the middle of a poll."""
    path = tmp_path / "state_entries.json"
    path.write_text(json.dumps({"pending": {}, "opened": {"AAPL": "x"}}))
    monkeypatch.setattr(eng, "_aux_path", lambda *a, **k: path)
    aux = eng.load_aux()
    assert aux[eng._ADOPTED_KEY] == {} and aux[eng._OPENED_KEY] == {"AAPL": "x"}


def test_a_retired_symbol_still_gets_the_backstop(sandbox):
    """SOXL is NOT in the share strategy any more (retired 2026-08-28 so its exit
    signal can never sell the sleeve's shares). The reconciler walks enabled
    symbols, so the backstop has to run for adopted symbols outside that list —
    otherwise a failed weekly run would leave 100 real shares unmanaged forever."""
    aux = _aux(); _adopt(aux, days_ago=12)
    om = FakeOM()
    eng._reconcile_positions({"NVDA": {"interval": "1h", "stop": 0.02}},
                             _fetched(NVDA=[_ev("buy")]),
                             FakeClient([_pos(price=80.0)]), om, dry_run=False, aux=aux)
    assert om.handled == [("SOXL", "close", "csp-assignment-stale")]


def test_a_disabled_symbols_adoption_still_retires_when_flat(sandbox):
    """The other half: once the shares are gone the record must go, or the put
    sleeve never sells again and no equity entry is ever allowed."""
    aux = _aux()
    aux[eng._ADOPTED_KEY]["SOXL"] = {"basis": 90.0, "reason": "csp_assignment"}
    om = FakeOM()
    eng._reconcile_positions({"NVDA": {"interval": "1h", "stop": 0.02}},
                             _fetched(NVDA=[_ev("buy")]),
                             FakeClient([]), om, dry_run=False, aux=aux)
    assert aux[eng._ADOPTED_KEY] == {}
    assert om.handled == []


# ---------------------------------------------------- what the recap says
def test_the_shadow_line_survives_the_fallback_trim():
    """THE FALLBACK IS WHAT SHIPS WHEN THE WRITER IS DOWN, and shadow is the
    state this sleeve will be in for weeks. Appended, it was the fifth line of
    a four-line list and never made it to air; the operator asked for the host
    to talk about the option status, so it has to be inside the trim."""
    import briefing.desk as desk
    dg = {"equity": 4848.0, "positions": [], "csp": {
        "enabled": True, "underlying": "SOXL", "assigned": None, "short_put": None,
        "last": {"action": "shadow", "strike": 90.0}}}
    facts = ["Account equity $4,848.", "No open positions.", "Board ranks ORCL top.",
             "No fills since the last briefing.", "Lifetime realised -$151."]
    lines = desk.fallback_lines(dg, facts)

    assert any("shadow" in ln for ln in lines), lines


def test_an_adopted_assignment_is_narrated_as_managed():
    """The presenter may say the shares are being worked off, and may name the
    stop — but only because both came off the sidecar. Before adoption existed
    the honest line was "nothing is watching those shares", and saying that
    about a managed position would be just as wrong in the other direction."""
    import briefing.desk as desk
    import briefing.host_briefing as hb
    dg = {"equity": 4848.0, "positions": [], "csp": {
        "enabled": True, "underlying": "SOXL",
        "assigned": {"qty": 100.0, "avg_price": 90.0, "adopted": True,
                     "basis": 90.0, "sell_by": "2026-09-04", "managed_by_engine": True},
        "short_put": None, "last": {"action": "skip"}}}

    truth = hb._options_truth(dg)
    assert "next weekly run" in truth and "2026-09-04" in truth
    assert "No stop, no covered calls" in truth
    assert "nothing is watching" not in truth

    sheet = desk.fallback_lines(dg, ["Account equity $4,848."])
    assert any("assigned" in ln.lower() for ln in sheet)


def test_an_unadopted_assignment_is_still_narrated_as_orphaned():
    """The warning has to survive: an assignment nobody adopted (basis
    unreadable, sleeve disabled mid-week) genuinely has no exit and no stop."""
    import briefing.host_briefing as hb
    dg = {"csp": {"enabled": True, "underlying": "SOXL",
                  "assigned": {"qty": 100.0, "avg_price": 90.0, "adopted": False,
                               "managed_by_engine": False},
                  "short_put": None, "last": None}}

    assert "nothing is watching" in hb._options_truth(dg)


def test_shadow_on_a_funded_account_does_not_claim_it_could_not_pay():
    """THE ACCOUNT GREW AND THE SENTENCE DID NOT.

    Until 2026-08-28 the only reason the sleeve shadowed was that $4,848 could not
    cover ~$10,350, so the reason was written into the sentence. On the $100,000
    account the cash IS there and shadow is a deliberate choice — narrating "the
    account does not have the collateral" would be a false statement about the
    presenter's own book, said out loud, in a video.

    The journalled decision already distinguishes them: `shortfall` is set only by
    gate 4, the affordability gate.
    """
    import briefing.desk as desk
    import briefing.host_briefing as hb
    funded = {"csp": {"enabled": True, "underlying": "SOXL", "assigned": None,
                      "short_put": None,
                      "last": {"action": "shadow", "strike": 90.0, "credit": 79.0,
                               "collateral": 9000.0}}}
    truth = hb._options_truth(funded)
    assert "BY CHOICE" in truth
    assert "cannot cover the collateral yet" not in truth, "that claim is now false"
    # and the writer is told, in as many words, not to reach for the old excuse
    assert "must NOT say she could not afford it" in truth

    _dg, facts = {"csp": funded["csp"]}, None
    sheet = desk.digest({"equity": 100000.0, "positions": [], "csp": funded["csp"]})[1]
    assert any("by choice" in f for f in sheet), sheet
    assert not any("does not have" in f for f in sheet), sheet


def test_shadow_with_a_real_shortfall_still_says_so():
    """The other half must survive: on an account that genuinely cannot pay, the
    refusal is the whole point of shadow mode and hiding it would be worse."""
    import briefing.host_briefing as hb
    broke = {"csp": {"enabled": True, "underlying": "SOXL", "assigned": None,
                     "short_put": None,
                     "last": {"action": "shadow", "strike": 90.0, "credit": 79.0,
                              "collateral": 9000.0, "shortfall": 5502.0}}}

    truth = hb._options_truth(broke)
    assert "cannot cover the collateral" in truth and "BY CHOICE" not in truth


# ------------------------------------------------- the account itself changed
def test_a_fresh_account_explains_its_own_empty_record():
    """"LIFETIME REALISED $0.00 OVER 0 ROUND TRIPS" IS TRUE AND SOUNDS LIKE A
    BROKEN SYSTEM.

    Lifetime P&L is computed from the BROKER's order history, so it correctly
    resets the day the account changes. Said aloud with no explanation it
    describes a strategy that has never worked. The account_switch row in the
    journal is the other half of that sentence.
    """
    import briefing.desk as desk
    import briefing.host_briefing as hb
    payload = {"portfolio": {"equity": 100000.0, "cash": 100000.0, "positions": []},
               "executions": {"rows": [], "realized_pnl": 0.0, "round_trips": 0,
                              "win_rate": None},
               "account": {"switched_days_ago": 0.0, "from_equity": 4848.01,
                           "to_equity": 100000.0, "reason": "operator switched"}}
    dg, facts = desk.digest(payload)

    assert any("account itself is new" in f for f in facts), facts
    assert any("track record starts here" in f for f in facts), facts
    truth = hb._account_age_truth(dg)
    assert "FRESH LEDGER" in truth
    assert "must NOT claim the previous account" in truth


def test_once_there_is_a_real_record_the_new_account_line_stops():
    """It is an explanation for an empty ledger, not a permanent banner. One
    closed round trip and the record speaks for itself."""
    import briefing.host_briefing as hb
    dg = {"equity": 100000.0, "round_trips": 3, "realized_pnl": 240.0,
          "account": {"switched_days_ago": 2.0, "from_equity": 4848.01}}

    assert hb._account_age_truth(dg) == ""


def test_an_old_switch_is_not_news():
    """desk_probe drops the key after four weeks; the truth block must also cope
    with a payload that simply has no account context."""
    import briefing.host_briefing as hb
    assert hb._account_age_truth({"equity": 100000.0}) == ""
    assert hb._account_age_truth(None) == ""
    assert hb._account_age_truth({"account": None}) == ""


def test_the_new_account_line_never_quotes_an_account_number():
    """The journal row carries both account numbers. They are credentials-adjacent
    and a public video is the wrong place for them, so desk_probe deliberately
    does not copy them into the payload."""
    import briefing.desk_probe as probe
    import inspect
    src = inspect.getsource(probe._account_context)
    assert "account_number" not in src.replace("account numbers", "")
    assert "from_account" not in src and "to_account" not in src


def test_the_recap_states_the_contract_count_when_it_is_not_one():
    """The sleeve stopped being a fixed one-lot on 2026-08-28. "It would have
    sold the 90 strike" is now an incomplete sentence — the size is a fact about
    the trade, and on a bigger account it is the most interesting one."""
    import briefing.desk as desk
    import briefing.host_briefing as hb
    csp_block = {"enabled": True, "underlying": "SOXL", "assigned": None,
                 "short_put": None,
                 "last": {"action": "shadow", "strike": 90.0, "credit": 114.0,
                          "collateral": 18_000.0, "contracts": 2,
                          "sized_by": "reserve"}}
    _dg, facts = desk.digest({"equity": 100_000.0, "positions": [], "csp": csp_block})

    line = [f for f in facts if "put strategy" in f][0]
    assert "2 contracts" in line
    assert "reserve" in line, "which ceiling chose the size is the story"

    truth = hb._options_truth({"csp": csp_block})
    assert "2 contracts" in truth and "moves with the account" in truth


def test_a_single_contract_is_not_announced_as_a_count():
    """"1 contracts of the 90 strike" is worse than saying nothing. One is the
    unremarkable case and reads better without the arithmetic."""
    import briefing.desk as desk
    csp_block = {"enabled": True, "underlying": "SOXL", "assigned": None,
                 "short_put": None,
                 "last": {"action": "shadow", "strike": 90.0, "credit": 57.0,
                          "collateral": 9_000.0, "contracts": 1, "sized_by": "cash"}}
    _dg, facts = desk.digest({"equity": 50_000.0, "positions": [], "csp": csp_block})

    line = [f for f in facts if "put strategy" in f][0]
    assert "contracts of" not in line and "1 contract" not in line
    assert "reserve allows" not in line, "cash-bound is not worth a clause"

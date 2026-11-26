"""Weekly cash-secured put on SOXL — the seller, its gates, and its shadow mode.

WHAT THIS IS
------------
Every week near the close: sell ONE next-Friday SOXL put a fixed distance out of
the money, funded from the sleeve's OWN reserve and regardless of what the share
book holds. If it expires worthless, do it again. If it is assigned, the 100
shares are SOLD at the very next weekly run — no covered calls, no stop — and a
new put goes out the same run if the reserve still covers one.

RUN IT FROM CRON, NOT FROM THE POLL LOOP. The signal engine polls every 300s and
must stay a bar-driven equity machine; this fires once a week on a wall clock.
See cron/at_et.sh, which already solves the ET/Melbourne DST drift.

WHAT THE 2026-08-27 STUDY SAID, BECAUSE IT SHAPES EVERY CHOICE HERE
------------------------------------------------------------------
* A ~95% win rate on SOXL needs 22-25% OTM, not the 5-10% of an ordinary name:
  SOXL's weekly stdev is 11.7% over 859 weeks back to 2010.
* THE STRIKE IS A FIXED DISTANCE, NEVER A DELTA OR A VOL-DERIVED PROBABILITY.
  The mechanical "re-pick a 5%-assignment strike each week from trailing
  realized vol" rule tests at -5.9% CAGR with an -82.7% drawdown, because vol
  clusters: trailing vol lags a regime change and you sell your CLOSEST strike
  immediately before the crash. This is the opposite of standard options
  practice and it is deliberate.
* There is a PREMIUM FLOOR. Breakeven is an IV of ~106-108% against SOXL's
  99.8% median realized vol, so a week that does not pay enough is a week not to
  sell. Nothing else in the design stops the strategy selling into a regime that
  has stopped compensating.
* Liquidity is a selection criterion. Only strikes ending in 0 or 5 quote
  tightly; crossing half the spread weekly costs ~5%/yr of collateral.

SHADOW MODE
-----------
Shadow chooses the full trade and journals it WITHOUT sending it, so the sleeve
can be watched for weeks before it ever trades and the recap can talk about it.

It was originally forced: $4,848 of options buying power against ~$10,350 needed
meant every Friday ended in an affordability refusal. Since 2026-08-28 the book
runs a $100,000 account with $100,000 of options buying power, so gate 4 no
longer fires and shadow is a DELIBERATE choice — the recap distinguishes the two
by whether the journalled decision carries a `shortfall`.

LIVE SINCE 2026-09-01 (operator's decision): `shadow` is false in
decisions/csp.json (NOT decisions.json — see CSP_CONFIG_FILE below), so the
weekly run SENDS a real sell-to-open. Set it back to true to return to choosing
and journalling without trading, or `enabled: false` to stop entirely; both are
read fresh on every run, so neither needs a restart. `--dry-run` still forces
shadow on for one invocation whatever the file says.

WHAT THE SLEEVE IS WORTH, measured 2026-08-28 and worth re-reading before
flipping that flag back. Gate 1 is GONE since c7e1181: the sleeve sells from its
own reserve every week, instead of the ~11 weeks a year (~17 on the Wed/Thu/Fri
retry) it managed while it waited for the all-in share book to be flat. On real
2024-26 Alpaca fills — a window that includes SOXL at -86% — the puts-only,
assigned-shares-sold-within-a-week form is the best variant tested: +17.3%/yr on
the sleeve's own collateral, -24% max drawdown, Sharpe 0.71. Every covered-call
and every stop variant was worse ("never sell below basis" is a -70% drawdown and
a 71-week hold at -81%).

The cost sits on the other side of the ledger, and it is why this is a genuine
judgement call rather than free money. `reserve_frac x equity` is capital the
share book no longer deploys: ~$736/contract/yr of foregone exposure on the
deploy sweep, against $59-$265/yr of premium at one contract. On those terms the
sleeve is NET NEGATIVE, and bigger is worse. The knob is really "how much do I
trust the equity backtest" — its 32.5% CAGR is in-sample on a bull tape, and if
the true expectancy is nearer zero the pledge costs nothing and the sleeve is a
small positive. Sensitivity is decisive and close to balanced: full-history CAGR
-3.7% at 0.7x premium, +12.9% at 1.0x, +50% at 1.3x. Note also that the sleeve
does NOT diversify the share book — SOXL's weekly correlation with the nine-name
watchlist mean is 0.80, and in the 12 weeks a 25%-OTM put would have been
assigned the other nine names averaged -8.12%.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

import logging

from .config import DECISIONS_FILE, JOURNAL_FILE

# OPERATOR POLICY LIVES IN ITS OWN FILE, NOT IN decisions.json.
#
# decisions.json is rewritten WHOLESALE by the research harness several times a
# day: agent/run_research.sh has `claude -p` write the file and
# agent/validate_decisions.py then does path.write_text(json.dumps(cleaned)).
# The research model has no reason to emit a `csp` block and no instruction to
# preserve one, so a block placed there survives only until the next cron —
# measured, not theorised: the 09:15 ET run on 2026-08-27 deleted it within
# hours of it being added.
#
# Whether to sell puts is an operator decision, not a research output, so it
# belongs in a file research never opens. Same reasoning as the postmarket
# script's hard guard that restores decisions.json after a recap run.
CSP_CONFIG_FILE = DECISIONS_FILE.parent / "csp.json"
from .logger import append_event
from .options_client import OptionsClient, PutQuote
from .risk_filter import EASTERN

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults. Every one is overridable from decisions.json's `csp` block so the
# research layer can retune or disable the strategy without a deploy.
# ---------------------------------------------------------------------------
DEFAULTS = {
    "enabled": False,          # opt-in: nothing happens until this is turned on
    "shadow": True,            # choose and journal the trade, do not send it
    "underlying": "SOXL",
    "min_otm": 0.22,           # fixed distance — see the module docstring
    "max_otm": 0.28,           # past this the premium is not worth the slot
    "dte_min": 5,              # next Friday, give or take a holiday
    "dte_max": 9,
    "contracts": "auto",       # "auto" sizes off the reserve; an int pins it
    "reserve_frac": 0.20,      # THE CAPITAL SPLIT: this slice of equity is the
                               # sleeve's — its collateral, and any shares it
                               # is assigned. The share book sizes off the rest.
    "max_contracts": 1,        # operator's choice 2026-08-28: one contract
    "min_credit_frac": 0.0035, # premium floor, as a fraction of collateral
    "max_spread_frac": 0.25,   # refuse an untradeable quote
    "close_reserve": 0.15,     # keep this much of the collateral free to buy back
}


@dataclass
class Decision:
    """What the manager decided this Friday, and why. Journaled either way."""
    action: str                       # "sell" | "shadow" | "skip"
    reason: str
    underlying: str
    spot: float | None = None
    symbol: str | None = None
    strike: float | None = None
    expiry: str | None = None
    limit: float | None = None
    credit: float | None = None
    collateral: float | None = None
    otm: float | None = None
    spread: float | None = None
    options_bp: float | None = None
    equity: float | None = None
    contracts: int | None = None
    sized_by: str | None = None       # which ceiling actually bound the count
    shortfall: float | None = None


_FRACTIONS = ("min_otm", "max_otm", "min_credit_frac", "max_spread_frac",
              "close_reserve", "reserve_frac")
_INTS = ("dte_min", "dte_max", "max_contracts")


def config(decisions: dict | None = None) -> dict:
    """The `csp` block from decisions.json, over the defaults — TYPE-COERCED.

    Every value here reaches either an order or a comparison, and JSON is
    hand-edited, so nothing may be trusted to be the type it looks like. Two
    coercions are safety rather than tidiness:

    * `shadow` is TRUE UNLESS IT IS LITERALLY False. Read by truthiness, a
      present-but-null `"shadow": null` overrode the default and armed a live
      order — a single keystroke between "watching" and "trading". Only the JSON
      boolean false turns the safety off; null, "false", 0 and a typo all leave
      it on.
    * `enabled` is the mirror: TRUE ONLY IF LITERALLY True, so `"enabled":
      "false"` — the most natural way to disable this by hand in a JSON file,
      and a non-empty string — disarms rather than arms.

    A bad number falls back to its default rather than raising: a research run
    writing `"min_otm": "0.22"` should not take the strategy down, and it
    certainly should not take it down mid-decision after the chain was fetched.
    A numeric string that parses is simply used — the string-ness was the
    accident, the value was the intent.

    `contracts` is either the string "auto" or an explicit integer. It is NOT
    floored here: run() refuses a non-positive explicit count outright rather
    than silently substituting 1, and the display paths should show what the
    file actually says. Anything unparseable falls back to "auto", which is the
    conservative direction — auto is bounded by a risk budget, a hand-typed
    integer is not.
    """
    if decisions is None:
        decisions = load_policy()
    cfg = dict(DEFAULTS)
    block = (decisions or {}).get("csp")
    if isinstance(block, dict):
        cfg.update(block)
    cfg["enabled"] = cfg.get("enabled") is True
    cfg["shadow"] = cfg.get("shadow") is not False
    cfg["underlying"] = str(cfg.get("underlying") or DEFAULTS["underlying"]).upper()
    for k in _FRACTIONS:
        try:
            cfg[k] = float(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    for k in _INTS:
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    # contracts: "auto" or an int, nothing else. A float, a null or a typo
    # becomes "auto" rather than a number nobody chose.
    c = cfg.get("contracts")
    if isinstance(c, str) and c.strip().lower() == "auto":
        cfg["contracts"] = "auto"
    else:
        try:
            cfg["contracts"] = int(c)
        except (TypeError, ValueError):
            cfg["contracts"] = "auto"
    # a negative or absurd reserve is a typo, not a setting
    if not (0.0 < cfg["reserve_frac"] <= 1.0):
        cfg["reserve_frac"] = DEFAULTS["reserve_frac"]
    if cfg["max_contracts"] < 1:
        cfg["max_contracts"] = DEFAULTS["max_contracts"]
    if cfg["min_otm"] > cfg["max_otm"]:
        cfg["min_otm"], cfg["max_otm"] = DEFAULTS["min_otm"], DEFAULTS["max_otm"]
    return cfg


def size_position(collateral: float, cfg: dict, equity: float,
                  options_bp: float, shares_value: float = 0.0) -> tuple[int, str]:
    """How many contracts to sell. Returns (n, which ceiling bound it).

    THE RESERVE *IS* THE CAPITAL SPLIT. `reserve_frac x equity` is the sleeve's
    money: it holds the put's collateral while flat and the assigned shares
    while it is working them off, and the share book sizes off everything
    else (sigengine.engine.fire_event subtracts the same reserve). Choosing the
    reserve is choosing the allocation; nothing else arbitrates.

    Sized off the RESERVE, not off account cash, for two reasons:

      * cash and equity diverge exactly when it matters — a book deployed in
        shares has equity and no cash, one just stopped out has the reverse —
        so cash-only sizing sells the most puts right after a drawdown;
      * cash alone would permit nine contracts on $100,000, i.e. 81% of the
        account converted into a 3x semiconductor ETF on one bad week.

    Measured 2026-08-28 (802 weekly windows of SOXL, 536 weeks of the whole
    watchlist): the sleeve does NOT diversify the share book — in the 12 weeks
    a 25%-OTM put would have been assigned the other nine names averaged
    -8.12%, and SOXL's weekly correlation with them is 0.80. More contracts is
    leverage on the bet already running. A fifth of equity keeps the sleeve's
    p99 bad week at ~2% of the account, inside the 5% daily halt.

    `shares_value` is the market value of assigned shares the sleeve is still
    holding (it sells them at this same weekly run; if that sale has not gone
    through they still occupy the reserve). Continuous weekly selling through
    an assignment therefore needs reserve >= shares + one more contract —
    about $16,000 per contract at today's prices, which is why the default
    reserve is 20% of a $100,000 account and not 10%.
    """
    if collateral <= 0:
        return 0, "no collateral figure"
    per = collateral * (1 + cfg["close_reserve"])
    reserve_pool = cfg["reserve_frac"] * max(0.0, equity) - max(0.0, shares_value)
    cash_pool = max(0.0, options_bp)
    pool = min(reserve_pool, cash_pool)
    n = int(pool // per) if pool > 0 else 0
    by_rail = int(cfg["max_contracts"])
    if n >= by_rail:
        return by_rail, "rail"
    # the smaller pool is the reason; cash wins a tie because it is the physical
    # limit and the only one that is a shortfall rather than a policy
    return n, ("cash" if cash_pool <= reserve_pool else "reserve")


def load_policy() -> dict:
    """The operator policy file, in the shape config()/run() take.

    Its own function because EVERY caller needs it and the one that did not have
    it silently ran the defaults: relay/csp_run.py used to hand run() the
    contents of decisions.json, which has no `csp` block by design — so config()
    never took the None branch that reads this file, and the live job reported
    "disabled" while csp.json said enabled. A config path with two ways in is a
    config path with one that is wrong.
    """
    try:
        block = json.loads(CSP_CONFIG_FILE.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:                                  # noqa: BLE001
        # A half-written or hand-corrupted policy file disarms the strategy
        # rather than running the defaults, and says why.
        _log.error("%s is unreadable (%s) — the put sleeve stays OFF this week",
                   CSP_CONFIG_FILE, e)
        return {}
    return {"csp": block}


def pledged_collateral(opt_client: OptionsClient) -> float:
    """Cash currently pledged to open short options, in dollars.

    THE ONE NUMBER BOTH BOOKS SHARE. sigengine.engine.fire_event subtracts it
    before sizing an equity entry, because Alpaca does NOT reduce `equity` when
    cash is pledged to a put — the engine would otherwise keep trying to buy
    0.97 x equity of stock and let a broker rejection decide which strategy
    wins. Best-effort: on any failure return 0.0 and let the broker be the
    backstop, since over-reporting would wrongly starve the equity book.
    """
    try:
        positions = opt_client.option_positions()
    except Exception as e:                                  # noqa: BLE001
        _log.warning("could not read option positions (%s) — reporting $0 pledged", e)
        return 0.0
    total = 0.0
    for p in positions:
        # PER POSITION, NOT PER PORTFOLIO. A single unparseable row used to
        # raise inside a function-wide try and return 0.0 for EVERYTHING, which
        # is the one answer that does real damage: fire_event subtracts this
        # before sizing an equity entry, so a false zero puts the equity book
        # back to bidding for cash that is already pledged to a put and lets a
        # broker rejection decide which strategy wins — precisely what this
        # function exists to prevent. Skipping one row understates the pledge by
        # one contract; swallowing the loop understates it by all of them.
        try:
            qty = float(getattr(p, "qty", 0) or 0)
            if qty >= 0:
                continue                       # long options pledge nothing
            k = _occ_strike(str(getattr(p, "symbol", "")))
            if k is not None:
                total += k * 100.0 * abs(qty)
        except Exception as e:                              # noqa: BLE001
            _log.warning("skipping an unparseable option position (%s)", e)
            continue
    return total


def _occ_strike(sym: str) -> float | None:
    """Strike of an OCC PUT symbol, or None if this is not one.

    One tolerant parser instead of three open-coded slices. Every one of those
    had the same off-by-one — `len(sym) >= 8` then `sym[-9]` — which raises on a
    9-character symbol and, inside a broad except, turns into a plausible wrong
    answer rather than an error.
    """
    s = (sym or "").upper()
    if len(s) < 16 or s[-9] != "P" or not s[-8:].isdigit():
        return None
    return int(s[-8:]) / 1000.0


def _occ_expiry(sym: str) -> str | None:
    """YYYY-MM-DD from an OCC symbol, or None."""
    s = (sym or "").upper()
    if len(s) < 16 or not s[-15:-9].isdigit():
        return None
    return f"20{s[-15:-13]}-{s[-13:-11]}-{s[-11:-9]}"


def next_expiry(opt: OptionsClient, cfg: dict, today: date) -> str | None:
    """NEXT FRIDAY's expiry, or the nearest thing to it inside the window.

    SOXL lists Monday, Wednesday AND Friday weeklies, and taking the nearest
    expiry in the window silently picks the WEDNESDAY. Run on Thursday
    2026-08-27 with the default 5-9 day window the candidates are 2026-09-02
    (Wed) and 2026-09-04 (Fri), and "first" is the Wednesday — a 5-day contract
    where the strategy is specified, backtested and priced as a 7-day one, at a
    strike distance calibrated for a full week.

    Narrowing the window is not a fix: it only lands correctly when the job runs
    on exactly the right weekday, and one holiday or one late cron breaks it
    again. Preferring the Friday is what the strategy actually means, so that is
    what is encoded — falling back to whatever is in the window if a holiday
    week lists no Friday at all.
    """
    lo = (today + timedelta(days=cfg["dte_min"])).isoformat()
    hi = (today + timedelta(days=cfg["dte_max"])).isoformat()
    exps = opt.expirations(cfg["underlying"], lo, hi)
    if not exps:
        return None
    fridays = [e for e in exps if date.fromisoformat(e).weekday() == 4]
    # No Friday listed means a holiday week; its LAST expiry is that week's
    # Friday-equivalent. Taking the first would reintroduce the very
    # short-dated contract this function exists to avoid.
    return fridays[0] if fridays else exps[-1]


def pick_strike(chain: list[PutQuote], cfg: dict) -> tuple[PutQuote | None, str]:
    """The furthest-out tradeable put inside the OTM band. Returns (quote, why).

    Ordering matters. Candidates are filtered to the band first, then to what is
    actually quotable, and only then is the FURTHEST one taken — being further
    out is strictly safer and, at the strikes that matter, often more liquid
    too (2026-08-26: strike 95 was both further OTM and 7x tighter than 97).
    """
    band = [q for q in chain if cfg["min_otm"] <= q.otm_frac <= cfg["max_otm"]]
    if not band:
        return None, f"no listed strike between {cfg['min_otm']:.0%} and {cfg['max_otm']:.0%} OTM"
    liquid = [q for q in band if q.spread_frac <= cfg["max_spread_frac"]]
    if not liquid:
        w = min(band, key=lambda q: q.spread_frac)
        return None, (f"every strike in the band is too wide to trade "
                      f"(best was {w.strike:g} at {w.spread_frac:.0%} of mid)")
    paying = [q for q in liquid if q.bid * 100 >= cfg["min_credit_frac"] * q.collateral]
    if not paying:
        b = max(liquid, key=lambda q: q.bid * 100 / q.collateral)
        return None, (f"premium below the floor — best was {b.strike:g} paying "
                      f"${b.bid * 100:,.0f} on ${b.collateral:,.0f} "
                      f"({b.bid * 100 / b.collateral:.3%} vs {cfg['min_credit_frac']:.3%} required). "
                      f"The regime is not paying for this risk this week.")
    return max(paying, key=lambda q: q.otm_frac), "ok"


def run(opt: OptionsClient, equity_positions, decisions: dict | None = None,
        today: date | None = None, log=print, sell_shares=None) -> Decision:
    """One weekly decision. Side effects: the order(s) and the journal.

    THE DESIGN (operator's choice 2026-08-28, from the backtest): a 25%-OTM put
    sold EVERY week from the sleeve's own reserve, and assigned shares sold at
    the very next weekly run — no covered calls, no stop. Puts-only was the
    best risk-adjusted form on real 2024-26 fills (+17%/yr, -24% max drawdown);
    every covered-call variant added drawdown, not safety, and holding the
    shares any longer than a week turned into one- to two-year bag-holds at
    -80% in the 2020 and 2022 crashes.

    Each run, in order:
      1. sell the shares last week's put was assigned into (if any);
      2. refuse if a put is still live or a sell order is still working;
      3. close today's expiring put for pennies if it is OTM (frees the
         collateral for the new sale; an ITM one is left to be assigned);
      4. pick the strike, size from the reserve, sell.

    `equity_positions` is the broker's position list (None = the fetch failed,
    which is a refusal, not a flat book). `sell_shares(symbol, qty) -> status`
    closes assigned shares through the relay; None (dry-run, tests) sells
    nothing and sizes around the shares instead.
    """
    cfg = config(decisions)
    und = cfg["underlying"]
    today = today or datetime.now(EASTERN).date()
    d = lambda **kw: Decision(underlying=und, **kw)                  # noqa: E731

    if not cfg["enabled"]:
        return _journal(d(action="skip", reason="csp.enabled is false"), log)

    # A PINNED CONTRACT COUNT THAT IS NOT A POSITIVE WHOLE NUMBER IS NOT A
    # SMALLER TRADE, IT IS A BROKEN ONE. `contracts: 0` would make the
    # collateral requirement $0 and a zero-lot order goes to the broker; -1
    # submits a BUY dressed as a sell. Refusing beats substituting a default —
    # someone who wrote 0 did not ask to trade. ("auto" sizes itself.)
    if cfg["contracts"] != "auto" and cfg["contracts"] < 1:
        return _journal(d(action="skip", reason=(
            f"csp.contracts is {cfg['contracts']} — refusing to size a trade "
            f"from a non-positive contract count")), log)

    # --- gate 0: do we even know what the book holds? ----------------------
    # None is "the positions fetch failed", not "the book is flat", and the two
    # must never collapse: an assignment we cannot see is one we cannot sell.
    if equity_positions is None:
        return _journal(d(action="skip", reason=(
            "could not read the share book — refusing to trade against an "
            "unknown position state")), log)

    # --- step 1: last week's assignment is sold NOW ------------------------
    # The share book is no longer consulted here at all (the old "gate 1"): the
    # sleeve has its own reserve, and what the share book holds is its own
    # business. The only shares this sleeve cares about are the ones a put of
    # its own was assigned into.
    shares_value = 0.0
    assigned = detect_assignment(None, equity_positions, decisions)
    if assigned is not None:
        qty = _f(getattr(assigned, "qty", None)) or 0.0
        px = _f(getattr(assigned, "current_price", None))
        if not px:
            mv = _f(getattr(assigned, "market_value", None))
            px = (mv / qty) if (mv and qty) else 0.0
        if sell_shares is None or cfg["shadow"]:
            shares_value = qty * px
            log(f"  [csp] holding {qty:g} assigned {und} shares (not sold: "
                f"{'shadow mode' if cfg['shadow'] else 'no seller'}) — sizing around them")
        else:
            try:
                status = sell_shares(und, qty)
                append_event(JOURNAL_FILE, {
                    "event": "csp_assignment_sold", "ticker": und, "qty": qty,
                    "price": px, "status": str(status),
                    "reason": "assigned shares sold at the sleeve's next weekly run"})
                log(f"  [csp] sold {qty:g} assigned {und} shares ({status})")
            except Exception as e:                          # noqa: BLE001
                shares_value = qty * px
                append_event(JOURNAL_FILE, {
                    "event": "csp_assignment_sell_failed", "ticker": und, "qty": qty,
                    "reason": str(e)})
                log(f"  [csp] could not sell assigned shares ({e}) — sizing around them")

    # --- gate 3: a put is still live, OR a sale is still working -----------
    # The put is a DAY LIMIT at the bid on a book that quotes as wide as 52% of
    # mid, so it frequently RESTS unfilled — and until it fills there is no
    # position. Checking positions alone let a second invocation in the same
    # session sell another contract on top of the first.
    #
    # TODAY'S EXPIRING CONTRACT DOES NOT COUNT. The run is at 15:45 ET on
    # expiry day, when last week's put is still in the position list for
    # another fifteen minutes. Treating it as "a put is already open" would
    # make the sleeve refuse every single Friday and never roll.
    expiring = []
    try:
        live = []
        for p in opt.option_positions():
            sym = str(getattr(p, "symbol", ""))
            (expiring if _occ_expiry(sym) == today.isoformat() else live).append(p)
        if live:
            return _journal(d(action="skip", reason=(
                "a short put is already open — one contract at a time")), log)
        # A working BUY (our own buy-to-close of the expiring contract) is not
        # a stacking risk; a working SELL is exactly that. An order whose side
        # cannot be read counts as a sell — not knowing is not permission.
        def _is_buy(o):
            side = getattr(o, "side", None)
            return str(getattr(side, "value", side) or "").lower().endswith("buy")
        sells = [o for o in opt.open_option_orders() if not _is_buy(o)]
        if sells:
            return _journal(d(action="skip", reason=(
                "a sell-to-open is already working — waiting for it to fill or "
                "expire rather than stacking a second contract on top")), log)
    except Exception as e:                                  # noqa: BLE001
        # FAIL CLOSED. Not knowing whether we already have a put is not a
        # licence to sell another one.
        return _journal(d(action="skip", reason=(
            f"could not read the option book ({e}) — refusing to sell blind")), log)

    exp = next_expiry(opt, cfg, today)
    if not exp:
        return _journal(d(action="skip", reason=(
            f"no {und} expiry between {cfg['dte_min']} and {cfg['dte_max']} days out")), log)

    spot = _spot(opt, und)
    if not spot:
        return _journal(d(action="skip", reason=f"no usable {und} price"), log)

    # --- step 3: free today's collateral -----------------------------------
    # An expiring put that is OUT of the money is worth a few cents and still
    # pledges strike x 100 until the close. Buying it back now releases that
    # cash for the new contract fifteen minutes early, at a cost of about 1% of
    # the week's premium. One that is IN the money is left alone: it will be
    # assigned tonight, and the backtest's "sold within a week" rule is
    # measured from exactly that assignment.
    if not cfg["shadow"]:
        for p in expiring:
            sym = str(getattr(p, "symbol", ""))
            k = _occ_strike(sym)
            if k is None or k >= spot:
                continue                       # ITM or unparseable: leave it
            try:
                opt.buy_put_to_close(sym, 0.05, int(abs(_f(getattr(p, "qty", 1)) or 1)))
                append_event(JOURNAL_FILE, {"event": "csp_close_expiring", "ticker": und,
                                           "symbol": sym, "limit": 0.05,
                                           "reason": "expiring OTM — freeing the collateral"})
            except Exception as e:                          # noqa: BLE001
                log(f"  [csp] could not close expiring {sym} ({e}) — the cash gate decides")

    chain = opt.put_chain(und, exp, spot * (1 - cfg["max_otm"] - 0.05),
                          spot * (1 - cfg["min_otm"] + 0.05), spot)
    q, why = pick_strike(chain, cfg)
    if q is None:
        # carry the expiry: this is the row shadow mode exists to produce, and a
        # refusal that cannot be tied to a contract week is not evidence
        return _journal(d(action="skip", reason=why, spot=spot, expiry=exp), log)

    # ONE account read, two numbers: equity sets the reserve, cash is what the
    # broker will actually let us pledge right now.
    acct = opt.account_snapshot()
    bp, equity = acct["options_buying_power"], acct["equity"]

    # --- step 4: how many? -------------------------------------------------
    if cfg["contracts"] == "auto":
        n, sized_by = size_position(q.collateral, cfg, equity, bp, shares_value)
    else:
        n, sized_by = int(cfg["contracts"]), "pinned"
        # A PINNED COUNT IS STILL CAPPED BY THE RESERVE — otherwise the one knob
        # that decides the capital split could be bypassed by typing a number
        # into the same file. Deliberately NOT capped by cash: trimming an
        # unaffordable pinned 2 to an affordable 1 would quietly trade something
        # the operator did not ask for; the cash check below refuses instead.
        cap, _ = size_position(q.collateral, {**cfg, "max_contracts": 10 ** 6},
                               equity, 10 ** 12, shares_value)
        if n > cap:
            n, sized_by = cap, "pinned, capped by the reserve"

    # THE ROW DESCRIBES THE INTENDED TRADE EVEN WHEN THE COUNT IS ZERO. Shadow
    # mode exists to name the trade that did not happen, and a refusal reported
    # as "$0 of collateral on 0 contracts" names nothing. `contracts` still
    # tells the truth.
    shown = n if n >= 1 else 1
    credit = q.bid * 100 * shown
    need = q.collateral * n * (1 + cfg["close_reserve"])
    base = dict(spot=spot, symbol=q.symbol, strike=q.strike, expiry=exp,
                limit=q.bid, credit=credit, collateral=q.collateral * shown,
                otm=q.otm_frac, spread=q.spread_frac, options_bp=bp,
                equity=equity, contracts=n, sized_by=sized_by)

    if n < 1:
        one = q.collateral * (1 + cfg["close_reserve"])
        reserve = cfg["reserve_frac"] * equity
        if sized_by == "cash":
            return _journal(d(action="shadow" if cfg["shadow"] else "skip",
                              reason=(f"cannot cash-secure even one contract: needs "
                                      f"${one:,.0f} (${q.collateral:,.0f} collateral + "
                                      f"{cfg['close_reserve']:.0%} buy-back reserve), "
                                      f"options buying power is ${bp:,.0f}"),
                              shortfall=one - bp, **base), log)
        held = (f" after the ${shares_value:,.0f} of assigned shares still in it"
                if shares_value else "")
        return _journal(d(action="skip", reason=(
            f"one contract needs ${one:,.0f} and the sleeve's reserve is "
            f"${reserve:,.0f} ({cfg['reserve_frac']:.0%} of ${equity:,.0f}){held}. "
            f"The account could pay; the reserve says not to."), **base), log)

    # --- the cash check, which only a PINNED count can now fail -------------
    if bp < need:
        return _journal(d(action="shadow" if cfg["shadow"] else "skip",
                          reason=(f"cannot cash-secure {n} contract"
                                  f"{'s' if n != 1 else ''}: needs ${need:,.0f} "
                                  f"(${q.collateral * n:,.0f} collateral + "
                                  f"{cfg['close_reserve']:.0%} buy-back reserve), "
                                  f"options buying power is ${bp:,.0f}"),
                          shortfall=need - bp, **base), log)

    if cfg["shadow"]:
        return _journal(d(action="shadow", reason=(
            f"shadow mode — this is the trade it would have placed "
            f"({n} contract{'s' if n != 1 else ''}, sized by {sized_by})"), **base), log)

    # --- send it -----------------------------------------------------------
    # SELL AT THE BID, not the mid. A limit that does not fill is a week with no
    # trade, which is a recoverable outcome; chasing a thin book is not.
    opt.sell_put_to_open(q.symbol, q.bid, n)
    return _journal(d(action="sell",
                      reason=f"sold {n} to open (sized by {sized_by})", **base), log)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------
def detect_assignment(opt: OptionsClient | None, equity_positions,
                      decisions: dict | None = None):
    """Have we been put the shares? Returns the position, or None.

    `opt` is accepted for symmetry with the rest of this module and is NOT used:
    the whole answer lives in the equity positions the caller already has, plus
    the journal. sigengine.engine._adopt_assignments passes None precisely so
    that adopting an assignment costs the poll loop no extra broker call.

    DETECT BY POLLING POSITIONS, NOT BY READING ACTIVITIES. On paper Alpaca
    syncs the non-trade activities that report an assignment (OPASN/OPEXP) only
    the NEXT day, while balances and positions update immediately — so an
    activities-driven detector would be a day late every time, which on a weekly
    strategy is most of the holding period.

    The signature of an assignment here is unambiguous because the strategy
    holds one thing at a time: shares of the underlying appear while the equity
    engine never bought them.
    """
    cfg = config(decisions)
    und = cfg["underlying"].upper()
    from .risk_filter import _is_equity_position
    for p in (equity_positions or []):
        if not _is_equity_position(p):
            continue
        if str(getattr(p, "symbol", "")).upper() != und:
            continue
        try:
            qty = float(getattr(p, "qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        # EVIDENCE, NOT COINCIDENCE. SOXL is an ENABLED trading symbol in
        # sigengine/config.json, so the equity engine buys and sells it in the
        # ordinary course — calling any long SOXL position an assignment would
        # relabel the engine's own trade and, through the recap, tell the
        # presenter she was put shares she actually chose to buy.
        #
        # An assignment is 100 shares per contract, exactly. The engine sizes
        # off 0.97 x equity and lands on whatever whole number that gives, so a
        # round multiple of 100 is weak but real evidence; requiring a prior
        # csp_sell in the journal is the strong form and is what
        # _assignment_is_corroborated adds on top.
        if qty % 100 != 0:
            continue
        if not _assignment_is_corroborated(und, qty):
            continue
        return p
    return None


def _assignment_is_corroborated(und: str, qty: float) -> bool:
    """Did this sleeve actually sell a put that could have produced `qty` shares?

    Reads the journal for a csp_sell on this underlying in the last ~30 days. A
    put that was never sold cannot have been assigned, so without one the shares
    are the equity engine's and must not be relabelled.

    Fails CLOSED — an unreadable journal answers "no". The cost of a false
    negative is that an assignment is described as an ordinary holding; the cost
    of a false positive is telling the operator, in a video, that they were put
    shares they in fact bought on purpose.
    """
    try:
        cutoff = datetime.now(EASTERN).timestamp() - 30 * 86400
        for ln in JOURNAL_FILE.read_text().splitlines():
            if '"csp_sell"' not in ln:
                continue
            r = json.loads(ln)
            if (str(r.get("ticker", "")).upper() == und
                    and float(r.get("ts", 0) or 0) >= cutoff):
                return True
    except Exception:                                       # noqa: BLE001
        return False
    return False


def assignment_state(opt: OptionsClient, equity_positions, aux: dict | None = None,
                     decisions: dict | None = None) -> dict:
    """A small dict describing where the strategy stands, for the recap and the
    dashboard. Never raises — a display path must not be able to break a render.

    `aux` is the engine's entry sidecar (sigengine.engine.load_aux). Shares the
    ENGINE bought appear in aux["opened"]; shares a put was assigned into appear
    in aux["adopted"], together with the basis their stop is measured from.
    Either way they are managed. A position in NEITHER map is something nobody
    is looking after — that is the one state worth saying out loud.
    """
    cfg = config(decisions)
    out = {"enabled": bool(cfg["enabled"]), "shadow": bool(cfg["shadow"]),
           "underlying": cfg["underlying"], "short_put": None, "assigned": None}
    try:
        for p in opt.option_positions():
            qty = float(getattr(p, "qty", 0) or 0)
            if qty >= 0:
                continue
            sym = str(getattr(p, "symbol", ""))
            out["short_put"] = {
                "symbol": sym, "contracts": abs(qty),
                # tolerant: a symbol we cannot parse degrades these two fields,
                # it does not erase the fact that a put is open — reporting "no
                # short put" while one is live is the misleading direction
                "strike": _occ_strike(sym),
                "expiry": _occ_expiry(sym),
                "unrealized": _f(getattr(p, "unrealized_pl", None)),
                "market_value": _f(getattr(p, "market_value", None)),
            }
            break
    except Exception:                                       # noqa: BLE001
        pass
    try:
        pos = detect_assignment(opt, equity_positions, decisions)
        if pos is not None:
            sym_u = str(getattr(pos, "symbol", "")).upper()
            adopted = ((aux or {}).get("adopted", {}) or {}).get(sym_u)
            engine_opened = sym_u in set((aux or {}).get("opened", {}) or {})
            basis, anchor = None, None
            if isinstance(adopted, dict):
                basis = _f(adopted.get("basis"))
                anchor = _f(adopted.get("anchor")) or basis
            out["assigned"] = {
                "symbol": str(getattr(pos, "symbol", "")),
                "qty": _f(getattr(pos, "qty", None)),
                "avg_price": _f(getattr(pos, "avg_entry_price", None)),
                "unrealized": _f(getattr(pos, "unrealized_pl", None)),
                "unrealized_pct": _f(getattr(pos, "unrealized_plpc", None)),
                # THE IMPORTANT FLAG. False means NOBODY is managing these
                # shares: the share strategies only close what they simulated
                # opening, the reconciler only closes what is in `opened`, and
                # without an adoption record there is no stop either. That state
                # needs an operator, and the recap says so.
                "managed_by_engine": bool(engine_opened or adopted),
                "adopted": bool(adopted),
                "basis": basis,             # the strike — what the shares cost
                "anchor": anchor,           # the price they arrived at
                # WHEN THEY LEAVE: at the sleeve's next weekly run, not at a
                # price. There is no stop on these shares by design (operator
                # 2026-08-28) — the backtest's "sold within a week" rule is the
                # exit, and the engine only steps in if that run fails to
                # happen for ten days.
                "sell_by": _sell_by(adopted),
            }
    except Exception:                                       # noqa: BLE001
        pass
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sell_by(adopted) -> str | None:
    """ISO date of the weekly run that will sell these shares: the first Friday
    after adoption. Display only."""
    try:
        since = datetime.fromisoformat(str((adopted or {}).get("since")).replace("Z", "+00:00"))
        days = (4 - since.weekday()) % 7 or 7
        return (since + timedelta(days=days)).date().isoformat()
    except Exception:                                       # noqa: BLE001
        return None


def _spot(opt: OptionsClient, und: str) -> float | None:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        c = opt._data._api_key, opt._data._secret_key
        sc = StockHistoricalDataClient(*c)
        t = sc.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=und))
        return float(t[und].price)
    except Exception:                                       # noqa: BLE001
        return None


def _journal(dec: Decision, log) -> Decision:
    log(f"  [csp] {dec.action.upper()}: {dec.reason}")
    if dec.symbol:
        log(f"  [csp]   {dec.symbol}  strike {dec.strike:g} ({dec.otm:.1%} OTM)  "
            f"credit ${dec.credit:,.0f} on ${dec.collateral:,.0f} collateral")
    append_event(JOURNAL_FILE, {"event": f"csp_{dec.action}", "ticker": dec.underlying,
                               **{k: v for k, v in asdict(dec).items()
                                  if k not in ("action", "underlying") and v is not None}})
    return dec

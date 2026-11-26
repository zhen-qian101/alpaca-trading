"""Pre-trade risk gate.

Reads decisions/decisions.json (written by the Claude research agent in Phase 4)
as an allowlist + per-symbol bias/sizing. If the file is missing or empty, the
filter is permissive (so Phase 1 works without the Claude layer). All hard
limits are enforced HERE in code — Claude can only loosen within these ceilings.

Schema of decisions.json:
{
  "watchlist": ["AAPL", "MSFT", "NVDA"],
  "bias": {"AAPL": "long", "MSFT": "flat", "NVDA": "long"},
  "suggested_size": {"AAPL": 0.97, "NVDA": 0.0},    # DERIVED readout, not an input:
                                                    #   the engine's deploy fraction for a
                                                    #   long, 0 otherwise. This book is
                                                    #   all-in/all-out; nothing here sizes.
  "max_position": {"AAPL": 100},                    # max shares (hard ceiling)
  "daily_loss_limit": 0.02,                          # halt at 2% daily loss
  "allow_extended_hours": false,
  "contingencies": {                                # OPTIONAL pre-approved overrides
    "MSFT": [
      {
        "condition": "close_above",                 # or "close_below"
        "level": 210.0,
        "action": "long",                            # only "long" supported
        "valid_until": "2026-07-07T04:00:00+00:00",   # ISO8601, UTC; expired = ignored
        "reason": "why this specific level would flip the flat/short bias"
      }
    ]
  }
}

Contingencies (2026-07-06): a symbol whose bias is "flat"/"short" normally
blocks every buy signal until the NEXT scheduled research refresh (hours away)
re-evaluates it — a real source of missed opportunities on fast-moving names,
since the signal engine reacts every poll cycle but bias only reacts on a fixed
schedule. A contingency lets the SAME scheduled research pass pre-authorize a
specific, well-reasoned price condition ("if it breaks above X, that's a real
setup, not just leverage/noise") that the risk gate can evaluate MECHANICALLY,
with zero added latency, the instant a signal fires — no live LLM call needed
at signal time. See `_active_contingency()` below for the evaluation logic.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import DECISIONS_FILE

# Hard-coded ceilings that no decisions.json value can exceed.
HARD_MAX_POSITION = 10000         # shares, absolute cap. RESTORED 2026-08-28 when the
                                  # book moved to a $100,000 paper account
                                  # (the current paper account); it was 2000 for the ~$5k account
                                  # and 10000 for the $100k one before that, so this
                                  # is the same calibration as the last time the
                                  # account was this size.
                                  #
                                  # The number has to move WITH the account or it
                                  # stops being an absurd-order rail and becomes a
                                  # silent position cap: a full 97% deploy of
                                  # $100,000 is $97,000, which binds at 2000 shares
                                  # for anything under ~$48.50 — and TQQQ, a 3x ETF
                                  # in this watchlist, trades near $90. One ordinary
                                  # drawdown and the engine would have been quietly
                                  # buying half-size while its own simulation
                                  # believed it was all-in.
                                  #
                                  # At 10000 the binding price is ~$9.70/share, which
                                  # nothing in the watchlist approaches. The stop and
                                  # daily_loss_limit remain the real risk bounds.
HARD_DAILY_LOSS_LIMIT = 0.05       # 5% of equity, absolute cap
DEDUP_WINDOW_SEC = 60               # identical alert within this window is dropped

EASTERN = ZoneInfo("America/New_York")

# Module-level dedup store, shared across requests (process-local).
_DEDUP: dict[str, float] = {}


class RiskError(Exception):
    """Raised when a trade should be blocked. Caught by the caller (signal engine
    or tests) and journaled; the relay is a library so there is no HTTP 403 anymore.

    `transient` separates a block that is a DELIBERATE REFUSAL — bias says flat,
    the symbol is off the allowlist, the daily loss limit has tripped, the size
    ceiling was exceeded — from one that is merely the wrong MOMENT: the 60s
    dedup window, an order already working, a rotation exit still resting. The
    engine may re-attempt the second kind on a later poll; re-attempting the
    first would be arguing with a decision that was correctly taken (see
    sigengine.engine._pending_entry). Default False, so a new raise site is a
    refusal until someone deliberately says otherwise.
    """

    def __init__(self, *args, transient: bool = False):
        super().__init__(*args)
        self.transient = transient


def _is_dup(ticker: str, action: str) -> bool:
    key = f"{ticker}:{action}"
    now = time.time()
    ts = _DEDUP.get(key)
    if ts is not None and now - ts < DEDUP_WINDOW_SEC:
        return True
    _DEDUP[key] = now
    return False


def _load_decisions() -> dict:
    try:
        return json.loads(DECISIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _parse_utc(ts) -> datetime | None:
    """Best-effort ISO8601 -> aware UTC datetime. None on anything malformed —
    callers must treat that as "can't verify, don't trust it"."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _active_contingency(rules: list, price: float | None) -> dict | None:
    """First rule in `rules` (decisions.json's contingencies[SYMBOL] list)
    that is (a) not expired and (b) whose price condition `price` currently
    satisfies. Returns None if `rules` is empty, `price` is unknown, or no
    rule matches. See risk_filter's module docstring for the schema — this is
    the mechanical, zero-LLM-call evaluation of a pre-approved breakout/
    breakdown level the research agent wrote in advance, so a fast-moving
    signal doesn't have to wait for the next scheduled research refresh to be
    allowed through a flat/short bias."""
    if not rules or price is None:
        return None
    now = datetime.now(timezone.utc)
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        valid_until = _parse_utc(rule.get("valid_until"))
        if valid_until is None or valid_until <= now:
            continue  # expired, malformed, or missing — never trust silently
        if rule.get("action") != "long":
            continue  # only entry-permission overrides are supported today
        condition = rule.get("condition")
        try:
            level = float(rule.get("level"))
        except (TypeError, ValueError):
            continue
        if condition == "close_above" and price > level:
            return rule
        if condition == "close_below" and price < level:
            return rule
    return None


def decisions_for_rotation() -> tuple[dict[str, int], dict[str, str]]:
    """Return (priority_map, bias_map) for the relay's rotation logic.

    Both maps are keyed by UPPERCASE symbol. `priority` is higher = more
    preferred (longs > 0, flats/absent = 0). Used by order_manager to decide
    whether a new candidate's buy signal should rotate out of the held symbol.
    """
    d = _load_decisions()
    bias = {s.upper(): b for s, b in (d.get("bias") or {}).items()}
    pri = {s.upper(): p for s, p in (d.get("priority") or {}).items()}
    # Default priority from bias: longs default to 1 if not declared, else 0.
    for sym, b in bias.items():
        if sym not in pri:
            pri[sym] = 1 if b == "long" else 0
    return pri, bias


def _market_open(allow_ext: bool) -> bool:
    """True during US equity regular session (9:30–16:00 ET, Mon–Fri), or
    Alpaca's full 24/5 window if allow_ext (extended 2026-07-06 from the
    previous 4:00-20:00 ET-only range: Alpaca's 24/5 trading — see
    https://alpaca.markets/learn/how-to-trade-us-stocks-24_5-overnight-with-python-and-alpaca —
    is nearly continuous from Sunday 20:00 ET through Friday 20:00 ET,
    covering the TRUE overnight session (20:00-04:00 ET) that the old
    4:00-20:00 range excluded, not just standard pre-market/after-hours).
    Only requires limit orders + extended_hours=True outside the regular
    9:30-16:00 ET session — see regular_market_hours_open() and
    order_manager.handle()."""
    now_et = datetime.now(EASTERN)
    wd = now_et.weekday()  # Mon=0 ... Sun=6
    minutes = now_et.hour * 60 + now_et.minute
    if allow_ext:
        if wd == 5:  # Saturday: always closed
            return False
        if wd == 4 and minutes >= 20 * 60:  # Friday after 20:00 ET: closed
            return False
        if wd == 6 and minutes < 20 * 60:  # Sunday before 20:00 ET: closed
            return False
        return True
    if wd >= 5:
        return False
    return 9 * 60 + 30 <= minutes <= 16 * 60


def regular_market_hours_open() -> bool:
    """True only during the standard 9:30-16:00 ET regular session, Mon-Fri —
    distinguishes "regular" (plain market orders fine) from "extended/
    overnight" (Alpaca requires extended_hours=True + a LIMIT order, not
    market — see order_manager.handle()) within the broader window
    _market_open(allow_ext=True) permits. Crypto trades 24/7 on Alpaca with
    no such distinction — callers should check is_crypto_symbol first."""
    now_et = datetime.now(EASTERN)
    if now_et.weekday() >= 5:
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def equity_market_closed_for_weekend() -> bool:
    """True from Friday 20:00 ET (extended-hours close) through Monday 04:00 ET
    (extended-hours open) — the one continuous stretch where equity data cannot
    possibly have changed, so polling it (IBKR or yfinance) burns request/rate
    budget for zero information. Added 2026-07-04 so sigengine/engine.py can
    skip equity data fetches over the weekend, both to stay well clear of
    IBKR's 60-historical-requests-per-10-min cap and because there's nothing to
    gain from it. Crypto is exempt — see is_crypto_symbol, it trades 24/7."""
    now_et = datetime.now(EASTERN)
    wd = now_et.weekday()  # Mon=0 ... Sun=6
    minutes = now_et.hour * 60 + now_et.minute
    if wd in (5, 6):  # Saturday, Sunday
        return True
    if wd == 4 and minutes >= 20 * 60:  # Friday after 20:00 ET
        return True
    if wd == 0 and minutes < 4 * 60:  # Monday before 04:00 ET
        return True
    return False


def is_option_symbol(sym: str) -> bool:
    """True for an OCC option symbol like SOXL260904P00090000.

    Used as a BACKSTOP wherever an asset_class field is not available (a test
    double, an older SDK). The real test is asset_class == US_OPTION; this
    catches the shape: root + 6-digit date + C/P + 8-digit strike = at least 15
    trailing characters that are digits with one C/P in the middle. No equity or
    crypto symbol this book trades can match it.
    """
    s = (sym or "").upper()
    if len(s) < 16 or "/" in s:
        return False
    tail = s[-15:]
    return tail[:6].isdigit() and tail[6] in "CP" and tail[7:].isdigit()


def _is_equity_position(p) -> bool:
    """Is this broker position a plain equity/crypto one the relay may act on?

    THE RELAY IS EQUITY-ONLY AND MUST SAY SO. Every helper below iterates
    whatever get_all_positions() returns, and that is an unfiltered passthrough
    to alpaca-py, which includes OPTION positions with `symbol` set to the OCC
    ticker. Before this filter existed a single open short put read as "the
    other position held": _should_rotate looked up the OCC string's priority in
    decisions.json, got the default 0, any long-bias candidate beat it, and the
    rotation branch called _close_position(<OCC>) — a market BUY-TO-CLOSE of
    the option, bypassing the risk gate entirely because check() had run
    against the equity payload. Outside regular hours it failed differently and
    just as badly: _reference_price built a STOCK quote request for an OCC
    symbol, found nothing, and raised, killing the equity buy path.

    That fires on the first equity signal after any option is opened, by the
    engine or by hand, with no options code written anywhere. See the
    2026-08-27 SOXL cash-secured put study.
    """
    ac = getattr(p, "asset_class", None)
    if ac is not None:
        return str(getattr(ac, "value", ac)).lower() != "us_option"
    return not is_option_symbol(str(getattr(p, "symbol", "")))


def is_crypto_symbol(ticker: str) -> bool:
    """Crypto pairs trade 24/7 on Alpaca and are exempt from the equity
    market-hours gate. Accepts the Alpaca canonical form (BTC/USD) and the
    slash-less variants (BTCUSD, BTC)."""
    t = ticker.upper().strip()
    if t.endswith("/USD"):
        return True
    return t in {"BTCUSD", "ETHUSD", "BTC", "ETH"}


# A crypto chart may emit the slash-less form (BTCUSD), but the Alpaca symbol +
# our watchlist/allowlist use the canonical pair (BTC/USD). Map the variants so
# allowlist, bias, rotation, and the submitted order all agree on one key.
# Equities are returned unchanged (already uppercase).
_CRYPTO_ALIASES = {
    "BTCUSD": "BTC/USD",
    "BTC": "BTC/USD",
    "ETHUSD": "ETH/USD",
    "ETH": "ETH/USD",
}


def canonical_symbol(ticker: str) -> str:
    return _CRYPTO_ALIASES.get(ticker.upper().strip(), ticker)


def check(payload, account=None, positions=None) -> dict | None:
    """Validate payload against risk rules. Raises RiskError to block.
    Returns the contingency dict that permitted this buy (see module
    docstring), if one was needed and matched — else None. The return value
    carries no obligation for the caller; order_manager.handle() uses it ONLY
    to journal the override for auditability. It does not resize the entry: a
    contingency permits a trade, it does not take a smaller one, because this
    book is all-in/all-out (operator directive 2026-08-27).

    `positions` (the currently-open Alpaca positions, each exposing `.symbol`)
    is accepted for API compatibility but is NOT used here — the
    "one position at a time" rule is now enforced as signal-driven rotation in
    order_manager.handle(): a buy in a new symbol while holding another rotates
    (close old, buy new) only if the new symbol's `priority` is higher; a
    same-symbol buy (top-up) just submits. This gate still blocks a buy on any
    symbol whose `bias` is not `long`, unless a contingency permits it.
    """
    d = _load_decisions()
    allow_ext = bool(d.get("allow_extended_hours", False))

    # 0. Dedup identical alerts within the window. TRANSIENT by definition —
    #    it is a 60-second window, not a judgement about the trade.
    if _is_dup(payload.ticker, payload.action):
        raise RiskError(f"duplicate {payload.ticker} {payload.action} within {DEDUP_WINDOW_SEC}s",
                        transient=True)

    # 1. Allowlist (only enforced if decisions.json declares a watchlist).
    #
    #    ENTRIES ONLY. The allowlist is a permission to take NEW risk in a
    #    symbol, not a freeze on a position already held — the same argument
    #    step 5 makes for the daily loss limit, and step 2 for bias.
    #
    #    Applying it to exits was a live trap (audit 2026-08-27, EXIT-3): the
    #    research prompt tells the model the watchlist is "yours to maintain"
    #    and that it may drop a symbol, and agent/rotate.py documents the
    #    intended consequence as "it is NOT force-closed here — it stays held
    #    until its own chart fires an exit signal". That recovery was
    #    impossible: when the chart fired, this check raised before the order
    #    was ever built, so the position could not be exited by the engine at
    #    all — and _reconcile_positions' fallback close goes through the same
    #    handle(), so it was blocked identically, journaling
    #    reconcile_close_failed every poll forever. Only a human could flatten
    #    it. Reproduced with a fake client holding 30 SOXL: on the watchlist
    #    the close submits; off it, RiskError and zero broker calls.
    watchlist = d.get("watchlist")
    if watchlist and payload.action == "buy" and payload.ticker not in watchlist:
        raise RiskError(f"{payload.ticker} not in Claude allowlist {watchlist}")

    # 2. Bias (only enforced if declared). For a long-only EMA-cross strategy:
    #    - long  -> allow buys (new entries) and sells/closes (exits)
    #    - flat  -> block buys (no new entries); allow exits
    #    - short -> block buys; allow exits
    #    A bias of None (symbol not in bias map) means undeclared -> allow.
    #
    #    Crypto is EXEMPT from this gate entirely (2026-07-03): bias is set by
    #    the Claude research cron, which only runs Mon-Fri (see crontab) — so a
    #    weekend BTC/USD buy signal would otherwise be blocked by Friday's
    #    stale bias with no way to refresh it until Monday. The EMA-cross entry
    #    signal itself already only fires on a fresh bullish cross (i.e. "we're
    #    already in an uptrend"), so trusting it alone for crypto isn't a
    #    weaker gate, just a mechanical one instead of an LLM-curated one — a
    #    reasonable trade for an asset class the research layer treats as a
    #    single mechanical candidate anyway, not a fundamentals-driven pick
    #    among many like the equity watchlist. Rotation `priority` (a SEPARATE
    #    mechanism — which asset wins the single position slot) still applies
    #    normally, so crypto can't force out a held equity on a stale priority.
    bias = d.get("bias", {}).get(payload.ticker)
    contingency = None
    if (bias is not None and payload.action == "buy" and bias != "long"
            and not is_crypto_symbol(payload.ticker)):
        rules = d.get("contingencies", {}).get(payload.ticker, [])
        contingency = _active_contingency(rules, payload.price)
        if contingency is None:
            raise RiskError(f"{payload.ticker} bias={bias}, blocking new buy entry")

    # 3. Market hours (crypto trades 24/7 — exempt crypto pairs).
    #
    #    `allow_extended_hours` is a permission to OPEN risk out of hours, so
    #    it binds entries only. An exit is always allowed into any session the
    #    broker will actually accept an order for — i.e. it is judged against
    #    the full 24/5 window regardless of the flag (audit 2026-08-27,
    #    EXIT-4). The flag is written by the research model and defaults to
    #    False whenever it simply omits the key, so under the old rule one
    #    missing field silenced every out-of-hours exit — including a stop —
    #    while leaving the position open. An exit is still blocked over the
    #    weekend, when there is no session to submit into at all; that is a
    #    fact about the market, not a permission.
    #
    #    Routing is unaffected: order_manager still sends a plain market order
    #    inside 9:30-16:00 ET and a marketable extended-hours LIMIT outside it
    #    (see regular_market_hours_open).
    if not is_crypto_symbol(payload.ticker):
        exiting = payload.action in ("sell", "close")
        if not _market_open(True if exiting else allow_ext):
            raise RiskError("outside market hours")

    # 4. Position size ceiling
    max_pos = d.get("max_position", {}).get(payload.ticker, HARD_MAX_POSITION)
    max_pos = min(max_pos, HARD_MAX_POSITION)
    if payload.qty and payload.qty > max_pos:
        raise RiskError(f"qty {payload.qty} exceeds max_position {max_pos}")

    # (The "one position at a time" rule used to live here as a max_open_positions
    # block. It is now signal-driven rotation in order_manager.handle() — see
    # decisions_for_rotation() — so a higher-priority candidate's buy signal can
    # rotate out of a lower-priority holding instead of being hard-blocked.)

    # 5. Daily loss limit (needs account equity + unrealized PnL).
    #    ENTRIES ONLY — this is a circuit breaker that stops opening NEW risk on
    #    a bad day, NOT a position freeze. Exits (sell/close) must always be
    #    allowed through, otherwise once the account is down >= the limit we'd
    #    block the very stop-loss / technical exit that is trying to cut the
    #    loss, trapping the position exactly when we most want out. (This bit
    #    matters at current sizing: a single ~97%-of-equity position hitting its
    #    2% stop is ~1.9% account drawdown, right at the 2% halt threshold.)
    if account is not None and payload.action == "buy":
        daily_limit = min(float(d.get("daily_loss_limit", HARD_DAILY_LOSS_LIMIT)), HARD_DAILY_LOSS_LIMIT)
        try:
            equity = float(account.equity)
            last_equity = float(getattr(account, "last_equity", equity))
            if last_equity > 0 and (equity / last_equity - 1.0) <= -daily_limit:
                raise RiskError(f"daily loss limit {daily_limit:.1%} reached — halt")
        except (ValueError, TypeError):
            pass

    return contingency
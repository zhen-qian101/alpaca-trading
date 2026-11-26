"""Order lifecycle: risk-check -> submit -> journal result.

Single entry point used by the signal engine (`sigengine/engine.py`), which
constructs an `OrderRequest` per fresh strategy event and calls `handle()`
IN-PROCESS. There is no HTTP layer anymore — the relay is a pure library. The
journal captures every decision regardless of outcome.
"""
from __future__ import annotations

import time

from .alpaca_client import AlpacaClient, is_definitive_rejection
from .config import JOURNAL_FILE
from .logger import append_event
from .models import OrderRequest
from .risk_filter import (
    RiskError,
    _is_equity_position,
    is_option_symbol,
    canonical_symbol,
    check,
    decisions_for_rotation,
    is_crypto_symbol,
    regular_market_hours_open,
)

# Marketable limit price buffer for extended-hours/overnight orders — pushes
# the limit through the current quote (ask for buys, bid for sells) by this
# fraction, so the order behaves like a "near-market" fill despite the wider
# spreads typical outside regular hours, while still protecting against a
# stale/erroneous quote (unlike a real market order, this can't fill
# arbitrarily far from the last quote). Added 2026-07-06 alongside genuine
# extended-hours/overnight order support — see handle()'s extended-hours
# branch and _extended_hours_limit_price.
EXTENDED_HOURS_LIMIT_BUFFER = 0.005  # 0.5%

# How far the quote side used for extended-hours pricing may sit from the last
# traded price before it is treated as unusable and the last trade is used
# instead.
#
# Why this exists: EXTENDED_HOURS_LIMIT_BUFFER above assumes a TIGHT two-sided
# quote. Outside regular hours the default IEX feed often breaks that
# assumption, in two ways seen live on 2026-07-15 pre-market:
#   - one-sided: META bid=632.49 ask=0.00 (last trade 661.04). Pricing a buy off
#     that ask produced limit=$0.00 and Alpaca rejected the order outright
#     (code 40010001, "limit price must be > 0") — the failure that prompted
#     this guard. Loud, but the signal was lost.
#   - stale/wide: NVDA bid=211.16 ask=230.00 against a 211.86 last trade. That
#     ask is ~9% high, passes any naive "> 0" check, and WOULD fill — silently
#     overpaying. Strictly worse than the rejection above.
# 2% is comfortably wider than a normal extended-hours spread on these names but
# well inside the junk above. See _reference_price.
MAX_QUOTE_DEVIATION = 0.02  # 2%

# The deviation guard above has an inverse failure mode, seen live 2026-08-03:
# after a weekend gap the "last trade" was Friday's close (AMZN 271.465) while
# the genuine Monday-overnight quote sat 4.6% higher (283.97). The guard read
# that as a junk quote, priced the buy off the stale trade, and the limit
# rested ~4.4% below the market — never filling, leaving the book flat while
# the engine believed it was long (its reconcile pass deliberately never
# chases missed entries). A large deviation therefore forces a judgment call:
# WHICH side is stale? The quote wins only when all three hold —
#   - it is two-sided and internally tight (junk quotes are one-sided, crossed,
#     or several-percent wide; a real overnight book is not),
#   - its own timestamp is fresh,
#   - the last trade's timestamp is at least a session-gap old (a fresh trade
#     disagreeing with the quote is the original junk-quote case — fall back,
#     exactly as before).
# Timestamps missing (older client/fake) → conservative: fall back to the
# last trade, the pre-2026-08 behavior.
STALE_TRADE_MAX_QUOTE_SPREAD = 0.01   # two-sided quote "tight" if spread/mid <= 1%
STALE_TRADE_QUOTE_MAX_AGE_SEC = 300.0    # quote must be < 5 min old to overrule
STALE_TRADE_MIN_TRADE_AGE_SEC = 1800.0   # trade older than 30 min = session-gap stale

# THE DEVIATION FALLBACK IS DIRECTIONAL, because the two sides of a trade have
# opposite worst cases (audit 2026-08-27, EXIT-2).
#
# For a BUY, falling back to the last trade is conservative: it yields a limit
# BELOW a spuriously-high ask, so the order rests unfilled rather than
# overpaying. _reference_price's docstring reasons exactly that way — "an
# unfilled order is a recoverable outcome; a 9%-through fill is not".
#
# For a SELL that reasoning inverts, and the old code applied it anyway. The
# bid outside regular hours is routinely 4-6% under a stale last trade, so the
# guard rejected the bid, returned the trade, and the resulting sell limit —
# even after the 0.5% buffer — sat several percent ABOVE the only bid in the
# book. It could not fill. Measured on the live journal: 470 quote_fallback
# rows with side="sell" against 9 with side="buy", and collapsing the sell rows
# to distinct pricing situations, the implied limit was above the prevailing
# bid in 6 of 6 — TSLA 2026-08-18 bid 323.28 / last 339.31 -> limit 337.61,
# +4.4% above the bid, which then rested unfilled for 10.5 hours pinning the
# whole position while every re-close was rejected 40310000.
#
# For an exit, UNFILLED IS THE DANGEROUS OUTCOME: the position stays on with no
# protection, and that loss is unbounded. Crossing the spread costs a known,
# bounded amount. So an exit prices DOWN toward the bid — but not to an
# arbitrary bid, because a junk one-sided or stale-low quote would otherwise
# sell the position for nothing. The markdown is floored: at most this far
# below the last trade, whatever the bid says.
#
# 8% is wider than the widest legitimate overnight bid discount observed here
# (6.4%) and wider than every configured stop (2%, or 6% on SOXL/TQQQ), so it
# never binds on a real book; it exists purely to stop a garbage bid from
# pricing an exit at a fraction of its value.
EXIT_MAX_MARKDOWN = 0.08  # 8% below the last trade, floor for a sell reference

# ...but only when the quote is NOT a credible two-sided book. A bid that is
# merely far from a stale last trade, inside a sanely-tight spread, is the
# market talking — that is a gap-down open, precisely when an exit must fill —
# and flooring it would re-create the unfillable limit. 4% is deliberately much
# wider than STALE_TRADE_MAX_QUOTE_SPREAD (which gates the far riskier act of
# overruling the guard for a BUY): here a wide-but-two-sided book still beats
# not exiting at all.
EXIT_TRUSTED_SPREAD = 0.04

# Journal an entry whose fill price diverges from the price its stop is anchored
# to by more than this. Half of the tightest configured stop (2%), so anything
# that meaningfully changes the real stop distance shows up. See
# OrderManager._report_stop_anchor_drift.
STOP_ANCHOR_DRIFT_REPORT = 0.01

# Bounded retry for the broker submit itself (see _submit_with_retry).
ORDER_SUBMIT_ATTEMPTS = 3
ORDER_RETRY_BACKOFF_SEC = 2.0  # multiplied by the attempt number


class OrderManager:
    def __init__(self, client: AlpacaClient):
        self.client = client
        # SYMBOLS THE ROTATION MUST NOT TOUCH. The put sleeve's assigned shares
        # sit in the same account as the share book, and "one position at a
        # time" would otherwise rotate out of them the moment any other name
        # signalled — selling the sleeve's stock because NVDA crossed a moving
        # average. The engine sets this from its ownership sidecar
        # (aux["adopted"]) before every handle(); a fresh OrderManager protects
        # nothing, so the sleeve's own runner can still close its shares.
        self.protected_symbols: set[str] = set()

    @staticmethod
    def _held_symbol_other_than(positions, ticker: str, ignore=()) -> str | None:
        """Return the EQUITY symbol currently held that is NOT `ticker`, else None.

        A buy that is a top-up of the already-held symbol (same ticker) returns
        None so it just submits. The "one position at a time" rule means there
        is at most one other symbol; if several somehow exist, the first is
        returned (the relay still closes only one per rotation — a follow-up
        signal cleans up the rest).

        Option positions are invisible here on purpose — see _is_equity_position.
        An open short put is a commitment of CASH, not a rotatable position, and
        the collateral it pledges is accounted for separately (see
        relay.csp_manager.pledged_collateral and the sizing in
        sigengine.engine.fire_event).
        """
        target = ticker.upper()
        skip = {str(s).upper() for s in (ignore or ())}
        for p in positions or []:
            if not _is_equity_position(p):
                continue
            sym = str(getattr(p, "symbol", "")).upper()
            if sym and sym != target and sym not in skip:
                return sym
        return None

    @staticmethod
    def _position_qty_and_side(positions, ticker: str) -> tuple[float, str] | None:
        """Find `ticker`'s currently held qty + the SIDE that closes it (a
        long position closes via "sell", a short via "buy"). Returns None if
        not held. Used by the extended-hours close path (see handle()) since
        Alpaca's close_position() endpoint always submits a market order
        internally with no way to request a limit price — outside regular
        hours that would just get rejected, so a manual limit_order() is
        submitted instead, and that needs the exact qty + direction."""
        target = ticker.upper()
        for p in positions or []:
            if not _is_equity_position(p):
                continue
            if str(getattr(p, "symbol", "")).upper() == target:
                qty = float(p.qty)
                return abs(qty), ("sell" if qty > 0 else "buy")
        return None

    def _open_order_symbols(self) -> set[str]:
        """Upper-cased symbols that currently have a WORKING order at the broker.

        The one-position-at-a-time invariant is about the account's
        commitments, and a resting order commits buying power just as much as a
        filled one does — it simply has not become a `position` yet. Outside
        regular hours everything this book sends is a limit that can rest for
        hours, so treating "no position" as "nothing committed" is wrong most of
        the day.

        Best-effort: on a fetch failure this returns an EMPTY set rather than
        raising, because the caller has already fail-closed on the positions
        fetch — this check can only ever add blocks, never remove them, so
        degrading it must not turn a working entry path into a dead one.
        """
        try:
            orders = self.client.get_open_orders()
        except Exception as e:  # pragma: no cover - network
            append_event(JOURNAL_FILE, {
                "event": "open_orders_unavailable",
                "reason": f"could not read working orders: {e}",
            })
            return set()
        out = set()
        for o in orders or []:
            sym = str(getattr(o, "symbol", "") or "").upper()
            if not sym:
                continue
            ac = getattr(o, "asset_class", None)
            if ac is not None and str(getattr(ac, "value", ac)).lower() == "us_option":
                continue        # an option order commits cash, not a slot
            if is_option_symbol(sym):
                continue
            out.add(canonical_symbol(sym).upper())
        return out

    def _submit_with_retry(self, ticker: str, describe: str, submit) -> None:
        """Call `submit()` — a zero-arg closure that PRICES AND submits an
        order — retrying only definitive broker rejections.

        Why the retry lives here rather than in the signal engine: the risk gate
        (relay.risk_filter.check) stamps a 60s dedup key on its FIRST call, so a
        caller that retried handle() wholesale would have its own second attempt
        blocked as a "duplicate". By this point the gate and rotation have
        already run exactly once; only the broker round-trip repeats.

        Why the closure prices INSIDE the loop: the failure this guards against
        is a transiently unusable extended-hours quote, so re-pricing from a
        fresh quote is precisely what makes a later attempt more likely to
        succeed. Retrying the same stale number would be pointless.

        Only is_definitive_rejection() errors are retried — an HTTP 4xx means
        the broker created nothing, so a re-submit cannot double-fill. Timeouts
        and 5xx are ambiguous (the order may have landed and filled) and
        propagate immediately; without an idempotency key, retrying those could
        open a second position. A RiskError from pricing is not an APIError and
        so also propagates at once.
        """
        for attempt in range(1, ORDER_SUBMIT_ATTEMPTS + 1):
            try:
                submit()
                return
            except Exception as e:
                if attempt == ORDER_SUBMIT_ATTEMPTS or not is_definitive_rejection(e):
                    raise
                append_event(JOURNAL_FILE, {
                    "event": "order_retry", "ticker": ticker, "order": describe,
                    "attempt": attempt, "of": ORDER_SUBMIT_ATTEMPTS, "reason": str(e),
                })
                time.sleep(ORDER_RETRY_BACKOFF_SEC * attempt)

    def _guard_equity_only(self, ticker: str, what: str) -> None:
        """Refuse to treat an OCC option symbol as a stock.

        A LOUD failure beats the two silent ones this replaces: at regular
        hours a raw close_position(<OCC>) buys the option back at market, and
        outside them _reference_price asks the STOCK quote endpoint for an
        option symbol and raises something that reads like a data outage. If an
        OCC symbol reaches here the caller has a bug; say which caller.
        """
        if is_option_symbol(ticker):
            raise RiskError(
                f"{what} was handed the option symbol {ticker}. The relay is "
                f"equity-only — options go through relay.csp_manager, never "
                f"through OrderManager.handle()."
            )

    def _close_position(self, ticker: str, positions) -> str:
        """Close `ticker`'s position, routing through a manual limit order
        with extended_hours=True when outside regular market hours — Alpaca's
        close_position() endpoint always submits a MARKET order internally
        (no way to request a limit price), which gets rejected outside the
        regular 9:30-16:00 ET session. Used by both the main "close" action
        and signal-driven rotation closes (see handle()). Returns a short
        status label for the journal: "closed" (crypto/regular-hours, or
        nothing was actually held) or "closed_limit_extended"."""
        self._guard_equity_only(ticker, "_close_position")
        if not is_crypto_symbol(ticker) and not regular_market_hours_open():
            # `positions` may be None because handle()'s own fetch FAILED, not
            # because the book is flat. Passing that through made
            # _position_qty_and_side iterate `positions or []`, find nothing,
            # and return "closed" WITHOUT SUBMITTING ANYTHING — journaled as a
            # successful `order_ok` exit. A stop that fires during a transient
            # Alpaca 500 would be recorded as filled while the position rode on
            # untouched. Re-read before believing the book is flat.
            if positions is None:
                try:
                    positions = self.client.get_all_positions()
                except Exception as e:  # pragma: no cover - network
                    raise RiskError(
                        f"cannot close {ticker}: position state unknown "
                        f"(positions fetch failed: {e}) — refusing to report an "
                        f"exit that was never submitted"
                    ) from e
            pos = self._position_qty_and_side(positions, ticker)
            if pos is None:
                return "closed"  # genuinely flat — matches close_position() being a no-op-ish call
            qty, side = pos
            self._submit_with_retry(
                ticker, "close_limit_extended",
                lambda: self.client.limit_order(
                    ticker, qty, side,
                    self._extended_hours_limit_price(ticker, side),
                    "day", extended_hours=True,
                ),
            )
            return "closed_limit_extended"
        self._submit_with_retry(ticker, "close", lambda: self.client.close_position(ticker))
        return "closed"

    def _reference_price(self, ticker: str, side: str) -> float:
        """The price to base an extended-hours limit on: the relevant quote
        side (ask for a buy, bid for a sell) when it is trustworthy, else the
        last traded price.

        The quote side is preferred when usable — it is the more current view
        of where the order can actually fill — but only if it is positive AND
        within MAX_QUOTE_DEVIATION of the last trade, because outside regular
        hours the IEX feed serves one-sided (ask=0) and stale, very wide quotes
        that are worse than useless for pricing (see MAX_QUOTE_DEVIATION).

        Falling back to the last trade is deliberately conservative FOR A BUY:
        it yields a limit BELOW a spuriously-high ask, so the order rests
        unfilled rather than overpaying. An unfilled order is a recoverable
        outcome; a 9%-through fill is not.

        FOR A SELL that trade-off inverts and the fallback goes the other way —
        toward the bid, floored at EXIT_MAX_MARKDOWN below the last trade. An
        exit that cannot fill leaves an unprotected position, which is the
        unbounded outcome; crossing the spread is the bounded one. See
        EXIT_MAX_MARKDOWN for the measured reason.

        One exception (2026-08-03 AMZN weekend gap — see the STALE_TRADE_*
        constants): when the quote is fresh, two-sided and tight while the
        last trade is a session-gap-old print, the TRADE is the stale side and
        the quote is used despite the deviation.

        Raises RiskError if neither source is usable — better to block and
        journal the signal than to send a garbage price to the broker.
        """
        self._guard_equity_only(ticker, "_reference_price")
        try:
            bid, ask, quote_ts = self.client.get_latest_quote(ticker)
        except Exception as e:  # pragma: no cover - network
            bid, ask, quote_ts, quote_err = 0.0, 0.0, None, str(e)
        else:
            quote_err = None
        quote_px = ask if side == "buy" else bid

        try:
            last, trade_ts = self.client.get_latest_trade(ticker)
            last = float(last)
        except Exception as e:  # pragma: no cover - network
            last, trade_ts, trade_err = 0.0, None, str(e)
        else:
            trade_err = None

        if quote_px > 0 and last > 0:
            deviation = abs(quote_px - last) / last
            if deviation <= MAX_QUOTE_DEVIATION:
                return quote_px
            # Large disagreement: decide which side is the stale one (see the
            # STALE_TRADE_* constants). A fresh, tight, two-sided quote beats a
            # session-gap-old last trade; anything less falls back to the last
            # trade exactly as before.
            now = time.time()
            quote_age = None if quote_ts is None else now - quote_ts
            trade_age = None if trade_ts is None else now - trade_ts
            quote_tight = (
                bid > 0 and ask >= bid
                and (ask - bid) / ((ask + bid) / 2) <= STALE_TRADE_MAX_QUOTE_SPREAD
            )
            if (quote_tight
                    and quote_age is not None and quote_age <= STALE_TRADE_QUOTE_MAX_AGE_SEC
                    and trade_age is not None and trade_age >= STALE_TRADE_MIN_TRADE_AGE_SEC):
                append_event(JOURNAL_FILE, {
                    "event": "quote_override_stale_trade", "ticker": ticker, "side": side,
                    "reason": "last trade is a session-gap-old print; pricing off the "
                              "fresh two-sided quote instead",
                    "quote_price": quote_px, "last_trade": last,
                    "deviation": round(deviation, 4),
                    "quote_age_sec": round(quote_age, 1), "trade_age_sec": round(trade_age, 1),
                })
                return quote_px
            # An EXIT prices toward the bid instead of up to the stale trade —
            # see EXIT_MAX_MARKDOWN. Only ever moves the reference DOWN (a bid
            # above the last trade leaves min() picking the trade, i.e. the old
            # behaviour), and never below the markdown floor.
            if side == "sell" and quote_px > 0:
                # TRUST A REAL BOOK; FLOOR A BROKEN ONE. The floor exists to stop
                # a junk quote pricing the exit at a fraction of its value — and
                # junk here is recognisable: it is one-sided, crossed, or
                # absurdly wide (see MAX_QUOTE_DEVIATION's own examples). A
                # two-sided, sanely-tight book that simply sits well below a
                # stale last trade is not junk, it is the market, and that is
                # exactly the gap-down morning when the exit most needs to fill.
                # Flooring there would re-create the unfillable limit this whole
                # change removes, so the floor applies only when the quote fails
                # the shape test.
                real_book = (bid > 0 and ask > bid
                             and (ask - bid) / ((ask + bid) / 2) <= EXIT_TRUSTED_SPREAD)
                ref = min(quote_px, last)
                if not real_book:
                    ref = max(ref, last * (1 - EXIT_MAX_MARKDOWN))
                append_event(JOURNAL_FILE, {
                    "event": "quote_fallback", "ticker": ticker, "side": side,
                    "reason": "quote outside sanity band, but this is an EXIT — "
                              "pricing toward the bid so it can actually fill, "
                              "floored at the max markdown",
                    "quote_price": quote_px, "last_trade": last, "reference": round(ref, 4),
                    "two_sided_book": bool(real_book),
                    "floored": bool(ref > min(quote_px, last)),
                    "deviation": round(deviation, 4), "max_deviation": MAX_QUOTE_DEVIATION,
                    "max_markdown": EXIT_MAX_MARKDOWN,
                    "quote_age_sec": None if quote_age is None else round(quote_age, 1),
                    "trade_age_sec": None if trade_age is None else round(trade_age, 1),
                })
                return ref
            append_event(JOURNAL_FILE, {
                "event": "quote_fallback", "ticker": ticker, "side": side,
                "reason": "quote outside sanity band; pricing off last trade",
                "quote_price": quote_px, "last_trade": last,
                "deviation": round(deviation, 4), "max_deviation": MAX_QUOTE_DEVIATION,
                "quote_age_sec": None if quote_age is None else round(quote_age, 1),
                "trade_age_sec": None if trade_age is None else round(trade_age, 1),
            })
            return last

        if last > 0:
            append_event(JOURNAL_FILE, {
                "event": "quote_fallback", "ticker": ticker, "side": side,
                "reason": "no usable quote; pricing off last trade",
                "bid": bid, "ask": ask, "last_trade": last, "quote_error": quote_err,
            })
            return last

        if quote_px > 0:
            return quote_px

        raise RiskError(
            f"cannot price {side} {ticker}: no usable quote (bid={bid}, ask={ask}"
            f"{f', err={quote_err}' if quote_err else ''}) and no usable last trade"
            f"{f' (err={trade_err})' if trade_err else ''}"
        )

    def _extended_hours_limit_price(self, ticker: str, side: str) -> float:
        """A marketable limit price for an extended-hours/overnight order:
        a trustworthy reference price (see _reference_price) pushed slightly
        through by EXTENDED_HOURS_LIMIT_BUFFER so it behaves like a
        near-market fill despite wider extended-hours spreads, while a real
        limit (unlike a market order) still can't fill arbitrarily far from
        the reference."""
        ref = self._reference_price(ticker, side)
        if side == "buy":
            return round(ref * (1 + EXTENDED_HOURS_LIMIT_BUFFER), 2)
        return round(ref * (1 - EXTENDED_HOURS_LIMIT_BUFFER), 2)

    @staticmethod
    def _report_stop_anchor_drift(payload: OrderRequest, limit_px: float) -> None:
        """Journal how far the price we are about to pay sits from the price the
        STOP will be measured against. Observability only — nothing is blocked.

        Both live strategies anchor their stop to a SIMULATED entry (the
        previous bar's close, used as a next-bar-open proxy) and evaluate
        `close <= entry_price * (1 - stop_pct)` against that number, while the
        real fill is whatever this marketable limit gets. Nothing reconciles
        the two, so the effective stop on the real position is stop_pct
        measured from a price the account never paid (audit 2026-08-27, E6).
        On 2026-08-19 that gap was ~2.4%: the signal priced MSFT off a junk bar
        close of 474.47 while the fill was ~485.75, leaving the "2% stop" some
        4.3% below the actual cost basis.

        Repairing it properly means feeding the real average fill back into the
        stop, which the strategies cannot express today — they are pure
        functions of the bar series. Until then the divergence is at least
        VISIBLE at the moment it is created, which is what makes it auditable.
        """
        signal_px = getattr(payload, "price", None)
        if payload.action != "buy" or not signal_px or signal_px <= 0 or limit_px <= 0:
            return
        drift = (limit_px - signal_px) / signal_px
        if abs(drift) < STOP_ANCHOR_DRIFT_REPORT:
            return
        append_event(JOURNAL_FILE, {
            "event": "stop_anchor_drift", "ticker": payload.ticker,
            "signal_price": round(float(signal_px), 4),
            "limit_price": round(float(limit_px), 4),
            "drift": round(float(drift), 4),
            "reason": "the stop is measured from the strategy's simulated entry, "
                      "not from this fill — the real stop distance differs from "
                      "the configured one by roughly this much",
        })

    @staticmethod
    def _should_rotate(held: str, new: str) -> tuple[bool, str]:
        """Decide whether a buy of `new` should rotate out of `held`.

        Rotate only if `new` has STRICTLY higher priority than `held`. A held
        symbol whose bias has flipped to flat (no longer a candidate) has
        priority 0, so any long candidate (priority > 0) rotates in. Equal or
        lower priority → block (keep holding, avoid churn).
        """
        pri, _bias = decisions_for_rotation()
        px = int(pri.get(new.upper(), 0))
        py = int(pri.get(held.upper(), 0))
        if px > py:
            return True, f"rotate {held}(pri={py}) -> {new}(pri={px})"
        return False, f"holding {held} (pri={py}) >= {new} (pri={px}); not rotating"

    def handle(self, payload: OrderRequest) -> dict:
        # Canonicalize once at the library boundary so direct callers (the signal
        # engine, tests) don't have to remember: BTCUSD -> BTC/USD, equities unchanged.
        payload.ticker = canonical_symbol(payload.ticker)
        result: dict = {"ticker": payload.ticker, "action": payload.action, "qty": payload.qty}

        try:
            account = self.client.get_account()
        except Exception as e:  # pragma: no cover - network
            account = None
            result["warn"] = f"account fetch failed: {e}"

        # Open positions feed the signal-driven rotation logic below.
        try:
            positions = self.client.get_all_positions()
        except Exception as e:  # pragma: no cover - network
            positions = None
            result["warn"] = (result.get("warn", "") + f" positions fetch failed: {e}").strip()

        # Risk gate (may raise RiskError -> blocked, journaled, 403)
        try:
            contingency = check(payload, account, positions)
        except RiskError as e:
            result.update(status="blocked", reason=str(e))
            append_event(JOURNAL_FILE, {"event": "risk_block", **result})
            raise

        # A contingency permitted a buy that bias alone would have blocked (see
        # relay.risk_filter's module docstring). Journal the override distinctly
        # so a pre-approved-breakout entry is never silently indistinguishable
        # from a normal long-bias entry in the audit trail.
        #
        # IT DOES NOT RESIZE THE ENTRY. A contingency used to carry an optional
        # `size_cap` and this block multiplied the engine's qty by it, which is
        # a different power from the one a contingency is for: permitting a
        # trade is not the same as taking a smaller one. This book is
        # all-in/all-out (operator directive 2026-08-27), so a permitted entry
        # is a full entry. The key is stripped upstream in
        # agent.decisions_schema too, so a model that keeps emitting one cannot
        # half-size a live trade — as the AAPL contingency standing at
        # size_cap 0.35 would have done on a break of $315.50.
        if contingency is not None:
            append_event(JOURNAL_FILE, {
                "event": "contingency_override", "ticker": payload.ticker,
                "condition": contingency.get("condition"), "level": contingency.get("level"),
                "price": payload.price, "qty": payload.qty,
                "reason": contingency.get("reason", ""),
            })

        # Signal-driven rotation (one position at a time, but rotated on EMA
        # signals, not on research timing). Only relevant for a BUY in a symbol
        # other than the one we already hold. See decisions_for_rotation().
        if payload.action == "buy":
            # FAIL CLOSED. This block is the ONLY thing enforcing "one position
            # at a time" — risk_filter.check() takes positions purely for API
            # compatibility and deliberately does not use them. Gating it on
            # `positions is not None` meant a failed fetch SKIPPED the check
            # entirely and let a second full-size position through, which is the
            # opposite of what an unknown account state should cause.
            if positions is None:
                reason = ("position state unknown (positions fetch failed) — "
                          "refusing to open while the one-position-at-a-time "
                          "invariant cannot be checked")
                result.update(status="blocked", reason=reason)
                append_event(JOURNAL_FILE, {"event": "risk_block", **result})
                # TRANSIENT: a failed positions fetch is a network blip, not a
                # decision about this trade — the next poll may well succeed.
                raise RiskError(reason, transient=True)

            # A submitted-but-unfilled order creates NO position, so a check
            # that reads positions alone is blind to it. Outside regular hours
            # every entry and exit is a resting limit that routinely sits
            # unfilled for hours (TSLA sat 10.5h on 2026-08-18), so this is the
            # normal case, not an edge one: two buys an hour apart both saw an
            # empty book and both went full size. Working orders count as
            # commitments of the account.
            # The sleeve's shares (and any order working on them — its own
            # weekly sale) are not the share book's business: neither a
            # position to rotate out of nor a commitment that blocks an entry.
            working = self._open_order_symbols() - {s.upper() for s in self.protected_symbols}
            held_other = self._held_symbol_other_than(positions, payload.ticker,
                                                      ignore=self.protected_symbols)

            # A symbol whose only claim on the account is a WORKING ORDER cannot
            # be rotated out of: _close_position finds no position and returns
            # "closed" without sending anything, leaving the original order
            # resting AND the new entry submitted on top — precisely the
            # stacking this guard exists to stop. Block instead. The resting
            # order is a DAY order, so it resolves (fills or expires) by the end
            # of the session rather than blocking the book indefinitely.
            if held_other is None:
                pending_other = next((sym for sym in sorted(working)
                                      if sym != payload.ticker.upper()), None)
                if pending_other is not None:
                    reason = (f"an order for {pending_other} is still working — "
                              f"refusing to open {payload.ticker.upper()} alongside "
                              f"it (it holds buying power but is not yet a "
                              f"position, so it cannot be rotated out of)")
                    result.update(status="blocked", reason=reason)
                    append_event(JOURNAL_FILE, {"event": "risk_block", **result})
                    # TRANSIENT: the blocking order is a DAY order, so it fills
                    # or expires within the session.
                    raise RiskError(reason, transient=True)

            if payload.ticker.upper() in working:
                reason = (f"an order for {payload.ticker.upper()} is already working "
                          f"— not stacking another entry on top of it")
                result.update(status="blocked", reason=reason)
                append_event(JOURNAL_FILE, {"event": "risk_block", **result})
                # TRANSIENT: same reasoning — our own resting order resolves.
                raise RiskError(reason, transient=True)

            if held_other is not None:
                rotate_ok, reason = self._should_rotate(held_other, payload.ticker)
                # ITS EXIT MAY ALREADY BE IN FLIGHT. The rotation branch below
                # SUBMITS a close, and the "resting unfilled" outcome is marked
                # transient so the engine's pending-entry retry re-attempts the
                # buy next poll (audit E2). Without this check that retry would
                # re-enter the branch and send ANOTHER close every poll, each one
                # rejected 40310000 because the first is pinning the shares —
                # rebuilding the exact 115-rejection loop reconcile._handle_pinned_exit
                # was written to stop. A working order for the held symbol means
                # the rotation is already under way: wait for it, do not re-issue it.
                if rotate_ok and held_other.upper() in working:
                    reason = (f"rotation exit for {held_other} is already working — "
                              f"waiting for it to complete before opening "
                              f"{payload.ticker.upper()} (not re-submitting the close)")
                    result.update(status="blocked", reason=reason)
                    append_event(JOURNAL_FILE, {"event": "risk_block", **result})
                    raise RiskError(reason, transient=True)
                if rotate_ok:
                    try:
                        closed_status = self._close_position(held_other, positions)
                        append_event(JOURNAL_FILE, {
                            "event": "rotation_close", "ticker": held_other,
                            "new_pick": payload.ticker.upper(), "reason": reason,
                            "status": closed_status,
                        })
                    except Exception as e:  # broker-side rejection on the close
                        result.update(status="error", reason=f"rotation close failed: {e}")
                        append_event(JOURNAL_FILE, {"event": "order_error", **result})
                        raise
                    if closed_status == "closed_limit_extended":
                        # The exit is a RESTING limit, not a completed one — it
                        # may not fill for hours, or at all. Falling straight
                        # through to the entry (as this used to) buys the new
                        # name while still holding the old one, on margin, with
                        # BOTH stops anchored to prices neither position was
                        # opened at. Let the exit finish first; the buy re-fires
                        # from the next poll's signal once the book is flat.
                        reason = (f"rotation exit for {held_other} is resting unfilled "
                                  f"(extended hours) — deferring the {payload.ticker.upper()} "
                                  f"entry until it completes")
                        result.update(status="blocked", reason=reason)
                        append_event(JOURNAL_FILE, {"event": "risk_block", **result})
                        # TRANSIENT, and the comment above already says so in
                        # words: "the buy re-fires from the next poll's signal
                        # once the book is flat". Until the engine grew a
                        # pending-entry record that promise was not kept — the
                        # state cursor had already advanced past the signal
                        # (audit 2026-08-27, E2).
                        raise RiskError(reason, transient=True)
                else:
                    # Keep holding the higher-priority name; block this buy.
                    result.update(status="blocked", reason=reason)
                    append_event(JOURNAL_FILE, {"event": "risk_block", **result})
                    raise RiskError(reason)

        # Alpaca requires LIMIT orders (not market) + extended_hours=True
        # outside the regular 9:30-16:00 ET session (pre-market/after-hours/
        # true overnight) — crypto is exempt, it trades 24/7 with no such
        # restriction. See relay.risk_filter.regular_market_hours_open and
        # _extended_hours_limit_price.
        needs_extended = not is_crypto_symbol(payload.ticker) and not regular_market_hours_open()

        # CRYPTO CANNOT USE `day`. OrderRequest.tif defaults to "day" (see
        # relay/models.py) because that is right for equities, and the branch
        # above deliberately never routes crypto through the extended-hours
        # path — so before this line every crypto buy fell into the plain
        # market branch and handed "day" to TimeInForce.__members__, which
        # Alpaca rejects outright with 42210000 "invalid crypto
        # time_in_force". Being a 4xx it counts as a definitive rejection, so
        # _submit_with_retry burned all three attempts re-sending the identical
        # bad order, and the engine then advanced its state cursor past the
        # signal — meaning no BTC/USD entry had EVER opened. The live journal
        # confirms it: three 42210000 rows on 2026-08-18 and zero crypto fills
        # in its entire history, while BTC/USD sat on the board as a
        # priority-1 long-bias candidate holding a rotation slot it could not
        # use (audit 2026-08-27, E1).
        #
        # Exits were never affected — close_position() sends no time_in_force
        # at all — so this is an entry-only repair.
        if is_crypto_symbol(payload.ticker) and payload.tif == "day":
            payload.tif = "gtc"

        # ALL-OUT IS ENFORCED HERE, not merely observed by the caller. `sell`
        # carries an arbitrary qty and would otherwise fall through to the
        # limit/market branches below and submit a PARTIAL exit — the relay is
        # the component that exists to enforce risk rules, so the invariant
        # cannot rest on the signal engine choosing not to ask (operator
        # directive 2026-08-27). fire_event already builds a close for both
        # actions; this is the backstop for any other caller.
        if payload.action == "sell":
            payload.action = "close"
            payload.qty = None
            result["action"], result["qty"] = "close", None

        try:
            if payload.action == "close":
                result["status"] = self._close_position(payload.ticker, positions)
            elif payload.order_type == "limit":
                if payload.limit_price is None:
                    raise RiskError("limit order requires limit_price")
                self._submit_with_retry(
                    payload.ticker, "limit",
                    lambda: self.client.limit_order(
                        payload.ticker, payload.qty, payload.action, payload.limit_price,
                        payload.tif, extended_hours=needs_extended,
                    ),
                )
                result["status"] = "submitted_limit"
            elif needs_extended:
                # Priced inside the closure so every retry re-quotes — see
                # _submit_with_retry.
                def _submit_ext():
                    px = self._extended_hours_limit_price(payload.ticker, payload.action)
                    self._report_stop_anchor_drift(payload, px)
                    return self.client.limit_order(
                        payload.ticker, payload.qty, payload.action, px,
                        payload.tif, extended_hours=True,
                    )
                self._submit_with_retry(payload.ticker, "limit_extended", _submit_ext)
                result["status"] = "submitted_limit_extended"
            else:
                self._submit_with_retry(
                    payload.ticker, "market",
                    lambda: self.client.market_order(
                        payload.ticker, payload.qty, payload.action, payload.tif,
                    ),
                )
                result["status"] = "submitted_market"
        except Exception as e:  # broker-side rejection
            result.update(status="error", reason=str(e))
            append_event(JOURNAL_FILE, {"event": "order_error", **result})
            raise

        append_event(JOURNAL_FILE, {"event": "order_ok", **result})
        return result
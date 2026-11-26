"""Precompute authoritative session + extended-hours prices for the research agents.

WHY THIS EXISTS
---------------
The post-market recap agent used to source its own prices through whatever MCP
call it happened to pick, and on this subscription `get_stock_snapshot` and
`get_stock_latest_trade` default to the **IEX** feed — one venue carrying a few
percent of the volume, which stops publishing around 17:00 ET. After the close
that feed's `latestTrade` is routinely a handful of shares inside an enormous
spread, and nothing about the response says so.

On 2026-07-29 that put a wrong number in the video. MSFT's IEX after-hours last
trade was $402.48: **40 shares**, quoted 394.50/404.89 (a 2.6% spread), printed at
16:55 ET. The recap reported "+2.9% to ~$402". Later the same evening the Blue
Ocean overnight book was 419.62/420.01 with 231x97 size — about **+7.5%**, more
than double the move that was reported. Nothing was fabricated: the agent read a
real print off the wrong feed, too early, from a trade too small to mean anything.

`relay/alpaca_client.get_latest_trade` already documents this exact trap for the
ORDER path ("outside regular hours the default IEX quote feed routinely returns a
one-sided quote (a real bid, ask=0) or a very wide stale one"). Research was the
one layer that had never been told.

So this module fetches the numbers deterministically, each from the feed that is
actually authoritative for its window, and JUDGES whether the extended quote is
fit to be quoted at all:

  * **regular session** — daily bars on `feed="sip"` (the consolidated tape),
    with the % move computed against the PREVIOUS sip close. Not an IEX daily
    bar: on 2026-07-29 IEX reported MSFT closing at $391.00 where sip said
    $390.54 on 47.3M shares.
  * **extended / overnight** — latest quote and trade on `feed="overnight"`, the
    Blue Ocean session that keeps trading long after IEX goes quiet. The recap
    cron fires at 10:00 AEST = ~20:00 ET, i.e. exactly when this is the live book.
  * **a `quality` verdict** per symbol derived from the SPREAD, the SIZE and the
    quote's AGE, plus a ready-made `statement` sentence. When the book is too thin
    the statement instructs the agent to describe direction in words and give NO
    percentage — the part that prompt guidance alone kept failing to enforce.

WHY NOT JUST INSTRUCT THE AGENT
-------------------------------
It was instructed. Prompt text re-litigates the tool choice on every run, and this
is the number most likely to be lifted verbatim into narration. Precomputing it
removes the choice — the same argument `agent/live_state.py` makes for indicators.

WHY ALPACA AND NOT IBKR
-----------------------
IBKR's dedicated OVERNIGHT venue is a genuine cross-check and `sigengine/ibkr.py`
already merges it for bars, but it needs IB Gateway running and a separate
overnight market-data entitlement, so it cannot be the dependency for an
unattended cron (and a missing entitlement fails silently to SMART-only). Alpaca
is already credentialed for this account, needs no local process, and answered
every probe. IBKR stays available for a manual second opinion.

Usage:
    .venv/bin/python -m agent.prices --symbols NVDA,MSFT --date 2026-07-29
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A quote is only worth quoting as a PRICE if both sides are real, close together
# and recent. These are deliberately strict: the cost of calling a thin print a
# price is a wrong number in a published video, while the cost of being too strict
# is a sentence that says "rose sharply" instead of "rose 7.5%".
# The SPREAD is the primary test: it is unit-free and decisive (2026-07-29 MSFT,
# IEX 2.6% vs the overnight book's 0.09%). Size is a secondary sanity check and is
# deliberately reported WITHOUT a unit — Alpaca quote sizes for US equities are
# round lots on some feeds and shares on others, and this code has not verified
# which applies per feed, so it must not print "shares" and imply it has.
MAX_SPREAD_PCT = 0.5
MIN_SIDE_SIZE = 50          # observed: good book 231x97, bad IEX print 40x40
MAX_STALE_MIN = 120         # `overnight` returns the last session's book when idle
MAX_TRADE_DISAGREE_PCT = 2.0  # last trade vs quote mid — disagreement means unsettled

# The consolidated tape is what "the close" means. Recent sip intraday data is
# gated on this subscription, so fall back in order and always RECORD which feed
# answered rather than silently degrading.
_DAILY_FEEDS = ("sip", "delayed_sip", "iex")


def _clients(key: str, secret: str):
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(key, secret)


def _daily_bars(client, symbols: list[str], start) -> tuple[dict, str]:
    """Daily bars from the best feed that answers. Returns (bars_by_symbol, feed)."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    last_exc = None
    for feed in _DAILY_FEEDS:
        try:
            bs = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                start=start, feed=DataFeed(feed)))
            data = bs.data if hasattr(bs, "data") else dict(bs)
            if data:
                return data, feed
        except Exception as exc:      # entitlement / rate / transient
            last_exc = exc
    # A batch is all-or-nothing at the API, so ONE unusable ticker takes the
    # whole request down with it. Fall back to per-symbol requests: slower, but
    # it costs one symbol instead of the entire file.
    if len(symbols) > 1:
        print(f"  [prices] batch failed ({last_exc}); retrying per symbol", file=sys.stderr)
        merged: dict = {}
        feed_used = "none"
        for sym in symbols:
            got, feed = _daily_bars(client, [sym], start)
            if got:
                merged.update(got)
                feed_used = feed
        if merged:
            return merged, feed_used
    print(f"  [prices] WARNING: no daily bars from any of {_DAILY_FEEDS}: {last_exc}",
          file=sys.stderr)
    return {}, "none"


def _latest(client, symbols: list[str], feed: str) -> tuple[dict, dict]:
    """(quotes, trades) on `feed`; either may be empty if the feed has nothing."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest
    out = []
    for req, call in ((StockLatestQuoteRequest, "get_stock_latest_quote"),
                      (StockLatestTradeRequest, "get_stock_latest_trade")):
        try:
            out.append(getattr(client, call)(
                req(symbol_or_symbols=symbols, feed=DataFeed(feed))) or {})
        except Exception as exc:
            print(f"  [prices] {feed} {call} failed: {exc}", file=sys.stderr)
            out.append({})
    return out[0], out[1]


def _session(bars: list, date: str) -> dict | None:
    """The `date` session bar plus the prior close, so the % move is vs the real
    previous close rather than the bar's own open."""
    if not bars:
        return None
    rows = sorted(bars, key=lambda b: b.timestamp)
    idx = next((i for i, b in enumerate(rows)
                if b.timestamp.date().isoformat() == date), None)
    if idx is None:                      # asked for a non-trading day
        idx = len(rows) - 1
    cur = rows[idx]
    prev = rows[idx - 1] if idx > 0 else None
    s = {"date": cur.timestamp.date().isoformat(),
         "close": round(float(cur.close), 4),
         "high": round(float(cur.high), 4), "low": round(float(cur.low), 4),
         "volume": int(cur.volume)}
    if prev is not None:
        s["prev_close"] = round(float(prev.close), 4)
        s["pct"] = round((float(cur.close) / float(prev.close) - 1) * 100, 2)
    return s


def _extended(quote, trade, close: float | None, now: datetime) -> dict:
    """Judge the overnight book, then say what may be claimed about it."""
    e: dict = {"quality": "unavailable"}
    bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
    ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
    bs = float(getattr(quote, "bid_size", 0) or 0) if quote else 0.0
    as_ = float(getattr(quote, "ask_size", 0) or 0) if quote else 0.0
    if quote is not None:
        e.update(bid=bid, ask=ask, bid_size=bs, ask_size=as_,
                 quote_at=getattr(quote, "timestamp", None) and
                 quote.timestamp.isoformat())
    if trade is not None:
        e.update(last=round(float(trade.price), 4), last_size=int(trade.size),
                 last_at=trade.timestamp.isoformat())

    reasons = []
    if not (bid > 0 and ask > 0):
        reasons.append("quote is one-sided or empty")
    else:
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100
        e["mid"] = round(mid, 4)
        e["spread_pct"] = round(spread_pct, 3)
        if spread_pct > MAX_SPREAD_PCT:
            reasons.append(f"spread {spread_pct:.2f}% > {MAX_SPREAD_PCT}%")
        if min(bs, as_) < MIN_SIDE_SIZE:
            reasons.append(f"quote size only {int(min(bs, as_))} on the thin side")
        age = e.get("quote_at") and (now - quote.timestamp).total_seconds() / 60
        if age is not None:
            e["quote_age_min"] = round(age, 1)
            if age > MAX_STALE_MIN:
                reasons.append(f"quote is {age:.0f} min stale")
        if e.get("last") and abs(e["last"] - mid) / mid * 100 > MAX_TRADE_DISAGREE_PCT:
            reasons.append(f"last trade {e['last']} disagrees with mid {round(mid, 2)}")

    price = e.get("mid") or e.get("last")
    pct = round((price / close - 1) * 100, 2) if (price and close) else None
    e["quality"] = "reliable" if (price and not reasons) else (
        "thin" if price else "unavailable")

    if e["quality"] == "reliable":
        e["pct_vs_close"] = pct
        return e

    # Do not hand back a quotable-looking number that we just judged unquotable.
    # A stale 12%-wide book still yields a confident-looking "-8.31%", and a model
    # reading this JSON will reach for a bare number next to a flag telling it not
    # to. So the derived figures move under a key nobody would quote by accident,
    # and only the DIRECTION survives in a usable place.
    e["concerns"] = reasons or ["no two-sided quote and no last trade"]
    if pct is not None:
        e["direction_vs_close"] = "higher" if pct > 0 else "lower"
    rejected = {k: e.pop(k) for k in ("mid", "spread_pct", "bid", "ask", "bid_size",
                                      "ask_size", "last", "last_size")
                if k in e}
    if pct is not None:
        rejected["pct_vs_close"] = pct
    if rejected:
        e["_rejected_do_not_quote"] = rejected
    return e


def _statement(sym: str, s: dict | None, e: dict, in_progress: bool = False) -> str:
    """One sentence the agent can lift verbatim — including, when the book is not
    quotable, an explicit instruction NOT to invent a percentage.

    `in_progress` matters because these runs are no longer all post-session: a
    daily bar read at 10:30 ET is a bar that is still forming, and calling its
    last print a "close" is how a briefing ends up telling viewers a stock closed
    at a price it is currently trading through.
    """
    if not s:
        return f"{sym}: no session data — do not state a price or % move."
    head = (f"{sym}: {'trading at' if in_progress else 'closed'} ${s['close']:,.2f}"
            + (f" ({s['pct']:+.2f}%)" if "pct" in s else "")
            + f", {'range so far' if in_progress else 'day range'} "
              f"${s['low']:,.2f}-${s['high']:,.2f} on {s['volume']:,} shares"
            + (" — THE SESSION IS STILL OPEN, so this is a live number, not a close"
               if in_progress else ""))
    if in_progress:
        # No extended clause at all while the session runs: the overnight book
        # closed at 04:00 ET, so anything it says is a stale comparison to a
        # price that is moving right now.
        return head + "."
    if e["quality"] == "reliable":
        return (head + f". EXTENDED: ~${e.get('mid') or e['last']:,.2f} "
                f"({e['pct_vs_close']:+.2f}% vs close), two-sided book "
                f"{e['bid']:,.2f}/{e['ask']:,.2f} with {int(e['bid_size'])}x"
                f"{int(e['ask_size'])} size — safe to quote this number.")
    if e["quality"] == "thin":
        direction = e.get("direction_vs_close", "unclear")
        return (head + f". EXTENDED: trading {direction} than the close but the book is "
                f"NOT quotable ({'; '.join(e.get('concerns', []))}). Describe the "
                f"direction in words and give NO percentage or price for the "
                f"extended move.")
    return head + ". EXTENDED: no usable data — say nothing about after-hours."


def _split_equities(symbols: list[str]) -> tuple[list[str], list[str]]:
    """(equities, non-equities). A crypto pair sent to a STOCK endpoint does not
    just return nothing for itself — Alpaca rejects the WHOLE batch with
    `invalid symbol: BTC/USD`, so one pair in the watchlist blanked the price
    file for all ten equities. Every intraday run had been failing this way (the
    daily run passes equities only, which is why it kept working)."""
    eq = [s for s in symbols if "/" not in s]
    return eq, [s for s in symbols if "/" in s]


def collect(symbols: list[str], date: str) -> dict:
    from relay.config import Settings
    st = Settings.from_env()
    client = _clients(st.alpaca_key, st.alpaca_secret)
    now = datetime.now(timezone.utc)
    start = datetime.fromisoformat(date).replace(tzinfo=timezone.utc) - timedelta(days=12)

    # Is the session we are asked about still running? Only then is the daily
    # bar a partial, live number rather than a settled close.
    et_now = datetime.now(ZoneInfo("America/New_York"))
    in_progress = (date == et_now.date().isoformat() and et_now.weekday() < 5
                   and (9, 30) <= (et_now.hour, et_now.minute) < (16, 0))

    symbols, non_equity = _split_equities(symbols)
    if non_equity:
        print(f"  [prices] not equities, skipped: {', '.join(non_equity)}", file=sys.stderr)

    bars, daily_feed = _daily_bars(client, symbols, start)
    oq, ot = _latest(client, symbols, "overnight")
    # IEX's own last print is kept purely as evidence: when it disagrees wildly
    # with the overnight book, that gap IS the bug this module exists to prevent.
    _, iex_t = _latest(client, symbols, "iex")

    out = {"generated_at": now.isoformat(timespec="seconds"), "session_date": date,
           "skipped_non_equity": non_equity,
           "session_in_progress": in_progress,
           "daily_feed": daily_feed, "extended_feed": "overnight", "symbols": {}}
    for sym in symbols:
        s = _session(bars.get(sym) or [], date)
        if in_progress:
            # The overnight book closed at 04:00 ET. Quoting it against a live
            # session price is comparing a stale, thin print to the real market.
            e = {"quality": "unavailable",
                 "concerns": ["the regular session is open; the extended/overnight "
                              "book is closed and is not the live market"]}
        else:
            e = _extended(oq.get(sym), ot.get(sym), s and s.get("close"), now)
        it = iex_t.get(sym)
        if it is not None:
            e["iex_last_for_contrast"] = {
                "price": round(float(it.price), 4), "size": int(it.size),
                "at": it.timestamp.isoformat()}
        out["symbols"][sym] = {"session": s, "extended": e,
                               "statement": _statement(sym, s, e, in_progress)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--symbols", required=True, help="comma-separated tickers")
    ap.add_argument("--date", default=None,
                    help="US session date YYYY-MM-DD (default: today in ET)")
    a = ap.parse_args()

    date = a.date
    if not date:
        from zoneinfo import ZoneInfo
        date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    data = collect(syms, date)
    text = json.dumps(data, indent=2)
    out = ROOT / "decisions" / f"_prices_{date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n")
    ok = sum(1 for v in data["symbols"].values() if v["extended"]["quality"] == "reliable")
    print(f"  [prices] {out} — daily feed={data['daily_feed']}, "
          f"{ok}/{len(syms)} quotable extended book(s)")


if __name__ == "__main__":
    main()

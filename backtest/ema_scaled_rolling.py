"""
    DORMANT UNDER THE CURRENT POLICY. This runner's half-target scale-out (the
    `sell` events below) is the only place in the repo that emits a partial
    exit, and since 2026-08-27 the book is ALL-IN/ALL-OUT by operator
    directive: sigengine.engine.fire_event routes `sell` to a full close. No
    configured symbol uses this strategy. If it is ever re-enabled, its
    scale-out will execute as a FULL exit — which is not what the numbers
    below were tuned for.
Scaled-exit EMA-cross with a ROLLING N-bar close-extreme reference (event-driven).

Same strategy as `ema_scaled.py` except the "previous high" (half-target) and the
"previous low" (structural stop reference) are no longer the *session* high/low
— they are the **highest / lowest CLOSE over the N bars immediately prior to the
entry bar**. This:

  * works identically on 24/7 crypto (BTC/USD) where there is no session reset;
  * gives a window-length knob (N) we can tune — 100 vs 250 vs other — instead of
    a fixed intraday session.

Strategy (unchanged logic, new reference levels):
  ENTRY   : fast EMA crosses above slow EMA -> deploy ~97% of equity at next open.
  TARGET  : previous high = max(close[i-N : i]) at the entry bar i. First CLOSE
            at/above it while trend holds -> sell HALF at that close.
  TREND   : fast crosses below slow -> exit ALL remaining (no half if before the
            high).  STOP : exit ALL if close <= entry*(1-stop) OR close < previous
            low = min(close[i-N : i]) at entry.

  ALL decisions (stop, trend exit, target) are made on the bar's CLOSE only —
  there is no intrabar high/low touch logic anywhere in this loop (matches how
  the live engine actually observes the world: it only ever sees closed bars).

Hand-rolled event loop (vectorbt can't express half/rest/stop). Default data
source is yfinance (free); BTC/USD is fetched as the yfinance ticker BTC-USD.

Usage:
    .venv/bin/python -m backtest.ema_scaled_rolling --symbol AAPL --window 100
    .venv/bin/python -m backtest.ema_scaled_rolling --symbol BTC/USD --interval 1h --sweep-windows 50,100,150,200,250
    .venv/bin/python -m backtest.ema_scaled_rolling --symbol NVDA --sweep
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

# yfinance period cap depends on interval.
_YF_PERIOD = {"1min": "7d", "5min": "60d", "15min": "60d", "30min": "60d", "1h": "730d", "1d": "max"}
_YF_INTERVAL = {"1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m", "1h": "1h", "1d": "1d"}


def _yf_symbol(symbol: str) -> str:
    """Map broker/TV symbols to yfinance tickers."""
    s = symbol.upper().strip()
    if s == "BTC/USD" or s == "BTCUSD":
        return "BTC-USD"
    if s == "ETH/USD":
        return "ETH-USD"
    return s


_YF_30M_MAX_DAYS = 60  # yfinance's hard cap on 30m history


def _is_crypto_ticker(symbol: str) -> bool:
    """True for the 24/7 crypto pairs, whose yfinance bars need no realignment."""
    s = symbol.upper().strip()
    return s.endswith("/USD") or s.endswith("-USD") or s in {"BTCUSD", "ETHUSD"}


def _clamp_30m_period(period: str) -> str:
    """Clamp a yfinance `period` to what the 30m endpoint will actually serve.

    yfinance answers an over-long 30m request with an empty frame rather than a
    short one, so asking for "730d" of 30m yields nothing at all — clamp to 60d.
    """
    p = (period or "").strip().lower()
    if p.endswith("d") and p[:-1].isdigit():
        return f"{min(int(p[:-1]), _YF_30M_MAX_DAYS)}d"
    if p in ("max", "ytd") or p.endswith(("y", "mo")):
        return f"{_YF_30M_MAX_DAYS}d"
    return period


def _yf_hourly_aligned(ticker: str, period: str, prepost: bool = False) -> pd.DataFrame:
    """Fetch :00-aligned hourly bars from yfinance by resampling its 30m series.

    yfinance anchors US-equity intraday bars to the 09:30 ET open, so its "1h"
    bars are stamped 09:30, 10:30, 11:30 ... — a grid that can NEVER coincide
    with IBKR's :00-aligned bars. Cached under the same (symbol, interval) key
    those two grids don't collide on the primary key, so both survive and the
    engine's warm-up window ends up interleaved at ~2 bars/hour (found
    2026-07-14: all 11 symbols, ~90-110 hours of the 60d window affected).
    Snapping the label :30 -> :00 would be a lie about which half-hour the OHLC
    covers, so instead we rebuild true hourly buckets from the 30m series, which
    IS on the :00/:30 grid and aggregates exactly onto hour boundaries.

    Bars are resampled in UTC with origin="epoch" so buckets land on real hour
    boundaries (US market zones are whole-hour offsets from UTC, so :00 UTC is
    also :00 local). label/closed="left" matches the bar-open-stamp convention
    the rest of the system uses.

    Coverage: with prepost=False yfinance serves REGULAR HOURS ONLY, so the
    09:00 bucket holds just the 09:30-10:00 half-hour, where IBKR's 09:00 bar
    covers the full hour including pre-market. prepost=True adds the 04:00-09:30
    ET pre-market and 16:00-20:00 ET post-market bars — the live cache writer
    passes True (added 2026-08-05 after a 15h IBKR ushmds farm outage left the
    equity cache frozen through an entire overnight+pre-market stretch because
    this fallback had nothing newer than the last RTH close). yfinance still has
    NO overnight-session data (20:00-04:00 ET is IBKR OVERNIGHT-exchange only),
    so an IBKR outage in that window still stalls the cache — prepost just
    shrinks the blind window from 16h to the 8h overnight. This is a fallback
    for transient IBKR outages, not the primary source — grid agreement (no
    interleaving, and the PK now dedupes) is worth far more than the half-bar
    edge. 30m history is capped by yfinance at 60 days.
    """
    import yfinance as yf

    df = yf.download(ticker, period=period, interval="30m", progress=False, auto_adjust=True,
                     prepost=prepost)
    if df.empty:
        raise SystemExit(f"no data for {ticker} (yfinance 30m)")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep].dropna(subset=["High", "Low", "Close"])
    df.columns = [c.lower() for c in keep]
    df = df[~df.index.duplicated(keep="last")].sort_index()

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert("UTC")

    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    out = df.resample("1h", label="left", closed="left", origin="epoch").agg(agg)
    return out.dropna(subset=["close"])


def load_ohlcv(symbol: str, interval: str = "5min", source: str = "yfinance",
               limit: int = 10000, yf_period_override: str | None = None,
               feed: str | None = None, align_hourly: bool = False,
               prepost: bool = False) -> pd.DataFrame:
    """tz-aware DataFrame indexed by timestamp with high/low/close.

    yf_period_override: yfinance-only. Overrides `_YF_PERIOD[interval]` (which
    always pulls the FULL lookback, e.g. "60d" for 5-min bars — correct for a
    one-shot backtest CLI run, wasteful for a live poller re-fetching every
    60s). Live callers with an already-warm cache should pass a short period
    (e.g. "5d") here; leave None to keep today's full-window behavior exactly.

    feed: alpaca-only. Passed straight to alpaca-py as a `DataFeed` (e.g.
    "sip", "iex"). None keeps the prior implicit-default behavior exactly.

    align_hourly: yfinance + interval="1h" only. Return :00-aligned hourly bars
    (rebuilt from the 30m series — see _yf_hourly_aligned) instead of yfinance's
    native 09:30-anchored ones, so they share IBKR's grid and can be cached
    alongside it without interleaving. The live cache writer
    (sigengine.engine._load_with_retry) passes True. Defaults to False because
    it costs history depth: yfinance serves 730d of 1h bars but only 60d of 30m,
    and the backtest CLIs that call this with source="yfinance" want the deep
    window.

    prepost: yfinance only. Include pre-market (04:00-09:30 ET) and post-market
    (16:00-20:00 ET) bars, matching the extended-hours coverage the IBKR
    primary already has (yfinance has no overnight-session data, so 20:00-04:00
    ET stays IBKR-only either way). The live cache writer passes True so an
    IBKR outage outside RTH can't freeze the cache (2026-08-05). Defaults to
    False so backtest CLIs keep the RTH-only series their params were validated
    on. No effect on crypto (24/7 venue, no extended sessions).

    Sources:
      "yfinance" — free, in-memory each call (default; the original source).
      "alpaca"   — Alpaca historical SIP bars (needs a paid plan for recent 5-min).
      "cache"    — read from the shared SQLite bar cache (relay.bars_db). Fast,
                   rate-limit-free sweeps; the engine fills the cache each poll.
                   Cold cache → SystemExit (fill it first via source="yfinance"
                   or "ibkr").
      "ibkr"     — IBKR historical bars via sigengine.ibkr (added in Part 2).
    """
    iv = interval.lower()
    if source == "cache":
        from relay.bars_db import read_bars
        df = read_bars(symbol, iv)
        if df.empty:
            raise SystemExit(
                f"cache cold for {symbol} {iv} — fill it first with "
                f"load_ohlcv({symbol!r}, {iv!r}, 'yfinance') or 'ibkr'")
        df = df[["high", "low", "close", "open"]].dropna(subset=["high", "low", "close"])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
    if source == "alpaca":
        import datetime as _dt
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from relay.config import get_settings
        # TimeFrameUnit.Minute only accepts amounts 1-59 (Alpaca rejects 60) —
        # "1h" must use TimeFrameUnit.Hour instead, or this raises ValueError.
        tf = TimeFrame(1, TimeFrameUnit.Hour) if iv == "1h" else TimeFrame(
            {"1min": 1, "5min": 5, "15min": 15, "30min": 30}[iv], TimeFrameUnit.Minute)
        s = get_settings()
        feed_kwarg = {"feed": DataFeed(feed)} if feed else {}
        # Alpaca's bars endpoint returns ZERO rows if `limit` is given with no
        # `start` (confirmed empirically — not documented) — a `start` far
        # enough back to cover `limit` bars is required, not optional.
        _lookback_days = {"1min": 10, "5min": 70, "15min": 70, "30min": 70, "1h": 800}
        start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=_lookback_days[iv])
        if symbol.upper().endswith("/USD") or symbol.upper() in {"BTCUSD", "ETHUSD"}:
            c = CryptoHistoricalDataClient(api_key=s.alpaca_key, secret_key=s.alpaca_secret)
            df = c.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=symbol,
                            timeframe=tf, start=start, limit=limit)).df
        else:
            c = StockHistoricalDataClient(api_key=s.alpaca_key, secret_key=s.alpaca_secret)
            df = c.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol,
                            timeframe=tf, start=start, limit=limit,
                            adjustment=Adjustment.RAW, **feed_kwarg)).df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        # alpaca-py's .df already has lower-case columns (open/high/low/close/...).
        cols = ["high", "low", "close"] + (["open"] if "open" in df.columns else []) \
            + (["volume"] if "volume" in df.columns else [])
        df = df[cols].dropna(subset=["high", "low", "close"])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
    if source == "ibkr":
        from sigengine.ibkr import fetch_bars
        df = fetch_bars(symbol, iv)
        if df is None or df.empty:
            raise SystemExit(f"no IBKR data for {symbol} {iv} (Gateway down or not subscribed)")
        cols = ["high", "low", "close"] + (["open"] if "open" in df.columns else [])
        df = df[cols].dropna(subset=["high", "low", "close"])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
    # default: yfinance
    import yfinance as yf
    period = yf_period_override or _YF_PERIOD[iv]
    # Crypto is exempt: a 24/7 venue has no 09:30 open to anchor to, so
    # yfinance's crypto hourly bars are ALREADY :00-aligned (verified in the
    # cache: 17,626 BTC/USD rows, every one at :00). Resampling them would be a
    # no-op that costs history — 30m only reaches 60d where 1h reaches 730d.
    if align_hourly and iv == "1h" and not _is_crypto_ticker(symbol):
        # 30m history is capped at 60d by yfinance, so a caller asking for the
        # full 730d window silently gets 60d unless we clamp it here explicitly.
        return _yf_hourly_aligned(_yf_symbol(symbol), _clamp_30m_period(period),
                                  prepost=prepost)
    df = yf.download(_yf_symbol(symbol), period=period, interval=_YF_INTERVAL[iv],
                     progress=False, auto_adjust=True, prepost=prepost)
    if df.empty:
        raise SystemExit(f"no data for {symbol} (yfinance {_yf_symbol(symbol)})")
    # yfinance >=0.2.something returns MultiIndex columns (field, ticker) even
    # for a single-ticker download — flatten to field names only. Without this,
    # df[["Open","High","Low","Close"]] raises KeyError and this entire branch
    # is unusable; since crypto ALWAYS routes through here (never IBKR, see
    # fetch_bars' Paxos-limited comment), this silently broke crypto trading.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna(subset=["High", "Low", "Close"])
    df.columns = ["open", "high", "low", "close"]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _rolling_close_extremes(close: np.ndarray, window: int):
    """Return (roll_high, roll_low) arrays where roll_high[i] = max(close[i-W:i])
    (the W closes BEFORE bar i, excluding i), NaN where i < window."""
    s = pd.Series(close)
    shifted = s.shift(1)
    roll_high = shifted.rolling(window).max().to_numpy(dtype=float)
    roll_low = shifted.rolling(window).min().to_numpy(dtype=float)
    return roll_high, roll_low


def _run_core(df: pd.DataFrame, fast: int, slow: int, window: int,
              init_cash: float = 100_000, deploy: float = 0.97,
              stop_pct: float = 0.02, fees: float = 0.0,
              bars_per_year: int | None = None,
              use_real_open: bool = False, whole_shares: bool = False,
              exec_next_open: bool = False):
    """Event-driven backtest with rolling N-bar close extremes fixed at entry.

    Returns (stats_dict, equity_series, events). `events` is the chronological
    list of order events {ts, action, price, reason} the strategy would place —
    used by the live signal engine (signal/engine.py) to diff against the last
    processed bar. Sharing this loop between backtest and live guarantees the
    signal logic cannot drift.

    use_real_open (default OFF): fill entries at the bar's REAL open price
    (from `df["open"]`, when present and finite) instead of the prior bar's
    close proxy. Falls back to the close proxy per-bar if open is missing/NaN
    (e.g. older cached rows). Default OFF so existing backtests and the live
    signal engine (which does not pass this) are byte-identical to before —
    this is an opt-in realism improvement, not a behavior change.

    whole_shares (default OFF): floor entry qty to whole shares, matching what
    `sigengine.engine._round_qty` actually does live for equities (crypto is
    never floored, live or here). Default OFF preserves the exact fractional
    qty this loop has always reported; turn on to see what live would actually
    have bought.

    exec_next_open (default OFF): decouple EXIT decision from EXIT fill, the
    same way entries already work. With this OFF (today's behavior), a stop /
    trend-exit / half-target decision made on bar i's close fills AT that same
    close — i.e. the strategy is assumed to react and transact at a price it
    could only have known once the bar had already finished printing, which is
    not achievable live. With this ON, the decision is still made on bar i's
    close (unchanged signal logic), but the fill is deferred to bar i+1's open
    (real open if available, else the prior-close proxy via the same `open_i`
    entries already use) — mirroring exactly how entries already behave here,
    and how the live engine actually can act (it only sees a bar's close once
    polling notices it has closed, and the next real trade opportunity is the
    following bar)."""
    close = df["close"].to_numpy(dtype=float)
    if use_real_open and "open" in df.columns:
        real_open = df["open"].to_numpy(dtype=float)
    else:
        real_open = None
    n = len(df)
    if n < max(slow, window) + 2:
        return ({"fast": fast, "slow": slow, "window": window,
                 "total_return": 0.0, "sharpe": float("nan"),
                 "max_drawdown": 0.0, "win_rate": 0.0, "n_trades": 0},
                pd.Series(init_cash, index=[df.index[0]]), [])

    fast_ema = pd.Series(close).ewm(span=fast, adjust=False).mean().to_numpy()
    slow_ema = pd.Series(close).ewm(span=slow, adjust=False).mean().to_numpy()
    cross_up = (fast_ema > slow_ema) & (np.r_[False, fast_ema[:-1] <= slow_ema[:-1]])
    cross_down = (fast_ema < slow_ema) & (np.r_[False, fast_ema[:-1] >= slow_ema[:-1]])

    roll_high, roll_low = _rolling_close_extremes(close, window)

    cash = init_cash
    position = 0.0
    entry_price = 0.0
    target = None
    prev_low = None          # rolling N-bar close min fixed at entry (stop reference)
    half_done = False
    pending_entry = False
    pending_exit = None  # None, or {"kind": "half"|"full", "reason": str} — only
                         # used when exec_next_open=True; queued at bar i's close,
                         # filled at bar i+1's open (mirrors pending_entry).

    equity = np.empty(n)
    trades = []
    events = []

    for i in range(n):
        proxy_open = close[i - 1] if i > 0 else close[i]  # proxy for next-bar open
        if real_open is not None and np.isfinite(real_open[i]) and real_open[i] > 0:
            open_i = real_open[i]
        else:
            open_i = proxy_open

        if pending_entry and position == 0:
            entry_px = open_i
            qty = (cash * deploy) / entry_px if entry_px > 0 else 0.0
            if whole_shares:
                qty = float(np.floor(qty))
            if qty > 0:
                cash -= qty * entry_px * (1 + fees)
                position = qty
                entry_price = entry_px
                half_done = False
                # previous high = highest CLOSE of the N bars before this entry bar
                rh = roll_high[i]
                target = float(rh) if np.isfinite(rh) and rh > entry_px else None
                rl = roll_low[i]
                prev_low = float(rl) if np.isfinite(rl) else None
                trades.append({"entry": entry_px, "qty": qty, "exit": np.nan, "exit_reason": ""})
                events.append({"ts": df.index[i], "action": "buy", "price": float(entry_px),
                               "reason": "cross_up"})
        pending_entry = False

        # Fill any exit queued from the PREVIOUS bar's close, at THIS bar's open.
        if pending_exit is not None and position > 0:
            if pending_exit["kind"] == "half":
                half_qty = position / 2.0
                cash += half_qty * open_i * (1 - fees)
                position -= half_qty
                half_done = True
                events.append({"ts": df.index[i], "action": "sell", "price": float(open_i),
                               "qty": float(half_qty), "reason": pending_exit["reason"]})
            else:
                cash += position * open_i * (1 - fees)
                trades[-1]["exit"] = open_i
                trades[-1]["exit_reason"] = pending_exit["reason"]
                position = 0.0
                events.append({"ts": df.index[i], "action": "close", "price": float(open_i),
                               "reason": pending_exit["reason"]})
        pending_exit = None

        if position > 0:
            c = close[i]
            stopped = False
            if prev_low is not None and c < prev_low:
                stopped = True
            elif c <= entry_price * (1 - stop_pct):
                stopped = True

            exit_kind = None
            exit_reason = None
            if stopped:
                exit_kind, exit_reason = "full", "stop"
            elif cross_down[i]:
                exit_kind = "full"
                exit_reason = "cross_down" + ("_after_half" if half_done else "")
            elif (not half_done) and target is not None and c >= target:
                # Bar-close decision (no intrabar high/low touches anywhere in
                # this loop): the half-target fires only once the bar CLOSES
                # at/above target — not at the exact target level, which would
                # imply a resting limit order filling on an intrabar touch.
                exit_kind, exit_reason = "half", "half"

            if exit_kind is not None:
                if exec_next_open:
                    pending_exit = {"kind": exit_kind, "reason": exit_reason}
                elif exit_kind == "half":
                    half_qty = position / 2.0
                    cash += half_qty * c * (1 - fees)
                    position -= half_qty
                    half_done = True
                    events.append({"ts": df.index[i], "action": "sell", "price": float(c),
                                   "qty": float(half_qty), "reason": exit_reason})
                else:
                    cash += position * c * (1 - fees)
                    trades[-1]["exit"] = c
                    trades[-1]["exit_reason"] = exit_reason
                    position = 0.0
                    events.append({"ts": df.index[i], "action": "close", "price": float(c),
                                   "reason": exit_reason})

        if cross_up[i] and position == 0:
            pending_entry = True

        equity[i] = cash + position * close[i]

    if position > 0:
        c = close[-1]
        cash += position * c * (1 - fees)
        trades[-1]["exit"] = c
        trades[-1]["exit_reason"] = "eod"
        position = 0.0
        equity[-1] = cash
        events.append({"ts": df.index[-1], "action": "close", "price": float(c),
                       "reason": "eod"})

    eq = pd.Series(equity, index=df.index)
    total_return = (cash / init_cash - 1.0) * 100 if position == 0 else (eq.iloc[-1] / init_cash - 1) * 100
    peak = eq.cummax()
    max_dd = float((eq / peak - 1.0).min() * 100)
    closed = [t for t in trades if not np.isnan(t["exit"])]
    wins = sum(1 for t in closed if t["exit"] > t["entry"])
    win_rate = (wins / len(closed) * 100) if closed else 0.0
    rets = eq.pct_change().dropna()
    sharpe = float("nan")
    if rets.std() > 0:
        bpy = bars_per_year if bars_per_year else (78 * 252)  # default 5-min assumption
        sharpe = float(rets.mean() / rets.std() * np.sqrt(bpy))
    return {
        "fast": fast, "slow": slow, "window": window,
        "total_return": float(total_return),
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "win_rate": float(win_rate),
        "n_trades": len(closed),
    }, eq, events


def run_scaled_rolling(df, fast, slow, window, **kw):
    """Backtest wrapper: returns (stats, equity) — discards events."""
    stats, eq, _events = _run_core(df, fast, slow, window, **kw)
    return stats, eq


def run_with_events(df, fast, slow, window, **kw):
    """Backtest + events: returns (stats, equity, events). Used by the live
    signal engine to diff the strategy's order events against the last
    processed bar."""
    return _run_core(df, fast, slow, window, **kw)


def sweep_windows(df, fast, slow, windows=(50, 100, 150, 200, 250), **kw):
    rows = []
    for w in windows:
        r, _ = run_scaled_rolling(df, fast, slow, w, **kw)
        rows.append(r)
    return pd.DataFrame(rows)


def sweep(df, fasts=(9, 20, 29), slows=(50, 65, 75), windows=(50, 100, 150, 200, 250), **kw):
    rows = []
    for fast in fasts:
        for slow in slows:
            if slow <= fast:
                continue
            for w in windows:
                r, _ = run_scaled_rolling(df, fast, slow, w, **kw)
                rows.append(r)
    return pd.DataFrame(rows).sort_values("total_return", ascending=False, na_position="last")


def main() -> None:
    ap = argparse.ArgumentParser(description="Scaled-exit EMA-cross, rolling N-bar close extremes")
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--interval", default="5min")
    ap.add_argument("--source", default="yfinance", choices=["yfinance", "alpaca", "cache", "ibkr"])
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=50)
    ap.add_argument("--window", type=int, default=100)
    ap.add_argument("--deploy", type=float, default=0.97)
    ap.add_argument("--stop", type=float, default=0.02)
    ap.add_argument("--fees", type=float, default=0.0)
    ap.add_argument("--sweep", action="store_true", help="sweep fast/slow x windows grid")
    ap.add_argument("--sweep-windows", default=None, help="comma list of windows to sweep at the given fast/slow")
    ap.add_argument("--use-real-open", action="store_true",
                    help="fill entries at the bar's real open (falls back to prior-close proxy if missing) "
                         "instead of always using the prior-close proxy — opt-in realism check")
    ap.add_argument("--whole-shares", action="store_true",
                    help="floor entry qty to whole shares (matches what live actually buys for equities) "
                         "instead of the exact fractional qty this loop has always reported")
    args = ap.parse_args()

    df = load_ohlcv(args.symbol, args.interval, args.source)
    bpy = 78 * 252 if args.interval in {"5min", "1min"} else (6.5 * 252 if args.interval in {"15min", "30min"} else (252 if args.interval == "1d" else 24 * 365))
    print(f"Loaded {len(df)} {args.interval} bars for {args.symbol} "
          f"({df.index[0]} -> {df.index[-1]})  [source={args.source}]")
    realism_kw = dict(use_real_open=args.use_real_open, whole_shares=args.whole_shares)

    if args.sweep_windows:
        windows = [int(x) for x in args.sweep_windows.split(",")]
        out = sweep_windows(df, args.fast, args.slow, windows=windows, deploy=args.deploy,
                            stop_pct=args.stop, fees=args.fees, bars_per_year=bpy, **realism_kw)
        print("\nWindow sweep (fixed fast/slow):")
        print(out.to_string(index=False))
        best = out.sort_values("total_return", ascending=False).iloc[0]
        print(f"\nBest window: {int(best.window)}  Return={best.total_return:.2f}% "
              f"Sharpe={best.sharpe:.2f} MaxDD={best.max_drawdown:.2f}% Trades={int(best.n_trades)}")
    elif args.sweep:
        out = sweep(df, deploy=args.deploy, stop_pct=args.stop, fees=args.fees, bars_per_year=bpy, **realism_kw)
        print("\nTop 8 by total return:")
        print(out.head(8).to_string(index=False))
        best = out.iloc[0]
        print(f"\nBest: fast={int(best.fast)} slow={int(best.slow)} window={int(best.window)} "
              f"Return={best.total_return:.2f}% Sharpe={best.sharpe:.2f} "
              f"MaxDD={best.max_drawdown:.2f}% Win={best.win_rate:.0f}% Trades={int(best.n_trades)}")
    else:
        r, _ = run_scaled_rolling(df, args.fast, args.slow, args.window, deploy=args.deploy,
                                  stop_pct=args.stop, fees=args.fees, bars_per_year=bpy, **realism_kw)
        print(f"\n{args.interval} fast={args.fast} slow={args.slow} window={args.window} "
              f"deploy={args.deploy} stop={args.stop}")
        print(f"  Total return: {r['total_return']:.2f}%")
        print(f"  Sharpe:      {r['sharpe']:.2f}")
        print(f"  Max DD:      {r['max_drawdown']:.2f}%")
        print(f"  Win rate:    {r['win_rate']:.0f}%")
        print(f"  Trades:      {r['n_trades']}")


if __name__ == "__main__":
    main()
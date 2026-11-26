"""Build the dashboard's candlestick chart data for the watchlist, with the actual
indicators behind each symbol's live strategy — Bollinger Bands + RSI, MACD
histogram (SMA-based) — plus buy/exit markers, all computed from the symbol's
ACTUAL configured `strategies` rotation (backtest.multi_strategy —
the exact functions sigengine/engine.py trades on), using the SAME cached
bars and config (relay/bars_db.py + sigengine/config.json) — not a fresh
network fetch, so this is instant and works even if IBKR/yfinance are
unreachable. Account-level exits that no strategy recompute can show —
rotation closes and reconcile closes from the relay journal — are overlaid
as amber arrows (see _journal_exit_markers), so what the chart shows matches
what the account actually did.

THREE KINDS OF MARK, answering three different questions. Green/red ARROWS are
the strategy recomputed over today's bars: "what does the strategy say now".
Amber arrows are account-level exits from the journal. Blue/violet CIRCLES are
actual broker FILLS: "what did I really trade" (_fill_markers). The last of
those is not a nicety — bars get rewritten under the chart (the engine upserts
fresh 10-day IBKR pulls that OVERWRITE values), so a recomputed arrow can vanish
while the trade behind it stays very real. On 2026-08-20, 5 of the 9 fills in
the displayed windows had no mark at all before fills were drawn. No EMA overlay: since the live engine moved off EMA-cross to a
MACD+SMA/Bollinger+RSI rotation (2026-07-04), plotting EMA lines would show a
signal the account isn't actually trading on.

The :8787 dashboard serves _build_chart_data's output at /api/chart
(web/server.py) and draws it with TradingView's open-source Lightweight Charts
library, vendored at web/vendor/.
"""
from __future__ import annotations

import bisect
import json
import math
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.bollinger_rsi import compute_indicators as _bb_rsi_indicators
from backtest.macd_sma import compute_indicators as _macd_sma_indicators
from backtest.multi_strategy import run_symbol_strategies
from relay.bars_db import read_bars
from relay.config import JOURNAL_FILE

_STRATEGY_LABELS = {"ema": "EMA", "bollinger_rsi": "BB", "macd_sma": "MACD"}
_MELBOURNE = ZoneInfo("Australia/Melbourne")


def _chart_ts(ts: pd.Timestamp) -> int:
    """Lightweight Charts formats axis/crosshair labels using the JS Date
    object's UTC getters, not the browser's local timezone — so to display
    Melbourne wall-clock time we bake the shift into the epoch value itself:
    convert to Melbourne local time (correctly handling the AEST/AEDT DST
    transition per-timestamp, not a fixed +10 offset) then re-tag those same
    Y/M/D/H/M/S fields as UTC before taking .timestamp()."""
    mel = ts.tz_convert(_MELBOURNE)
    return int(pd.Timestamp(mel.year, mel.month, mel.day, mel.hour, mel.minute,
                            mel.second, tz="UTC").timestamp())


def _strategy_spec(cfg: dict, type_name: str) -> dict | None:
    for spec in cfg.get("strategies", []):
        if spec.get("type") == type_name:
            return spec
    return None


def _series_points(index, values) -> list[dict]:
    """Tail-align a full-history indicator array to the displayed window's
    index and drop NaNs (indicator warm-up), in Lightweight Charts'
    {time, value} line-series format."""
    vals = values[-len(index):]
    return [{"time": _chart_ts(ts), "value": round(float(v), 4)}
            for ts, v in zip(index, vals) if not math.isnan(v)]


def _hist_points(index, values) -> list[dict]:
    """Same as _series_points but for a histogram series — each point also
    carries its own up/down color so the zero-crossing (this strategy's
    actual entry/exit trigger) reads at a glance."""
    vals = values[-len(index):]
    pts = []
    for ts, v in zip(index, vals):
        if math.isnan(v):
            continue
        pts.append({"time": _chart_ts(ts), "value": round(float(v), 6),
                    "color": "#34b892" if v >= 0 else "#e2596b"})
    return pts


def _markers_for_window(full_df, cfg: dict, sym: str, window_start) -> list[dict]:
    """Recompute the symbol's configured strategies over the FULL cached
    history (needed for indicators with long lookbacks, e.g. MACD+SMA's
    veryslow_len=200) and return only the buy/exit markers that fall within
    the displayed window — same recompute-and-diff data source the live
    engine itself trades on (backtest.multi_strategy.run_symbol_strategies),
    so what's drawn here is exactly what the engine would flag, not a
    separate approximation. "eod" events are a backtest-window artifact
    (close at end of the recomputed series) and are dropped, same as
    sigengine/engine.py ignores them live."""
    events = run_symbol_strategies(full_df, cfg, sym)
    markers = []
    for e in events:
        if e["ts"] < window_start or e.get("reason") == "eod":
            continue
        label = _STRATEGY_LABELS.get(e.get("strategy"), (e.get("strategy") or "?").upper())
        if e["action"] == "buy":
            markers.append({"time": _chart_ts(e["ts"]), "position": "belowBar",
                            "color": "#34b892", "shape": "arrowUp", "text": label})
        elif e["action"] in ("close", "sell"):
            markers.append({"time": _chart_ts(e["ts"]), "position": "aboveBar",
                            "color": "#e2596b", "shape": "arrowDown", "text": label})
    markers.sort(key=lambda m: m["time"])
    return markers


# Journal events that are ACTUAL account exits but never appear in the
# strategy recompute above: a rotation_close fires off ANOTHER symbol's buy
# signal (relay/order_manager._should_rotate), and a reconcile_close is the
# engine flattening a strategy/broker divergence. Without these the chart
# shows a position as still open after the account exited it — seen live
# 2026-08-03 when GOOG rotated into AMZN with no exit arrow anywhere.
_JOURNAL_EXIT_EVENTS = {"rotation_close", "reconcile_close"}

# Journal events meaning "that close did NOT go to the broker". Used to drop
# exit arrows for closes that were only ATTEMPTED — see _REJECTED_CLOSE_WINDOW_SEC.
_FAILED_CLOSE_EVENTS = {"order_error", "risk_block", "reconcile_close_failed"}

# How long after a journaled exit a failure for the same ticker still counts as
# "that exit is the one that failed".
#
# Until 2026-08-19 the engine journaled reconcile_close BEFORE attempting the
# order, so a rejected close left an exit arrow behind exactly like a real one.
# That is why TSLA showed 115 exit arrows on 2026-08-18 for a SINGLE genuine
# exit: the position's shares were pinned by a resting sell that had not filled,
# so every poll's extra close was rejected (code 40310000) while still drawing
# an arrow. The engine no longer journals intent, but ~230 such rows are already
# on disk and the chart reads the journal from the beginning, so they still have
# to be filtered out here. The window only has to span one handle() call: three
# submit attempts with 2s/4s backoff plus quote fetches, i.e. well under a
# minute.
_REJECTED_CLOSE_WINDOW_SEC = 90.0

# Colours for FILL markers — deliberately not the green/red the recomputed
# signal arrows use. A fill and a signal answer different questions and a reader
# must be able to tell at a glance which one they are looking at.
_FILL_BUY_COLOR = "#7b8fff"      # the same blue the Bollinger basis uses
_FILL_SELL_COLOR = "#c58fff"


def _fill_markers(fills: list[dict], index, window_start) -> list[dict]:
    """Markers for what the account ACTUALLY traded, from broker fills.

    WHY THIS EXISTS. Every other marker on this chart is derived: the arrows
    from _markers_for_window are the strategy RECOMPUTED over the current bar
    cache, and the journal markers are account events. None of them is a fill.
    So a real execution appears only for as long as the bars still reproduce the
    signal behind it — and bars are not stable. The engine upserts fresh IBKR
    pulls of the last 10 days every poll, and since the source-precedence fix
    those pulls OVERWRITE values, so recovering from a data outage silently
    rewrites ~10 days of history.

    Measured 2026-08-20: 5 of the 9 fills inside the displayed windows had no
    marker of any kind. MSFT was the clearest case — a real buy and a real sell,
    both driven by a bollinger_rsi signal computed on a corrupt yfinance bar,
    both invisible once IBKR came back and corrected that bar. The user could
    not see their own trades on the chart they use to check their trading.

    Fills are ground truth and never need to be re-derived, so these markers do
    not move when the bars underneath them do. `fills` comes from the broker
    (web.server.build_executions), NOT from the journal: an `order_ok` row means
    SUBMITTED, and extended-hours limits genuinely strand unfilled — drawing
    those would repeat the mistake that put 115 phantom exit arrows on TSLA.
    """
    out = []
    seen: set = set()
    for f in fills or []:
        ts = _journal_ts(f.get("ts"))
        if ts is None:
            continue
        pos = index.searchsorted(ts, side="right") - 1
        if pos < 0 or index[pos] < window_start:
            continue
        buy = str(f.get("side", "")).lower() == "buy"
        qty, px = f.get("qty"), f.get("price")
        label = (f"{'BUY' if buy else 'SELL'} {qty:g} @ {px:.2f}"
                 if isinstance(qty, (int, float)) and isinstance(px, (int, float))
                 else ("BUY" if buy else "SELL"))
        # Two fills of the same side can land on one bar (a partial, or a
        # re-submit); one marker per bar per side is all the chart can show.
        key = (_chart_ts(index[pos]), buy)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "time": key[0],
            "position": "belowBar" if buy else "aboveBar",
            "color": _FILL_BUY_COLOR if buy else _FILL_SELL_COLOR,
            # circles, not arrows: arrows are signals, circles are fills
            "shape": "circle",
            "text": label,
        })
    return out


def _journal_ts(raw) -> pd.Timestamp | None:
    """Journal `ts` fields are epoch floats (relay.logger.append_event) except
    for a handful of legacy string entries — accept both, always UTC-aware.

    None means "no usable time" and callers skip the row on `is None`. NaT had
    to be folded into that: `pd.Timestamp(None)` returns NaT rather than raising,
    NaT is not None, so a row with a missing timestamp sailed past every caller's
    guard and then `searchsorted(NaT)` anchored it to an arbitrary bar. Reached
    the moment fills were drawn — build_executions leaves `ts` None whenever the
    broker reports no filled_at — but _journal_exit_markers had the same hole.
    """
    try:
        ts = pd.Timestamp(float(raw), unit="s", tz="UTC")
        return None if pd.isna(ts) else ts
    except (TypeError, ValueError):
        pass
    try:
        ts = pd.Timestamp(raw)
    except (TypeError, ValueError):
        return None
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _journal_exit_markers(sym: str, index, window_start) -> list[dict]:
    """Markers for account-level exits of `sym` from the relay journal, snapped
    to the bar containing (or last preceding) the event so Lightweight Charts
    can anchor them. Entries voided by a later `correction` event (journal
    hygiene: corrections append, never delete) are not drawn."""
    if not JOURNAL_FILE.exists():
        return []
    voided: set = set()
    events = []
    failures: list[float] = []
    target = sym.upper()
    for line in JOURNAL_FILE.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = e.get("event")
        if kind == "correction":
            voided.update(e.get("voids_ts") or [])
            continue
        if str(e.get("ticker", "")).upper() != target:
            continue
        if kind in _JOURNAL_EXIT_EVENTS:
            events.append(e)
        elif kind in _FAILED_CLOSE_EVENTS and e.get("action", "close") == "close":
            ts = _journal_ts(e.get("ts"))
            if ts is not None:
                failures.append(ts.timestamp())
    failures.sort()

    def _was_rejected(ts) -> bool:
        """True if a close failure for this ticker landed within the window just
        after `ts` — i.e. the journaled exit never reached the broker."""
        t = ts.timestamp()
        i = bisect.bisect_left(failures, t)
        return i < len(failures) and failures[i] - t <= _REJECTED_CLOSE_WINDOW_SEC

    markers = []
    seen: set = set()
    for e in events:
        if e.get("ts") in voided:
            continue
        ts = _journal_ts(e.get("ts"))
        if ts is None or _was_rejected(ts):
            continue
        pos = index.searchsorted(ts, side="right") - 1
        if pos < 0 or index[pos] < window_start:
            continue
        text = (f"ROT>{e.get('new_pick', '?')}" if e["event"] == "rotation_close"
                else "RECONCILE")
        # Several exits can snap to the same bar (the pass runs every poll while
        # a divergence lasts); one arrow per bar per kind is all the chart can
        # actually show.
        key = (_chart_ts(index[pos]), text)
        if key in seen:
            continue
        seen.add(key)
        markers.append({"time": key[0], "position": "aboveBar",
                        "color": "#f2a93b", "shape": "arrowDown", "text": text})
    return markers


def _build_chart_data(symbols: dict, n_bars: int, fills: list[dict] | None = None) -> dict:
    """`fills` is the broker's executed fills (web.server.build_executions rows:
    ts/symbol/side/qty/price). Optional, and deliberately passed IN rather than
    fetched here, so the chart builder never needs the network or broker
    credentials. The dashboard, which already talks to Alpaca, supplies them."""
    by_sym: dict[str, list[dict]] = {}
    for f in fills or []:
        by_sym.setdefault(str(f.get("symbol", "")).upper(), []).append(f)
    out = {}
    for sym, cfg in symbols.items():
        if not cfg.get("enabled", False):
            continue
        full_df = read_bars(sym, cfg["interval"])
        if full_df.empty:
            print(f"[skip] {sym}: no cached bars — has the engine polled it yet?")
            continue
        df = full_df.tail(n_bars).copy()
        close_full = full_df["close"].to_numpy(dtype=float)
        candles = [
            {"time": _chart_ts(ts),
             "open": round(float(r["open"]) if pd_notna(r["open"]) else float(r["close"]), 4),
             "high": round(float(r["high"]), 4), "low": round(float(r["low"]), 4),
             "close": round(float(r["close"]), 4)}
            for ts, r in df.iterrows()
        ]
        # Melbourne's AEDT->AEST fall-back transition (Apr) repeats one local
        # hour, which could otherwise collide two adjacent bars onto the same
        # _chart_ts value — Lightweight Charts requires strictly ascending
        # times. Twice-yearly, single-bar edge case; drop the duplicate rather
        # than let it silently break rendering.
        deduped = []
        for c in candles:
            if deduped and c["time"] <= deduped[-1]["time"]:
                continue
            deduped.append(c)
        candles = deduped
        markers = _markers_for_window(full_df, cfg, sym, df.index[0])
        markers += _journal_exit_markers(sym, full_df.index, df.index[0])
        markers += _fill_markers(by_sym.get(sym.upper(), []),
                                 full_df.index, df.index[0])
        markers.sort(key=lambda m: m["time"])

        bb_upper = bb_basis = bb_lower = rsi_pts = []
        bb_label = ""
        bb_spec = _strategy_spec(cfg, "bollinger_rsi")
        if bb_spec:
            ind = _bb_rsi_indicators(close_full, bb_spec["rsi_length"], bb_spec["bb_length"], bb_spec["bb_mult"])
            bb_upper = _series_points(df.index, ind["upper"])
            bb_basis = _series_points(df.index, ind["basis"])
            bb_lower = _series_points(df.index, ind["lower"])
            rsi_pts = _series_points(df.index, ind["rsi"])
            bb_label = f"BB {bb_spec['bb_length']}/{bb_spec['bb_mult']}  RSI {bb_spec['rsi_length']}"

        macd_hist = []
        macd_label = ""
        macd_spec = _strategy_spec(cfg, "macd_sma")
        if macd_spec:
            ind = _macd_sma_indicators(close_full, macd_spec["fast_len"], macd_spec["slow_len"],
                                       macd_spec["signal_len"], macd_spec["veryslow_len"])
            macd_hist = _hist_points(df.index, ind["hist"])
            macd_label = (f"MACD {macd_spec['fast_len']}/{macd_spec['slow_len']}/{macd_spec['signal_len']}"
                          f"  SMA{macd_spec['veryslow_len']}")

        out[sym] = {
            "interval": cfg["interval"],
            "candles": candles,
            "markers": markers,
            "bbUpper": bb_upper, "bbBasis": bb_basis, "bbLower": bb_lower,
            "rsi": rsi_pts,
            "macdHist": macd_hist,
            "bbLabel": bb_label, "macdLabel": macd_label,
        }
        print(f"{sym:8s} bars={len(df):4d}  last_close={candles[-1]['close']}  "
              f"signals={len(markers)}  {macd_label}  {bb_label}")
    return out


def pd_notna(v):
    import pandas as pd
    return pd.notna(v)

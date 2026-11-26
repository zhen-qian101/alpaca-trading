"""Precompute the live trigger state the research agent cannot compute itself.

WHY THIS EXISTS
---------------
`agent/research_prompt*.md` ask the model to rank candidates by which one fires a
live entry trigger NEXT — MACD-histogram zero-cross imminence, RSI/Bollinger
posture. But `run_research*.sh` launch `claude -p` with an `--allowed-tools` list
that has no `Bash`, so the agent cannot run anything against the engine's bar
cache. It was left eyeballing raw OHLCV pulled through the Alpaca MCP, and said
so itself in three consecutive runs ("I could NOT compute the actual live
indicator state ... I am eyeballing it").

Worse, the prompts describe the triggers only in words, so an agent has no choice
but to assume textbook parameters — and this book trades none of them:

  * the MACD here is **SMA-based** (`sma(fast) - sma(slow)`), not the standard
    EMA-based MACD;
  * `slow_len` is 35, not 26;
  * `rsi_length` is 6, not 14; Bollinger is 200 / 2.5, not 20 / 2.0;
  * a buy needs three more gates besides the cross, including a **lagged** close
    filter (`close[i - slow_len] > veryslow_ma[i]`) that no chart eyeball reveals.

Measured on live cached bars 2026-07-15, that gap is not academic: NVDA's RSI arm
reads 48.46 (NOT armed) on the real length-6 but 56.67 (armed) on textbook 14,
and META's MACD histogram sits at -0.185 (a hair from crossing) versus -1.14 on
textbook lengths — a ~6x error in exactly the "how imminent" quantity the whole
ranking is built on.

So this module computes that state with the engine's OWN functions
(`backtest.macd_sma`, `backtest.bollinger_rsi`) and the engine's OWN per-symbol
config, and writes it to JSON for the agent to `Read`. That mirrors the rule
`sigengine/engine.py` already states for itself — sharing the backtest loop
"guarantees the signal logic cannot drift". Research was the one layer still
drifting; this is what stops it.

WHY NOT JUST ALLOW `Bash`
-------------------------
This agent runs unattended on cron and sets the priorities that govern rotation —
i.e. what the account holds. Arbitrary shell is a large blast radius for that.
Precomputed state needs no new permission, is deterministic, is auditable after
the fact, and cannot drift from what the engine trades.

Usage:
    .venv/bin/python -m agent.live_state                    # -> agent/live_state.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from backtest.bollinger_rsi import compute_indicators as bb_indicators
from backtest.macd_sma import compute_indicators as macd_indicators
from relay.bars_db import read_bars

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "sigengine" / "config.json"
OUT_FILE = ROOT / "agent" / "live_state.json"

# How far back to look for refused signals when there is no previous
# live_state.json to use as a cursor (first run, or the file was cleared).
_BLOCKED_LOOKBACK_H = 24.0


def _f(x) -> float | None:
    """JSON-safe float (NaN/inf -> None, numpy -> python)."""
    if x is None:
        return None
    v = float(x)
    return v if np.isfinite(v) else None


def _macd_sma_state(close: np.ndarray, p: dict) -> dict:
    """Live MACD+SMA arm state, exactly as backtest.macd_sma._signals evaluates it.

    A buy there is `hist_co & (macd > 0) & (fast_ma > slow_ma) & (close_lag >
    veryslow_ma)` — the zero-cross is only the TRIGGER; the other three are gates.
    Reporting the gates matters as much as the distance: a histogram sitting on
    zero fires nothing while a gate is false, which is precisely the nuance an
    eyeballed chart read misses.
    """
    fast_len, slow_len = int(p["fast_len"]), int(p["slow_len"])
    signal_len, veryslow_len = int(p["signal_len"]), int(p["veryslow_len"])
    ind = macd_indicators(close, fast_len, slow_len, signal_len, veryslow_len)
    i = len(close) - 1
    hist, macd = ind["hist"], ind["macd"]
    fast_ma, slow_ma, veryslow_ma = ind["fast_ma"], ind["slow_ma"], ind["veryslow_ma"]

    # The strategy compares the close from `slow_len` bars ago against today's
    # veryslow MA — not today's close. Replicated, not approximated.
    close_lag = close[i - slow_len] if i - slow_len >= 0 else np.nan

    gates = {
        "macd_gt_0": bool(macd[i] > 0) if np.isfinite(macd[i]) else None,
        "fast_ma_gt_slow_ma": (bool(fast_ma[i] > slow_ma[i])
                               if np.isfinite(fast_ma[i]) and np.isfinite(slow_ma[i]) else None),
        "lagged_close_gt_veryslow_ma": (bool(close_lag > veryslow_ma[i])
                                        if np.isfinite(close_lag) and np.isfinite(veryslow_ma[i])
                                        else None),
    }
    hist_prev = hist[i - 1] if i >= 1 else np.nan
    return {
        "params": {"type": "macd_sma", "fast_len": fast_len, "slow_len": slow_len,
                   "signal_len": signal_len, "veryslow_len": veryslow_len,
                   "note": "SMA-based MACD (sma(fast)-sma(slow)), NOT the EMA-based textbook MACD"},
        "hist": _f(hist[i]),
        "hist_prev": _f(hist_prev),
        "hist_rising": (bool(hist[i] > hist_prev)
                        if np.isfinite(hist[i]) and np.isfinite(hist_prev) else None),
        "hist_distance_to_zero": _f(abs(hist[i])) if np.isfinite(hist[i]) else None,
        "hist_above_zero": bool(hist[i] > 0) if np.isfinite(hist[i]) else None,
        "macd": _f(macd[i]),
        "fast_ma": _f(fast_ma[i]),
        "slow_ma": _f(slow_ma[i]),
        "veryslow_ma": _f(veryslow_ma[i]),
        "lagged_close": _f(close_lag),
        "gates": gates,
        # True => a zero-cross on the next bar would actually fire a buy.
        # False => the cross is gated and imminence is irrelevant.
        "gates_all_pass": (all(v is True for v in gates.values())
                           if all(v is not None for v in gates.values()) else None),
    }


def _bollinger_rsi_state(close: np.ndarray, p: dict) -> dict:
    """Live Bollinger+RSI arm state, exactly as backtest.bollinger_rsi._signals
    evaluates it: a buy needs RSI to cross UP through 50 AND close to cross UP
    through the lower band ON THE SAME BAR — both, together, not either."""
    rsi_length, bb_length = int(p["rsi_length"]), int(p["bb_length"])
    bb_mult = float(p["bb_mult"])
    ind = bb_indicators(close, rsi_length, bb_length, bb_mult)
    i = len(close) - 1
    rsi, lower, basis, upper = ind["rsi"], ind["lower"], ind["basis"], ind["upper"]
    px = close[i]
    return {
        "params": {"type": "bollinger_rsi", "rsi_length": rsi_length,
                   "bb_length": bb_length, "bb_mult": bb_mult,
                   "note": "Wilder RSI at length 6 (NOT 14); Bollinger 200/2.5 (NOT 20/2.0)"},
        "rsi": _f(rsi[i]),
        "rsi_prev": _f(rsi[i - 1]) if i >= 1 else None,
        "rsi_distance_to_50": _f(abs(rsi[i] - 50.0)) if np.isfinite(rsi[i]) else None,
        "rsi_above_50": bool(rsi[i] > 50) if np.isfinite(rsi[i]) else None,
        "close": _f(px),
        "bb_lower": _f(lower[i]),
        "bb_basis": _f(basis[i]),
        "bb_upper": _f(upper[i]),
        "close_above_lower": (bool(px > lower[i]) if np.isfinite(lower[i]) else None),
        "pct_close_to_lower": (_f((px - lower[i]) / px * 100.0)
                               if np.isfinite(lower[i]) and px else None),
    }


def _near_trigger(entry: dict) -> dict | None:
    """Is this symbol about to fire, on the engine's OWN terms?

    The research agent decides bias on a ~3-hourly schedule while the engine
    fires on any 1h bar, so the expensive mistake is leaving a symbol `flat`
    while its next bar would have bought it: the gate refuses that buy, the
    signal is edge-triggered and never retried, and the setup is simply gone.
    (2026-07-29: MSFT's buy fired at 07:05 ET against a bias set 15.5h earlier,
    was blocked, never re-fired, and MSFT closed +15.51% the next session.)

    So this flags the cases where a contingency is worth writing — mechanically,
    from the same numbers the engine trades, rather than leaving "is it close?"
    to the agent's judgement of a chart it cannot see:

      macd_sma       all three non-trigger gates already pass and the histogram
                     is below zero but rising — one more bar like this crosses it.
      bollinger_rsi  RSI within 5 of the 50 line and price within 1.5% of the
                     lower band; the buy needs both to reclaim on ONE bar, so
                     "close on both" is the only meaningful proximity.
    """
    reasons = []
    m = entry.get("macd_sma") or {}
    if (m.get("gates_all_pass")            # the three non-trigger gates already pass
            and m.get("hist_above_zero") is False and m.get("hist_rising")):
        reasons.append(f"macd_sma: 3/3 gates pass, histogram {m.get('hist'):+.4f} "
                       f"below zero and rising — a zero-cross fires the buy")
    b = entry.get("bollinger_rsi") or {}
    d50, pct = b.get("rsi_distance_to_50"), b.get("pct_close_to_lower")
    if (d50 is not None and pct is not None and d50 <= 5.0 and abs(pct) <= 1.5):
        reasons.append(f"bollinger_rsi: RSI {b.get('rsi'):.1f} is {d50:.1f} from 50 "
                       f"and close is {pct:+.2f}% from the lower band — both must "
                       f"reclaim on one bar")
    return {"armed": True, "why": reasons} if reasons else None


def blocked_signals(since: datetime | None = None, journal: Path | None = None) -> dict:
    """Signals the risk gate REFUSED since the last research run.

    Nothing else closes this loop. The gate journals every refusal, but the
    research agent that caused it never saw them, so a bias that blocked a good
    trade looked exactly like a bias that blocked a bad one — and both were
    silently re-affirmed at the next refresh. This hands the agent its own
    record and the prompt requires a verdict on each.
    """
    from relay.config import JOURNAL_FILE

    path = Path(journal) if journal else JOURNAL_FILE
    cutoff = (since or datetime.now(timezone.utc) - timedelta(hours=_BLOCKED_LOOKBACK_H))
    out: list[dict] = []
    if not path.exists():
        return {"since": cutoff.isoformat(timespec="seconds"), "events": out,
                "note": "journal not found"}
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") != "risk_block":
            continue
        ts = r.get("ts")
        try:
            when = (datetime.fromtimestamp(float(ts), timezone.utc)
                    if isinstance(ts, (int, float)) else datetime.fromisoformat(str(ts)))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            continue
        out.append({"at": when.isoformat(timespec="seconds"),
                    "symbol": r.get("ticker") or r.get("symbol"),
                    "action": r.get("action"), "qty": r.get("qty"),
                    "reason": r.get("reason")})
    return {
        "since": cutoff.isoformat(timespec="seconds"),
        "count": len(out),
        "events": out,
        "note": ("Every one of these is a trade the engine wanted to make and the "
                 "risk gate refused, usually because YOUR bias said flat. A blocked "
                 "signal is NOT retried — it is edge-triggered and gone. Judge each "
                 "one: if the block was wrong, set the symbol long or write a "
                 "contingency; if it was right, say so in notes."),
    }


def _previous_run(out_file: Path) -> datetime | None:
    """`generated_at` of the last live_state — the cursor for "since the last
    research run". Missing or unreadable falls back to a fixed lookback."""
    try:
        ts = json.loads(Path(out_file).read_text()).get("generated_at")
        d = datetime.fromisoformat(ts)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def build_state(symbols: list[str] | None = None, positions=None,
                prev_run: datetime | None = None) -> dict:
    """Compute per-symbol live trigger state for every enabled symbol.

    `positions` and `prev_run` are injected (not fetched here) so this stays a
    pure, testable function; main() supplies the live ones.
    """
    cfg = json.loads(CONFIG_FILE.read_text())
    enabled = {s: c for s, c in cfg.get("symbols", {}).items() if c.get("enabled")}
    if symbols:
        want = {s.upper() for s in symbols}
        enabled = {s: c for s, c in enabled.items() if s.upper() in want}

    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "blocked_since_last_run": blocked_signals(since=prev_run),
        "source": "agent/live_state.py — computed with the engine's own "
                  "backtest.macd_sma / backtest.bollinger_rsi and sigengine/config.json",
        "authoritative": "These numbers ARE what the engine trades. Prefer them over "
                         "anything inferred from a chart or from textbook indicator "
                         "defaults, which this book does not use.",
        "positions": positions,
        "symbols": {},
    }

    for sym, scfg in enabled.items():
        interval = scfg.get("interval", "1h")
        entry: dict = {"interval": interval}
        try:
            df = read_bars(sym, interval)
        except Exception as e:  # pragma: no cover - defensive
            entry["error"] = f"read_bars failed: {e}"
            out["symbols"][sym] = entry
            continue
        if df.empty:
            entry["error"] = "no cached bars"
            out["symbols"][sym] = entry
            continue

        close = df["close"].to_numpy(dtype=float)
        entry["bars"] = int(len(close))
        entry["last_bar"] = str(df.index[-1])
        entry["close"] = _f(close[-1])

        for strat in scfg.get("strategies", []):
            kind = strat.get("type")
            # Each arm needs enough history for its longest lookback; below that
            # the indicator is NaN and a number here would be a lie.
            try:
                if kind == "macd_sma":
                    need = int(strat["veryslow_len"]) + int(strat["slow_len"]) + 5
                    entry[kind] = ({"error": f"insufficient bars ({len(close)} < {need})"}
                                   if len(close) < need else _macd_sma_state(close, strat))
                elif kind == "bollinger_rsi":
                    need = int(strat["bb_length"]) + 5
                    entry[kind] = ({"error": f"insufficient bars ({len(close)} < {need})"}
                                   if len(close) < need else _bollinger_rsi_state(close, strat))
            except Exception as e:  # pragma: no cover - defensive
                entry[kind] = {"error": f"{type(e).__name__}: {e}"}

        near = _near_trigger(entry)
        if near:
            entry["near_trigger"] = near
        out["symbols"][sym] = entry

    armed = sorted(k for k, v in out["symbols"].items() if v.get("near_trigger"))
    out["near_trigger_symbols"] = armed
    return out


def _live_positions() -> dict:
    """Current broker positions.

    The research prompt asks the agent to weigh "the currently-held name", but
    the Alpaca MCP server exposes NO positions tool at all — so no allowlist
    change could have fixed this. Fetching here and handing the result over as
    data is the only route that works.
    """
    try:
        from relay.alpaca_client import AlpacaClient
        from relay.config import Settings
        client = AlpacaClient(Settings.from_env())
        from relay.risk_filter import _is_equity_position
        rows = []
        for p in client.get_all_positions():
            # EQUITIES ONLY. This file is handed to the research model as
            # "the ground truth for what is held" (agent/research_prompt.md), and
            # an option row would arrive with qty = CONTRACTS and a negative
            # market value for a short — read as a 1-share holding, or a short
            # stock position the book is not allowed to have. The options sleeve
            # is reported separately; see relay.csp_manager.assignment_state.
            if not _is_equity_position(p):
                continue
            rows.append({
                "symbol": str(p.symbol), "qty": _f(p.qty),
                "avg_entry_price": _f(p.avg_entry_price),
                "current_price": _f(getattr(p, "current_price", None)),
                "unrealized_pl": _f(getattr(p, "unrealized_pl", None)),
                "unrealized_plpc": _f(getattr(p, "unrealized_plpc", None)),
            })
        return {"held": rows, "count": len(rows),
                "note": "empty list = flat (this is authoritative, not an error)"}
    except Exception as e:
        # Never fail the research run over this — say so honestly instead, so the
        # agent discounts its own position-dependent reasoning rather than
        # assuming flat.
        return {"held": None, "error": f"{type(e).__name__}: {e}",
                "note": "position fetch FAILED — treat holdings as UNKNOWN, not flat"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.parse_args()

    positions = _live_positions()
    state = build_state(None, positions, prev_run=_previous_run(OUT_FILE))
    text = json.dumps(state, indent=2)
    OUT_FILE.write_text(text)
    n = len(state["symbols"])
    held = (state.get("positions") or {}).get("held")
    blocked = state.get("blocked_since_last_run", {})
    armed = state.get("near_trigger_symbols") or []
    print(f"live_state: {n} symbol(s) -> {OUT_FILE} "
          f"(positions: {len(held) if held is not None else 'UNKNOWN'}; "
          f"{blocked.get('count', 0)} refused signal(s) since the last run"
          + (f"; ARMED: {', '.join(armed)}" if armed else "") + ")")


if __name__ == "__main__":
    main()

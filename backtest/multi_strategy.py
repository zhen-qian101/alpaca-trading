"""Multi-strategy dispatch: run more than one independent signal source (the
EMA trend-follower, the Bollinger+RSI mean-reversion strategy, and/or the
MACD+SMA-200 strategy — see STRATEGY_RUNNERS for the full, extensible list)
over the same symbol's bars, and merge their events.

This is the SHARED logic behind "dual-candidate rotation" — used by BOTH the
live signal engine (`sigengine/engine.py`) and the dashboard chart
(`backtest/chart_view.py`). Sharing it means the chart's markers show exactly
what live would have done — no drift, same principle as
`backtest/ema_scaled_rolling.py` being the canonical loop shared with live.

Each strategy simulates its own hypothetical position from a flat start and
emits its own {ts, action, price, reason} events; events from all of a
symbol's strategies are merged in time order. Whichever strategy's entry
signal fires FIRST wins the symbol's rotation slot (same "first candidate
whose buy signal fires while flat enters" precedent already used for
cross-symbol priority rotation in decisions.json), and an exit from EITHER
strategy's own rules closes the real/simulated position (an OR-exit — more
willing to get out, never less safe).

BACKWARD COMPAT: a symbol config with no "strategies" key normalizes to a
single implicit EMA strategy using its legacy flat fast/slow/window keys.
"""
from __future__ import annotations

import pandas as pd

from backtest.bollinger_rsi import run_with_events as _bollinger_rsi_run_with_events
from backtest.ema_scaled_rolling import run_with_events as _ema_run_with_events
from backtest.macd_sma import run_with_events as _macd_sma_run_with_events

STRATEGY_RUNNERS = {
    "ema": lambda df, p, deploy, stop: _ema_run_with_events(
        df, p["fast"], p["slow"], p["window"], deploy=deploy, stop_pct=stop)[2],
    "bollinger_rsi": lambda df, p, deploy, stop: _bollinger_rsi_run_with_events(
        df, p["rsi_length"], p["bb_length"], p["bb_mult"], deploy=deploy, stop_pct=stop)[2],
    "macd_sma": lambda df, p, deploy, stop: _macd_sma_run_with_events(
        df, p["fast_len"], p["slow_len"], p["signal_len"], p["veryslow_len"], deploy=deploy, stop_pct=stop)[2],
}


# Tie-break for events sharing a timestamp: flatten before you open. See
# run_symbol_strategies.
_ACTION_RANK = {"close": 0, "sell": 1, "buy": 2}


def symbol_strategies(cfg: dict) -> list[dict]:
    """Normalize a symbol config into a list of strategy specs."""
    if "strategies" in cfg:
        return cfg["strategies"]
    return [{"type": "ema", "fast": cfg["fast"], "slow": cfg["slow"], "window": cfg["window"]}]


def run_symbol_strategies(df: pd.DataFrame, cfg: dict, sym: str = "") -> list[dict]:
    """Run every strategy configured for this symbol over `df`, tag each event
    with which strategy produced it, and return them merged in time order."""
    events = []
    for spec in symbol_strategies(cfg):
        runner = STRATEGY_RUNNERS.get(spec.get("type"))
        if runner is None:
            raise ValueError(f"{sym}: unknown strategy type {spec.get('type')!r}")
        ev = runner(df, spec, cfg["deploy"], cfg["stop"])
        for e in ev:
            e["strategy"] = spec["type"]
        events.extend(ev)
    # AN EXIT AT A TIMESTAMP IS PROCESSED BEFORE AN ENTRY AT THE SAME ONE.
    #
    # Sorting on ts alone left same-bar ties to Python's stable sort, i.e. to
    # whichever strategy happens to appear first in the symbol's `strategies`
    # list in sigengine/config.json — a config ordering silently deciding
    # trading semantics. Every symbol currently lists macd_sma first, which is
    # safe by luck; reversed, a bollinger_rsi `buy` at bar T would be fired
    # before a macd_sma `close` at the same T, fire_event would skip the buy on
    # `held > 0`, and the close would then flatten the book — a legitimate
    # strategy handoff turned into an exit with no re-entry (audit 2026-08-27,
    # E8). Ranking exits first makes the handoff work in either config order,
    # and matches the book's standing rule that reducing exposure comes first.
    events.sort(key=lambda e: (e["ts"], _ACTION_RANK.get(e.get("action"), 9)))
    return events

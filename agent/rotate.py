"""Rotation audit — run AFTER the (intraday or daily) research writes decisions.json.

This is a STATUS-ONLY audit. It does NOT place or close any orders.

The research layer sets `bias=long` on multiple candidates, each with a
`priority` (higher = more preferred). Rotation between candidates is
**signal-driven**, not research-driven: the relay (relay/order_manager.py)
rotates out of the current holding only when a *higher-priority* candidate's
EMA buy signal fires, or the held symbol's own exit signal fires. Re-running
research and re-ranking priorities therefore does NOT trade. This module just
prints the held position vs. the ranked candidate list so the post-research log
shows the system's current state.

Edge case (by design): if the held symbol is pruned from the watchlist entirely,
it is NOT force-closed here — it stays held until its own chart fires an exit
signal. Flatten manually if you want out sooner.

Usage:
    .venv/bin/python -m agent.rotate

Exits with code 0 always; prints a summary.
"""
from __future__ import annotations

import argparse
import json

from relay.alpaca_client import AlpacaClient
from relay.config import get_settings


def _candidates(path: str) -> list[tuple[str, int]]:
    """Return [(symbol, priority)] for every bias=long symbol, sorted by
    priority descending (highest preference first)."""
    try:
        d = json.loads(open(path).read())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    bias = d.get("bias") or {}
    pri = d.get("priority") or {}
    longs = []
    for sym, b in bias.items():
        if b == "long":
            try:
                p = int(pri.get(sym, 1))
            except (TypeError, ValueError):
                p = 1
            longs.append((sym.upper(), max(p, 0)))
    return sorted(longs, key=lambda x: -x[1])


def main() -> None:
    ap = argparse.ArgumentParser(description="Rotation audit (status only — no orders)")
    ap.parse_args()

    settings = get_settings()
    client = AlpacaClient(settings)
    cands = _candidates("decisions/decisions.json")

    try:
        positions = list(client.get_all_positions())
    except Exception as e:
        print(f"✗ could not fetch positions: {e}")
        positions = []

    held = [str(getattr(p, "symbol", "")).upper() for p in positions]
    held = [s for s in held if s]

    print(f"candidates (long, by priority): {cands or '(none — all flat/defensive)'}")
    if held:
        for sym in held:
            tag = "still a candidate" if any(sym == c[0] for c in cands) else "NO LONGER a candidate (holds until its own exit signal)"
            print(f"held: {sym} — {tag}")
    else:
        print("held: (none — flat; first candidate buy signal to fire will enter)")

    print("no orders placed (rotation is signal-driven; this is an audit only).")


if __name__ == "__main__":
    main()
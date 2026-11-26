"""CLI: load decisions/decisions.json, clamp to hard ceilings, write it back.

Run after the Claude harness writes the file:
    .venv/bin/python -m agent.validate_decisions [--watchlist AAPL,MSFT]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .decisions_schema import validate_decision

DECISIONS_PATH = Path("decisions/decisions.json")


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate + clamp decisions.json")
    ap.add_argument("--watchlist", default="", help="comma-separated fallback watchlist")
    ap.add_argument("--path", default=str(DECISIONS_PATH))
    args = ap.parse_args()
    path = Path(args.path)
    raw = json.loads(path.read_text())
    wl = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    cleaned = validate_decision(raw, wl or None)
    path.write_text(json.dumps(cleaned, indent=2) + "\n")
    print(f"✅ validated + clamped {path}")
    print(json.dumps(cleaned, indent=2))


if __name__ == "__main__":
    main()
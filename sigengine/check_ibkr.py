"""IBKR pre-flight check — does the Gateway answer, and are we subscribed?

Run this BEFORE relying on IBKR as the signal-engine data source. It connects,
requests live market data type, and pulls a few 5-min AAPL bars. Exit codes:
  0  connected + got bars (you're subscribed — IBKR is usable as primary)
  1  connected but no bars (sign the Market Data API Acknowledgement + add
     exchange subscriptions; the engine's yfinance fallback will cover you)
  2  could not connect (start IB Gateway via deploy/run_ibgateway.sh first)

    PYTHONPATH=. .venv/bin/python -m sigengine.check_ibkr
"""
from __future__ import annotations

import sys

from sigengine.ibkr import connect, fetch_bars


def main() -> int:
    ib = connect("AAPL")
    if ib is None:
        print("FAIL: could not connect to IBKR. Start IB Gateway (paper) via")
        print("  ./deploy/run_ibgateway.sh")
        print("  (and ensure IBC + IB Gateway are installed; see deploy/ibc.ini).")
        return 2
    print(f"OK: connected to IBKR ({ib.clientId if hasattr(ib, 'clientId') else 'client'})")

    df = fetch_bars("AAPL", "5min")
    if df is None or df.empty:
        print("PARTIAL: connected but no bars returned — likely not subscribed.")
        print("  Sign the Market Data API Acknowledgement in Client Portal")
        print("  (Settings -> Market Data Subscriptions) and confirm the equity")
        print("  network subscriptions (Network A/B/C, ~$4.50/mo non-pro).")
        print("  Until then the engine uses the yfinance fallback automatically.")
        return 1

    print(f"OK: fetched {len(df)} AAPL 5-min bars from IBKR.")
    print(f"  last bar: {df.index[-1]}  close={df['close'].iloc[-1]:.2f}")
    print("IBKR is usable as the primary data source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
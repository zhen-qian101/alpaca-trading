"""CLI entry point for the weekly cash-secured put. See agent/run_csp.sh.

    PYTHONPATH=. .venv/bin/python -m relay.csp_run            # decide + act
    PYTHONPATH=. .venv/bin/python -m relay.csp_run --dry-run  # decide, never send

--dry-run is stronger than `csp.shadow`: shadow is the STRATEGY refusing to
trade because it cannot pay, and it still journals; --dry-run is the OPERATOR
inspecting, and it forces shadow on for this invocation whatever the config
says. Neither can place an order by accident.
"""
from __future__ import annotations

import argparse
import sys

from .alpaca_client import AlpacaClient
from .config import get_settings
from .csp_manager import CSP_CONFIG_FILE, config, load_policy, run
from .models import OrderRequest
from .options_client import OptionsClient
from .order_manager import OrderManager


def main() -> None:
    ap = argparse.ArgumentParser(description="Weekly SOXL cash-secured put")
    ap.add_argument("--dry-run", action="store_true",
                    help="choose and journal the trade, never submit it")
    args = ap.parse_args()

    # POLICY COMES FROM decisions/csp.json, NOT decisions.json — the research
    # harness rewrites the latter wholesale several times a day and deleted a
    # csp block placed there within hours (2026-08-27). See csp_manager's
    # CSP_CONFIG_FILE comment.
    decisions = load_policy()
    cfg = config(decisions)
    if args.dry_run:
        decisions = {"csp": {**(decisions.get("csp") or {}), "shadow": True}}

    if not cfg["enabled"]:
        print(f"  [csp] disabled ({CSP_CONFIG_FILE.name} enabled is not true) — "
              f"nothing to do")
        return

    settings = get_settings()
    client = AlpacaClient(settings)
    equity_positions = client.get_all_positions()
    # Assigned shares are sold THROUGH THE RELAY, so the sale is risk-gated and
    # journaled like every other close in this account. A fresh OrderManager
    # protects nothing, so it can close the sleeve's own shares.
    om = OrderManager(client)

    def sell_shares(symbol: str, qty: float) -> str:
        return str(om.handle(OrderRequest(ticker=symbol, action="close",
                                          strategy="csp-assignment-exit")).get("status"))

    dec = run(OptionsClient(settings), equity_positions, decisions,
              sell_shares=None if args.dry_run else sell_shares)
    # a refusal is a normal outcome, not a failure — never make cron shout
    sys.exit(0 if dec.action in ("sell", "shadow", "skip") else 1)


if __name__ == "__main__":
    main()

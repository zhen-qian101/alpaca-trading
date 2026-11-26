"""Standalone Alpaca connectivity check. Run after filling in .env:

    python relay/check_account.py
"""
from __future__ import annotations

from .alpaca_client import AlpacaClient
from .config import get_settings


def main() -> None:
    s = get_settings()
    print(f"paper trading: {s.paper}")
    acct = AlpacaClient(s).get_account()
    print(f"status:        {getattr(acct, 'status', '?')}")
    print(f"equity:        ${getattr(acct, 'equity', '?')}")
    print(f"buying power:  ${getattr(acct, 'buying_power', '?')}")
    print(f"cash:          ${getattr(acct, 'cash', '?')}")
    status = getattr(acct, "status", "")
    status_str = getattr(status, "value", str(status)).lower()
    print("\n✅ Alpaca connection OK" if status_str == "active"
          else f"\n⚠️  account status {status} — check keys/permissions")


if __name__ == "__main__":
    main()
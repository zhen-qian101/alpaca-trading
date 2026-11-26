"""Pydantic model for an order request routed through the relay library.

The signal engine (`sigengine/engine.py`) constructs an `OrderRequest` for each
fresh strategy event and hands it to `relay.order_manager.OrderManager.handle()`
IN-PROCESS — there is no HTTP layer anymore (TradingView was removed). Pydantic
validates the shape so a malformed request raises a clean error before any
handler runs.

Fields:
  ticker      symbol as the strategy emits it (equities uppercase; crypto may
              arrive as BTCUSD and is canonicalized to BTC/USD in handle()).
  action      "buy" | "sell" | "close"
  qty         shares/contracts to trade (None for "close" — liquidates all).
  order_type  "market" (default) | "limit"
  limit_price required when order_type == "limit"
  tif          "day" (default) | "gtc"
  strategy    free-form label for the journal (which strategy/agent fired this).
  price       the signal's trigger price (e.g. the bar close that fired a buy),
              if known. Only used by relay.risk_filter.check() to evaluate
              decisions.json "contingencies" (pre-approved breakout/breakdown
              levels that can override a flat/short bias) — not sent to the
              broker, purely an input to the risk gate.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class OrderRequest(BaseModel):
    ticker: str
    action: Literal["buy", "sell", "close"]
    qty: float | None = None
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    tif: Literal["day", "gtc"] = "day"
    strategy: str = "default"
    price: float | None = None

    model_config = {"extra": "ignore"}
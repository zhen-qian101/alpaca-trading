"""Thin wrapper over alpaca-py's OPTIONS surface — a sibling of alpaca_client.

Deliberately a separate module and a separate client object, not a branch inside
AlpacaClient/OrderManager. The relay's value is the equity invariants baked into
OrderManager.handle(): one position at a time, all-in/all-out coercion,
extended-hours limit pricing off a stock quote, rotation by decisions.json
priority. An option leg satisfies none of them, and the 2026-08-27 study found
that letting an option anywhere near those helpers is actively dangerous — an
open short put read as "the other position held" and the first equity signal
would have bought it back at market.

So: options live here, the equity path stays equity-only (see
order_manager._is_equity_position), and the ONE thing the two share is the cash
reservation (see csp_manager.pledged_collateral).

Reuses AlpacaClient's two safety behaviours rather than reimplementing them:
_disable_ambiguous_retries (never let the SDK silently re-POST an order after a
504) and _apply_request_timeout. Neither is option-specific and both are
safety-critical.
"""
from __future__ import annotations

from dataclasses import dataclass

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderSide,
    PositionIntent,
    TimeInForce,
)
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest

from .alpaca_client import AlpacaClient
from .config import Settings


@dataclass(frozen=True)
class PutQuote:
    """One candidate put, with everything a sizing decision needs."""
    symbol: str                 # OCC, e.g. SOXL260904P00090000
    strike: float
    expiry: str                 # YYYY-MM-DD
    bid: float
    ask: float
    underlying: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.bid

    @property
    def spread_frac(self) -> float:
        """Bid-ask spread as a fraction of mid. The far-OTM SOXL weekly book is
        thin and uneven — measured 2026-08-26, strike 95 quoted 7% wide while
        strike 97 quoted 52% — and crossing half of that every week is a large
        fraction of the whole edge. Liquidity is a selection criterion here, not
        a detail."""
        # A ONE-SIDED QUOTE IS MAXIMALLY WIDE, NOT PERFECTLY TIGHT. With ask
        # missing, `mid` falls back to bid and this computed (0 - bid)/bid =
        # -1.0 — so an unquotable contract looked TIGHTER than every real one
        # and sailed through the liquidity gate, and would even be named as the
        # "tightest" strike in a too-wide-to-trade refusal. put_chain happens to
        # drop ask<=0 first, but pick_strike is a separate public function with
        # its own contract; the value has to be right wherever the chain came
        # from.
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return 1.0
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else 1.0

    @property
    def collateral(self) -> float:
        """Cash Alpaca pledges for a cash-secured short: strike x 100.

        Alpaca has NO naked-put tier at any approval level — level 3 is spreads —
        so this is the requirement, not a choice. The premium received nets off
        it, but budget to the gross: it is within a rounding error and erring
        high is the safe direction.
        """
        return self.strike * 100.0

    @property
    def otm_frac(self) -> float:
        return (self.underlying - self.strike) / self.underlying if self.underlying else 0.0


class OptionsClient:
    def __init__(self, settings: Settings):
        kwargs = dict(api_key=settings.alpaca_key,
                      secret_key=settings.alpaca_secret,
                      paper=settings.paper)
        if settings.base_url:
            kwargs["url_override"] = settings.base_url
        self.client = TradingClient(**kwargs)
        # borrowed wholesale from the equity client — see this module's docstring
        AlpacaClient._disable_ambiguous_retries(self.client)
        AlpacaClient._apply_request_timeout(self.client)
        self._data = OptionHistoricalDataClient(settings.alpaca_key, settings.alpaca_secret)

    # ---------------------------------------------------------------- reads
    def expirations(self, underlying: str, gte: str, lte: str) -> list[str]:
        """Sorted distinct expiry dates with listed PUTs in the window."""
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying], type=ContractType.PUT,
            expiration_date_gte=gte, expiration_date_lte=lte,
            status=AssetStatus.ACTIVE, limit=10000)
        out = {c.expiration_date for c in (self.client.get_option_contracts(req).option_contracts or [])}
        return sorted(str(d) for d in out)

    def put_chain(self, underlying: str, expiry: str, lo: float, hi: float,
                  underlying_price: float) -> list[PutQuote]:
        """Quoted puts for one expiry between two strikes, nearest-strike first.

        Contracts with no two-sided quote are dropped rather than priced off a
        stale last trade — an unquotable far-OTM weekly is one we cannot sell at
        a knowable price, which is the same thing as one we do not want.
        """
        req = OptionChainRequest(
            underlying_symbol=underlying, type=ContractType.PUT,
            expiration_date=expiry, strike_price_gte=lo, strike_price_lte=hi)
        snaps = self._data.get_option_chain(req) or {}
        out: list[PutQuote] = []
        for sym, snap in snaps.items():
            q = getattr(snap, "latest_quote", None)
            if q is None:
                continue
            bid, ask = float(getattr(q, "bid_price", 0) or 0), float(getattr(q, "ask_price", 0) or 0)
            # A CROSSED quote (ask < bid) is as unusable as a missing one, and
            # worse for us: it yields a NEGATIVE spread fraction that wins the
            # liquidity gate outright.
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            out.append(PutQuote(symbol=sym, strike=_strike_from_occ(sym), expiry=expiry,
                                bid=bid, ask=ask, underlying=underlying_price))
        return sorted(out, key=lambda p: -p.strike)

    def open_option_orders(self) -> list:
        """Working OPTION orders. Needed because a sell-to-open limit that has
        not filled yet is a commitment with no position behind it, and the
        equity path deliberately ignores option orders (see
        order_manager._open_order_symbols) so nothing else would notice it."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)) or []
        out = []
        for o in orders:
            ac = str(getattr(getattr(o, "asset_class", None), "value", "")).lower()
            sym = str(getattr(o, "symbol", "") or "")
            if ac == "us_option" or _looks_like_occ(sym):
                out.append(o)
        return out

    def option_positions(self) -> list:
        """Only the OPTION positions. The equity path filters these OUT (see
        order_manager._is_equity_position); this is the other half of that."""
        out = []
        for p in (self.client.get_all_positions() or []):
            ac = str(getattr(getattr(p, "asset_class", None), "value", "")).lower()
            if ac == "us_option":
                out.append(p)
        return out

    def account_snapshot(self) -> dict:
        """The two numbers position sizing needs, in ONE broker call.

        `options_buying_power` is THE gate for opening a short put — not
        `buying_power`. On the old account buying_power read $19,392 (4x margin)
        while options_buying_power read $4,848 (cash); reading the wrong field
        would approve a trade the broker then rejects. Confirmed against
        Alpaca's docs and the live account 2026-08-27.

        `equity` is what the RISK budget is a fraction of, and it is a different
        question from "can I pay". Cash says how many contracts the broker will
        let us sell; equity says how many we should. Fetched together because
        sizing needs both and a weekly job should not make two round trips to
        read one account object.
        """
        acct = self.client.get_account()
        bp = getattr(acct, "options_buying_power", None)
        return {
            "options_buying_power": float(bp) if bp is not None else float(acct.cash),
            "equity": float(getattr(acct, "equity", 0) or 0),
        }

    def options_buying_power(self) -> float:
        """Just the cash gate. Kept because callers outside sizing want only
        this; run() uses account_snapshot so it reads the account once."""
        return self.account_snapshot()["options_buying_power"]

    # --------------------------------------------------------------- writes
    def sell_put_to_open(self, occ_symbol: str, limit_price: float, qty: int = 1):
        """SELL TO OPEN one put, as a LIMIT order. Never a market order.

        A market fill on a thin far-OTM weekly is unbounded in the wrong
        direction — the strikes in question quote as wide as 52% of mid. Options
        are DAY-only and cannot trade outside regular hours, so there is no
        extended-hours variant of this call.
        """
        px = round(float(limit_price), 2)
        # A SELL limit at $0.00 is a market sell in disguise, and alpaca-py
        # validates neither the price nor the quantity — verified: it accepts
        # 0.0, -1.0 and a fractional qty without complaint.
        if px <= 0:
            raise ValueError(f"refusing to sell {occ_symbol} at a limit of {px}")
        if int(qty) != qty or qty < 1:
            raise ValueError(f"refusing to sell {qty} contracts of {occ_symbol}")
        return self.client.submit_order(LimitOrderRequest(
            symbol=occ_symbol, qty=int(qty), side=OrderSide.SELL,
            type="limit", limit_price=px,
            time_in_force=TimeInForce.DAY,
            position_intent=PositionIntent.SELL_TO_OPEN))

    def buy_put_to_close(self, occ_symbol: str, limit_price: float, qty: int = 1):
        """BUY TO CLOSE. Note this ALSO consumes options buying power, so an
        account funded to exactly the collateral cannot buy its way out of a
        losing put — its only exit is to accept assignment. That is a funding
        argument, and it is why csp_manager keeps a close-out reserve."""
        return self.client.submit_order(LimitOrderRequest(
            symbol=occ_symbol, qty=qty, side=OrderSide.BUY,
            type="limit", limit_price=round(float(limit_price), 2),
            time_in_force=TimeInForce.DAY,
            position_intent=PositionIntent.BUY_TO_CLOSE))


def _looks_like_occ(sym: str) -> bool:
    s = (sym or "").upper()
    if len(s) < 16 or "/" in s:
        return False
    tail = s[-15:]
    return tail[:6].isdigit() and tail[6] in "CP" and tail[7:].isdigit()


def _strike_from_occ(sym: str) -> float:
    """SOXL260904P00090000 -> 90.0. The last 8 digits are the strike in
    thousandths of a dollar."""
    return int(sym[-8:]) / 1000.0

"""Thin wrapper over alpaca-py's TradingClient.

Keeps the rest of the relay decoupled from the SDK so we can swap brokers later.
"""
from __future__ import annotations

from alpaca.common.exceptions import APIError
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoLatestQuoteRequest,
    CryptoLatestTradeRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)

from .config import Settings
from .risk_filter import is_crypto_symbol


def is_definitive_rejection(exc: Exception) -> bool:
    """True if `exc` means the broker categorically did NOT create the order.

    Only an HTTP 4xx from Alpaca qualifies: the request reached the broker and
    was refused, so nothing exists on their side and a re-submit cannot produce
    a duplicate fill. Everything else — connection errors, timeouts, 5xx — is
    AMBIGUOUS: the order may have been accepted and filled while the response
    was lost, and without an idempotency key a blind re-submit could open a
    second position. Those must never be retried automatically.

    Lives here (not in order_manager) so SDK-specific exception knowledge stays
    behind the client wrapper — see this module's docstring.
    """
    if not isinstance(exc, APIError):
        return False
    status = exc.status_code
    return status is not None and 400 <= status < 500


class AlpacaClient:
    def __init__(self, settings: Settings):
        kwargs = dict(
            api_key=settings.alpaca_key,
            secret_key=settings.alpaca_secret,
            paper=settings.paper,
        )
        if settings.base_url:
            kwargs["url_override"] = settings.base_url
        self.client = TradingClient(**kwargs)
        self._disable_ambiguous_retries(self.client)
        self._apply_request_timeout(self.client)
        self.paper = settings.paper
        # Alpaca's own market-data clients, used ONLY to price extended-hours/
        # overnight limit orders (see get_latest_quote) — unrelated to the
        # IBKR/yfinance bar sources sigengine uses for signal generation.
        self._stock_data = StockHistoricalDataClient(settings.alpaca_key, settings.alpaca_secret)
        self._crypto_data = CryptoHistoricalDataClient(settings.alpaca_key, settings.alpaca_secret)

    # Codes alpaca-py may safely re-send. 429 means the request was REJECTED
    # outright (rate limited), so nothing was created and a retry cannot
    # duplicate anything. That is the ONLY one left in.
    _SAFE_RETRY_CODES = [429]

    @staticmethod
    def _disable_ambiguous_retries(client) -> None:
        """Stop the SDK from silently re-POSTing an order after an ambiguous
        failure.

        is_definitive_rejection() (below) implements the rule that a 5xx or a
        timeout must never be retried: the order may have been accepted and
        filled while the response was lost, and with no idempotency key a blind
        re-submit opens a second position. But that rule is enforced in
        _submit_with_retry, one layer ABOVE the SDK — and alpaca-py runs its own
        retry loop underneath it. Its defaults (alpaca.common.constants) are
        DEFAULT_RETRY_EXCEPTION_CODES = [429, 504], 3 attempts, 3s apart, so a
        504 on POST /v2/orders is re-sent up to three more times before our code
        ever sees an error — exactly the double-fill this codebase refuses to
        risk. 504 is a GATEWAY timeout: the broker may well have booked the
        order.

        It cannot be passed in: RESTClient.__init__ accepts retry_exception_codes,
        but TradingClient.__init__ does not forward it (its params are
        api_key/secret_key/oauth_token/paper/raw_data/url_override), so the
        attribute is set directly afterwards. Verified against alpaca-py 0.43.4.

        Best-effort and deliberately quiet on failure: this is reaching into SDK
        internals, so a future rename must not stop the client from being
        built — it just restores the old, riskier default.
        """
        if hasattr(client, "_retry_codes"):
            client._retry_codes = list(AlpacaClient._SAFE_RETRY_CODES)

    # (connect, read) seconds for every HTTP call to Alpaca.
    #
    # The FETCH phase of a poll is carefully bounded (sigengine.engine's
    # _IBKR_CALL_TIMEOUT = 20s and _FETCH_PHASE_TIMEOUT = 90s) but the FIRE
    # phase — the part that actually places and closes orders — runs
    # synchronously on the main thread with no bound at all, because alpaca-py
    # passes no timeout to requests and requests defaults to WAITING FOREVER.
    # A network drop that leaves a half-open socket (no RST — exactly the class
    # of failure seen on 2026-08-19) parks the engine in recv() until Linux TCP
    # keepalive intervenes, which by default is ~2h11m. Through all of it the
    # process is alive and the dashboard's /proc check reports "engine live",
    # while no stop is enforced and no signal fires.
    #
    # Read timeout is per-socket-read, not for the whole request, so 30s is
    # generous for an order round-trip while still failing in seconds rather
    # than hours. A timeout raises requests.Timeout, which
    # is_definitive_rejection() correctly classes as NON-definitive, so
    # _submit_with_retry propagates it instead of re-sending — an order that
    # may have been booked is never blindly duplicated.
    _REQUEST_TIMEOUT = (10, 30)

    @classmethod
    def _apply_request_timeout(cls, client) -> None:
        """Give every SDK HTTP call a deadline.

        requests.Session has no default-timeout setting, and alpaca-py builds
        its request kwargs internally, so the session's request() is wrapped to
        fill in a timeout when the caller did not pass one. setdefault, not
        override: if a future SDK version starts passing its own timeout, its
        choice wins.

        Idempotent (a second call is a no-op) and best-effort — reaching into
        SDK internals must never stop the client being constructed.
        """
        session = getattr(client, "_session", None)
        if session is None or getattr(session, "_relay_timeout_wrapped", False):
            return
        original = session.request

        def request_with_timeout(*args, **kwargs):
            kwargs.setdefault("timeout", cls._REQUEST_TIMEOUT)
            return original(*args, **kwargs)

        try:
            session.request = request_with_timeout
            session._relay_timeout_wrapped = True
        except Exception:  # pragma: no cover - exotic session object
            pass

    # --- account / positions -------------------------------------------------
    def get_account(self):
        return self.client.get_account()

    def get_all_positions(self):
        return self.client.get_all_positions()

    # --- open orders ---------------------------------------------------------
    def get_open_orders(self, symbol: str | None = None) -> list:
        """Orders still working at the broker, optionally for one symbol.

        Needed because a position's shares can be fully committed to an order
        that has not filled: Alpaca reports the position as qty=13 while
        qty_available=0 and held_for_orders=13, and any further sell is
        rejected with code 40310000 ("insufficient qty available"). The
        reconcile pass consults this to tell "the exit is still working" apart
        from "the exit was missed" — see sigengine.engine._reconcile_positions.
        """
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN,
                               symbols=[symbol] if symbol else None)
        return list(self.client.get_orders(req) or [])

    def cancel_order(self, order_id) -> None:
        """Cancel one working order by id. Used only to release shares pinned
        by an exit that will not fill, so the position can be flattened at
        market — never to abandon an exit."""
        self.client.cancel_order_by_id(str(order_id))

    # --- market data --------------------------------------------------------
    @staticmethod
    def _epoch(ts) -> float | None:
        """SDK timestamps are tz-aware datetimes; None survives as None."""
        try:
            return None if ts is None else ts.timestamp()
        except (AttributeError, TypeError, ValueError):
            return None

    def get_latest_quote(self, symbol: str) -> tuple[float, float, float | None]:
        """Returns (bid_price, ask_price, quote_epoch_ts) for `symbol`. Used to
        compute a marketable limit price for extended-hours/overnight orders —
        Alpaca rejects market orders outside the regular 9:30-16:00 ET session
        (see order_manager.handle()'s extended-hours branch), so those orders
        need a real limit price derived from a live quote instead. The
        timestamp lets _reference_price judge whether the quote or the last
        trade is the stale one when they disagree."""
        if is_crypto_symbol(symbol):
            quote = self._crypto_data.get_crypto_latest_quote(
                CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            )[symbol]
        else:
            quote = self._stock_data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            )[symbol]
        return float(quote.bid_price), float(quote.ask_price), self._epoch(quote.timestamp)

    def get_latest_trade(self, symbol: str) -> tuple[float, float | None]:
        """Returns (last_traded_price, trade_epoch_ts) for `symbol` — the
        sanity anchor for extended-hours limit pricing (see
        order_manager._extended_hours_limit_price).

        Why a second source at all: outside regular hours the default IEX quote
        feed routinely returns a one-sided quote (a real bid, ask=0) or a very
        wide stale one, and get_latest_quote reports those verbatim. The last
        trade is the more trustworthy reference in that window, so it is used
        both to validate the quote and as the fallback when the quote is
        unusable. The timestamp exposes the opposite failure: after a weekend/
        overnight gap the "last trade" can itself be a session-old print (e.g.
        Friday's close on a Monday-overnight order), which _reference_price
        must not treat as the authority over a fresh two-sided quote."""
        if is_crypto_symbol(symbol):
            trade = self._crypto_data.get_crypto_latest_trade(
                CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            )[symbol]
        else:
            trade = self._stock_data.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=symbol)
            )[symbol]
        return float(trade.price), self._epoch(trade.timestamp)

    # --- orders ---------------------------------------------------------------
    def market_order(self, symbol: str, qty: float, side: str, tif: str = "day",
                     extended_hours: bool = False):
        return self.client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.__members__[tif.upper()],
                extended_hours=extended_hours,
            )
        )

    def limit_order(self, symbol: str, qty: float, side: str, limit_price: float, tif: str = "day",
                    extended_hours: bool = False):
        return self.client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                limit_price=limit_price,
                time_in_force=TimeInForce.__members__[tif.upper()],
                extended_hours=extended_hours,
            )
        )

    def close_position(self, symbol: str):
        """Flatten any open position in the symbol (handles residual/fractional).
        Alpaca's close-position endpoint always submits a MARKET order
        internally with no extended_hours control — unusable outside regular
        hours. order_manager.handle() routes extended-hours closes through a
        manual limit_order() instead of this method; see its extended-hours
        branch."""
        return self.client.close_position(symbol)
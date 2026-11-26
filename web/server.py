"""Read-only local dashboard for the sigengine -> relay -> Alpaca system.

Serves the single-page UI in `web/static/` plus a small JSON API that reads —
and only ever reads — the same sources the live system already writes:

    /api/portfolio   Alpaca account + open positions (live SDK call), synthetic
                     stop distance per position, and the daily-loss circuit
                     breaker mirrored from relay.risk_filter's own rule.
    /api/board       decisions/decisions.json — watchlist ranked by priority,
                     bias, sizing, contingencies, research notes.
    /api/journal     journal/trades.jsonl tail + engine liveness from
                     sigengine/state.json and the engine log.
    /api/chart       candles + the symbol's REAL strategy overlays/markers,
                     reusing backtest.chart_view._build_chart_data (the same
                     multi_strategy runner the engine trades on).
    /api/look        the presenter's look for a date — the stylist's record,
                     the wardrobe plate and the stage keyframe — so a human can
                     sign it off BEFORE the 20:00 ET render bakes it into video.

READ-ONLY OVER THE TRADING PATH. This module never imports relay.order_manager
and has no route that can place, cancel or size an order — it is a viewer for a
system that trades itself. That property is what matters and it is unchanged.

It is no longer literally method-read-only, though, and the difference is worth
stating rather than leaving this paragraph to rot: there is exactly ONE write
route, `POST /api/look/approve`, and its blast radius is one boolean in one
cosmetic file (`briefing/assets/<host>/stylist/<date>.json`). It cannot write
anywhere else — the date is regex-pinned to YYYY-MM-DD, the directory comes from
the presenting host's own config, and the handler edits a single known key
rather than accepting a body to persist. Nothing downstream of it touches
decisions.json, the journal, or the broker; the worst a stolen token achieves is
a presenter wearing the wrong shirt.

Kept on the stdlib http.server (no FastAPI/uvicorn) so watching the account
adds no dependency to the trading path.

Usage:
    .venv/bin/python -m web.server                     # localhost:8787
    .venv/bin/python -m web.server --host 0.0.0.0      # LAN (token required)
    .venv/bin/python -m web.server --port 9000

Auth: binding to anything other than loopback REQUIRES a shared token — set
DASHBOARD_TOKEN in .env, or one is generated at startup and written to a file
only this user can read (DASHBOARD_TOKEN_FILE, default
~/.config/alpaca/dashboard_token, mode 0600). The token itself is never printed
or logged: startup output is teed into a log file other accounts may be able to
read. Pass it
once as http://host:8787/?token=... and it is stored in a cookie. On loopback a
token is optional (enforced only if DASHBOARD_TOKEN is set).
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import re
import ipaddress
import mimetypes
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from requests import adapters as requests_adapters

from relay.config import (
    DECISIONS_FILE,
    JOURNAL_FILE,
    LOGS_DIR,
    SIGENGINE_STATE_FILE,
    ensure_data_dirs,
    get_settings,
)
from relay.risk_filter import HARD_DAILY_LOSS_LIMIT

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "web" / "static"
VENDOR_DIR = ROOT / "web" / "vendor"
# Durable archive of past research-briefing videos (briefing/generate.py writes
# here; default matches briefing/config.json archive_dir). Served read-only under
# /archive/ so the dashboard can play historical briefings that no longer live on
# the system SSD. Missing dir is fine — the route just 404s.
MEDIA_ARCHIVE_DIR = Path(os.getenv("BRIEFING_ARCHIVE_DIR", "/mnt/sandisk/alpaca/briefings"))
CONFIG_FILE = ROOT / "sigengine" / "config.json"
BRIEFING_CONFIG_FILE = ROOT / "briefing" / "config.json"
EASTERN = ZoneInfo("America/New_York")

# The ONLY shape a look date may take. Everything that reaches the filesystem on
# behalf of /api/look is built from a string matched by this, so a traversal
# attempt ("../../.env") never becomes a path in the first place.
_LOOK_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Alpaca is a network round-trip and the UI polls; cache briefly so an open tab
# (or three) doesn't hammer the broker. Charts recompute indicators over the
# full cached history, which is the expensive local call — cache those longer.
PORTFOLIO_TTL = 5.0
FILES_TTL = 2.0
CHART_TTL = 30.0
# How long a FAILED result is cached. Deliberately much longer than any success
# TTL: it turns a broker outage into one quiet retry per minute instead of one
# per tab per 10s. See _Cache._ttl_for.
ERROR_TTL = 60.0


# --------------------------------------------------------------------------
# tiny TTL cache
# --------------------------------------------------------------------------
class _Cache:
    """TTL cache with PER-KEY locking.

    The lock is per key, not global. A single global lock meant one slow
    producer stalled every other key too: when the Alpaca API went unresponsive,
    a hung /api/portfolio held the lock and took the purely file-backed
    /api/board and /api/journal down with it — the whole dashboard froze over a
    broker hiccup it did not even need. Now a hanging producer only blocks its
    own key, and the panels that need no network keep rendering.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()          # guards _store and _locks only
        self._store: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, threading.Lock] = {}

    @staticmethod
    def _ttl_for(value, ttl: float) -> float:
        """A failed result is held far longer than a good one — this is the
        backoff. Re-polling a broker that is timing out, once per 10s tab tick,
        is what got the Alpaca key throttled on 2026-07-14: each attempt hung
        holding a connection, so the retries themselves were the outage. Failure
        must be cheap and quiet, not a retry storm.
        """
        if isinstance(value, dict) and value.get("error"):
            return ERROR_TTL
        return ttl

    def _fresh(self, key: str, ttl: float):
        hit = self._store.get(key)
        if hit and (time.time() - hit[0]) < self._ttl_for(hit[1], ttl):
            return hit
        return None

    def get(self, key: str, ttl: float, producer):
        """Return a cached value for `key`, calling `producer()` on miss/expiry.

        Concurrent callers for the SAME key wait on one producer rather than
        each firing their own; callers for other keys are never blocked.
        """
        with self._guard:
            hit = self._fresh(key, ttl)
            if hit:
                return hit[1]
            lock = self._locks.setdefault(key, threading.Lock())

        with lock:
            # Re-check: another thread may have produced it while we queued.
            with self._guard:
                hit = self._fresh(key, ttl)
                if hit:
                    return hit[1]
            value = producer()
            with self._guard:
                self._store[key] = (time.time(), value)
            return value


_CACHE = _Cache()
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


# alpaca-py builds a bare requests.Session and calls
# `self._session.request(method, url, **opts)` with NO timeout (common/rest.py),
# so a stalled connection blocks forever — observed 2026-07-14, when the Alpaca
# API stopped answering and every dashboard request hung indefinitely on an SSL
# read instead of failing. requests has no session-level timeout, so inject a
# default at the adapter, which is the one place every call funnels through.
ALPACA_TIMEOUT = (5, 15)  # (connect, read) seconds


class _TimeoutAdapter(requests_adapters.HTTPAdapter):
    def send(self, request, **kwargs):  # noqa: D102
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = ALPACA_TIMEOUT
        return super().send(request, **kwargs)


def _client():
    """Lazily build one AlpacaClient. Imported here, not at module scope, so the
    server still boots (and serves the file-backed panels) when keys are absent."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            from relay.alpaca_client import AlpacaClient

            client = AlpacaClient(get_settings())
            for sub in (client.client, getattr(client, "_stock_data", None),
                        getattr(client, "_crypto_data", None)):
                session = getattr(sub, "_session", None)
                if session is not None:
                    session.mount("https://", _TimeoutAdapter())
                    session.mount("http://", _TimeoutAdapter())
            _CLIENT = client
        return _CLIENT


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _f(obj, attr, default=None):
    """float(getattr(...)) that yields `default` rather than raising — Alpaca
    leaves fields like unrealized_pl unset on some assets."""
    try:
        v = getattr(obj, attr, None)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# panel builders
# --------------------------------------------------------------------------
def build_portfolio() -> dict:
    """Account equity, day P&L, open positions, stop distance, halt status."""
    try:
        cl = _client()
        acct = cl.get_account()
        from relay.risk_filter import _is_equity_position
        # The positions panel is equity-shaped — it pairs each row with its
        # sigengine stop config and computes a distance-to-stop. An option row
        # has no stop config, so it renders with stop_pct 0 and a meaningless
        # stop price, and its qty is contracts rather than shares.
        raw_positions = [p for p in (cl.get_all_positions() or [])
                         if _is_equity_position(p)]
    except Exception as exc:  # noqa: BLE001 — surface broker errors in the UI
        return {"error": f"{type(exc).__name__}: {exc}"}

    equity = _f(acct, "equity", 0.0)
    last_equity = _f(acct, "last_equity", equity) or equity
    day_pl = equity - last_equity
    day_pl_pct = (equity / last_equity - 1.0) if last_equity else 0.0

    decisions = _load_json(DECISIONS_FILE, {})
    sym_cfg = _load_json(CONFIG_FILE, {}).get("symbols", {})

    positions = []
    for p in raw_positions:
        sym = p.symbol
        entry = _f(p, "avg_entry_price", 0.0)
        price = _f(p, "current_price", 0.0)
        # The stop is SYNTHETIC — the engine enforces it in code from this
        # config value; there is no resting stop order at the broker. Showing
        # the level here is informational, and matches what the engine will act
        # on at the next bar close.
        stop_pct = float(sym_cfg.get(sym, {}).get("stop", 0.0) or 0.0)
        stop_price = entry * (1.0 - stop_pct) if (entry and stop_pct) else None
        positions.append(
            {
                "symbol": sym,
                "side": getattr(p, "side", None) and str(p.side).split(".")[-1].lower(),
                "qty": _f(p, "qty", 0.0),
                "avg_entry_price": entry,
                "current_price": price,
                "market_value": _f(p, "market_value", 0.0),
                "cost_basis": _f(p, "cost_basis", 0.0),
                "unrealized_pl": _f(p, "unrealized_pl", 0.0),
                "unrealized_plpc": _f(p, "unrealized_plpc", 0.0),
                "stop_pct": stop_pct or None,
                "stop_price": stop_price,
                # How far price can fall before the synthetic stop trips, as a
                # fraction of current price. Negative = already through it.
                "stop_distance_pct": ((price / stop_price - 1.0) if (stop_price and price) else None),
                "priority": decisions.get("priority", {}).get(sym),
                "bias": decisions.get("bias", {}).get(sym),
            }
        )
    positions.sort(key=lambda r: -(r["market_value"] or 0))

    # Mirror relay.risk_filter.check()'s rule exactly: clamp the configured
    # limit by the hard ceiling, halt when equity/last_equity - 1 <= -limit.
    # BUYS ONLY — exits are exempt (a halt must never trap a position).
    limit = min(float(decisions.get("daily_loss_limit", HARD_DAILY_LOSS_LIMIT)), HARD_DAILY_LOSS_LIMIT)
    return {
        "equity": equity,
        "last_equity": last_equity,
        "cash": _f(acct, "cash", 0.0),
        "buying_power": _f(acct, "buying_power", 0.0),
        "day_pl": day_pl,
        "day_pl_pct": day_pl_pct,
        "paper": bool(getattr(_client(), "paper", True)),
        "status": str(getattr(acct, "status", "")).split(".")[-1],
        "positions": positions,
        "halt": {
            "limit": limit,
            "hard_cap": HARD_DAILY_LOSS_LIMIT,
            "tripped": bool(last_equity and (equity / last_equity - 1.0) <= -limit),
            # Fraction of remaining headroom used up, for the meter.
            "used_frac": min(max((-day_pl_pct) / limit, 0.0), 1.0) if limit else 0.0,
            "applies_to": "buys only — exits always allowed",
        },
    }


def build_board() -> dict:
    """decisions.json as a rotation board: candidates ranked by priority."""
    d = _load_json(DECISIONS_FILE, {})
    if not d:
        return {"error": f"no decisions file at {DECISIONS_FILE}"}

    bias = d.get("bias", {})
    priority = d.get("priority", {})
    rows = [
        {
            "symbol": s,
            "bias": bias.get(s, "flat"),
            "priority": priority.get(s, 0),
            "suggested_size": d.get("suggested_size", {}).get(s),
            "max_position": d.get("max_position", {}).get(s),
            "contingencies": d.get("contingencies", {}).get(s, []),
        }
        for s in d.get("watchlist", [])
    ]
    # Highest priority first — the order rotation actually prefers. Longs carry
    # priority > 0, flats are 0, so this puts tradeable candidates on top.
    rows.sort(key=lambda r: (-(r["priority"] or 0), r["symbol"]))

    notes = d.get("notes", {})
    return {
        "date": d.get("date"),
        "rows": rows,
        "max_open_positions": d.get("max_open_positions"),
        "daily_loss_limit": d.get("daily_loss_limit"),
        "allow_extended_hours": d.get("allow_extended_hours"),
        "notes": notes if isinstance(notes, dict) else {"_": str(notes)},
        "mtime": DECISIONS_FILE.stat().st_mtime if DECISIONS_FILE.exists() else None,
    }


# Alpaca answers at most 500 orders per request, and every CLOSED order takes
# a slot: a rejected retry or a cancelled resting exit counts just like a fill.
# One request (direction=asc, limit=500) therefore froze the fills at the
# account's oldest 500 closed orders once it had more, so build_executions
# pages through them.
ORDERS_PAGE = 500
# The bound: 20 pages, the newest 10,000 closed orders. Paging runs newest
# first, so reaching it leaves out the OLDEST orders, never the latest fills.
ORDERS_MAX_PAGES = 20
# `until` is exclusive and two orders can share a submission time, so each
# page asks for a little past the oldest order already seen and drops repeats
# by order id. Losing an order at a page boundary would pair a later exit
# against the wrong entry.
ORDERS_PAGE_OVERLAP = timedelta(seconds=1)

# Walking every page on every refresh is too many requests. build_executions
# runs behind a 5 s cache entry (the executions panel, the chart's fill marks,
# the cloud publisher's snapshot), on the same Alpaca key the engine trades
# with, and Alpaca allows that key about 200 requests a minute in total. A
# 20-page walk every 5 s could take all of them. An order's fill does not
# change once the order is closed, so the walk is kept (_ClosedOrders) and a
# refresh asks only for orders submitted after the newest one already held:
# one request, normally.
#
# That misses an order submitted BEFORE the newest one held that closes later,
# such as a resting limit exit that fills an hour after it was placed. Two
# things catch it. The refresh starts ORDERS_REFRESH_OVERLAP before the newest
# order held, so an order that rested less than that is caught at once; the
# repeats are dropped by order id. Everything else is caught by the full walk,
# which runs again whenever the kept one is ORDERS_FULL_REFRESH_SEC old, so a
# late fill is never more than that late.
ORDERS_REFRESH_OVERLAP = timedelta(minutes=15)
ORDERS_FULL_REFRESH_SEC = 600.0

_log = logging.getLogger(__name__)


def _order_epoch(o, *names: str) -> float | None:
    """The first of `names` that is a datetime on order `o`, as epoch seconds."""
    for name in names:
        v = getattr(o, name, None)
        if v is None:
            continue
        try:
            return float(v.timestamp())
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            continue
    return None


def _submitted(o) -> float | None:
    return _order_epoch(o, "submitted_at", "created_at")


def _identifiable(orders: list) -> bool:
    """Every order has an id and a submission time (_ClosedOrders needs both)."""
    return all(getattr(o, "id", None) is not None and _submitted(o) is not None
               for o in orders)


def _walk_closed_orders(trading, after: datetime | None = None) -> tuple[list, bool]:
    """(closed orders newest first, whether the walk reached the end).

    Pages walk back in submission time from the newest order: each asks for
    the orders submitted until (just past) the oldest one the last page held.
    With `after`, only orders submitted after it are asked for. The walk
    reaches the end when a page comes back short. It stops early at the bound
    of ORDERS_MAX_PAGES pages of ORDERS_PAGE, or when paging cannot move on,
    and returns False then. A failed page raises out of here, so
    build_executions reports an error instead of a history with a hole in it.
    """
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    out: list = []
    seen: set[str] = set()
    until: datetime | None = None
    for _ in range(ORDERS_MAX_PAGES):
        page = list(trading.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, limit=ORDERS_PAGE, direction="desc",
            after=after, until=until)) or [])
        # Repeats are only possible across pages (the overlap); within one
        # page every order is new.
        fresh = [o for o in page if getattr(o, "id", None) is None or str(o.id) not in seen]
        seen.update(str(o.id) for o in page if getattr(o, "id", None) is not None)
        out.extend(fresh)
        if len(page) < ORDERS_PAGE:
            return out, True                    # the oldest order asked for is in
        stamps = [t for t in (_submitted(o) for o in page) if t is not None]
        if not stamps:
            _log.warning("closed orders: a full page carries no submission times; "
                         "stopped after %d orders", len(out))
            return out, False
        oldest = datetime.fromtimestamp(min(stamps), timezone.utc)
        # The overlap, unless a whole page was repeats (more than a page's
        # worth of orders inside it): then step to the oldest time itself.
        nxt = oldest + ORDERS_PAGE_OVERLAP if fresh else oldest
        if until is not None and nxt >= until:
            nxt = oldest
        if until is not None and nxt >= until:
            _log.warning("closed orders: paging made no progress at %s; stopped after "
                         "%d orders", oldest.isoformat(), len(out))
            return out, False
        until = nxt
    _log.warning("closed orders: stopped at the bound of %d pages (%d orders); older "
                 "fills are left out", ORDERS_MAX_PAGES, len(out))
    return out, False


class _ClosedOrders:
    """The closed orders already read, kept between refreshes so a refresh
    asks the broker only for what is new (see ORDERS_REFRESH_OVERLAP).

    Kept per trading client: a different client is a different account, or a
    test's fake, and starts again with a full walk. Kept only when every order
    has an id and a submission time, since a refresh finds its repeats by id
    and its starting point by time; without them every read is a full walk.

    The list is held in the order a full walk returns it, newest submission
    first, so build_executions gives the same output for the same orders
    however they were read. One lock covers the whole read: two callers at
    once wait for one walk instead of each making their own requests.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._client = None          # strong reference: an id() could be reused
        self._orders: list = []
        self._full_at: float | None = None

    def _drop(self) -> None:
        self._client, self._orders, self._full_at = None, [], None

    def get(self, trading) -> list:
        with self._lock:
            now = self.clock()
            if (self._client is not trading or self._full_at is None
                    or now - self._full_at >= ORDERS_FULL_REFRESH_SEC):
                return self._full(trading, now)
            return self._refresh(trading, now)

    def _full(self, trading, now: float) -> list:
        # A walk that raises leaves the kept orders as they were: they are
        # still true, and the next read tries the full walk again.
        orders, _ = _walk_closed_orders(trading)
        self._keep(trading, orders, now)
        return list(orders)

    def _keep(self, trading, orders: list, full_at: float) -> None:
        if _identifiable(orders):
            self._client, self._orders, self._full_at = trading, list(orders), full_at
        else:
            self._drop()

    def _refresh(self, trading, now: float) -> list:
        newest = max((_submitted(o) for o in self._orders), default=None)
        after = (datetime.fromtimestamp(newest, timezone.utc) - ORDERS_REFRESH_OVERLAP
                 if newest is not None else None)
        new, complete = _walk_closed_orders(trading, after=after)
        if not complete:
            # More than the page bound since `after`: these are the newest
            # ORDERS_MAX_PAGES pages, which is what a full walk returns too.
            self._keep(trading, new, now)
            return list(new)
        if not _identifiable(new):
            return self._full(trading, now)
        # The overlap's repeats: the copy just read replaces the kept one.
        ids = {str(o.id) for o in new}
        merged = new + [o for o in self._orders if str(o.id) not in ids]
        # Newest submission first; the sort is stable, so orders submitted at
        # the same instant stay in the order the broker listed them.
        merged.sort(key=_submitted, reverse=True)
        # The same bound a full walk has: the oldest orders drop out first.
        del merged[ORDERS_MAX_PAGES * ORDERS_PAGE:]
        self._orders = merged
        return list(merged)


_ORDERS = _ClosedOrders()


def _closed_orders(trading) -> list:
    """Every closed order on the account, newest first, up to ORDERS_MAX_PAGES
    pages of ORDERS_PAGE: a full walk at most every ORDERS_FULL_REFRESH_SEC,
    and only the new orders in between (_ClosedOrders)."""
    return _ORDERS.get(trading)


def build_executions(limit: int = 60) -> dict:
    """Real executed fills from Alpaca, paired into entry -> exit with P&L.

    The journal (trades.jsonl) records the DECISION, not the execution: an
    order row carries action/qty/status but never a FILL price, and an order_ok
    is appended at submit time when a market order has no fill price yet. So
    price has to come from the broker.

    (Do not read that as "the journal has no prices anywhere" — it did once, but
    diagnostic rows added 2026-07-15 carry their own price-shaped fields:
    quote_fallback records quote_price/last_trade, and reconcile_* and
    correction rows carry neither an action nor a qty. None of them describe an
    execution, which is why this function reads the broker instead of the
    journal and does not care what event types exist.)

    Closed orders carry `filled_avg_price`, which is the executed price. All of
    them are read, page by page (_closed_orders), not just the first 500, and
    kept between calls, so a refresh asks the broker only for the new ones.
    Walking the fills oldest-first by fill time per symbol and tracking average
    cost turns a flat list of fills into round-trips: a sell reports the entry
    it is closing against, the exit it got, and the realized P&L. Average cost
    (not FIFO) matches how the strategy actually scales — a half-exit closes
    against the blended entry, and Alpaca reports position cost the same way.
    """
    try:
        cl = _client()
        orders = _closed_orders(cl.client)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    fills = []
    # _closed_orders is newest first; reversed, it is oldest submission first.
    for o in reversed(orders):
        qty = _f(o, "filled_qty", 0.0) or 0.0
        price = _f(o, "filled_avg_price")
        if qty <= 0 or price is None:
            continue  # cancelled/expired/rejected — never executed
        fills.append((o, qty, price))
    # Oldest fill first, whatever order the pages came in: average cost has to
    # move in the order the fills happened. Two fills stamped the same instant
    # go in submission order (the sort is stable over the oldest-first list),
    # as they did when the orders were read oldest first.
    _never = float("-inf")
    fills.sort(key=lambda f: (
        _order_epoch(f[0], "filled_at", "submitted_at", "created_at") or _never,
        _order_epoch(f[0], "submitted_at", "created_at") or _never))

    pos: dict[str, dict] = {}   # symbol -> {qty, avg_cost}
    rows = []
    for o, qty, price in fills:
        side = str(getattr(o, "side", "")).split(".")[-1].lower()
        sym = o.symbol
        p = pos.setdefault(sym, {"qty": 0.0, "avg_cost": 0.0})
        row = {
            "ts": o.filled_at.timestamp() if getattr(o, "filled_at", None) else None,
            "symbol": sym, "side": side, "qty": qty, "price": price,
            "entry_price": None, "exit_price": None, "pnl": None, "pnl_pct": None,
            "order_type": str(getattr(o, "order_type", "")).split(".")[-1].lower(),
        }
        if side == "buy":
            # Blend into average cost.
            total = p["avg_cost"] * p["qty"] + price * qty
            p["qty"] += qty
            p["avg_cost"] = (total / p["qty"]) if p["qty"] else 0.0
            row["entry_price"] = price
        else:
            entry = p["avg_cost"] or None
            closed = min(qty, p["qty"]) if p["qty"] > 0 else qty
            row["entry_price"] = entry
            row["exit_price"] = price
            if entry:
                row["pnl"] = (price - entry) * closed
                row["pnl_pct"] = price / entry - 1.0
            p["qty"] = max(0.0, p["qty"] - qty)
            if p["qty"] == 0:
                p["avg_cost"] = 0.0
        row["held_after"] = p["qty"]
        rows.append(row)

    rows.reverse()  # newest first
    realized = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    wins = [r for r in rows if r["pnl"] is not None and r["pnl"] > 0]
    closes = [r for r in rows if r["pnl"] is not None]
    return {
        "rows": rows[:limit],
        "realized_pnl": realized,
        "round_trips": len(closes),
        "win_rate": (len(wins) / len(closes)) if closes else None,
    }


def _norm_ts(v):
    """trades.jsonl carries two timestamp shapes — an ISO bar time on signal_*
    rows and a float epoch on order_* rows. Normalize both to epoch seconds."""
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except (TypeError, ValueError):
        return None


def _tail_lines(path: Path, n: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block, data = 8192, b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        return data.decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def build_journal(limit: int = 60) -> dict:
    """Recent journal rows + engine liveness from state.json and the log."""
    rows = []
    for line in _tail_lines(JOURNAL_FILE, limit * 2):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        r["_ts"] = _norm_ts(r.get("ts"))
        rows.append(r)
    rows = rows[-limit:][::-1]

    state = _load_json(SIGENGINE_STATE_FILE, {})
    parsed = {s: _norm_ts(t) for s, t in state.items()}
    newest = max([t for t in parsed.values() if t], default=None)
    symbols = [
        {
            "symbol": s,
            "last_bar": t,
            # A symbol a bar or so behind its peers is the expected transient
            # (yfinance fallback lag); the engine re-pulls it via _backfill_gaps.
            "behind_s": (newest - t) if (newest and t) else None,
        }
        for s, t in sorted(parsed.items())
    ]

    log_path = LOGS_DIR / "sigengine.log"
    log_tail = _tail_lines(log_path, 40)
    fallbacks = sorted({
        ln.split()[1].strip("[]")
        for ln in log_tail
        if "yfinance fallback" in ln and len(ln.split()) > 1
    })

    return {
        "rows": rows,
        "engine": {
            "symbols": symbols,
            "newest_bar": newest,
            "last_poll": SIGENGINE_STATE_FILE.stat().st_mtime if SIGENGINE_STATE_FILE.exists() else None,
            "running": _engine_running(),
            "polling": _engine_polling(),
            "stale_after_sec": _POLL_STALE_AFTER_SEC,
            "log_mtime": log_path.stat().st_mtime if log_path.exists() else None,
            "log_tail": log_tail[-12:],
            "yfinance_fallback": fallbacks,
        },
    }


# A poll runs every 300s (sigengine/config.json poll_seconds). Two missed polls
# plus slack is a confident "it has stopped working" without flapping on one
# slow IBKR cycle.
_POLL_STALE_AFTER_SEC = 900.0


def _engine_polling() -> bool:
    """True if the engine is actually DOING something, not merely present.

    _engine_running() below answers "is there a process", which is not the
    failure this system has actually had. Both real incidents looked alive: the
    2026-07-04 fetch-phase wedge (a hung IBKR call froze the loop while the
    process sat there), and the ushmds farm-wedge where the engine polls happily
    but every symbol returns zero bars. A green "engine live" badge through
    either of those is worse than no badge, because it is checked precisely when
    something feels wrong.

    Liveness is measured off the state file instead: sigengine.engine writes it
    at the end of every poll, so its mtime IS the last completed cycle. That
    also makes the badge honest about a process that exited without the
    supervisor having restarted it yet.
    """
    try:
        if not SIGENGINE_STATE_FILE.exists():
            return False
        return (time.time() - SIGENGINE_STATE_FILE.stat().st_mtime) <= _POLL_STALE_AFTER_SEC
    except OSError:
        return False


def _engine_running() -> bool:
    """True if a sigengine.engine process is alive. Scans /proc directly rather
    than shelling out to pgrep, so this stays cheap enough to poll.

    PRESENCE only — see _engine_polling() for whether it is still working. Note
    this also matches a one-off `--once --dry-run` diagnostic run, so it can be
    green with no daemon at all."""
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue
            if "sigengine.engine" in cmd or "sigengine/engine.py" in cmd:
                return True
    except OSError:
        pass
    return False


def build_chart(symbol: str | None, bars: int) -> dict:
    """Candles + real strategy overlays, via the engine's own strategy runner,
    plus the account's actual FILLS.

    The fills come from the broker, through the same cached build_executions the
    executions panel uses — so opening a chart costs no extra Alpaca round-trip
    beyond what the page already makes. They are passed into the chart builder
    rather than fetched inside it, so the chart builder never needs broker
    credentials.

    NON-FATAL by design: a broker hiccup must degrade the chart to what it drew
    before fills existed, not blank it. The recomputed arrows, the indicators and
    the candles are all local and stay correct without the network.

    Shares the executions panel's cache entry deliberately, which also means it
    inherits that call's newest-first limit of 60 fills. Over the ~2 weeks a
    220-bar 1h chart spans that is far more than this account trades; if the
    limit is ever raised for the panel the chart simply gets more.
    """
    from backtest.chart_view import _build_chart_data

    symbols = _load_json(CONFIG_FILE, {}).get("symbols", {})
    if symbol:
        sym = symbol.upper()
        if sym not in symbols:
            return {"error": f"{sym} not in sigengine/config.json"}
        symbols = {sym: symbols[sym]}
    try:
        ex = _CACHE.get("executions", PORTFOLIO_TTL, build_executions)
        fills = ex.get("rows") or []
    except Exception:                                   # noqa: BLE001
        fills = []
    data = _build_chart_data(symbols, bars, fills=fills)
    if not data:
        return {"error": "no cached bars — has the engine polled yet?"}
    return {"symbols": data}


# --------------------------------------------------------------------------
# the presenter's look — review before the render, not after
# --------------------------------------------------------------------------
def _session_date() -> str:
    """Today's US SESSION date. The stylist keys looks by this and so must the
    dashboard: at 09:35 ET the Melbourne date is already tomorrow, and a page
    defaulting to the local date would show 'no look yet' every evening while
    one sat on disk. Same rule as agent/run_stylist.sh.

    ZoneInfo, not a fixed -4 offset: ET is -5 for four months of the year, and a
    hardcoded offset would silently shift the date boundary every November — the
    exact class of bug cron/at_et.sh exists to kill.
    """
    return datetime.now(EASTERN).strftime("%Y-%m-%d")


def _presenting_host() -> tuple[str, dict, Path | None]:
    """(host filename, host json, that host's stylist dir) for whoever is
    ACTUALLY presenting — resolved exactly the way briefing/generate.py and
    host_briefing.day_keyframe resolve it, via config.host.host_json.

    Reading a single hardcoded path is how one presenter's look gets shown —
    or approved — for another the moment the presenter changes, and nothing
    reports it. The stylist dir comes off the HOST file, not from a guess keyed
    on the filename, so a new presenter needs no change here. There is no
    default presenter: with host.host_json unset, or a presenter file that is
    absent (a clean checkout — the persona is local-only) or carries no
    `stylist_dir`, the stylist dir is None and the page shows no looks.
    """
    cfg = _load_json(BRIEFING_CONFIG_FILE, {}).get("host", {})
    name = os.getenv("BRIEFING_HOST_JSON") or cfg.get("host_json") or ""
    host = _load_json(ROOT / "briefing" / name, {}) if name else {}
    sdir = ROOT / host["stylist_dir"] if host.get("stylist_dir") else None
    return name, host, sdir


def _look_path(date: str) -> Path | None:
    """The look record for `date`, or None if the date is not a plain
    YYYY-MM-DD — which also keeps '../..' from ever becoming a path — or the
    presenter has no stylist dir."""
    if not _LOOK_DATE_RE.match(date or ""):
        return None
    sdir = _presenting_host()[2]
    return sdir / f"{date}.json" if sdir is not None else None


def _look_asset(date: str, kind: str) -> Path | None:
    """The plate or keyframe image named INSIDE the look record.

    Confined twice over: the record is only reachable through _look_path (so
    the date is already pinned), and the path it names must resolve inside
    briefing/assets. A look file is written by our own stylist, but it is still
    data, and data that names a file to serve is a path-traversal question.
    """
    if kind not in ("plate", "keyframe"):
        return None
    p = _look_path(date)
    if p is None or not p.exists():
        return None
    rel = _load_json(p, {}).get(kind)
    if not rel:
        return None
    try:
        target = (ROOT / rel).resolve()
        if not target.is_relative_to((ROOT / "briefing" / "assets").resolve()):
            return None
    except (OSError, ValueError):
        return None
    return target if target.exists() else None


def build_look(date: str | None = None) -> dict:
    """The day's look plus the ONE thing the reviewer actually needs to know:
    what tonight's render will put on screen if nobody touches anything.

    `approved` is deliberately tri-state to the UI — True, False, or absent for
    a look written before the gate existed — because "nobody has looked at this
    yet" and "somebody looked and said no" are different situations and the page
    should not render them identically.
    """
    date = date or _session_date()
    if not _LOOK_DATE_RE.match(date):
        return {"error": "date must be YYYY-MM-DD"}
    name, host, sdir = _presenting_host()
    stylist_on = bool(_load_json(BRIEFING_CONFIG_FILE, {})
                      .get("host", {}).get("stylist", True)
                      and host.get("stylist", True))
    out = {
        "date": date,
        "session_date": _session_date(),
        "host": host.get("name") or name,
        "host_json": name,
        "stylist_enabled": stylist_on,
        "locked_keyframe": host.get("keyframe"),
        "dates": sorted(
            (p.stem for p in sdir.glob("*.json") if _LOOK_DATE_RE.match(p.stem)),
            reverse=True) if sdir is not None else [],
    }
    p = _look_path(date)
    rec = _load_json(p, None) if p else None
    if not rec:
        out["exists"] = False
        # No look is not an error and not a blocker: day_keyframe falls back to
        # the host's locked keyframe, which is the whole failure-safe design.
        out["renders_with"] = "locked"
        return out
    out.update({
        "exists": True,
        "weather": rec.get("weather"),
        "style": rec.get("style"),
        "plate_identity": rec.get("plate_identity"),
        "keyframe_identity": rec.get("keyframe_identity"),
        "approved": rec.get("approved"),
        "approved_at": rec.get("approved_at"),
        "renders_with": "look" if rec.get("approved") else "locked",
        "plate_url": f"/api/look/asset?date={date}&kind=plate",
        "keyframe_url": f"/api/look/asset?date={date}&kind=keyframe",
    })
    return out


def set_look_approval(date: str, approved: bool) -> dict:
    """Stamp (or clear) the approval on one look record.

    Writes ONLY the three approval keys back into the existing object — the
    request body never becomes file content, so a malformed or hostile payload
    cannot rewrite the outfit, the identity scores, or the image paths. Written
    via a temp file + os.replace so a crash mid-write cannot leave the render
    reading half a JSON file tonight.
    """
    if not _LOOK_DATE_RE.match(date or ""):
        return {"error": "date must be YYYY-MM-DD"}
    p = _look_path(date)
    if p is None:
        return {"error": "the presenter has no stylist_dir"}
    rec = _load_json(p, None)
    if not rec:
        return {"error": f"no look for {date}"}
    if approved:
        rec["approved"] = True
        rec["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rec["approved_by"] = "dashboard"
    else:
        for k in ("approved", "approved_at", "approved_by"):
            rec.pop(k, None)
    tmp = p.with_suffix(".json.tmp")
    try:
        with tmp.open("w") as f:
            json.dump(rec, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return {"error": f"could not write {p.name}: {exc}"}
    return build_look(date)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
_TOKEN_IN_QUERY = re.compile(r"([?&]token=)[^&\s]*")


def _redact_token(line: str) -> str:
    """Strip the dashboard token out of anything about to be logged.

    log_request() hands log_message() the raw requestline, which includes the
    full query string — and the documented way to authenticate is
    `http://host:8787/?token=...` (deploy/run_dashboard.sh). So every
    authenticated request wrote the one credential guarding a live brokerage
    account, in cleartext, into /mnt/sandisk/alpaca/logs/dashboard.log: mode
    0664, never rotated, on a disk shared with the ComfyUI/briefing stack and
    whatever backs it up. Anything able to read that file could then read the
    account.

    Redacting at the logging boundary rather than at each call site means a new
    route or a future log line cannot reintroduce the leak.
    """
    return _TOKEN_IN_QUERY.sub(r"\1<redacted>", line)


# What a request without a valid token gets back, on every route and verb. It
# says nothing about this server: no hint at how to authenticate, no app name.
# Internet scanners index response bodies, so any distinctive text here would
# let someone find every host running this server by searching for it.
_UNAUTHORIZED = b"401 Unauthorized\n"


def _token_file() -> Path:
    """Where a token generated at startup is saved for the operator."""
    return Path(os.getenv("DASHBOARD_TOKEN_FILE")
                or Path.home() / ".config" / "alpaca" / "dashboard_token").expanduser()


def _write_token_file(path: Path, token: str) -> None:
    """Save `token` to `path`, readable and writable by this user only (0600).

    The file is created with mode 0600 by os.open, so the token is never on
    disk under a looser mode, even briefly. It is written to a new temp file
    and renamed over `path`, so a file already there with a looser mode, or a
    symlink placed there, is replaced instead of written through.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)          # the umask can only narrow it; pin it
            os.write(fd, (token + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Handler(BaseHTTPRequestHandler):
    token: str | None = None  # set on the class at startup

    # The stdlib's own error pages (501 for an unsupported verb, 400 for a
    # malformed request) are sent before the token check can run. Keep them as
    # bare as the 401: plain text, no HTML page.
    error_message_format = "%(code)d %(message)s\n"
    error_content_type = "text/plain; charset=utf-8"

    def send_response(self, code, message=None):
        """The stdlib's send_response minus the Server header.

        That header named the app and the exact Python version on every
        response, including the 401 a scanner gets. Nothing needs it.
        """
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def log_message(self, fmt, *args):  # quieter than the stdlib default
        print(f"[{self.log_date_time_string()}] {_redact_token(fmt % args)}")

    def handle_one_request(self):
        """Swallow the disconnect races that are normal for a browser dashboard.

        The UI polls every few seconds and the page is often reloaded or closed
        mid-response, which aborts the socket while _send is still writing —
        surfacing as BrokenPipeError/ConnectionResetError out of end_headers()
        or wfile.write(). The stdlib only catches socket timeouts here, so the
        rest reach socketserver.handle_error, which dumps a full traceback per
        occurrence (50 of them in dashboard.log over ~24h, all benign).

        That noise is the actual harm: a log full of routine tracebacks is one
        a real error hides in — the failure mode that let the 2026-07-15 META
        order rejection sit unnoticed. There is nothing to recover here (the
        client is already gone), so drop the connection and move on, leaving
        genuine faults as the only tracebacks in the file.
        """
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This page reads a live brokerage account; keep it out of caches and
        # out of other origins' frames.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: object):
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def _authorized(self, query: dict) -> bool:
        if not self.token:
            return True
        # SimpleCookie.get() yields a Morsel (which has .value) or the default —
        # so pull the morsel out and guard it, rather than defaulting to another
        # SimpleCookie and reading .value off a plain dict.
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get("dash_token")
        supplied = (
            query.get("token", [None])[0]
            or self.headers.get("X-Dashboard-Token")
            or (morsel.value if morsel else None)
        )
        if not supplied:
            return False
        # compare_digest keeps the check constant-time. It is given bytes:
        # handed two str it raises TypeError when either holds a non-ASCII
        # character, and every channel here can carry one (a %-escaped query
        # value, or a header or cookie the server decodes as latin-1). That
        # TypeError escaped the handler and dropped the connection with a
        # traceback in the log instead of answering 401.
        try:
            return hmac.compare_digest(str(supplied).encode("utf-8"),
                                       self.token.encode("utf-8"))
        except (TypeError, UnicodeError):
            return False

    # -- routes -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        """The one write the dashboard has: sign off (or un-sign) a day's look.

        Same token as every other route. The body supplies only a date and a
        boolean — it is never persisted as-is — and an unknown path 404s rather
        than falling through to the static handler, so this method cannot become
        a general write surface by accident later.
        """
        url = urlparse(self.path)
        if not self._authorized(parse_qs(url.query)):
            self._send(401, _UNAUTHORIZED, "text/plain; charset=utf-8")
            return
        if url.path != "/api/look/approve":
            self._send(404, b"not found\n", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:                       # a date and a bool; nothing else
                self._send(413, b"body too large\n", "text/plain")
                return
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            out = set_look_approval(str(body.get("date", "")),
                                    bool(body.get("approved", True)))
        except Exception as exc:               # noqa: BLE001 — never 500 silently
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(400 if out.get("error") else 200, out)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path

        if not self._authorized(query):
            self._send(401, _UNAUTHORIZED, "text/plain; charset=utf-8")
            return

        # A correct ?token= on any page load parks it in a cookie so the UI's
        # own fetches (and a plain bookmark) carry it from then on.
        extra = {}
        if self.token and query.get("token"):
            extra["Set-Cookie"] = f"dash_token={self.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"

        try:
            if path == "/api/portfolio":
                self._json(200, _CACHE.get("portfolio", PORTFOLIO_TTL, build_portfolio))
                return
            if path == "/api/board":
                self._json(200, _CACHE.get("board", FILES_TTL, build_board))
                return
            if path == "/api/journal":
                self._json(200, _CACHE.get("journal", FILES_TTL, build_journal))
                return
            if path == "/api/executions":
                # Broker round-trip, same cadence as the portfolio call.
                self._json(200, _CACHE.get("executions", PORTFOLIO_TTL, build_executions))
                return
            if path == "/api/chart":
                sym = (query.get("symbol", [None])[0] or "").upper() or None
                bars = max(20, min(int(query.get("bars", ["220"])[0] or 220), 1000))
                key = f"chart:{sym}:{bars}"
                self._json(200, _CACHE.get(key, CHART_TTL, lambda: build_chart(sym, bars)))
                return
            if path == "/api/symbols":
                cfg = _load_json(CONFIG_FILE, {}).get("symbols", {})
                self._json(200, {"symbols": [s for s, c in cfg.items() if c.get("enabled")]})
                return
            if path == "/api/look":
                # Uncached on purpose: this page's whole job is to show the
                # CURRENT approval state, and a stale card that still says
                # "unapproved" seconds after you approved it is worse than a
                # file read per request on a single-user dashboard.
                self._json(200, build_look(query.get("date", [None])[0]))
                return
            if path == "/api/look/asset":
                target = _look_asset(query.get("date", [""])[0],
                                     query.get("kind", [""])[0])
                if target is None:
                    self._send(404, b"no such look asset\n", "text/plain")
                    return
                ctype = mimetypes.guess_type(target.name)[0] or "image/png"
                self._send(200, target.read_bytes(), ctype, extra)
                return
        except Exception as exc:  # noqa: BLE001 — never 500 silently
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return

        self._static(path, extra)

    def _static(self, path: str, extra: dict):
        if path in ("/", ""):
            target = STATIC_DIR / "index.html"
        elif path.startswith("/vendor/"):
            target = VENDOR_DIR / path[len("/vendor/"):]
        elif path.startswith("/archive/"):
            target = MEDIA_ARCHIVE_DIR / path[len("/archive/"):]
        else:
            target = STATIC_DIR / path.lstrip("/")

        # Resolve and confine: refuse anything that escapes the served roots.
        try:
            target = target.resolve()
            roots = (STATIC_DIR, VENDOR_DIR, MEDIA_ARCHIVE_DIR)
            if not any(target.is_relative_to(r.resolve()) for r in roots):
                raise ValueError
            body = target.read_bytes()
        except (OSError, ValueError):
            self._send(404, b"not found\n", "text/plain")
            return

        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, body, ctype, extra)


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only dashboard for the Alpaca system")
    ap.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
                    help="bind address (default 127.0.0.1; use 0.0.0.0 for LAN — requires a token)")
    ap.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8787")))
    args = ap.parse_args()

    ensure_data_dirs()

    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host in ("localhost", "")

    # The token is never printed. deploy/run_dashboard.sh tees this output into
    # a log file, and a token in a log is a token for anyone who can read it.
    token = os.getenv("DASHBOARD_TOKEN") or None
    if token:
        where = "from DASHBOARD_TOKEN (environment or .env)"
    elif not loopback:
        # Reachable beyond this machine with no token set — generate one rather
        # than serving a live brokerage account to the whole network, and save
        # it where only this user can read it.
        token = secrets.token_urlsafe(24)
        path = _token_file()
        try:
            _write_token_file(path, token)
        except OSError as exc:
            raise SystemExit(
                "non-loopback bind with no DASHBOARD_TOKEN, and the generated "
                f"token could not be saved to {path}: {exc.strerror or exc}. "
                "Set DASHBOARD_TOKEN in .env.") from None
        where = f"generated for this run, saved to {path} (mode 0600)"
        print("\n  ! non-loopback bind with no DASHBOARD_TOKEN — generated a token for this run.",
              flush=True)
        print("    Set DASHBOARD_TOKEN in .env to keep it stable across restarts.", flush=True)
    else:
        where = None
    Handler.token = token

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = "localhost" if loopback else args.host
    url = f"http://{shown}:{args.port}/"
    print(f"\n  dashboard: {url}")
    if where:
        print(f"  token:     required, {where}")
        print("             open the page once with ?token=<token>; a cookie keeps it after that")
    else:
        print("  token:     none (loopback bind, DASHBOARD_TOKEN unset)")
    print(f"  read-only — no order routes. serving {STATIC_DIR}")
    print("  ctrl-c to stop\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.server_close()


if __name__ == "__main__":
    main()

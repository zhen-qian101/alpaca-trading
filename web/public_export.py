"""Build the PUBLIC portfolio file and push it to its own R2 bucket.

The public portfolio site (apps/portfolio) shows anyone the owner's paper
account. It never calls this machine: this exporter runs here, reads the same
read-only builders the private dashboard uses (web/server.py,
web/options_view.py) plus Alpaca's portfolio history, and uploads ONE object.

    python -m web.public_export                 # one run (the default, --once)
    python -m web.public_export --out-dir DIR   # write DIR/portfolio.json, no R2, no boto3
    python -m web.public_export --dry-run       # build + validate; print what would be sent

THE OBJECT: `portfolio.json` in the bucket named by R2_PUBLIC_BUCKET (default
`alpaca-public`), a bucket of its own, never the private dashboard's
(R2_BUCKET, default `alpaca-dashboard`). Credentials are the publisher's
R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY (web/r2.py), so the
token needs write access to the public bucket too.

WHAT GOES OUT IS A WHITELIST. Every section below is a NEW dict built field by
field from the builders' output; no builder dict is ever passed through, so a
field a builder grows later stays at home until someone adds it here AND to
the schema. The result must then pass apps/portfolio/schema/portfolio.schema.json
(additionalProperties: false on every object, checked by web/schema_gate.py)
or NOTHING is uploaded and the run exits 1. A missing or loose schema file
also stops the run (exit 2): the gate fails closed.

    meta       schema, as_of, data_delay (a fixed note), generated_by, unavailable
    account    paper, start_date, start_equity, equity, day_pl_pct,
               total_return_pct, max_drawdown_pct, best_day{date, return_pct},
               worst_day{date, return_pct}, daily[{date, equity}]
    positions  equities[{symbol, side, qty, avg_entry_price, stop_pct}],
               options[{underlying, type, side, strike, expiry, qty, premium_received}]
    trades     stats{round_trips, wins, losses, win_rate, avg_win, avg_loss,
               avg_win_pct, avg_loss_pct, profit_factor, realized_pnl},
               round_trips[{symbol, side, qty, entry_price, exit_price, entry_at,
               exit_at, pnl, pnl_pct, strategy}]
    signals    date, rows[{symbol, bias, priority, suggested_size,
               contingencies[{condition, level, valid_until}]}]
    strategy   symbols[{symbol, enabled, on_watchlist, interval, stop_pct, deploy,
               strategies[{type, and that type's parameters only: ema fast, slow,
               window | bollinger_rsi rsi_length, bb_length, bb_mult |
               macd_sma fast_len, slow_len, signal_len, veryslow_len}]}],
               risk{daily_loss_limit, daily_loss_hard_cap, max_position_shares_cap,
               allow_extended_hours, sleeve_reserve_pct}
    recaps     [{date, youtube_id, duration_s}], newest first, at most 12

NOT IN THIS FILE, by the owner's decision: third-party market data (no candles
or bars, no current or last price, no market value, no option quote: the data
vendors' terms restrict redistribution), model-written text of any kind (no
research notes or contingency reasons, and no recap title: a recap is the
date, the video id and its length), engine logs, account or order ids, file
paths, host names, the presenter's look, the desk digest and the zh cut. An
open position's unrealized P&L (in dollars or percent) and its distance to the
stop are left out too: each is the broker's mark in disguise, and with the
entry price and quantity it gives back the current price to the cent.

THE RECAP VIDEOS ARE NOT COVERED BY THAT LIST. They are public on YouTube and
the site embeds them. A language model writes their scripts and titles, an
AI-generated presenter reads them, and most of them show the live dashboard
(the desk slide), including its market candles and current prices. This file
carries only each video's date, id and length.

NO EQUITY FROM A SESSION THAT IS STILL GOING, by the owner's rule that no
published figure may give back a live price. With one share position open
and no cash flows, price = avg_entry_price + (equity - start_equity -
realized_pnl) / qty, and every one of those is published: so the account's
CURRENT equity is a live price to the cent, and so is the broker's day P&L
against the previous close. Neither is published. Every equity figure
(`equity`, `day_pl_pct`, the returns, best and worst day, `daily`) comes from
the broker's daily history up to the last session that is OVER, so the price
the same formula gives back is that session's end-of-day mark. That is the
cost of publishing the account's value at all, and the site says so. For the
same reason there is no intraday equity series (DAILY EQUITY ONLY).

THE EQUITY CURVE is Alpaca's portfolio history for the CURRENT account (read
only): timeframe 1D from the account's created_at to now, one point per
session, dated by the US/Eastern date of Alpaca's stamp (Alpaca stamps a
session's close at 20:00 ET, after the extended session). A point is used only
once its US/Eastern date has ended, whatever the broker
returns for the day in progress, and nothing is ever dated with the current
equity. `equity` is the last of those points. Best and worst day, and
`day_pl_pct` (the last point's change), compare a session with the session
BEFORE it on the calendar: a session the broker leaves out of the history, or
lists without equity, is skipped, never folded into the next day's change.
Cash flows are not adjusted for: a paper account has none.

STOPS: a position's stop_pct is published only where the site can vouch for
a stop (see armed_stop): an enabled symbol, a position the engine itself
opened, and not shares the put sleeve was assigned. The stop belongs to a
strategy's simulated trade, not to the broker's position, so null does not
prove there is none and a value does not prove one is armed (armed_stop says
when each happens). It is a check on closed bars, not an order resting at the
broker.

A SOURCE THAT FAILS makes its section null and is named in meta.unavailable;
it never blocks the other sections, and stale or partial data is never
substituted. Unchanged content is not re-sent (the hash leaves out
meta.as_of), except once an hour, so as_of never claims the box went quiet
when only the market did. A run lock makes an overlapping run exit at once.

READ-ONLY OVER THE TRADING PATH: this module never imports relay.order_manager,
relay.csp_manager or relay.options_client (tests/test_public_export.py checks
it in a fresh interpreter), and the only files it writes are its own state and
lock under $ALPACA_DATA_DIR/public_export/ and, with --out-dir, that folder.
Logs carry section names, counts and sizes, never a data value.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from relay.config import DATA_DIR
from relay.risk_filter import (
    HARD_DAILY_LOSS_LIMIT,
    HARD_MAX_POSITION,
    canonical_symbol,
    is_crypto_symbol,
)
from web import options_view
from web import publisher as _pub
from web import schema_gate
from web import server as web_server
from web.r2 import _BUCKET_RE, ConfigError, DirDestination, R2Config, R2Destination, check_key
from web.r2 import DEFAULT_BUCKET as PRIVATE_BUCKET

__version__ = "1.0.0"

log = logging.getLogger("public_export")

ROOT = Path(__file__).resolve().parent.parent
# Patched by the tests.
STATE_DIR = DATA_DIR / "public_export"
SCHEMA_FILE = ROOT / "apps" / "portfolio" / "schema" / "portfolio.schema.json"

SCHEMA_VERSION = 1
PORTFOLIO_KEY = "portfolio.json"
DEFAULT_PUBLIC_BUCKET = "alpaca-public"
JSON_TYPE = "application/json"
CACHE_CONTROL = "public, max-age=300"
# Re-send unchanged content this often, so as_of keeps proving the box is up.
HEARTBEAT_SEC = 3600
# The real cadence: the home cron (every 15 minutes, docs/public-portfolio.md)
# and the deploy schedule in .github/workflows/portfolio.yml (every 30 minutes
# 13:07-21:37 UTC on weekdays, otherwise at most 6 hours apart).
DATA_DELAY_NOTE = ("Exported about every 15 minutes and published about every 30 minutes in US "
                   "market hours on weekdays, every 6 hours otherwise: positions and trades can "
                   "trail the account by about 45 minutes, or 6 hours out of hours. Equity and "
                   "returns stop at the last completed session. Paper account.")

SECTIONS = ("account", "positions", "options", "trades", "signals", "strategy", "recaps")
MAX_DAILY_POINTS = 5000
MAX_POSITIONS = 100
MAX_TRADES = 500
MAX_SIGNAL_ROWS = 100
MAX_CONTINGENCIES = 10
MAX_RECAPS = 12
EASTERN = ZoneInfo("America/New_York")

# The strategy types the engine runs and the parameters each one reads:
# exactly backtest/multi_strategy.py STRATEGY_RUNNERS (a test holds the two,
# and the schema, together). A spec of any other type is dropped, and anything
# else in a spec stays at home.
STRATEGY_PARAMS: dict[str, tuple[str, ...]] = {
    "ema": ("fast", "slow", "window"),
    "bollinger_rsi": ("rsi_length", "bb_length", "bb_mult"),
    "macd_sma": ("fast_len", "slow_len", "signal_len", "veryslow_len"),
}
BIASES = ("long", "flat", "short")
CONDITIONS = ("close_above", "close_below")
# relay.csp_manager.DEFAULTS["reserve_frac"]: what config() uses when the
# policy's value is missing, not a number, or outside (0, 1]. The exporter
# cannot import csp_manager (it loads the options order path).
SLEEVE_DEFAULT_RESERVE = 0.20

# The dashboard contract's SYMBOL_RE, as the publisher applies it.
_SYMBOL_RE = _pub._SYMBOL_RE
# An OCC option symbol: root, YYMMDD, C/P, strike x 1000.
_OCC_RE = re.compile(r"^([A-Z]{1,6})([0-9]{6})([CP])([0-9]{8})$")
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_INTERVAL_RE = re.compile(r"^[0-9]{1,3}(?:m|h|d|wk|mo)$")
_ZERO_QTY = 1e-9
# Alpaca takes a crypto buy's fee out of the coins received, so the closing
# sell is for slightly less than the buy's filled_qty (BTC/USD: 0.25% less).
# A crypto trip counts as closed once at most this fraction of what it bought
# is left unsold; the coins never received are a cost of the trip.
CRYPTO_DUST_FRAC = 0.01


class SourceError(RuntimeError):
    """A builder could not produce its data (its message is never published)."""


# --------------------------------------------------------------------------
# value helpers: every published scalar goes through one of these
# --------------------------------------------------------------------------
def _num(v: Any, ndigits: int, lo: float | None = None, hi: float | None = None) -> float | None:
    """A finite float rounded to `ndigits`, or None. Numeric strings count
    (alpaca-py models carry strings); bools, NaN, infinities, anything else
    and values outside [lo, hi] are None."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(v):
        return None
    v = round(v, ndigits)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v + 0.0            # -0.0 -> 0.0


def _money(v: Any) -> float | None:
    return _num(v, 2, -1e9, 1e9)


def _ratio(v: Any) -> float | None:
    return _num(v, 6, -1000.0, 1000.0)


def _unit(v: Any) -> float | None:
    return _num(v, 6, 0.0, 1.0)


def _price(v: Any) -> float | None:
    p = _num(v, 4, 0.0, 1e7)
    return p if p else None


def _qty(v: Any) -> float | None:
    q = _num(v, 6, 0.0, 1e9)
    return q if q else None


def _int(v: Any, lo: int, hi: int) -> int | None:
    f = _num(v, 6)
    if f is None or not f.is_integer() or not lo <= f <= hi:
        return None
    return int(f)


def _symbol(v: Any) -> str | None:
    return v if isinstance(v, str) and _SYMBOL_RE.fullmatch(v) else None


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _et_date(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, EASTERN).date().isoformat()


def _date(v: Any) -> str | None:
    return v if isinstance(v, str) and _pub._valid_date(v) else None


def _utc_ts(v: Any) -> str | None:
    """An ISO-8601 timestamp with an offset, as UTC 'YYYY-MM-DDTHH:MM:SSZ'."""
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return _ts(dt.timestamp())


def _source(fn: Callable[[], Any]) -> Any:
    """Call a builder; its {"error": ...} answer (or an exception) becomes
    SourceError, whose text is never published or logged."""
    out = fn()
    if isinstance(out, dict) and "error" in out:
        raise SourceError("the builder reported an error")
    return out


# --------------------------------------------------------------------------
# account: Alpaca portfolio history, one point per session
# --------------------------------------------------------------------------
def _series(history: Any) -> list[tuple[float, float | None]]:
    """A PortfolioHistory's (timestamp, equity) pairs, every stamped session
    kept: equity is None where it is missing or not positive (days before the
    account was funded, or a hole the broker left)."""
    ts = getattr(history, "timestamp", None) or []
    eq = getattr(history, "equity", None) or []
    return _stamps([[t, e] for t, e in zip(ts, eq)])


def _opened_sessions(days: Any, now: float) -> list[str]:
    """US/Eastern dates of the exchange-calendar sessions that had opened by
    `now`, oldest first. alpaca-py's Calendar carries `date` and a naive
    `open` in New York time."""
    out = set()
    for day in days if isinstance(days, list) else []:
        d, opened = getattr(day, "date", None), getattr(day, "open", None)
        if isinstance(d, datetime):
            d = d.date()
        if not isinstance(d, date) or not isinstance(opened, datetime):
            continue
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=EASTERN)
        if opened.timestamp() <= now:
            out.add(d.isoformat())
    return sorted(out)


def fetch_history(now: float) -> dict:
    """The current account's daily portfolio history and the exchange
    calendar, read-only. No intraday series is requested.

    {"start": epoch of the account's creation,
     "daily": [(epoch, equity or None)], 1D from start to now,
     "sessions": ["YYYY-MM-DD"] sessions since start that had opened by now,
                 or None when the calendar could not be read}
    """
    from alpaca.trading.requests import GetCalendarRequest, GetPortfolioHistoryRequest

    cl = web_server._client()
    acct = cl.get_account()
    end = datetime.fromtimestamp(now, timezone.utc)
    created = getattr(acct, "created_at", None)
    start = created if isinstance(created, datetime) and created.tzinfo else end - timedelta(days=365)
    daily = cl.client.get_portfolio_history(
        GetPortfolioHistoryRequest(timeframe="1D", start=start, end=end))
    out = {"start": start.timestamp(), "daily": _series(daily), "sessions": None}
    try:
        days = cl.client.get_calendar(GetCalendarRequest(
            start=start.astimezone(EASTERN).date(), end=end.astimezone(EASTERN).date()))
        out["sessions"] = _opened_sessions(list(days or []), now)
    except Exception as exc:                    # noqa: BLE001 — the daily curve still ships
        log.info("market calendar unavailable (%s)", type(exc).__name__)
    return out


def _stamps(series: Any) -> list[tuple[float, float | None]]:
    """(epoch, equity) for every item with a finite timestamp; equity is
    None unless it is a positive, finite amount."""
    out: list[tuple[float, float | None]] = []
    for item in series if isinstance(series, list) else []:
        try:
            t, e = _num(item[0], 3), _money(item[1])
        except (TypeError, IndexError, KeyError):
            continue
        if t is not None:
            out.append((t, e if e is not None and e > 0 else None))
    return out


def account_section(portfolio: dict, history: dict, now: float) -> dict:
    """The equity curve and its statistics, from CLOSED sessions only.

    `portfolio` (build_portfolio) supplies only `paper`: its current equity
    and day P&L are live marks and are never read (see NO EQUITY FROM A
    SESSION THAT IS STILL GOING above). `equity` is the broker's last daily
    point dated before today's US/Eastern date; with none yet (an account on
    its first day) the section is unavailable rather than live.
    """
    start_date = _et_date(history["start"])
    today = _et_date(now)                       # every day before this one is over

    by_date: dict[str, float] = {}
    listed: set[str] = set()                    # every closed session the broker stamped
    for t, e in _stamps(history.get("daily")):
        d = _et_date(t)
        # Before the account existed, or a day that is not over yet: if the
        # broker serves the day in progress, that point is a live mark.
        if d < start_date or d >= today:
            continue
        listed.add(d)
        if e is not None:
            by_date[d] = e
    if not by_date:
        raise SourceError("no closed session yet")

    sessions = [d for d in history.get("sessions") or [] if start_date <= d < today]
    dates = sorted(by_date)[-MAX_DAILY_POINTS:]
    daily = [{"date": d, "equity": by_date[d]} for d in dates]
    curve = [by_date[d] for d in dates]
    equity = curve[-1]
    start_equity = curve[0]

    peak, drawdown = None, 0.0
    for e in curve:
        peak = e if peak is None else max(peak, e)
        drawdown = min(drawdown, e / peak - 1.0)
    # A day's change is against the session just before it: on the calendar
    # when it could be read, else among the sessions the broker stamped. A
    # pair with a hole in it is skipped, so a two-session move is never filed
    # under one date.
    order = sorted(set(sessions) | listed | set(by_date))
    before = dict(zip(order[1:], order))
    changes = {d: by_date[d] / by_date[before[d]] - 1.0
               for d in dates if before.get(d) in by_date}
    days = list(changes.items())
    best = max(days, key=lambda x: x[1]) if days else None
    worst = min(days, key=lambda x: x[1]) if days else None

    def day(x):
        return None if x is None else {"date": x[0], "return_pct": _ratio(x[1])}

    return {
        "paper": portfolio.get("paper") is not False,
        "start_date": dates[0],
        "start_equity": start_equity,
        "equity": equity,
        # The last completed session's change, not the broker's day P&L.
        "day_pl_pct": _ratio(changes[dates[-1]]) if dates[-1] in changes else None,
        "total_return_pct": _ratio(equity / start_equity - 1.0),
        "max_drawdown_pct": _num(drawdown, 6, -1.0, 0.0),
        "best_day": day(best),
        "worst_day": day(worst),
        "daily": daily,
    }


# --------------------------------------------------------------------------
# positions
# --------------------------------------------------------------------------
def _engine_enabled(cfg: Any) -> bool:
    """sigengine.engine.run_once: a symbol is polled when `enabled` is truthy."""
    return isinstance(cfg, dict) and bool(cfg.get("enabled", False))


def armed_stop(symbol: str, side: Any, engine_symbols: Any, entries: Any) -> float | None:
    """The symbol's stop where the site can vouch for it, or None.

    HOW THE ENGINE SELLS ON A STOP. There is no stop order at the broker. Each
    strategy keeps its own simulated trade, entered at the close of the bar its
    buy signal fired on, and emits a `close` when a later bar CLOSES at or
    below that entry less the symbol's `stop` (backtest/*.run_with_events). The
    engine runs the strategies of ENABLED symbols only
    (sigengine.engine.run_once) and fires a new `close` as a full sale of
    whatever the account holds in the symbol (sigengine.engine.fire_event
    checks only that something is held). So the stop follows the SIMULATED
    trade, not the broker's position, and a value is published only when all
    of these hold:

      * the position is long: the book never sets a stop on a short;
      * the symbol is enabled in sigengine/config.json with a numeric stop,
        looked up by its canonical name (Alpaca calls a bitcoin position
        BTCUSD, the config calls it BTC/USD);
      * the engine's entry sidecar says the ENGINE opened it (`opened`): its
        buy went out on a strategy's entry, so that strategy's simulated
        trade is long behind it;
      * it is not shares the put sleeve was assigned (`adopted`): those carry
        no stop by the operator's choice, and the sleeve sells them at its
        next weekly run.

    Neither answer is proof, and the site says so. None does not mean no stop
    exists: a strategy that is long in its own simulation can also stop out a
    position the engine did not open. A value does not mean one is armed: when
    a simulation enters and stops out on the same bar, the engine fires that
    stop before the real buy, and the position can be left with none (the
    same-bar stop orphan, unfixed).

    An unreadable sidecar leaves the last two unknown, and no stop is claimed.
    """
    if side != "long" or not isinstance(engine_symbols, dict) or not isinstance(entries, dict):
        return None
    canon = canonical_symbol(symbol)
    opened, adopted = entries.get("opened"), entries.get("adopted")
    if not isinstance(opened, dict):
        return None
    if canon.upper() not in {str(k).upper() for k in opened}:
        return None
    if isinstance(adopted, dict) and canon.upper() in {str(k).upper() for k in adopted}:
        return None
    cfg = engine_symbols.get(canon)
    if not _engine_enabled(cfg):
        return None
    return _unit(cfg.get("stop"))


def positions_section(portfolio: dict, engine: Any = None, entries: Any = None) -> list[dict]:
    """Open share/crypto positions. `engine` is sigengine/config.json and
    `entries` the engine's entry sidecar; together they decide which
    positions carry a stop (armed_stop)."""
    rows = portfolio.get("positions")
    if not isinstance(rows, list):
        raise SourceError("no positions list")
    engine_symbols = engine.get("symbols") if isinstance(engine, dict) else None
    out = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        raw = p.get("symbol")
        sym = _symbol(canonical_symbol(raw)) if isinstance(raw, str) else None
        side = p.get("side")
        qty, entry = _qty(abs(_num(p.get("qty"), 6) or 0.0)), _price(p.get("avg_entry_price"))
        if sym is None or side not in ("long", "short") or qty is None or entry is None:
            continue
        # No unrealized P&L and no distance to the stop: both are computed
        # from the broker's current price, and either one with the entry
        # price gives that price back (see NOT IN THIS FILE above).
        out.append({
            "symbol": sym,
            "side": side,
            "qty": qty,
            "avg_entry_price": entry,
            "stop_pct": armed_stop(sym, side, engine_symbols, entries),
        })
    return out[:MAX_POSITIONS]


def options_section(sleeve: Any) -> list[dict]:
    """The put sleeve's open short put, without any option quote."""
    if sleeve is None:
        return []                               # no sleeve configured, nothing held
    if not isinstance(sleeve, dict):
        raise SourceError("unexpected options sleeve")
    put = sleeve.get("short_put")
    if not isinstance(put, dict):
        return []
    occ = _OCC_RE.fullmatch(str(put.get("symbol") or ""))
    underlying = occ.group(1) if occ else _symbol(str(sleeve.get("underlying") or ""))
    qty = _qty(abs(_num(put.get("contracts"), 6) or 0.0))
    if underlying is None or qty is None:
        return []
    premium = _num(put.get("avg_entry_price"), 6, 0.0, 1e6)
    return [{
        "underlying": underlying,
        "type": ("put" if occ.group(3) == "P" else "call") if occ else "put",
        "side": "short",
        "strike": _price(put.get("strike")),
        "expiry": _date(put.get("expiry")),
        "qty": qty,
        "premium_received": _money(premium * 100.0 * qty) if premium else None,
    }]


# --------------------------------------------------------------------------
# trades: build_executions' fills paired into flat -> flat round trips
# --------------------------------------------------------------------------
def round_trips(rows: list[dict]) -> list[dict]:
    """Closed share/crypto round trips from build_executions rows.

    A trip opens on a buy while no trip is open for the symbol and closes when
    its own sells have taken back what its buys bought (for crypto: all but
    the fee, see CRYPTO_DUST_FRAC). The trip keeps its own count; the
    builder's running position (held_after) is not used, because for crypto
    it keeps every past trip's fee and would merge later trips into one.

    Its entry price is the average of its buys and its exit price the average
    of its sells. Its P&L is cash basis: what the sells brought in less what
    the buys cost. For shares that equals the builder's average-cost P&L; for
    crypto it also counts the coins Alpaca kept as the buy's fee. A dollar fee
    the broker charges on a crypto sale is not in the fill price, so it is not
    in these figures. A sell for more than the trip holds counts only up to
    what it holds.

    Option fills (OCC symbols), sells with no open trip (shares that arrived by
    assignment, or opened before the order history) and trips still open are
    left out.
    """
    fills = [r for r in rows if isinstance(r, dict) and _num(r.get("ts"), 3) is not None]
    fills.sort(key=lambda r: float(r["ts"]))
    open_trips: dict[str, dict] = {}
    closed: list[dict] = []
    for r in fills:
        sym = _symbol(r.get("symbol"))
        if sym is None or _OCC_RE.fullmatch(sym):
            continue
        qty, price = _num(r.get("qty"), 9), _num(r.get("price"), 6)
        if not qty or qty <= 0 or not price or price <= 0:
            continue
        side, ts = r.get("side"), float(r["ts"])
        trip = open_trips.get(sym)
        if side == "buy":
            if trip is None:
                trip = open_trips[sym] = {"entry_at": ts, "buy_qty": 0.0, "buy_cost": 0.0,
                                          "sell_qty": 0.0, "sell_value": 0.0}
            trip["buy_qty"] += qty
            trip["buy_cost"] += qty * price
        elif side == "sell" and trip is not None:
            sold = min(qty, trip["buy_qty"] - trip["sell_qty"])
            trip["sell_qty"] += sold
            trip["sell_value"] += sold * price
            left = trip["buy_qty"] - trip["sell_qty"]
            dust = CRYPTO_DUST_FRAC * trip["buy_qty"] if is_crypto_symbol(sym) else 0.0
            if left <= max(_ZERO_QTY, dust):
                del open_trips[sym]
                trip["symbol"], trip["exit_at"] = sym, ts
                trip["pnl"] = trip["sell_value"] - trip["buy_cost"]
                closed.append(trip)
    out = []
    for t in sorted(closed, key=lambda t: t["exit_at"], reverse=True):
        entry, exit_ = t["buy_cost"] / t["buy_qty"], t["sell_value"] / t["sell_qty"]
        row = {
            "symbol": t["symbol"],
            "side": "long",
            "qty": _qty(t["buy_qty"]),
            "entry_price": _price(entry),
            "exit_price": _price(exit_),
            "entry_at": _ts(t["entry_at"]),
            "exit_at": _ts(t["exit_at"]),
            "pnl": _money(t["pnl"]),
            "pnl_pct": _ratio(t["pnl"] / t["buy_cost"]),
            # build_executions does not say which strategy fired a fill, and
            # the journal's order rows do not record it either.
            "strategy": None,
        }
        if None in (row["qty"], row["entry_price"], row["exit_price"], row["pnl"], row["pnl_pct"]):
            continue
        out.append(row)
    return out[:MAX_TRADES]


def trade_stats(trips: list[dict]) -> dict:
    wins = [t for t in trips if t["pnl"] > 0]
    losses = [t for t in trips if t["pnl"] < 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    return {
        "round_trips": len(trips),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _unit(len(wins) / len(trips)) if trips else None,
        "avg_win": _money(mean([t["pnl"] for t in wins])),
        "avg_loss": _money(mean([t["pnl"] for t in losses])),
        "avg_win_pct": _ratio(mean([t["pnl_pct"] for t in wins])),
        "avg_loss_pct": _ratio(mean([t["pnl_pct"] for t in losses])),
        "profit_factor": _num(gross_win / gross_loss, 4, 0.0, 1e6) if gross_loss > 0 else None,
        "realized_pnl": _money(sum(t["pnl"] for t in trips)) or 0.0,
    }


def trades_section(executions: dict) -> dict:
    rows = executions.get("rows")
    if not isinstance(rows, list):
        raise SourceError("no execution rows")
    trips = round_trips(rows)
    return {"stats": trade_stats(trips), "round_trips": trips}


# --------------------------------------------------------------------------
# signals: the research board, without its words
# --------------------------------------------------------------------------
def _contingencies(rules: Any, now: float) -> list[dict]:
    out = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("condition") not in CONDITIONS:
            continue
        level = _price(rule.get("level"))
        until = _utc_ts(rule.get("valid_until"))
        if level is None or until is None or until <= _ts(now):
            continue                            # malformed or expired: the gate ignores it too
        out.append({"condition": rule["condition"], "level": level, "valid_until": until})
    return out[:MAX_CONTINGENCIES]


def signals_section(board: dict, now: float) -> dict:
    rows = board.get("rows")
    if not isinstance(rows, list):
        raise SourceError("no board rows")
    out = []
    for r in rows:
        if not isinstance(r, dict) or _symbol(r.get("symbol")) is None:
            continue
        bias = r.get("bias")
        out.append({
            "symbol": r["symbol"],
            "bias": bias if bias in BIASES else None,
            "priority": _int(r.get("priority"), -1000, 1000),
            "suggested_size": _unit(r.get("suggested_size")),
            "contingencies": _contingencies(r.get("contingencies"), now),
        })
    return {"date": _date(board.get("date")), "rows": out[:MAX_SIGNAL_ROWS]}


# --------------------------------------------------------------------------
# strategy: sigengine/config.json + the risk rules
# --------------------------------------------------------------------------
def _strategy_specs(cfg: dict) -> list[dict]:
    """backtest.multi_strategy.symbol_strategies, whitelisted: a `strategies`
    list, else the legacy single EMA from fast/slow/window."""
    specs = cfg.get("strategies")
    if not isinstance(specs, list):
        specs = [{"type": "ema", "fast": cfg.get("fast"), "slow": cfg.get("slow"),
                  "window": cfg.get("window")}]
    out = []
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("type") not in STRATEGY_PARAMS:
            continue
        row: dict[str, Any] = {"type": spec["type"]}
        for name in STRATEGY_PARAMS[spec["type"]]:
            value = _num(spec.get(name), 6, 0.0, 100000.0)
            if value is not None:
                row[name] = value
        out.append(row)
    return out[:10]


def sleeve_reserve(policy: dict | None) -> float:
    """The share of equity the put sleeve keeps for itself, which the share
    book does not size against (sigengine.engine._sleeve_reserve; that also
    takes the live collateral when it is larger, which is not published).

    decisions/csp.json `reserve_frac` while the sleeve is enabled, coerced as
    relay.csp_manager.config() does: enabled only when literally true, and a
    value that is missing, not a number or outside (0, 1] is the default.
    No policy file, or one that cannot be read, is a disabled sleeve: 0.
    """
    block = policy if isinstance(policy, dict) else {}
    if block.get("enabled") is not True:
        return 0.0
    try:
        frac = float(block.get("reserve_frac", SLEEVE_DEFAULT_RESERVE))
    except (TypeError, ValueError, OverflowError):
        frac = SLEEVE_DEFAULT_RESERVE
    if not 0.0 < frac <= 1.0:                   # NaN fails this too
        frac = SLEEVE_DEFAULT_RESERVE
    return frac


def _watchlist(decisions: dict) -> list | None:
    """The research allowlist as relay.risk_filter.check applies it: None when
    decisions.json declares none (the gate is off), else the list."""
    watchlist = decisions.get("watchlist")
    return watchlist if isinstance(watchlist, list) and watchlist else None


def strategy_section(config: dict, decisions: dict, policy: dict | None = None) -> dict:
    symbols = config.get("symbols")
    if not isinstance(symbols, dict):
        raise SourceError("no symbols in the engine config")
    watchlist = _watchlist(decisions)
    rows = []
    for sym, cfg in symbols.items():
        if _symbol(sym) is None or not isinstance(cfg, dict):
            continue
        interval = cfg.get("interval")
        rows.append({
            "symbol": sym,
            "enabled": _engine_enabled(cfg),
            # The gate refuses every buy, crypto included, for a symbol not
            # on the watchlist (null: no watchlist, so nothing is refused).
            "on_watchlist": None if watchlist is None else canonical_symbol(sym) in watchlist,
            "interval": interval if isinstance(interval, str) and _INTERVAL_RE.fullmatch(interval) else None,
            "stop_pct": _unit(cfg.get("stop")),
            "deploy": _unit(cfg.get("deploy")),
            "strategies": _strategy_specs(cfg),
        })
    # The limits relay.risk_filter.check() applies, read the way it reads them:
    # the decisions value clamped by the hard cap, and allow_extended_hours as
    # bool(value), so a missing key is False. decisions.json's
    # max_open_positions is NOT published: nothing enforces it (one position
    # at a time is relay.order_manager's rotation, fixed in code, and shares
    # the put sleeve was assigned are held alongside it, outside the rotation).
    limit = _unit(decisions.get("daily_loss_limit", HARD_DAILY_LOSS_LIMIT))
    return {
        "symbols": rows[:100],
        "risk": {
            "daily_loss_limit": min(limit, HARD_DAILY_LOSS_LIMIT) if limit is not None else None,
            "daily_loss_hard_cap": HARD_DAILY_LOSS_LIMIT,
            "max_position_shares_cap": HARD_MAX_POSITION,
            "allow_extended_hours": bool(decisions.get("allow_extended_hours", False)),
            # A buy is sized as deploy x (equity less this reserve).
            "sleeve_reserve_pct": _unit(sleeve_reserve(policy)),
        },
    }


def _read_json_or_none(path: Path) -> dict | None:
    """_read_json, or None where that raises (the caller decides what unknown means)."""
    try:
        return _read_json(path)
    except SourceError:
        return None


def _read_json(path: Path) -> dict:
    """A JSON object from `path`; SourceError (with no path in it) otherwise."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(type(exc).__name__) from None
    if not isinstance(data, dict):
        raise SourceError("not a JSON object")
    return data


# --------------------------------------------------------------------------
# recaps: the English YouTube uploads recorded in the archive
# --------------------------------------------------------------------------
def recaps_section(root: Path, limit: int = MAX_RECAPS) -> list[dict]:
    """The newest `limit` recap editions whose meta.json records an ENGLISH
    YouTube upload with privacy exactly "public". Slideshow briefings,
    underscore folders (specials, tests) and the zh cut are never listed. Each
    entry is the edition's US/Eastern date (the archive folder's name: a session
    date, or the Sunday of a weekend edition), the video id and
    the length; nothing else is read out of meta.json. In particular no title:
    a language model writes it, and the site labels each video by its date."""
    try:
        days = sorted((p for p in Path(root).iterdir() if p.is_dir() and _pub._valid_date(p.name)),
                      key=lambda p: p.name, reverse=True)
    except OSError as exc:
        raise SourceError(type(exc).__name__) from None
    out: list[dict] = []
    seen: set[str] = set()
    for day in days:
        found = []
        try:
            slots = [p for p in day.iterdir() if p.is_dir()]
        except OSError:
            continue
        for slot in slots:
            if not _pub._SLOT_RE.fullmatch(slot.name) or slot.name.startswith("slideshow"):
                continue
            meta = _read_meta(slot / "meta.json")
            yt = meta.get("youtube") if meta else None
            en = yt.get("en") if isinstance(yt, dict) else None
            if not isinstance(en, dict) or en.get("privacy") != "public":
                continue
            vid = en.get("id")
            if not isinstance(vid, str) or not _YOUTUBE_ID_RE.fullmatch(vid):
                continue
            dur = _num(meta.get("duration_s"), 1, 0.0, 36000.0)
            found.append((str(meta.get("generated_at") or ""), {
                "date": day.name,
                "youtube_id": vid,
                "duration_s": dur,
            }))
        for _, rec in sorted(found, key=lambda x: x[0], reverse=True):
            if rec["youtube_id"] in seen:
                continue
            seen.add(rec["youtube_id"])
            out.append(rec)
            if len(out) >= limit:
                return out
    return out


def _read_meta(path: Path) -> dict | None:
    try:
        if path.stat().st_size > _pub.META_MAX_BYTES:
            return None
        meta = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------
_VERSION: str | None = None


def export_version() -> str:
    global _VERSION
    if _VERSION is None:
        sha = ""
        try:
            sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short=10", "HEAD"],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        _VERSION = f"{__version__}+g{sha}" if re.fullmatch(r"[0-9a-f]{4,40}", sha) else __version__
    return _VERSION


def build_payload(now: float) -> dict:
    """portfolio.json for time `now`. Each section is built on its own; one
    that fails is null and named in meta.unavailable."""
    unavailable: list[str] = []

    def section(name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:                # noqa: BLE001 — isolate every source
            log.warning("%s unavailable (%s)", name, type(exc).__name__)
            unavailable.append(name)
            return None

    try:
        portfolio = _source(web_server.build_portfolio)
    except Exception as exc:                    # noqa: BLE001
        log.warning("portfolio builder failed (%s)", type(exc).__name__)
        portfolio = None

    def need_portfolio() -> dict:
        if not isinstance(portfolio, dict):
            raise SourceError("no portfolio")
        return portfolio

    account = section("account", lambda: account_section(need_portfolio(), fetch_history(now), now))
    equities = section("positions", lambda: positions_section(
        need_portfolio(), _read_json_or_none(web_server.CONFIG_FILE),
        _read_json_or_none(options_view.AUX_FILE)))
    options = section("options", lambda: options_section(_source(
        lambda: options_view.build_options(now))))
    trades = section("trades", lambda: trades_section(_source(
        lambda: web_server.build_executions(limit=1_000_000))))
    signals = section("signals", lambda: signals_section(_source(web_server.build_board), now))
    strategy = section("strategy", lambda: strategy_section(
        _read_json(web_server.CONFIG_FILE), _read_json(web_server.DECISIONS_FILE),
        options_view.load_policy()))
    recaps = section("recaps", lambda: recaps_section(web_server.MEDIA_ARCHIVE_DIR))

    return {
        "meta": {
            "schema": SCHEMA_VERSION,
            "as_of": _ts(now),
            "data_delay": DATA_DELAY_NOTE,
            "generated_by": f"public_export/{export_version()}",
            "unavailable": [s for s in SECTIONS if s in unavailable],
        },
        "account": account,
        "positions": {"equities": equities, "options": options},
        "trades": trades,
        "signals": signals,
        "strategy": strategy,
        "recaps": recaps,
    }


def content_digest(payload: dict) -> str:
    """sha256 of the payload without meta.as_of (what "unchanged" means)."""
    meta = {k: v for k, v in payload.get("meta", {}).items() if k != "as_of"}
    canon = json.dumps({**payload, "meta": meta}, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def encode(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------
class Exporter:
    def __init__(self, dest, state_dir: Path, schema: dict, *, dry_run: bool = False,
                 force: bool = False, clock: Callable[[], float] = time.time, out=None) -> None:
        self.dest = dest
        self.schema = schema
        self.dry_run = dry_run
        self.force = force
        self.clock = clock
        self.out = out if out is not None else sys.stdout
        self.state = _pub.StateStore(Path(state_dir) / "state.json", dest.ident, readonly=dry_run)

    def run(self) -> int:
        """Build, gate, upload. 0 = sent or unchanged, 1 = refused by the gate."""
        now = self.clock()
        payload = build_payload(now)
        errors = schema_gate.validate(payload, self.schema)
        if errors:
            # Paths and rule names only: the failing value is never logged.
            log.error("portfolio.json failed the schema gate (%d problem(s)); nothing uploaded",
                      len(errors))
            for e in errors[:10]:
                log.error("  %s", e)
            return 1
        body = encode(payload)
        # What ships is these bytes: gate them as the site will read them
        # (schema_gate.loads refuses a duplicate key), not only the dict.
        errors = schema_gate.validate_text(body, self.schema)
        if errors:
            log.error("the encoded portfolio.json failed the schema gate (%d problem(s)); "
                      "nothing uploaded", len(errors))
            return 1
        digest = content_digest(payload)
        missing = payload["meta"]["unavailable"]
        note = f"; unavailable: {', '.join(missing)}" if missing else ""
        if self.dry_run:
            print(f"[dry-run] PUT {PORTFOLIO_KEY}  {len(body)} bytes{note}", file=self.out)
            return 0
        self.state.refresh()
        prev = (self.state.dest().get("objects") or {}).get(PORTFOLIO_KEY) or {}
        if (not self.force and prev.get("sha256") == digest
                and now - float(prev.get("at") or 0) < HEARTBEAT_SEC):
            log.info("%s unchanged%s", PORTFOLIO_KEY, note)
            return 0
        self.dest.put_bytes(check_key(PORTFOLIO_KEY), body, JSON_TYPE, CACHE_CONTROL)

        def record(d: dict) -> None:
            d.setdefault("objects", {})[PORTFOLIO_KEY] = {
                "sha256": digest, "at": now, "size": len(body)}
        self.state.update(record)
        log.info("%s put (%d bytes)%s", PORTFOLIO_KEY, len(body), note)
        return 0


def _bucket_names(env) -> tuple[str, str]:
    public = (env.get("R2_PUBLIC_BUCKET") or DEFAULT_PUBLIC_BUCKET).strip()
    private = (env.get("R2_BUCKET") or PRIVATE_BUCKET).strip()
    return public, private


def make_destination(out_dir: str | None, dry_run: bool, env=None):
    if out_dir:
        return DirDestination(_pub._check_out_dir(Path(out_dir)))
    env = os.environ if env is None else env
    public, private = _bucket_names(env)
    try:
        if not _BUCKET_RE.fullmatch(public):
            raise ConfigError("R2_PUBLIC_BUCKET is not a valid bucket name")
        if public == private:
            raise ConfigError("R2_PUBLIC_BUCKET must name a bucket of its own, "
                              "not the private dashboard's bucket")
        cfg = R2Config.from_env({**env, "R2_BUCKET": public})
        return R2Destination.from_config(cfg)
    except ConfigError as exc:
        if not dry_run:
            raise
        log.warning("%s", exc)
        return _pub.NullDestination()


def lock_path(state_dir: Path, dest) -> Path:
    if dest.ident.startswith("r2:"):
        return state_dir / "export.lock"
    tag = hashlib.sha1(dest.ident.encode()).hexdigest()[:10]
    return state_dir / f"export-dir-{tag}.lock"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m web.public_export",
        description="Publish the public portfolio file (portfolio.json) to its own R2 "
                    "bucket, or to a local folder.")
    ap.add_argument("--once", action="store_true",
                    help="run once and exit (the default; for cron)")
    ap.add_argument("--out-dir", metavar="PATH",
                    help="write PATH/portfolio.json instead of uploading (no boto3, no R2 keys)")
    ap.add_argument("--dry-run", action="store_true",
                    help="build and validate, print what would be uploaded; change nothing")
    ap.add_argument("--force", action="store_true",
                    help="upload even when the content is unchanged")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, *, clock: Callable[[], float] = time.time) -> int:
    args = parse_args(argv)
    if args.verbose:
        log.setLevel(logging.DEBUG)
    try:
        schema = schema_gate.load_schema(SCHEMA_FILE)
    except (OSError, ValueError) as exc:
        log.error("the schema gate is unavailable (%s); nothing is published without it",
                  type(exc).__name__)
        return 2
    try:
        dest = make_destination(args.out_dir, args.dry_run)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    state_dir = Path(STATE_DIR)
    exporter = Exporter(dest, state_dir, schema, dry_run=args.dry_run, force=args.force,
                        clock=clock)
    log.debug("publishing to %s", dest.describe())
    if args.dry_run:                            # reads only: no lock, no state
        return exporter.run()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _pub.try_lock(lock_path(state_dir, dest)) as got:
        if not got:
            log.info("another export holds the lock; exiting")
            return 0
        return exporter.run()


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s",
                                           "%Y-%m-%dT%H:%M:%S%z"))
    handler.addFilter(_pub.RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    log.setLevel(logging.INFO)
    for noisy in ("botocore", "boto3", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    _setup_logging()
    sys.exit(main())

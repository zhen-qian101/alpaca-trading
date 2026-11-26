"""Tests for web/public_export.py: the PUBLIC portfolio file.

Everything this exporter writes is world-readable, so the properties locked in
below are about what can NOT get out:

  * the output is built from a whitelist: builders stuffed with canary fields
    and values (an account number, an order id, a log line, research notes, a
    contingency reason, a current price, a path, an email, a host name, zh
    text) leak none of them, and every key is one the schema declares;
  * no third-party market data: no current/last price, market value, quote,
    bar or candle field exists, and the canary quotes never appear as numbers;
  * daily equity only: no intraday series is requested from the broker or
    published, and a session the broker leaves out never merges two days;
  * no equity from a session that is still going: the current equity and the
    broker's day P&L never reach the file, so equity, entry price, quantity
    and realized P&L cannot give back a live price;
  * a stop is published only where the site can vouch for one;
  * the strategy types are exactly the engine's (backtest/multi_strategy.py
    STRATEGY_RUNNERS), in the exporter's whitelist and in the schema;
  * only English recaps whose recorded YouTube privacy is exactly "public",
    each as its date, video id and length: never its title (a language model
    writes it), the zh cut, a slideshow, a special edition, desk or look data;
  * every output validates against apps/portfolio/schema/portfolio.schema.json,
    including when every source fails, and a payload that does not validate
    is never uploaded;
  * unchanged content is not re-sent (except the hourly heartbeat), runs are
    locked, --dry-run writes nothing, the public bucket is never the private
    one, logs carry no values, and the module cannot reach the order path.

No network and no real data: the broker is an in-memory fake that only the
builders talk to (invented symbols ACME / NOVA / VOLT / QBIT/USD...), and the
decisions, engine config and recap archive are temporary files.

    .venv/bin/python -m pytest -q tests/test_public_export.py
"""
from __future__ import annotations

import ast
import itertools
import json
import logging
import os
import random
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from web import options_view, schema_gate
from web import public_export as pe
from web import server as web_server
from web.r2 import ConfigError, R2Config, R2Destination

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
SCHEMA = schema_gate.load_schema(pe.SCHEMA_FILE)

CREATED_AT = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)        # a Monday
SESSION = date(2026, 6, 12)                                  # a Friday
NOW = datetime(2026, 6, 12, 22, 5, tzinfo=UTC).timestamp()  # 18:05 ET, after the close

# --- canaries: none of these may appear anywhere in the output ---------------
CANARY_ACCOUNT = "CANARYACCT7731"
CANARY_UUID = "c0ffee00-0000-4000-8000-00000000ca11"
CANARY_ORDER = "canary-order-5f3e"
CANARY_LOG = "2026-06-12 11:30:51,651 INFO sigengine.engine: ACME: canary log line"
CANARY_NOTE = "Canary research note: the model says buy everything"
CANARY_REASON = "canary contingency reason text"
CANARY_PATH = "/home/canary-user/secret/state.json"
CANARY_EMAIL = "canary.person@example.com"
CANARY_HOST = "canary-host.example.net"
CANARY_ZH = "中文金丝雀标题"
# A recap's English title: a language model writes it, so it is never published.
CANARY_TITLE = "Canary model-written recap headline"
CANARY_STRINGS = (CANARY_ACCOUNT, CANARY_UUID, CANARY_ORDER, CANARY_LOG, CANARY_NOTE,
                  CANARY_REASON, CANARY_PATH, CANARY_EMAIL, CANARY_HOST, CANARY_ZH, CANARY_TITLE)
# Market quotes: these exact numbers are only ever a third party's price.
QUOTE_ACME = 104.3719
QUOTE_OPTION = 0.3117
QUOTE_UNDERLYING = 33.3337
CANARY_NUMBERS = ("104.3719", "247.1188", "62010.75", "0.3117", "33.3337", "98765.4321",
                  "4321.987", "12524.6281")

# Keys that would carry third-party market data or private data.
FORBIDDEN_KEYS = {
    "current_price", "market_value", "last_price", "lastday_price", "price", "quote", "bid",
    "ask", "bars", "bar", "candles", "candle", "open", "high", "low", "close", "volume",
    "underlying_price", "strike_distance_pct", "change_today", "cost_basis", "cash",
    "buying_power", "notes", "note", "reason", "briefing", "account_number", "id", "order_id",
    "client_order_id", "asset_id", "path", "file", "host", "email", "log", "desk", "look",
    "title", "title_zh", "voice", "error", "mtime",
    # computed from the broker's current price: each gives that price back
    "unrealized_pl", "unrealized_pl_pct", "unrealized_plpc", "stop_distance_pct",
    # an intraday equity series: with a position open it traces that price
    "intraday", "points", "t",
}


def schema_keys(node=SCHEMA, out=None) -> set[str]:
    """Every property name the schema declares, anywhere."""
    out = set() if out is None else out
    if isinstance(node, dict):
        out.update((node.get("properties") or {}).keys())
        for v in node.values():
            schema_keys(v, out)
    elif isinstance(node, list):
        for v in node:
            schema_keys(v, out)
    return out


def all_keys(obj, out=None) -> set[str]:
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            all_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            all_keys(v, out)
    return out


def text_of(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def assert_valid(payload) -> None:
    errors = schema_gate.validate(payload, SCHEMA)
    assert errors == [], errors


# --------------------------------------------------------------------------
# the fake broker and the world around it
# --------------------------------------------------------------------------
def sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def et_epoch(d: date, hh: int, mm: int = 0) -> float:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).timestamp()


_ORDER_IDS = itertools.count(1)


class FakeTrading:
    """The TradingClient slice build_executions and fetch_history call."""

    def __init__(self, broker: "FakeBroker") -> None:
        self.broker = broker

    def get_orders(self, req):
        """Pages like Alpaca's GET /v2/orders, which web.server._closed_orders
        walks: at most `limit` (<= 500) closed orders sorted by submission
        time in `direction`, only those submitted strictly after `after` and
        strictly before `until`."""
        self.broker.order_requests.append(req)
        assert str(getattr(req.status, "value", req.status)) == "closed"
        assert req.limit is not None and req.limit <= 500
        desc = str(getattr(req.direction, "value", req.direction) or "desc") == "desc"
        rows = sorted(self.broker.orders, key=lambda o: o.submitted_at, reverse=desc)
        if getattr(req, "after", None) is not None:
            rows = [o for o in rows if o.submitted_at > req.after]
        if req.until is not None:
            rows = [o for o in rows if o.submitted_at < req.until]
        return rows[:req.limit]

    def get_portfolio_history(self, req):
        self.broker.history_requests.append(req)
        if self.broker.history_error:
            raise RuntimeError(f"history down {CANARY_HOST}")
        # Daily only: the exporter must never ask for an intraday series.
        series = self.broker.daily if req.timeframe == "1D" else self.broker.session_path
        return SimpleNamespace(
            timestamp=[t for t, _ in series], equity=[e for _, e in series],
            profit_loss=[0.0 for _ in series], profit_loss_pct=[0.0 for _ in series],
            base_value=100000.0, timeframe=req.timeframe,
            cashflow={"account": CANARY_ACCOUNT})

    def get_calendar(self, req):
        """alpaca-py's Calendar rows: weekdays, open/close as naive New York times."""
        if self.broker.calendar_error:
            raise RuntimeError(f"calendar down {CANARY_HOST}")
        return [SimpleNamespace(date=d, open=datetime.combine(d, time(9, 30)),
                                close=datetime.combine(d, time(16)))
                for d in sessions(req.start, req.end) if d not in self.broker.holidays]


class FakeBroker:
    """The AlpacaClient slice the builders use: account, positions, last trade."""

    paper = True

    def __init__(self, *, trips: int, seed: int = 7) -> None:
        rnd = random.Random(seed)
        self.client = FakeTrading(self)
        self.history_error = False
        self.calendar_error = False
        self.holidays: set[date] = set()
        self.history_requests: list = []
        self.order_requests: list = []
        # Daily closes, stamped 20:00 ET like Alpaca, from a few days BEFORE
        # the account existed (filtered out) to the day before the session
        # (the broker has not rolled the session's own point yet).
        eq, self.daily = 100000.0, []
        for d in sessions(date(2026, 1, 1), SESSION - timedelta(days=1)):
            if d >= CREATED_AT.date() + timedelta(days=1):
                eq = round(eq * (1 + rnd.gauss(0.0006, 0.009)), 2)
            self.daily.append((et_epoch(d, 20), eq))
        # The session's own moves to the current equity. The broker would serve
        # this as an intraday series; the exporter must never ask for it.
        self.session_path = []
        for i in range(79):                     # 09:30 .. 16:00 ET, 5 minutes
            eq = round(eq * (1 + rnd.gauss(0, 0.0008)), 2)
            self.session_path.append((et_epoch(SESSION, 9, 30) + 300 * i, eq))
        self.account = SimpleNamespace(
            equity=str(eq), last_equity=str(self.daily[-1][1]), cash="4321.987",
            buying_power="98765.4321", status="AccountStatus.ACTIVE", created_at=CREATED_AT,
            account_number=CANARY_ACCOUNT, id=CANARY_UUID)
        self.positions = [
            self._pos("ACME", 120, 101.25, QUOTE_ACME),
            self._pos("NOVA", 40, 250.5, 247.1188),
            self._pos("QBIT/USD", 0.5, 61234.5, 62010.75),
            SimpleNamespace(symbol="VOLT260619P00035000", qty="-1", side="PositionSide.SHORT",
                            avg_entry_price="0.42", current_price=str(QUOTE_OPTION),
                            market_value="-31.17", unrealized_pl="10.83", unrealized_plpc="0.2579",
                            cost_basis="-42", asset_class=SimpleNamespace(value="us_option"),
                            asset_id=CANARY_UUID),
        ]
        self.orders = self._orders(rnd, trips)

    @staticmethod
    def _pos(sym, qty, entry, price):
        return SimpleNamespace(
            symbol=sym, qty=str(qty), side="PositionSide.LONG", avg_entry_price=str(entry),
            current_price=str(price), market_value=str(round(qty * price, 4)),
            cost_basis=str(qty * entry), unrealized_pl=str(round(qty * (price - entry), 4)),
            unrealized_plpc=str(round(price / entry - 1, 6)),
            asset_class=SimpleNamespace(value="us_equity"), asset_id=CANARY_UUID)

    @staticmethod
    def _order(sym, side, qty, price, when, filled=True):
        # Each order has its own id (the paging drops repeats by id), and
        # every id still carries the canary.
        return SimpleNamespace(
            symbol=sym, side=f"OrderSide.{side.upper()}", filled_qty=str(qty if filled else 0),
            filled_avg_price=str(price) if filled else None,
            filled_at=datetime.fromtimestamp(when, UTC) if filled else None,
            submitted_at=datetime.fromtimestamp(when, UTC),
            order_type="OrderType.MARKET", id=f"{CANARY_UUID}-{next(_ORDER_IDS)}",
            client_order_id=CANARY_ORDER)

    def _orders(self, rnd, trips):
        syms = ["ACME", "NOVA", "ORBX", "LUMA", "QBIT/USD", "ZEPH"]
        days = sessions(CREATED_AT.date() + timedelta(days=1), SESSION - timedelta(days=3))
        out = []
        for i in range(trips):
            d = days[(i * 5) % len(days)]
            sym = syms[i % len(syms)]
            qty = 0.25 if sym == "QBIT/USD" else rnd.randint(10, 90)
            entry = round(rnd.uniform(20, 400), 2)
            exit_ = round(entry * (1 + rnd.gauss(0.002, 0.02)), 2)
            out.append(self._order(sym, "buy", qty, entry, et_epoch(d, 10, 30)))
            if i % 7 == 3:                      # a scale-in and a two-step exit
                out.append(self._order(sym, "buy", qty, round(entry * 1.01, 2), et_epoch(d, 11)))
                out.append(self._order(sym, "sell", qty, exit_, et_epoch(d, 13)))
                out.append(self._order(sym, "sell", qty, round(exit_ * 1.003, 2), et_epoch(d, 15)))
            else:
                out.append(self._order(sym, "sell", qty, exit_, et_epoch(d, 14, 45)))
        # Noise that must not become a trade: an expired order, the put
        # sleeve's option fills, and assigned shares sold with no buy.
        d = days[-1]
        out.append(self._order("NOVA", "buy", 5, 0, et_epoch(d, 9, 45), filled=False))
        out.append(self._order("VOLT260605P00030000", "sell", 1, 0.55, et_epoch(d, 15, 45)))
        out.append(self._order("VOLT260605P00030000", "buy", 1, 0.05, et_epoch(d, 15, 50)))
        out.append(self._order("VOLT", "sell", 100, 29.4, et_epoch(d, 15, 55)))
        # The open ACME position: bought, never sold.
        out.append(self._order("ACME", "buy", 120, 101.25, et_epoch(SESSION, 10)))
        out.sort(key=lambda o: o.filled_at or datetime.min.replace(tzinfo=UTC))
        return out

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return list(self.positions)

    def get_latest_trade(self, symbol):
        return QUOTE_UNDERLYING, NOW


WATCHLIST = ["ACME", "NOVA", "ORBX", "LUMA", "QBIT/USD", "ZEPH", "KITE", "VOLT", "PYRO", "FERN"]


def write_decisions(path: Path) -> None:
    longs = {"ACME", "NOVA", "LUMA"}
    path.write_text(json.dumps({
        "date": SESSION.isoformat(),
        "watchlist": WATCHLIST,
        "bias": {s: ("long" if s in longs else "short" if s == "PYRO" else "flat") for s in WATCHLIST},
        "priority": {s: (3 - i if s in longs else 0) for i, s in enumerate(WATCHLIST)},
        "suggested_size": {s: (0.97 if s in longs else 0.0) for s in WATCHLIST},
        "max_position": {s: 10000 for s in WATCHLIST},
        "max_open_positions": 1,
        "daily_loss_limit": 0.05,
        "allow_extended_hours": True,
        "notes": {"_": CANARY_NOTE, "ACME": CANARY_NOTE},
        "briefing": {"_": CANARY_NOTE},
        "contingencies": {
            "ORBX": [
                {"condition": "close_above", "level": 61.5, "action": "long",
                 "valid_until": "2026-06-16T04:00:00+00:00", "reason": CANARY_REASON},
                {"condition": "close_above", "level": 58.0, "action": "long",   # expired
                 "valid_until": "2026-06-01T04:00:00+00:00", "reason": CANARY_REASON},
                {"condition": "sideways", "level": 1.0, "valid_until": "2026-06-16T04:00:00Z"},
            ],
            "ZEPH": [{"condition": "close_below", "level": 12.25, "action": "long",
                      "valid_until": "2026-06-15T20:00:00Z", "reason": CANARY_REASON}],
        },
    }))


def write_engine_config(path: Path) -> None:
    def symbol(stop, enabled=True, **extra):
        return {"fast": 9, "slow": 50, "window": 300, "interval": "1h", "deploy": 0.97,
                "stop": stop, "enabled": enabled, "_stop_note": CANARY_NOTE,
                "strategies": [
                    {"type": "macd_sma", "fast_len": 12, "slow_len": 26, "signal_len": 9,
                     "veryslow_len": 150, "_note": CANARY_PATH},
                    {"type": "bollinger_rsi", "rsi_length": 6, "bb_length": 20, "bb_mult": 1.5},
                ], **extra}
    legacy = {"fast": 20, "slow": 75, "window": 300, "interval": "1h", "deploy": 0.97,
              "stop": 0.06, "enabled": False}
    path.write_text(json.dumps({
        "_comment": CANARY_PATH,
        "poll_seconds": 300,
        "data": {"primary": "ibkr", "ibkr_host": CANARY_HOST, "ibkr_port": 4002},
        "symbols": {
            "ACME": symbol(0.02),
            "NOVA": symbol(0.02),
            "ORBX": {**symbol(0.03), "strategies": [{"type": "ema", "fast": 12, "slow": 50,
                                                     "window": 200}]},
            "QBIT/USD": legacy,
            # an unknown type, and one the engine no longer runs (its module is gone)
            "ZEPH": {**symbol(0.02), "strategies": [
                {"type": "mystery", "secret": CANARY_HOST},
                {"type": "donchian", "entry_window": 55, "exit_window": 20}]},
            "../etc": symbol(0.02),
        },
    }))


def write_edition(root: Path, day: str, slot: str, meta: dict) -> None:
    d = root / day / slot
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))


def recap_meta(n: int, *, privacy="public", vid=None, **extra) -> dict:
    return {
        "date": "x", "title": f"{CANARY_TITLE} number {n}",
        "title_zh": CANARY_ZH, "duration_s": 61.0 + n, "generated_at": f"2026-06-{n:02d}T20:40:00",
        "briefing_file": "briefing.mp4", "briefing_file_zh": "briefing_zh.mp4",
        "desk": {"account": CANARY_ACCOUNT, "equity": 98765.4321},
        "look": {"outfit": CANARY_NOTE}, "voice": CANARY_EMAIL, "renderer": CANARY_HOST,
        "clips": [{"file": CANARY_PATH}],
        "youtube": {"en": {"id": vid or f"FAKEyt{n:05d}", "url": f"https://youtu.be/{vid}",
                           "posted_at": "2026-06-01T21:00:00Z", "privacy": privacy,
                           "uploader": CANARY_EMAIL},
                    "zh": {"id": "ZhCanary001", "privacy": "public"}},
        **extra,
    }


def write_archive(root: Path, recaps: int) -> list[str]:
    """`recaps` public English recaps, one per session, newest last. Returns
    their dates."""
    days = sessions(date(2026, 5, 1), SESSION)[-recaps:] if recaps else []
    for i, d in enumerate(days, start=1):
        write_edition(root, d.isoformat(), "video", recap_meta(i))
    return [d.isoformat() for d in days]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return install_world(tmp_path, monkeypatch)


def install_world(tmp_path: Path, monkeypatch, *, trips: int = 20, recaps: int = 4) -> SimpleNamespace:
    broker = FakeBroker(trips=trips)
    decisions = tmp_path / "decisions.json"
    config = tmp_path / "config.json"
    csp = tmp_path / "csp.json"
    archive = tmp_path / "briefings"
    archive.mkdir()
    write_decisions(decisions)
    write_engine_config(config)
    csp.write_text(json.dumps({"enabled": True, "shadow": False, "underlying": "VOLT"}))
    recap_dates = write_archive(archive, recaps)
    monkeypatch.setattr(web_server, "_client", lambda: broker)
    monkeypatch.setattr(web_server, "DECISIONS_FILE", decisions)
    monkeypatch.setattr(web_server, "CONFIG_FILE", config)
    monkeypatch.setattr(web_server, "MEDIA_ARCHIVE_DIR", archive)
    monkeypatch.setattr(options_view, "CSP_CONFIG_FILE", csp)
    aux = tmp_path / "state_entries.json"
    write_entries(aux, opened=["ACME", "NOVA", "QBIT/USD"])
    monkeypatch.setattr(options_view, "AUX_FILE", aux)
    monkeypatch.setattr(options_view, "JOURNAL_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(pe, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(pe, "_VERSION", "1.0.0")
    return SimpleNamespace(broker=broker, root=tmp_path, archive=archive, decisions=decisions,
                           config=config, aux=aux, recap_dates=recap_dates, clock=lambda: NOW)


def write_entries(path: Path, *, opened=(), adopted=()) -> None:
    """sigengine's entry sidecar: what the engine opened, what it adopted."""
    path.write_text(json.dumps({
        "pending": {},
        "opened": {s: "2026-06-12T14:00:00+00:00" for s in opened},
        "adopted": {s: {"basis": 30.0, "anchor": 29.5, "qty": 100, "reason": "csp_assignment",
                        "underlying": s, "since": "2026-06-08T20:00:00+00:00"} for s in adopted},
    }))


def stuff(obj):
    """Add canary keys to every dict in a builder's output."""
    if isinstance(obj, dict):
        for v in list(obj.values()):
            stuff(v)
        obj.update({
            "account_number": CANARY_ACCOUNT, "id": CANARY_UUID, "order_id": CANARY_ORDER,
            "log": CANARY_LOG, "notes": CANARY_NOTE, "reason": CANARY_REASON,
            "current_price": QUOTE_ACME, "market_value": 12524.6281, "path": CANARY_PATH,
            "email": CANARY_EMAIL, "host": CANARY_HOST, "title_zh": CANARY_ZH,
            "bars": [[1, 2, 3, 4]], "underlying_price": QUOTE_UNDERLYING,
        })
    elif isinstance(obj, list):
        for v in obj:
            stuff(v)
    return obj


@pytest.fixture
def canary_world(world, monkeypatch):
    """The world, with every builder's output stuffed with canaries and the
    archive full of editions that must not be listed."""
    for mod, name in ((web_server, "build_portfolio"), (web_server, "build_board"),
                      (web_server, "build_executions"), (options_view, "build_options"),
                      (pe, "fetch_history")):
        original = getattr(mod, name)
        monkeypatch.setattr(mod, name, lambda *a, _f=original, **k: stuff(_f(*a, **k)))
    a = world.archive
    write_edition(a, "2026-06-12", "video-unlisted", recap_meta(30, privacy="unlisted", vid="Unlisted001"))
    write_edition(a, "2026-06-12", "video-private", recap_meta(31, privacy="private", vid="Private0001"))
    write_edition(a, "2026-06-12", "video-caps", recap_meta(32, privacy="Public", vid="CapsPublic1"))
    write_edition(a, "2026-06-12", "video-nopriv", recap_meta(33, privacy=None, vid="NoPrivacy01"))
    write_edition(a, "2026-06-12", "slideshow-midday-briefing", recap_meta(34, vid="Slideshow01"))
    write_edition(a, "_special_2026-06-05", "video", recap_meta(35, vid="Special0001"))
    write_edition(a, "2026-06-12", "video-zh-only",
                  {**recap_meta(36), "youtube": {"zh": {"id": "ZhOnlyPub01", "privacy": "public"}}})
    write_edition(a, "2026-06-12", "video-badid", recap_meta(37, vid="not a youtube id!"))
    return world


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------
def test_the_payload_validates_and_carries_every_section(world):
    p = pe.build_payload(NOW)
    assert_valid(p)
    assert p["meta"] == {"schema": 1, "as_of": "2026-06-12T22:05:00Z",
                         "data_delay": pe.DATA_DELAY_NOTE,
                         "generated_by": "public_export/1.0.0", "unavailable": []}
    acct = p["account"]
    assert acct["paper"] is True
    assert acct["start_date"] == "2026-01-05"
    assert acct["daily"][0]["date"] == "2026-01-05"             # pre-account days dropped
    # NOW is 18:05 ET on 06-12, a session that is not over: it ends at 06-11
    assert acct["daily"][-1] == {"date": "2026-06-11", "equity": acct["equity"]}
    assert acct["equity"] == world.broker.daily[-1][1]
    assert "intraday" not in acct
    # build_portfolio orders by market value, which is not published
    assert [r["symbol"] for r in p["positions"]["equities"]] == ["QBIT/USD", "ACME", "NOVA"]
    assert p["positions"]["equities"][1] == {
        "symbol": "ACME", "side": "long", "qty": 120.0, "avg_entry_price": 101.25,
        "stop_pct": 0.02}
    # QBIT/USD is paused in the engine config: nothing would stop it out
    assert p["positions"]["equities"][0]["stop_pct"] is None
    assert p["positions"]["options"] == [{
        "underlying": "VOLT", "type": "put", "side": "short", "strike": 35.0,
        "expiry": "2026-06-19", "qty": 1.0, "premium_received": 42.0}]
    assert p["trades"]["stats"]["round_trips"] == 20 == len(p["trades"]["round_trips"])
    assert len(p["signals"]["rows"]) == 10
    assert [r["date"] for r in p["recaps"]] == sorted(world.recap_dates, reverse=True)


def test_every_section_failing_still_validates_and_names_them(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"down {CANARY_PATH}")
    monkeypatch.setattr(web_server, "build_portfolio", lambda: {"error": f"APIError {CANARY_HOST}"})
    monkeypatch.setattr(web_server, "build_board", boom)
    monkeypatch.setattr(web_server, "build_executions", lambda **k: {"error": CANARY_UUID})
    monkeypatch.setattr(options_view, "build_options", boom)
    monkeypatch.setattr(web_server, "CONFIG_FILE", world.root / "missing.json")
    monkeypatch.setattr(web_server, "MEDIA_ARCHIVE_DIR", world.root / "no-archive")
    p = pe.build_payload(NOW)
    assert_valid(p)
    assert p["meta"]["unavailable"] == list(pe.SECTIONS)
    assert p["account"] is p["trades"] is p["signals"] is p["strategy"] is p["recaps"] is None
    assert p["positions"] == {"equities": None, "options": None}
    assert "canary" not in text_of(p).lower()


def test_a_history_outage_takes_only_the_account_section(world):
    world.broker.history_error = True
    p = pe.build_payload(NOW)
    assert_valid(p)
    assert p["meta"]["unavailable"] == ["account"]
    assert p["account"] is None and p["positions"]["equities"]


def test_a_calendar_outage_still_publishes_the_daily_curve(world):
    world.broker.calendar_error = True
    p = pe.build_payload(NOW)
    assert_valid(p)
    assert p["meta"]["unavailable"] == []
    assert p["account"]["daily"][-1]["date"] == "2026-06-11"   # nothing invented for the session


def test_daily_equity_only(world):
    """Owner decision: no intraday equity is requested or published. With a
    position open, equity every 5 minutes over its quantity traces that
    symbol's price through the session."""
    p = pe.build_payload(NOW)
    assert world.broker.history_requests
    assert {r.timeframe for r in world.broker.history_requests} == {"1D"}
    assert all(getattr(r, "intraday_reporting", None) is None for r in world.broker.history_requests)
    assert set(p["account"]) == {
        "paper", "start_date", "start_equity", "equity", "day_pl_pct", "total_return_pct",
        "max_drawdown_pct", "best_day", "worst_day", "daily"}
    dates = [d["date"] for d in p["account"]["daily"]]
    assert len(dates) == len(set(dates))                        # one point per session
    assert not {"intraday", "points"} & schema_keys()
    # none of the session's 5-minute values is in the file, the last one (the
    # current equity) included
    path = {e for _, e in world.broker.session_path}
    assert float(world.broker.account.equity) in path
    assert not path & set(_numbers(p))


def _numbers(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield float(obj)


def test_an_empty_book_validates(world):
    world.broker.positions = []
    world.broker.orders = []
    p = pe.build_payload(NOW)
    assert_valid(p)
    assert p["positions"] == {"equities": [], "options": []}
    assert p["trades"]["stats"] == {
        "round_trips": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_win": None,
        "avg_loss": None, "avg_win_pct": None, "avg_loss_pct": None, "profit_factor": None,
        "realized_pnl": 0.0}


# --------------------------------------------------------------------------
# leaks
# --------------------------------------------------------------------------
def test_canaries_in_every_builder_never_reach_the_output(canary_world):
    p = pe.build_payload(NOW)
    assert_valid(p)
    text = text_of(p)
    for c in CANARY_STRINGS:
        assert c not in text, c
    assert "canary" not in text.lower()
    for n in CANARY_NUMBERS:
        assert n not in text, n
    assert not re.search(r"[　-鿿]", text)              # no zh text at all


def test_every_key_is_declared_by_the_schema_and_none_is_forbidden(canary_world):
    keys = all_keys(pe.build_payload(NOW))
    assert keys <= schema_keys()
    assert not keys & FORBIDDEN_KEYS
    assert not {k for k in keys if re.search(r"zh|look|desk|stylist|voice|host|path|file", k)}
    # and the schema itself declares none of them
    assert not schema_keys() & FORBIDDEN_KEYS


def test_no_third_party_price_survives_even_as_a_number(canary_world):
    def numbers(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from numbers(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from numbers(v)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            yield float(obj)
    found = set(numbers(pe.build_payload(NOW)))
    assert not found & {float(n) for n in CANARY_NUMBERS}


def test_no_position_figure_gives_back_the_current_price(canary_world):
    """Owner decision: no current or last price. An open position's
    unrealized P&L or distance to the stop, with its entry price and
    quantity, is the broker's mark to the cent, so none of them is published."""
    p = pe.build_payload(NOW)
    acme = next(r for r in p["positions"]["equities"] if r["symbol"] == "ACME")
    assert set(acme) == {"symbol", "side", "qty", "avg_entry_price", "stop_pct"}
    entry, qty, stop = acme["avg_entry_price"], acme["qty"], acme["stop_pct"]
    figures = [v for r in p["positions"]["equities"] for v in r.values()
               if isinstance(v, (int, float)) and not isinstance(v, bool)]
    for x in figures:
        for implied in (entry * (1 + x), entry + x / qty, entry * (1 - stop) * (1 + x)):
            assert abs(implied - QUOTE_ACME) > 0.01, (x, implied)


def test_only_public_english_recaps_are_listed(canary_world):
    recaps = pe.build_payload(NOW)["recaps"]
    ids = {r["youtube_id"] for r in recaps}
    assert ids == {f"FAKEyt{n:05d}" for n in range(1, 5)}
    for r in recaps:
        assert set(r) == {"date", "youtube_id", "duration_s"}


def test_recaps_are_the_newest_twelve_once_each(world):
    write_archive(world.archive, 20)
    # the same upload recorded twice keeps only the newest
    write_edition(world.archive, "2026-06-12", "video-rerun",
                  recap_meta(99, vid="FAKEyt00020", generated_at="2026-06-30T23:00:00"))
    recaps = pe.recaps_section(world.archive)
    assert len(recaps) == 12
    assert [r["date"] for r in recaps] == sorted({r["date"] for r in recaps}, reverse=True)
    assert len({r["youtube_id"] for r in recaps}) == 12
    assert recaps[0] == {"date": "2026-06-12", "youtube_id": "FAKEyt00020", "duration_s": 160.0}


def test_a_recap_is_its_date_id_and_length_never_its_title(world):
    """A language model writes each edition's title, so the file carries
    none: the site labels a recap by its date."""
    recaps = pe.build_payload(NOW)["recaps"]
    assert recaps and all(set(r) == {"date", "youtube_id", "duration_s"} for r in recaps)
    assert [r["date"] for r in recaps] == sorted(world.recap_dates, reverse=True)
    assert CANARY_TITLE not in json.dumps(recaps)


def test_the_signals_board_carries_no_words(world):
    sig = pe.build_payload(NOW)["signals"]
    assert sig["date"] == "2026-06-12"
    rows = {r["symbol"]: r for r in sig["rows"]}
    assert set(rows) == set(WATCHLIST)
    assert rows["ACME"]["bias"] == "long" and rows["PYRO"]["bias"] == "short"
    assert rows["ACME"]["suggested_size"] == 0.97 and isinstance(rows["ACME"]["priority"], int)
    # active, well-formed rules only: no reason, no action, no expired rule
    assert rows["ORBX"]["contingencies"] == [
        {"condition": "close_above", "level": 61.5, "valid_until": "2026-06-16T04:00:00Z"}]
    assert rows["ZEPH"]["contingencies"] == [
        {"condition": "close_below", "level": 12.25, "valid_until": "2026-06-15T20:00:00Z"}]


def test_strategy_section_whitelists_parameters(world):
    st = pe.build_payload(NOW)["strategy"]
    syms = {r["symbol"]: r for r in st["symbols"]}
    assert set(syms) == {"ACME", "NOVA", "ORBX", "QBIT/USD", "ZEPH"}    # "../etc" dropped
    assert syms["ACME"] == {
        "symbol": "ACME", "enabled": True, "on_watchlist": True, "interval": "1h",
        "stop_pct": 0.02, "deploy": 0.97,
        "strategies": [
            {"type": "macd_sma", "fast_len": 12, "slow_len": 26, "signal_len": 9, "veryslow_len": 150},
            {"type": "bollinger_rsi", "rsi_length": 6, "bb_length": 20, "bb_mult": 1.5}]}
    assert syms["ORBX"]["strategies"] == [{"type": "ema", "fast": 12, "slow": 50, "window": 200}]
    assert syms["QBIT/USD"]["enabled"] is False
    assert syms["QBIT/USD"]["strategies"] == [{"type": "ema", "fast": 20, "slow": 75, "window": 300}]
    assert syms["ZEPH"]["strategies"] == []        # an unknown or retired type is left out
    assert st["risk"] == {"daily_loss_limit": 0.05, "daily_loss_hard_cap": 0.05,
                          "max_position_shares_cap": 10000, "allow_extended_hours": True,
                          "sleeve_reserve_pct": 0.2}


def _runner_params() -> dict[str, set[str]]:
    """The parameters each of the engine's strategy runners reads from its
    spec, found by running it on a spec that records every key asked of it.
    The runner reads them all before it touches the bars, so no bars are
    needed: it fails on the missing bars after the reads."""
    from backtest.multi_strategy import STRATEGY_RUNNERS

    out: dict[str, set[str]] = {}
    for kind, runner in STRATEGY_RUNNERS.items():
        seen: set[str] = set()

        class Spec(dict):
            def __getitem__(self, key, seen=seen):
                seen.add(key)
                return 1

        try:
            runner(None, Spec(), 0.97, 0.02)
        except Exception:       # noqa: BLE001 - no bars; the reads came first
            pass
        out[kind] = seen
    return out


def test_strategy_types_are_exactly_the_engines():
    """The exporter's whitelist, the schema and the engine name the same
    strategy types, and each type the same parameters: a type the engine
    drops (Donchian, 2026) leaves all three, and one it adds is not silently
    dropped from the public page."""
    engine = _runner_params()
    assert engine and all(engine.values())
    assert {k: set(v) for k, v in pe.STRATEGY_PARAMS.items()} == engine

    schema = json.loads(pe.SCHEMA_FILE.read_text())
    defs = schema["$defs"]
    items = defs["strategy"]["properties"]["symbols"]["items"]
    specs = {}
    for ref in items["properties"]["strategies"]["items"]["oneOf"]:
        spec = defs[ref["$ref"].rsplit("/", 1)[1]]
        specs[spec["properties"]["type"]["const"]] = set(spec["properties"]) - {"type"}
    assert specs == engine
    # and no definition is left behind for a type the engine dropped
    assert {k for k in defs if k.startswith("strategy_")} - {"strategy_param"} == {
        f"strategy_{k}" for k in engine}
    trip = defs["round_trip"]["properties"]["strategy"]["anyOf"]
    assert sorted(next(b["enum"] for b in trip if "enum" in b)) == sorted(engine)


def test_symbols_off_the_watchlist_are_marked(world):
    """The allowlist binds crypto too (only the bias gate exempts it)."""
    d = json.loads(world.decisions.read_text())
    d["watchlist"] = [s for s in WATCHLIST if s not in ("QBIT/USD", "ORBX")]
    world.decisions.write_text(json.dumps(d))
    syms = {r["symbol"]: r["on_watchlist"] for r in pe.build_payload(NOW)["strategy"]["symbols"]}
    assert syms == {"ACME": True, "NOVA": True, "ORBX": False, "QBIT/USD": False, "ZEPH": True}
    # no watchlist declared: the gate refuses nothing, so nothing is marked
    d.pop("watchlist")
    world.decisions.write_text(json.dumps(d))
    st = pe.build_payload(NOW)["strategy"]
    assert {r["on_watchlist"] for r in st["symbols"]} == {None}


def test_a_crypto_alias_on_the_watchlist_counts_as_the_gate_counts_it():
    cfg = {"symbols": {"BTC/USD": {"enabled": True, "deploy": 0.97}}}
    on = lambda wl: pe.strategy_section(cfg, {"watchlist": wl})["symbols"][0]["on_watchlist"]
    assert on(["BTC/USD"]) is True
    assert on(["ACME"]) is False
    assert on([]) is None                       # an empty list is no allowlist at all


@pytest.mark.parametrize("block", [
    None, {}, {"enabled": False, "reserve_frac": 0.3}, {"enabled": "true", "reserve_frac": 0.3},
    {"enabled": True}, {"enabled": True, "reserve_frac": 0.35}, {"enabled": True, "reserve_frac": "0.25"},
    {"enabled": True, "reserve_frac": 0}, {"enabled": True, "reserve_frac": 1.5},
    {"enabled": True, "reserve_frac": None}, {"enabled": True, "reserve_frac": "lots"},
    {"enabled": True, "reserve_frac": float("nan")}, {"enabled": True, "reserve_frac": 1},
])
def test_the_sleeve_reserve_is_what_the_engine_sizes_against(block):
    """sigengine.engine._sleeve_reserve: reserve_frac from csp_manager.config()
    while the sleeve is enabled, else nothing."""
    from relay.csp_manager import DEFAULTS, config
    cfg = config({"csp": block} if block is not None else {})
    expected = float(cfg.get("reserve_frac") or 0.0) if cfg.get("enabled") else 0.0
    assert pe.sleeve_reserve(block) == expected
    assert pe.SLEEVE_DEFAULT_RESERVE == DEFAULTS["reserve_frac"]


def test_the_export_reads_the_sleeve_policy(world):
    csp = options_view.CSP_CONFIG_FILE
    risk = lambda: pe.build_payload(NOW)["strategy"]["risk"]["sleeve_reserve_pct"]
    assert risk() == 0.2                        # enabled, default reserve
    csp.write_text(json.dumps({"enabled": True, "reserve_frac": 0.32}))
    assert risk() == 0.32
    csp.write_text(json.dumps({"enabled": False, "reserve_frac": 0.32}))
    assert risk() == 0.0
    csp.write_text("{ half written")
    assert risk() == 0.0                        # unreadable: the sleeve is off
    csp.unlink()
    assert risk() == 0.0


def test_risk_limits_are_read_the_way_the_gate_reads_them(world):
    d = json.loads(world.decisions.read_text())
    d.update(daily_loss_limit=0.2, max_open_positions=50)
    d.pop("allow_extended_hours")
    world.decisions.write_text(json.dumps(d))
    risk = pe.build_payload(NOW)["strategy"]["risk"]
    assert risk["daily_loss_limit"] == pe.HARD_DAILY_LOSS_LIMIT
    # relay.risk_filter.check: bool(d.get("allow_extended_hours", False))
    assert risk["allow_extended_hours"] is False
    # max_open_positions is enforced nowhere, so it is not published
    assert not {"max_open_positions", "max_open_positions_hard_cap"} & set(risk)


@pytest.mark.parametrize("value, gate", [(True, True), (False, False), (1, True), (0, False),
                                         ("yes", True), ("", False), (None, False)])
def test_extended_hours_is_published_as_the_gate_reads_it(value, gate, monkeypatch, tmp_path):
    from relay import risk_filter
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"allow_extended_hours": value}))
    monkeypatch.setattr(risk_filter, "DECISIONS_FILE", decisions)
    assert bool(risk_filter._load_decisions().get("allow_extended_hours", False)) is gate
    cfg = {"symbols": {}}
    assert pe.strategy_section(cfg, {"allow_extended_hours": value})["risk"]["allow_extended_hours"] is gate


# --------------------------------------------------------------------------
# the numbers
# --------------------------------------------------------------------------
def hist(points, start="2026-03-02", sessions=None):
    s = datetime.fromisoformat(start).replace(hour=15, tzinfo=UTC).timestamp()
    return {"start": s, "daily": [(et_epoch(date.fromisoformat(d), 20), e) for d, e in points],
            "sessions": sessions}


def weekdays(first: str, last: str) -> list[str]:
    return [d.isoformat() for d in sessions(date.fromisoformat(first), date.fromisoformat(last))]


def test_account_statistics():
    h = hist([("2026-02-27", 90000.0),                         # before the account: dropped
              ("2026-03-02", 100000.0), ("2026-03-03", 102000.0), ("2026-03-04", 96900.0),
              ("2026-03-05", 98000.0)],
             sessions=weekdays("2026-02-23", "2026-03-06"))
    # 03-06 is in session: the broker's live equity and day P&L are ignored
    now = et_epoch(date(2026, 3, 6), 14)
    a = pe.account_section({"equity": 99000.0, "day_pl_pct": 0.0102, "paper": True}, h, now)
    assert a["start_date"] == "2026-03-02" and a["start_equity"] == 100000.0
    assert [p["date"] for p in a["daily"]] == ["2026-03-02", "2026-03-03", "2026-03-04",
                                               "2026-03-05"]
    assert a["daily"][-1] == {"date": "2026-03-05", "equity": 98000.0}
    assert a["equity"] == 98000.0
    assert a["total_return_pct"] == -0.02
    assert a["max_drawdown_pct"] == -0.05
    assert a["best_day"] == {"date": "2026-03-03", "return_pct": 0.02}
    assert a["worst_day"] == {"date": "2026-03-04", "return_pct": -0.05}
    assert a["day_pl_pct"] == round(98000 / 96900 - 1, 6)      # 03-05 against 03-04
    assert "intraday" not in a


def test_the_current_equity_is_never_published():
    """Neither the broker's current equity nor its day P&L: both are live
    marks. Nothing is dated with them and the drawdown does not count them."""
    h = hist([("2026-03-02", 100000.0), ("2026-03-03", 110000.0)])
    a = pe.account_section({"equity": 88000.0, "day_pl_pct": -0.2}, h, et_epoch(date(2026, 3, 4), 11))
    assert a["equity"] == 110000.0 and a["max_drawdown_pct"] == 0.0
    assert a["daily"][-1]["date"] == "2026-03-03"
    assert a["day_pl_pct"] == 0.1                               # 03-03 against 03-02
    assert not {88000.0, -0.2} & set(_numbers(a))


def test_a_point_for_the_day_in_progress_is_never_published():
    """If the broker serves the day that is still going, that point is the
    live equity: it waits until the day is over in New York."""
    h = hist([("2026-03-02", 100000.0), ("2026-03-03", 100800.0), ("2026-03-04", 100480.4)],
             sessions=weekdays("2026-03-02", "2026-03-04"))
    for now in (et_epoch(date(2026, 3, 4), 9, 31), et_epoch(date(2026, 3, 4), 20),
                et_epoch(date(2026, 3, 4), 23, 59)):
        a = pe.account_section({}, h, now)
        assert a["daily"][-1]["date"] == "2026-03-03" and a["equity"] == 100800.0
        assert 100480.4 not in set(_numbers(a))
    a = pe.account_section({}, h, et_epoch(date(2026, 3, 5), 0, 1))
    assert a["daily"][-1] == {"date": "2026-03-04", "equity": 100480.4} == {
        "date": "2026-03-04", "equity": a["equity"]}
    assert a["day_pl_pct"] == round(100480.4 / 100800 - 1, 6)


def test_the_published_figures_give_back_no_live_price():
    """The review's reconstruction: one share position, no cash flows.
    price = avg_entry_price + (equity - start_equity - realized_pnl) / qty
    returned the live mark to the cent while the current equity was
    published. Now it returns the last completed session's mark."""
    start, entry, qty, live_mark, close_mark = 100000.0, 101.25, 120, 103.17, 102.0
    realized = 250.0                                            # NOVA, 06-10
    h = hist([("2026-06-08", start), ("2026-06-09", start), ("2026-06-10", start + realized),
              ("2026-06-11", start + realized + qty * (close_mark - entry))],
             start="2026-06-08", sessions=weekdays("2026-06-08", "2026-06-12"))
    live_equity = round(start + realized + qty * (live_mark - entry), 2)
    portfolio = {"equity": live_equity, "day_pl_pct": live_equity / h["daily"][-1][1] - 1,
                 "positions": [{"symbol": "ACME", "side": "long", "qty": qty,
                                "avg_entry_price": entry}]}
    acct = pe.account_section(portfolio, h, et_epoch(date(2026, 6, 12), 14))
    pos = pe.positions_section(portfolio)[0]
    trades = pe.trades_section({"rows": [
        {"symbol": "NOVA", "side": "buy", "qty": 10, "price": 50.0, "ts": et_epoch(date(2026, 6, 10), 10)},
        {"symbol": "NOVA", "side": "sell", "qty": 10, "price": 75.0, "ts": et_epoch(date(2026, 6, 10), 11)},
    ]})
    assert trades["stats"]["realized_pnl"] == realized
    price = pos["avg_entry_price"] + (
        acct["equity"] - acct["start_equity"] - trades["stats"]["realized_pnl"]) / pos["qty"]
    assert price == pytest.approx(close_mark)                   # an end-of-day mark
    assert abs(price - live_mark) > 1.0
    # and the day P&L is 06-11's, not the live move since the last close
    assert acct["day_pl_pct"] == round(h["daily"][-1][1] / h["daily"][-2][1] - 1, 6)
    assert live_equity not in set(_numbers(acct))


def test_an_account_with_no_completed_session_is_unavailable():
    with pytest.raises(pe.SourceError):
        pe.account_section({"equity": 100000.0}, hist([]), et_epoch(date(2026, 3, 2), 12))
    # its first day, even if the broker already serves that day
    with pytest.raises(pe.SourceError):
        pe.account_section({"equity": 100000.0}, hist([("2026-03-02", 100000.0)]),
                           et_epoch(date(2026, 3, 2), 21))


def test_a_session_listed_without_equity_is_skipped_not_merged():
    """The broker stamps 08-31 with no equity: 08-28 -> 09-01 is two
    sessions' move and must not be filed as 09-01's change."""
    h = hist([("2026-08-27", 100000.0), ("2026-08-28", 101000.0), ("2026-08-31", 0.0),
              ("2026-09-01", 99000.0), ("2026-09-02", 99495.0)], start="2026-08-27")
    for sessions_known in (None, weekdays("2026-08-27", "2026-09-02")):
        h["sessions"] = sessions_known
        a = pe.account_section({}, h, et_epoch(date(2026, 9, 3), 10))
        assert [d["date"] for d in a["daily"]] == ["2026-08-27", "2026-08-28", "2026-09-01",
                                                   "2026-09-02"]
        assert a["best_day"] == {"date": "2026-08-28", "return_pct": 0.01}
        # not 09-01 at -1.98%: that was 08-28 to 09-01, two sessions
        assert a["worst_day"] == {"date": "2026-09-02", "return_pct": 0.005}
        assert a["max_drawdown_pct"] == round(99000 / 101000 - 1, 6)   # the curve still counts
        assert a["day_pl_pct"] == 0.005


def test_a_session_missing_from_the_history_is_found_on_the_calendar():
    """No stamp at all for 08-31: only the exchange calendar knows it was a
    session, and 09-01's -1.98% is really two sessions."""
    h = hist([("2026-08-27", 100000.0), ("2026-08-28", 101000.0), ("2026-09-01", 99000.0),
              ("2026-09-02", 98010.0)], start="2026-08-27",
             sessions=weekdays("2026-08-27", "2026-09-02"))
    a = pe.account_section({}, h, et_epoch(date(2026, 9, 3), 10))
    assert a["best_day"] == {"date": "2026-08-28", "return_pct": 0.01}
    assert a["worst_day"] == {"date": "2026-09-02", "return_pct": -0.01}
    # A holiday is not a hole: Labor Day 2026-09-07 is not on the calendar.
    cal = [d for d in weekdays("2026-09-03", "2026-09-09") if d != "2026-09-07"]
    h = hist([("2026-09-03", 100000.0), ("2026-09-04", 100500.0), ("2026-09-08", 98490.0),
              ("2026-09-09", 98490.0)], start="2026-09-03", sessions=cal)
    a = pe.account_section({}, h, et_epoch(date(2026, 9, 10), 10))
    assert a["worst_day"] == {"date": "2026-09-08", "return_pct": -0.02}


def test_the_session_in_progress_never_reaches_the_file(world):
    """The account's current equity is never published, nor dated as the
    latest session: equity stops at the broker's last point for a day that
    is over, served or not for the day in progress."""
    live = float(world.broker.account.equity)
    closed = world.broker.daily[-1]                             # 06-11, 20:00 ET
    for now in (et_epoch(SESSION, 9, 0), et_epoch(SESSION, 12), NOW):
        p = pe.build_payload(now)
        acct = p["account"]
        assert acct["daily"][-1] == {"date": "2026-06-11", "equity": closed[1]}
        assert acct["equity"] == closed[1]
        assert live not in set(_numbers(p))
    # a broker that serves the day in progress changes nothing
    world.broker.daily.append((et_epoch(SESSION, 20), live))
    p = pe.build_payload(NOW)
    assert p["account"]["daily"][-1]["date"] == "2026-06-11"
    assert live not in set(_numbers(p))
    # once 06-12 is over in New York, its rolled point is published
    after = et_epoch(SESSION + timedelta(days=1), 1)
    acct = pe.build_payload(after)["account"]
    assert acct["daily"][-1] == {"date": "2026-06-12", "equity": live}
    assert acct["day_pl_pct"] == round(live / closed[1] - 1, 6)


def test_opened_sessions_reads_alpacas_calendar_rows():
    rows = [SimpleNamespace(date=date(2026, 6, 11), open=datetime(2026, 6, 11, 9, 30)),
            SimpleNamespace(date=date(2026, 6, 12), open=datetime(2026, 6, 12, 9, 30)),
            SimpleNamespace(date="2026-06-15", open=None), "junk"]
    assert pe._opened_sessions(rows, NOW) == ["2026-06-11", "2026-06-12"]
    assert pe._opened_sessions(rows, et_epoch(SESSION, 9, 29)) == ["2026-06-11"]
    assert pe._opened_sessions(None, NOW) == []


# --------------------------------------------------------------------------
# stops: only where the engine would act
# --------------------------------------------------------------------------
ENGINE = {"symbols": {
    "ACME": {"enabled": True, "stop": 0.02},
    "PAUSED": {"enabled": False, "stop": 0.03},
    "BTC/USD": {"enabled": True, "stop": 0.04},
    "VOLT": {"enabled": True, "stop": 0.06},
    "NOSTOP": {"enabled": True},
}}


def held(symbol, side="long"):
    return {"positions": [{"symbol": symbol, "side": side, "qty": 100, "avg_entry_price": 10.0}]}


@pytest.mark.parametrize("symbol, side, opened, adopted, stop", [
    ("ACME", "long", ["ACME"], [], 0.02),            # the engine's own position
    ("ACME", "short", ["ACME"], [], None),           # the book sets no stop on a short
    ("PAUSED", "long", ["PAUSED"], [], None),        # not polled, so nothing checks it
    ("BTCUSD", "long", ["BTC/USD"], [], 0.04),       # Alpaca's name, the config's key
    ("VOLT", "long", [], ["VOLT"], None),            # assigned by the put sleeve: no stop
    ("VOLT", "long", ["VOLT"], ["VOLT"], None),
    ("ACME", "long", [], [], None),                  # the engine did not open it
    ("NOSTOP", "long", ["NOSTOP"], [], None),        # no stop configured
    ("UNLISTED", "long", ["UNLISTED"], [], None),    # not in the engine config at all
])
def test_a_stop_is_published_only_where_the_site_can_vouch_for_it(symbol, side, opened, adopted,
                                                                  stop, tmp_path):
    aux = tmp_path / "entries.json"
    write_entries(aux, opened=opened, adopted=adopted)
    (row,) = pe.positions_section(held(symbol, side), ENGINE, json.loads(aux.read_text()))
    assert row["stop_pct"] == stop
    assert row["symbol"] == ("BTC/USD" if symbol == "BTCUSD" else symbol)


@pytest.mark.parametrize("engine, entries", [(None, {"opened": {"ACME": "x"}}), (ENGINE, None),
                                             (ENGINE, {}), (ENGINE, {"opened": ["ACME"]})])
def test_no_stop_is_claimed_when_the_engine_state_cannot_be_read(engine, entries):
    (row,) = pe.positions_section(held("ACME"), engine, entries)
    assert row["stop_pct"] is None


def test_the_export_reads_the_engines_entry_sidecar(world):
    def stops():
        return {r["symbol"]: r["stop_pct"] for r in pe.build_payload(NOW)["positions"]["equities"]}
    assert stops() == {"QBIT/USD": None, "ACME": 0.02, "NOVA": 0.02}
    write_entries(world.aux, opened=["NOVA"])
    assert stops() == {"QBIT/USD": None, "ACME": None, "NOVA": 0.02}
    world.aux.write_text("{ half written")
    assert stops() == {"QBIT/USD": None, "ACME": None, "NOVA": None}
    world.aux.unlink()
    p = pe.build_payload(NOW)
    assert_valid(p)
    assert p["meta"]["unavailable"] == []                     # the positions still publish


def row(sym, side, qty, price, ts, held, pnl=None):
    return {"ts": ts, "symbol": sym, "side": side, "qty": qty, "price": price,
            "pnl": pnl, "held_after": held, "order_id": CANARY_ORDER}


def test_round_trips_pair_fills_flat_to_flat():
    rows = [
        row("ACME", "buy", 10, 100.0, 1000, 10),
        row("ACME", "buy", 10, 110.0, 2000, 20),
        row("ACME", "sell", 5, 120.0, 3000, 15, pnl=75.0),
        row("ACME", "sell", 15, 130.0, 4000, 0, pnl=375.0),
        row("NOVA", "sell", 100, 30.0, 4500, 0),                # assigned shares: no buy
        row("VOLT260605P00030000", "sell", 1, 0.55, 4600, 0),   # option fill
        row("ZEPH", "buy", 4, 50.0, 5000, 4),
        row("ZEPH", "sell", 4, 45.0, 6000, 0, pnl=-20.0),
        row("ACME", "buy", 3, 140.0, 7000, 3),                  # still open
    ]
    trips = pe.round_trips(list(reversed(rows)))                # builder order: newest first
    assert trips == [
        {"symbol": "ZEPH", "side": "long", "qty": 4.0, "entry_price": 50.0, "exit_price": 45.0,
         "entry_at": "1970-01-01T01:23:20Z", "exit_at": "1970-01-01T01:40:00Z",
         "pnl": -20.0, "pnl_pct": -0.1, "strategy": None},
        {"symbol": "ACME", "side": "long", "qty": 20.0, "entry_price": 105.0, "exit_price": 127.5,
         "entry_at": "1970-01-01T00:16:40Z", "exit_at": "1970-01-01T01:06:40Z",
         "pnl": 450.0, "pnl_pct": round(450 / 2100, 6), "strategy": None},
    ]
    stats = pe.trade_stats(trips)
    assert stats == {"round_trips": 2, "wins": 1, "losses": 1, "win_rate": 0.5,
                     "avg_win": 450.0, "avg_loss": -20.0, "avg_win_pct": round(450 / 2100, 6),
                     "avg_loss_pct": -0.1, "profit_factor": 22.5, "realized_pnl": 430.0}


def test_a_crypto_trip_closes_despite_the_fee_dust():
    rows = [
        row("QBIT/USD", "buy", 0.4, 60000.0, 1000, 0.4),
        row("QBIT/USD", "sell", 0.399, 61000.0, 2000, 0.001, pnl=399.0),     # 0.25% fee dust
        row("ACME", "buy", 100, 10.0, 3000, 100),
        row("ACME", "sell", 99.75, 11.0, 4000, 0.25, pnl=99.75),            # shares: not closed
    ]
    trips = pe.round_trips(rows)
    # cash basis: 0.399 x 61000 in, 0.4 x 60000 out (the builder said +399)
    assert [(t["symbol"], t["qty"], t["pnl"]) for t in trips] == [("QBIT/USD", 0.4, 339.0)]


def test_a_crypto_trip_counts_the_coins_kept_as_its_fee():
    """Alpaca keeps a crypto buy's fee in coins, so the sell is for less than
    was bought. A losing trip must not look smaller than it was."""
    rows = [row("QBIT/USD", "buy", 0.8, 50000.0, 1000, 0.8),
            row("QBIT/USD", "sell", 0.798, 49500.0, 2000, 0.002, pnl=-399.0)]
    (trip,) = pe.round_trips(rows)
    assert trip["pnl"] == round(0.798 * 49500 - 0.8 * 50000, 2) == -499.0
    assert trip["pnl_pct"] == round(-499.0 / 40000.0, 6)
    assert (trip["entry_price"], trip["exit_price"]) == (50000.0, 49500.0)


def builder_rows(monkeypatch, fills) -> list[dict]:
    """web.server.build_executions' own rows for `fills` [(symbol, side, qty,
    price)], one hour apart."""
    t0 = datetime(2026, 3, 2, 15, tzinfo=UTC).timestamp()
    orders = [FakeBroker._order(sym, side, qty, px, t0 + 3600 * i)
              for i, (sym, side, qty, px) in enumerate(fills)]
    trading = SimpleNamespace(get_orders=lambda req: orders)
    monkeypatch.setattr(web_server, "_client", lambda: SimpleNamespace(client=trading))
    return web_server.build_executions(limit=1_000_000)["rows"]


def test_equal_crypto_trips_never_merge_as_the_fee_builds_up(monkeypatch):
    """The builder's running position keeps every trip's fee (0.25% of 0.94
    each), past the dust allowance by the fifth trip. Each trip counts its
    own coins, so six trips stay six, each at its own cash P&L."""
    fills = []
    for _ in range(6):
        fills += [("QBIT/USD", "buy", 0.94, 80000.0),
                  ("QBIT/USD", "sell", round(0.94 * 0.9975, 9), 81000.0)]
    rows = builder_rows(monkeypatch, fills)
    held = [r["held_after"] for r in sorted(rows, key=lambda r: r["ts"]) if r["side"] == "sell"]
    assert held[4] > pe.CRYPTO_DUST_FRAC * 0.94               # the builder's count has drifted
    trips = pe.round_trips(rows)
    cash = round(0.94 * 0.9975 * 81000 - 0.94 * 80000, 2)
    assert [(t["qty"], t["pnl"]) for t in trips] == [(0.94, cash)] * 6
    assert pe.trade_stats(trips)["realized_pnl"] == round(6 * cash, 2)


def test_share_trips_match_the_builders_average_cost(monkeypatch):
    fills = [("ACME", "buy", 10, 100.0), ("ACME", "buy", 30, 104.0),
             ("ACME", "sell", 15, 110.0), ("ACME", "sell", 25, 98.0),
             ("ACME", "buy", 5, 90.0), ("ACME", "sell", 5, 93.5)]
    rows = builder_rows(monkeypatch, fills)
    trips = pe.round_trips(rows)
    builder = [r["pnl"] for r in sorted(rows, key=lambda r: r["ts"]) if r["side"] == "sell"]
    assert [t["pnl"] for t in reversed(trips)] == [round(builder[0] + builder[1], 2),
                                                   round(builder[2], 2)]


def test_a_sell_larger_than_the_trip_counts_only_what_the_trip_holds():
    """Shares that arrived without a buy fill (an assignment) and went out in
    the same sale are not part of the trip."""
    rows = [row("ACME", "buy", 100, 20.0, 1000, 100),
            row("ACME", "sell", 200, 21.0, 2000, 0, pnl=100.0)]
    (trip,) = pe.round_trips(rows)
    assert (trip["qty"], trip["exit_price"], trip["pnl"]) == (100.0, 21.0, 100.0)


def test_round_trips_match_the_builder_on_the_fake_book(world):
    trips = pe.build_payload(NOW)["trades"]["round_trips"]
    assert [t["exit_at"] for t in trips] == sorted((t["exit_at"] for t in trips), reverse=True)
    assert not {t["symbol"] for t in trips} & {"VOLT", "VOLT260605P00030000"}
    assert all(t["entry_at"] <= t["exit_at"] for t in trips)
    assert {t["symbol"] for t in trips if t["symbol"] == "QBIT/USD"} == {"QBIT/USD"}


def test_trades_count_every_closed_order_past_the_first_500(world, monkeypatch):
    """The exporter reads the fills through web.server.build_executions, which
    pages through closed orders newest first. An account whose history holds
    more than 500 closed orders (rejected retries count) still publishes every
    trip, the newest included, and the trades match a one-request read of the
    same book."""
    few = pe.build_payload(NOW)["trades"]
    assert len(world.broker.order_requests) == 1
    # 1,000 rejected retries OLDER than every fill. The old single request
    # (limit 500, oldest first) saw only these and published no trip at all.
    first = min(o.submitted_at for o in world.broker.orders).timestamp()
    world.broker.orders += [FakeBroker._order("NOVA", "sell", 5, 0, first - 2000 + k, filled=False)
                            for k in range(1000)]
    world.broker.order_requests.clear()
    # A full walk, as a fresh exporter process makes: within one process
    # build_executions keeps the orders it has read and asks only for newer
    # ones (tests/test_executions_cache.py), which these older ones are not.
    monkeypatch.setattr(web_server, "_ORDERS", web_server._ClosedOrders())

    many = pe.build_payload(NOW)["trades"]

    assert many["stats"]["round_trips"] == 20
    assert many == few
    assert len(world.broker.order_requests) == 3          # 500 + 500 + the rest
    assert world.broker.order_requests[0].until is None


def test_values_are_finite_rounded_numbers():
    assert pe._num(float("nan"), 2) is None and pe._num(float("inf"), 2) is None
    assert pe._num(True, 2) is None and pe._num("x", 2) is None and pe._num(None, 2) is None
    assert pe._num("1.23456", 2) == 1.23 and pe._num(-0.0001, 2) == 0.0
    assert pe._price(0) is None and pe._qty(-3) is None
    assert pe._utc_ts("2026-06-16T00:00:00-04:00") == "2026-06-16T04:00:00Z"
    assert pe._utc_ts("2026-06-16T00:00:00") is None           # no offset: not trusted


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------
class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.puts: list[str] = []

    def put_object(self, *, Bucket, Key, Body, ContentType=None, CacheControl=None):
        self.puts.append(Key)
        self.objects[Key] = {"bucket": Bucket, "body": bytes(Body), "type": ContentType,
                             "cache": CacheControl}
        return {}


def r2_exporter(world, s3, clock, **kw):
    dest = R2Destination(s3, "alpaca-public", account_id="0" * 32)
    return pe.Exporter(dest, world.root / "state", SCHEMA, clock=clock, **kw)


def test_out_dir_run_writes_a_valid_portfolio_json(world, tmp_path):
    out = tmp_path / "bucket"
    assert pe.main(["--out-dir", str(out)], clock=world.clock) == 0
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) \
        == ["portfolio.json"]
    assert_valid(json.loads((out / "portfolio.json").read_text()))


def test_a_full_run_writes_only_the_object_and_its_own_state(world, tmp_path):
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    out = tmp_path / "bucket"
    assert pe.main(["--out-dir", str(out)], clock=world.clock) == 0
    after = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    changed = {p for p in after if before.get(p) != after[p]}
    assert all(p.is_relative_to(out) or p.is_relative_to(tmp_path / "state") for p in changed)
    assert not [p for p in before if p not in after]


def test_unchanged_content_is_not_resent_until_the_heartbeat(world):
    s3, now = FakeS3(), [NOW]
    exp = r2_exporter(world, s3, lambda: now[0])
    assert exp.run() == 0 and s3.puts == ["portfolio.json"]
    obj = s3.objects["portfolio.json"]
    assert obj["bucket"] == "alpaca-public" and obj["type"] == "application/json"
    assert obj["cache"] == pe.CACHE_CONTROL
    now[0] += 60                                                # same content, new as_of
    assert exp.run() == 0 and s3.puts == ["portfolio.json"]
    now[0] += pe.HEARTBEAT_SEC                                  # heartbeat due
    assert exp.run() == 0 and len(s3.puts) == 2
    world.broker.positions = world.broker.positions[1:]         # content changed
    now[0] += 60
    assert exp.run() == 0 and len(s3.puts) == 3
    assert json.loads(s3.objects["portfolio.json"]["body"])["meta"]["as_of"] == pe._ts(now[0])


def test_the_uploaded_bytes_are_the_validated_object(world):
    s3 = FakeS3()
    assert r2_exporter(world, s3, world.clock).run() == 0
    body = s3.objects["portfolio.json"]["body"]
    assert schema_gate.validate_text(body, SCHEMA) == []
    assert schema_gate.loads(body) == pe.build_payload(NOW)


def test_bytes_that_would_read_two_ways_are_never_uploaded(world, monkeypatch, caplog):
    """A duplicate key: json.loads keeps the last copy, so the dict validates
    while the bytes still carry the first."""
    def smuggle(payload):
        text = pe.json.dumps(payload, separators=(",", ":"))
        return text.replace('"meta":{', '"meta":{"data_delay":"' + CANARY_PATH + '",', 1).encode()
    monkeypatch.setattr(pe, "encode", smuggle)
    s3 = FakeS3()
    with caplog.at_level(logging.INFO, logger="public_export"):
        assert r2_exporter(world, s3, world.clock).run() == 1
    assert s3.puts == []
    assert CANARY_PATH not in caplog.text


def test_force_resends_unchanged_content(world):
    s3 = FakeS3()
    assert r2_exporter(world, s3, world.clock).run() == 0
    assert r2_exporter(world, s3, world.clock, force=True).run() == 0
    assert len(s3.puts) == 2


def test_a_payload_that_fails_the_gate_is_never_uploaded(world, monkeypatch, caplog):
    real = pe.build_payload

    def leaky(now):
        p = real(now)
        p["positions"]["equities"][0]["current_price"] = QUOTE_ACME
        return p
    monkeypatch.setattr(pe, "build_payload", leaky)
    s3 = FakeS3()
    with caplog.at_level(logging.INFO, logger="public_export"):
        assert r2_exporter(world, s3, world.clock).run() == 1
    assert s3.puts == []
    assert "unexpected property 'current_price'" in caplog.text
    assert "104.3719" not in caplog.text                       # the value is never logged
    assert not (world.root / "state" / "state.json").exists()


def test_a_missing_or_loose_schema_fails_closed(world, tmp_path, monkeypatch):
    out = tmp_path / "bucket"
    monkeypatch.setattr(pe, "SCHEMA_FILE", tmp_path / "nope.json")
    assert pe.main(["--out-dir", str(out)], clock=world.clock) == 2
    loose = tmp_path / "loose.json"
    loose.write_text(json.dumps({"type": "object", "properties": {"a": {"type": "string"}}}))
    monkeypatch.setattr(pe, "SCHEMA_FILE", loose)
    assert pe.main(["--out-dir", str(out)], clock=world.clock) == 2
    assert not out.exists()


def test_dry_run_writes_nothing(world, tmp_path, capsys):
    out = tmp_path / "bucket"
    assert pe.main(["--dry-run", "--out-dir", str(out)], clock=world.clock) == 0
    assert "[dry-run] PUT portfolio.json" in capsys.readouterr().out
    assert not out.exists()
    assert not (world.root / "state").exists()


def test_dry_run_without_r2_settings_still_builds(world, monkeypatch, capsys):
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert pe.main(["--dry-run"], clock=world.clock) == 0
    assert "[dry-run] PUT portfolio.json" in capsys.readouterr().out


def test_an_overlapping_run_exits_without_uploading(world, tmp_path):
    out = tmp_path / "bucket"
    state = world.root / "state"
    state.mkdir()
    dest = pe.make_destination(str(out), False)
    with pe._pub.try_lock(pe.lock_path(state, dest)) as got:
        assert got
        assert pe.main(["--out-dir", str(out)], clock=world.clock) == 0
    assert not (out / "portfolio.json").exists()


R2_ENV = {"R2_ACCOUNT_ID": "a" * 32, "R2_ACCESS_KEY_ID": "AKIDEXAMPLE",
          "R2_SECRET_ACCESS_KEY": "not-a-real-secret"}


def test_the_public_bucket_is_its_own(monkeypatch):
    seen = []
    monkeypatch.setattr(R2Destination, "from_config",
                        classmethod(lambda cls, cfg: seen.append(cfg) or "dest"))
    assert pe.make_destination(None, False, env=dict(R2_ENV)) == "dest"
    assert seen[-1].bucket == "alpaca-public"
    pe.make_destination(None, False, env={**R2_ENV, "R2_PUBLIC_BUCKET": "my-public-site"})
    assert seen[-1].bucket == "my-public-site"
    # the private dashboard's bucket (named or default) is refused
    for env in ({**R2_ENV, "R2_PUBLIC_BUCKET": "alpaca-dashboard"},
                {**R2_ENV, "R2_BUCKET": "shared", "R2_PUBLIC_BUCKET": "shared"},
                {**R2_ENV, "R2_PUBLIC_BUCKET": "Bad_Name"}):
        with pytest.raises(ConfigError):
            pe.make_destination(None, False, env=env)
    assert isinstance(R2Config.from_env({**R2_ENV, "R2_BUCKET": "alpaca-public"}), R2Config)


def test_logs_never_carry_values(canary_world, tmp_path, caplog):
    canary_world.broker.calendar_error = True
    with caplog.at_level(logging.DEBUG):
        assert pe.main(["--out-dir", str(tmp_path / "b"), "-v"], clock=canary_world.clock) == 0
        assert pe.main(["--out-dir", str(tmp_path / "b")], clock=canary_world.clock) == 0
    text = caplog.text
    assert "canary" not in text.lower()
    for n in CANARY_NUMBERS + (str(canary_world.broker.account.equity),):
        assert n not in text


# --------------------------------------------------------------------------
# safety rails
# --------------------------------------------------------------------------
def test_importing_the_exporter_loads_no_order_path_and_no_boto3(tmp_path):
    code = (
        "import json, sys\n"
        "import web.public_export\n"
        "bad = sorted(m for m in sys.modules if m in ('relay.order_manager', 'relay.csp_manager',"
        " 'relay.options_client', 'relay.logger') or 'order_manager' in m"
        " or m.split('.')[0] in ('boto3', 'botocore', 's3transfer'))\n"
        "print(json.dumps(bad))\n"
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "ALPACA_DATA_DIR": str(tmp_path),
           "PYTHONPATH": str(ROOT)}
    res = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout.strip().splitlines()[-1]) == []


@pytest.mark.parametrize("module", ["web/public_export.py", "web/schema_gate.py"])
def test_exporter_source_never_imports_the_order_path(module):
    tree = ast.parse((ROOT / module).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{a.name}" for a in node.names)
    assert not any(bad in name for name in imported
                   for bad in ("order_manager", "csp_manager", "options_client", "relay.models",
                               "relay.logger"))
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not ({"submit_order", "market_order", "limit_order", "close_position",
                 "close_all_positions", "cancel_orders", "cancel_order"} & calls)


def test_the_whitelist_in_the_docstring_matches_the_schema():
    """The module docstring lists every published field; keep it honest."""
    doc = pe.__doc__.split("WHAT GOES OUT IS A WHITELIST")[1].split("NOT IN THIS FILE")[0]
    listed = set(re.findall(r"[a-z][a-z0-9_]*", doc.split("or NOTHING is uploaded")[1]))
    assert schema_keys() <= listed | {"schema"}

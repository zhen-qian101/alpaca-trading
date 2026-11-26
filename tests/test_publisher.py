"""Tests for web/publisher.py and web/r2.py: the home box's push to R2.

The cloud dashboard only ever sees what this publisher uploads, and the one
thing the cloud can send back (a look approval) is applied here. So the
properties locked in below are:

  * the bucket layout is exactly the contract (apps/dashboard/src/contract.ts);
  * one failing data source never blocks the others or the push;
  * unchanged objects are not re-sent, except the snapshot, which is the
    heartbeat and goes up every run, after the images it points at;
  * an approval is applied once, a malformed one never, and none once its
    date's 20:00 ET render has started;
  * only final recap videos, covers and meta.json leave the box, within the
    retention window, and history.json never points at a missing object;
  * overlapping cron runs exit at once;
  * the publisher cannot reach the order path, and needs no boto3 for --out-dir.

No network and no real data: the builders are replaced with synthetic returns
(invented symbols ACME / NOVA / QBIT/USD) and R2 with an in-memory fake.

    .venv/bin/python -m pytest -q tests/test_publisher.py
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import logging
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from web import options_view
from web import publisher as pubmod
from web import server as web_server
from web.r2 import (ConfigError, DestinationError, DirDestination, R2Config, R2Destination,
                    check_key)

ROOT = Path(__file__).resolve().parent.parent
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(32))
JPEG = b"\xff\xd8\xff\xe0" + bytes(32)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """The slice of the boto3 S3 client the publisher uses, in memory."""

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.page_size = page_size
        self.fail_keys: set[str] = set()

    def _store(self, key, body, extra):
        if key in self.fail_keys:
            raise FakeClientError("InternalError")
        self.objects[key] = {"body": body, **(extra or {})}

    def put_object(self, *, Bucket, Key, Body, ContentType=None, CacheControl=None):
        self.calls.append(("put_object", Key))
        self._store(Key, bytes(Body), {"ContentType": ContentType, "CacheControl": CacheControl})
        return {}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None, Config=None):
        self.calls.append(("upload_file", Key, dict(ExtraArgs or {}), Config))
        self._store(Key, Path(Filename).read_bytes(), ExtraArgs)

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get_object", Key))
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key]["body"])}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        self.calls.append(("list_objects_v2", Prefix))
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + self.page_size]
        resp = {"Contents": [{"Key": k, "Size": len(self.objects[k]["body"]),
                              "ETag": '"%s"' % hashlib.md5(self.objects[k]["body"]).hexdigest()}
                             for k in page],
                "IsTruncated": start + self.page_size < len(keys)}
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = str(start + self.page_size)
        return resp

    def delete_objects(self, *, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        self.calls.append(("delete_objects", tuple(keys)))
        for k in keys:
            self.objects.pop(k, None)
        return {}

    # -- test helpers --
    def json(self, key):
        return json.loads(self.objects[key]["body"])

    def written(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] in ("put_object", "upload_file")]

    def reset_calls(self):
        self.calls.clear()


class Clock:
    """Starts at NOON US/Eastern on today's ET date, whatever the wall clock
    says: approvals are refused once the session's 20:00 ET render has
    started, so a clock that followed the real time would make every
    approval test depend on the hour the suite runs at. Everything the tests
    date (file mtimes, request times) is stamped from this clock too."""

    def __init__(self) -> None:
        today = datetime.now(pubmod.EASTERN).date()
        self.t = datetime(today.year, today.month, today.day, 12,
                          tzinfo=pubmod.EASTERN).timestamp()

    def __call__(self) -> float:
        return self.t


class World:
    """Synthetic builders, config, look and archive, all under tmp_path."""

    def __init__(self, tmp: Path, clock: Clock) -> None:
        self.tmp = tmp
        self.clock = clock
        self.today = pubmod._et_today(clock.t)
        self.session = self.today.isoformat()
        self.portfolio = {
            "equity": 25000.0, "last_equity": 24800.0, "cash": 1000.0, "buying_power": 2000.0,
            "day_pl": 200.0, "day_pl_pct": 0.0080645, "paper": True, "status": "ACTIVE",
            "positions": [{"symbol": "ACME", "side": "long", "qty": 12.0, "avg_entry_price": 101.5,
                           "current_price": 103.0, "market_value": 1236.0, "cost_basis": 1218.0,
                           "unrealized_pl": 18.0, "unrealized_plpc": 0.0148, "stop_pct": 0.02,
                           "stop_price": 99.47, "stop_distance_pct": 0.0355, "priority": 3,
                           "bias": "long"}],
            "halt": {"limit": 0.05, "hard_cap": 0.05, "tripped": False, "used_frac": 0.0,
                     "applies_to": "buys only — exits always allowed"},
        }
        self.board = {"date": self.session, "rows": [{"symbol": "ACME", "bias": "long", "priority": 3,
                                                      "suggested_size": 0.97, "max_position": 50,
                                                      "contingencies": []}],
                      "max_open_positions": 1, "daily_loss_limit": 0.05,
                      "allow_extended_hours": True, "notes": {"_": "Synthetic overview."},
                      "mtime": clock.t - 600}
        self.journal = {"rows": [{"event": "order_ok", "ts": clock.t - 900, "_ts": clock.t - 900,
                                  "ticker": "ACME", "action": "buy", "qty": 12}],
                        "engine": {"symbols": [], "newest_bar": None, "last_poll": None,
                                   "running": True, "polling": True, "stale_after_sec": 900.0,
                                   "log_mtime": None, "log_tail": ["poll ok"],
                                   "yfinance_fallback": []}}
        self.executions = {"rows": [{"ts": clock.t - 900, "symbol": "ACME", "side": "buy",
                                     "qty": 12.0, "price": 101.5, "entry_price": 101.5,
                                     "exit_price": None, "pnl": None, "pnl_pct": None,
                                     "order_type": "market", "held_after": 12.0}],
                           "realized_pnl": 0.0, "round_trips": 0, "win_rate": None}
        self.look_approved: bool | None = None
        self.look_exists = True
        self.set_calls: list[tuple] = []
        self.set_result: dict | None = None
        self.chart_calls: list[str] = []
        self.chart_close = 103.0
        self.chart_error: dict | None = None
        self.config = tmp / "config.json"
        self.config.write_text(json.dumps({"symbols": {
            "ACME": {"enabled": True}, "NOVA": {"enabled": True},
            "OFFL": {"enabled": False}, "QBIT/USD": {"enabled": True}}}))
        self.assets = tmp / "assets"
        self.assets.mkdir()
        (self.assets / "plate.png").write_bytes(PNG)
        (self.assets / "keyframe.png").write_bytes(PNG + b"k")
        self.archive = tmp / "archive"
        self.archive.mkdir()
        self.state_dir = tmp / "state"
        # The look RECORD on disk: only its mtime matters (it tells a change
        # made at home from one this publisher applied). Absent until the fake
        # set_look_approval writes it, like a record nobody has touched.
        self.look_dir = tmp / "looks"
        self.look_dir.mkdir()
        self.options: dict | None = {"enabled": True, "shadow": False, "underlying": "ORBT",
                                     "short_put": None, "assigned": None}

    # builders -------------------------------------------------------------
    def build_look(self, date=None):
        date = date or self.session
        base = {"date": date, "session_date": self.session, "host": "Demo Host",
                "host_json": "demo-host.json", "stylist_enabled": True,
                "locked_keyframe": "briefing/assets/demo/keyframe.png", "dates": [self.session]}
        if date != self.session or not self.look_exists:
            return {**base, "exists": False, "renders_with": "locked"}
        return {**base, "exists": True, "weather": None, "style": {"slug": "navy", "outfit": "navy",
                "rationale": "synthetic"}, "plate_identity": 0.9, "keyframe_identity": 0.9,
                "approved": self.look_approved, "approved_at": None,
                "renders_with": "look" if self.look_approved else "locked",
                "plate_url": f"/api/look/asset?date={date}&kind=plate",
                "keyframe_url": f"/api/look/asset?date={date}&kind=keyframe"}

    def set_look_approval(self, date, approved):
        self.set_calls.append((date, approved))
        if self.set_result is not None:
            return self.set_result
        self.look_approved = True if approved else None
        self.touch_look(date)
        return self.build_look(date)

    def look_path(self, date):
        return self.look_dir / f"{date}.json"

    def touch_look(self, date, at: float | None = None):
        """Rewrite the look record, as set_look_approval or :8787 would, at
        `at` (default: the test clock's now)."""
        p = self.look_path(date)
        p.write_text(json.dumps({"approved": self.look_approved}))
        at = self.clock.t if at is None else at
        os.utime(p, (at, at))

    def look_asset(self, date, kind):
        if date != self.session or not self.look_exists:
            return None
        return self.assets / f"{kind}.png"

    def build_chart(self, symbol, bars):
        self.chart_calls.append(symbol)
        if self.chart_error is not None:
            return self.chart_error
        t0 = 1772658000  # an invented hour, true UTC
        return {"symbols": {symbol: {
            "interval": "1h",
            "candles": [{"time": t0 + 3600 * i, "open": 100.0, "high": 104.0, "low": 99.0,
                         "close": self.chart_close} for i in range(3)],
            "markers": [], "bbUpper": [], "bbBasis": [], "bbLower": [], "rsi": [],
            "macdHist": [], "bbLabel": "", "macdLabel": ""}}}

    # archive --------------------------------------------------------------
    def day(self, offset: int) -> str:
        return (self.today + timedelta(days=offset)).isoformat()

    def edition(self, date: str, slot: str = "video", *, zh=True, covers=True, legacy=False,
                meta_extra: dict | None = None, skip_files: tuple = (), age: float = 3600.0):
        d = self.archive / date / slot
        d.mkdir(parents=True)
        label = "midday-briefing" if slot.startswith("slideshow") else "post-market-recap"
        stem = f"{label}_{date}"
        meta = {"date": date, "generated_at": f"{date}T21:00:00+00:00",
                "briefing_file": f"{stem}.mp4", "duration_s": 60.0, "mode": "factual",
                "label": label, "renderer": None, "title": f"Synthetic {date}", "model": None,
                "images": False, "subtitles": True, "director": None, "voice": "en-US-DemoNeural",
                "desk": {"equity": 12345.0}, "youtube": {"en": {"id": "demo"}},
                "kind": "video", "slot": slot, "clips": []}
        files = {meta["briefing_file"]: b"EN-" + date.encode()}
        if zh:
            meta["briefing_file_zh"] = f"{stem}_zh.mp4"
            files[meta["briefing_file_zh"]] = b"ZH-" + date.encode()
        if covers:
            meta["cover_file"] = f"{stem}_cover.png"
            files[meta["cover_file"]] = PNG + b"en"
            if zh:
                meta["cover_file_zh"] = f"{stem}_cover_zh.png"
                files[meta["cover_file_zh"]] = PNG + b"zh"
        if legacy:
            meta["mode"] = "story"
            meta["clips"] = [
                {"symbol": "ACME", "clip_file": meta["briefing_file"],
                 "clip_file_zh": meta.get("briefing_file_zh"), "duration_s": 30.0,
                 "bias": "long", "headline": "one", "start_s": 0.0},
                {"symbol": "NOVA", "clip_file": meta["briefing_file"], "duration_s": 30.0,
                 "bias": "flat", "headline": "two", "start_s": 30.0}]
        else:
            for sym in ("OVERVIEW", "ACME"):
                meta["clips"].append({"symbol": sym, "clip_file": f"clip_{sym}.mp4",
                                      "clip_file_zh": f"clip_{sym}_zh.mp4", "duration_s": 30.0,
                                      "bias": None, "headline": sym})
                files[f"clip_{sym}.mp4"] = b"CLIP"
                files[f"clip_{sym}_zh.mp4"] = b"CLIP"
        meta.update(meta_extra or {})
        for name, body in files.items():
            if name not in skip_files:
                (d / name).write_bytes(body)
        (d / "meta.json").write_text(json.dumps(meta))
        stamp = self.clock.t - age
        for p in d.iterdir():
            os.utime(p, (stamp, stamp))
        return d, meta


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(monkeypatch, tmp_path, clock):
    w = World(tmp_path, clock)
    monkeypatch.setattr(web_server, "build_portfolio", lambda: w.portfolio)
    monkeypatch.setattr(web_server, "build_board", lambda: w.board)
    monkeypatch.setattr(web_server, "build_journal", lambda limit=60: w.journal)
    monkeypatch.setattr(web_server, "build_executions", lambda limit=60: w.executions)
    monkeypatch.setattr(web_server, "build_look", w.build_look)
    monkeypatch.setattr(web_server, "set_look_approval", w.set_look_approval)
    monkeypatch.setattr(web_server, "_look_asset", w.look_asset)
    monkeypatch.setattr(web_server, "_look_path", w.look_path)
    monkeypatch.setattr(options_view, "build_options", lambda: w.options)
    monkeypatch.setattr(web_server, "build_chart", w.build_chart)
    monkeypatch.setattr(web_server, "_session_date", lambda: w.session)
    monkeypatch.setattr(web_server, "CONFIG_FILE", w.config)
    monkeypatch.setattr(web_server, "MEDIA_ARCHIVE_DIR", w.archive)
    monkeypatch.setattr(web_server, "_CACHE", web_server._Cache())
    monkeypatch.setattr(pubmod, "STATE_DIR", w.state_dir)
    # A developer's .env may set these; the tests must not depend on it.
    monkeypatch.delenv("PUBLISHER_HOST_LABEL", raising=False)
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    return w


@pytest.fixture
def s3():
    return FakeS3()


@pytest.fixture
def make_pub(world, s3, clock):
    def make(dest=None, **opts):
        dest = dest or R2Destination(s3, "alpaca-dashboard", account_id="0" * 32)
        return pubmod.Publisher(dest, world.state_dir, pubmod.Options(**opts), clock=clock,
                                out=io.StringIO())
    return make


# --------------------------------------------------------------------------
# layout + out-dir mode
# --------------------------------------------------------------------------
def _tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and not p.name.startswith(".")}


def test_out_dir_mode_writes_the_exact_bucket_layout(world, tmp_path):
    world.edition(world.day(-1), "video")
    world.edition(world.day(-1), "slideshow-midday-briefing", zh=False)
    out = tmp_path / "bucket"
    approval = out / "approvals" / f"{world.session}.json"
    approval.parent.mkdir(parents=True)
    approval.write_text(json.dumps({"date": world.session, "approved": True,
                                    "requested_at": "2026-03-04T20:58:12+00:00",
                                    "requested_by": "reviewer@example.com"}))

    assert pubmod.main(["--once", "--out-dir", str(out), "--media"], clock=world.clock) == 0

    d = world.day(-1)
    rec, mid = f"post-market-recap_{d}", f"midday-briefing_{d}"
    assert _tree(out) == {
        "snapshot/latest.json",
        "chart/ACME.json", "chart/NOVA.json", "chart/QBIT-USD.json",
        f"look/{world.session}/plate.png", f"look/{world.session}/keyframe.png",
        f"approvals/{world.session}.json",
        "media/history.json",
        f"media/{d}/video/meta.json", f"media/{d}/video/{rec}.mp4", f"media/{d}/video/{rec}_zh.mp4",
        f"media/{d}/video/{rec}_cover.png", f"media/{d}/video/{rec}_cover_zh.png",
        f"media/{d}/slideshow-midday-briefing/meta.json",
        f"media/{d}/slideshow-midday-briefing/{mid}.mp4",
        f"media/{d}/slideshow-midday-briefing/{mid}_cover.png",
    }
    snap = json.loads((out / "snapshot/latest.json").read_text())
    assert set(snap) == {"schema", "as_of", "publisher", "portfolio", "board", "journal",
                         "executions", "symbols", "look", "options"}
    # the approval in the folder was applied before the snapshot was built
    assert world.set_calls == [(world.session, True)]
    assert snap["look"]["approved"] is True
    chart = json.loads((out / "chart/QBIT-USD.json").read_text())
    assert list(chart["symbols"]) == ["QBIT/USD"] and chart["time_basis"] == "utc"
    assert (out / f"look/{world.session}/plate.png").read_bytes() == PNG


def test_out_dir_mode_needs_no_boto3(world, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)       # any import of it now fails
    out = tmp_path / "bucket"
    assert pubmod.main(["--out-dir", str(out)], clock=world.clock) == 0
    assert (out / "snapshot/latest.json").exists()
    assert not (out / "media").exists()                   # --media is off by default here


def test_out_dir_refuses_the_archive_and_the_repo(world):
    assert pubmod.main(["--out-dir", str(world.archive)]) == 2
    assert pubmod.main(["--out-dir", str(ROOT)]) == 2


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------
def test_snapshot_has_every_section(world, make_pub, s3, clock):
    pub = make_pub()
    assert pub.core_cycle()
    snap = s3.json("snapshot/latest.json")
    assert snap["schema"] == 1
    assert snap["as_of"] == pubmod._iso(clock.t) and snap["as_of"].endswith("+00:00")
    assert snap["publisher"]["host"] == "home"
    assert snap["publisher"]["version"].startswith(pubmod.__version__)
    assert snap["portfolio"] == world.portfolio
    assert snap["board"] == world.board
    assert snap["journal"] == json.loads(json.dumps(world.journal))
    assert snap["executions"] == world.executions
    assert snap["symbols"] == ["ACME", "NOVA", "QBIT/USD"]     # config order, enabled only
    assert snap["look"]["date"] == world.session and snap["look"]["exists"] is True
    assert snap["options"] == world.options
    assert snap["publisher"]["approval"] is None        # no request handled yet
    assert s3.objects["snapshot/latest.json"]["ContentType"] == "application/json"


def test_fallback_symbols_come_from_the_log_not_the_clock(world, make_pub, s3, monkeypatch,
                                                         tmp_path):
    """build_journal reports the TIME field of each "yfinance fallback" line;
    the snapshot carries the symbols the contract promises."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "sigengine.log").write_text("\n".join([
        "2026-03-04 11:30:50,810 INFO sigengine.engine: ACME: IBKR unavailable -> yfinance fallback",
        "2026-03-04 11:30:51,000 INFO sigengine.engine: poll done",
        "2026-03-04 11:30:52,475 INFO sigengine.engine: QBIT/USD: IBKR crypto unavailable -> yfinance fallback",
        "2026-03-04 11:30:53,000 INFO sigengine.engine: ACME: IBKR unavailable -> yfinance fallback",
    ]) + "\n")
    monkeypatch.setattr(web_server, "LOGS_DIR", logs)
    world.journal["engine"]["yfinance_fallback"] = ["11:30:50,810", "11:30:52,475"]
    make_pub().core_cycle()
    eng = s3.json("snapshot/latest.json")["journal"]["engine"]
    assert eng["yfinance_fallback"] == ["ACME", "QBIT/USD"]
    assert world.journal["engine"]["yfinance_fallback"] == ["11:30:50,810", "11:30:52,475"]  # not mutated


def test_engine_symbols_say_whether_the_engine_still_polls_them(world, make_pub, s3):
    """A symbol disabled in sigengine/config.json stays in sigengine/state.json
    with its last bar from months ago. Marked here, so the page can say "not
    polled" instead of "2158 h behind"."""
    t = world.clock.t
    world.journal["engine"]["symbols"] = [
        {"symbol": "ACME", "last_bar": t - 3600, "behind_s": 0.0},
        {"symbol": "OFFL", "last_bar": t - 2158 * 3600, "behind_s": 2157 * 3600.0},
        {"symbol": "QBIT/USD", "last_bar": t - 3600, "behind_s": 0.0},
        {"symbol": "GONE", "last_bar": t - 99 * 3600, "behind_s": 98 * 3600.0},   # no longer configured
    ]
    make_pub().core_cycle()
    rows = s3.json("snapshot/latest.json")["journal"]["engine"]["symbols"]
    assert {r["symbol"]: r["enabled"] for r in rows} == {
        "ACME": True, "OFFL": False, "QBIT/USD": True, "GONE": False}
    assert rows[1]["behind_s"] == 2157 * 3600.0                 # the raw figure is kept
    assert "enabled" not in world.journal["engine"]["symbols"][0]   # builder output not mutated


@pytest.mark.parametrize("config_text", [
    None,                                  # missing file
    "{not json",                           # half-written / corrupt
    '["ACME"]',                            # JSON, wrong shape
    '{"symbols": ["ACME"]}',               # symbols not a map
])
def test_unreadable_config_leaves_engine_symbols_unknown_not_disabled(world, make_pub, s3,
                                                                      config_text):
    """D1: when sigengine/config.json cannot be read, the publisher does not
    know which symbols are polled. Saying enabled=False for all of them made
    the page call every symbol "disabled — not polled" and hid real lag at the
    moment something was broken. Unknown is null, and the rest of the journal
    section still publishes."""
    if config_text is None:
        world.config.unlink()
    else:
        world.config.write_text(config_text)
    t = world.clock.t
    world.journal["engine"]["symbols"] = [
        {"symbol": "ACME", "last_bar": t - 3600, "behind_s": 0.0},
        {"symbol": "NOVA", "last_bar": t - 5 * 3600, "behind_s": 4 * 3600.0},
    ]
    make_pub().core_cycle()
    snap = s3.json("snapshot/latest.json")
    rows = snap["journal"]["engine"]["symbols"]
    assert [r["enabled"] for r in rows] == [None, None]
    assert rows[1]["behind_s"] == 4 * 3600.0
    assert "error" not in snap["journal"]
    assert snap["journal"]["rows"] == json.loads(json.dumps(world.journal["rows"]))
    assert snap["symbols"] == []


def test_config_helpers_tell_unknown_from_empty(world):
    assert pubmod.configured_enabled() == {"ACME", "NOVA", "QBIT/USD"}
    world.config.write_text('{"symbols": {}}')
    assert pubmod.configured_enabled() == set()          # readable, nothing enabled
    assert pubmod.enabled_symbols() == []
    world.config.write_text("{oops")
    assert pubmod.configured_enabled() is None           # not readable: unknown
    assert pubmod.config_symbols() is None
    assert pubmod.enabled_symbols() == []


def test_fallback_symbols_ignores_other_lines():
    assert pubmod.fallback_symbols(["no fallback here", "x: y", ""]) == []


def test_options_section_failure_and_absence(world, make_pub, s3, monkeypatch, clock):
    def boom():
        raise RuntimeError("broker down")
    monkeypatch.setattr(options_view, "build_options", boom)
    pub = make_pub()
    pub.core_cycle()
    snap = s3.json("snapshot/latest.json")
    assert snap["options"] == {"error": "RuntimeError: broker down"}
    assert snap["portfolio"] == world.portfolio
    monkeypatch.setattr(options_view, "build_options", lambda: None)
    clock.t += 60
    pub.core_cycle()
    assert s3.json("snapshot/latest.json")["options"] is None


@pytest.mark.parametrize("section,attr", [
    ("portfolio", "build_portfolio"), ("board", "build_board"), ("journal", "build_journal"),
    ("executions", "build_executions"), ("look", "build_look"),
])
def test_one_failing_builder_never_blocks_the_others(world, make_pub, s3, monkeypatch,
                                                     section, attr):
    def boom(*a, **k):
        raise RuntimeError("source down")
    monkeypatch.setattr(web_server, attr, boom)
    assert make_pub().core_cycle()
    snap = s3.json("snapshot/latest.json")
    assert snap[section] == {"error": "RuntimeError: source down"}
    healthy = {"portfolio": world.portfolio, "board": world.board,
               "executions": world.executions}
    for name, value in healthy.items():
        if name != section:
            assert snap[name] == value
    assert snap["symbols"] == ["ACME", "NOVA", "QBIT/USD"]


def test_everything_failing_still_pushes_a_snapshot(world, make_pub, s3, monkeypatch):
    for attr in ("build_portfolio", "build_board", "build_journal", "build_executions",
                 "build_look", "_session_date"):
        monkeypatch.setattr(web_server, attr, lambda *a, **k: 1 / 0)
    world.config.write_text("{not json")
    pub = make_pub()
    pub.core_cycle()
    snap = s3.json("snapshot/latest.json")
    for name in ("portfolio", "board", "journal", "executions", "look"):
        assert snap[name]["error"].startswith("ZeroDivisionError"), name
    assert snap["symbols"] == []


def test_builder_error_results_are_passed_through(world, make_pub, s3):
    world.portfolio = {"error": "APIError: forbidden"}
    make_pub().core_cycle()
    assert s3.json("snapshot/latest.json")["portfolio"] == {"error": "APIError: forbidden"}


def test_secrets_never_reach_the_snapshot_or_the_log(world, make_pub, s3, monkeypatch, caplog):
    secret = "sekrit-value-0123456789"
    monkeypatch.setenv("ALPACA_SECRET", secret)

    def leaky():
        raise RuntimeError(f"auth failed for {secret} at https://x.invalid/v2?token=tok123&a=1")
    monkeypatch.setattr(web_server, "build_portfolio", leaky)
    caplog.set_level(logging.DEBUG, logger="publisher")
    make_pub().core_cycle()
    err = s3.json("snapshot/latest.json")["portfolio"]["error"]
    assert secret not in err and "tok123" not in err and "<redacted>" in err
    assert secret not in caplog.text and "tok123" not in caplog.text


def test_redact_filter_scrubs_any_log_record(monkeypatch):
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "r2-secret-abcdef")
    rec = logging.LogRecord("botocore", logging.WARNING, __file__, 1,
                            "retrying %s with Authorization: AWS4 abc", ("r2-secret-abcdef",), None)
    pubmod.RedactFilter().filter(rec)
    assert "r2-secret-abcdef" not in rec.getMessage()
    assert "AWS4" not in rec.getMessage()


def test_non_finite_numbers_become_null(world, make_pub, s3):
    world.portfolio = {**world.portfolio, "equity": math.nan, "day_pl_pct": math.inf}
    make_pub().core_cycle()
    body = s3.objects["snapshot/latest.json"]["body"].decode()
    assert "NaN" not in body and "Infinity" not in body

    def no_constants(name):
        raise AssertionError(f"non-JSON constant {name}")
    snap = json.loads(body, parse_constant=no_constants)
    assert snap["portfolio"]["equity"] is None and snap["portfolio"]["day_pl_pct"] is None


# --------------------------------------------------------------------------
# hash skip
# --------------------------------------------------------------------------
def test_the_snapshot_is_sent_on_every_run(world, make_pub, s3, clock):
    """`as_of` is the cloud's only sign of life: an unchanged snapshot still
    goes up every run, while unchanged images and charts do not."""
    pub = make_pub()
    pub.core_cycle()
    first = s3.json("snapshot/latest.json")["as_of"]
    for _ in range(3):
        s3.reset_calls()
        clock.t += 60
        pub.core_cycle()
        assert s3.written() == ["snapshot/latest.json"]
    assert s3.json("snapshot/latest.json")["as_of"] > first


def _contract_freshness() -> dict[str, int]:
    """FRESHNESS from the dashboard's contract.ts: the SPA's side of the
    heartbeat, which the test below holds the publisher to."""
    text = (ROOT / "apps/dashboard/src/contract.ts").read_text("utf-8")
    block = text[text.index("export const FRESHNESS"):]
    block = block[:block.index("} as const")]
    return {k: int(v.replace("_", "")) for k, v in re.findall(r"(\w+):\s*([\d_]+),", block)}


def test_the_snapshot_never_ages_into_the_stale_banner_under_cron_jitter(world, s3, clock,
                                                                         monkeypatch):
    """Hours of quiet minutes (nothing changes) with cron start jitter, slow
    builders and PUT latency, as on the real box. The oldest `as_of` the
    dashboard can be showing is the gap to the next upload plus one SPA poll;
    it must stay clear of the dashboard's stale threshold with room to spare,
    or the 'home box not reporting' banner fires on a healthy box."""
    fresh = _contract_freshness()
    rng = random.Random(20261126)

    class SlowS3(FakeS3):
        def put_object(self, **kw):
            clock.t += rng.uniform(0.1, 0.4)                 # PUT latency
            super().put_object(**kw)
            if kw["Key"] == "snapshot/latest.json":
                landed.append(clock.t)

    def slow(builder):
        def run(*a, **k):
            clock.t += rng.uniform(0.3, 1.0)                 # broker / file reads
            return builder(*a, **k)
        return run

    for name in ("build_portfolio", "build_board", "build_journal", "build_executions"):
        monkeypatch.setattr(web_server, name, slow(getattr(web_server, name)))
    landed: list[float] = []
    bucket = SlowS3()
    pub = pubmod.Publisher(R2Destination(bucket, "alpaca-dashboard", account_id="0" * 32),
                           world.state_dir, clock=clock, out=io.StringIO())
    start, as_of = clock.t, []
    for minute in range(240):
        clock.t = start + 60 * minute + rng.uniform(0.5, 2.0)   # cron start jitter
        pub.core_cycle()
        as_of.append(datetime.fromisoformat(bucket.json("snapshot/latest.json")["as_of"]).timestamp())
    assert len(landed) == len(as_of) == 240                  # one upload per run, no skips
    poll = fresh["snapshotPollMs"] / 1000
    oldest_shown = max(landed[i + 1] + poll - as_of[i] for i in range(len(as_of) - 1))
    margin = 30                                              # network, Worker, a slow run
    assert oldest_shown + margin <= fresh["staleAfterMs"] / 1000, oldest_shown


def test_look_images_and_charts_skip_when_unchanged(world, make_pub, s3, clock):
    pub = make_pub()
    pub.core_cycle()
    assert len(world.chart_calls) == 3
    assert s3.objects[f"look/{world.session}/plate.png"]["ContentType"] == "image/png"
    s3.reset_calls()

    clock.t += 60                                   # charts not due, nothing changed
    pub.core_cycle()
    assert len(world.chart_calls) == 3
    assert not [k for k in s3.written() if k.startswith(("look/", "chart/"))]

    clock.t += pubmod.CHART_REBUILD_SEC             # due: rebuilt but identical
    pub.core_cycle()
    assert len(world.chart_calls) == 6
    assert not [k for k in s3.written() if k.startswith("chart/")]

    clock.t += pubmod.CHART_REBUILD_SEC             # a new bar
    world.chart_close = 104.0
    pub.core_cycle()
    assert sorted(k for k in s3.written() if k.startswith("chart/")) == [
        "chart/ACME.json", "chart/NOVA.json", "chart/QBIT-USD.json"]

    s3.reset_calls()
    (world.assets / "plate.png").write_bytes(JPEG)  # a new plate, of another type
    clock.t += 60
    pub.core_cycle()
    assert [k for k in s3.written() if k.startswith("look/")] == [f"look/{world.session}/plate.png"]
    assert s3.objects[f"look/{world.session}/plate.png"]["ContentType"] == "image/jpeg"


def test_look_images_go_up_before_the_snapshot_that_points_at_them(world, make_pub, s3):
    make_pub().core_cycle()
    order = s3.written()
    snap_at = order.index("snapshot/latest.json")
    for kind in ("plate", "keyframe"):
        assert order.index(f"look/{world.session}/{kind}.png") < snap_at


def test_a_same_date_redraw_gives_the_image_a_new_url(world, make_pub, s3, clock):
    """The stylist can redraw a date's look in place. Same key, new bytes:
    the URL must change too, or a page that already shows the look keeps the
    old picture under the new outfit's text."""
    pub = make_pub()
    pub.core_cycle()
    look = s3.json("snapshot/latest.json")["look"]
    plate_v1, key_v1 = look["plate_url"], look["keyframe_url"]
    assert key_v1 == pubmod.look_asset_url(world.session, "keyframe", PNG + b"k")
    assert key_v1.startswith(f"/api/look/asset?date={world.session}&kind=keyframe&v=")
    assert re.fullmatch(r".*&v=[0-9a-f]{12}", key_v1)

    clock.t += 60
    pub.core_cycle()                                        # nothing redrawn: same URLs
    look = s3.json("snapshot/latest.json")["look"]
    assert (look["plate_url"], look["keyframe_url"]) == (plate_v1, key_v1)

    (world.assets / "keyframe.png").write_bytes(PNG + b"redrawn")
    s3.reset_calls()
    clock.t += 60
    pub.core_cycle()
    look = s3.json("snapshot/latest.json")["look"]
    assert look["keyframe_url"] != key_v1 and look["plate_url"] == plate_v1
    assert s3.written().index(f"look/{world.session}/keyframe.png") < s3.written().index(
        "snapshot/latest.json")


def test_an_image_that_is_not_uploaded_keeps_the_plain_url_until_it_is(world, make_pub, s3, clock,
                                                                      monkeypatch):
    real = world.look_asset
    monkeypatch.setattr(web_server, "_look_asset",
                        lambda d, kind: None if kind == "keyframe" else real(d, kind))
    pub = make_pub()
    pub.core_cycle()
    look = s3.json("snapshot/latest.json")["look"]
    assert look["keyframe_url"] == f"/api/look/asset?date={world.session}&kind=keyframe"
    assert "&v=" in look["plate_url"]
    assert f"look/{world.session}/keyframe.png" not in s3.objects

    monkeypatch.setattr(web_server, "_look_asset", real)    # the stylist finishes the draw
    clock.t += 60
    pub.core_cycle()
    assert "&v=" in s3.json("snapshot/latest.json")["look"]["keyframe_url"]
    assert f"look/{world.session}/keyframe.png" in s3.objects


def test_unchanged_chart_is_resent_hourly(world, make_pub, s3, clock):
    pub = make_pub()
    pub.core_cycle()
    s3.reset_calls()
    clock.t += pubmod.CHART_HEARTBEAT_SEC
    pub.core_cycle()
    assert "chart/ACME.json" in s3.written()


def test_a_failed_chart_keeps_the_last_good_one(world, make_pub, s3, clock):
    pub = make_pub()
    pub.core_cycle()
    good = s3.json("chart/ACME.json")
    world.chart_error = {"error": "no cached bars — has the engine polled yet?"}
    clock.t += pubmod.CHART_REBUILD_SEC
    s3.reset_calls()
    pub.core_cycle()
    assert not [k for k in s3.written() if k.startswith("chart/")]
    assert s3.json("chart/ACME.json") == good


def test_a_chart_that_never_built_is_published_as_an_error(world, make_pub, s3):
    world.chart_error = {"error": "no cached bars — has the engine polled yet?"}
    make_pub().core_cycle()
    chart = s3.json("chart/NOVA.json")
    assert chart["error"].startswith("no cached bars")
    assert chart["schema"] == 1 and chart["time_basis"] == "utc" and "symbols" not in chart


def test_state_is_kept_per_destination(world, make_pub, s3, tmp_path):
    make_pub(DirDestination(tmp_path / "bucket")).core_cycle()
    make_pub().core_cycle()                         # the bucket is still empty: send all
    assert {"snapshot/latest.json", "chart/ACME.json",
            f"look/{world.session}/plate.png"} <= set(s3.written())


def test_force_resends_everything(world, make_pub, s3, clock):
    make_pub().core_cycle()
    s3.reset_calls()
    clock.t += 1
    make_pub(force=True).core_cycle()
    assert {"snapshot/latest.json", "chart/ACME.json",
            f"look/{world.session}/keyframe.png"} <= set(s3.written())


def test_state_file_is_private_and_survives_corruption(world, make_pub, s3, clock):
    make_pub().core_cycle()
    state = world.state_dir / "state.json"
    assert state.stat().st_mode & 0o777 == 0o600
    data = json.loads(state.read_text())
    assert "snapshot/latest.json" in data["destinations"]["r2:" + "0" * 32 + "/alpaca-dashboard"]["objects"]

    state.write_text("{half a json")
    s3.reset_calls()
    clock.t += 1
    assert make_pub().core_cycle()
    assert "snapshot/latest.json" in s3.written()   # no state: everything is sent again
    json.loads(state.read_text())                   # and the file is whole again


# --------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------
def _approval(date, approved=True, **over):
    body = {"date": date, "approved": approved, "requested_at": "2026-03-04T20:58:12+00:00",
            "requested_by": "reviewer@example.com", **over}
    return json.dumps(body).encode()


def test_an_approval_is_applied_once(world, make_pub, s3, clock):
    s3.objects[f"approvals/{world.session}.json"] = {"body": _approval(world.session)}
    pub = make_pub()
    pub.core_cycle()
    assert world.set_calls == [(world.session, True)]
    # applied BEFORE the snapshot, so the same push already shows it
    assert s3.json("snapshot/latest.json")["look"]["approved"] is True

    s3.reset_calls()
    clock.t += 60
    pub.core_cycle()
    assert world.set_calls == [(world.session, True)]
    assert not [c for c in s3.calls if c[0] == "get_object"]   # etag unchanged: not even read

    rec = json.loads((world.state_dir / "state.json").read_text())
    rec = next(iter(rec["destinations"].values()))["approvals"][f"approvals/{world.session}.json"]
    assert rec["outcome"] == "applied" and rec["requested_by"] == "reviewer@example.com"
    assert rec["look_mtime_ns"] == world.look_path(world.session).stat().st_mtime_ns
    report = s3.json("snapshot/latest.json")["publisher"]["approval"]
    assert report["date"] == world.session and report["outcome"] == "applied"
    assert report["requested_at"] == "2026-03-04T20:58:12+00:00" and report["done"] is True


def _requested_at(epoch: float) -> str:
    return pubmod._iso(epoch)


def test_a_request_older_than_a_change_made_at_home_is_superseded(world, make_pub, s3, clock):
    """Signed off in the cloud at T, then the look record was rewritten at
    home (a withdrawal or a redraw on :8787) at T+30: the older request must
    not undo the newer local change."""
    world.touch_look(world.session, at=clock.t - 30)
    s3.objects[f"approvals/{world.session}.json"] = {
        "body": _approval(world.session, requested_at=_requested_at(clock.t - 60))}
    make_pub().core_cycle()
    assert world.set_calls == []
    report = s3.json("snapshot/latest.json")["publisher"]["approval"]
    assert report["outcome"].startswith("superseded") and report["done"] is True


def test_a_lost_state_file_never_replays_an_old_request(world, make_pub, s3, clock):
    key = f"approvals/{world.session}.json"
    s3.objects[key] = {"body": _approval(world.session, requested_at=_requested_at(clock.t - 120))}
    pub = make_pub()
    pub.core_cycle()
    assert world.set_calls == [(world.session, True)]
    # Withdrawn later on :8787, then state.json is lost.
    clock.t += 600
    world.look_approved = None
    world.touch_look(world.session, at=clock.t - 60)
    (world.state_dir / "state.json").unlink()
    pub.core_cycle()
    assert world.set_calls == [(world.session, True)]          # not re-applied
    assert s3.json("snapshot/latest.json")["look"]["approved"] is None


def test_the_publishers_own_write_does_not_supersede_a_newer_request(world, make_pub, s3, clock):
    """A withdrawal written while the sign-off was being applied carries an
    earlier requested_at than the publisher's own write; it still applies."""
    key = f"approvals/{world.session}.json"
    s3.objects[key] = {"body": _approval(world.session, requested_at=_requested_at(clock.t - 10))}
    pub = make_pub()
    pub.core_cycle()
    own = world.look_path(world.session).stat().st_mtime
    s3.objects[key] = {"body": _approval(world.session, False,
                                         requested_at=_requested_at(own - 1))}
    clock.t += 60
    pub.core_cycle()
    assert world.set_calls == [(world.session, True), (world.session, False)]


def test_a_new_request_for_the_same_date_is_applied_again(world, make_pub, s3, clock):
    key = f"approvals/{world.session}.json"
    s3.objects[key] = {"body": _approval(world.session)}
    pub = make_pub()
    pub.core_cycle()
    s3.objects[key] = {"body": _approval(world.session, False,
                                         requested_at="2026-03-04T21:01:00+00:00")}
    clock.t += 60
    pub.core_cycle()
    assert world.set_calls == [(world.session, True), (world.session, False)]
    assert s3.json("snapshot/latest.json")["look"]["approved"] is None


def test_a_request_the_look_already_reflects_writes_nothing(world, make_pub, s3):
    world.look_approved = True
    s3.objects[f"approvals/{world.session}.json"] = {"body": _approval(world.session)}
    s3.objects[f"approvals/{world.day(1)}.json"] = {"body": _approval(world.day(1), False)}
    make_pub().core_cycle()
    assert world.set_calls == []


def test_render_start_is_20_00_eastern_across_dst():
    assert pubmod._iso(pubmod.render_start("2026-03-04")) == "2026-03-05T01:00:00+00:00"   # EST
    assert pubmod._iso(pubmod.render_start("2026-07-06")) == "2026-07-07T00:00:00+00:00"   # EDT


def _at_et(world, day_offset: int, hour: int, minute: int = 0, second: int = 0) -> float:
    d = world.today + timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=pubmod.EASTERN).timestamp()


def test_a_request_that_arrives_after_the_render_is_refused(world, make_pub, s3, clock):
    """Made at 19:50 ET, but the home box was down until 20:05 ET: the render
    already used the locked keyframe, so stamping the sign-off now would
    only leak into a later re-run of single clips. Refused, for good."""
    clock.t = _at_et(world, 0, 20, 5)
    s3.objects[f"approvals/{world.session}.json"] = {"body": _approval(
        world.session, requested_at=_requested_at(_at_et(world, 0, 19, 50)))}
    pub = make_pub()
    pub.core_cycle()
    assert world.set_calls == []
    report = s3.json("snapshot/latest.json")["publisher"]["approval"]
    assert report["outcome"].startswith("rejected: arrived after the 20:00 ET render")
    assert "sign off on the local dashboard" in report["outcome"]
    assert report["done"] is True
    clock.t += 60
    pub.core_cycle()
    assert world.set_calls == []


def test_a_withdrawal_after_the_render_is_refused_too(world, make_pub, s3, clock):
    world.look_approved = True
    clock.t = _at_et(world, 0, 20, 0)                        # the very minute it starts
    s3.objects[f"approvals/{world.session}.json"] = {"body": _approval(
        world.session, False, requested_at=_requested_at(_at_et(world, 0, 19, 59)))}
    make_pub().core_cycle()
    assert world.set_calls == []
    outcome = s3.json("snapshot/latest.json")["publisher"]["approval"]["outcome"]
    assert outcome.startswith("rejected:") and "withdraw on the local dashboard" in outcome


def test_a_request_just_before_the_render_is_applied(world, make_pub, s3, clock):
    clock.t = _at_et(world, 0, 19, 59, 59)
    s3.objects[f"approvals/{world.session}.json"] = {"body": _approval(
        world.session, requested_at=_requested_at(clock.t - 30))}
    make_pub().core_cycle()
    assert world.set_calls == [(world.session, True)]


def test_after_tonights_render_tomorrows_look_can_still_be_signed_off(world, make_pub, s3, clock):
    tomorrow, yesterday = world.day(1), world.day(-1)
    clock.t = _at_et(world, 0, 21, 0)
    for d in (tomorrow, yesterday):
        s3.objects[f"approvals/{d}.json"] = {"body": _approval(d, requested_at=_requested_at(clock.t - 60))}
    make_pub().core_cycle()
    assert world.set_calls == [(tomorrow, True)]             # yesterday's render is long done


def test_a_late_request_the_look_already_reflects_is_still_a_no_op(world, make_pub, s3, clock):
    world.look_approved = True
    clock.t = _at_et(world, 0, 22, 0)
    s3.objects[f"approvals/{world.session}.json"] = {"body": _approval(world.session)}
    make_pub().core_cycle()
    assert world.set_calls == []
    assert s3.json("snapshot/latest.json")["publisher"]["approval"]["outcome"] == "already approved"


MALFORMED = {
    "not json": b"{approved: yes",
    "not an object": b"[1, 2]",
    "missing approved": json.dumps({"date": "D", "requested_at": "2026-03-04T20:58:12+00:00",
                                    "requested_by": "a@example.com"}).encode(),
    "string boolean": None,
    "integer boolean": None,
    "date mismatch": None,
    "bad calendar date": None,
    "extra field": None,
    "naive requested_at": None,
    "future requested_at": None,
    "empty requested_by": None,
    "control char requested_by": None,
    "oversized": None,
    "not utf-8": b"\xff\xfe{}",
}


def _malformed_body(kind, date):
    if MALFORMED[kind] is not None:
        return MALFORMED[kind].replace(b'"D"', json.dumps(date).encode())
    body = json.loads(_approval(date))
    if kind == "string boolean":
        body["approved"] = "true"
    elif kind == "integer boolean":
        body["approved"] = 1
    elif kind == "date mismatch":
        body["date"] = "2020-01-01"
    elif kind == "bad calendar date":
        body["date"] = date[:5] + "02-30"
    elif kind == "extra field":
        body["approved_by"] = "someone"
    elif kind == "naive requested_at":
        body["requested_at"] = "2026-03-04T20:58:12"
    elif kind == "future requested_at":
        body["requested_at"] = "2099-01-01T00:00:00+00:00"
    elif kind == "empty requested_by":
        body["requested_by"] = ""
    elif kind == "control char requested_by":
        body["requested_by"] = "a@example.com\n[forged log line]"
    elif kind == "oversized":
        body["requested_by"] = "x" * 300
        return json.dumps(body).encode() + b" " * pubmod.APPROVAL_MAX_BYTES
    return json.dumps(body).encode()


@pytest.mark.parametrize("kind", sorted(MALFORMED))
def test_a_malformed_approval_is_never_applied(world, make_pub, s3, clock, caplog, kind):
    key = f"approvals/{world.session}.json"
    s3.objects[key] = {"body": _malformed_body(kind, world.session)}
    caplog.set_level(logging.WARNING, logger="publisher")
    pub = make_pub()
    pub.core_cycle()
    assert world.set_calls == []
    assert caplog.text.count("rejected") == 1
    report = s3.json("snapshot/latest.json")["publisher"]["approval"]
    assert report["outcome"].startswith("rejected: ") and report["done"] is True
    assert report["requested_at"] is None or report["requested_at"].isprintable()
    caplog.clear()
    clock.t += 60
    pub.core_cycle()                                 # remembered: not re-read, not re-logged
    assert world.set_calls == [] and "rejected" not in caplog.text


def test_approvals_out_of_window_or_misnamed_are_ignored(world, make_pub, s3):
    for name in (f"approvals/{world.day(-3)}.json", f"approvals/{world.day(4)}.json",
                 "approvals/notes.txt", "approvals/2026-02-30.json",
                 f"approvals/{world.session}.json.bak"):
        s3.objects[name] = {"body": _approval(world.session)}
    make_pub().core_cycle()
    assert world.set_calls == []
    assert not [c for c in s3.calls if c[0] == "get_object"]


def test_a_failed_apply_is_retried_then_given_up(world, make_pub, s3, clock):
    world.set_result = {"error": "could not write look.json: disk full"}
    s3.objects[f"approvals/{world.session}.json"] = {"body": _approval(world.session)}
    pub = make_pub()
    for _ in range(pubmod.APPROVAL_MAX_ATTEMPTS + 2):
        pub.core_cycle()
        clock.t += 60
    assert len(world.set_calls) == pubmod.APPROVAL_MAX_ATTEMPTS


def test_parse_approval_accepts_a_well_formed_request(clock):
    req, why = pubmod.parse_approval(_approval("2026-03-04"), "2026-03-04", clock.t)
    assert why is None
    assert req == pubmod.ApprovalRequest("2026-03-04", True, "2026-03-04T20:58:12+00:00",
                                         "reviewer@example.com")


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------
def test_dry_run_changes_nothing(world, s3, clock, tmp_path):
    world.edition(world.day(-1))
    s3.objects[f"approvals/{world.session}.json"] = {"body": _approval(world.session)}
    s3.objects["media/2000-01-01/video/old.mp4"] = {"body": b"old"}
    out = io.StringIO()
    pub = pubmod.Publisher(R2Destination(s3, "alpaca-dashboard", account_id="0" * 32),
                           world.state_dir, pubmod.Options(dry_run=True), clock=clock, out=out)
    assert pubmod.run_once(pub, world.state_dir) == 0
    assert s3.written() == [] and not [c for c in s3.calls if c[0] == "delete_objects"]
    assert world.set_calls == []
    assert not world.state_dir.exists()
    text = out.getvalue()
    assert "[dry-run] PUT snapshot/latest.json" in text
    assert f"[dry-run] APPLY approvals/{world.session}.json" in text
    assert text.count("[dry-run] PUT media/history.json") == 1
    assert "[dry-run] DELETE media/2000-01-01/video/old.mp4" in text


def test_dry_run_without_r2_config_still_reports(world, monkeypatch, capsys):
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert pubmod.main(["--dry-run", "--no-media"], clock=world.clock) == 0
    assert "[dry-run] PUT snapshot/latest.json" in capsys.readouterr().out


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------
def test_media_uploads_only_final_videos_covers_and_meta(world, make_pub, s3):
    d1, d2, d3 = world.day(-1), world.day(-2), world.day(-3)
    world.edition(d1, "video")
    world.edition(d2, "slideshow-midday-briefing", zh=False, covers=False)
    world.edition(d3, "video", legacy=True)
    world.edition(world.day(-60), "video")                       # past retention
    world.edition(world.day(-4), "video", skip_files=(f"post-market-recap_{world.day(-4)}.mp4",))
    world.edition(world.day(-5), "video", meta_extra={"cover_file": "../../../etc/passwd"})
    (world.archive / "_special_x" / "video").mkdir(parents=True)  # undated: never published
    (world.archive / world.day(-6) / "Bad Slot").mkdir(parents=True)

    assert make_pub().media_cycle()

    rec = lambda d, f: f"media/{d}/video/post-market-recap_{d}{f}"   # noqa: E731
    expected = {
        rec(d1, ".mp4"), rec(d1, "_zh.mp4"), rec(d1, "_cover.png"), rec(d1, "_cover_zh.png"),
        f"media/{d1}/video/meta.json",
        f"media/{d2}/slideshow-midday-briefing/midday-briefing_{d2}.mp4",
        f"media/{d2}/slideshow-midday-briefing/meta.json",
        rec(d3, ".mp4"), rec(d3, "_zh.mp4"), rec(d3, "_cover.png"), rec(d3, "_cover_zh.png"),
        f"media/{d3}/video/meta.json",
        rec(world.day(-5), ".mp4"), rec(world.day(-5), "_zh.mp4"),
        rec(world.day(-5), "_cover_zh.png"), f"media/{world.day(-5)}/video/meta.json",
        "media/history.json",
    }
    assert set(s3.objects) == expected
    assert not [k for k in s3.objects if "clip_" in k]
    uploads = [c for c in s3.calls if c[0] == "upload_file"]
    assert uploads and all(c[2]["ContentType"] in ("video/mp4", "image/png") for c in uploads)
    assert {c[2]["ContentType"] for c in uploads if c[1].endswith(".mp4")} == {"video/mp4"}

    meta = s3.json(f"media/{d1}/video/meta.json")
    assert "desk" not in meta and "youtube" not in meta
    assert "briefing" not in meta and all("file" not in c for c in meta["clips"])

    hist = s3.json("media/history.json")
    assert hist["current"] == d1
    assert [(e["date"], e["slot"]) for e in hist["briefings"]] == [
        (d1, "video"), (d2, "slideshow-midday-briefing"), (d3, "video"),
        (world.day(-5), "video")]
    top = hist["briefings"][0]
    v = lambda key: hashlib.sha256(s3.objects[key]["body"]).hexdigest()[:12]   # noqa: E731
    for field_, suffix in (("briefing", ".mp4"), ("briefing_zh", "_zh.mp4"),
                           ("cover", "_cover.png"), ("cover_zh", "_cover_zh.png")):
        key = rec(d1, suffix)
        assert top[field_] == f"/{key}?v={v(key)}"
    assert top["local"] is False and top["kind"] == "video"
    assert "desk" not in top and "youtube" not in top
    assert all("file" not in c and "file_zh" not in c for c in top["clips"])   # scene clips stay home
    slide = hist["briefings"][1]
    assert slide["kind"] == "slideshow" and "briefing_zh" not in slide and "cover" not in slide
    legacy = hist["briefings"][2]
    assert [c["file"] for c in legacy["clips"]] == [legacy["briefing"]] * 2
    assert legacy["clips"][0]["file_zh"] == legacy["briefing_zh"]
    assert legacy["clips"][1]["start_s"] == 30.0
    unsafe = hist["briefings"][3]
    assert "cover" not in unsafe and unsafe["cover_zh"].split("?")[0].endswith("_cover_zh.png")
    for e in hist["briefings"]:      # every URL is an uploaded object, at that object's version
        for k in ("briefing", "briefing_zh", "cover", "cover_zh", "cover_zh_bili"):
            if k in e:
                path, _, query = e[k].partition("?")
                assert path.startswith("/media/") and path[1:] in s3.objects
                assert query == f"v={v(path[1:])}"


def test_a_re_rendered_edition_gets_new_media_urls(world, make_pub, s3, clock):
    """F10: a redo of the same date and slot keeps every file name. The URL
    must still change, or a browser that cached the old video (the Worker
    lets it keep a versioned binary for an hour) plays it under the new
    edition. Unchanged files keep their URL, so their cache stays good."""
    d = world.day(-1)
    slot_dir, meta = world.edition(d, "video")
    pub = make_pub()
    assert pub.media_cycle()
    before = s3.json("media/history.json")["briefings"][0]

    clock.t += 3600
    (slot_dir / meta["briefing_file"]).write_bytes(b"EN re-rendered")
    (slot_dir / meta["cover_file"]).write_bytes(PNG + b"new cover")
    stamp = clock.t - 600                         # settled, and newer than the first run
    for name in (meta["briefing_file"], meta["cover_file"]):
        os.utime(slot_dir / name, (stamp, stamp))
    assert pub.media_cycle()
    after = s3.json("media/history.json")["briefings"][0]

    assert after["briefing"] != before["briefing"] and after["cover"] != before["cover"]
    assert after["briefing"].split("?")[0] == before["briefing"].split("?")[0]
    assert after["briefing"].endswith(
        "?v=" + hashlib.sha256(b"EN re-rendered").hexdigest()[:12])
    assert after["briefing_zh"] == before["briefing_zh"]        # untouched file, same URL
    assert after["cover_zh"] == before["cover_zh"]


def test_media_url_versions_only_a_real_sha256():
    key = "media/2026-03-04/video/v.mp4"
    assert pubmod.media_url(key, "ab" * 32) == f"/{key}?v={'ab' * 6}"
    for bad in (None, "", "xyz", "AB" * 32, "ab" * 31):
        assert pubmod.media_url(key, bad) == f"/{key}"


def test_media_large_files_go_through_the_managed_transfer(world, s3, clock):
    world.edition(world.day(-1))
    sentinel = object()
    dest = R2Destination(s3, "alpaca-dashboard", account_id="0" * 32, transfer_config=sentinel)
    pubmod.Publisher(dest, world.state_dir, clock=clock).media_cycle()
    uploads = [c for c in s3.calls if c[0] == "upload_file"]
    assert uploads and all(c[3] is sentinel for c in uploads)
    assert all(c[2]["CacheControl"] for c in uploads)


def test_media_is_not_reread_or_resent_when_unchanged(world, make_pub, s3, clock, monkeypatch):
    d, _ = world.edition(world.day(-1))
    pub = make_pub()
    pub.media_cycle()
    s3.reset_calls()
    hashed = []
    real = pubmod._sha256_file
    monkeypatch.setattr(pubmod, "_sha256_file", lambda p: hashed.append(p.name) or real(p))

    clock.t += 60
    pub.media_cycle()
    assert s3.written() == [] and hashed == []

    en = d / f"post-market-recap_{world.day(-1)}.mp4"
    os.utime(en, (clock.t - 1000, clock.t - 1000))      # touched, same bytes
    clock.t += 60
    pub.media_cycle()
    assert s3.written() == [] and hashed == [en.name]

    en.write_bytes(b"EN-rerendered")                    # really changed
    os.utime(en, (clock.t - 500, clock.t - 500))
    clock.t += 60
    pub.media_cycle()
    # ...and history.json, whose URL for it now carries the new version
    assert s3.written() == [f"media/{world.day(-1)}/video/{en.name}", "media/history.json"]


def test_media_waits_for_files_still_being_written(world, make_pub, s3, clock):
    d1 = world.day(-1)
    world.edition(d1, age=10)                            # meta.json just written
    pub = make_pub()
    pub.media_cycle()
    assert s3.written() == ["media/history.json"]
    assert s3.json("media/history.json")["briefings"] == []

    clock.t += pubmod.MEDIA_SETTLE_SEC
    pub.media_cycle()
    assert s3.json("media/history.json")["current"] == d1


def test_a_failed_upload_leaves_no_dangling_url(world, make_pub, s3, clock):
    d1, d2 = world.day(-1), world.day(-2)
    world.edition(d1)
    world.edition(d2)
    s3.fail_keys = {f"media/{d1}/video/post-market-recap_{d1}.mp4",
                    f"media/{d2}/video/post-market-recap_{d2}_zh.mp4"}
    assert make_pub().media_cycle() is False
    hist = s3.json("media/history.json")
    assert [e["date"] for e in hist["briefings"]] == [d2]          # d1 has no EN video
    assert "briefing_zh" not in hist["briefings"][0]

    s3.fail_keys = set()
    clock.t += 60
    assert make_pub().media_cycle() is True
    hist = s3.json("media/history.json")
    assert [e["date"] for e in hist["briefings"]] == [d1, d2]
    assert "briefing_zh" in hist["briefings"][1]


def test_retention_deletes_only_old_dated_media_and_looks(world, make_pub, s3, clock):
    days = pubmod.DEFAULT_RETENTION_DAYS
    old, keep = world.day(-days - 1), world.day(-days)
    for key in (f"media/{old}/video/a.mp4", f"media/{old}/video/meta.json",
                f"look/{old}/plate.png", f"media/{keep}/video/b.mp4", f"look/{keep}/plate.png",
                "media/history.json", f"approvals/{old}.json", "chart/ACME.json",
                "snapshot/latest.json"):
        s3.objects[key] = {"body": b"x"}
    pub = make_pub()
    pub.media_cycle()
    assert set(s3.objects) == {f"media/{keep}/video/b.mp4", f"look/{keep}/plate.png",
                               "media/history.json", f"approvals/{old}.json",
                               "chart/ACME.json", "snapshot/latest.json"}

    s3.objects[f"media/{old}/video/late.mp4"] = {"body": b"x"}
    s3.reset_calls()
    clock.t += 60
    pub.media_cycle()                                   # pruning runs at most hourly
    assert f"media/{old}/video/late.mp4" in s3.objects
    clock.t += pubmod.PRUNE_EVERY_SEC
    pub.media_cycle()
    assert f"media/{old}/video/late.mp4" not in s3.objects


def test_default_retention_fits_the_storage_budget():
    """EXTRA B: 40 days of the measured worst edition (174 MB, every calendar
    day, one more before the prune) plus look images stays under 8 GB."""
    assert pubmod.DEFAULT_RETENTION_DAYS == 40
    assert pubmod.parse_args([]).retention_days == pubmod.DEFAULT_RETENTION_DAYS
    worst_gb = (pubmod.DEFAULT_RETENTION_DAYS + 1) * (0.174 + 0.0027) + 0.174 + 0.002
    assert worst_gb <= 8.0


def test_history_is_resent_daily_so_the_lifecycle_rule_never_expires_it(world, make_pub, s3,
                                                                          clock):
    world.edition(world.day(-1))
    pub = make_pub()
    pub.media_cycle()
    s3.reset_calls()
    clock.t += 3600 * 23
    pub.media_cycle()
    assert "media/history.json" not in s3.written()
    clock.t += 3600 * 2
    pub.media_cycle()
    assert s3.written() == ["media/history.json"]


def test_retention_days_flag_limits_what_is_uploaded(world, make_pub, s3):
    world.edition(world.day(-1))
    world.edition(world.day(-3))
    make_pub(retention_days=2).media_cycle()
    assert [e["date"] for e in s3.json("media/history.json")["briefings"]] == [world.day(-1)]


# --------------------------------------------------------------------------
# locks + loop
# --------------------------------------------------------------------------
def test_an_overlapping_run_exits_immediately(world, tmp_path, monkeypatch):
    out = tmp_path / "bucket"
    pub_probe = pubmod.Publisher(DirDestination(out), world.state_dir)
    core_lock, _ = pubmod._lock_paths(world.state_dir, pub_probe.dest)
    world.state_dir.mkdir(parents=True)
    called = []
    monkeypatch.setattr(web_server, "build_portfolio", lambda: called.append(1) or {})
    with pubmod.try_lock(core_lock) as got:
        assert got
        started = time.monotonic()
        assert pubmod.main(["--out-dir", str(out)], clock=world.clock) == 0
        assert time.monotonic() - started < 5
    assert called == [] and not out.exists()
    assert pubmod.main(["--out-dir", str(out)], clock=world.clock) == 0     # lock released: runs
    assert (out / "snapshot/latest.json").exists()


def test_a_running_media_sync_does_not_stop_the_snapshot(world, tmp_path):
    world.edition(world.day(-1))
    out = tmp_path / "bucket"
    _, media_lock = pubmod._lock_paths(world.state_dir, DirDestination(out))
    world.state_dir.mkdir(parents=True)
    with pubmod.try_lock(media_lock) as got:
        assert got
        assert pubmod.main(["--out-dir", str(out), "--media"], clock=world.clock) == 0
    assert (out / "snapshot/latest.json").exists() and not (out / "media").exists()


def test_loop_runs_cycles_until_stopped(world, make_pub, monkeypatch):
    pub = make_pub()
    stop = threading.Event()
    cycles, media = [], threading.Event()

    def core():
        cycles.append(1)
        if len(cycles) == 2:
            stop.set()
        return True
    monkeypatch.setattr(pub, "core_cycle", core)
    monkeypatch.setattr(pub, "media_cycle", lambda: media.set() or True)
    monkeypatch.setattr(stop, "wait", lambda timeout=None: None)
    assert pubmod.run_loop(pub, world.state_dir, 60, stop) == 0
    assert len(cycles) == 2 and media.wait(5)


# --------------------------------------------------------------------------
# R2 client + config
# --------------------------------------------------------------------------
def test_r2_config_from_env():
    env = {"R2_ACCOUNT_ID": "a" * 32, "R2_ACCESS_KEY_ID": "key-id",
           "R2_SECRET_ACCESS_KEY": "very-secret-value"}
    cfg = R2Config.from_env(env)
    assert cfg.bucket == "alpaca-dashboard"
    assert cfg.endpoint == f"https://{'a' * 32}.r2.cloudflarestorage.com"
    assert "very-secret-value" not in repr(cfg)
    with pytest.raises(ConfigError) as exc:
        R2Config.from_env({"R2_SECRET_ACCESS_KEY": "very-secret-value"})
    assert "R2_ACCOUNT_ID" in str(exc.value) and "very-secret-value" not in str(exc.value)
    with pytest.raises(ConfigError):
        R2Config.from_env({**env, "R2_ACCOUNT_ID": "evil.example.com/x"})
    with pytest.raises(ConfigError):
        R2Config.from_env({**env, "R2_BUCKET": "Bad_Bucket"})


def test_main_without_r2_config_exits_2_and_leaks_nothing(world, monkeypatch, caplog):
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "abc-key-id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "do-not-print-me")
    caplog.set_level(logging.INFO, logger="publisher")
    assert pubmod.main(["--once"]) == 2
    assert "R2_ACCOUNT_ID" in caplog.text and "do-not-print-me" not in caplog.text
    assert not world.state_dir.exists()


def test_main_without_boto3_explains_the_extra(world, monkeypatch, caplog):
    monkeypatch.setenv("R2_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "abc-key-id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "do-not-print-me")
    monkeypatch.setitem(sys.modules, "boto3", None)
    caplog.set_level(logging.INFO, logger="publisher")
    assert pubmod.main(["--once"]) == 2
    assert "pip install -c requirements/constraints.txt boto3" in caplog.text


def test_r2_list_paginates_and_a_missing_object_is_none():
    s3 = FakeS3(page_size=2)
    for i in range(5):
        s3.objects[f"approvals/2026-03-0{i + 1}.json"] = {"body": b"{}"}
    dest = R2Destination(s3, "alpaca-dashboard")
    assert [o.key for o in dest.list("approvals/")] == sorted(s3.objects)
    assert len([c for c in s3.calls if c[0] == "list_objects_v2"]) == 3
    assert dest.get_bytes("approvals/2027-01-01.json", 10) is None
    assert dest.get_bytes("approvals/2026-03-01.json", 10) == b"{}"


def test_r2_get_reads_at_most_the_limit_plus_one():
    s3 = FakeS3()
    s3.objects["approvals/2026-03-01.json"] = {"body": b"x" * 100}
    assert len(R2Destination(s3, "b-1").get_bytes("approvals/2026-03-01.json", 10)) == 11


def test_r2_other_errors_are_not_swallowed():
    class Broken(FakeS3):
        def get_object(self, **kw):
            raise FakeClientError("AccessDenied")
    with pytest.raises(FakeClientError):
        R2Destination(Broken(), "b-1").get_bytes("approvals/2026-03-01.json", 10)


def test_r2_delete_batches_by_a_thousand_and_reports_errors():
    s3 = FakeS3()
    keys = [f"media/2020-01-01/video/f{i}.mp4" for i in range(2500)]
    R2Destination(s3, "b-1").delete(keys)
    assert [len(c[1]) for c in s3.calls if c[0] == "delete_objects"] == [1000, 1000, 500]

    class Failing(FakeS3):
        def delete_objects(self, **kw):
            return {"Errors": [{"Key": "x", "Code": "AccessDenied"}]}
    with pytest.raises(DestinationError):
        R2Destination(Failing(), "b-1").delete(keys[:3])


@pytest.mark.parametrize("key", ["../x", "a/../b", "/abs", "a//b", ".hidden", "a/.b", "a\\b",
                                 "a%2fb", "", "a/", "a b", "x" * 600])
def test_unsafe_keys_are_refused(key, tmp_path):
    with pytest.raises(ValueError):
        check_key(key)
    with pytest.raises(ValueError):
        DirDestination(tmp_path).put_bytes(key, b"x", "text/plain")


def test_dir_destination_round_trip(tmp_path):
    dest = DirDestination(tmp_path / "b")
    dest.put_bytes("approvals/2026-03-04.json", b"{}", "application/json")
    src = tmp_path / "v.mp4"
    src.write_bytes(b"video")
    dest.put_file("media/2026-03-04/video/v.mp4", src, "video/mp4")
    assert [o.key for o in dest.list("media/")] == ["media/2026-03-04/video/v.mp4"]
    assert dest.get_bytes("approvals/2026-03-04.json", 10) == b"{}"
    assert dest.get_bytes("approvals/2026-03-05.json", 10) is None
    dest.delete(["media/2026-03-04/video/v.mp4"])
    assert not (tmp_path / "b" / "media").exists()            # empty dirs tidied
    assert dest.list("nothing/") == []


# --------------------------------------------------------------------------
# safety rails
# --------------------------------------------------------------------------
def test_importing_the_publisher_loads_no_order_path_and_no_boto3(tmp_path):
    """Mirrors test_web_server's guarantee, at run time: importing the
    publisher and the chart builder it calls pulls in nothing that can place an
    order, and boto3 stays unloaded until a real upload needs it."""
    code = (
        "import json, sys\n"
        "import web.publisher\n"
        "import backtest.chart_view\n"
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


# A whole publisher run in a fresh interpreter, with the REAL builders
# (web.server build_portfolio / build_executions / build_chart, web.options_view
# build_options) talking to a fake broker. Only the broker client and the one
# file write (set_look_approval) are replaced. It prints what the run loaded and
# what it asked the broker for.
_MAIN_PATH_PROBE = r"""
import importlib.abc, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

FORBIDDEN = ("relay.order_manager", "relay.csp_manager", "relay.options_client")
attempts = []

class Tripwire(importlib.abc.MetaPathFinder):
    # Records every ATTEMPT, so an import that fails, or is caught, still counts.
    def find_spec(self, name, path=None, target=None):
        if name in FORBIDDEN or "order_manager" in name:
            attempts.append(name)
        return None

sys.meta_path.insert(0, Tripwire())

tmp = Path(sys.argv[1])
import numpy as np, pandas as pd
from relay import bars_db
import relay.alpaca_client as alpaca_client
from web import publisher, server as web_server
from web.r2 import DirDestination

# --- a fake broker: the read methods the builders use, and a tripwire for the rest
asked = []

class Unexpected:
    def __getattr__(self, name):
        if not name.startswith("_"):
            asked.append(f"{type(self).__name__}.{name}")
        raise AttributeError(name)

class FakeTrading(Unexpected):
    def get_orders(self, req):
        t0 = datetime(2026, 3, 2, 15, tzinfo=timezone.utc)
        def fill(side, qty, price, hours):
            return SimpleNamespace(symbol="ACME", side=side, filled_qty=str(qty),
                                   filled_avg_price=str(price), order_type="market",
                                   filled_at=t0 + timedelta(hours=hours))
        return [fill("buy", 10, 100.0, 0), fill("sell", 10, 103.0, 30)]

class FakeAlpacaClient(Unexpected):
    paper = True
    def __init__(self, settings):
        self.client = FakeTrading()
    def get_account(self):
        return SimpleNamespace(equity="25000", last_equity="24800", cash="1000",
                               buying_power="2000", status="ACTIVE")
    def get_all_positions(self):
        return [SimpleNamespace(symbol="ACME", qty="12", side="long", avg_entry_price="101.5",
                                current_price="103", market_value="1236", cost_basis="1218",
                                unrealized_pl="18", unrealized_plpc="0.0148",
                                asset_class="us_equity")]
    def get_latest_trade(self, symbol):
        return 103.0, None

alpaca_client.AlpacaClient = FakeAlpacaClient
web_server._CLIENT = None

applied = []
web_server.set_look_approval = lambda date, approved: applied.append((date, approved)) or {}

# --- synthetic inputs, all under tmp
entry = json.loads((Path(web_server.ROOT) / "sigengine" / "config.json").read_text())["symbols"]["AAPL"]
config = tmp / "config.json"
config.write_text(json.dumps({"symbols": {"ACME": {**entry, "enabled": True},
                                          "OFFL": {**entry, "enabled": False}}}))
web_server.CONFIG_FILE = config
decisions = tmp / "decisions.json"
decisions.write_text(json.dumps({"date": "2026-03-04", "watchlist": ["ACME"],
                                 "bias": {"ACME": "long"}, "priority": {"ACME": 3},
                                 "suggested_size": {"ACME": 0.97}, "notes": {"_": "synthetic"}}))
web_server.DECISIONS_FILE = decisions
idx = pd.date_range("2026-01-05 14:00", periods=400, freq="h", tz="UTC")
close = 100 + np.cumsum(np.sin(np.arange(400) / 7.0))
bars_db.upsert_bars("ACME", entry["interval"], pd.DataFrame(
    {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0},
    index=idx), "alpaca")

noon = datetime.now(publisher.EASTERN).replace(hour=12, minute=0, second=0, microsecond=0)
out = tmp / "bucket"
session = noon.date().isoformat()
approval = out / "approvals" / f"{session}.json"
approval.parent.mkdir(parents=True)
approval.write_text(json.dumps({"date": session, "approved": True,
                                "requested_at": (noon - timedelta(hours=1)).isoformat(),
                                "requested_by": "viewer@example.com"}))
archive = Path(web_server.MEDIA_ARCHIVE_DIR) / session / "video"
archive.mkdir(parents=True)
(archive / "recap.mp4").write_bytes(b"synthetic video")
(archive / "meta.json").write_text(json.dumps({"date": session, "briefing_file": "recap.mp4",
                                               "generated_at": noon.isoformat(), "clips": []}))
import os
for f in archive.iterdir():
    os.utime(f, (noon.timestamp() - 3600,) * 2)

rc = publisher.main(["--out-dir", str(out), "--media", "--retention-days", "5"],
                    clock=noon.timestamp)
snap = json.loads((out / "snapshot" / "latest.json").read_text())
chart = json.loads((out / "chart" / "ACME.json").read_text())
print(json.dumps({
    "rc": rc,
    "attempts": attempts,
    "loaded": sorted(m for m in sys.modules if m in FORBIDDEN or "order_manager" in m),
    "asked": asked,
    "applied": applied,
    "broker_client_loaded": "relay.alpaca_client" in sys.modules,
    "equity": snap["portfolio"].get("equity"),
    "fills": len(snap["executions"].get("rows") or []),
    "options": type(snap["options"]).__name__,
    "chart_candles": len(chart.get("symbols", {}).get("ACME", {}).get("candles", [])),
    "history": (out / "media" / "history.json").exists(),
}))
"""


def test_a_full_publisher_run_never_reaches_the_order_path(tmp_path):
    """SEC-5: the import-time check above is not the whole story. Every cycle
    builds a real broker client (web.server._client -> relay.alpaca_client),
    which COULD place orders. This runs main() end to end: approvals, look,
    snapshot, charts with strategy markers, media. It checks that nothing
    imports relay.order_manager, relay.csp_manager or relay.options_client,
    even in an attempt that failed, and that the broker is asked only for
    the read methods the fake provides. A future builder that calls
    market_order, close_position, submit_order or anything else fails here."""
    data = tmp_path / "data"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT),
           "ALPACA_DATA_DIR": str(data), "BRIEFING_ARCHIVE_DIR": str(tmp_path / "archive"),
           "ALPACA_KEY": "PKTESTKEY", "ALPACA_SECRET": "testsecret", "PAPER_TRADING": "true"}
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "PUBLISHER_HOST_LABEL"):
        env.pop(name, None)
    res = subprocess.run([sys.executable, "-c", _MAIN_PATH_PROBE, str(tmp_path)], cwd=ROOT,
                         env=env, capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-4000:]
    got = json.loads(res.stdout.strip().splitlines()[-1])
    assert got["attempts"] == [] and got["loaded"] == []
    assert got["asked"] == []
    # ...and the run really went through the broker-backed builders.
    assert got["rc"] == 0
    assert got["broker_client_loaded"]
    assert got["equity"] == 25000.0 and got["fills"] == 2
    assert got["options"] in ("dict", "NoneType")
    assert got["chart_candles"] > 0
    assert got["history"]


@pytest.mark.parametrize("module", ["web/publisher.py", "web/r2.py", "web/options_view.py"])
def test_publisher_source_never_imports_the_order_path(module):
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
                   for bad in ("order_manager", "csp_manager", "options_client", "relay.models"))
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not ({"submit_order", "market_order", "limit_order", "close_position",
                 "close_all_positions", "cancel_orders"} & calls)


def test_a_full_run_writes_only_the_bucket_and_its_own_state(world, tmp_path):
    world.edition(world.day(-1))
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    out = tmp_path / "bucket"
    assert pubmod.main(["--out-dir", str(out), "--media"], clock=world.clock) == 0
    after = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    changed = {p for p in after if before.get(p) != after[p]}
    assert changed and all(p.is_relative_to(out) or p.is_relative_to(world.state_dir)
                           for p in changed)
    assert {p.name for p in changed if p.is_relative_to(world.state_dir)} <= {
        "state.json", "state.json.lock"} | {p.name for p in world.state_dir.glob("*.lock")}

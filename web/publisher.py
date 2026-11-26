"""Push the dashboard's read-only data from the home box to Cloudflare R2.

The cloud dashboard (apps/dashboard) never calls this machine. Instead this
publisher runs here, every minute from cron, reuses the SAME read-only builders
the local dashboard serves (web/server.py), and uploads their output to an R2
bucket. Broker keys never leave the box; the bucket only ever holds what the
builders already show on :8787.

    python -m web.publisher                 # one cycle (the cron default, --once)
    python -m web.publisher --loop 60       # keep running, one cycle a minute
    python -m web.publisher --out-dir DIR   # write the bucket layout to DIR, no R2
    python -m web.publisher --dry-run       # print what would be uploaded/applied

THE BUCKET LAYOUT is the contract with the Worker (apps/dashboard/src/contract.ts):

    snapshot/latest.json        portfolio, board, journal, executions, symbols, look,
                                options (the put sleeve), publisher.approval
    chart/<KEY>.json            build_chart(symbol, 220) in TRUE UTC epochs;
                                KEY = symbol with "/" -> "-" (BTC/USD -> BTC-USD)
    look/<date>/plate.png       the look's wardrobe plate
    look/<date>/keyframe.png    the look's stage keyframe
    media/history.json          the recap index, URLs rewritten to /media/...?v=<sha>
    media/<date>/<slot>/<file>  final EN + zh recap videos, covers, meta.json
    approvals/<date>.json       written by the Worker; read and applied here

ONE CYCLE, in order:
  1. approvals  apply any new approvals/<date>.json via set_look_approval, once
                per distinct object (content hash), never a malformed one, and
                never once the date's 20:00 ET render has started
  2. look       build the session date's look and upload its plate + keyframe
                images, BEFORE the snapshot that points at them; each image URL
                carries a content version (&v=), so a redraw is a new URL
  3. snapshot   build every section separately (one failing source becomes
                {"error": ...} in its own section and blocks nothing else)
  4. charts     at most every 5 minutes
  5. media      new/changed archive files, history.json, retention pruning.
                Runs under its OWN lock after the main lock is released, so a
                long video upload never stops the next minute's snapshot.

Unchanged objects are not re-uploaded: every key's content hash lives in
$ALPACA_DATA_DIR/publisher/state.json (written atomically). The snapshot is the
exception: it is re-sent on EVERY run, changed or not, because its `as_of` is
the cloud's only evidence that this box is alive, and the dashboard warns after
3 minutes of silence. A skip-unless-older-than-N heartbeat slips by a whole
cron period under jitter (a run 119.6 s after the last upload skips, so the
resend lands at 3 minutes); one PUT a minute is ~43k a month, well inside
R2's free tier.

READ-ONLY OVER THE TRADING PATH, like web/server.py. A run never imports
relay.order_manager, relay.csp_manager or relay.options_client. It DOES hold
a broker client: web.server._client() builds a relay.alpaca_client.AlpacaClient
for the account, executions and options sections. That client can place
orders, but the publisher only ever calls read methods on it (get_account,
get_all_positions, get_latest_trade, and get_orders on the trading client).
tests/test_publisher.py runs a whole cycle in a fresh interpreter against a
fake broker that offers only those and fails on anything else. The only file
outside its own state/lock directory it can change is the look record's
approval boolean, through web.server.set_look_approval.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import io
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from zoneinfo import ZoneInfo

from relay.config import DATA_DIR
from web import options_view
from web import server as web_server
from web.r2 import ConfigError, DirDestination, ObjectInfo, R2Config, R2Destination, check_key

__version__ = "1.0.0"

log = logging.getLogger("publisher")

ROOT = Path(__file__).resolve().parent.parent
# Patched by the tests; everything the publisher writes for itself lives here.
STATE_DIR = DATA_DIR / "publisher"
EASTERN = ZoneInfo("America/New_York")

SCHEMA = 1
SNAPSHOT_KEY = "snapshot/latest.json"
HISTORY_KEY = "media/history.json"
CHART_BARS = 220

# Charts recompute every strategy over the full bar history: at most every 5
# minutes. The slack absorbs cron start jitter, so a run 299.6 s after the last
# build does not push the next one to 6 minutes.
CHART_REBUILD_SEC = 300
CRON_JITTER_SEC = 10
# The nightly recap renders at 20:00 US/Eastern on the session date and reads
# the look's approval when it starts (briefing/host_briefing.day_keyframe). A
# sign-off for date D that reaches this box at or after that moment is refused:
# the video already used the locked keyframe, and stamping the approval now
# would put the look into a later re-run of single clips (2026-09-18).
RENDER_HOUR_ET = 20
# An unchanged chart is re-sent hourly so its as_of does not claim the chart
# has been stale all weekend.
CHART_HEARTBEAT_SEC = 3600
PRUNE_EVERY_SEC = 3600

# R2's free tier is 10 GB-month; the budget is 8 GB at steady state. Measured
# on the recap archive over 2026-10-27..11-26: 22 editions in 30 days, each
# uploading 155 MB on average (EN + zh mp4, two covers, meta.json; max
# 174 MB), i.e. 114 MB per calendar day, plus ~2.7 MB of look images per date
# and ~1 MB of snapshot/charts/history. 40 days keeps 41 dated folders:
# ~4.8 GB at the measured rate, ~7.4 GB even with a 174 MB edition EVERY day
# and one more uploaded before the hourly prune. (45 would be ~8.3 GB then.)
# The bucket's media/ lifecycle rule must expire objects LATER than this:
# retention + 2 days (docs/cloud-dashboard.md, step 2).
DEFAULT_RETENTION_DAYS = 40
# history.json is re-sent at least this often even when unchanged, so its
# upload age stays far below the bucket's media/ lifecycle rule. Otherwise a
# long gap in recaps would let the rule expire the index of the recaps that
# are still there.
HISTORY_HEARTBEAT_SEC = 86400
# A file modified this recently may still be being written by the renderer
# (briefing/generate.py copies the mp4s first and writes meta.json last).
MEDIA_SETTLE_SEC = 120
META_MAX_BYTES = 1024 * 1024
LOOK_ASSET_MAX_BYTES = 25 * 1024 * 1024

APPROVAL_MAX_BYTES = 4096
# The Worker accepts [ET today - 1, ET today + 3]; one more day in the past
# here so a request made just before ET midnight is still applied after it.
APPROVAL_WINDOW_DAYS = (-2, 3)
APPROVAL_MAX_ATTEMPTS = 3
_APPROVAL_FIELDS = frozenset({"date", "approved", "requested_at", "requested_by"})

JSON_TYPE = "application/json"
NO_CACHE = "private, no-cache"
MEDIA_CACHE = "private, max-age=300"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_APPROVAL_KEY_RE = re.compile(r"^approvals/(\d{4}-\d{2}-\d{2})\.json$")
_DATED_KEY_RE = re.compile(r"^(?:media|look)/(\d{4}-\d{2}-\d{2})/")
# The same shapes the Worker's /media route accepts (worker/validate.ts), so
# nothing is uploaded that the dashboard would then refuse to serve.
_SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MEDIA_PATH_MAX = 256
# The contract's SYMBOL_RE: a symbol becomes part of an object key.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}(?:[/.-][A-Z0-9]{1,15})?$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

MEDIA_TYPES = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".webp": "image/webp"}
# (meta.json field naming a file, history.json field carrying its URL)
MEDIA_FILE_FIELDS = (
    ("briefing_file", "briefing"),
    ("briefing_file_zh", "briefing_zh"),
    ("cover_file", "cover"),
    ("cover_file_zh", "cover_zh"),
    ("cover_file_zh_bili", "cover_zh_bili"),
)
# Never displayed by the dashboard, so never sent: `desk` is an account digest
# and `youtube` holds upload records.
_META_DROP = ("desk", "youtube")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _et_today(epoch: float) -> Date:
    return datetime.fromtimestamp(epoch, EASTERN).date()


def _valid_date(text: str) -> bool:
    if not isinstance(text, str) or not _ISO_DATE_RE.fullmatch(text):
        return False
    try:
        Date.fromisoformat(text)
    except ValueError:
        return False
    return True


def chart_key(symbol: str) -> str:
    """R2 key segment for a symbol: "/" becomes "-" (BTC/USD -> BTC-USD)."""
    return symbol.replace("/", "-")


def _clean(obj):
    """JSON-safe copy: NaN/Infinity become null (JavaScript's JSON.parse rejects
    them and would lose the WHOLE snapshot over one bad broker field), numpy
    scalars become Python numbers, anything else unknown becomes a string."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_clean(v) for v in obj]
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return _clean(item())
        except (TypeError, ValueError):
            pass
    return str(obj)


def _dumps(obj) -> bytes:
    return json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(obj) -> str:
    canon = json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"),
                       sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# --------------------------------------------------------------------------
# secrets never reach a log line or the snapshot's error strings
# --------------------------------------------------------------------------
_SECRET_ENV_NAMES = ("ALPACA_KEY", "ALPACA_SECRET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                     "DASHBOARD_TOKEN", "WEBHOOK_SECRET", "TWS_PASSWORD", "ANTHROPIC_API_KEY")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|key|secret|signature|sig|apikey|access_token|x-amz-[a-z-]+)=)[^&\s\"'<>]*")
_HEADER_SECRET_RE = re.compile(
    r"(?i)\b(authorization|apca-api-secret-key|apca-api-key-id|x-dashboard-token)(\s*[:=]\s*)\S+")


def redact(text: object) -> str:
    """Remove secret env values, secret-looking query parameters and auth
    headers from `text`."""
    out = str(text)
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name) or ""
        if len(value) >= 6:
            out = out.replace(value, "<redacted>")
    out = _QUERY_SECRET_RE.sub(r"\1<redacted>", out)
    return _HEADER_SECRET_RE.sub(r"\1\2<redacted>", out)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def _err(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:500]


# --------------------------------------------------------------------------
# chart time: TRUE UTC
# --------------------------------------------------------------------------
_CHART_PATCH_LOCK = threading.Lock()


def utc_epoch(ts) -> int:
    """True UTC epoch seconds of a bar timestamp."""
    import pandas as pd

    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.timestamp())


@contextlib.contextmanager
def true_utc_chart_time() -> Iterator[None]:
    """Make backtest.chart_view stamp TRUE UTC epochs while the block runs.

    chart_view._chart_ts converts each bar to Melbourne wall-clock and re-tags
    those fields as UTC (so the old page, which formats with UTC getters, reads
    Melbourne time). That shift cannot be undone from the output: in the April
    fall-back hour two different bars map to the same fake epoch, and the
    builder then drops one of them as a duplicate. So the shift is never
    applied in the first place. Every candle, indicator and marker path in
    chart_view calls `_chart_ts` through the module global, so swapping that
    one function is exact for all of them, across both DST transitions.

    The local dashboard runs in its own process and is untouched; the swap is
    undone even if the build raises.
    """
    from backtest import chart_view

    with _CHART_PATCH_LOCK:
        original = chart_view._chart_ts
        chart_view._chart_ts = utc_epoch
        try:
            yield
        finally:
            chart_view._chart_ts = original


# --------------------------------------------------------------------------
# state + locks
# --------------------------------------------------------------------------
def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _flock(path: Path, *, blocking: bool) -> Iterator[bool]:
    """flock() on `path`; yields whether it was acquired. The kernel drops the
    lock when the process dies, so a crashed run never leaves a stale lock."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            got = True
        except BlockingIOError:
            got = False
        yield got
    finally:
        os.close(fd)


def try_lock(path: Path):
    """Non-blocking run lock: `with try_lock(p) as got: if not got: ...`."""
    return _flock(path, blocking=False)


class StateStore:
    """state.json: per-destination upload hashes, chart timer, applied approvals.

    {"version": 1, "destinations": {"<dest ident>": {
        "objects":   {key: {"sha256", "at", "size", ...}},   small objects
        "media":     {key: {"sha256", "size", "mtime_ns", "at"}},
        "approvals": {key: {"sha256", "etag", "outcome", "done", ...}},
        "charts_built_at": epoch, "pruned_at": epoch}}}

    State is kept per destination so an --out-dir run never makes the real
    bucket look up to date. Every write re-reads the file under a lock and
    changes only the caller's keys, so the main cycle and a media upload
    running in another process never lose each other's updates.
    """

    def __init__(self, path: Path, dest_ident: str, *, readonly: bool = False) -> None:
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self.ident = dest_ident
        self.readonly = readonly
        self._mem = threading.Lock()
        self._data: dict = {}

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("state file unreadable (%s); starting from empty state",
                        type(exc).__name__)
            return {}
        return data if isinstance(data, dict) else {}

    def refresh(self) -> dict:
        with self._mem:
            self._data = self._load()
        return self.dest()

    def dest(self) -> dict:
        dests = self._data.get("destinations")
        sub = dests.get(self.ident) if isinstance(dests, dict) else None
        return sub if isinstance(sub, dict) else {}

    def update(self, fn: Callable[[dict], None]) -> None:
        if self.readonly:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._mem, _flock(self.lock_path, blocking=True):
            data = self._load()
            dests = data.get("destinations")
            if not isinstance(dests, dict):
                dests = data["destinations"] = {}
            sub = dests.get(self.ident)
            if not isinstance(sub, dict):
                sub = dests[self.ident] = {}
            fn(sub)
            data["version"] = 1
            _atomic_write(self.path, json.dumps(data, indent=1, sort_keys=True).encode())
            self._data = data


# --------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ApprovalRequest:
    date: str
    approved: bool
    requested_at: str
    requested_by: str


def parse_approval(raw: bytes, key_date: str, now: float) -> tuple[ApprovalRequest | None, str | None]:
    """(request, None) for a well-formed approvals/<key_date>.json body, else
    (None, reason). Strict on purpose: the body comes from the internet side,
    and a request is only ever applied when every field is exactly right."""
    if len(raw) > APPROVAL_MAX_BYTES:
        return None, f"larger than {APPROVAL_MAX_BYTES} bytes"
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "not valid JSON"
    if not isinstance(body, dict):
        return None, "not a JSON object"
    missing = _APPROVAL_FIELDS - body.keys()
    if missing:
        return None, f"missing {', '.join(sorted(missing))}"
    extra = body.keys() - _APPROVAL_FIELDS
    if extra:
        return None, f"unexpected field(s) {', '.join(sorted(map(str, extra)))}"
    d = body["date"]
    if not _valid_date(d):
        return None, "date is not YYYY-MM-DD"
    if d != key_date:
        return None, "date does not match the object key"
    if not isinstance(body["approved"], bool):
        return None, "approved must be true or false"
    at = body["requested_at"]
    if not isinstance(at, str) or len(at) > 64:
        return None, "requested_at is not a timestamp"
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return None, "requested_at is not ISO-8601"
    if when.tzinfo is None:
        return None, "requested_at has no UTC offset"
    if when.timestamp() > now + 86400:
        return None, "requested_at is in the future"
    who = body["requested_by"]
    if (not isinstance(who, str) or not 1 <= len(who) <= 320
            or any(ord(c) < 32 or ord(c) == 127 for c in who)):
        return None, "requested_by is not a plain identity string"
    return ApprovalRequest(d, body["approved"], at, who), None


def _peek_requested_at(raw: bytes) -> str | None:
    """requested_at from a body that failed validation, if it is readable.
    Display only: it lets the page match a refusal to its request."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    at = body.get("requested_at") if isinstance(body, dict) else None
    return at if isinstance(at, str) and len(at) <= 64 and at.isprintable() else None


def _look_mtime_ns(date: str) -> int | None:
    """mtime of the look record for `date`, or None when there is none."""
    try:
        p = web_server._look_path(date)
        return p.stat().st_mtime_ns if p is not None else None
    except OSError:
        return None


def _in_approval_window(d: str, today: Date) -> bool:
    day = Date.fromisoformat(d)
    lo, hi = APPROVAL_WINDOW_DAYS
    return today + timedelta(days=lo) <= day <= today + timedelta(days=hi)


def render_start(d: str) -> float:
    """Epoch of the 20:00 US/Eastern render for session date `d` (DST-aware)."""
    day = Date.fromisoformat(d)
    return datetime(day.year, day.month, day.day, RENDER_HOUR_ET, tzinfo=EASTERN).timestamp()


def look_asset_url(date: str, kind: str, data: bytes) -> str:
    """The Worker URL of a look image, versioned by the image's content.

    build_look's URL is the same for every image a date ever has, so a page
    showing it never re-requests the image when the stylist redraws the look
    for the same date: the new outfit's text would sit over the old picture.
    A content version makes every redraw a new URL. The Worker ignores `v`."""
    return f"/api/look/asset?date={date}&kind={kind}&v={hashlib.sha256(data).hexdigest()[:12]}"


# --------------------------------------------------------------------------
# the recap archive
# --------------------------------------------------------------------------
@dataclass
class ArchiveEdition:
    date: str
    slot: str
    dir: Path
    meta: dict
    files: dict[str, Path] = field(default_factory=dict)   # file name -> local path

    @property
    def prefix(self) -> str:
        return f"media/{self.date}/{self.slot}/"

    def clean_meta(self) -> dict:
        """meta.json as uploaded: file NAMES only, no URLs, nothing undisplayed."""
        m = copy.deepcopy(self.meta)
        for k in (*_META_DROP, "local", *(url for _, url in MEDIA_FILE_FIELDS)):
            m.pop(k, None)
        for c in m.get("clips") or []:
            if isinstance(c, dict):
                c.pop("file", None)
                c.pop("file_zh", None)
        m.setdefault("date", self.date)
        m["kind"] = "slideshow" if self.slot.startswith("slideshow") else "video"
        m["slot"] = self.slot
        return m


def _safe_media_file(slot_dir: Path, archive_root: Path, name) -> Path | None:
    """The archive file `name` inside `slot_dir`, or None when the name is not
    a plain file name the Worker would serve, is a per-scene clip, or resolves
    anywhere outside the archive."""
    if not isinstance(name, str) or not _FILE_RE.fullmatch(name) or name.startswith("clip_"):
        return None
    if Path(name).suffix.lower() not in MEDIA_TYPES:
        return None
    if len(f"{slot_dir.parent.name}/{slot_dir.name}/{name}") > _MEDIA_PATH_MAX:
        return None
    p = slot_dir / name
    try:
        real = p.resolve(strict=True)
    except OSError:
        return None
    if not real.is_relative_to(archive_root) or not real.is_file():
        return None
    return p


def scan_archive(root: Path, cutoff: Date, now: float) -> list[ArchiveEdition]:
    """Dated editions (<root>/<YYYY-MM-DD>/<slot>/meta.json) on or after `cutoff`,
    newest first. Underscore dirs (_special_..., test renders) are not dated and
    are skipped, exactly as briefing/generate.py's rebuild_history skips them."""
    try:
        root_real = root.resolve(strict=True)
        days = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []
    out: list[ArchiveEdition] = []
    for day in days:
        if not _valid_date(day.name) or Date.fromisoformat(day.name) < cutoff:
            continue
        try:
            slots = sorted(p for p in day.iterdir() if p.is_dir())
        except OSError:
            continue
        for slot_dir in slots:
            if not _SLOT_RE.fullmatch(slot_dir.name):
                continue
            meta_path = slot_dir / "meta.json"
            try:
                st = meta_path.stat()
                if st.st_size > META_MAX_BYTES:
                    log.warning("media %s/%s: meta.json too large; skipped", day.name, slot_dir.name)
                    continue
                if now - st.st_mtime < MEDIA_SETTLE_SEC:
                    log.debug("media %s/%s: still being written", day.name, slot_dir.name)
                    continue
                meta = json.loads(meta_path.read_text("utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                log.warning("media %s/%s: unreadable meta.json (%s)", day.name, slot_dir.name,
                            type(exc).__name__)
                continue
            if not isinstance(meta, dict) or not isinstance(meta.get("briefing_file"), str):
                continue
            ed = ArchiveEdition(day.name, slot_dir.name, slot_dir, meta)
            for field_name, _ in MEDIA_FILE_FIELDS:
                name = meta.get(field_name)
                if not name:
                    continue
                p = _safe_media_file(slot_dir, root_real, name)
                if p is not None:
                    ed.files[name] = p
                elif field_name == "briefing_file":
                    log.warning("media %s/%s: briefing file missing or unsafe; edition skipped",
                                day.name, slot_dir.name)
            if meta["briefing_file"] in ed.files:
                out.append(ed)
    out.sort(key=lambda e: (str(e.meta.get("date") or e.date),
                            str(e.meta.get("generated_at") or "")), reverse=True)
    return out


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def media_url(key: str, sha256: str | None) -> str:
    """The Worker URL of media object `key`, versioned by its content.

    A re-render of the same date and slot (the redo-one-slide and stale-clip
    repairs) keeps every file name, so an unversioned URL would stay the same
    and a browser that cached the old video or cover would keep showing it.
    `?v=<first 12 hex of the SHA-256>` makes new bytes a new URL, as
    look_asset_url does for the look images. The Worker ignores `v` when it
    picks the object, and caches a binary only under a versioned URL."""
    url = "/" + key
    return f"{url}?v={sha256[:12]}" if isinstance(sha256, str) and _SHA256_RE.fullmatch(sha256) else url


def build_history(editions: list[ArchiveEdition], present: set[str], now: float,
                  versions: dict[str, str] | None = None) -> dict:
    """media/history.json: briefing/generate.py's rebuild_history shape, with
    /archive/ URLs rewritten to /media/ and every URL pointing at an object
    that is actually in the bucket. An edition whose EN video is not uploaded
    is left out; a clip keeps `file` only when it names an uploaded full video
    (legacy story renders, whose clips are chapters of it). `versions` maps a
    media key to the SHA-256 of the object the bucket holds (media_url)."""
    versions = versions or {}
    briefings = []
    for ed in editions:
        e = ed.clean_meta()

        def url_for(name):
            if isinstance(name, str) and name in ed.files and ed.prefix + name in present:
                return media_url(ed.prefix + name, versions.get(ed.prefix + name))
            return None

        for field_name, url_key in MEDIA_FILE_FIELDS:
            url = url_for(ed.meta.get(field_name))
            if url:
                e[url_key] = url
        if "briefing" not in e:
            continue
        for c in e.get("clips") or []:
            if not isinstance(c, dict):
                continue
            for src, dst in (("clip_file", "file"), ("clip_file_zh", "file_zh")):
                url = url_for(c.get(src))
                if url:
                    c[dst] = url
        e["local"] = False
        briefings.append(e)
    briefings.sort(key=lambda m: (str(m.get("date") or ""), str(m.get("generated_at") or "")),
                   reverse=True)
    return {"generated_at": _iso(now),
            "current": briefings[0].get("date") if briefings else None,
            "briefings": briefings}


# --------------------------------------------------------------------------
# snapshot sections
# --------------------------------------------------------------------------
def config_symbols() -> dict | None:
    """sigengine/config.json's "symbols" map, or None when the file is missing,
    unreadable, not JSON, or not shaped {"symbols": {...}}. None means "not
    known", which is not the same as "nothing enabled"."""
    cfg = web_server._load_json(web_server.CONFIG_FILE, None)
    syms = cfg.get("symbols") if isinstance(cfg, dict) else None
    return syms if isinstance(syms, dict) else None


def enabled_symbols() -> list[str]:
    """Enabled symbols from sigengine/config.json, in config order ([] when
    the config cannot be read)."""
    cfg = config_symbols() or {}
    out = []
    for sym, c in cfg.items():
        if not (isinstance(c, dict) and c.get("enabled")):
            continue
        if not _SYMBOL_RE.fullmatch(str(sym)):
            log.warning("symbol %r is not a plain ticker; left out of the snapshot", sym)
            continue
        out.append(sym)
    return out


_VERSION: str | None = None


def publisher_version() -> str:
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


def host_label() -> str:
    """A label for this box, never its hostname or address."""
    label = (os.getenv("PUBLISHER_HOST_LABEL") or "home").strip()
    return label if _LABEL_RE.fullmatch(label) else "home"


# "2026-11-26 11:30:51,651 INFO sigengine.engine: ORCL: IBKR unavailable -> yfinance fallback"
_FALLBACK_RE = re.compile(r":\s+([A-Z0-9]{1,15}(?:[/.-][A-Z0-9]{1,15})?):\s.*yfinance fallback")
ENGINE_LOG_TAIL = 40         # the window build_journal reads


def fallback_symbols(lines: list[str]) -> list[str]:
    """Symbols named on the engine's "yfinance fallback" log lines."""
    return sorted({m.group(1) for ln in lines if "yfinance fallback" in ln
                   for m in [_FALLBACK_RE.search(ln)] if m})


def configured_enabled() -> set[str] | None:
    """Every symbol sigengine/config.json has enabled, in its trading spelling
    (the keys of sigengine/state.json use the same one). None when the config
    cannot be read: then nobody knows which symbols are polled."""
    cfg = config_symbols()
    if cfg is None:
        return None
    return {str(sym) for sym, c in cfg.items() if isinstance(c, dict) and c.get("enabled")}


def build_journal_section() -> object:
    """build_journal(), with two corrections to its engine block.

    engine.yfinance_fallback holds SYMBOLS. build_journal takes the second
    whitespace field of each fallback log line, which with the engine's log
    format is the clock time, not the symbol. The old page only shows how many
    there are, so it is left alone there; the contract promises symbols, so
    they are re-read here from the same lines.

    engine.symbols[].enabled says whether the engine still polls the symbol.
    build_journal lists every symbol sigengine/state.json remembers, including
    one disabled in config months ago, whose last bar is old by design and
    must not read as "2158 h behind". When sigengine/config.json cannot be
    read, enabled is None (unknown) for every row, never False: marking them
    all "not polled" would hide real lag exactly when something is broken."""
    j = web_server.build_journal()
    eng = j.get("engine") if isinstance(j, dict) else None
    if not isinstance(eng, dict):
        return j
    eng = dict(eng)
    if "yfinance_fallback" in eng:
        lines = web_server._tail_lines(web_server.LOGS_DIR / "sigengine.log", ENGINE_LOG_TAIL)
        eng["yfinance_fallback"] = fallback_symbols(lines)
    if isinstance(eng.get("symbols"), list):
        enabled = configured_enabled()
        if enabled is None:
            log.warning("sigengine/config.json could not be read; engine symbols "
                        "marked enabled=null (unknown)")
        eng["symbols"] = [{**row, "enabled": None if enabled is None
                           else row.get("symbol") in enabled}
                          if isinstance(row, dict) else row for row in eng["symbols"]]
    return {**j, "engine": eng}


def _section(name: str, fn: Callable[[], object]) -> object:
    """Run one builder; its failure becomes that section's {"error": ...}."""
    try:
        out = fn()
    except Exception as exc:                    # noqa: BLE001 — isolate every source
        log.warning("%s builder failed: %s", name, _err(exc))
        return {"error": _err(exc)}
    if isinstance(out, dict) and isinstance(out.get("error"), str):
        out = {**out, "error": redact(out["error"])[:500]}
    return out


# --------------------------------------------------------------------------
# the publisher
# --------------------------------------------------------------------------
@dataclass
class Options:
    dry_run: bool = False
    media: bool = True
    retention_days: int = DEFAULT_RETENTION_DAYS
    force: bool = False


class Publisher:
    def __init__(self, dest, state_dir: Path, opts: Options | None = None, *,
                 clock: Callable[[], float] = time.time, out=None) -> None:
        self.dest = dest
        self.opts = opts or Options()
        self.clock = clock
        self.out = out if out is not None else sys.stdout
        self.state = StateStore(Path(state_dir) / "state.json", dest.ident,
                                readonly=self.opts.dry_run)
        self.snapshot: dict | None = None
        # This cycle's look section, built (and its image URLs versioned) by
        # the look phase so the snapshot carries exactly what was uploaded.
        self.look_section: object | None = None
        self._dry_sent: dict[str, str] = {}     # dry run: key -> digest "sent" this run
        # media key -> SHA-256 of the object the bucket holds (or, in a dry
        # run, would hold), for the versioned URLs in history.json.
        self._media_versions: dict[str, str] = {}

    # -- primitives -------------------------------------------------------
    def _dry(self, verb: str, key: str, detail: str = "") -> None:
        print(f"[dry-run] {verb} {key}{'  ' + detail if detail else ''}", file=self.out)

    def _dry_put(self, key: str, digest: str, size: int) -> bool:
        if self._dry_sent.get(key) == digest:
            return False
        self._dry_sent[key] = digest
        self._dry("PUT", key, f"{size} bytes")
        return True

    def _record(self, section: str, key: str, rec: dict | None) -> None:
        def fn(d: dict) -> None:
            sec = d.setdefault(section, {})
            if rec is None:
                sec.pop(key, None)
            else:
                sec[key] = rec
        self.state.update(fn)

    def _unchanged(self, key: str, digest: str, heartbeat: float | None) -> bool:
        prev = (self.state.dest().get("objects") or {}).get(key)
        if self.opts.force or not prev or prev.get("sha256") != digest:
            return False
        return heartbeat is None or self.clock() - float(prev.get("at") or 0) < heartbeat

    def put_json(self, key: str, payload: dict, *, volatile: tuple = (),
                 heartbeat: float | None = None, always: bool = False,
                 extra: dict | None = None) -> bool:
        """Upload `payload` unless its content (minus `volatile` fields such as
        as_of) is what was last uploaded; `always` sends it regardless.
        Returns True when it was sent."""
        check_key(key)
        digest = _digest({k: v for k, v in payload.items() if k not in volatile})
        if not always and self._unchanged(key, digest, heartbeat):
            return False
        body = _dumps(payload)
        if self.opts.dry_run:
            return self._dry_put(key, digest, len(body))
        self.dest.put_bytes(key, body, JSON_TYPE, NO_CACHE)
        self._record("objects", key, {"sha256": digest, "at": self.clock(),
                                      "size": len(body), **(extra or {})})
        return True

    def put_blob(self, key: str, data: bytes, content_type: str) -> bool:
        check_key(key)
        digest = hashlib.sha256(data).hexdigest()
        if self._unchanged(key, digest, None):
            return False
        if self.opts.dry_run:
            return self._dry_put(key, digest, len(data))
        self.dest.put_bytes(key, data, content_type, NO_CACHE)
        self._record("objects", key, {"sha256": digest, "at": self.clock(), "size": len(data)})
        return True

    # -- 1. approvals -----------------------------------------------------
    def apply_approvals(self) -> str:
        today = _et_today(self.clock())
        seen = self.state.dest().get("approvals") or {}
        counts = {"applied": 0, "noop": 0, "rejected": 0, "failed": 0}
        for obj in self.dest.list("approvals/"):
            m = _APPROVAL_KEY_RE.fullmatch(obj.key)
            if not m or not _valid_date(m.group(1)) or not _in_approval_window(m.group(1), today):
                continue        # not an approval, or too old/new to matter
            prev = seen.get(obj.key) or {}
            if prev.get("done") and obj.etag and prev.get("etag") == obj.etag:
                continue        # this exact object was handled already
            raw = self.dest.get_bytes(obj.key, APPROVAL_MAX_BYTES)
            if raw is None:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            same = prev.get("sha256") == digest
            if same and prev.get("done"):
                if obj.etag and obj.etag != prev.get("etag"):
                    self._record("approvals", obj.key, {**prev, "etag": obj.etag})
                continue
            rec = {"sha256": digest, "etag": obj.etag, "seen_at": _iso(self.clock())}
            req, why = parse_approval(raw, m.group(1), self.clock())
            if req is None:
                counts["rejected"] += 1
                log.warning("approval %s rejected: %s", obj.key, why)
                if self.opts.dry_run:
                    self._dry("REJECT", obj.key, why or "")
                else:
                    # date + requested_at (when readable) let the page match
                    # this refusal to the request it is showing as pending
                    self._record("approvals", obj.key, {
                        **rec, "date": m.group(1), "requested_at": _peek_requested_at(raw),
                        "done": True, "outcome": f"rejected: {why}"})
                continue
            if self.opts.dry_run:
                self._dry("APPLY", obj.key, f"approved={str(req.approved).lower()}")
                continue
            attempts = int(prev.get("attempts") or 0) if same else 0
            outcome, done, attempts = self._apply_approval(req, attempts)
            extra = {}
            if outcome == "applied":
                counts["applied"] += 1
                # Our own write's mtime, so a later look at this record can
                # tell this publisher's change from one made at home.
                extra["look_mtime_ns"] = _look_mtime_ns(req.date)
            elif outcome.startswith("error"):
                counts["failed"] += 1
            elif outcome.startswith("rejected"):
                counts["rejected"] += 1
            else:
                counts["noop"] += 1
            self._record("approvals", obj.key, {
                **rec, "date": req.date, "approved": req.approved,
                "requested_at": req.requested_at, "requested_by": req.requested_by,
                "outcome": outcome, "done": done, "attempts": attempts,
                "applied_at": _iso(self.clock()), **extra})
            log.info("approval %s (approved=%s, by %s): %s", req.date,
                     str(req.approved).lower(), req.requested_by, outcome)
        return ",".join(f"{k}:{v}" for k, v in counts.items() if v) or "none"

    def _apply_approval(self, req: ApprovalRequest, attempts: int) -> tuple[str, bool, int]:
        """(outcome, done, attempts). Idempotent: a request the look already
        reflects is recorded without touching the file. A request that arrives
        once its date's 20:00 ET render has started is refused for good."""
        try:
            look = web_server.build_look(req.date)
            current = look.get("approved") if isinstance(look, dict) else None
            if req.approved and current is True:
                return "already approved", True, attempts
            if not req.approved and current is not True:
                return "already not approved", True, attempts
            if self.clock() >= render_start(req.date):
                # The render has read the look already (or the home box was
                # down through it): applying now changes no video tonight and
                # would leak into a later re-run of single clips.
                verb = "sign off" if req.approved else "withdraw"
                return (f"rejected: arrived after the {RENDER_HOUR_ET}:00 ET render of "
                        f"{req.date} started; {verb} on the local dashboard if a re-run "
                        "should use it", True, attempts)
            if self._changed_at_home_after(req):
                return ("superseded: the look record changed on the home box after this "
                        "request", True, attempts)
            res = web_server.set_look_approval(req.date, req.approved)
            error = res.get("error") if isinstance(res, dict) else "unexpected result"
        except Exception as exc:                # noqa: BLE001 — record and retry later
            error = _err(exc)
        if not error:
            return "applied", True, attempts
        attempts += 1
        return f"error: {redact(error)[:200]}", attempts >= APPROVAL_MAX_ATTEMPTS, attempts

    def _changed_at_home_after(self, req: ApprovalRequest) -> bool:
        """Was the look record rewritten after this request, by something
        other than this publisher? Then a sign-off or withdrawal made at home
        (on :8787), or a redraw, is newer than the request and wins.

        This matters when the request is handled a second time, e.g. after
        state.json was lost: approvals/<date>.json stays in the bucket, and
        without this check an old cloud request would undo a later local
        change. The publisher's own writes are recognised by the mtime it
        recorded when it applied them."""
        mtime_ns = _look_mtime_ns(req.date)
        if mtime_ns is None:
            return False
        try:
            requested_ns = int(datetime.fromisoformat(req.requested_at).timestamp() * 1e9)
        except ValueError:
            return False
        if mtime_ns <= requested_ns:
            return False
        ours = {rec.get("look_mtime_ns") for rec in (self.state.dest().get("approvals") or {}).values()
                if isinstance(rec, dict) and rec.get("date") == req.date}
        return mtime_ns not in ours

    def approval_report(self, look_date: object) -> dict | None:
        """snapshot.publisher.approval: what happened to the newest request
        for the look's date, so the page can tell waiting from refused."""
        if not isinstance(look_date, str) or not _valid_date(look_date):
            return None
        rec = (self.state.dest().get("approvals") or {}).get(f"approvals/{look_date}.json")
        if not isinstance(rec, dict) or not rec.get("outcome"):
            return None
        approved = rec.get("approved")
        return {"date": look_date,
                "approved": approved if isinstance(approved, bool) else None,
                "requested_at": rec.get("requested_at") if isinstance(rec.get("requested_at"), str) else None,
                "outcome": redact(rec["outcome"])[:300],
                "done": bool(rec.get("done")),
                "at": rec.get("applied_at") or rec.get("seen_at")}

    # -- 2. look images ---------------------------------------------------
    def build_look_section(self) -> object:
        """build_look(<US/Eastern session date>), or that failure as {"error"}."""
        session = _section("session date", web_server._session_date)
        if not isinstance(session, str):
            return session
        return _section("look", lambda: web_server.build_look(session))

    def publish_look_assets(self) -> str:
        """Build the session's look and upload its plate + keyframe BEFORE the
        snapshot that points at them, so a page never asks for an image the
        bucket does not hold yet. Each URL in the look then carries the
        uploaded image's content version (look_asset_url). An image that was
        not uploaded keeps the builder's plain URL; once it is uploaded, the
        URL gains a version, and a page that failed to load it tries again."""
        look = self.build_look_section()
        if isinstance(look, dict):
            look = dict(look)
        self.look_section = look
        if not isinstance(look, dict) or not look.get("exists") or not _valid_date(look.get("date")):
            return "none"
        d, sent = look["date"], 0
        for kind in ("plate", "keyframe"):
            path = web_server._look_asset(d, kind)
            if path is None:
                continue
            if path.stat().st_size > LOOK_ASSET_MAX_BYTES:
                log.warning("look %s %s is larger than %d bytes; not uploaded", d, kind,
                            LOOK_ASSET_MAX_BYTES)
                continue
            data = path.read_bytes()
            ctype = _image_type(data)
            if ctype is None:
                log.warning("look %s %s is not a PNG/JPEG/WebP image; not uploaded", d, kind)
                continue
            sent += self.put_blob(f"look/{d}/{kind}.png", data, ctype)
            look[f"{kind}_url"] = look_asset_url(d, kind, data)
        return f"{sent} put" if sent else "same"

    # -- 3. snapshot ------------------------------------------------------
    def build_snapshot(self, look: object | None = None) -> dict:
        if look is None:
            look = self.build_look_section()
        symbols = _section("symbols", enabled_symbols)
        look_date = look.get("date") if isinstance(look, dict) else None
        return {
            "schema": SCHEMA,
            "as_of": _iso(self.clock()),
            "publisher": {"version": publisher_version(), "host": host_label(),
                          "approval": self.approval_report(look_date)},
            "portfolio": _section("portfolio", web_server.build_portfolio),
            "board": _section("board", web_server.build_board),
            "journal": _section("journal", build_journal_section),
            # Through the server's cache so the chart builds that follow reuse
            # this one broker round trip for their fill markers.
            "executions": _section("executions", lambda: web_server._CACHE.get(
                "executions", web_server.PORTFOLIO_TTL, web_server.build_executions)),
            "symbols": symbols if isinstance(symbols, list) else [],
            "look": look,
            # The put sleeve, which build_portfolio leaves out (read-only:
            # web/options_view.py). None when no sleeve is configured.
            "options": _section("options", options_view.build_options),
        }

    def publish_snapshot(self) -> str:
        """Sent on every run, changed or not: `as_of` is the heartbeat."""
        self.snapshot = self.build_snapshot(self.look_section)
        self.put_json(SNAPSHOT_KEY, self.snapshot, volatile=("as_of",), always=True)
        return "put"

    # -- 4. charts --------------------------------------------------------
    def build_chart_payload(self, symbol: str, as_of: str) -> dict:
        envelope = {"schema": SCHEMA, "as_of": as_of, "time_basis": "utc"}
        try:
            # chart_view prints a line per symbol; keep the cron log to ours.
            with true_utc_chart_time(), contextlib.redirect_stdout(io.StringIO()):
                data = web_server.build_chart(symbol, CHART_BARS)
        except Exception as exc:                # noqa: BLE001
            return {**envelope, "error": _err(exc)}
        if not isinstance(data, dict):
            return {**envelope, "error": "chart builder returned nothing"}
        if data.get("error") or not data.get("symbols"):
            return {**envelope, "error": redact(data.get("error") or "no chart data")[:500]}
        return {**envelope, "symbols": data["symbols"]}

    def publish_charts(self) -> str:
        now = self.clock()
        last = float(self.state.dest().get("charts_built_at") or 0)
        if not self.opts.force and now - last < CHART_REBUILD_SEC - CRON_JITTER_SEC:
            return "not due"
        # Stamped first: a build that crashes must not be retried every minute.
        self.state.update(lambda d: d.__setitem__("charts_built_at", now))
        symbols = (self.snapshot or {}).get("symbols") or enabled_symbols()
        as_of, sent, kept = _iso(now), 0, 0
        for sym in symbols:
            key = f"chart/{chart_key(sym)}.json"
            payload = self.build_chart_payload(sym, as_of)
            ok = "error" not in payload
            if not ok:
                prev = (self.state.dest().get("objects") or {}).get(key) or {}
                if prev.get("ok"):
                    kept += 1
                    log.warning("chart %s: %s (keeping the last good chart)", sym, payload["error"])
                    continue
            sent += self.put_json(key, payload, volatile=("as_of",),
                                  heartbeat=CHART_HEARTBEAT_SEC, extra={"ok": ok})
        return f"{len(symbols)} built, {sent} put" + (f", {kept} kept" if kept else "")

    def core_cycle(self) -> bool:
        """approvals -> look -> snapshot -> charts. Each phase fails alone."""
        started = time.monotonic()
        self.state.refresh()
        self.look_section = None        # never reuse the last cycle's look
        ok, parts = True, []
        for name, fn in (("approvals", self.apply_approvals),
                         ("look", self.publish_look_assets),
                         ("snapshot", self.publish_snapshot),
                         ("charts", self.publish_charts)):
            try:
                parts.append(f"{name}={fn()}")
            except Exception as exc:            # noqa: BLE001 — never block the others
                ok = False
                parts.append(f"{name}=FAILED")
                log.error("%s failed: %s", name, _err(exc))
        log.info("%s%s (%.1fs)", "[dry-run] " if self.opts.dry_run else "",
                 " ".join(parts), time.monotonic() - started)
        return ok

    # -- 5. media ---------------------------------------------------------
    def _sync_media_file(self, key: str, path: Path, present: set[str]) -> str:
        """'put' | 'same' | 'settling'. Unchanged files are recognised by
        size + mtime without being read; a changed stat is hashed before
        anything is re-sent."""
        check_key(key)
        st = path.stat()
        prev = (self.state.dest().get("media") or {}).get(key)
        if prev and not self.opts.force and (prev.get("size"), prev.get("mtime_ns")) == (
                st.st_size, st.st_mtime_ns):
            return "same"
        if self.clock() - st.st_mtime < MEDIA_SETTLE_SEC:
            return "settling"
        digest = _sha256_file(path)
        rec = {"sha256": digest, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
               "at": self.clock()}
        if prev and not self.opts.force and prev.get("sha256") == digest:
            self._record("media", key, {**prev, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
            return "same"
        if self.opts.dry_run:
            self._dry("PUT", key, f"{st.st_size} bytes")
            present.add(key)
            self._media_versions[key] = digest
            return "put"
        self.dest.put_file(key, path, MEDIA_TYPES[path.suffix.lower()], MEDIA_CACHE)
        self._record("media", key, rec)
        present.add(key)
        self._media_versions[key] = digest
        return "put"

    def media_cycle(self) -> bool:
        started = time.monotonic()
        self.state.refresh()
        now = self.clock()
        cutoff = _et_today(now) - timedelta(days=self.opts.retention_days)
        editions = scan_archive(Path(web_server.MEDIA_ARCHIVE_DIR), cutoff, now)
        # Everything the bucket already holds counts as present from the start,
        # so a history.json written mid-run never drops an older edition.
        known = self.state.dest().get("media") or {}
        present = {ed.prefix + name for ed in editions for name in ed.files
                   if ed.prefix + name in known}
        # What the bucket holds now. A file still settling keeps the version
        # of the copy already uploaded, which is the one its URL serves.
        self._media_versions = {k: r["sha256"] for k, r in known.items()
                                if isinstance(r, dict) and isinstance(r.get("sha256"), str)}
        ok, sent, failed = True, 0, 0
        for ed in editions:
            changed = False
            for name, path in ed.files.items():
                try:
                    if self._sync_media_file(ed.prefix + name, path, present) == "put":
                        sent += 1
                        changed = True
                except Exception as exc:        # noqa: BLE001 — next file, next run
                    ok, failed = False, failed + 1
                    log.error("media %s%s failed: %s", ed.prefix, name, _err(exc))
            if ed.prefix + ed.meta["briefing_file"] not in present:
                continue
            try:
                changed |= self.put_json(ed.prefix + "meta.json", ed.clean_meta())
                if changed:     # newest first: a fresh recap shows up before a backfill ends
                    self.put_json(HISTORY_KEY, build_history(editions, present, self.clock(),
                                                             self._media_versions),
                                  volatile=("generated_at",))
            except Exception as exc:            # noqa: BLE001
                ok = False
                log.error("media %smeta.json failed: %s", ed.prefix, _err(exc))
        history = "FAILED"
        try:
            history = "put" if self.put_json(
                HISTORY_KEY, build_history(editions, present, self.clock(), self._media_versions),
                volatile=("generated_at",), heartbeat=HISTORY_HEARTBEAT_SEC) else "same"
        except Exception as exc:                # noqa: BLE001
            ok = False
            log.error("history.json failed: %s", _err(exc))
        pruned = "FAILED"
        try:
            pruned = self.prune(cutoff)
        except Exception as exc:                # noqa: BLE001
            ok = False
            log.error("retention pruning failed: %s", _err(exc))
        if sent or failed or history != "same" or pruned not in ("0", "not due"):
            log.info("%smedia editions=%d put=%d failed=%d history=%s pruned=%s (%.1fs)",
                     "[dry-run] " if self.opts.dry_run else "", len(editions), sent, failed,
                     history, pruned, time.monotonic() - started)
        return ok

    def prune(self, cutoff: Date) -> str:
        """Delete media/<date>/ and look/<date>/ objects dated before `cutoff`.
        Never touches media/history.json, snapshot/, chart/ or approvals/."""
        now = self.clock()
        last = float(self.state.dest().get("pruned_at") or 0)
        if not self.opts.force and now - last < PRUNE_EVERY_SEC:
            return "not due"
        limit = cutoff.isoformat()
        stale: list[ObjectInfo] = []
        for prefix in ("media/", "look/"):
            for obj in self.dest.list(prefix):
                m = _DATED_KEY_RE.match(obj.key)
                if m and m.group(1) < limit:
                    stale.append(obj)
        keys = [o.key for o in stale]
        if self.opts.dry_run:
            for k in keys:
                self._dry("DELETE", k)
            return str(len(keys))
        if keys:
            self.dest.delete(keys)

        def forget(d: dict) -> None:
            for section in ("media", "objects"):
                sec = d.get(section) or {}
                for k in keys:
                    sec.pop(k, None)
            d["pruned_at"] = now
        self.state.update(forget)
        return str(len(keys))


# --------------------------------------------------------------------------
# running it
# --------------------------------------------------------------------------
class NullDestination:
    """Dry-run stand-in when R2 is not configured: lists nothing, stores nothing."""

    ident = "r2:unconfigured"

    def describe(self) -> str:
        return "R2 (not configured; dry run compares against no remote state)"

    def list(self, prefix: str) -> list[ObjectInfo]:
        return []

    def get_bytes(self, key: str, max_bytes: int) -> bytes | None:
        return None

    def put_bytes(self, *a, **k) -> None:   # pragma: no cover — dry run never writes
        raise RuntimeError("dry run")

    put_file = put_bytes
    delete = put_bytes


def _lock_paths(state_dir: Path, dest) -> tuple[Path, Path]:
    if dest.ident.startswith("r2:"):
        return state_dir / "publish.lock", state_dir / "media.lock"
    tag = hashlib.sha1(dest.ident.encode()).hexdigest()[:10]
    return state_dir / f"publish-dir-{tag}.lock", state_dir / f"media-dir-{tag}.lock"


def _check_out_dir(path: Path) -> Path:
    out = path.expanduser().resolve()
    refused = {Path("/"), ROOT.resolve(), Path(DATA_DIR).resolve(),
               Path(web_server.MEDIA_ARCHIVE_DIR).resolve()}
    if out in refused:
        raise ConfigError(f"refusing --out-dir {out}: pick an empty folder of its own")
    return out


def make_destination(out_dir: str | None, dry_run: bool):
    if out_dir:
        return DirDestination(_check_out_dir(Path(out_dir)))
    try:
        return R2Destination.from_config(R2Config.from_env())
    except ConfigError as exc:
        if not dry_run:
            raise
        log.warning("%s", exc)
        return NullDestination()


def run_once(pub: Publisher, state_dir: Path) -> int:
    """One cycle under the run lock. A second run while one is in progress
    exits at once (status 0): the next minute's cron run catches up."""
    if pub.opts.dry_run:                        # reads only; no lock, no state writes
        ok = pub.core_cycle()
        if pub.opts.media:
            ok &= pub.media_cycle()
        return 0 if ok else 1
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    core_lock, media_lock = _lock_paths(state_dir, pub.dest)
    with try_lock(core_lock) as got:
        if not got:
            log.info("another publisher run holds the lock; exiting")
            return 0
        ok = pub.core_cycle()
    if pub.opts.media:
        with try_lock(media_lock) as got:
            if got:
                ok &= pub.media_cycle()
            else:
                log.debug("media sync already running in another process")
    return 0 if ok else 1


def run_loop(pub: Publisher, state_dir: Path, interval: float,
             stop: threading.Event) -> int:
    """--loop: one core cycle per `interval` on this thread, media on a
    background thread so a long upload never delays the snapshot."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    core_lock, media_lock = _lock_paths(state_dir, pub.dest)

    def media() -> None:
        with try_lock(media_lock) as got:
            if got:
                try:
                    pub.media_cycle()
                except Exception as exc:        # noqa: BLE001
                    log.error("media cycle failed: %s", _err(exc))

    worker: threading.Thread | None = None
    while not stop.is_set():
        started = time.monotonic()
        with try_lock(core_lock) as got:
            if got:
                pub.core_cycle()
            else:
                log.info("another publisher run holds the lock; skipping this tick")
        if pub.opts.media and (worker is None or not worker.is_alive()):
            worker = threading.Thread(target=media, name="publisher-media", daemon=True)
            worker.start()
        stop.wait(max(1.0, interval - (time.monotonic() - started)))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m web.publisher",
        description="Publish the dashboard's read-only data to Cloudflare R2 "
                    "(or a local folder) for the cloud dashboard.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="run one cycle and exit (the default; for a 1-minute cron)")
    mode.add_argument("--loop", type=float, metavar="SECONDS",
                      help="keep running, one cycle every SECONDS (at least 15)")
    ap.add_argument("--out-dir", metavar="PATH",
                    help="write the exact bucket layout into PATH instead of R2 "
                         "(no boto3, no credentials); approvals are read from PATH/approvals/")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be uploaded, applied or deleted; change nothing")
    ap.add_argument("--media", action=argparse.BooleanOptionalAction, default=None,
                    help="sync the recap archive (default: on for R2, off with --out-dir, "
                         "since it copies every video within the retention window)")
    ap.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, metavar="N",
                    help=f"keep N days of recaps in the bucket (default {DEFAULT_RETENTION_DAYS})")
    ap.add_argument("--force", action="store_true",
                    help="ignore content hashes and the chart timer; re-send everything")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    if args.loop is not None and args.loop < 15:
        ap.error("--loop must be at least 15 seconds")
    if not 1 <= args.retention_days <= 3650:
        ap.error("--retention-days must be between 1 and 3650")
    if args.media is None:
        args.media = not args.out_dir
    return args


def main(argv: list[str] | None = None, *, clock: Callable[[], float] = time.time) -> int:
    args = parse_args(argv)
    if args.verbose:
        log.setLevel(logging.DEBUG)
    try:
        dest = make_destination(args.out_dir, args.dry_run)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    opts = Options(dry_run=args.dry_run, media=args.media,
                   retention_days=args.retention_days, force=args.force)
    state_dir = Path(STATE_DIR)
    pub = Publisher(dest, state_dir, opts, clock=clock)
    log.debug("publishing to %s", dest.describe())
    if args.loop is None:
        return run_once(pub, state_dir)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    return run_loop(pub, state_dir, args.loop, stop)


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s",
                                           "%Y-%m-%dT%H:%M:%S%z"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    log.setLevel(logging.INFO)
    for noisy in ("botocore", "boto3", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    _setup_logging()
    sys.exit(main())

"""Smaller fixes from the 2026-08-19 audit that have no natural home elsewhere.

* the dashboard token was written in cleartext to an unrotated, world-readable
  log on every authenticated request;
* the "engine live" badge reported process PRESENCE, so it stayed green through
  a wedged fetch phase — the one moment someone checks it;
* append_event let a caller overwrite the journal's wall-clock timestamp, which
  sigengine.engine's signal_skip did with a bar timestamp;
* delete_bars, the only way to retire a cache that a split has invalidated.

    .venv/bin/pytest -q tests/test_audit_hardening.py
"""
from __future__ import annotations

import json
import os
import time

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pandas as pd  # noqa: E402

import web.server as web  # noqa: E402
from relay.bars_db import bar_count, delete_bars, read_bars, upsert_bars  # noqa: E402
from relay.logger import append_event  # noqa: E402


# ------------------------------------------------------------ token redaction
def test_the_token_never_reaches_a_log_line():
    line = '"GET /api/board?token=s3cr3t-abc_123 HTTP/1.1" 200 -'
    out = web._redact_token(line)
    assert "s3cr3t-abc_123" not in out
    assert "<redacted>" in out


def test_a_token_among_other_params_is_still_redacted():
    line = '"GET /api/chart?symbol=TSLA&token=s3cr3t&bars=200 HTTP/1.1" 200 -'
    out = web._redact_token(line)
    assert "s3cr3t" not in out
    assert "symbol=TSLA" in out and "bars=200" in out


def test_lines_without_a_token_are_untouched():
    line = '"GET /api/board HTTP/1.1" 401 -'
    assert web._redact_token(line) == line


# ------------------------------------------------------------- engine liveness
def test_a_fresh_state_file_means_the_engine_is_polling(monkeypatch, tmp_path):
    f = tmp_path / "state.json"
    f.write_text("{}")
    monkeypatch.setattr(web, "SIGENGINE_STATE_FILE", f)
    assert web._engine_polling() is True


def test_a_stale_state_file_means_it_has_stopped_working(monkeypatch, tmp_path):
    """The wedged-but-alive case: the process is there, the polls are not."""
    f = tmp_path / "state.json"
    f.write_text("{}")
    old = time.time() - (web._POLL_STALE_AFTER_SEC + 60)
    os.utime(f, (old, old))
    monkeypatch.setattr(web, "SIGENGINE_STATE_FILE", f)
    assert web._engine_polling() is False


def test_a_missing_state_file_is_not_polling(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "SIGENGINE_STATE_FILE", tmp_path / "nope.json")
    assert web._engine_polling() is False


# ------------------------------------------------------------- journal stamping
def test_a_caller_cannot_overwrite_the_journal_timestamp(tmp_path):
    """signal_skip passed the strategy event's BAR timestamp as "ts", which
    replaced the write time — leaving the journal out of chronological order."""
    path = tmp_path / "trades.jsonl"
    before = time.time()
    append_event(path, {"event": "signal_skip", "ticker": "AAPL",
                        "ts": "2026-08-03 22:00:00+00:00"})
    row = json.loads(path.read_text().strip())
    assert isinstance(row["ts"], float)
    assert row["ts"] >= before


def test_ordinary_events_still_get_a_timestamp(tmp_path):
    path = tmp_path / "trades.jsonl"
    append_event(path, {"event": "order_ok", "ticker": "AAPL"})
    row = json.loads(path.read_text().strip())
    assert isinstance(row["ts"], float) and row["ticker"] == "AAPL"


# ------------------------------------------------------------------ delete_bars
def _frame(closes, start="2026-08-01 10:00"):
    idx = pd.date_range(start, periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": [1.0] * len(closes)}, index=idx)


def test_delete_bars_clears_one_symbol_and_interval_only(monkeypatch, tmp_path):
    import relay.bars_db as db
    # relay.bars_db opens BARS_DB_FILE (imported by value from relay.config).
    # This used to patch a non-existent DB_PATH with raising=False, so the test
    # silently wrote into the LIVE bars.sqlite; raising=True keeps a future
    # rename from failing open the same way.
    monkeypatch.setattr(db, "_CONN", None)
    monkeypatch.setattr(db, "BARS_DB_FILE", tmp_path / "bars.sqlite")

    upsert_bars("ZZZA", "1h", _frame([1.0, 2.0, 3.0]), "ibkr")
    upsert_bars("ZZZB", "1h", _frame([1.0, 2.0, 3.0]), "ibkr")
    assert bar_count("ZZZA", "1h") == 3

    removed = delete_bars("ZZZA", "1h")
    assert removed == 3
    assert bar_count("ZZZA", "1h") == 0
    assert bar_count("ZZZB", "1h") == 3      # the neighbour is untouched
    assert read_bars("ZZZA", "1h").empty

    delete_bars("ZZZB", "1h")

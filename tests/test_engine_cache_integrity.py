"""Bar-cache and state-file integrity — the 2026-08-19 audit's data findings.

Three separate ways the engine could end up trading on data it knew was wrong:

* a vendor SPLIT re-states history, but a warm cache only ever tops up the last
  10-15 days while the strategies replay the whole series, so everything older
  keeps pre-split prices forever;
* a failed cache WRITE was logged as "proceeding on fetched df" while the next
  line actually re-read the un-updated cache;
* state.json was written non-atomically, so a torn write silently reads back as
  "no state" and every pending signal is dropped.

    .venv/bin/pytest -q tests/test_engine_cache_integrity.py
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import sigengine.engine as eng  # noqa: E402


@pytest.fixture(autouse=True)
def _never_touch_the_live_journal(monkeypatch, tmp_path):
    """Every test in this file gets a throwaway journal.

    Not paranoia — a live leak. `load_state` gained a `state_lost` journal row
    on 2026-08-27 (audit S5, so that losing the state file stops being silent),
    and that instantly turned the previously-harmless torn-state test below into
    one that appended to the REAL /mnt/sandisk/alpaca/journal/trades.jsonl: 17
    rows landed there, each naming a pytest tmp dir, before it was noticed. The
    weekly journal review reads that file and the dashboard charts from it.

    Autouse and file-wide on purpose: the hazard is that a test which does not
    journal TODAY starts journaling when production code grows a new
    append_event, and nothing about the test itself changes to warn you.
    """
    monkeypatch.setattr(eng, "JOURNAL_FILE", tmp_path / "trades.jsonl")


def _frame(closes, start="2026-08-01 10:00", freq="1h"):
    idx = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "volume": [100.0] * len(closes)},
        index=idx,
    )


# ------------------------------------------------------- split re-adjustment
class _Cache:
    """Stands in for relay.bars_db for one symbol/interval."""

    def __init__(self, df):
        self.df = df
        self.deleted = 0

    def read(self, *_a, **_k):
        return self.df

    def delete(self, *_a, **_k):
        n = len(self.df)
        self.df = self.df.iloc[0:0]
        self.deleted += n
        return n


def test_a_split_is_detected_from_the_overlap(monkeypatch):
    """A 10:1 split: the vendor now reports a tenth of the cached price for the
    SAME timestamps. That means every cached bar older than the overlap is on a
    dead price scale."""
    cached = _frame([1000.0] * 10)
    monkeypatch.setattr(eng, "read_bars", _Cache(cached).read)
    incoming = _frame([100.0] * 10)
    ratio = eng._cache_needs_readjustment("NVDA", "1h", incoming)
    assert ratio is not None
    assert ratio == pytest.approx(0.1, rel=1e-6)


def test_matching_prices_are_left_alone(monkeypatch):
    cached = _frame([100.0] * 10)
    monkeypatch.setattr(eng, "read_bars", _Cache(cached).read)
    assert eng._cache_needs_readjustment("NVDA", "1h", _frame([100.0] * 10)) is None


def test_one_corrected_print_does_not_condemn_the_cache(monkeypatch):
    """A vendor fixing a single bad bar is routine and upsert_bars already
    handles it — it must not trigger a full re-download."""
    cached = _frame([100.0] * 10)
    monkeypatch.setattr(eng, "read_bars", _Cache(cached).read)
    incoming = _frame([100.0] * 10)
    incoming.iloc[4, incoming.columns.get_loc("close")] = 130.0
    assert eng._cache_needs_readjustment("NVDA", "1h", incoming) is None


def test_a_tiny_overlap_is_not_enough_to_judge(monkeypatch):
    cached = _frame([1000.0] * 2)
    monkeypatch.setattr(eng, "read_bars", _Cache(cached).read)
    assert eng._cache_needs_readjustment("NVDA", "1h", _frame([100.0] * 2)) is None


def test_an_empty_cache_is_not_a_re_adjustment(monkeypatch):
    monkeypatch.setattr(eng, "read_bars", lambda *a, **k: _frame([]).iloc[0:0])
    assert eng._cache_needs_readjustment("NVDA", "1h", _frame([100.0] * 10)) is None


def test_rounding_noise_is_tolerated(monkeypatch):
    cached = _frame([100.0] * 10)
    monkeypatch.setattr(eng, "read_bars", _Cache(cached).read)
    incoming = _frame([100.005] * 10)          # 0.005% — far inside tolerance
    assert eng._cache_needs_readjustment("NVDA", "1h", incoming) is None


def test_a_reverse_split_is_detected_too(monkeypatch):
    """1:10 reverse split — the ratio goes the other way, still a dead scale."""
    cached = _frame([10.0] * 10)
    monkeypatch.setattr(eng, "read_bars", _Cache(cached).read)
    ratio = eng._cache_needs_readjustment("NVDA", "1h", _frame([100.0] * 10))
    assert ratio == pytest.approx(10.0, rel=1e-6)


def test_a_failed_check_never_breaks_the_poll(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sqlite is unhappy")

    monkeypatch.setattr(eng, "read_bars", boom)
    assert eng._cache_needs_readjustment("NVDA", "1h", _frame([100.0] * 10)) is None


# --------------------------------------------------- atomic state persistence
def test_state_is_written_atomically_and_reads_back(monkeypatch, tmp_path):
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "state.json")
    eng.save_state({"AAPL": "2026-08-18T22:00:00+00:00"})
    assert json.loads((tmp_path / "state.json").read_text())["AAPL"].startswith("2026-08-18")


def test_a_failed_write_leaves_the_previous_state_intact(monkeypatch, tmp_path):
    """The point of temp+rename: the real file is never truncated, so a failure
    mid-write cannot turn it into the 0-byte file that load_state reads as {}."""
    path = tmp_path / "state.json"
    monkeypatch.setattr(eng, "STATE_FILE", path)
    eng.save_state({"AAPL": "first"})

    real_replace = eng.os.replace

    def failing_replace(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(eng.os, "replace", failing_replace)
    with pytest.raises(OSError):
        eng.save_state({"AAPL": "second"})
    monkeypatch.setattr(eng.os, "replace", real_replace)

    assert json.loads(path.read_text()) == {"AAPL": "first"}
    assert eng.load_state() == {"AAPL": "first"}


def test_a_failed_write_leaves_no_temp_file_behind(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    monkeypatch.setattr(eng, "STATE_FILE", path)
    monkeypatch.setattr(eng.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        eng.save_state({"AAPL": "x"})
    assert [p.name for p in tmp_path.iterdir() if ".tmp" in p.name] == []


def test_a_torn_state_file_still_degrades_to_empty(monkeypatch, tmp_path):
    """load_state's existing behavior is unchanged — atomicity is what stops the
    file getting into this shape in the first place."""
    path = tmp_path / "state.json"
    monkeypatch.setattr(eng, "STATE_FILE", path)
    path.write_text('{"AAPL": "2026-08')       # truncated mid-write
    assert eng.load_state() == {}

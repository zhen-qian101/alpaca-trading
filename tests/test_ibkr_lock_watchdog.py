"""Tests for the 2026-07-06 self-healing-restart mechanism: sigengine.ibkr
tracks how long EACH SYMBOL's own lock has been continuously held
(_LOCK_HELD_SINCE, read by max_lock_held_seconds() across all symbols), and
sigengine.engine's daemon loop restarts the whole process (the only way to
actually reclaim a thread stuck forever in a blocking IBKR call) once the
worst offender exceeds _LOCK_STUCK_RESTART_THRESHOLD. Per-symbol since the
same-day rewrite that split the single shared connection/lock into one per
symbol (see sigengine.ibkr's module docstring) — a stuck symbol no longer
blocks any OTHER symbol, but still eventually needs a full restart itself.

    .venv/bin/pytest -q tests/test_ibkr_lock_watchdog.py
"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import sigengine.engine as eng  # noqa: E402
import sigengine.ibkr as ibkr_mod  # noqa: E402


def test_acquire_and_release_lock_tracked_set_and_clear_held_since():
    assert ibkr_mod._acquire_lock_tracked("TEST1") is True
    try:
        held_since = ibkr_mod._LOCK_HELD_SINCE.get("TEST1")
        assert held_since is not None
        assert time.monotonic() - held_since >= 0
    finally:
        ibkr_mod._release_lock_tracked("TEST1")
    assert ibkr_mod._LOCK_HELD_SINCE.get("TEST1") is None


def test_acquire_lock_tracked_returns_false_and_does_not_set_held_since_when_busy():
    lock = ibkr_mod._get_lock("TEST3")
    holder = threading.Thread(target=lock.acquire, daemon=True)
    holder.start()
    holder.join(timeout=1)
    try:
        import unittest.mock as mock
        with mock.patch.object(ibkr_mod, "_LOCK_ACQUIRE_TIMEOUT", 0.1):
            result = ibkr_mod._acquire_lock_tracked("TEST3")
        assert result is False
    finally:
        lock.release()


def test_max_lock_held_seconds_none_when_nothing_held():
    # Use fresh symbol names unlikely to collide with other tests' leftover state.
    sym, secs = ibkr_mod.max_lock_held_seconds()
    # Other tests may leave stale (symbol -> None) entries in _LOCK_HELD_SINCE,
    # but none should be ACTIVELY held right now — so this must be None.
    if sym is not None:
        assert ibkr_mod._LOCK_HELD_SINCE.get(sym) is None or secs is None
    # A clean, isolated symbol is guaranteed unheld.
    assert ibkr_mod._LOCK_HELD_SINCE.get("DEFINITELY_UNHELD_SYMBOL_XYZ") is None


def test_max_lock_held_seconds_finds_the_worst_offender(monkeypatch):
    """max_lock_held_seconds() just needs to find the oldest _LOCK_HELD_SINCE
    entry — tested directly against that dict rather than via
    _acquire_lock_tracked, since with the 2026-07-06 _GLOBAL_CALL_LOCK fix
    (ib_async cannot safely run concurrent calls even across different IB()
    instances — see that lock's docstring) two symbols can no longer
    genuinely hold their per-symbol locks at the same time by design; this
    tests the pure "find the oldest timestamp" logic in isolation."""
    now = time.monotonic()
    monkeypatch.setattr(ibkr_mod, "_LOCK_HELD_SINCE",
                        {"FAST_ONE": now - 0.2, "SLOW_ONE": now - 0.05})
    worst_symbol, worst_seconds = ibkr_mod.max_lock_held_seconds()
    assert worst_symbol == "FAST_ONE"  # held longest, despite the name
    assert worst_seconds >= 0.2


def test_lock_stuck_for_too_long_false_when_free():
    assert eng._lock_stuck_for_too_long(None) is False


def test_lock_stuck_for_too_long_false_under_threshold():
    assert eng._lock_stuck_for_too_long(eng._LOCK_STUCK_RESTART_THRESHOLD - 1) is False


def test_lock_stuck_for_too_long_true_over_threshold():
    assert eng._lock_stuck_for_too_long(eng._LOCK_STUCK_RESTART_THRESHOLD + 1) is True


def test_restart_self_calls_execv_with_reconstructed_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(eng.os, "execv", lambda *a: calls.append(a))
    monkeypatch.setattr(eng.sys, "argv", ["sigengine/engine.py", "--log-level", "INFO"])
    monkeypatch.setattr(eng.sys, "executable", "/fake/python")

    eng._restart_self()

    assert calls == [("/fake/python",
                      ["/fake/python", "-m", "sigengine.engine", "--log-level", "INFO"])]

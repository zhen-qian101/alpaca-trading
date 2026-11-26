"""Regression test for the ACTUAL root cause found 2026-07-06 behind BTC/USD's
IBKR fetch hanging on nearly every live poll cycle: sigengine.ibkr's lock was
a plain `with _LOCK:` (no timeout). ib.qualifyContracts() hung with no
exception INSIDE that lock; the engine's own outer timeout
(sigengine.engine._ibkr_fetch_with_timeout) abandoned the CALLING thread's
wait, but the underlying thread kept running forever (Python cannot force-
kill a thread) and never released the lock. Every SUBSEQUENT poll cycle's
fresh attempt then blocked forever just trying to ACQUIRE that lock — before
even reaching connect() or logging anything.

fetch_bars uses a bounded `lock.acquire(timeout=...)` so a
previous call stuck holding the lock forever can only ever delay a cycle by
_LOCK_ACQUIRE_TIMEOUT, never block it forever.

IMPORTANT correction (same day, later): the per-symbol rewrite (see
sigengine.ibkr's module docstring) initially assumed a stuck lock for one
symbol couldn't affect any OTHER symbol at all. That's only true for
CONNECTION STATE (suspect flags, reconnects) — it turned out ib_async cannot
safely run concurrent calls even across DIFFERENT IB() instances (confirmed
empirically: 2 separate connections both hung when driven concurrently from
2 threads). _GLOBAL_CALL_LOCK now serializes the actual ib_async calls
process-wide, so a genuinely in-flight stuck call for one symbol CAN delay
(bounded to _LOCK_ACQUIRE_TIMEOUT, never forever) another symbol's turn —
that's an intentional, necessary trade-off for reliability, not a bug.

    .venv/bin/pytest -q tests/test_ibkr_lock_timeout.py
"""
from __future__ import annotations

import threading
import time

import sigengine.ibkr as ibkr_mod


def test_fetch_bars_returns_none_promptly_when_lock_already_held(monkeypatch):
    monkeypatch.setattr(ibkr_mod, "_LOCK_ACQUIRE_TIMEOUT", 0.2)

    # Simulate a previous call stuck forever holding BTC/USD's own lock
    # (never released, exactly like the real incident) by acquiring it from
    # another thread and simply never releasing it for the duration of this test.
    lock = ibkr_mod._get_lock("BTC/USD")
    holder = threading.Thread(target=lock.acquire, daemon=True)
    holder.start()
    holder.join(timeout=1)  # give it a moment to actually acquire

    start = time.monotonic()
    result = ibkr_mod.fetch_bars("BTC/USD", "1h")
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 0.5, f"fetch_bars blocked far longer than _LOCK_ACQUIRE_TIMEOUT ({elapsed:.2f}s)"

    # Clean up: release so other tests aren't affected.
    lock.release()


def test_marking_one_symbol_suspect_does_not_affect_a_different_symbols_connection(monkeypatch):
    """Per-symbol CONNECTION STATE isolation still holds even after the
    _GLOBAL_CALL_LOCK correction: MSFT being flagged suspect must not force
    AAPL's already-good connection to reconnect too."""
    monkeypatch.setattr(ibkr_mod, "_SUSPECT", {})
    ibkr_mod.mark_suspect("MSFT")

    assert ibkr_mod._SUSPECT.get("MSFT") is True
    assert ibkr_mod._SUSPECT.get("AAPL") is not True


def test_a_stuck_global_call_lock_bounds_but_does_not_forever_block_another_symbol(monkeypatch):
    """Corrected expectation (see module docstring): a call ACTUALLY in
    flight for one symbol (holding _GLOBAL_CALL_LOCK, not just its own
    per-symbol lock) legitimately delays another symbol's turn — bounded,
    never forever — because ib_async cannot safely run truly concurrent calls
    even across different IB() instances.

    2026-08-28: the global wait is no longer _LOCK_ACQUIRE_TIMEOUT. That
    constant bounds ONE symbol's stuck call; the global lock is a queue whose
    length is the watchlist, and sharing the number starved whichever symbol
    landed 8th (see tests/test_ibkr_global_lock_budget.py). The property under
    test here is unchanged — bounded, not forever — so both knobs are shrunk to
    keep the test fast rather than asserting a specific one.
    """
    monkeypatch.setattr(ibkr_mod, "_LOCK_ACQUIRE_TIMEOUT", 0.2)
    monkeypatch.setattr(ibkr_mod, "_GLOBAL_WAIT_PER_SYMBOL", 0.05)
    monkeypatch.setattr(ibkr_mod, "_GLOBAL_WAIT_CAP", 0.2)

    holder = threading.Thread(target=ibkr_mod._GLOBAL_CALL_LOCK.acquire, daemon=True)
    holder.start()
    holder.join(timeout=1)
    try:
        start = time.monotonic()
        result = ibkr_mod.fetch_bars("AAPL", "1h")
        elapsed = time.monotonic() - start

        assert result is None
        assert elapsed < 0.5, f"did not bound the wait on the global call lock ({elapsed:.2f}s)"
    finally:
        ibkr_mod._GLOBAL_CALL_LOCK.release()


def test_global_call_lock_serializes_concurrent_fetches_across_symbols(monkeypatch):
    """Direct proof _GLOBAL_CALL_LOCK does what it's for: two DIFFERENT
    symbols' fetch_bars calls, fired concurrently from two threads, must
    never actually execute their ib_async calls at the same time — confirmed
    empirically 2026-07-06 that genuinely concurrent execution (even across
    separate IB() instances) makes ib_async hang."""
    from ib_async import BarData
    import datetime as dt
    import threading as th

    in_flight = []
    max_concurrent = []
    guard = threading.Lock()

    class FakeIB:
        def isConnected(self):
            return True

        def qualifyContracts(self, contract):
            with guard:
                in_flight.append(1)
                max_concurrent.append(len(in_flight))
            time.sleep(0.05)  # give a real overlap window to any race
            with guard:
                in_flight.pop()
            return [contract]

        def reqHistoricalData(self, *a, **kw):
            base = dt.datetime(2026, 7, 2, 8, 0, tzinfo=dt.timezone.utc)
            return [BarData(date=base, open=100.0, high=101.0, low=99.0,
                            close=100.5, volume=1000.0)]

    monkeypatch.setattr(ibkr_mod, "connect", lambda symbol: FakeIB())
    import relay.bars_db as bars_db_mod
    monkeypatch.setattr(bars_db_mod, "bar_count", lambda sym, iv: 10)

    threads = [th.Thread(target=ibkr_mod.fetch_bars, args=(sym, "1h"))
              for sym in ["AAPL", "MSFT", "GOOG"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max(max_concurrent) == 1, (
        f"more than one ib_async call ran concurrently: {max_concurrent}")


def test_fetch_bars_succeeds_normally_when_lock_is_free(monkeypatch):
    """Sanity check the bounded acquire doesn't break the normal, uncontended
    path — same fixture style as test_ibkr_fetch.py's RecordingFakeIB."""
    from ib_async import BarData
    import datetime as dt

    class FakeIB:
        def isConnected(self):
            return True

        def qualifyContracts(self, contract):
            return [contract]

        def reqHistoricalData(self, *a, **kw):
            base = dt.datetime(2026, 7, 2, 8, 0, tzinfo=dt.timezone.utc)
            return [BarData(date=base, open=100.0, high=101.0, low=99.0,
                            close=100.5, volume=1000.0)]

    monkeypatch.setattr(ibkr_mod, "connect", lambda symbol: FakeIB())
    import relay.bars_db as bars_db_mod
    monkeypatch.setattr(bars_db_mod, "bar_count", lambda sym, iv: 10)

    result = ibkr_mod.fetch_bars("BTC/USD", "1h")
    assert result is not None
    assert len(result) == 1

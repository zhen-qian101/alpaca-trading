"""The global lock's wait budget must scale with the QUEUE, not stay fixed.

THE BUG. `_LOCK_ACQUIRE_TIMEOUT = 15.0` was sized for one symbol's own hung
call, which is the right number for that. It was then reused for
`_GLOBAL_CALL_LOCK`, where the wait is not "how long can one call hang" but
"how long until N symbols ahead of me finish" — a different quantity, and one
that grows every time a symbol joins the watchlist.

Measured on the live engine 2026-08-28, one poll of 10 symbols: the serialized
wave ran 13:41:05.4 -> 13:41:23.9, i.e. **18.4s**, ~2.1s per symbol. Against a
15.0s budget, the last symbol to queue waited its full 15s and gave up at
13:41:20.4 — while its slot came free at 13:41:19.2. It missed by about a
second, every cycle.

That is the whole explanation for a year-old mystery: exactly ONE symbol was
always starved while the other nine were perfect, and the victim MOVED when the
watchlist changed. SOXL got 12 IBKR fetches on 2026-08-18 against ~265 for
everything else; once SOXL left the rotation, TQQQ inherited both the 8th slot
and the failure (1 of 13, against 13 of 13 for the other nine). It was never a
connection limit — the logs carry zero connect failures — and never about a
particular symbol. It was position 8, where ThreadPoolExecutor(max_workers=8)
puts the last thread of the first wave.

Losing the race is not harmless: the loser falls back to yfinance, which is the
feed that served the corrupt bar behind the 2026-08-19 MSFT trade.
"""
from __future__ import annotations

import threading

import sigengine.ibkr as ibkr_mod


def _reset(monkeypatch, symbols):
    """A fresh lock registry holding exactly `symbols`."""
    monkeypatch.setattr(ibkr_mod, "_LOCKS", {s: threading.Lock() for s in symbols})
    monkeypatch.setattr(ibkr_mod, "_LOCKS_GUARD", threading.Lock())


def test_the_budget_covers_the_measured_ten_symbol_wave(monkeypatch):
    """THE regression, in the units that caused it: 10 symbols took 18.4s and
    the budget was 15.0s."""
    _reset(monkeypatch, [f"S{i}" for i in range(10)])

    assert ibkr_mod._global_acquire_timeout() > 18.4, (
        "the budget must exceed the measured serialized wave, or the last "
        "symbol in the queue is starved every single cycle")


def test_the_budget_grows_with_the_watchlist(monkeypatch):
    """The wait is N x one call chain, so a fixed number is wrong by
    construction — adding a symbol must widen the budget."""
    _reset(monkeypatch, ["A", "B", "C", "D", "E"])
    five = ibkr_mod._global_acquire_timeout()
    _reset(monkeypatch, [f"S{i}" for i in range(12)])
    twelve = ibkr_mod._global_acquire_timeout()

    assert twelve > five


def test_a_single_symbol_caller_is_unchanged(monkeypatch):
    """sigengine/check_ibkr.py connects one symbol. It must not inherit a
    minutes-long wait, and it must not get less than the old behaviour."""
    _reset(monkeypatch, ["AAPL"])
    assert ibkr_mod._global_acquire_timeout() == ibkr_mod._LOCK_ACQUIRE_TIMEOUT


def test_an_empty_registry_does_not_produce_a_zero_budget(monkeypatch):
    """On the very first call no locks are registered yet. A zero budget would
    make every symbol fail instantly on a cold start."""
    _reset(monkeypatch, [])
    assert ibkr_mod._global_acquire_timeout() >= ibkr_mod._LOCK_ACQUIRE_TIMEOUT


def test_the_budget_is_capped_inside_the_callers_own_bounds(monkeypatch):
    """A runaway watchlist must not let one symbol sit on the lock past the
    engine's fetch-phase timeout (90s) — the cap is what keeps this change from
    turning a starved symbol into a stalled poll."""
    _reset(monkeypatch, [f"S{i}" for i in range(200)])

    assert ibkr_mod._global_acquire_timeout() <= 60.0


def test_the_per_symbol_budget_is_left_alone(monkeypatch):
    """The per-symbol lock guards a DIFFERENT thing — this symbol's own stuck
    call — and 15s is correct for it. Widening both was never the fix."""
    assert ibkr_mod._LOCK_ACQUIRE_TIMEOUT == 15.0

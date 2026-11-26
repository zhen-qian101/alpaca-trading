"""The poll loop must survive a data-source outage.

REGRESSION for 2026-08-19 09:58 AEST, when the engine died silently in the
middle of a DNS outage and stayed dead for 94 minutes.

The chain: yfinance swallows network errors and returns an EMPTY DataFrame
rather than raising, backtest.ema_scaled_rolling signals that by raising
SystemExit (right for a CLI, fatal for a daemon), _load_with_retry re-raised it
after 4 attempts, ThreadPoolExecutor stored it on the future because its worker
catches BaseException, fut.result() re-raised it on the main thread, and NEITHER
`except Exception` guard caught it — SystemExit is a BaseException. The process
printed one bare line with no timestamp or traceback:

    no data for AMZN (yfinance 30m)

and exited. Verified against the live log: exactly one such line in 47 days and
1.7M lines, 1114 lines before the log stops dead.

    .venv/bin/pytest -q tests/test_engine_survives_data_outage.py
"""
from __future__ import annotations

import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pytest  # noqa: E402

import sigengine.engine as eng  # noqa: E402


def test_data_unavailable_is_an_ordinary_exception():
    """The whole point: `except Exception` must be able to catch it. If this
    ever becomes a BaseException again, the daemon dies on the next outage."""
    assert issubclass(eng.DataUnavailable, Exception)
    assert not issubclass(eng.DataUnavailable, SystemExit)


def test_exhausted_retries_raise_a_catchable_error_not_systemexit(monkeypatch):
    """_load_with_retry used to `raise last`, propagating the SystemExit itself."""
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise SystemExit("no data for AMZN (yfinance 30m)")

    monkeypatch.setattr(eng, "load_ohlcv", boom)
    monkeypatch.setattr(eng.time, "sleep", lambda *_: None)

    with pytest.raises(eng.DataUnavailable) as ei:
        eng._load_with_retry("AMZN", "1h", "yfinance", retries=4, delay=0)

    assert len(calls) == 4                      # still retries before giving up
    assert "AMZN" in str(ei.value)
    # the original message is preserved for the log, just not the exception TYPE
    assert "no data for AMZN" in str(ei.value)
    assert isinstance(ei.value.__cause__, SystemExit)


def test_the_failure_is_caught_by_a_plain_except_exception(monkeypatch):
    """Mirrors the two guards that let the real one through — the per-symbol
    handler and the main loop's 'never let the daemon die on a poll error'."""
    monkeypatch.setattr(eng, "load_ohlcv",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit("no data")))
    monkeypatch.setattr(eng.time, "sleep", lambda *_: None)

    survived = False
    try:
        eng._load_with_retry("AMZN", "1h", "yfinance", retries=1, delay=0)
    except Exception:            # noqa: BLE001 - exactly what the engine does
        survived = True
    assert survived, "a plain `except Exception` must catch it"


def test_a_successful_load_still_returns_normally(monkeypatch):
    monkeypatch.setattr(eng, "load_ohlcv", lambda *a, **k: "FRAME")
    assert eng._load_with_retry("AMZN", "1h", "yfinance") == "FRAME"


def test_retry_succeeds_after_a_transient_empty_frame(monkeypatch):
    """The retry exists because yfinance intermittently returns nothing for a
    symbol that is fine — that behavior must be unchanged."""
    n = {"i": 0}

    def flaky(*a, **k):
        n["i"] += 1
        if n["i"] < 3:
            raise SystemExit("no data for AMZN (yfinance 30m)")
        return "FRAME"

    monkeypatch.setattr(eng, "load_ohlcv", flaky)
    monkeypatch.setattr(eng.time, "sleep", lambda *_: None)
    assert eng._load_with_retry("AMZN", "1h", "yfinance", retries=4, delay=0) == "FRAME"
    assert n["i"] == 3

"""Tests for relay/bars_db.py's upsert precedence — which cached bar wins when
the same (symbol, interval, ts) is written again.
"""
from __future__ import annotations

import pandas as pd
import pytest

from relay import bars_db


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(bars_db, "BARS_DB_FILE", tmp_path / "test_bars.sqlite")
    yield
    bars_db.close()


def _idx(n=3):
    return pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")


# --------------------------------------------------------------------------
# upsert precedence — a forming bar must be correctable
# --------------------------------------------------------------------------
def _bar(idx, close, volume=None):
    d = {"open": [close], "high": [close], "low": [close], "close": [close]}
    if volume is not None:
        d["volume"] = [volume]
    return pd.DataFrame(d, index=idx)


def test_a_bar_cached_while_forming_is_corrected_when_the_hour_completes():
    """The engine polls every 5 minutes, so the current hour is cached while it
    is still FORMING. Same-source re-fetches used to be ignored outright, which
    froze that partial print forever: on 2026-07-31, 148 of 151 cached MSFT
    bars disagreed with IBKR's own values, one by $11.09 — and every indicator
    the engine trades on is computed from these closes."""
    idx = _idx(1)
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 451.48, 120_000), source="ibkr")
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 462.57, 7_307_678), source="ibkr")
    assert bars_db.read_bars("MSFT", "1h").close.iloc[0] == pytest.approx(462.57)


def test_a_degraded_refetch_cannot_clobber_a_more_complete_bar():
    """Volume is the completeness test: a forming bar only ever gains volume."""
    idx = _idx(1)
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 462.57, 7_307_678), source="ibkr")
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 1.0, 5), source="ibkr")
    assert bars_db.read_bars("MSFT", "1h").close.iloc[0] == pytest.approx(462.57)


def test_a_lower_ranked_source_still_cannot_win_however_complete_it_claims_to_be():
    idx = _idx(1)
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 462.57, 7_307_678), source="ibkr")
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 2.0, 99_999_999), source="yfinance")
    assert bars_db.read_bars("MSFT", "1h").close.iloc[0] == pytest.approx(462.57)


def test_a_higher_ranked_source_still_upgrades_a_cached_bar():
    """The 2026-07-14 rule this must not regress: a transient IBKR outage lets a
    yfinance bar land first, and the real IBKR bar must still be able to replace
    it — even though yfinance reports far more volume than IBKR does."""
    idx = _idx(1)
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 400.0, 99_999_999), source="yfinance")
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 462.57, 7_307_678), source="ibkr")
    assert bars_db.read_bars("MSFT", "1h").close.iloc[0] == pytest.approx(462.57)


def test_a_bar_with_no_volume_cannot_displace_one_that_has_it():
    idx = _idx(1)
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 462.57, 7_307_678), source="ibkr")
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 1.0), source="ibkr")     # no volume column
    assert bars_db.read_bars("MSFT", "1h").close.iloc[0] == pytest.approx(462.57)


def test_volume_arriving_for_a_row_that_had_none_is_accepted():
    idx = _idx(1)
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 451.48), source="ibkr")   # NULL volume
    bars_db.upsert_bars("MSFT", "1h", _bar(idx, 462.57, 7_307_678), source="ibkr")
    assert bars_db.read_bars("MSFT", "1h").close.iloc[0] == pytest.approx(462.57)

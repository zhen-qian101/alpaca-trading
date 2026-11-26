"""Signal Terminal chart times must display as Melbourne local time (AEST in
Australian winter, AEDT in Australian summer), not UTC — Lightweight Charts
formats using the JS Date object's UTC getters, so chart_view._chart_ts bakes
the DST-correct Melbourne offset into the epoch value itself rather than
relying on any client-side timezone config.

    .venv/bin/pytest -q tests/test_chart_view_tz.py
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from backtest.chart_view import _chart_ts


def test_aest_winter_offset_is_plus_ten_hours():
    # 2026-07-05 10:00 UTC (Australian winter -> AEST, UTC+10) -> 20:00 local.
    ts = pd.Timestamp("2026-07-05 10:00:00", tz="UTC")
    shifted = _chart_ts(ts)
    expected_local = dt.datetime(2026, 7, 5, 20, 0, 0, tzinfo=dt.timezone.utc)
    assert shifted == int(expected_local.timestamp())


def test_aedt_summer_offset_is_plus_eleven_hours():
    # 2026-01-15 10:00 UTC (Australian summer -> AEDT, UTC+11) -> 21:00 local.
    ts = pd.Timestamp("2026-01-15 10:00:00", tz="UTC")
    shifted = _chart_ts(ts)
    expected_local = dt.datetime(2026, 1, 15, 21, 0, 0, tzinfo=dt.timezone.utc)
    assert shifted == int(expected_local.timestamp())


def test_output_is_strictly_increasing_for_increasing_input():
    times = pd.date_range("2026-07-05 00:00:00", periods=10, freq="1h", tz="UTC")
    shifted = [_chart_ts(t) for t in times]
    assert shifted == sorted(shifted)
    assert len(set(shifted)) == len(shifted)

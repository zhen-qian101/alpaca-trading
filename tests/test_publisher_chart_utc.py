"""The cloud dashboard's charts must carry TRUE UTC epochs.

backtest/chart_view.py stamps every candle, indicator point and marker with
Melbourne wall-clock time re-tagged as UTC, for the old page that formats with
UTC getters. The publisher builds charts with that shift switched off
(web.publisher.true_utc_chart_time). These tests run the REAL chart builder
(web.server.build_chart -> backtest.chart_view._build_chart_data -> the real
strategy runner) over synthetic hourly bars that straddle each DST transition
that could bite: Melbourne's April fall-back (where the shifted times collide
and a bar is dropped), Melbourne's October spring-forward (a fake 2-hour gap),
and both US transitions (which must change nothing at all in UTC).

Every bar and price here is invented.

    .venv/bin/python -m pytest -q tests/test_publisher_chart_utc.py
"""
from __future__ import annotations

import json
import math
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backtest import chart_view
from web import publisher as pubmod
from web import server as web_server

MELBOURNE = ZoneInfo("Australia/Melbourne")
SYMBOL = "QBIT/USD"        # 24/7 like crypto, so every hour has a bar
N_BARS = 400

# (name, first UTC hour that is on the far side of the transition)
TRANSITIONS = [
    # 2026-04-05 03:00 AEDT -> 02:00 AEST: local 02:xx happens twice
    ("melbourne-fall-back", pd.Timestamp("2026-04-04 16:00", tz="UTC")),
    # 2026-10-04 02:00 AEST -> 03:00 AEDT: local 02:xx never happens
    ("melbourne-spring-forward", pd.Timestamp("2026-10-03 16:00", tz="UTC")),
    # 2026-03-08 02:00 EST -> 03:00 EDT
    ("us-spring-forward", pd.Timestamp("2026-03-08 07:00", tz="UTC")),
    # 2026-11-01 02:00 EDT -> 01:00 EST
    ("us-fall-back", pd.Timestamp("2026-11-01 06:00", tz="UTC")),
]


def _bars(transition: pd.Timestamp) -> pd.DataFrame:
    """N_BARS hourly bars with the transition 300 bars in, i.e. inside the
    220-bar window the chart shows. A slow sine plus drift gives the strategy
    runner real crossings to mark."""
    idx = pd.date_range(transition - pd.Timedelta(hours=300), periods=N_BARS,
                        freq="h", tz="UTC")
    closes = [100 + 0.02 * i + 6 * math.sin(i / 9.0) + 2 * math.sin(i / 2.3)
              for i in range(N_BARS)]
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "open": opens,
        "high": [max(o, c) + 0.4 for o, c in zip(opens, closes)],
        "low": [min(o, c) - 0.4 for o, c in zip(opens, closes)],
        "close": closes,
        "volume": [1000.0] * N_BARS,
    }, index=idx)


@pytest.fixture
def real_chart(monkeypatch, tmp_path):
    """Point the real builder at synthetic bars, config, fills and journal."""
    cfg = {"symbols": {SYMBOL: {
        "enabled": True, "interval": "1h", "stop": 0.06, "deploy": 0.97,
        "strategies": [
            {"type": "macd_sma", "fast_len": 8, "slow_len": 21, "signal_len": 9,
             "veryslow_len": 50},
            {"type": "bollinger_rsi", "rsi_length": 6, "bb_length": 20, "bb_mult": 1.5},
        ]}}}
    config = tmp_path / "config.json"
    config.write_text(json.dumps(cfg))
    monkeypatch.setattr(web_server, "CONFIG_FILE", config)
    monkeypatch.setattr(chart_view, "JOURNAL_FILE", tmp_path / "no-journal.jsonl")
    web_server._CACHE = web_server._Cache()
    state = {}

    def use(transition):
        df = _bars(transition)
        state["df"] = df
        monkeypatch.setattr(chart_view, "read_bars", lambda sym, iv: df.copy())
        # One broker fill 10 minutes into the bar right at the transition.
        fill_bar = transition
        state["fill_bar"] = fill_bar
        monkeypatch.setattr(web_server, "build_executions", lambda limit=60: {"rows": [
            {"ts": (fill_bar + pd.Timedelta(minutes=10)).timestamp(), "symbol": SYMBOL,
             "side": "buy", "qty": 3.0, "price": 101.25}]})
        return df

    state["use"] = use
    state["state_dir"] = tmp_path / "state"
    return state


def _publish(transition, real_chart) -> tuple[pd.DataFrame, dict]:
    df = real_chart["use"](transition)
    pub = pubmod.Publisher(pubmod.NullDestination(), real_chart["state_dir"],
                           pubmod.Options(dry_run=True))
    payload = pub.build_chart_payload(SYMBOL, "2026-03-04T21:05:00+00:00")
    assert "error" not in payload, payload
    return df, payload


@pytest.mark.parametrize("name,transition", TRANSITIONS, ids=[t[0] for t in TRANSITIONS])
def test_every_time_is_the_true_utc_epoch_of_its_bar(real_chart, name, transition):
    df, payload = _publish(transition, real_chart)
    assert payload["time_basis"] == "utc"
    assert list(payload["symbols"]) == [SYMBOL]
    chart = payload["symbols"][SYMBOL]

    window = df.index[-220:]
    want = [int(ts.timestamp()) for ts in window]
    times = [c["time"] for c in chart["candles"]]
    # Exactly one candle per bar: nothing dropped, nothing shifted.
    assert times == want
    assert all(b - a == 3600 for a, b in zip(times, times[1:]))

    bar_times = set(want)
    for series in ("bbUpper", "bbBasis", "bbLower", "rsi", "macdHist"):
        assert chart[series], f"{series} unexpectedly empty"
        assert {p["time"] for p in chart[series]} <= bar_times, series
    assert chart["markers"], "the synthetic series should produce signals"
    assert {m["time"] for m in chart["markers"]} <= bar_times
    assert [m["time"] for m in chart["markers"]] == sorted(m["time"] for m in chart["markers"])

    # The fill lands on the bar that contains it, in UTC.
    fills = [m for m in chart["markers"] if m["shape"] == "circle"]
    assert [m["time"] for m in fills] == [int(real_chart["fill_bar"].timestamp())]
    assert fills[0]["text"] == "BUY 3 @ 101.25"


@pytest.mark.parametrize("name,transition", TRANSITIONS, ids=[t[0] for t in TRANSITIONS])
def test_the_shift_removed_is_exactly_melbournes_offset(real_chart, name, transition):
    """What the publisher removes is precisely chart_view's shift, DST-correct
    per bar: +11 h on AEDT bars and +10 h on AEST bars. And the shift is back
    in place for everyone else once the build is done."""
    df, payload = _publish(transition, real_chart)
    utc_times = [c["time"] for c in payload["symbols"][SYMBOL]["candles"]]
    assert chart_view._chart_ts is not pubmod.utc_epoch       # restored
    for ts, utc in zip(df.index[-220:], utc_times):
        offset = int(ts.tz_convert(MELBOURNE).utcoffset().total_seconds())
        assert offset in (36000, 39600)
        assert chart_view._chart_ts(ts) - utc == offset


def test_the_old_melbourne_times_lose_a_bar_at_fall_back(real_chart):
    """Why the shift is switched off rather than reversed: in the April
    fall-back hour two bars share one Melbourne wall-clock time, the old
    builder drops one, and no reverse conversion could tell them apart."""
    transition = TRANSITIONS[0][1]
    real_chart["use"](transition)
    cfg = json.loads(web_server.CONFIG_FILE.read_text())["symbols"]
    old = chart_view._build_chart_data(cfg, 220)[SYMBOL]["candles"]
    assert len(old) == 219
    _, payload = _publish(transition, real_chart)
    assert len(payload["symbols"][SYMBOL]["candles"]) == 220
    # the two bars the old times collapsed are both present, an hour apart
    a = int((transition - pd.Timedelta(hours=1)).timestamp())
    b = int(transition.timestamp())
    times = {c["time"] for c in payload["symbols"][SYMBOL]["candles"]}
    assert {a, b} <= times and b - a == 3600


def test_the_patch_is_undone_when_the_build_raises():
    original = chart_view._chart_ts
    with pytest.raises(RuntimeError):
        with pubmod.true_utc_chart_time():
            assert chart_view._chart_ts is pubmod.utc_epoch
            raise RuntimeError("boom")
    assert chart_view._chart_ts is original


def test_utc_epoch_matches_pandas_for_aware_and_naive_bars():
    ts = pd.Timestamp("2026-04-04 15:00", tz="UTC")
    assert pubmod.utc_epoch(ts) == 1775314800
    assert pubmod.utc_epoch(ts.tz_convert(MELBOURNE)) == 1775314800
    assert pubmod.utc_epoch(pd.Timestamp("2026-04-04 15:00")) == 1775314800

"""ChartArt's "MACD + SMA 200 Strategy", translated from Pine Script v2 (the
Pine source was retired with TradingView) to Python, using this codebase's
bar-close-only, long/flat, deploy%/stop_pct conventions (see
backtest/bollinger_rsi.py).

Note this MACD is SMA-based (fastMA/slowMA are plain SMAs of close, not the
usual EMA-based MACD) — a deliberate ChartArt variant, not a translation
error. Pine -> Python mapping:
  fastMA     = SMA(close, 12)
  slowMA     = SMA(close, 26)
  macd       = fastMA - slowMA
  signal     = SMA(macd, 9)
  hist       = macd - signal
  veryslowMA = SMA(close, 200)
  long entry  : crossover(hist, 0) AND macd > 0 AND fastMA > slowMA
                AND close[26 bars ago] > veryslowMA[now]
  short entry : crossunder(hist, 0) AND macd < 0 AND fastMA < slowMA
                AND close[26 bars ago] < veryslowMA[now]

The `close[slowLength]` (price 26 bars in the past vs. TODAY's SMA-200) is
exactly what the original script says, even though its own comment claims
"the recent price has to be above the SMA 200" (which would be `close`, not
`close[26]`) — kept as literally coded, not as commented, since the code is
the actual backtested behavior.

Two simplifications vs. the original, both consistent with prior
translations in this file set:
  - The original places entries as stop orders (`stop=buyprice/sellprice`,
    the signal bar's own low/high) with a same-direction `strategy.cancel`
    each bar the filter condition goes false. Since the stop level sits at a
    price the signal bar has already reached, this resolves to "fill on the
    next bar" in practice — modeled here as decide-on-close/fill-at-next-open,
    same as every other strategy in this codebase. The cancel-if-filter-flips
    logic has no effect under a single-bar fill delay, so it's not modeled.
  - The original's `strategy.risk.max_intraday_loss(50%, percent_of_equity)`
    account-wide circuit breaker is omitted: this codebase's per-trade
    stop_pct (default 2%) already caps risk far tighter than a 50%-of-equity
    breaker could ever bind, so adding it would be dead code, not a real
    control (see [[chop-gate-lesson]] — keep the parameter surface small).

The original is stop-and-reverse (long<->short); this codebase trades
long-only, so the short condition means "exit to flat," same adaptation as
bollinger_rsi.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _crossover(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    prev_le = np.r_[False, a[:-1] <= b[:-1]]
    return prev_le & (a > b)


def _crossunder(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    prev_ge = np.r_[False, a[:-1] >= b[:-1]]
    return prev_ge & (a < b)


def _sma(x: np.ndarray, length: int) -> np.ndarray:
    return pd.Series(x).rolling(length).mean().to_numpy()


def compute_indicators(close: np.ndarray, fast_len: int, slow_len: int, signal_len: int, veryslow_len: int) -> dict:
    """The raw indicator arrays behind this strategy's signals — exposed
    separately so callers (e.g. backtest/chart_view.py) can plot them without
    duplicating the SMA-based-MACD math."""
    fast_ma = _sma(close, fast_len)
    slow_ma = _sma(close, slow_len)
    macd = fast_ma - slow_ma
    signal = _sma(macd, signal_len)
    hist = macd - signal
    veryslow_ma = _sma(close, veryslow_len)
    return {"fast_ma": fast_ma, "slow_ma": slow_ma, "macd": macd, "signal": signal,
            "hist": hist, "veryslow_ma": veryslow_ma}


def _signals(close: np.ndarray, fast_len: int, slow_len: int, signal_len: int, veryslow_len: int):
    n = len(close)
    ind = compute_indicators(close, fast_len, slow_len, signal_len, veryslow_len)
    fast_ma, slow_ma = ind["fast_ma"], ind["slow_ma"]
    macd, signal, hist = ind["macd"], ind["signal"], ind["hist"]
    veryslow_ma = ind["veryslow_ma"]

    close_lag = np.full(n, np.nan)
    close_lag[slow_len:] = close[:-slow_len]

    hist_co = _crossover(hist, np.zeros(n))
    hist_cu = _crossunder(hist, np.zeros(n))

    valid = ~(np.isnan(fast_ma) | np.isnan(slow_ma) | np.isnan(signal) | np.isnan(veryslow_ma) | np.isnan(close_lag))

    buy_signal = valid & hist_co & (macd > 0) & (fast_ma > slow_ma) & (close_lag > veryslow_ma)
    sell_signal = valid & hist_cu & (macd < 0) & (fast_ma < slow_ma) & (close_lag < veryslow_ma)
    return buy_signal, sell_signal


def _run_core(df: pd.DataFrame, fast_len: int = 12, slow_len: int = 26, signal_len: int = 9,
              veryslow_len: int = 200, init_cash: float = 100_000, deploy: float = 0.97,
              stop_pct: float = 0.02, fees: float = 0.0,
              bars_per_year: int | None = None):
    close = df["close"].to_numpy(dtype=float)
    n = len(df)
    if n < veryslow_len + slow_len + 5:
        return ({"fast_len": fast_len, "slow_len": slow_len, "signal_len": signal_len,
                 "veryslow_len": veryslow_len, "total_return": 0.0, "sharpe": float("nan"),
                 "max_drawdown": 0.0, "win_rate": 0.0, "n_trades": 0},
                pd.Series(init_cash, index=[df.index[0]]), [])

    buy_signal, sell_signal = _signals(close, fast_len, slow_len, signal_len, veryslow_len)

    cash = init_cash
    position = 0.0
    entry_price = 0.0
    pending_entry = False

    equity = np.empty(n)
    trades = []
    events = []

    for i in range(n):
        open_i = close[i - 1] if i > 0 else close[i]  # proxy for next-bar open

        if pending_entry and position == 0:
            entry_px = open_i
            qty = (cash * deploy) / entry_px if entry_px > 0 else 0.0
            if qty > 0:
                cash -= qty * entry_px * (1 + fees)
                position = qty
                entry_price = entry_px
                trades.append({"entry": entry_px, "qty": qty, "exit": np.nan, "exit_reason": ""})
                events.append({"ts": df.index[i], "action": "buy", "price": float(entry_px),
                               "reason": "macd_sma_bullish"})
        pending_entry = False

        if position > 0:
            c = close[i]
            stopped = c <= entry_price * (1 - stop_pct)
            if stopped:
                cash += position * c * (1 - fees)
                trades[-1]["exit"] = c
                trades[-1]["exit_reason"] = "stop"
                position = 0.0
                events.append({"ts": df.index[i], "action": "close", "price": float(c), "reason": "stop"})
            elif sell_signal[i]:
                cash += position * c * (1 - fees)
                trades[-1]["exit"] = c
                trades[-1]["exit_reason"] = "macd_sma_bearish"
                position = 0.0
                events.append({"ts": df.index[i], "action": "close", "price": float(c),
                               "reason": "macd_sma_bearish"})

        if buy_signal[i] and position == 0:
            pending_entry = True

        equity[i] = cash + position * close[i]

    if position > 0:
        c = close[-1]
        cash += position * c * (1 - fees)
        trades[-1]["exit"] = c
        trades[-1]["exit_reason"] = "eod"
        position = 0.0
        equity[-1] = cash
        events.append({"ts": df.index[-1], "action": "close", "price": float(c), "reason": "eod"})

    eq = pd.Series(equity, index=df.index)
    total_return = (cash / init_cash - 1.0) * 100 if position == 0 else (eq.iloc[-1] / init_cash - 1) * 100
    peak = eq.cummax()
    max_dd = float((eq / peak - 1.0).min() * 100)
    closed = [t for t in trades if not np.isnan(t["exit"])]
    wins = sum(1 for t in closed if t["exit"] > t["entry"])
    win_rate = (wins / len(closed) * 100) if closed else 0.0
    rets = eq.pct_change().dropna()
    sharpe = float("nan")
    if rets.std() > 0:
        bpy = bars_per_year if bars_per_year else (78 * 252)
        sharpe = float(rets.mean() / rets.std() * np.sqrt(bpy))
    return {
        "fast_len": fast_len, "slow_len": slow_len, "signal_len": signal_len, "veryslow_len": veryslow_len,
        "total_return": float(total_return),
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "win_rate": float(win_rate),
        "n_trades": len(closed),
    }, eq, events


def run_with_events(df, fast_len=12, slow_len=26, signal_len=9, veryslow_len=200, **kw):
    return _run_core(df, fast_len, slow_len, signal_len, veryslow_len, **kw)

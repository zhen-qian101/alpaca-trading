"""ChartArt's "RSI + Bollinger Bands, Double Strategy" v1.1, translated from
Pine Script v2 (the Pine source was retired with TradingView) to Python,
using this codebase's bar-close-only, long/flat, deploy%/stop_pct conventions.

Pine -> Python mapping:
  vrsi    = RSI(close, RSIlength)             (Wilder-smoothed, RSIlength=6 default)
  BBbasis = SMA(close, BBlength)               (BBlength=200 default)
  BBdev   = BBmult * stdev(close, BBlength)    (BBmult=2, population stdev)
  BBupper/BBlower = BBbasis +/- BBdev
  long entry ("RSI_BB_L")  : crossover(vrsi, 50) AND crossover(close, BBlower)
  short entry ("RSI_BB_S") : crossunder(vrsi, 50) AND crossunder(close, BBupper)

Note the thresholds are 50, not the usual 30/70 — this isn't an
overbought/oversold extreme filter, it's "RSI momentum flipped bullish/
bearish at the same moment price reclaimed/lost a Bollinger Band," a
mean-reversion-off-the-band trade gated by a momentum-direction confirmation.

The original Pine strategy places the entry as a stop order AT the band level
(`stop=BBlower`/`stop=BBupper`) rather than a market order — a Pine v2
mechanism for resting a trigger order. Since both conditions already require
price to have just crossed that same level intrabar-close, this is
functionally "enter once both conditions are true," which this port executes
at the next bar's open — the same bar-close-decision/next-open-fill
convention used across this codebase's strategies (see ema_scaled_rolling.py).

The original is stop-and-reverse (long<->short) with no risk stop of its own.
This codebase trades long-only, so the short condition here means "exit to
flat" (the same adaptation macd_sma.py makes), and a stop_pct risk stop is
added for comparability with the other strategies — it is NOT part of
ChartArt's original script.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rsi_wilder(close: np.ndarray, period: int) -> np.ndarray:
    n = len(close)
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    if n <= period:
        return np.full(n, np.nan)

    avg_gain[period] = gain[1:period + 1].mean()
    avg_loss[period] = loss[1:period + 1].mean()
    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period

    rs = np.divide(avg_gain, avg_loss, out=np.full(n, np.nan), where=avg_loss != 0)
    rsi = 100 - 100 / (1 + rs)
    rsi[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    rsi[(avg_loss == 0) & (avg_gain == 0)] = 50.0
    return rsi


def _bollinger(close: np.ndarray, length: int, mult: float):
    basis = pd.Series(close).rolling(length).mean().to_numpy()
    dev = mult * pd.Series(close).rolling(length).std(ddof=0).to_numpy()
    return basis, basis + dev, basis - dev


def _crossover(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    prev_le = np.r_[False, a[:-1] <= b[:-1]]
    return prev_le & (a > b)


def _crossunder(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    prev_ge = np.r_[False, a[:-1] >= b[:-1]]
    return prev_ge & (a < b)


def compute_indicators(close: np.ndarray, rsi_length: int, bb_length: int, bb_mult: float) -> dict:
    """The raw indicator arrays behind this strategy's signals — exposed
    separately so callers (e.g. backtest/chart_view.py) can plot RSI/Bollinger
    Bands without duplicating the math."""
    rsi = _rsi_wilder(close, rsi_length)
    basis, upper, lower = _bollinger(close, bb_length, bb_mult)
    return {"rsi": rsi, "basis": basis, "upper": upper, "lower": lower}


def _signals(close: np.ndarray, rsi_length: int, bb_length: int, bb_mult: float):
    ind = compute_indicators(close, rsi_length, bb_length, bb_mult)
    rsi, upper, lower = ind["rsi"], ind["upper"], ind["lower"]

    valid = ~(np.isnan(rsi) | np.isnan(upper) | np.isnan(lower))
    rsi_co = _crossover(rsi, np.full_like(rsi, 50.0))
    rsi_cu = _crossunder(rsi, np.full_like(rsi, 50.0))
    price_co_lower = _crossover(close, lower)
    price_cu_upper = _crossunder(close, upper)

    buy_signal = valid & rsi_co & price_co_lower
    sell_signal = valid & rsi_cu & price_cu_upper
    return buy_signal, sell_signal


def _run_core(df: pd.DataFrame, rsi_length: int = 6, bb_length: int = 200, bb_mult: float = 2.0,
              init_cash: float = 100_000, deploy: float = 0.97,
              stop_pct: float = 0.02, fees: float = 0.0,
              bars_per_year: int | None = None):
    close = df["close"].to_numpy(dtype=float)
    n = len(df)
    if n < bb_length + rsi_length + 5:
        return ({"rsi_length": rsi_length, "bb_length": bb_length, "bb_mult": bb_mult,
                 "total_return": 0.0, "sharpe": float("nan"),
                 "max_drawdown": 0.0, "win_rate": 0.0, "n_trades": 0},
                pd.Series(init_cash, index=[df.index[0]]), [])

    buy_signal, sell_signal = _signals(close, rsi_length, bb_length, bb_mult)

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
                               "reason": "rsi_bb_long"})
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
                trades[-1]["exit_reason"] = "rsi_bb_short"
                position = 0.0
                events.append({"ts": df.index[i], "action": "close", "price": float(c),
                               "reason": "rsi_bb_short"})

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
        "rsi_length": rsi_length, "bb_length": bb_length, "bb_mult": bb_mult,
        "total_return": float(total_return),
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "win_rate": float(win_rate),
        "n_trades": len(closed),
    }, eq, events


def run_with_events(df, rsi_length=6, bb_length=200, bb_mult=2.0, **kw):
    return _run_core(df, rsi_length, bb_length, bb_mult, **kw)

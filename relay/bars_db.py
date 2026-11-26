"""SQLite bar cache shared by the signal engine and the backtester.

All bars are stored normalized to **UTC** epoch-seconds so mixed sources
(yfinance America/New_York, IBKR UTC) compare consistently. The engine writes
newly fetched bars here each poll and runs the backtest over the cached warm-up
window; backtests read from here (`source="cache"`) for fast, rate-limit-free
sweeps. Single file at `relay.config.BARS_DB_FILE` (on the big disk).

Single shared sqlite3.Connection across threads (`check_same_thread=False`) —
note that flag only disables the same-thread safety CHECK, it does NOT itself
make concurrent multi-threaded use of one connection safe; ALL access (reads
and writes alike) is therefore serialized under `_LOCK` here. The engine
parallelizes per-symbol bar fetching across threads (2026-07-03, for latency —
see sigengine/engine.py's _fetch_and_recompute), so this module's calls are no
longer single-threaded in practice.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Optional

import pandas as pd

from .config import BARS_DB_FILE, ensure_data_dirs

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        ensure_data_dirs()
        _CONN = sqlite3.connect(str(BARS_DB_FILE), check_same_thread=False)
        _CONN.execute(
            """CREATE TABLE IF NOT EXISTS bars (
                   symbol   TEXT NOT NULL,
                   interval TEXT NOT NULL,
                   ts       INTEGER NOT NULL,   -- UTC epoch seconds
                   open     REAL,
                   high     REAL,
                   low      REAL,
                   close    REAL NOT NULL,
                   volume   REAL,
                   source   TEXT,
                   PRIMARY KEY (symbol, interval, ts)
               )"""
        )
        _CONN.execute(
            "CREATE INDEX IF NOT EXISTS idx_bars_sym_ts ON bars(symbol, interval, ts)"
        )
        _CONN.commit()
    return _CONN


def _to_utc_epoch(ts) -> int:
    """Coerce any timestamp (tz-aware or naive) to UTC epoch seconds."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.tz_convert("UTC").timestamp())


# Source precedence. A bar for a given (symbol, interval, ts) may arrive from
# more than one vendor; the higher rank wins and OVERWRITES the lower, while a
# lower-ranked source can never clobber a higher-ranked bar already cached.
#
# Why this exists (2026-07-14): upsert_bars was plain INSERT OR IGNORE, i.e.
# whoever wrote a ts FIRST owned it forever. With yfinance as the fallback that
# is exactly backwards — a transient IBKR outage would let a yfinance bar land
# first and then permanently block the real IBKR bar for that hour, so the
# degraded source would win by being early. IBKR is the configured primary
# (sigengine/config.json `data.primary`) and carries real volume + true 24h
# coverage, so it outranks everything.
_SOURCE_RANK = {"yfinance": 0, "alpaca": 1, "ibkr": 2}
_RANK_SQL = "CASE {col} WHEN 'ibkr' THEN 2 WHEN 'alpaca' THEN 1 ELSE 0 END"


def source_rank(source: str) -> int:
    """Precedence of `source`; unknown sources rank lowest (0)."""
    return _SOURCE_RANK.get((source or "").lower(), 0)


def upsert_bars(symbol: str, interval: str, df: pd.DataFrame, source: str) -> int:
    """Insert bars, resolving same-ts collisions by source precedence.

    A ts not yet cached is inserted. A ts already cached is OVERWRITTEN when the
    incoming bar is BETTER, which is two different things:

    * a higher-ranked source (see _SOURCE_RANK) — IBKR upgrades a yfinance bar,
      and yfinance can never downgrade an IBKR one;
    * the SAME source with at least as much volume — i.e. a more complete print
      of the same hour.

    That second rule is not a refinement, it is a correctness fix (2026-07-31).
    Same-source re-fetches used to be ignored outright, which sounds harmless
    until you notice WHEN a bar is first written: the engine polls every 5
    minutes, so the current hour's bar is cached while it is still FORMING, and
    the completed bar fetched later was then thrown away. Every 1h close in the
    cache was the price a few minutes into the hour, frozen forever. Measured on
    2026-07-31, 148 of 151 cached MSFT bars disagreed with IBKR's own values,
    one by $11.09 — and MACD, RSI, Bollinger and every gate the engine trades on
    are computed from these closes.

    Volume is the completeness test because a forming bar only ever gains
    volume, so it distinguishes "later and more complete" from "later and
    degraded" without needing to know the clock.

    `df` must be tz-aware-indexed with at least a `close` column (high/low used
    by the backtest; open/volume optional). Returns the number of rows inserted
    or upgraded.
    """
    if df is None or df.empty:
        return 0
    conn = _connect()
    rows = []
    for ts, r in df.iterrows():
        epoch = _to_utc_epoch(ts)
        rows.append((
            symbol.upper(), interval.lower(), epoch,
            float(r.get("open")) if "open" in r and pd.notna(r.get("open")) else None,
            float(r.get("high")) if "high" in r and pd.notna(r.get("high")) else None,
            float(r.get("low")) if "low" in r and pd.notna(r.get("low")) else None,
            float(r["close"]) if pd.notna(r.get("close")) else None,
            float(r.get("volume")) if "volume" in r and pd.notna(r.get("volume")) else None,
            source,
        ))
    # ON CONFLICT ... DO UPDATE ... WHERE: the WHERE clause is what makes this a
    # precedence-aware upsert rather than a last-writer-wins one. It updates on a
    # better SOURCE, or on the same source with a more complete bar (>= volume).
    # COALESCE(...,-1) makes a NULL volume the least complete value there is, so
    # an incoming bar with no volume can never displace one that has it.
    sql = (
        "INSERT INTO bars (symbol, interval, ts, open, high, low, close, volume, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(symbol, interval, ts) DO UPDATE SET "
        "  open=excluded.open, high=excluded.high, low=excluded.low, "
        "  close=excluded.close, volume=excluded.volume, source=excluded.source "
        f"WHERE ({_RANK_SQL.format(col='excluded.source')}) > ({_RANK_SQL.format(col='bars.source')}) "
        f"   OR (({_RANK_SQL.format(col='excluded.source')}) = ({_RANK_SQL.format(col='bars.source')}) "
        f"       AND COALESCE(excluded.volume, -1) >= COALESCE(bars.volume, -1))"
    )
    with _LOCK:
        cur = conn.executemany(sql, rows)
        conn.commit()
    return cur.rowcount


def delete_bars(symbol: str, interval: str) -> int:
    """Drop every cached bar for one symbol/interval. Returns rows deleted.

    upsert_bars can only ever ADD or IMPROVE a row for a timestamp present in
    the incoming frame — by design, so a partial re-fetch never erases history.
    That leaves no way to retire prices that were correct when written and are
    now wrong for the whole series, which is exactly what a SPLIT does: vendors
    re-adjust their entire history, but a warm cache only ever asks for the last
    10-15 days, so everything older keeps pre-split prices forever while the
    strategies replay the lot. Purging and re-fetching cold is the only repair.

    Deliberately narrow: one symbol, one interval, no date range, no wildcard —
    the caller is a recovery path, not routine maintenance.
    """
    conn = _connect()
    with _LOCK:
        cur = conn.execute("DELETE FROM bars WHERE symbol = ? AND interval = ?",
                           (symbol.upper(), interval))
        conn.commit()
    return cur.rowcount


def delete_bars_at(symbol: str, interval: str, timestamps, source: str | None = None) -> int:
    """Drop specific cached bars by timestamp. Returns rows deleted.

    The surgical counterpart to delete_bars. It exists for one caller —
    sigengine.engine._reject_outlier_bars condemning a print as junk — and
    without it that guard could not finish its job (2026-08-27, audit DG-3):
    the guard screened only the freshly-fetched frame, so a junk bar that had
    already been upserted stayed in the cache for good. upsert_bars has no
    DELETE path and only ever touches timestamps present in an incoming frame,
    so the strategies kept replaying the bad print until a higher-ranked source
    happened to overwrite that exact timestamp — which for a yfinance-only
    outage never happens.

    SAFE TO BE WRONG, because the condemned timestamps always lie inside the
    top-up window the engine just fetched: the next poll re-offers those bars,
    a clean one is simply re-inserted, and a genuinely bad one is condemned
    again. A false positive therefore costs one poll of that bar's absence, not
    permanent data loss. The hole left in the meantime is itself meaningful —
    _gap_blocked_events declines to open a position across it.

    `source` OBEYS THE SAME PRECEDENCE upsert_bars enforces, and passing it
    matters. The judgement that condemned these timestamps was made about ONE
    vendor's frame, and the engine only falls back to yfinance when IBKR is
    down — exactly when the cache is most likely to hold a better IBKR bar at
    the same timestamp. Deleting unconditionally would let a junk yfinance
    print destroy the good IBKR bar underneath it, and with IBKR unavailable
    nothing would bring it back. With `source` given, a cached row from a
    HIGHER-ranked vendor survives: only rows at or below the condemning
    source's rank are removed. Omitting it deletes regardless, which is only
    right when the caller means the timestamp itself to be gone.
    """
    ts = [_to_utc_epoch(t) for t in timestamps]
    if not ts:
        return 0
    sql = (f"DELETE FROM bars WHERE symbol = ? AND interval = ? "
           f"AND ts IN ({','.join('?' * len(ts))})")
    args = [symbol.upper(), interval.lower(), *ts]
    if source is not None:
        sql += f" AND ({_RANK_SQL.format(col='source')}) <= ?"
        args.append(source_rank(source))
    conn = _connect()
    with _LOCK:
        cur = conn.execute(sql, args)
        conn.commit()
    return cur.rowcount


def read_bars(symbol: str, interval: str, since=None) -> pd.DataFrame:
    """Return cached bars for symbol/interval as a UTC-tz DataFrame indexed by
    timestamp with columns high, low, close (+ open, volume if stored). Empty
    DataFrame with the right columns if none."""
    conn = _connect()
    sym = symbol.upper()
    iv = interval.lower()
    with _LOCK:
        if since is not None:
            since_epoch = _to_utc_epoch(since)
            cur = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM bars "
                "WHERE symbol=? AND interval=? AND ts>=? ORDER BY ts",
                (sym, iv, since_epoch),
            )
        else:
            cur = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM bars "
                "WHERE symbol=? AND interval=? ORDER BY ts",
                (sym, iv),
            )
        data = cur.fetchall()
    if not data:
        return pd.DataFrame(columns=["high", "low", "close"])
    ts = [pd.Timestamp(row[0], unit="s", tz="UTC") for row in data]
    df = pd.DataFrame(
        {
            "open": [r[1] for r in data],
            "high": [r[2] for r in data],
            "low": [r[3] for r in data],
            "close": [r[4] for r in data],
            "volume": [r[5] for r in data],
        },
        index=pd.DatetimeIndex(ts, name="ts"),
    )
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[["high", "low", "close", "open", "volume"]]


def latest_ts(symbol: str, interval: str):
    """Most recent cached bar ts (UTC pd.Timestamp) or None."""
    conn = _connect()
    with _LOCK:
        cur = conn.execute(
            "SELECT MAX(ts) FROM bars WHERE symbol=? AND interval=?",
            (symbol.upper(), interval.lower()),
        )
        row = cur.fetchone()
    if row and row[0] is not None:
        return pd.Timestamp(row[0], unit="s", tz="UTC")
    return None


def bar_count(symbol: str, interval: str) -> int:
    conn = _connect()
    with _LOCK:
        cur = conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? AND interval=?",
            (symbol.upper(), interval.lower()),
        )
        return int(cur.fetchone()[0])


def close() -> None:
    """Close the connection (tests)."""
    global _CONN
    if _CONN is not None:
        _CONN.close()
        _CONN = None
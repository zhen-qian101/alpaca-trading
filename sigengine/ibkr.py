"""IBKR real-time bar fetch via ib_async (the maintained successor to ib_insync).

This is the PRIMARY bar source for the signal engine (Part 2). The engine tries
IBKR first per symbol; if IBKR is down or the symbol is not subscribed, the
fetcher returns `None` and the engine falls back to yfinance for that poll —
so trading never stops because the Gateway blipped.

PREREQUISITES (the user must do these — cannot be automated):
  1. Install IB Gateway (paper) on this machine.
  2. Run it headlessly via IBC (deploy/run_ibgateway.sh + deploy/ibc.ini) so it
     auto-starts and auto-reconnects. IBC + Watchdog handle restarts.
  3. Sign the Market Data API Acknowledgement in Client Portal
     (Settings → Market Data Subscriptions) and confirm exchange subscriptions
     (Network A/B/C ~$4.50/mo non-pro). Without it, live API data returns
     "not subscribed" -> this fetcher returns None -> yfinance fallback kicks in.
  4. Crypto (BTC/USD) does NOT go through IBKR (Paxos-limited) -> the engine uses
     yfinance/Alpaca for crypto directly (see sigengine/config.json crypto_source).

Connection:
  - Port from env IBKR_PORT (default 4002 = Gateway paper; 7497 = TWS paper).
  - host from env IBKR_HOST (default 127.0.0.1).
  - clientId: EACH SYMBOL gets its OWN dedicated connection/clientId (rewrite
    2026-07-06 — see the note above _CONNECTIONS), offset from env
    IBKR_CLIENT_ID (default 1): base, base+1, base+2, ... assigned lazily in
    first-seen order, not a single shared clientId anymore.
  - reqMarketDataType(1) = Live (use 3 for Delayed if no subscription).

Usage:
    from sigengine.ibkr import fetch_bars
    df = fetch_bars("AAPL", "5min")   # -> tz-aware OHLCV DataFrame, or None
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import pandas as pd

try:
    from ib_async import IB, Stock, Crypto, util
except ImportError:  # pragma: no cover
    IB = None  # type: ignore
    Stock = None  # type: ignore
    Crypto = None  # type: ignore
    util = None  # type: ignore

log = logging.getLogger("sigengine.ibkr")

# interval -> (IBKR barSize, durationStr). Two duration tables:
#   _COLD_DURATION — the FULL warm-up window, used ONLY the first time a
#     symbol/interval has no cached bars yet (cache is empty).
#   _TOPUP_DURATION — a SMALL window used on every SUBSEQUENT poll once the
#     cache already holds the long history; only needs to cover the gap since
#     the last cached bar (plus a safety margin for downtime). This is what
#     keeps polling cheap: at poll_seconds=60 across N symbols, requesting the
#     full _COLD_DURATION on every single poll (the previous behavior here)
#     multiplies N symbols x 1 req/60s into a request volume that can exceed
#     IBKR's documented pacing limit (60 historical-data requests per 10 min,
#     https://interactivebrokers.github.io/tws-api/historical_limitations.html)
#     once more than ~6 symbols are enabled — this table is what makes 60s
#     polling safe to keep. keepUpToDate=False — we cache ourselves.
_BAR_SIZE = {"5min": "5 mins", "15min": "15 mins", "30min": "30 mins", "1h": "1 hour", "1d": "1 day"}
_COLD_DURATION = {"5min": "60 D", "15min": "60 D", "30min": "60 D", "1h": "2 Y", "1d": "2 Y"}
_TOPUP_DURATION = {"5min": "2 D", "15min": "2 D", "30min": "2 D", "1h": "10 D", "1d": "30 D"}

# PER-SYMBOL connections/locks (rewritten 2026-07-06 — see the incident notes
# below and [[sigengine-ibkr-lock-deadlock]]). Originally this module used ONE
# shared _IB connection + ONE shared _LOCK across ALL symbols, serializing
# every equity/crypto fetch through a single queue. Once ib.RequestTimeout
# (see connect()) was fixed to bound individual calls to ~12s instead of
# hanging forever, the true "engine completely wedged" failure mode was
# eliminated (confirmed live: zero self-restarts over 20+ min, vs. one every
# ~8 min before) — but a NEW problem became visible: with 9-10 symbols still
# sharing one lock, and each one legitimately able to take up to ~12s to
# resolve (success or a clean "not subscribed" timeout), a symbol queued late
# in a cycle can exhaust its OWN 15-20s patience budget just waiting in line
# behind several others, even though nothing is actually stuck. Confirmed
# live: 5 of 10 symbols got ZERO fresh data across 6+ full poll cycles (21+
# minutes) purely from queueing, not hangs. Giving each symbol its own
# connection + lock means one symbol's slow/stuck call can never delay any
# OTHER symbol's turn — they proceed fully independently and concurrently.
_CONNECTIONS: dict[str, "IB"] = {}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()  # guards lazy creation of entries in _LOCKS only
_SUSPECT: dict[str, bool] = {}
_LOCK_HELD_SINCE: dict[str, float] = {}
_SYMBOL_CLIENT_IDS: dict[str, int] = {}
_CLIENT_ID_GUARD = threading.Lock()


def _get_lock(symbol: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if symbol not in _LOCKS:
            _LOCKS[symbol] = threading.Lock()
        return _LOCKS[symbol]


def _client_id_for(symbol: str) -> int:
    """Each symbol needs its OWN clientId — IBKR requires a unique clientId
    per simultaneous connection to the same Gateway. Assigned lazily, in
    first-seen order, offset from IBKR_CLIENT_ID (env, default 1): base,
    base+1, base+2, ... Stable for the lifetime of the process (a given
    symbol always gets the same id across repeated connects), but NOT
    guaranteed stable ACROSS process restarts (depends on which symbol is
    first to connect that run) — fine, since nothing depends on a specific
    symbol->id mapping surviving a restart."""
    with _CLIENT_ID_GUARD:
        if symbol not in _SYMBOL_CLIENT_IDS:
            base = int(os.getenv("IBKR_CLIENT_ID", "1"))
            _SYMBOL_CLIENT_IDS[symbol] = base + len(_SYMBOL_CLIENT_IDS)
        return _SYMBOL_CLIENT_IDS[symbol]


def mark_suspect(symbol: str) -> None:
    """Call when a CALLER (sigengine.engine._ibkr_fetch_with_timeout) gives up
    waiting on a fetch_bars(symbol, ...) call via its own timeout. The
    abandoned call's thread keeps running forever in the background (Python
    cannot force-kill it), and if its pending request never gets cleaned up
    in this SYMBOL's connection's internal state, a later call reusing that
    same object could inherit whatever confusion is left behind even though
    isConnected() still happily reports True. Forces a full disconnect+
    reconnect for THIS symbol only the next time connect(symbol) is called —
    cheap insurance, isolated to the one symbol that actually had a problem."""
    _SUSPECT[symbol] = True


# Real incident 2026-07-06, root-caused via the step-level timing logs added
# to _fetch_one: `ib.qualifyContracts()` for BTC/PAXOS hung with NO exception
# and NO timeout of its own, INSIDE the per-symbol lock below — meaning that
# call's thread (abandoned by the engine's own _IBKR_CALL_TIMEOUT, but still
# running forever since Python cannot force-kill a thread) never released the
# lock. Bounding the acquire here means a stuck previous call for THIS SYMBOL
# can never block a new cycle for more than this long — and since locks are
# now per-symbol, it can never affect any OTHER symbol at all.
_LOCK_ACQUIRE_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# The GLOBAL lock needs a bigger budget than the per-symbol one, because it is
# waiting on a QUEUE rather than on one stuck call.
#
# _LOCK_ACQUIRE_TIMEOUT above was sized for a single symbol's own hung call, and
# 15s is right for that. It was then reused for _GLOBAL_CALL_LOCK, where the
# wait is inherently N_SYMBOLS x one call chain — a completely different
# quantity, and one that grows every time a symbol is added.
#
# Measured 2026-08-28 on the live engine, one poll of 10 symbols: the serialized
# wave runs 13:41:05.4 -> 13:41:23.9, i.e. **18.4s**, about 2.1s per symbol
# (connect + qualify + reqHistoricalData on SMART and again on OVERNIGHT). The
# budget was 15.0s. So the LAST symbol to queue waited 15s, gave up at
# 13:41:20.4 — and its slot came free at 13:41:19.2. It missed by roughly a
# second, every single cycle.
#
# That is why exactly one symbol was always starved while the other nine were
# perfect, and why the victim MOVED when the watchlist changed: SOXL got 12
# IBKR fetches on 2026-08-18 against ~265 for everything else, and once SOXL
# left the rotation TQQQ inherited the slot and the failure (1 of 13 vs 13 of 13
# for the other nine). It was never a connection limit — there are zero connect
# failures in the logs — and never about a specific symbol. It was the 8th
# position, which is where ThreadPoolExecutor(max_workers=8) puts the last
# thread of the first wave.
#
# Falling back is not a cheap consolation either: the yfinance fallback is what
# served the corrupt bar that fabricated the 2026-08-19 MSFT trade. Waiting
# longer for good data beats being handed bad data quickly.
#
# So scale the budget with the number of symbols actually contending. 5s each
# against a measured 2.1s is ~2.4x headroom, and the cap keeps the whole wait
# inside the caller's own bounds (engine._FETCH_PHASE_TIMEOUT is 90s and one
# call chain is bounded by _IBKR_CALL_TIMEOUT at 20s).
_GLOBAL_WAIT_PER_SYMBOL = 5.0
_GLOBAL_WAIT_CAP = 60.0


def _global_acquire_timeout() -> float:
    """Seconds to wait for _GLOBAL_CALL_LOCK, scaled to the queue length.

    Uses the number of symbols that have registered a lock, which is exactly
    the set that can be contending. Never shorter than the per-symbol budget,
    so a single-symbol caller (sigengine/check_ibkr.py) behaves as before.
    """
    with _LOCKS_GUARD:
        n = len(_LOCKS) or 1
    return min(_GLOBAL_WAIT_CAP, max(_LOCK_ACQUIRE_TIMEOUT,
                                     _GLOBAL_WAIT_PER_SYMBOL * n))


def max_lock_held_seconds() -> tuple[str | None, float | None]:
    """Across every symbol with a currently-held lock, the (symbol, seconds)
    of whichever has been held longest — or (None, None) if none are held.
    Used by sigengine.engine's watchdog: the ONLY way to actually reclaim a
    thread that's genuinely hung forever inside a blocking IBKR call is to
    kill the whole PROCESS (Python cannot force-kill a thread, but the OS can
    reclaim everything a process holds) — so ANY one symbol stuck long enough
    still warrants a full restart, even though (unlike before this rewrite)
    it no longer blocks the other symbols in the meantime."""
    worst_symbol: str | None = None
    worst_seconds: float | None = None
    for sym, held_since in list(_LOCK_HELD_SINCE.items()):
        if held_since is None:
            continue
        secs = time.monotonic() - held_since
        if worst_seconds is None or secs > worst_seconds:
            worst_symbol, worst_seconds = sym, secs
    return worst_symbol, worst_seconds


# CRITICAL FINDING (2026-07-06, discovered AFTER the per-symbol rewrite above
# was first deployed): ib_async is not merely unsafe for concurrent calls on
# the SAME IB() instance (which the per-symbol split above already fixes) —
# it cannot safely handle concurrent calls across DIFFERENT IB() instances
# either, when driven from different threads at the same time. Confirmed
# empirically: opening just TWO separate connections and firing one
# qualifyContracts() each from two threads SIMULTANEOUSLY made BOTH hang
# (every single time, reproduced repeatedly) — even though each had its own
# distinct clientId/socket/RequestTimeout. This matches ib_async's own
# documented caveat ("not designed for concurrent multi-threaded calls") more
# broadly than initially assumed when this file was split into per-symbol
# connections: that split correctly isolates CONNECTION STATE (a stuck/
# suspect symbol's reconnect no longer affects any other symbol), but the
# actual ib_async CALLS must still be serialized process-wide, not just
# per-symbol. Sequential sanity tests (a plain for-loop over 9 connections,
# no threads) always succeeded fine — it's specifically genuine multi-
# threaded concurrency that breaks, likely something in ib_async/asyncio's
# handling of simultaneous event-loop-driven socket I/O across threads,
# rather than anything specific to this account/Gateway.
_GLOBAL_CALL_LOCK = threading.Lock()


def _acquire_lock_tracked(symbol: str) -> bool:
    """Acquires symbol's own lock (timeout=_LOCK_ACQUIRE_TIMEOUT) AND THEN
    the process-wide _GLOBAL_CALL_LOCK (see its docstring — ib_async cannot
    safely run concurrent calls even across different IB() instances), so
    only ONE symbol's connect/qualify/reqHistoricalData call chain is ever
    actually executing at a time, while still recording
    _LOCK_HELD_SINCE[symbol] for the watchdog. Logs and returns False
    (caller should treat as "IBKR unavailable this cycle") if either lock
    times out.

    The two budgets are deliberately DIFFERENT: the per-symbol lock guards
    against this symbol's own stuck call, while the global one is a queue whose
    length is the watchlist. Sharing one constant is what starved whichever
    symbol landed 8th — see _global_acquire_timeout.
    """
    if not _get_lock(symbol).acquire(timeout=_LOCK_ACQUIRE_TIMEOUT):
        log.warning("%s: IBKR lock busy for %ss (a previous call may still be stuck "
                    "holding it) — skipping IBKR this cycle", symbol, _LOCK_ACQUIRE_TIMEOUT)
        return False
    _LOCK_HELD_SINCE[symbol] = time.monotonic()
    budget = _global_acquire_timeout()
    if not _GLOBAL_CALL_LOCK.acquire(timeout=budget):
        log.warning("%s: IBKR global call lock busy for %.0fs (the queue of %d "
                    "symbol(s) did not clear) — skipping IBKR this cycle",
                    symbol, budget, len(_LOCKS))
        _LOCK_HELD_SINCE[symbol] = None
        _get_lock(symbol).release()
        return False
    return True


def _release_lock_tracked(symbol: str) -> None:
    _GLOBAL_CALL_LOCK.release()
    _LOCK_HELD_SINCE[symbol] = None
    _get_lock(symbol).release()


def connect(symbol: str) -> Optional["IB"]:
    """Connect `symbol` FRESH, every single call — never reuses a previous
    connection, even if `isConnected()` would report it healthy. Returns the
    connected IB, or None if ib_async is not installed / the Gateway is
    unreachable. Each symbol gets its own connection (own clientId) — see the
    per-symbol rewrite note above _CONNECTIONS for why.

    CHANGED 2026-07-06 (same investigation, later): connections were
    previously kept warm and REUSED across poll cycles (only reconnecting
    when marked suspect). That reuse pattern turned out to be the actual
    trigger for silent hangs — confirmed live and repeatedly: every
    connection's FIRST-ever use (right after connecting) succeeded reliably,
    but the SAME connection's reuse ~208s later (one poll cycle) hung
    uniformly, every single restart, every single time. Isolated tests
    matched this exactly: a connection that's fully torn down and replaced
    with a genuinely NEW one (new TCP session, same or different clientId)
    for the next fetch always succeeds fast; a kept-alive, reused connection
    does not. This matches documented IBKR behavior researched for this
    incident: exceeding IBKR's historical-data pacing limits (60 requests/10
    min — see https://interactivebrokers.github.io/tws-api/historical_limitations.html)
    or other server-side session staleness can produce a SILENT hang with NO
    error message at all (confirmed: zero ib_async-level error/warning log
    lines accompanied any of today's hangs, unlike the one time we did see a
    clean `Error 162` on 2026-07-04) — reconnecting fresh is IBKR's own
    documented recovery method for a stuck pacing/session state (their
    guidance: restart TWS, or Ctrl+Alt+F to force a fresh reconnect to the
    data servers). This trades a small amount of connect overhead (~0.15-1s
    typically) every cycle for far better reliability. If _SUSPECT is set,
    it's just already-redundant bookkeeping now (every connect is
    effectively a "suspect reconnect") — kept for the log message and
    because mark_suspect()'s caller-side contract is unchanged."""
    if IB is None:
        log.warning("ib_async not installed; IBKR unavailable")
        return None
    if _SUSPECT.get(symbol):
        log.warning("%s: IBKR connection marked suspect (a previous call was abandoned "
                    "as stuck) — forcing a fresh disconnect+reconnect instead of "
                    "reusing it", symbol)
        _SUSPECT[symbol] = False
    disconnect(symbol)
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_PORT", "4002"))
    client_id = _client_id_for(symbol)
    ib = IB()
    t_connect = time.monotonic()
    try:
        log.info("%s: connect() starting: %s:%s clientId=%s", symbol, host, port, client_id)
        # readonly=True (found 2026-07-06 investigating why connects always
        # took ~8s and logged "open orders request timed out" / "completed
        # orders request timed out" on EVERY single connect): ib_async's
        # connectAsync() unconditionally requests open-orders + completed-
        # orders sync unless readonly=True is passed — Gateway's own
        # ReadOnlyApi=yes (see deploy/ibc.ini) already blocks those requests
        # server-side, so they were guaranteed to fail every time regardless.
        # This connection only ever calls qualifyContracts/reqHistoricalData
        # (Alpaca is the sole order-execution path) — genuinely read-only, so
        # this flag just stops the client from doing guaranteed-wasted work on
        # every connect. Real config mismatch, not a workaround: the Gateway
        # side already declares read-only, the client side just wasn't told.
        ib.connect(host, port, clientId=client_id, timeout=8, readonly=True)
        # THE actual root-cause fix for qualifyContracts/reqHistoricalData
        # hanging forever (found 2026-07-06 via ib_async's own docstring for
        # IB.RequestTimeout, after ruling out ReadOnlyApi and confirming this
        # is a well-known, IBKR-acknowledged TWS API limitation — see
        # https://github.com/erdewit/ib_insync/issues/474,
        # https://groups.io/g/twsapi/topic/reqcontractdetails_not_always/4047493,
        # and ib_async's own IB class docstring, verbatim: "RequestTimeout
        # (float): Timeout (in seconds) to wait for a blocking request to
        # finish before raising asyncio.TimeoutError. The default value of 0
        # will wait indefinitely." — ib_async ships this exact protection
        # DISABLED by default (0 = wait forever) for every synchronous
        # wrapper call (ib.qualifyContracts, ib.reqHistoricalData, ...),
        # since they all route through IB._run() -> util.run(..., timeout=
        # self.RequestTimeout). We were never enabling it. This is not
        # another "give up and move on" layer like our own
        # _LOCK_ACQUIRE_TIMEOUT/_IBKR_CALL_TIMEOUT (which only stop OUR code
        # from waiting, while the underlying asyncio task keeps running
        # forever and the lock stays held) — asyncio.wait_for() actually
        # CANCELS the awaited coroutine on timeout, so a hang becomes a
        # clean, fast asyncio.TimeoutError exception that propagates through
        # _fetch_one's try/except and releases the lock normally, instead of
        # an unkillable stuck thread. Set below our own outer timeouts (15s
        # lock acquire, 20s call timeout) so this fires FIRST and is the
        # thing that actually resolves a hang; those remain as defense-in-
        # depth in case this doesn't catch every case (e.g. a hang below the
        # asyncio layer, in the underlying socket read itself). Confirmed
        # live this bounds individual calls to ~12s but does NOT by itself
        # prevent queueing delays across many symbols sharing one connection
        # — that's what the per-symbol connection split (this rewrite) fixes.
        ib.RequestTimeout = 12.0
        # 1 = Live; 3 = Delayed (useful when the subscription is absent but the
        # ack is signed). Try Live first; on a subscription error per-request the
        # fetch returns None and the engine falls back.
        try:
            ib.reqMarketDataType(1)
        except Exception as e:  # non-fatal — per-request will surface the real error
            log.debug("reqMarketDataType(1) failed: %s", e)
        _CONNECTIONS[symbol] = ib
        log.info("%s: IBKR connected %s:%s clientId=%s (%.2fs)", symbol, host, port,
                 client_id, time.monotonic() - t_connect)
        return ib
    except Exception as e:
        log.warning("%s: IBKR connect failed (%s:%s): %s — will use yfinance fallback",
                    symbol, host, port, e)
        # best-effort cleanup of a half-opened socket
        try:
            ib.disconnect()
        except Exception:
            pass
        return None


def disconnect(symbol: str) -> None:
    existing = _CONNECTIONS.pop(symbol, None)
    if existing is not None:
        try:
            existing.disconnect()
        except Exception:
            pass


_CRYPTO_SYMBOLS = {"BTC/USD": "BTC", "BTCUSD": "BTC", "BTC": "BTC",
                   "ETH/USD": "ETH", "ETHUSD": "ETH", "ETH": "ETH"}


def _contract_for(symbol: str, exchange: str = "SMART"):
    """Build a Stock contract. exchange="OVERNIGHT" targets IBKR's dedicated
    overnight-session venue (added ~2024) — a SEPARATE contract from the SMART
    one; useRTH=False on SMART only covers the standard pre-market/after-hours
    window (~04:00-20:00 ET), NOT the true 20:00-04:00 ET overnight session.
    See fetch_bars. Crypto symbols use _crypto_contract_for instead."""
    sym = symbol.upper().replace("/USD", "")
    return Stock(sym, exchange, "USD")


def _crypto_contract_for(symbol: str):
    """Build a Crypto contract. IBKR crypto trades on a single 24/7 venue,
    Paxos (exchange="PAXOS") — no SMART/OVERNIGHT split needed, unlike equities.
    https://interactivebrokers.github.io/tws-api/cryptocurrency.html"""
    sym = _CRYPTO_SYMBOLS[symbol.upper()]
    return Crypto(sym, "PAXOS", "USD")


def _fetch_one(ib: "IB", contract, bar_size: str, duration: str,
               what_to_show: str = "TRADES") -> Optional[pd.DataFrame]:
    """reqHistoricalData for one contract -> tz-aware UTC OHLCV DataFrame, or
    None on any qualify/subscription/request error.

    what_to_show: "TRADES" for equities (default). Crypto contracts use
    "AGGTRADES" instead — IBKR added this whatToShow value specifically for
    Crypto contracts (see the cryptocurrency doc page linked above).

    Step-level timing logs added 2026-07-06 while investigating BTC/USD's IBKR
    fetch silently hanging on the live engine (but not in isolated
    reproductions) — a hang means the log simply stops after whichever
    "starting X" line was last printed, pinpointing exactly which IBKR call
    never returned instead of a bare unexplained timeout from the caller."""
    t0 = time.monotonic()
    try:
        log.info("qualifyContracts starting: %s", contract)
        qualified = ib.qualifyContracts(contract)
        log.info("qualifyContracts done (%.2fs): %s", time.monotonic() - t0, qualified)
        if not qualified:
            return None
        t1 = time.monotonic()
        log.info("reqHistoricalData starting: %s bar_size=%s duration=%s what_to_show=%s",
                 contract, bar_size, duration, what_to_show)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            barSizeSetting=bar_size,
            durationStr=duration,
            whatToShow=what_to_show,
            useRTH=False,        # include extended hours (this venue's own session)
            keepUpToDate=False,  # we cache + re-poll ourselves
            formatDate=2,        # epoch seconds (UTC)
        )
        log.info("reqHistoricalData done (%.2fs): %d bars", time.monotonic() - t1,
                 len(bars) if bars else 0)
        if not bars:
            return None
        df = util.df(bars)  # columns: date, open, high, low, close, volume, ...
        # Capture the date index BEFORE dropping other columns — selecting
        # ["open","high","low","close","volume"] below drops "date" from df,
        # so building the index from df["date"] AFTER that selection (the
        # previous bug here) silently falls through to df's meaningless
        # default RangeIndex, which pd.DatetimeIndex(..., tz="UTC") then
        # misinterprets as nanoseconds-since-epoch — every bar ends up dated
        # ~1970-01-01. Verified live 2026-07-03: raw util.df(bars)["date"] is
        # already a proper tz-aware datetime64[ns, UTC] column; no unit
        # conversion needed, just don't drop it before reading it.
        idx = pd.DatetimeIndex(df["date"])
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        df = df[["open", "high", "low", "close", "volume"]].copy()
        df.index = idx
        return df
    except Exception as e:
        log.debug("IBKR reqHistoricalData failed for %s: %s", contract, e)
        return None


_BAR_PERIOD_S = {"5min": 300, "15min": 900, "30min": 1800, "1h": 3600, "1d": 86400}
# Bars this far (or less) off the grid are treated as a venue stamping quirk and
# snapped; anything further off is left alone and logged, since a large offset
# would mean the bar genuinely covers a different window.
_GRID_SNAP_TOLERANCE_S = 120


def _snap_to_grid(df: "pd.DataFrame", interval: str) -> "pd.DataFrame":
    """Snap near-grid bar stamps onto exact interval boundaries.

    IBKR's Crypto/PAXOS feed stamps the daily session-boundary bar at 20:01 UTC
    (16:01 ET) instead of 20:00 — 13 such rows were found in the cache on
    2026-07-14, each a 1-minute-off duplicate of the same hour already held from
    yfinance. Because the cache keys on exact ts, an off-by-60s stamp doesn't
    collide with the :00 bar and both rows survive, putting two bars in one hour
    (exactly the interleaving this grid work exists to remove).

    Only offsets within _GRID_SNAP_TOLERANCE_S are moved, so this can never
    silently re-label a genuinely different window (e.g. yfinance's :30 hourly
    bars, which are a real half-hour off and are fixed at the source instead —
    see backtest.ema_scaled_rolling._yf_hourly_aligned).
    """
    period = _BAR_PERIOD_S.get(interval.lower())
    if period is None or df is None or df.empty:
        return df
    idx = pd.DatetimeIndex(df.index)
    offset = (idx.asi8 // 10**9) % period
    near = (offset > 0) & (offset <= _GRID_SNAP_TOLERANCE_S)
    if not near.any():
        return df
    log.info("%s: snapping %d off-grid bar(s) onto the %s boundary",
             interval, int(near.sum()), interval)
    df = df.copy()
    df.index = idx.floor(f"{period}s")
    # A snapped bar can now collide with a real one for the same slot; the
    # snapped (session-boundary) bar is the later, more complete print.
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch_bars(symbol: str, interval: str) -> Optional[pd.DataFrame]:
    """Fetch historical bars for `symbol` at `interval`. Returns a tz-aware
    OHLCV DataFrame indexed by bar time (UTC), or None on any connection /
    subscription / symbol error (caller falls back to yfinance).

    Equities: queries BOTH the SMART contract (covers ~04:00-20:00 ET:
    pre-market + regular + after-hours, via useRTH=False) AND the OVERNIGHT
    contract (covers ~20:00-04:00 ET, IBKR's separate 24h venue) and merges
    them, so the result is genuine 24h coverage — not just extended hours. The
    OVERNIGHT leg requires its own market-data entitlement; if it's not
    subscribed / not supported for this symbol, that leg silently returns
    nothing and the SMART coverage is used alone (never fails the whole fetch
    over a missing overnight entitlement).

    Crypto (BTC/USD, ETH/USD): single Crypto/PAXOS contract, no SMART/OVERNIGHT
    split needed — Paxos already trades 24/7. Requires IBKR's separate crypto
    market data subscription; if not entitled, returns None and the caller
    falls back to yfinance (crypto_source in sigengine/config.json), same as
    any other unsubscribed-symbol failure here.

    `interval` must be one of 5min/15min/30min/1h/1d."""
    if IB is None:
        return None
    iv = interval.lower()
    if iv not in _BAR_SIZE:
        log.error("unsupported interval for IBKR: %s", iv)
        return None

    # Everything from here on touches THIS SYMBOL's own dedicated IB client —
    # serialize repeat calls for the SAME symbol (see the per-symbol rewrite
    # note above _CONNECTIONS), never across different symbols. yfinance-
    # fallback callers never reach this far, so they're unaffected and still
    # run fully concurrently. Bounded acquire (see _LOCK_ACQUIRE_TIMEOUT's
    # docstring): a PREVIOUS call for this symbol stuck forever inside this
    # same lock must not also block this symbol's future cycles forever just
    # trying to get in — and can never affect any OTHER symbol at all.
    if not _acquire_lock_tracked(symbol):
        return None
    try:
        ib = connect(symbol)
        if ib is None:
            return None

        # Cold cache (never polled this symbol/interval before) -> full warm-up
        # window. Warm cache -> small top-up only; the long history already
        # lives in relay.bars_db and upsert_bars is INSERT-OR-IGNORE, so
        # re-covering a few days is harmless and cheap.
        from relay.bars_db import bar_count
        is_cold = bar_count(symbol, iv) == 0
        bar_size = _BAR_SIZE[iv]
        duration = _COLD_DURATION[iv] if is_cold else _TOPUP_DURATION[iv]

        if symbol.upper() in _CRYPTO_SYMBOLS:
            df = _fetch_one(ib, _crypto_contract_for(symbol), bar_size, duration,
                            what_to_show="AGGTRADES")
            if df is None:
                log.warning("IBKR returned no crypto bars for %s %s (not subscribed?)", symbol, iv)
                return None
            return _snap_to_grid(df[["high", "low", "close", "open", "volume"]], iv)

        smart_df = _fetch_one(ib, _contract_for(symbol, "SMART"), bar_size, duration)
        if smart_df is None:
            log.warning("IBKR returned no SMART bars for %s %s (not subscribed?)", symbol, iv)
            return None
        overnight_df = _fetch_one(ib, _contract_for(symbol, "OVERNIGHT"), bar_size, duration)
        if overnight_df is None:
            log.debug("%s: no OVERNIGHT bars (not entitled or not supported) — SMART-only", symbol)
            df = smart_df
        else:
            df = pd.concat([smart_df, overnight_df])
    finally:
        _release_lock_tracked(symbol)

    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[["high", "low", "close", "open", "volume"]]
    return _snap_to_grid(df, iv)

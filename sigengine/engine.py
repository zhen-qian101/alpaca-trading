"""Self-hosted signal engine — the system's signal source (TradingView was removed).

DESIGN (recompute-and-diff):
  For each enabled symbol, pull live bars (yfinance by default; alpaca needs a
  paid plan for recent 5-min SIP data), run the SAME rolling EMA-cross backtest
  loop the backtester uses (`backtest.ema_scaled_rolling.run_with_events`), and
  diff the resulting order events against the last bar we have already
  accounted for. Any event with ts > last_bar_ts is a fresh signal.

  Each fresh event is translated into a `relay.models.OrderRequest` and handed
  to `relay.order_manager.OrderManager.handle()` IN-PROCESS — so it goes through
  the SAME risk gate (allowlist / bias / market-hours / sizing / daily-loss),
  the SAME signal-driven rotation, and the SAME `trades.jsonl` journal as the
  old TradingView webhook path did. There is no HTTP layer anymore.

  Sharing the backtest loop between research/backtest and live trading
  guarantees the signal logic cannot drift — the engine is literally a
  sliding-window re-run of the backtest.

EVENT -> ORDER MAPPING:
  buy   -> payload(action="buy",  qty = equity*deploy/price)   ; relay does
           bias/rotation/size checks (a buy on a flat-bias symbol is blocked,
           journaled as risk_block — expected, not an error).
  sell  -> same as close. This book is ALL-IN/ALL-OUT (operator directive
  close    2026-08-27): there is one way out of a position and it takes the
           whole thing.
        -> payload(action="close")                              ; liquidates the
           real position — the broker sizes it, we send no qty. Skipped if we
           don't hold the symbol, though a still-working ENTRY order is
           cancelled in that case rather than left to fill into a setup the
           strategy has already left (see _cancel_working_entry).
  reason=="eod" events are IGNORED — that is a backtest-window artifact (close
  at end of test data) and has no live meaning; acting on it would flatten the
  position every poll.

WARM-UP: on the first poll for a symbol (no state), record last_bar_ts and fire
  nothing. This means pre-existing signals that formed before the engine started
  do not fire; only fresh crosses from here on do.

State is persisted to relay.config.SIGENGINE_STATE_FILE (on the big disk at
DATA_DIR/sigengine/state.json) so a restart does not re-fire history.

Usage:
    .venv/bin/python -m sigengine.engine            # daemon loop
    .venv/bin/python -m sigengine.engine --once     # one poll, print, exit (still fires orders unless --dry-run)
    .venv/bin/python -m sigengine.engine --dry-run   # loop but never call handle()
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.ema_scaled_rolling import load_ohlcv
from backtest.multi_strategy import run_symbol_strategies as _run_symbol_strategies
from relay.alpaca_client import AlpacaClient
from relay.bars_db import (bar_count, delete_bars, delete_bars_at, latest_ts,
                           read_bars, upsert_bars)
from relay.config import get_settings, JOURNAL_FILE, SIGENGINE_STATE_FILE, ensure_data_dirs
from relay.logger import append_event
from relay.models import OrderRequest
from relay.order_manager import OrderManager
from relay.risk_filter import (EASTERN, RiskError, _is_equity_position, canonical_symbol,
                               equity_market_closed_for_weekend, is_crypto_symbol,
                               regular_market_hours_open)

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
STATE_FILE = SIGENGINE_STATE_FILE   # on the big disk (relay.config.DATA_DIR)

log = logging.getLogger("sigengine.engine")


# --------------------------------------------------------------------------- config
def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def _state_path(dry_run: bool) -> Path:
    """--dry-run must never advance the REAL state.json — doing so would
    silently mark a signal "already handled" with no order ever placed, and
    it would then NEVER fire for real either, even after dry-run testing ends
    (found 2026-07-03: two `--once --dry-run` review runs advanced state.json
    and permanently swallowed 3 live exit signals — harmless that time only
    because the account held zero positions, so the exits were no-ops
    regardless). Dry-run gets its own sibling state file so repeated dry-run
    polls don't just re-report the same signals forever, without ever
    touching the file live firing depends on."""
    path = STATE_FILE
    if dry_run:
        path = path.with_name(path.stem + "_dryrun" + path.suffix)
    return path


def load_state(dry_run: bool = False) -> dict:
    """The per-symbol "last processed bar" map. Nothing else — entry
    bookkeeping lives in the sidecar, see _aux_path.

    LOSING IT IS LOUD. Both failure paths below mean the same thing — every
    symbol re-warms from scratch, and a warm-up deliberately fires NOTHING, so
    every signal that formed while the state was unreadable is dropped. That
    used to happen in complete silence (audit 2026-08-27, S5): the daemon runs
    _diff_and_fire with quiet=True, so not even the per-symbol "warm-up" line
    was printed, and there was no journal event at all. The realistic trigger
    is not exotic — the data dir is on a removable disk (see relay.config), so
    a supervisor restart while it is unmounted silently drops the session's
    pending signals.

    A missing file on a genuinely first run is normal and says so; anything
    else is corruption and is journaled as well as logged.
    """
    path = _state_path(dry_run)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        log.warning("no state file at %s — every symbol will re-baseline and "
                    "fire nothing this poll (normal on a first run; NOT normal "
                    "on a restart, and it drops any signal formed since the "
                    "last poll)", path)
        return {}
    except json.JSONDecodeError as e:
        log.error("state file %s is unreadable (%s) — every symbol re-baselines "
                  "and every signal formed since the last poll is dropped", path, e)
        append_event(JOURNAL_FILE, {
            "event": "state_lost", "path": str(path), "reason": str(e),
        })
        return {}


def save_state(state: dict, dry_run: bool = False) -> None:
    """Persist the per-symbol "last processed bar" map ATOMICALLY.

    This was a bare write_text(), which open()s with "w" — truncating the real
    file first and only then writing. Anything that interrupts the write (power
    loss, the external SSD this lives on going away mid-write — see
    relay.config, the data dir is on a removable disk) leaves a 0-byte or
    half-written file. load_state() catches JSONDecodeError and returns {},
    which reads as "no state at all": every symbol silently re-warms, and a
    warm-up fires NOTHING, so every signal formed while the file was broken is
    dropped and the record of what had already been processed is gone.

    temp-file + os.replace makes the swap atomic on POSIX — a reader sees either
    the whole old file or the whole new one, never a torn one. fsync before the
    rename so the bytes are on disk, not just in the page cache, before the
    directory entry points at them.
    """
    path = _state_path(dry_run)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # atomic
    except Exception:
        tmp.unlink(missing_ok=True)    # never leave a stray .tmp behind
        raise


# --------------------------------------------------------------------------
# Entry bookkeeping — a SIDECAR file, deliberately not part of state.json.
#
# state.json's contract is "symbol -> last processed bar ISO", and it is read
# by another process: web/server.py builds the dashboard's per-symbol engine
# liveness panel with `{s: _norm_ts(t) for s, t in state.items()}`, which would
# read any bookkeeping key as if it were a ticker. Adding structure to a file a
# separate program parses positionally is how you break the thing you were not
# looking at, so this lives beside it instead.
#
# Two maps, both keyed by canonical symbol:
#   pending: an entry that was blocked for a TRANSIENT reason and may be
#            re-attempted for a few bars (see _retry_pending_entry, audit E2).
#   opened:  the bar at which the ENGINE actually opened the position it holds
#            (audit S2) — the reconciler's real answer to "did I open this?",
#            which _simulated_position cannot give because the strategies
#            replay the whole cached history and so almost always claim they
#            did.
#   adopted: shares the engine did NOT buy but has been made responsible for —
#            today only a cash-secured-put assignment (see _adopt_assignments).
#            Holds the basis the stop is measured from, because an assignment's
#            true cost basis is the STRIKE and nothing in the equity path knows
#            that number.
_PENDING_KEY, _OPENED_KEY, _ADOPTED_KEY = "pending", "opened", "adopted"

# How stale a CATCH-UP entry may be before the engine declines to place it, in
# bars of the symbol's own interval (see _diff_and_fire).
#
# _diff_and_fire fires every event newer than the state cursor regardless of
# age, and sizes it from the price on its own bar. After any downtime — a
# supervisor restart, a wedged IBKR lock, an overnight outage — that means
# executing a day-old signal at today's price with yesterday's sizing, with
# nothing but the share-count ceiling downstream to notice (audit S4). Three
# bars is the same tolerance _PENDING_ENTRY_MAX_BARS uses, for the same reason:
# past it, the setup the strategy saw is not the market in front of us.
#
# EXITS ARE NEVER AGED OUT. A stale exit is still an exit, and the standing
# rule of this book is that reducing exposure is always allowed.
_CATCHUP_ENTRY_MAX_BARS = 3

# How long a blocked entry stays live, in bars of the symbol's own interval.
# Three 1h bars: long enough to outlast a resting rotation exit or a working
# order (the transient blocks this exists for), short enough that the engine
# never chases a setup the market has moved away from. A retry is priced from
# the ORIGINAL signal bar, so an unbounded window would size a stale trade —
# the same hazard S4 covers for catch-up events.
_PENDING_ENTRY_MAX_BARS = 3


def _aux_path(dry_run: bool) -> Path:
    p = _state_path(dry_run)
    return p.with_name(p.stem + "_entries" + p.suffix)


def load_aux(dry_run: bool = False) -> dict:
    """Load the entry-bookkeeping sidecar.

    LOSING THIS IS LOUD TOO, for the same reason load_state is — and with a
    sharper consequence. An empty `opened` map makes _reconcile_positions treat
    every real position as one the engine did not open, so the missed-exit
    backstop stands down (it alerts instead of closing) until a fresh
    buy->close cycle rebuilds the record. That is the safe direction, but it is
    not a state anyone should be in without knowing.
    """
    path = _aux_path(dry_run)
    try:
        d = json.loads(path.read_text())
    except FileNotFoundError:
        d = {}                       # first run after this change: expected
    except json.JSONDecodeError as e:
        log.error("entry sidecar %s is unreadable (%s) — the reconciler will "
                  "ALERT on divergences instead of closing them until a fresh "
                  "buy/close cycle rebuilds it", path, e)
        append_event(JOURNAL_FILE, {
            "event": "entry_state_lost", "path": str(path), "reason": str(e),
        })
        d = {}
    if not isinstance(d, dict):      # valid JSON, wrong shape
        log.error("entry sidecar %s holds %s, not an object — ignoring it",
                  path, type(d).__name__)
        d = {}
    if not isinstance(d.get(_PENDING_KEY), dict):
        d[_PENDING_KEY] = {}
    if not isinstance(d.get(_OPENED_KEY), dict):
        d[_OPENED_KEY] = {}
    if not isinstance(d.get(_ADOPTED_KEY), dict):
        d[_ADOPTED_KEY] = {}
    return d


def save_aux(aux: dict, dry_run: bool = False) -> None:
    """Same atomic temp-file + os.replace discipline as save_state."""
    path = _aux_path(dry_run)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(aux, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

# Multi-strategy dispatch (dual-candidate rotation, or any future strategy) now
# lives in backtest.multi_strategy so the live engine and the dashboard chart
# share the exact same merge/dedup logic — see that module's docstring for the
# full design rationale.


# --------------------------------------------------------------------------- sizing
def _held_qty(positions, symbol: str) -> float:
    """Qty of `symbol` currently held in the live account (0 if not held)."""
    target = symbol.upper()
    for p in positions or []:
        if str(getattr(p, "symbol", "")).upper() == target:
            try:
                return abs(float(getattr(p, "qty", 0.0)))
            except (ValueError, TypeError):
                return 0.0
    return 0.0


def _round_qty(qty: float, symbol: str) -> float:
    """Whole shares for equities; BTC/ETH accepts fractional (4dp)."""
    if is_crypto_symbol(symbol):
        return round(qty, 4)
    return math.floor(qty)


# --------------------------------------------------------------------------- firing
def _sleeve_reserve(equity: float) -> float:
    """Dollars the put sleeve owns and the share book may not size against.

    reserve_frac x equity when the sleeve is enabled, and never less than the
    collateral actually pledged right now (the reserve is a policy number, the
    pledge is a fact at the broker; if the two disagree the fact wins). Any
    failure to read the policy falls back to the pledge alone, i.e. exactly the
    pre-reserve behaviour.
    """
    pledged = _pledged_collateral()
    try:
        from relay.csp_manager import config as _csp_config
        cfg = _csp_config()
        frac = float(cfg.get("reserve_frac") or 0.0) if cfg.get("enabled") else 0.0
    except Exception as e:                                  # noqa: BLE001
        log.warning("could not read the sleeve's reserve fraction (%s) — using the "
                    "live pledge alone", e)
        frac = 0.0
    return max(pledged, frac * max(0.0, equity))


def _pledged_collateral() -> float:
    """Dollars locked as collateral behind open short options, or 0.0.

    Imported lazily and swallowed whole: the options client is optional
    machinery and a failure to reach it must never stop the equity engine
    trading. Erring to 0.0 restores exactly the pre-options behaviour — the
    broker then remains the backstop, which is where it was before.
    """
    try:
        # NOT gated on csp.enabled. The flag governs whether a NEW put may be
        # sold; it says nothing about whether one is ALREADY open. Turning the
        # strategy off with a live short put would otherwise release its
        # collateral back to the equity book, which would then size an entry
        # against cash the broker has pledged — the exact conflict this
        # subtraction exists to prevent, reachable by editing a config flag.
        from relay.config import get_settings
        from relay.csp_manager import pledged_collateral
        from relay.options_client import OptionsClient
        return pledged_collateral(OptionsClient(get_settings()))
    except Exception as e:                                  # noqa: BLE001
        log.warning("could not read pledged option collateral (%s) — sizing off "
                    "full equity, as before options existed", e)
        return 0.0


def _cancel_working_entry(canon: str, client: AlpacaClient) -> list[str]:
    """Cancel any working BUY order for `canon`. Returns the ids cancelled.

    Called when the strategy wants out of a symbol the broker shows no position
    in — see fire_event's close branch. Only buy-side orders are touched: a
    working SELL is an exit already in flight and cancelling it would be
    undoing the very thing being asked for (_handle_pinned_exit owns that
    case, and only once there is a real session to re-close into).

    Best-effort throughout. Nothing here may raise into the poll loop, and a
    broker that will not list or cancel simply leaves the order alone — the
    pre-existing behaviour.
    """
    try:
        open_orders = client.get_open_orders(canon)
    except Exception as e:  # pragma: no cover - network
        log.warning("%s: open-order fetch failed while trying to pull an "
                    "unfilled entry: %s", canon, e)
        return []
    cancelled = []
    for o in open_orders or []:
        side = str(getattr(o, "side", "") or "").lower()
        if "buy" not in side:
            continue
        oid = getattr(o, "id", None)
        try:
            client.cancel_order(oid)
        except Exception as e:  # pragma: no cover - network
            log.warning("%s: cancel of unfilled entry %s failed: %s", canon, oid, e)
            continue
        cancelled.append(str(oid))
    if cancelled:
        log.error("%s: strategy exited while its own entry was still working — "
                  "cancelled %d unfilled buy order(s) rather than letting them "
                  "fill into an abandoned setup", canon, len(cancelled))
        append_event(JOURNAL_FILE, {
            "event": "entry_cancelled_on_exit", "ticker": canon,
            "order_ids": ",".join(cancelled),
            "reason": "the strategy signalled an exit while its entry order was "
                      "still unfilled; the entry was pulled so it cannot fill "
                      "into a setup the strategy has already left",
        })
    return cancelled


def fire_event(sym: str, ev: dict, cfg: dict, client: AlpacaClient,
               om: OrderManager, settings, dry_run: bool,
               aux: dict | None = None) -> dict:
    """Translate one strategy event into an OrderRequest and route it through
    the relay's OrderManager in-process. Returns a summary dict."""
    canon = canonical_symbol(sym)
    action = ev["action"]
    reason = ev.get("reason", "")
    # "ticker", not "symbol", and "bar_ts", not "ts".
    #
    # relay.logger.append_event builds `{"ts": time.time(), **event}` — the
    # spread comes LAST, so a caller-supplied "ts" silently overwrites the
    # wall-clock stamp. This one passed the strategy event's BAR timestamp, so
    # every signal_skip row in the journal is dated by the bar it describes
    # rather than when it was written: the file is not in ts order, and any
    # time-window query over it is wrong for these rows.
    #
    # The key mattered too. Every other journal event uses "ticker", so
    # ticker-filtered consumers — backtest.chart_view, and the weekly review's
    # per-symbol table — silently dropped every signal_skip. That is why the
    # 2026-08-08 review had to infer symbols out of the reason text.
    out = {"ticker": sym, "action": action, "reason": reason, "bar_ts": str(ev["ts"])}
    if "strategy" in ev:
        out["strategy"] = ev["strategy"]

    # Refresh live positions for the size/skip guards below.
    try:
        positions = client.get_all_positions()
    except Exception as e:  # network
        out["status"] = "skip"
        out["reason"] += f" | positions fetch failed: {e}"
        append_event(JOURNAL_FILE, {"event": "signal_skip", **out})
        return out
    held = _held_qty(positions, canon)

    if action == "buy":
        # Skip if we already hold this symbol (the backtest emits one buy per
        # cross-up cycle; a duplicate means state drifted, not a new signal).
        if held > 0:
            out["status"] = "skip"
            out["reason"] += f" | already long {canon} ({held})"
            append_event(JOURNAL_FILE, {"event": "signal_skip", **out})
            return out
        # THE SLEEVE'S SHARES ARE NOT THIS BOOK'S TO ROTATE OUT OF. OrderManager
        # rotates: a buy in a higher-priority symbol closes whatever else is
        # held first, and applied to a put assignment that would sell the
        # sleeve's stock because NVDA crossed a moving average. Marking them
        # protected makes the relay look straight through them, so the two
        # books trade independently on their own slices of the account — which
        # is what a reserve is for. (It used to BLOCK this entry instead, which
        # froze the whole share book for the life of every assignment.)
        om.protected_symbols = set((aux or {}).get(_ADOPTED_KEY, {}) or {})
        try:
            acct = client.get_account()
            equity = float(acct.equity)
        except Exception as e:
            out["status"] = "skip"
            out["reason"] += f" | account fetch failed: {e}"
            append_event(JOURNAL_FILE, {"event": "signal_skip", **out})
            return out
        # THE SHARE BOOK SIZES OFF EQUITY MINUS THE SLEEVE'S RESERVE. Alpaca
        # leaves `equity` unchanged when cash is pledged as put collateral, so
        # sizing off it alone made the two books claim the same dollars and let
        # a broker rejection decide which one won (2026-08-27 study). The
        # reserve (csp.json reserve_frac x equity, or the live pledge if that is
        # somehow larger) is the single arbitration point, and
        # relay.csp_manager.size_position sizes the put off the same number.
        equity = max(0.0, equity - _sleeve_reserve(equity))
        px = float(ev.get("price") or 0.0)
        if px <= 0:
            out["status"] = "skip"
            out["reason"] += " | no entry price"
            append_event(JOURNAL_FILE, {"event": "signal_skip", **out})
            return out
        qty = _round_qty(equity * cfg["deploy"] / px, sym)
        if qty <= 0:
            out["status"] = "skip"
            out["reason"] += f" | computed qty {qty} <= 0"
            append_event(JOURNAL_FILE, {"event": "signal_skip", **out})
            return out
        payload = OrderRequest(ticker=canon, action="buy", qty=qty, strategy="signal-engine",
                               price=px)
        out["qty"] = qty
        out["price"] = px      # lets a transiently-blocked entry be rebuilt

    elif action in ("sell", "close"):
        # ALL-OUT. `sell` used to be a HALF exit (qty = held / 2), which this
        # book does not do: one position at a time, entered at the full deploy
        # fraction and left in one piece (operator directive 2026-08-27).
        # Neither configured strategy has ever emitted `sell` — macd_sma and
        # bollinger_rsi both emit `close` — so this was dormant rather than
        # wrong in practice, but a dormant half-exit is a trapdoor: the day
        # something did emit one, the engine would have sold half a position
        # its own simulation then believed was fully closed. Routed to the same
        # full close as `close`, so there is exactly one way out. It also
        # inherits that branch's entry-cancel (audit EXIT-6): a sell arriving
        # while nothing is held pulls the still-working buy, where the old sell
        # branch journaled a skip and left it to fill.
        if held <= 0:
            # NOT HOLDING IS NOT THE SAME AS NOTHING TO DO. `held` counts
            # FILLED positions, but outside regular hours an entry is a resting
            # limit that routinely sits unfilled for hours — so a strategy that
            # buys and then exits within that window found the book flat and
            # skipped the close, leaving its own entry still working and free
            # to fill into a setup the strategy has already abandoned (audit
            # 2026-08-27, EXIT-6). The strategy wants out; the honest way to
            # honour that is to pull the entry that has not filled yet.
            # DRY-RUN MUST NOT REACH THE BROKER. This branch returns before
            # fire_event's own `if dry_run` gate further down, so without this
            # the one engine path that mutates broker state during a --dry-run
            # inspection would be a cancel (caught by the regression test
            # test_a_dry_run_never_cancels_a_real_order).
            cancelled = [] if dry_run else _cancel_working_entry(canon, client)
            out["status"] = "skip"
            out["reason"] += f" | not holding {canon}"
            if cancelled:
                out["cancelled_working"] = cancelled
                out["reason"] += (f" | cancelled {len(cancelled)} unfilled entry "
                                  f"order(s) instead")
            append_event(JOURNAL_FILE, {"event": "signal_skip", **out})
            return out
        payload = OrderRequest(ticker=canon, action="close", strategy="signal-engine")

    else:
        out["status"] = "skip"
        out["reason"] += f" | unknown action {action}"
        append_event(JOURNAL_FILE, {"event": "signal_skip", **out})
        return out

    if dry_run:
        out["status"] = "dry_run"
        log.info("DRY-RUN would fire: %s", out)
        return out

    try:
        result = om.handle(payload)
        out["status"] = result.get("status", "ok")
        log.info("fired: %s -> %s", out, out["status"])
    except RiskError as e:
        # Expected for flat-bias buys etc. — already journaled by handle().
        # `transient` separates "the wrong moment" (dedup window, an order still
        # working, a rotation exit still resting) from "a decision was taken"
        # (bias flat, off the allowlist, size ceiling). Only the first kind is
        # worth re-attempting — see _retry_pending_entry.
        out["status"] = "blocked"
        out["transient"] = bool(getattr(e, "transient", False))
        out["reason"] += f" | {e}"
        log.info("blocked: %s (%s)", out, e)
    except Exception as e:
        out["status"] = "error"
        out["reason"] += f" | {e}"
        log.error("order error: %s (%s)", out, e)
    return out


# --------------------------------------------------------------------------- main loop
# yfinance live-poll top-up: like sigengine.ibkr's _TOPUP_DURATION, a warm
# cache only needs a few days to cover the gap since the last poll, not the
# full lookback (_YF_PERIOD) every single call — see _fetch_bars.
_YF_TOPUP_PERIOD = {"5min": "5d", "15min": "5d", "30min": "5d", "1h": "15d", "1d": "60d"}

# Bar interval -> seconds, for the end-of-poll gap-backfill peer comparison
# (see _backfill_gaps). Only the intervals the engine actually trades appear.
_INTERVAL_SECONDS = {"5min": 300, "15min": 900, "30min": 1800, "1h": 3600, "1d": 86400}

# Bounds the whole concurrent fetch phase (see run_once). Real incident
# 2026-07-04: a yfinance fallback call for one symbol hung with no exception
# and no timeout of its own (ib_async's reqHistoricalData already has a
# timeout=60 default, but a stuck yfinance HTTP/session call does not surface
# one) — as_completed() with no timeout then waited on it forever, freezing
# run_once() and the entire poll loop for 41+ hours with zero new log lines.
# Generous vs poll_seconds (default 300s) so slow-but-healthy IBKR/yfinance
# calls are never falsely cut off.
_FETCH_PHASE_TIMEOUT = 90.0

# Source label for the weekend short-circuit, which returns the CACHE rather
# than fetching anything. It is not a vendor, and it must never be written back
# into the bar cache's `source` column: relay.bars_db.source_rank has no entry
# for it, so it ranks 0 — the same as yfinance — and a genuine IBKR bar could
# then be overwritten by a re-upsert of itself relabelled. See
# _fetch_and_recompute, which now skips the write entirely for this source.
_WEEKEND_CACHE_SOURCE = "cache-weekend-skip"


# A cached bar and a freshly-fetched bar for the SAME timestamp should agree to
# within rounding. When they disagree by more than this, the vendor has
# re-stated its history — a split, a reverse split, or a dividend adjustment.
#
# Why that is a correctness problem and not a curiosity: once a symbol's cache
# is warm the engine only asks for a top-up (IBKR "10 D", yfinance "15d"), and
# upsert_bars only ever touches timestamps present in that incoming frame. It
# has no DELETE and no rewrite path for anything older. But the strategies are
# fed the ENTIRE cached series (read_bars with no `since`). So after a split,
# bars from ~10-15 days back are silently left on the OLD price scale forever,
# and nothing will ever repair them. A 10:1 split leaves a 10x cliff in the
# middle of the series: every moving average, Bollinger band and stop level
# computed across it is nonsense, and the engine trades on it without a word.
# TQQQ and SOXL are both enabled here and split more often than most.
#
# 2% is far wider than float/rounding noise or a vendor revising one bar, and
# far narrower than any real split ratio (the smallest common one is 3:2).
_ADJUSTMENT_TOLERANCE = 0.02

# How many overlapping bars must disagree before the cache is condemned. One
# disagreeing bar is a vendor correcting a single bad print — normal, and
# upsert_bars already handles it. A re-adjustment moves the WHOLE overlap at
# once, so requiring a majority keeps a routine correction from triggering a
# full re-download.
_ADJUSTMENT_MIN_BARS = 3

# ---------------------------------------------------------------------------
# Single-bar junk rejection (2026-08-21). See _reject_outlier_bars.
#
# ALL THREE CONSTANTS WERE CALIBRATED AGAINST THE REAL CACHE (93,688 bars over
# 11 symbols and ~3 years), not chosen by eye. Two earlier designs were measured
# and thrown away, and both failures are the reason this one has the shape it
# does:
#
#   * "deviates >2% from neighbours" — percent cannot work. 2% is impossible
#     noise for MSFT and an ordinary hour for SOXL, which legitimately moved 14%
#     top-to-bottom on 2026-08-19.
#   * "deviates from the median of a 10-bar window" — wrong in a TREND, where a
#     bar legitimately sits away from the window median. Measured: it flagged
#     447 bars, including an ORCL bar whose range was SMALLER than its
#     neighbours', and MSFT's real 2026-04-29 earnings hour.
#   * excursion size alone — cannot separate junk from news. Real bars reach 12
#     to 25 (META's earnings hour 12.2, a BTC flash move 25.3) while the known
#     junk bar sits at 15.2. There is no threshold between them.
#
# What DOES separate them is what happens afterwards. A real move leaves the
# price at a NEW level (META 600 -> 578, BTC 73k -> 63k); a bad print leaves no
# trace at all (MSFT 484.4 before, 484.5 after, via a low of 450.97). So a bar
# is junk only when it is BOTH a violent excursion AND fully retraced.
#
# Excursion beyond the band spanned by the immediate neighbours' closes,
# measured in LOCAL bar ranges — local so an earnings hour is judged against
# earnings-hour volatility and a quiet post-market hour against quiet ones.
_OUTLIER_EXCURSION = 8.0

# How far the before/after medians may drift and still count as "the price came
# back", in the same local units.
_OUTLIER_DRIFT = 1.5

# Immediate neighbours forming the plausible band, and the window defining local
# volatility and the before/after levels.
_OUTLIER_NEAR = 2
_OUTLIER_LOCAL = 10

# Measured at these values: 3 of 93,688 real cached bars are rejected (0.0032%),
# while the bar that cost $30.51 scores excursion 15.2 and drift 0.18 — caught
# with ~2x margin on one test and ~8x on the other.

# Both guards re-examine the ENTIRE cached history every poll, so a finding is
# re-found every 300s for as long as the bar stays in the cache — which for a
# 3-year series is forever. Reporting it every time buried the journal: the
# first live poll wrote 33 rows, i.e. ~9,500/day, all of them re-statements of
# the same handful of facts (a 2025-07-07 holiday gap among them). These sets
# keep the SUPPRESSION unconditional while making the REPORT once-per-finding,
# the same shape _PENDING_EXIT_SEEN uses for the same reason. A restart reports
# each finding once more, which is the point — a fresh process should say what
# it is ignoring.
_OUTLIER_REPORTED: set[tuple[str, str]] = set()
_GAP_REPORTED: set[tuple[str, str]] = set()

# Bounds a single ibkr.fetch_bars() call specifically (see
# _ibkr_fetch_with_timeout). Real incident 2026-07-06: IBKR's crypto/PAXOS
# historical-data service silently hung with NO exception and NO data for
# BTC/USD, every poll cycle, for 15+ hours straight — confirmed via isolated
# testing to be IBKR/Gateway-side (a single-session-per-account crypto market
# data entitlement getting into a stuck state, `Error 162: Trading TWS session
# is connected from a different IP address` observed once), not a bug in this
# fetch/threading code. reqHistoricalData's own advertised timeout=60 does not
# reliably fire for this contract. Without a bound here, a stuck IBKR call
# consumes the ENTIRE _FETCH_PHASE_TIMEOUT and the symbol gets NO data at all
# that cycle (the yfinance fallback branch below is never even reached).
# 20s is generous vs every observed successful IBKR call today (~1-9s) and
# leaves ~70s of the 90s phase budget for the yfinance fallback to still
# complete within the SAME cycle. Since Python cannot force-kill a thread, an
# abandoned call keeps running (and keeps holding sigengine.ibkr's internal
# _LOCK) until/unless it ever actually returns — but because this wraps the
# WHOLE ibkr.fetch_bars() call including its own lock acquisition, a NEXT
# cycle stuck waiting on that same held lock also gets bounded to this same
# timeout, so every cycle falls back to yfinance within ~20s regardless of
# whether THIS cycle's call or an earlier orphaned one is the thing stuck.
_IBKR_CALL_TIMEOUT = 20.0

# Real incident 2026-07-06 (same day, same investigation): _IBKR_CALL_TIMEOUT
# and sigengine.ibkr's bounded _LOCK.acquire() together stop a single stuck
# IBKR call from freezing the engine — but if that call NEVER returns (a
# genuine, occasionally-occurring IBKR-side hang with no timeout of its own),
# _LOCK stays held FOREVER, and every future cycle correctly-but-uselessly
# fails over to yfinance via the now-permanently-busy lock. Confirmed live:
# once one call hung, EVERY cycle for 10+ minutes straight hit "lock busy" for
# 8 of 9 equities, with no path back to IBKR short of a manual restart.
# Python cannot force-kill the thread holding it — but the OS CAN reclaim an
# entire process, including any threads stuck in blocking native calls. This
# threshold (5min, vastly longer than any legitimate call — the slowest
# observed real SMART+OVERNIGHT double-fetch took under 10s) distinguishes a
# permanently abandoned holder from ordinary contention; once crossed, the
# engine restarts ITSELF (see _restart_self) rather than waiting for a human
# to notice and do it manually. State files (_state_path) already make this
# safe — a restart resumes from the last-processed baseline, firing nothing
# retroactively.
_LOCK_STUCK_RESTART_THRESHOLD = 300.0


def _lock_stuck_for_too_long(held_for: float | None) -> bool:
    """Pure predicate wrapping the threshold check, split out from the main
    loop so it's directly testable without needing to drive the infinite
    `while True` loop itself."""
    return held_for is not None and held_for > _LOCK_STUCK_RESTART_THRESHOLD


def _restart_self() -> None:
    """Re-exec this process in place (same PID, same argv) — the only way to
    actually reclaim a thread that's genuinely hung forever inside a blocking
    IBKR call (see _LOCK_STUCK_RESTART_THRESHOLD). os.execv replaces the
    process image without forking, so open file descriptors (the tmux pane's
    stdout, piped to `tee` in the usual launch command) survive the restart —
    logging continues to the same file uninterrupted."""
    log.error("IBKR lock has been stuck for over %ss (a call hung forever and "
              "will never release it) — self-restarting to recover a clean "
              "process instead of staying wedged indefinitely.",
              _LOCK_STUCK_RESTART_THRESHOLD)
    os.execv(sys.executable, [sys.executable, "-m", "sigengine.engine"] + sys.argv[1:])


def _detach_worker_threads(ex) -> None:
    """Let the interpreter exit even though `ex` has a worker wedged forever.

    Both executors here are abandoned with `shutdown(wait=False)` while a worker
    may still be inside a permanently hung IBKR call — a documented, recurring
    condition (see _LOCK_STUCK_RESTART_THRESHOLD: a 15+ hour PAXOS hang was
    observed live, and Python cannot force-kill the thread). Since Python 3.9
    ThreadPoolExecutor's workers are NON-daemon and registered in
    concurrent.futures.thread._threads_queues, and the module installs an
    atexit-style hook that JOINS every one of them. So an abandoned-but-hung
    worker does not just leak: it makes any orderly exit block forever.

    That matters more now than it did. The engine restarts itself on a stuck
    lock (_restart_self) and deploy/run_local.sh supervises it, but both of
    those need the process to actually reach exit — otherwise the result is a
    zombie that never dies and never restarts while web/server.py still reports
    "engine live" (it matches on /proc cmdline).

    Dropping our threads from that registry means they are simply not waited on;
    they stay parked in their blocking call until the process dies, which is the
    behavior the abandoning code already assumes. Touches a private CPython
    detail deliberately, so every step is guarded — failing to detach is no
    worse than today.
    """
    try:
        from concurrent.futures import thread as _cf_thread
        registry = getattr(_cf_thread, "_threads_queues", None)
        if registry is None:  # pragma: no cover - future CPython rename
            return
        for t in list(getattr(ex, "_threads", None) or ()):
            registry.pop(t, None)
    except Exception:  # pragma: no cover - never let cleanup break a poll
        pass


def _ibkr_fetch_with_timeout(sym: str, interval: str):
    """ibkr.fetch_bars(sym, interval), bounded to _IBKR_CALL_TIMEOUT.

    Returns the fetched DataFrame, or None on ANY failure mode — a clean
    "not subscribed"/connection error (same as calling ibkr.fetch_bars
    directly), OR a hang that exceeded the timeout (logged distinctly as
    "stuck", since that's a different, real IBKR-side reliability problem
    worth being able to grep for separately from a normal fallback)."""
    from sigengine import ibkr
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(ibkr.fetch_bars, sym, interval)
    try:
        return fut.result(timeout=_IBKR_CALL_TIMEOUT)
    except FutureTimeoutError:
        log.warning("%s: IBKR call exceeded %ss (stuck) — abandoning for this "
                    "cycle, falling back", sym, _IBKR_CALL_TIMEOUT)
        # The abandoned call's thread keeps running in the background forever
        # (Python cannot force-kill it) and may leave this SYMBOL's own IBKR
        # connection object in a confused state even though its TCP socket
        # stays "connected" — mark it suspect so the NEXT call for this
        # symbol forces a fresh disconnect+reconnect instead of blindly
        # reusing it (see sigengine.ibkr.mark_suspect's docstring — per-symbol
        # connections since the 2026-07-06 rewrite, so this can't affect any
        # OTHER symbol).
        ibkr.mark_suspect(sym)
        return None
    except Exception as e:
        log.warning("%s: ibkr.fetch_bars raised %s", sym, e)
        return None
    finally:
        ex.shutdown(wait=False)
        _detach_worker_threads(ex)


class DataUnavailable(RuntimeError):
    """No bars could be loaded for a symbol this cycle.

    Exists purely to be an ordinary Exception. backtest.ema_scaled_rolling
    signals "the vendor returned an empty frame" by raising SystemExit — correct
    for a CLI, fatal for a daemon: SystemExit derives from BaseException, so it
    is NOT caught by `except Exception`, and it killed this engine on
    2026-08-19.

    The exact path: a DNS outage made yfinance return empty frames for every
    symbol (yfinance swallows network errors and returns an empty DataFrame
    rather than raising), _load_with_retry exhausted its 4 attempts and
    re-raised the SystemExit, ThreadPoolExecutor's worker stored it on the
    future (its own handler catches BaseException), fut.result() re-raised it on
    the MAIN thread, and BOTH `except Exception` guards — the per-symbol one and
    the main loop's "never let the daemon die on a poll error" — let it through.
    CPython then printed one bare line with no timestamp, level or traceback:

        no data for AMZN (yfinance 30m)

    and exited. The engine was dead for 94 minutes with nothing to notice it:
    no stop was enforced and no exit signal fired in that window. Converting it
    here restores the guarantee those handlers already claim to give.
    """


def _load_with_retry(sym: str, interval: str, source: str, retries: int = 4,
                     delay: float = 1.5, yf_period_override: str | None = None):
    """yfinance intermittently returns 'possibly delisted; no price data' for a
    symbol that is perfectly fine — retry a few times before giving up.

    align_hourly=True: everything this loads gets cached (see
    _fetch_and_recompute -> upsert_bars), and this engine is the ONLY writer to
    the shared bar cache, so it is the one place that must not put yfinance's
    09:30-anchored hourly bars on IBKR's :00 grid. See
    backtest.ema_scaled_rolling._yf_hourly_aligned for what that cost the
    warm-up window before this was fixed (2026-07-14).

    prepost=True: the IBKR primary's bars cover extended hours, so the fallback
    must too — without it, an IBKR historical outage outside RTH freezes the
    equity cache entirely (2026-08-05: the ushmds farm wedged for 15h and no
    equity gained a single bar through the whole overnight + pre-market
    stretch). yfinance still has no overnight-session (20:00-04:00 ET) data,
    so that window remains IBKR-only regardless."""
    last = None
    for _ in range(retries):
        try:
            return load_ohlcv(sym, interval, source,
                              yf_period_override=yf_period_override,
                              align_hourly=True, prepost=True)
        except SystemExit as e:
            last = e
            time.sleep(delay)
    # NOT `raise last`: that would propagate a BaseException out of the poll and
    # kill the daemon outright (see DataUnavailable).
    raise DataUnavailable(f"{sym} {interval} via {source}: {last}") from last


def _fetch_bars(sym: str, cfg: dict, data_cfg: dict) -> tuple[pd.DataFrame, str]:
    """Try the configured primary (IBKR), then the fallback (yfinance).
    Returns (df, source_label). Raises only if BOTH paths fail for a symbol.

    Crypto: `crypto_source` picks the path. "ibkr" tries IBKR's Crypto/PAXOS
    contract first (added 2026-07-03 — sigengine.ibkr no longer skips crypto
    outright) then falls back to yfinance on any failure, mirroring the
    equity primary/fallback behavior below. Any other value (e.g. "yfinance",
    the default) is used directly with no IBKR attempt, unchanged from
    before. IBKR crypto data requires its own market-data entitlement,
    separate from the equity one, AND (confirmed 2026-07-06) only tolerates
    ONE active session per account at a time — `Error 162: Trading TWS
    session is connected from a different IP address` observed when a second
    client briefly contended for it. Once that entitlement gets into a stuck
    state, IBKR's crypto historical-data service can hang indefinitely with
    NO exception and NO data — confirmed via direct isolated testing this is
    IBKR/Gateway-side, not a bug here, and it does NOT self-heal even once
    the contending session is gone. Both IBKR legs (crypto and equity, below)
    go through `_ibkr_fetch_with_timeout` specifically so a stuck call can
    never consume the whole poll cycle — see that function's docstring.

    Only pulls the FULL lookback window from yfinance on a cold cache (first
    poll ever for this symbol/interval); once warm, pulls a small top-up —
    same fix as sigengine.ibkr's cold/topup duration split, for the same
    reason (repeatedly re-fetching 60 days of history every 60s does nothing
    useful once the cache already has it).

    Equities skip the network entirely over the weekend (Fri 20:00 ET -> Mon
    04:00 ET, see relay.risk_filter.equity_market_closed_for_weekend) and
    return whatever's already cached, source label "cache-weekend-skip" —
    added 2026-07-04 to stay well clear of IBKR's 60-requests-per-10-min cap
    and because there's nothing to fetch anyway. Crypto is unaffected."""
    interval = cfg["interval"]
    yf_period = None if bar_count(sym, interval) == 0 else _YF_TOPUP_PERIOD.get(interval)

    # Skip the network entirely for equities over the weekend (Fri 20:00 ET ->
    # Mon 04:00 ET) — no bar can possibly have changed, so polling IBKR/yfinance
    # during it only burns request budget (IBKR's 60-per-10-min cap) for zero
    # information. Crypto is exempt (24/7). Falls back to whatever's already
    # cached; if the cache is cold (e.g. a brand new symbol added over a
    # weekend), still fetch once so it isn't left empty until Monday.
    if not is_crypto_symbol(sym) and equity_market_closed_for_weekend() and bar_count(sym, interval) > 0:
        return read_bars(sym, interval), _WEEKEND_CACHE_SOURCE

    if is_crypto_symbol(sym):
        crypto_source = data_cfg.get("crypto_source", "yfinance")
        if crypto_source == "ibkr":
            df = _ibkr_fetch_with_timeout(sym, interval)
            if df is not None and not df.empty:
                return df, "ibkr"
            log.info("%s: IBKR crypto unavailable -> yfinance fallback", sym)
            return _load_with_retry(sym, interval, "yfinance", yf_period_override=yf_period), "yfinance"
        return _load_with_retry(sym, interval, crypto_source, yf_period_override=yf_period), crypto_source

    primary = data_cfg.get("primary", "ibkr")
    fallback = data_cfg.get("fallback", "yfinance")
    if primary == "ibkr":
        df = _ibkr_fetch_with_timeout(sym, interval)
        if df is not None and not df.empty:
            return df, "ibkr"
        log.info("%s: IBKR unavailable -> %s fallback", sym, fallback)
    return _load_with_retry(sym, interval, fallback, yf_period_override=yf_period), fallback


def _reject_outlier_bars(sym: str, incoming):
    """Drop single junk bars before they reach the cache.

    WHAT THIS IS FOR. On 2026-08-19, with IBKR down and the engine on the
    yfinance fallback, ONE MSFT bar came back as high 503.58 / low 450.97 /
    close 474.47 against a true 485.29 / 483.34 / 484.26. That bar alone
    fabricated a bollinger_rsi entry — the fake plunge put price under the lower
    band and RSI at 24, and the "recovery" on the next bar (the price simply
    being where it had always been) completed a textbook mean-reversion buy. The
    trade lost $30.51. The SAME junk reached the order path as a quote of 504.45,
    and there `relay.order_manager.MAX_QUOTE_DEVIATION` rejected it on sight.
    Bars had no equivalent check; this is it.

    Not covered by _cache_needs_readjustment, which is the split detector: that
    deliberately requires _ADJUSTMENT_MIN_BARS bars to move together, precisely
    so one corrected print does not trigger a re-download. A lone bad bar is the
    case it is built to ignore.

    THE TEST IS RELATIVE, NOT ABSOLUTE, and it is two-sided. A bar is junk when
    its high or low sits more than _OUTLIER_RANGE_MULT robust bar-ranges from
    the neighbourhood — where the neighbourhood is measured BEFORE and AFTER the
    candidate separately, and the two sides must agree with each other first.
    Each part is load-bearing:
      * median close of neighbours, so one bad bar cannot define "normal";
      * the median bar RANGE as the unit, so the threshold self-scales — a
        percent threshold tight enough to catch this would fire constantly on
        SOXL, which legitimately moved 14% top-to-bottom the same day;
      * BOTH SIDES, because that is the only thing separating a bad print from
        real news. A spike is unconfirmed: the bars after it return to where the
        bars before it were. A genuine repricing — a gap-down, a halt reopening
        — is confirmed: the after-side stays at the new level. Caught by its own
        test, which showed a one-sided version rejecting the two boundary bars
        of an honest 6% step, i.e. deleting real prices at exactly the moment
        the series mattered most.

    A bar with no after-side yet is never judged, because the confirmation it
    needs has not arrived — that means the newest `_OUTLIER_NEAR` rows, and
    nothing more (the loop below clips its local window at the edges instead of
    skipping a whole `_OUTLIER_LOCAL` of them; see the comment there). Those
    get judged on the next poll, and signals never depend on the very last one
    anyway — _events_from_cached drops the still-forming bar.

    Junk is DROPPED, never repaired. Inventing a plausible replacement would put
    a fabricated price into the series the engine trades on, and the hole a drop
    leaves is itself meaningful: _gap_blocked_events refuses to emit signals
    whose crossing spans it, so the engine goes quiet on this symbol instead of
    trading through a region it knows is damaged. The next poll re-fetches; a
    bar that was merely a transient vendor glitch comes back clean and is kept.

    Returns (clean_frame, dropped_index).
    """
    need = {"high", "low", "close"}
    if incoming is None or not need.issubset(getattr(incoming, "columns", [])):
        return incoming, []
    n = len(incoming)
    near, loc = _OUTLIER_NEAR, _OUTLIER_LOCAL
    if n < 2 * loc + 1:
        return incoming, []

    close = incoming["close"].to_numpy(dtype=float)
    high = incoming["high"].to_numpy(dtype=float)
    low = incoming["low"].to_numpy(dtype=float)
    rng = np.abs(high - low)
    # Floor the scale so a dead-flat series (a halted or barely-traded name)
    # cannot drive the tolerance to zero and condemn everything after it.
    floor = float(np.median(close)) * 0.0005
    bad, why = [], {}
    # JUDGE EVERY BAR THAT HAS NEIGHBOURS ON BOTH SIDES, with the local window
    # TRUNCATED at the edges rather than skipped (2026-08-27, audit E3/DG-2).
    #
    # This loop used to run `range(loc, n - loc)`, i.e. it skipped the newest
    # TEN rows, not the newest one as the docstring below claims. Since
    # _events_from_cached drops exactly one still-forming bar, bars 1..9 back
    # from the live edge were upserted unscreened and traded on immediately —
    # a ~10-hour unjudged window on 1h bars, and precisely the window a
    # fabricated entry fires in. The 2026-08-19 MSFT trade is the shape of it:
    # junk at 20:00, the buy on the 21:00 bar, and the guard would not have
    # looked at either for another ten hours.
    #
    # The confirmation this test needs is the AFTER side, and `near` bars of it
    # is enough to form the band; `loc` only sets the volatility scale and the
    # before/after medians, all of which tolerate a shorter window. So the
    # bound moves to `near` and the windows are clipped. Bars with no after
    # side at all are still never judged — that part of the docstring was and
    # remains true.
    for i in range(near, n - near):
        lo, hi = max(0, i - loc), min(n, i + loc + 1)
        band = np.concatenate([close[i - near:i], close[i + 1:i + near + 1]])
        band_lo, band_hi = float(band.min()), float(band.max())
        scale = max(float(np.median(rng[lo:hi])), floor)
        if not (scale > 0):
            continue
        excursion = max(high[i] - band_hi, band_lo - low[i], 0.0) / scale
        if excursion <= _OUTLIER_EXCURSION:
            continue
        before = float(np.median(close[lo:i]))
        after = float(np.median(close[i + 1:hi]))
        drift = abs(before - after) / scale
        if drift > _OUTLIER_DRIFT:
            continue                 # the price is somewhere new: news, not junk
        ts = incoming.index[i]
        bad.append(ts)
        why[ts] = (excursion, drift)

    if not bad:
        return incoming, []
    # The drop is unconditional; only the REPORT is deduped (see _OUTLIER_REPORTED).
    fresh = [t for t in bad if (sym, str(t)) not in _OUTLIER_REPORTED]
    for ts in fresh:
        e, d = why[ts]
        _OUTLIER_REPORTED.add((sym, str(ts)))
        log.error("%s: rejecting bar %s as a bad print — H %.4f L %.4f is %.1f "
                  "local ranges outside its neighbours and the price came right "
                  "back (drift %.2f); dropping it rather than trading on it",
                  sym, ts, float(incoming.at[ts, "high"]),
                  float(incoming.at[ts, "low"]), e, d)
    if fresh:
        append_event(JOURNAL_FILE, {
            "event": "bar_rejected_outlier", "ticker": canonical_symbol(sym),
            "bars": [str(t) for t in fresh], "count": len(fresh),
            "excursion": round(max(why[t][0] for t in fresh), 2),
            "drift": round(min(why[t][1] for t in fresh), 3),
            "reason": "a violent high/low excursion that left no trace — the "
                      "price before and after the bar is the same, so the move "
                      "never happened; a single such print fabricates indicator "
                      "crossings",
        })
    return incoming.drop(index=bad), bad


def _cache_needs_readjustment(sym: str, interval: str, incoming) -> float | None:
    """Has the vendor re-stated history since the cache was written?

    Compares the freshly-fetched frame against the cache on the timestamps they
    SHARE. Agreement means the two price scales match and the older cached bars
    are still valid. A consistent disagreement across the overlap means they do
    not, and every cached bar older than the overlap is on a dead scale (see
    _ADJUSTMENT_TOLERANCE).

    Returns the median incoming/cached close ratio when a re-adjustment is
    detected, else None. The ratio is returned rather than a bool purely so the
    log can name it — a value near 0.1 reads as "10:1 split" at a glance.

    Requires _ADJUSTMENT_MIN_BARS disagreeing bars so that one corrected print
    cannot trigger a full re-download, and uses the MEDIAN so a single outlier
    cannot skew the reported ratio.
    """
    if incoming is None or len(incoming) == 0:
        return None
    try:
        cached = read_bars(sym, interval)
        if cached.empty:
            return None
        shared = cached.index.intersection(incoming.index)
        if len(shared) < _ADJUSTMENT_MIN_BARS:
            return None
        old = cached.loc[shared, "close"].astype(float)
        new = incoming.loc[shared, "close"].astype(float)
        both = (old > 0) & (new > 0)
        if int(both.sum()) < _ADJUSTMENT_MIN_BARS:
            return None
        ratio = (new[both] / old[both])
        disagreeing = int(((ratio - 1.0).abs() > _ADJUSTMENT_TOLERANCE).sum())
        if disagreeing < _ADJUSTMENT_MIN_BARS or disagreeing * 2 <= int(both.sum()):
            return None
        return float(ratio.median())
    except Exception as e:  # pragma: no cover - defensive, never break a poll
        log.warning("%s: adjustment check failed (%s) — leaving cache alone", sym, e)
        return None


def _fetch_and_recompute(sym: str, cfg: dict, data_cfg: dict):
    """Fetch bars (IBKR primary -> yfinance fallback), cache to SQLite, and
    recompute the full strategy event history over the cached warm-up window.

    Returns (events, last_closed_ts, source), or None if there aren't yet
    enough cached bars to act on. Side effects are limited to the (now
    lock-serialized, thread-safe) bar cache — no Alpaca account interaction —
    which is what makes this safe to run CONCURRENTLY across symbols (see
    run_once): this is the slow, network-bound part of a poll (one or two
    HTTP round-trips per symbol, run sequentially today takes ~2-3s per
    symbol x N symbols), while diffing + order-firing must stay strictly
    sequential (below) to preserve the one-position-at-a-time rotation
    invariant — two symbols' buy signals racing the SAME account/position
    check concurrently could both pass it before either fill lands."""
    df, source = _fetch_bars(sym, cfg, data_cfg)
    # Cache the freshly fetched bars (UTC-normalized) so mixed sources compare
    # consistently and warm-up is instant on restart.
    #
    # The weekend short-circuit is exempt: `df` there IS the cache, just read
    # back, so writing it returns zero information — and writing it does active
    # harm. Friday 20:00 ET -> Monday 04:00 ET is ~60 hours, i.e. ~720 polls at
    # poll_seconds=300, each re-upserting EVERY cached row for every equity
    # (thousands of rows each) purely to rewrite them with the same values; and
    # each write relabels the `source` column to "cache-weekend-skip", which
    # source_rank scores 0, demoting bars that were originally IBKR.
    if source == _WEEKEND_CACHE_SOURCE:
        return _events_from_cached(sym, cfg, df, source)

    # Drop single junk bars BEFORE anything else looks at the frame — including
    # the split detector, which compares medians and would be skewed by one wild
    # print, and the upsert, which would otherwise put it in the series the
    # engine trades on (see _reject_outlier_bars).
    df, dropped = _reject_outlier_bars(sym, df)
    # ACT on the rejection, do not merely filter it out of this frame. Before
    # 2026-08-27 the return value was discarded, so a junk bar that had already
    # been cached on an earlier poll stayed there forever — upsert_bars has no
    # DELETE path and only ever touches timestamps present in an incoming
    # frame, so nothing could ever retire it and the strategies kept replaying
    # it (audit DG-3). Every condemned timestamp is inside the window we just
    # fetched, so the next poll re-offers it: a clean bar comes straight back
    # and a bad one is condemned again. See bars_db.delete_bars_at.
    if len(dropped):
        purged = delete_bars_at(sym, cfg["interval"], dropped, source=source)
        if purged:
            log.error("%s: purged %d previously-cached copy/copies of the "
                      "rejected bar(s) so the strategies stop replaying them",
                      sym, purged)

    # Repair a split/dividend re-statement BEFORE writing, or the write would
    # leave the series half on each price scale (see _cache_needs_readjustment).
    ratio = _cache_needs_readjustment(sym, cfg["interval"], df)
    if ratio is not None:
        removed = delete_bars(sym, cfg["interval"])
        log.error("%s: vendor re-stated history (incoming/cached close ratio "
                  "%.4f — split or dividend adjustment); purged %d stale cached "
                  "bar(s) and re-fetching the full window cold",
                  sym, ratio, removed)
        append_event(JOURNAL_FILE, {
            "event": "bar_cache_readjusted", "ticker": canonical_symbol(sym),
            "interval": cfg["interval"], "ratio": round(ratio, 6),
            "bars_purged": removed,
            "reason": "fetched bars disagreed with cached bars on shared timestamps; "
                      "cache was on a pre-adjustment price scale",
        })
        # Cold re-fetch: bar_count is now 0, so _fetch_bars asks for the FULL
        # lookback instead of the top-up that cannot reach the stale rows. It
        # gets the same junk-bar screening as the first fetch — this frame is
        # about to become the ENTIRE cached history for the symbol, so a bad
        # print here would be even more damaging than in a top-up.
        df, source = _fetch_bars(sym, cfg, data_cfg)
        df, _dropped = _reject_outlier_bars(sym, df)

    try:
        upsert_bars(sym, cfg["interval"], df, source)
    except Exception as e:
        # NOT "proceeding on fetched df" — that comment was wrong, and
        # dangerously so: the very next statement re-read the CACHE, i.e. the
        # bars that had just failed to update. Every downstream decision
        # (last_closed_ts, the event diff) then came from data the engine knew
        # was stale, with only a WARNING to say so. Skip the symbol instead:
        # returning None is the same "no opinion this cycle" signal a failed
        # fetch gives, so the reconcile pass also stays quiet about it rather
        # than acting on a stale view.
        log.error("%s: bar cache upsert failed (%s) — SKIPPING this symbol for this "
                  "cycle rather than computing signals from the un-updated cache", sym, e)
        return None
    # Run the backtest over the full cached window (warm-up history lives here).
    return _events_from_cached(sym, cfg, read_bars(sym, cfg["interval"]), source)


def _bar_step(interval: str) -> pd.Timedelta:
    """The nominal spacing of one bar."""
    return pd.Timedelta(interval.replace("min", "min").replace("h", "h"))


def _slot_is_normally_populated(index, missing, step) -> bool:
    """Does this symbol USUALLY have a bar at this clock slot?

    A gap is only suspicious if data that should exist is absent. Equities gap
    every weekend and every night the symbol does not trade overnight, and those
    gaps are not damage — blocking on them would silence the engine permanently.
    Rather than model market calendars (holidays, half-days, per-symbol overnight
    eligibility, all of which drift), ask the series about itself: look at the
    same weekday-and-hour on the previous three weeks, and call the slot normal
    if it was populated at least twice.

    A holiday therefore reads as anomalous, because the same slot was populated
    on ordinary weeks. That is the right way to be wrong: the market is shut, so
    suppressing signals costs nothing.
    """
    # PROBE IN EXCHANGE-LOCAL WALL CLOCK, NOT IN UTC.
    #
    # "The same weekday and hour" is a statement about the SESSION, and a
    # session is defined in New York time. In UTC it moves by an hour twice a
    # year, and the acceptance window here is only step/2 — 30 minutes on the
    # configured 1h bars — so a probe reaching back across a DST changeover
    # landed 60 minutes away and found nothing. Every slot then looked
    # abnormal, and for the ~2 weeks it takes all three weekly probes to clear
    # the boundary the guard answered this question wrongly (audit 2026-08-27,
    # DG-4). The old comment claimed step/2 "tolerated the DST shift"; it is
    # exactly half of what that needs.
    #
    # Converting to ET and dropping the offset makes the subtraction a
    # WALL-CLOCK one, so the probe lands on the same session hour by
    # construction and the window stays tight. Widening the window instead was
    # tried and is wrong: +/-1h leaks across midnight into the adjacent trading
    # day, so a Saturday hole matches Friday 23:00 and the weekend starts
    # looking "normally populated" — which would suppress every Monday entry.
    #
    # A non-tz index (a hand-built test frame) falls back to the raw values,
    # which is the pre-2026-08-27 behaviour.
    try:
        local = index.tz_convert(EASTERN).tz_localize(None)
        here = pd.Timestamp(missing).tz_convert(EASTERN).tz_localize(None)
    except (TypeError, AttributeError):                     # naive index
        local, here = index, missing

    hits = probes = 0
    for weeks in (1, 2, 3):
        probe = here - pd.Timedelta(days=7 * weeks)
        if probe < local[0]:
            continue
        probes += 1
        near = local[(local >= probe - step / 2) & (local < probe + step / 2)]
        if len(near):
            hits += 1
    return probes > 0 and hits >= min(2, probes)


def _gap_blocked_events(sym: str, cfg: dict, df, events: list[dict]) -> list[dict]:
    """Drop signals whose crossing is an artifact of missing data.

    WHY. Every indicator here — RSI, MACD, Bollinger — crosses on bar ADJACENCY,
    never on elapsed time. When bars are missing, the two rows either side of the
    hole are treated as consecutive, and the whole move across the gap is read as
    one bar's move. That is not a subtle distortion: on 2026-08-20 MSFT's exit
    fired exactly this way. With IBKR down there were no overnight bars, so the
    series jumped from 08-19 23:00 UTC to 08-20 08:00 UTC, and nine hours of
    ordinary drift (484.86 -> 482.50) presented as a single-bar crossunder of the
    upper band with RSI crossing down through 50 — both rsi_bb_short conditions
    at once, neither of them real.

    Only the CROSSING bar is checked, not the whole lookback: a crossover is a
    comparison between a bar and the one before it, so that is where a hole
    corrupts the answer. Requiring an unbroken lookback instead would block every
    signal for a full BB window after any gap, including the weekend, which would
    stop the engine trading altogether.

    Gaps that are normal for this symbol are allowed through — see
    _slot_is_normally_populated. Blocked signals are journaled, not silently
    dropped: the whole failure mode here is decisions made on data nobody
    inspected.

    ENTRIES ONLY (2026-08-27, audit EXIT-1/DG-5). Until then this filtered every
    event, and suppressing an EXIT turned out to be the most dangerous thing the
    engine could do. Three reasons, in order of weight:

      1. A `stop` is not a crossing at all. It is a level test —
         `c <= entry_price * (1 - stop_pct)` — comparing one bar to a stored
         number, so a hole in the series cannot corrupt it. It was being
         suppressed on a rule that has no bearing on it. And for an equity, the
         first bar of a session is BY CONSTRUCTION a bar whose predecessor is
         missing, so this fired on the normal case, not an edge one: of the 131
         suppressions in the live journal, 110 were exits and 108 of those were
         stops.

      2. Suppressing an exit DESYNCHRONISES the strategy from the book,
         permanently. The strategy's own simulated position goes flat at that
         bar whatever we do with the event — it is a pure function of the
         series — so it will never re-emit that exit, nor any later stop for
         that position. The real position then rides with no protection until
         an entirely new buy->close cycle completes.

      3. The safety net could not see it either, because _events_from_cached
         returns this list and _reconcile_positions replays that same object
         into _simulated_position. A suppressed close left sim_long True, so the
         "real long + sim flat" branch never ran and the one mechanism written
         to catch a missed exit was fed the version of history in which the exit
         never happened.

    The asymmetry decides it, and it is the same asymmetry _reconcile_positions
    already states for itself: automatic action may only ever REDUCE exposure. A
    gap that fabricates an ENTRY commits new capital on a price that never
    traded — unbounded, and worth guarding. A gap that fabricates an EXIT closes
    a position early, which costs some upside and is bounded. Letting a
    fabricated exit through is the cheap error; holding an unprotected position
    is not.

    NOTE this does re-admit the fabricated MSFT exit of 2026-08-20 (see
    bad-bars-fabricate-signals). That is accepted deliberately: the entry was
    the real defect there — the position should never have been open — and that
    side is still guarded, by both this function and _reject_outlier_bars.
    """
    if not events or len(df) < 2:
        return events
    step = _bar_step(cfg["interval"])
    index = df.index
    kept, blocked = [], []
    for e in events:
        if e.get("action") in ("close", "sell"):
            kept.append(e)
            continue
        ts = e.get("ts")
        try:
            i = index.get_loc(ts)
        except KeyError:
            kept.append(e)
            continue
        if not isinstance(i, int) or i == 0:
            kept.append(e)
            continue
        actual = index[i] - index[i - 1]
        if actual <= step * 1.5:                    # consecutive; nothing to do
            kept.append(e)
            continue
        # walk the slots the gap swallowed and ask whether they normally exist
        holes = []
        t = index[i - 1] + step
        while t < index[i] and len(holes) < 64:
            holes.append(t)
            t = t + step
        if any(_slot_is_normally_populated(index, h, step) for h in holes):
            blocked.append((e, actual))
        else:
            kept.append(e)

    # The suppression is unconditional; only the REPORT is deduped, or every
    # poll re-announces the same historical gaps forever (see _GAP_REPORTED).
    for e, actual in blocked:
        key = (sym, str(e.get("ts")))
        if key in _GAP_REPORTED:
            continue
        _GAP_REPORTED.add(key)
        log.error("%s: SUPPRESSING %s/%s at %s — the previous bar is %s earlier, "
                  "not %s, so this 'crossing' is a data gap rather than a price "
                  "move", sym, e.get("strategy"), e.get("reason"), e.get("ts"),
                  actual, step)
        append_event(JOURNAL_FILE, {
            "event": "signal_gap_blocked", "ticker": canonical_symbol(sym),
            "action": e.get("action"), "strategy": e.get("strategy"),
            "signal_reason": e.get("reason"), "bar_ts": str(e.get("ts")),
            "gap": str(actual), "expected": str(step),
            "reason": "indicator crossings compare adjacent BARS, not adjacent "
                      "times; the bar before this one is missing, so the move "
                      "across the hole was read as a single-bar move",
        })
    return kept


def _events_from_cached(sym: str, cfg: dict, df, source: str):
    """Shared tail of _fetch_and_recompute: run the strategies over `df` and
    return (events, last_closed_ts, source), or None if there is not yet enough
    history. Split out so the weekend short-circuit — which already holds the
    cached frame and must NOT write it back — reaches the identical code path
    instead of a near-copy that could drift."""
    # Act on CLOSED bars only — the last row is the in-progress candle.
    if len(df) < 2:
        return None
    df = df.iloc[:-1]
    last_closed_ts = df.index[-1]
    events = _gap_blocked_events(sym, cfg, df, _run_symbol_strategies(df, cfg, sym))
    return events, last_closed_ts, source


def _diff_and_fire(sym: str, cfg: dict, state: dict, events: list[dict],
                   last_closed_ts, source: str, client: AlpacaClient,
                   om: OrderManager, settings, dry_run: bool,
                   quiet: bool = False, aux: dict | None = None) -> list[dict]:
    """Diff precomputed events against last-processed state and fire anything
    new. Must run SEQUENTIALLY across symbols (see _fetch_and_recompute) —
    this is where real orders get placed.

    `aux` is the entry-bookkeeping sidecar (see _aux_path). Passing None keeps
    the pre-2026-08-27 behaviour exactly — no retries, no opened-record — which
    is what the standalone/debug callers and older tests want."""
    prev = state.get(sym)
    if prev is None:
        # Warm-up: record the baseline closed bar and fire nothing. Pre-existing
        # signals that formed before the engine started do NOT fire.
        state[sym] = last_closed_ts.isoformat()
        if not quiet:
            log.info("warm-up %s: baseline=%s source=%s (firing nothing)", sym, state[sym], source)
        return []

    prev_ts = pd.Timestamp(prev)
    # eod is a backtest-window artifact (close at end of data); ignore it live.
    new = [e for e in events if e["ts"] > prev_ts and e.get("reason") != "eod"]
    new = _drop_stale_entries(sym, cfg, new, last_closed_ts)

    # A previously-blocked entry gets its retry BEFORE this poll's own events,
    # so that if both exist the older signal is resolved first and the newer
    # one still sees an accurate book (audit E2).
    fired = _retry_pending_entry(sym, cfg, aux, events, new, last_closed_ts,
                                 client, om, settings, dry_run)

    if not new:
        # No new signals, but advance state to the latest closed bar.
        state[sym] = last_closed_ts.isoformat()
        return fired

    if not quiet:
        log.info("%s: %d new event(s) since %s (source=%s)", sym, len(new), prev, source)
    fired += _fire_new_events(sym, new, cfg, client, om, settings, dry_run, aux=aux)
    _record_entry_outcomes(sym, cfg, aux, fired, last_closed_ts, dry_run)
    state[sym] = last_closed_ts.isoformat()
    return fired


def _drop_stale_entries(sym: str, cfg: dict, new: list[dict], last_closed_ts) -> list[dict]:
    """Refuse catch-up ENTRIES that are older than _CATCHUP_ENTRY_MAX_BARS.

    Exits pass through untouched — see the constant. Journaled rather than
    silently dropped, because a decision not to trade is still a decision.
    """
    if not new:
        return new
    step = _bar_step(cfg["interval"])
    kept = []
    for e in new:
        if e.get("action") != "buy":
            kept.append(e)
            continue
        try:
            age = (pd.Timestamp(last_closed_ts) - pd.Timestamp(e["ts"])) / step
        except Exception:                                   # noqa: BLE001
            kept.append(e)
            continue
        if age <= _CATCHUP_ENTRY_MAX_BARS:
            kept.append(e)
            continue
        log.error("%s: NOT firing the %s entry from %s — it is %.0f bars old "
                  "(limit %d). Its price is stale, so sizing from it would open "
                  "a position the market has already moved away from.",
                  sym, e.get("reason"), e.get("ts"), age, _CATCHUP_ENTRY_MAX_BARS)
        append_event(JOURNAL_FILE, {
            "event": "stale_entry_skipped", "ticker": canonical_symbol(sym),
            "action": "buy", "strategy": e.get("strategy"),
            "signal_reason": e.get("reason"), "bar_ts": str(e.get("ts")),
            "age_bars": round(float(age), 1), "max_bars": _CATCHUP_ENTRY_MAX_BARS,
            "reason": "catch-up entry older than the staleness limit; it would be "
                      "sized from a price that is no longer the market",
        })
    return kept


def _record_entry_outcomes(sym: str, cfg: dict, aux: dict | None, fired: list[dict],
                           last_closed_ts, dry_run: bool) -> None:
    """Update the sidecar from what this poll actually did.

    Two records, for two different questions (see _aux_path):
      * `opened` — the bar at which the ENGINE opened the position it now
        holds. _reconcile_positions asks this before auto-liquidating, and the
        simulated book cannot answer it.
      * `pending` — an entry blocked for a transient reason, kept so the next
        poll can re-attempt it instead of discarding a signal the strategy will
        never emit again.
    """
    if aux is None or dry_run:
        return
    canon = canonical_symbol(sym)
    for res in fired:
        if res.get("retry_of"):
            continue        # _retry_pending_entry does its own bookkeeping
        act, status = res.get("action"), res.get("status")
        if act == "buy" and status not in ("skip", "blocked", "error", "dry_run"):
            aux[_OPENED_KEY][canon] = str(res.get("bar_ts") or last_closed_ts)
            aux[_PENDING_KEY].pop(canon, None)
        elif act in ("close", "sell") and status not in ("skip", "blocked", "error"):
            # Either action is a FULL exit — fire_event routes both to the same
            # close, because this book is all-in/all-out — so both retire the
            # ownership record.
            aux[_OPENED_KEY].pop(canon, None)
            aux[_PENDING_KEY].pop(canon, None)
        elif act == "buy" and status == "blocked" and res.get("transient"):
            prior = aux[_PENDING_KEY].get(canon) or {}
            aux[_PENDING_KEY][canon] = {
                "bar_ts": str(res.get("bar_ts")),
                "price": res.get("price"),
                "reason": res.get("reason", ""),
                "strategy": res.get("strategy", ""),
                "attempts": int(prior.get("attempts", 0)) + 1,
            }
            log.info("%s: entry blocked for a transient reason — holding it for "
                     "up to %d bar(s) and retrying next poll", canon,
                     _PENDING_ENTRY_MAX_BARS)


def _retry_pending_entry(sym: str, cfg: dict, aux: dict | None, events: list[dict],
                         new: list[dict], last_closed_ts, client: AlpacaClient,
                         om: OrderManager, settings, dry_run: bool) -> list[dict]:
    """Re-attempt an entry that a TRANSIENT block swallowed. See _aux_path.

    Why this exists (audit 2026-08-27, E2): state[sym] advances after every
    poll whatever the outcome, so a buy that was blocked was marked "already
    processed" and could never fire again — each strategy emits exactly one buy
    per cross-up cycle, so the opportunity was simply gone. That silently broke
    an explicit promise the rotation path makes in its own comment: "the buy
    re-fires from the next poll's signal once the book is flat". Across the
    live journal, 45 entries were blocked or errored against 13 fills ever, and
    not one was re-attempted.

    Deliberately narrow, because the failure mode of a retry loop is order
    spam — the same shape as the 115 rejected TSLA closes. Four ways it stops,
    checked in this order:
      1. the strategy is no longer long in its own simulation — it has moved
         on, and chasing an abandoned setup is worse than missing it;
      2. the signal is older than _PENDING_ENTRY_MAX_BARS;
      3. this poll carries a fresh exit for the symbol, which supersedes it;
      4. the re-attempt is refused for a NON-transient reason (bias flipped to
         flat, say) — a decision has now been taken, so stop asking.
    A success clears it too, via _record_entry_outcomes.
    """
    if aux is None or dry_run:
        return []
    canon = canonical_symbol(sym)
    pend = aux[_PENDING_KEY].get(canon)
    if not pend:
        return []

    def drop(why: str) -> list[dict]:
        aux[_PENDING_KEY].pop(canon, None)
        log.info("%s: dropping the held entry from %s — %s", canon,
                 pend.get("bar_ts"), why)
        append_event(JOURNAL_FILE, {
            "event": "pending_entry_dropped", "ticker": canon,
            "bar_ts": pend.get("bar_ts"), "attempts": pend.get("attempts"),
            "reason": why,
        })
        return []

    sim_long, _ = _simulated_position(events)
    if not sim_long:
        return drop("the strategy is no longer long, so the setup is gone")
    if any(e.get("action") in ("close", "sell") for e in new):
        return drop("this poll carries an exit for the same symbol")
    try:
        age = (pd.Timestamp(last_closed_ts) - pd.Timestamp(pend["bar_ts"])) / _bar_step(cfg["interval"])
    except Exception:                                       # noqa: BLE001
        return drop("its bar timestamp is unreadable")
    if age > _PENDING_ENTRY_MAX_BARS:
        return drop(f"it is {age:.0f} bars old (limit {_PENDING_ENTRY_MAX_BARS})")

    ev = {"ts": pd.Timestamp(pend["bar_ts"]), "action": "buy",
          "price": pend.get("price"), "reason": pend.get("reason", ""),
          "strategy": pend.get("strategy", "")}
    log.info("%s: retrying the entry held from %s (attempt %s)", canon,
             pend.get("bar_ts"), pend.get("attempts"))
    result = fire_event(sym, ev, cfg, client, om, settings, dry_run, aux=aux)
    result["retry_of"] = pend.get("bar_ts")
    status = result.get("status")
    if status not in ("skip", "blocked", "error"):
        aux[_PENDING_KEY].pop(canon, None)
        aux[_OPENED_KEY][canon] = str(pend.get("bar_ts"))
    elif status == "blocked" and not result.get("transient"):
        drop(f"the retry was refused outright: {result.get('reason', '')[:120]}")
    elif status == "skip" and "fetch failed" not in str(result.get("reason", "")):
        # A skip is normally a real answer — "already long", "no entry price",
        # "computed qty <= 0" — so the entry is resolved. But fire_event also
        # returns "skip" for a positions/account FETCH FAILURE, which is a
        # network blip and the very thing this record exists to survive; that
        # falls through to the attempts branch below and is bounded by expiry.
        drop(f"the retry resolved to a skip: {result.get('reason', '')[:120]}")
    else:
        pend["attempts"] = int(pend.get("attempts", 0)) + 1
    return [result]


def _fire_new_events(sym: str, new: list[dict], cfg: dict, client: AlpacaClient,
                     om: OrderManager, settings, dry_run: bool,
                     aux: dict | None = None) -> list[dict]:
    """Fire each new event in time order via fire_event, with a same-poll
    double-buy guard: once one event in this batch has successfully opened a
    position, any LATER buy in the same batch (e.g. a second strategy signaling
    independently on the same symbol) is skipped without hitting the order
    path — the live position API may not yet reflect the first fill by the
    time the second event is evaluated, so this can't rely on fire_event's own
    `held > 0` check alone."""
    fired = []
    bought_this_poll = False
    for ev in new:
        if ev["action"] == "buy" and bought_this_poll:
            # See fire_event's `out` for why this is "ticker"/"bar_ts" and not
            # "symbol"/"ts" — a caller-supplied "ts" overwrites append_event's
            # wall-clock stamp, and "symbol" hides the row from every
            # ticker-filtered journal consumer.
            out = {"ticker": sym, "action": "buy", "status": "skip",
                   "reason": f"{ev.get('reason', '')} | duplicate buy in same poll "
                             f"(strategy={ev.get('strategy')})",
                   "bar_ts": str(ev["ts"])}
            append_event(JOURNAL_FILE, {"event": "signal_skip", **out})
            fired.append(out)
            continue
        result = fire_event(sym, ev, cfg, client, om, settings, dry_run, aux=aux)
        fired.append(result)
        if ev["action"] == "buy" and result.get("status") not in ("skip", "blocked", "error"):
            bought_this_poll = True
        elif (ev["action"] in ("close", "sell")
              and result.get("status") not in ("skip", "blocked", "error")):
            # A SUBMITTED FULL CLOSE CLEARS THE FLAG. It was only ever set,
            # never reset, so a catch-up batch shaped buy -> close -> buy
            # silently swallowed the final legitimate re-entry: the book is
            # flat again by the time it is reached, but the guard still said
            # "already bought this poll" (audit 2026-08-27, S3). The guard
            # exists because the positions API may not yet reflect the first
            # fill — once a close has actually been SENT, that staleness
            # argument no longer holds for this symbol.
            #
            # Still narrow on the outcome: a close that was skipped, blocked
            # or errored changed nothing, so re-opening the guard on it would
            # let a second full-size entry through on a book that is still
            # long. Both exit actions qualify, since both are full exits.
            bought_this_poll = False
    return fired


def _simulated_position(events: list[dict]) -> tuple[bool, bool]:
    """Replay one symbol's window events -> (sim_is_long, sim_bought_in_window).

    This is what the STRATEGY believes it holds, as opposed to what the broker
    actually holds — the two are compared by _reconcile_positions.

    Events with reason "eod" are skipped, and that is load-bearing, not a
    detail: the backtest always closes any open position at the end of its data
    window, so counting that artifact would make EVERY symbol look flat at the
    end of EVERY poll — and a reconciler acting on that would liquidate the
    whole account. _diff_and_fire filters "eod" for exactly the same reason;
    the two must agree on what counts as a live signal.

    A "sell" clears the position exactly as a "close" does: this book is
    all-in/all-out and fire_event routes both to a full close (operator
    directive 2026-08-27). It used to be a HALF exit that left the flag set,
    which no configured strategy ever exercised. Because the backtest re-runs from a flat book at the start of each
    rolling window, sim_bought_in_window is False for a position opened before
    the window begins; the caller needs that to avoid mistaking an old holding
    for an orphan (see _reconcile_positions).
    """
    is_long = False
    bought = False
    for ev in events:
        if ev.get("reason") == "eod":
            continue
        action = ev.get("action")
        if action == "buy":
            is_long, bought = True, True
        elif action in ("close", "sell"):
            is_long = False
    return is_long, bought


def _adopt_assignments(positions, aux: dict | None) -> None:
    """Take ownership of shares a cash-secured put was assigned into.

    THE PROBLEM THIS SOLVES. The operator's rule for the put sleeve is: if the
    put is assigned, keep the 100 shares and let the share strategy work them
    off — its exit signal, or its stop. But nothing in the engine could do that.
    The strategies simulate from a FLAT book and only emit an exit while their
    own simulated position is long, so an assignment they never bought produces
    no exit, ever; and the reconciler only closes what is in `opened`, which an
    assignment is also not in. Assigned shares would therefore sit unmanaged and
    unstopped for as long as they were held (2026-08-27 study).

    Adoption is the missing record. It says: these shares are ours to manage,
    here is the basis to measure the stop from, and the equity book may not
    rotate out of them. It is written from the positions the reconciler has
    already fetched, so it costs no extra broker call.

    NOT the same as `opened`. Putting an assignment in `opened` would tell the
    reconciler "the engine opened this and the strategy is now flat", and it
    would market-sell the shares on the very next poll — the opposite of the
    rule. Adoption deliberately keeps the missed-exit backstop standing down.
    """
    if aux is None:
        return
    try:
        from relay.csp_manager import config as _csp_config, detect_assignment
        cfg = _csp_config()
        pos = detect_assignment(None, positions)
        if pos is None:
            return
        canon = canonical_symbol(str(getattr(pos, "symbol", ""))).upper()
        if not canon or canon in aux.get(_OPENED_KEY, {}) or canon in aux[_ADOPTED_KEY]:
            return
        # THE BASIS IS THE STRIKE, and avg_entry_price is where the broker
        # records it after an assignment — the shares were bought at the strike,
        # not at the market. A 6% stop measured from anything else is not the
        # stop the operator asked for.
        try:
            basis = float(getattr(pos, "avg_entry_price", 0) or 0)
        except (TypeError, ValueError):
            basis = 0.0
        if basis <= 0:
            log.error("assignment in %s has no usable basis — NOT adopting it; "
                      "these shares have no stop until this is resolved", canon)
            return
        # `anchor` is the price the shares arrived at, kept for the record and
        # the recap. Nothing is measured against it any more: there is no stop
        # on assigned shares (operator's choice 2026-08-28), the sleeve sells
        # them at its next weekly run, and _check_adopted_stale is the calendar
        # backstop for that run failing.
        anchor = (_f_or_none(getattr(pos, "current_price", None))
                  or _f_or_none(getattr(pos, "avg_entry_price", None)) or basis)
        aux[_ADOPTED_KEY][canon] = {
            "basis": basis,             # the STRIKE — what the shares cost
            "anchor": anchor,           # the price when they arrived — what the stop measures from
            "qty": _f_or_none(getattr(pos, "qty", None)),
            "reason": "csp_assignment",
            "underlying": cfg["underlying"],
            "since": pd.Timestamp.utcnow().isoformat(timespec="seconds"),
        }
        log.warning("adopted %s assigned shares: basis (strike) %.2f, arrived at %.2f "
                    "— the sleeve sells them at its next weekly run", canon, basis, anchor)
        append_event(JOURNAL_FILE, {
            "event": "csp_assignment_adopted", "ticker": canon, "basis": basis,
            "qty": _f_or_none(getattr(pos, "qty", None)),
            "reason": "put assigned — shares handed to the share strategy",
        })
    except Exception as e:                                  # noqa: BLE001
        # Never let the option sleeve stop the equity engine reconciling.
        log.warning("assignment adoption check failed (%s) — skipping it", e)


def _f_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# How long assigned shares may sit before the ENGINE sells them. The sleeve
# sells them at its next weekly run — that is the exit, by design, and there is
# no price stop on them at all (operator's choice 2026-08-28: every stop and
# every covered-call variant backtested worse than "sold within a week"). This
# is not a stop either; it is the backstop for the weekly run itself failing —
# a cron that did not fire, a broker outage on a Friday — so that a position
# meant to live seven days cannot quietly live seventy.
_ADOPTED_MAX_DAYS = 10


def _check_adopted_stale(canon: str, pos, aux: dict | None, cfg: dict,
                         om: OrderManager, dry_run: bool) -> bool:
    """Sell assigned shares the sleeve should already have sold. True if a
    close was submitted. `pos` and `cfg` are accepted for call-site symmetry
    and not read: this is a calendar test, not a price test."""
    if aux is None or dry_run:
        return False
    rec = aux.get(_ADOPTED_KEY, {}).get(canon)
    if not isinstance(rec, dict):
        return False
    try:
        since = pd.Timestamp(rec.get("since"))
        if pd.isna(since):
            return False                   # no date is "unknown", never "overdue"
        since = since.tz_localize("UTC") if since.tzinfo is None else since.tz_convert("UTC")
        age_days = (pd.Timestamp.utcnow() - since).total_seconds() / 86400
    except Exception:                                       # noqa: BLE001
        return False
    if not (age_days > _ADOPTED_MAX_DAYS):   # NaN compares False too
        return False
    log.error("adopted %s has been held %.1f days — the sleeve's weekly sale did not "
              "happen; selling as the backstop", canon, age_days)
    try:
        om.protected_symbols = set(aux.get(_ADOPTED_KEY, {})) - {canon}
        om.handle(OrderRequest(ticker=canon, action="close", strategy="csp-assignment-stale"))
    except Exception as e:                                  # noqa: BLE001
        log.error("adopted %s stale close failed: %s", canon, e)
        append_event(JOURNAL_FILE, {
            "event": "csp_assignment_stale_failed", "ticker": canon,
            "age_days": round(age_days, 1), "reason": str(e)})
        return False
    append_event(JOURNAL_FILE, {
        "event": "csp_assignment_stale_sold", "ticker": canon, "age_days": round(age_days, 1),
        "reason": (f"assigned shares held {age_days:.0f} days, past the "
                   f"{_ADOPTED_MAX_DAYS}-day backstop — the weekly run should have sold them")})
    return True


def _reconcile_positions(enabled: dict, fetched: dict, client: AlpacaClient,
                         om: OrderManager, dry_run: bool,
                         aux: dict | None = None) -> None:
    """Safety-net pass (runs at the END of each poll): compare what each
    strategy thinks it holds against what the broker ACTUALLY holds, and close
    positions the strategy has already exited.

    Why this exists: state[sym] advances to the latest closed bar whether or not
    an order actually landed (see _diff_and_fire), and a strategy's exit is
    emitted from its SIMULATED position (backtest.ema_scaled_rolling and
    friends only evaluate a stop while their own `position > 0`). So if a close
    order fails, the simulated book goes flat, the real position stays OPEN, and
    the exit is never re-emitted — the position then rides on with no stop, and
    because fire_event skips a buy while `held > 0` it also blocks every future
    entry in that symbol. This pass is what notices.

    Deliberately ASYMMETRIC — automatic action may only ever REDUCE exposure:

      real long + sim flat, sim traded this window -> CLOSE (a missed exit)
          — UNLESS the shares are already pinned by a working order, in which
          case the exit is not missed but merely unfilled, and a second close
          would be rejected outright. See _handle_pinned_exit.
      real long + sim flat, sim never bought here  -> alert only: the position
          predates the rolling window, so the strategy has no opinion on it and
          closing could flatten a legitimate holding.
      short position                               -> alert only, NEVER cover.
          The book is long-only, so a short is an anomaly, not a signal — and a
          broker-side phantom short has been seen on this account before.
          Covering one automatically would trade against a glitch.

    The mirror case — sim long while the broker is flat — is deliberately NOT
    reported at all. It looks like a missed entry but is the NORMAL resting
    state of this book: the relay holds one position at a time and rotates into
    whichever candidate has the highest priority (see
    order_manager._should_rotate), so every candidate that is long in its own
    simulation but wasn't selected sits flat by design. Alerting on it would
    fire for most symbols on every poll and drown the signals above. A genuinely
    missed entry is already visible as an `order_error` in the journal, which is
    where it belongs. Auto-entering would be worse still: it chases a stale
    signal at a price the strategy never saw while its stop stays anchored to
    the simulated entry, silently widening real risk.

    Best-effort and non-fatal: a reconciliation failure must never break a poll.
    Silent in dry-run.
    """
    if dry_run:
        return
    try:
        positions = client.get_all_positions()
    except Exception as e:  # pragma: no cover - network
        log.warning("reconcile: positions fetch failed: %s", e)
        return

    # Working orders, fetched ONCE for the whole pass. Needed to retire the
    # ownership record safely: a submitted entry that has not filled yet shows
    # qty == 0 at the broker, so "flat" alone does NOT mean "the position we
    # opened is gone" — retiring on that erased the record in the same poll it
    # was written, which silently disarmed this entire pass (it then alerts
    # instead of closing). `None` means the question could not be answered, and
    # nothing is retired on a poll where we do not know.
    working: set[str] | None
    try:
        working = {canonical_symbol(str(getattr(o, "symbol", "") or "")).upper()
                   for o in (client.get_open_orders() or [])}
    except Exception as e:  # pragma: no cover - network
        log.warning("reconcile: working-order fetch failed (%s) — not retiring "
                    "any ownership record this poll", e)
        working = None

    # An assignment appears in exactly this list, and taking ownership of it
    # here costs no extra round trip. Must run BEFORE the loop below, so the
    # first poll after an assignment already knows the shares are managed.
    _adopt_assignments(positions, aux)

    held = {}
    live_pos: dict[str, object] = {}
    for p in positions or []:
        # OPTIONS ARE NOT THIS PASS'S BUSINESS. get_all_positions() is an
        # unfiltered passthrough and includes option positions under their OCC
        # symbol; canonical_symbol would pass one straight through and it would
        # never match an `enabled` key, but the qty/avail bookkeeping below is
        # equity-shaped and an option's "qty" is contracts. The cash a short
        # option pledges is accounted for in fire_event's sizing instead.
        if not _is_equity_position(p):
            continue
        try:
            qty = float(p.qty)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        # qty_available is qty MINUS whatever a still-working order has pinned
        # (Alpaca's held_for_orders). Absent on older SDKs and on test doubles —
        # default to the full qty, i.e. "nothing pinned", the pre-2026-08-19
        # behavior.
        try:
            avail = float(getattr(p, "qty_available", qty))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            avail = qty
        held[canonical_symbol(str(p.symbol))] = (qty, avail)
        live_pos[canonical_symbol(str(p.symbol)).upper()] = p

    for sym in enabled:
        canon = canonical_symbol(sym)
        result = fetched.get(sym)
        if not isinstance(result, tuple):
            continue  # fetch failed/skipped this cycle — no opinion, stay quiet
        events = result[0]
        sim_long, sim_bought = _simulated_position(events)
        # DID THE ENGINE ACTUALLY OPEN THIS? `sim_bought` was meant to answer
        # that, and cannot: the "rolling window" it replays is the ENTIRE cached
        # history (thousands of bars), so a simulated buy is almost always
        # somewhere in it and sim_bought is effectively always True. The branch
        # below that exists to protect a position the engine did not open was
        # therefore unreachable, and the reconciler would auto-liquidate an
        # operator's own holding (audit 2026-08-27, S2).
        #
        # The sidecar records the bar at which a buy actually SUBMITTED, which
        # is the real answer. When it is absent — an older install, a first run
        # after this change, or aux not passed by a debug caller — fall back to
        # the simulated answer, i.e. exactly the previous behaviour, so this can
        # only ever make the reconciler more cautious, never less.
        if aux is not None:
            sim_bought = canon in aux.get(_OPENED_KEY, {})
        qty, avail = held.get(canon, (0.0, 0.0))

        if qty <= 0 or sim_long:
            _PENDING_EXIT_SEEN.pop(canon, None)  # divergence resolved
        if (qty <= 0 and aux is not None
                and canon.upper() in aux.get(_ADOPTED_KEY, {})):
            # The assigned shares are gone — sold by the strategy's own exit, by
            # the stop below, or by hand. Either way the sleeve is free again
            # and csp_manager's gate 1 will let next Friday sell a put.
            aux[_ADOPTED_KEY].pop(canon.upper(), None)
            append_event(JOURNAL_FILE, {
                "event": "csp_assignment_cleared", "ticker": canon,
                "reason": "assigned shares are flat — the put sleeve may resume"})
        if (qty <= 0 and aux is not None and working is not None
                and canon.upper() not in working):
            # Flat AND nothing working: whatever we opened is genuinely gone
            # (filled then closed, or the entry expired unfilled). Retiring it
            # keeps the record from becoming a stale claim over some LATER
            # position the engine did not open.
            aux.get(_OPENED_KEY, {}).pop(canon, None)
        if qty < 0:
            _reconcile_alert(canon, "short position held; book is long-only "
                                    "(never auto-covered)", qty, sim_long)
            continue
        # ADOPTED SHARES ARE THE SLEEVE'S. It sells them at its next weekly run;
        # the engine only steps in if that run has failed to happen.
        if qty > 0 and aux is not None and canon.upper() in aux.get(_ADOPTED_KEY, {}):
            if _check_adopted_stale(canon.upper(), live_pos.get(canon.upper()), aux,
                                    enabled[sym], om, dry_run):
                continue
        if qty > 0 and not sim_long:
            if not sim_bought:
                if aux is not None and canon.upper() in aux.get(_ADOPTED_KEY, {}):
                    # Not an orphan and not a missed exit: an assignment the
                    # share strategy has not yet signalled its way out of.
                    # Saying "not closing" once a poll for a week would bury the
                    # real divergences this alert exists to surface.
                    _reconcile_alert(canon, "assigned shares held under the put "
                                            "sleeve; it sells them at its next "
                                            "weekly run", qty, sim_long)
                    continue
                _reconcile_alert(canon, "held but strategy never opened it in this "
                                        "window (predates window?); not closing", qty, sim_long)
                continue
            # The exit may already be WORKING rather than missed: a resting
            # order pins shares (qty_available < qty) and a further close is
            # rejected outright with code 40310000. Submitting one anyway is
            # what produced 115 rejected TSLA closes on 2026-08-18 and 109 for
            # AAPL on 2026-08-11. Wait for it — or, once there is a real session
            # to trade in, cancel it so the next poll can flatten at market.
            #
            # `avail < qty`, NOT `avail <= 0`: a close is always sized to the
            # WHOLE position (order_manager._close_position -> close_position(),
            # or _position_qty_and_side returning the full qty for the
            # extended-hours limit), so the broker rejects it whenever ANY
            # shares are held_for_orders — not only when every share is. A
            # PARTIALLY FILLED close is the live case: 6 of 13 shares done
            # leaves qty=7/avail=0... but an operator's own resting order, or a
            # partial fill still working, leaves qty=13/avail=7, and the
            # original reject-every-poll loop came straight back at `avail <= 0`.
            if avail < qty:
                _handle_pinned_exit(canon, qty, client)
                continue
            log.error("reconcile: %s held %s but strategy is flat — missed exit, closing",
                      canon, qty)
            try:
                om.handle(OrderRequest(ticker=canon, action="close", strategy="reconcile"))
            except Exception as e:
                # RiskError here is usually risk_filter's 60s dedup rejecting a
                # close the fire phase already attempted seconds ago; the next
                # poll retries once the window lapses. Already journaled by handle().
                log.error("reconcile: %s close failed: %s", canon, e)
                append_event(JOURNAL_FILE, {
                    "event": "reconcile_close_failed", "ticker": canon, "qty": qty,
                    "reason": str(e),
                })
            else:
                # Journaled only on a SUBMITTED close, never on intent: the
                # chart draws an exit arrow per reconcile_close
                # (backtest.chart_view._JOURNAL_EXIT_EVENTS), and journaling
                # before the attempt drew one for every rejection too.
                if aux is not None:
                    # this WAS our position and we have just closed it
                    aux.get(_OPENED_KEY, {}).pop(canon, None)
                append_event(JOURNAL_FILE, {
                    "event": "reconcile_close", "ticker": canon, "qty": qty,
                    "reason": "strategy exited but position still open (missed exit)",
                })

    # ADOPTED SHARES IN A SYMBOL THAT IS NOT `enabled` — WHICH IS NOW THE NORMAL
    # CASE: SOXL was retired from the share strategy on 2026-08-28 precisely so
    # that its exit signal can never sell the sleeve's shares. The loop above
    # walks the enabled symbols only, so the backstop and the retirement of the
    # record both have to run here.
    if aux is not None:
        for canon in list(aux.get(_ADOPTED_KEY, {})):
            if canon in {canonical_symbol(s).upper() for s in enabled}:
                continue                       # already handled in the loop
            qty, _avail = held.get(canon, (0.0, 0.0))
            if qty <= 0:
                aux[_ADOPTED_KEY].pop(canon, None)
                append_event(JOURNAL_FILE, {
                    "event": "csp_assignment_cleared", "ticker": canon,
                    "reason": "assigned shares are flat — the put sleeve may resume"})
                continue
            _check_adopted_stale(canon, live_pos.get(canon), aux, {}, om, dry_run)


# canonical symbol -> the id of the resting order we have ALREADY reported as
# pinning that symbol's shares. Purely a noise damper: without it the "exit is
# still working" notice would be journaled on every poll for as long as the
# order rests, which is the 115-line flood this whole guard exists to stop. Keyed
# by order id so a genuinely NEW resting order is still reported once.
_PENDING_EXIT_SEEN: dict[str, str] = {}


def _handle_pinned_exit(canon: str, qty: float, client: AlpacaClient) -> None:
    """Called when the strategy is flat but the broker still shows a position
    with shares pinned by a working order (qty_available < qty — a PARTIAL pin
    is enough, because a close is always sized to the whole position).

    Never closes in the poll that calls it — this poll can only report or
    release. Whatever it does, the close itself is the NEXT poll's job, once
    the broker has actually freed the shares.

    The failure this exists for (TSLA 2026-08-18, AAPL 2026-08-11): the exit
    signal was REAL and its order was accepted, but priced off a frozen
    overnight last trade it rested ~4.4% above the bid and did not fill for
    10.5 hours. The position stayed open, so this pass called it a missed exit
    every poll and submitted another close — each one rejected with code
    40310000 ("insufficient qty available for order"), because the FIRST order
    was holding all the shares. 115 rejections, 115 spurious exit arrows on the
    chart, and no progress: the second close could never have worked while the
    first was alive.

    So the exit is not missed, it is WORKING — and the useful question is
    whether waiting can still pay off:

      - Outside regular hours: wait. Report it once (see _PENDING_EXIT_SEEN)
        and leave the order alone. Re-pricing is pointless here — on 2026-08-18
        Alpaca's quote AND last trade for TSLA were both frozen at
        323.28/339.31 for 17 straight hours, so a cancel/replace would have
        recomputed the identical limit indefinitely.
      - Once there is a real session (regular hours, or crypto, which has no
        session at all): stop waiting. Cancel the resting order to release the
        shares; the NEXT poll sees qty_available > 0 and flattens at market,
        where there is actual liquidity. Cancelling and closing in the SAME
        poll would just race the cancel and earn one more 40310000.

    Never abandons an exit: the only outcome of a cancel here is a market close
    one poll later, which is strictly more decisive than the order it replaced.
    Best-effort — any broker failure falls back to waiting.
    """
    try:
        open_orders = client.get_open_orders(canon)
    except Exception as e:  # pragma: no cover - network
        log.warning("reconcile: %s open-order fetch failed: %s", canon, e)
        return

    if not open_orders:
        # Nothing visibly pinning the shares — most likely we raced a fill or a
        # cancel. Say nothing and let the next poll re-read the position.
        return

    order_ids = ",".join(sorted(str(getattr(o, "id", "?")) for o in open_orders))

    if not (is_crypto_symbol(canon) or regular_market_hours_open()):
        if _PENDING_EXIT_SEEN.get(canon) != order_ids:
            _PENDING_EXIT_SEEN[canon] = order_ids
            log.error("reconcile: %s exit already working (qty %s pinned by %d open "
                      "order(s)); waiting for regular hours", canon, qty, len(open_orders))
            append_event(JOURNAL_FILE, {
                "event": "reconcile_exit_pending", "ticker": canon, "qty": qty,
                "open_orders": len(open_orders), "order_ids": order_ids,
                "reason": "strategy exited and a close order is still working; "
                          "waiting rather than submitting a close that would be "
                          "rejected (insufficient qty available)",
            })
        return

    cancelled = []
    for o in open_orders:
        oid = getattr(o, "id", None)
        try:
            client.cancel_order(oid)
        except Exception as e:  # pragma: no cover - network
            log.warning("reconcile: %s cancel of %s failed: %s", canon, oid, e)
            continue
        cancelled.append(str(oid))

    if not cancelled:
        return

    _PENDING_EXIT_SEEN.pop(canon, None)
    log.error("reconcile: %s cancelled %d unfilled exit order(s) pinning qty %s — "
              "next poll closes at market", canon, len(cancelled), qty)
    append_event(JOURNAL_FILE, {
        "event": "reconcile_cancel_pinned", "ticker": canon, "qty": qty,
        "order_ids": ",".join(cancelled),
        "reason": "unfilled exit order pinned the position into regular hours; "
                  "cancelled so the next poll can close at market",
    })


def _reconcile_alert(canon: str, reason: str, qty: float, sim_long: bool) -> None:
    """Journal + log a divergence we deliberately do NOT act on. Loud on purpose:
    the whole failure mode this pass exists for went unnoticed because a single
    ERROR line in a 50MB log is invisible."""
    log.error("reconcile: %s — %s (qty=%s, strategy_long=%s)", canon, reason, qty, sim_long)
    append_event(JOURNAL_FILE, {
        "event": "reconcile_alert", "ticker": canon, "qty": qty,
        "strategy_long": sim_long, "reason": reason,
    })


def _backfill_gaps(enabled: dict, data_cfg: dict) -> None:
    """Safety-net pass (runs at the END of each poll): re-pull from IBKR any
    enabled EQUITY whose latest cached bar is left >1 bar behind its peers.

    Why: when a single symbol hits a transient IBKR stall on a given cycle it
    falls back to yfinance, whose 1h bar lags IBKR's by ~1 bar — so that symbol's
    cache (and the Signal Terminal chart) ends up one bar behind the rest until
    some later cycle happens to re-pull it. This closes that window immediately.

    Data-completeness ONLY — it upserts bars into the cache and fires NO orders.
    The normal fire phase already ran this cycle; any signal sitting on a
    backfilled bar is picked up by the NEXT cycle's diff (state still points at
    the older processed bar), so nothing is double-fired or skipped.

    Detection is peer-relative: a symbol is only "behind" if it trails the
    FRESHEST enabled equity by more than ~1 bar. During market-closed periods
    every equity is equally stale, so the reference is stale too and nothing is
    flagged — no false backfills overnight/weekends. Crypto is excluded: it
    trades 24/7 on a different cadence and its IBKR/PAXOS leg has its own quirks
    (see _fetch_bars); it self-heals without this and mixing it into the peer
    reference would mis-flag every equity while crypto keeps printing.
    """
    if data_cfg.get("primary", "ibkr") != "ibkr":
        return  # nothing to re-pull from if IBKR isn't the primary
    if equity_market_closed_for_weekend():
        return  # every equity is legitimately frozen — no gaps to fill
    eq = {s: c for s, c in enabled.items() if not is_crypto_symbol(s)}
    lts: dict[str, "pd.Timestamp"] = {}
    for sym, scfg in eq.items():
        t = latest_ts(sym, scfg["interval"])
        if t is not None:
            lts[sym] = t
    if len(lts) < 2:
        return  # need at least two symbols to have a peer reference
    reference = max(lts.values())
    for sym, scfg in eq.items():
        t = lts.get(sym)
        if t is None:
            continue
        step = _INTERVAL_SECONDS.get(scfg["interval"], 3600)
        behind = (reference - t).total_seconds()
        # Bar stamps are boundary-aligned, so `behind` is a whole multiple of
        # `step`; a 0.5-bar threshold cleanly means "at least one full bar behind
        # the freshest peer" while never flagging same-bar peers (behind == 0).
        if behind <= step * 0.5:
            continue
        df = _ibkr_fetch_with_timeout(sym, scfg["interval"])
        if df is not None and not df.empty:
            try:
                n = upsert_bars(sym, scfg["interval"], df, "ibkr")
            except Exception as e:  # pragma: no cover - cache write failure
                log.warning("gap-backfill %s: upsert failed: %s", sym, e)
                continue
            log.info("gap-backfill %s: was %.1f bars behind peers -> IBKR re-pull "
                     "+%d bar(s), latest now %s", sym, behind / step, n,
                     latest_ts(sym, scfg["interval"]))
        else:
            log.info("gap-backfill %s: %.1f bars behind peers, IBKR still "
                     "unavailable — will retry next cycle", sym, behind / step)


def run_once(settings, dry_run: bool, quiet: bool = False) -> dict:
    ensure_data_dirs()
    cfg = load_config()
    state = load_state(dry_run)
    aux = load_aux(dry_run)
    data_cfg = cfg.get("data", {})
    # Surface the IBKR connection settings to sigengine.ibkr (which reads os.environ).
    if data_cfg:
        os.environ.setdefault("IBKR_HOST", str(data_cfg.get("ibkr_host", "127.0.0.1")))
        os.environ.setdefault("IBKR_PORT", str(data_cfg.get("ibkr_port", 4002)))
        os.environ.setdefault("IBKR_CLIENT_ID", str(data_cfg.get("ibkr_client_id", 1)))
    client = AlpacaClient(settings)
    om = OrderManager(client)
    enabled = {sym: scfg for sym, scfg in cfg["symbols"].items() if scfg.get("enabled", False)}

    # Fetch phase: network-bound (IBKR/yfinance) and side-effect-limited to the
    # (lock-serialized) bar cache — safe to run concurrently across symbols.
    # This is the dominant cost of a poll cycle (one poll of 10 symbols on the
    # yfinance fallback measured ~19s sequential; threaded, it's bounded by the
    # SLOWEST single symbol instead of the sum of all of them).
    fetched: dict[str, object] = {}
    ex = ThreadPoolExecutor(max_workers=min(len(enabled), 8) or 1)
    futures = {ex.submit(_fetch_and_recompute, sym, scfg, data_cfg): sym
              for sym, scfg in enabled.items()}
    try:
        for fut in as_completed(futures, timeout=_FETCH_PHASE_TIMEOUT):
            sym = futures[fut]
            try:
                fetched[sym] = fut.result()
            except Exception as e:
                log.error("%s fetch/compute failed: %s", sym, e)
                fetched[sym] = e
    except FutureTimeoutError:
        # Don't wait on the stuck future(s) — mark them failed for THIS cycle
        # and move on; the next poll spins up a brand-new executor/thread, so
        # this cycle's abandoned thread (Python can't force-kill a thread) is
        # orphaned. It is only harmless because _detach_worker_threads() below
        # unregisters it: left in place, a wedged worker blocks interpreter
        # EXIT forever (non-daemon since 3.9), turning a restart into a zombie.
        stuck = [futures[f] for f in futures if not f.done()]
        log.error("fetch phase exceeded %ss, abandoning stuck symbol(s) for this "
                  "cycle: %s", _FETCH_PHASE_TIMEOUT, stuck)
        for sym in stuck:
            fetched[sym] = RuntimeError(f"fetch phase timed out after {_FETCH_PHASE_TIMEOUT}s")
    finally:
        ex.shutdown(wait=False)
        _detach_worker_threads(ex)

    # Fire phase: strictly SEQUENTIAL, in config order — this is where real
    # orders get placed, and the one-position-at-a-time rotation logic
    # (relay/order_manager.py) depends on each symbol's account/position check
    # happening only after the previous symbol's order (if any) has been
    # submitted, so two buy signals can never race the same "is anything held"
    # check concurrently.
    summary = {}
    for sym, scfg in enabled.items():
        result = fetched.get(sym)
        if isinstance(result, Exception):
            summary[sym] = [{"status": "error", "reason": str(result)}]
            continue
        if result is None:
            summary[sym] = []
            continue
        try:
            events, last_closed_ts, source = result
            fired = _diff_and_fire(sym, scfg, state, events, last_closed_ts,
                                   source, client, om, settings, dry_run, quiet,
                                   aux=aux)
            summary[sym] = fired
        except Exception as e:
            log.error("%s diff/fire failed: %s", sym, e)
            summary[sym] = [{"status": "error", "reason": str(e)}]
    # Safety-net: re-pull from IBKR any equity left >1 bar behind its peers by a
    # yfinance fallback this cycle, so the cache/chart stay complete. Fires no
    # orders (see _backfill_gaps). Best-effort — never let it break a poll.
    try:
        _backfill_gaps(enabled, data_cfg)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("gap-backfill pass failed: %s", e)
    # Safety-net: close any position the strategy has already exited but that is
    # still open at the broker (a failed close leaves it riding with no stop —
    # see _reconcile_positions). Runs after the fire phase so it sees this
    # cycle's orders. Best-effort — never let it break a poll.
    try:
        _reconcile_positions(enabled, fetched, client, om, dry_run, aux=aux)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("reconcile pass failed: %s", e)
    save_state(state, dry_run)
    try:
        save_aux(aux, dry_run)
    except Exception as e:  # pragma: no cover - defensive
        # Bookkeeping, not the trade path — the same "never break a poll" rule
        # _backfill_gaps and _reconcile_positions are held to.
        log.error("could not persist the entry sidecar: %s", e)
    return summary


def _effective_poll_seconds(cfg: dict) -> int:
    """The daemon loop's sleep interval: config.json's poll_seconds."""
    return int(cfg.get("poll_seconds", 60))


def main() -> None:
    ap = argparse.ArgumentParser(description="Self-hosted signal engine (replaces TradingView)")
    ap.add_argument("--once", action="store_true", help="run a single poll and exit (still fires orders unless --dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="compute signals but do NOT call order_manager.handle()")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()

    if args.once:
        run_once(settings, dry_run=args.dry_run)
        return

    cfg = load_config()
    interval = _effective_poll_seconds(cfg)
    log.info("signal engine starting (poll=%ds, dry_run=%s)",
            interval, args.dry_run)
    while True:
        try:
            run_once(settings, dry_run=args.dry_run, quiet=True)
        except Exception as e:  # never let the daemon die on a poll error
            log.error("poll cycle failed: %s", e)
        except SystemExit as e:
            # Belt to DataUnavailable's braces. `except Exception` above cannot
            # catch SystemExit (it is a BaseException), and on 2026-08-19 one
            # raised deep in the yfinance fallback ended this process with a
            # single untimestamped line and no traceback. _load_with_retry no
            # longer re-raises that particular one, but backtest/ modules are
            # written as CLIs and use SystemExit freely as "give up on this
            # input" — any future path into one would kill the daemon the same
            # silent way. Inside the poll loop there is no such thing as a
            # legitimate reason to exit, so swallow it LOUDLY and keep polling.
            #
            # KeyboardInterrupt is deliberately NOT caught: Ctrl-C must still
            # stop the engine.
            log.critical("poll cycle raised SystemExit (%s) — swallowed to keep the "
                         "daemon alive; this is a BUG in the call path, not a "
                         "normal exit", e)
        # Watchdog: see _LOCK_STUCK_RESTART_THRESHOLD's docstring. Checked
        # once per cycle (not more often) — a stuck holder, once crossed, only
        # gets MORE stuck, never resolves on its own, so cycle-granularity
        # detection is plenty timely without adding a separate poll loop.
        # Per-symbol connections since the 2026-07-06 rewrite (see
        # sigengine.ibkr's module docstring) mean a stuck symbol no longer
        # blocks any OTHER symbol — but it still needs a full process restart
        # to actually reclaim its thread (Python can't force-kill it), so this
        # checks the WORST offender across all symbols' locks.
        from sigengine import ibkr
        stuck_symbol, held_for = ibkr.max_lock_held_seconds()
        if _lock_stuck_for_too_long(held_for):
            log.error("%s: stuck longest (%.0fs)", stuck_symbol, held_for)
            _restart_self()
        time.sleep(interval)


if __name__ == "__main__":
    main()
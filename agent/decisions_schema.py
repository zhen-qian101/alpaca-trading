"""Shared decision schema + hard-ceiling validation.

Used by the Claude Code harness path (run_research*.sh -> `claude -p` ->
validate_decisions.py) so the relay always loads a schema-valid, clamped
decisions.json.

Hard ceilings (enforced HERE and re-clamped by relay/risk_filter.py):
    max_position   <= 10000 shares (scaled for the $100k paper account)
    daily_loss_limit <= 0.05 (5% of equity)
    suggested_size  DERIVED, not accepted — the engine's deploy fraction
    max_open_positions <= 10 (kept at 1 for the single-position policy)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

HARD_MAX_POSITION = 10000         # restored 2026-08-28 for the $100,000 paper account
                                  # (was 2000 for the ~$5k one). MUST match
                                  # relay.risk_filter.HARD_MAX_POSITION — the relay
                                  # re-clamps with its own copy, so a lower value here
                                  # silently wins and a higher one is silently ignored.
                                  # See that module for why the number tracks account
                                  # size rather than staying put.
HARD_DAILY_LOSS_LIMIT = 0.05
HARD_MAX_OPEN_POSITIONS = 10
HARD_MAX_CONTINGENCY_HOURS = 48    # a pre-approved override can't outlive 2 days
                                   # unattended — the whole point is a bridge to
                                   # the NEXT research refresh, not a standing rule

# THIS BOOK IS ALL-IN / ALL-OUT (operator directive 2026-08-27). There is one
# position at a time and it is opened at the engine's full `deploy` fraction of
# equity; there is no partial entry and no partial exit.
#
# `suggested_size` is therefore DERIVED, not decided — see _derive_sizes. It
# used to be presented to the research model as a per-symbol sizing lever with a
# 0.5 ceiling, and it was a fiction: nothing in the trade path ever read it
# (audit 2026-08-27, E5). It was still written into decisions.json, clamped
# here, and rendered on the dashboard and in the video briefing, so the one
# number a human saw for "how big is this position" disagreed with the only
# number that mattered by more than 10x — research asked for MSFT 0.08 while the
# engine deployed 0.97. Deriving it keeps that display honest instead of
# deleting the field and leaving a hole in every consumer.
_ENGINE_CONFIG = Path(__file__).resolve().parent.parent / "sigengine" / "config.json"
ALL_IN_FRACTION = 0.97            # fallback only; the engine config is the authority


def deploy_fraction(symbol: str, _cfg: dict | None = None) -> float:
    """The fraction of equity the ENGINE will actually deploy for `symbol`.

    Read from sigengine/config.json rather than duplicated, because two copies
    of this number silently drifting apart is exactly the failure this readout
    exists to end.

    THREE ANSWERS, not two. A symbol the engine has no entry for — the research
    prompt actively invites the model to mine new names — or one with
    `enabled: false` is never polled and can never be opened, so its honest
    readout is 0.0, not the house fraction. Only a genuine read/parse failure
    falls back to ALL_IN_FRACTION, and a research run must never die over a
    display value.
    """
    try:
        cfg = _cfg if _cfg is not None else json.loads(_ENGINE_CONFIG.read_text())
    except Exception:                                       # noqa: BLE001
        return ALL_IN_FRACTION                              # config unreadable
    entry = (cfg.get("symbols") or {}).get(symbol.upper())
    if not isinstance(entry, dict) or not entry.get("enabled", False):
        return 0.0                                          # not traded at all
    try:
        return float(entry["deploy"])
    except (KeyError, TypeError, ValueError):
        return ALL_IN_FRACTION


def _will_open(d: dict, sym: str) -> bool:
    """Will the risk gate let a full position be opened in `sym` at all?

    `bias == "long"` is the common answer but not the only one, and reading it
    alone made the board lie in two directions the audit's own live file
    demonstrates:

      * a FLAT symbol with an armed contingency — AAPL was exactly this, flat
        with close_above 315.5 live — is one price print away from a full-size
        entry, because relay.risk_filter.check lets a matching contingency
        permit the buy and order_manager no longer shrinks it;
      * CRYPTO is exempt from the bias gate entirely (risk_filter step 2), so
        BTC/USD can open on a mechanical signal whatever the board says.

    Reporting 0.0 for either is the same class of untruth the derived field was
    introduced to remove, just pointing the other way.
    """
    if d.get("bias", {}).get(sym) == "long":
        return True
    if (d.get("contingencies") or {}).get(sym):
        return True
    return sym.upper().endswith("/USD")          # crypto: see risk_filter step 2


def _derive_sizes(d: dict) -> None:
    """Overwrite `suggested_size` with what the engine will really deploy.

    Whatever the model wrote is discarded. That is deliberate — the field is a
    readout, not an input, and leaving the model's number in place is how the
    dashboard came to advertise a position size the engine had no intention of
    taking (audit E5: research asked 0.08 while the engine deployed 0.97).

    The config is read ONCE here and threaded through, rather than re-parsed
    per symbol.
    """
    try:
        cfg = json.loads(_ENGINE_CONFIG.read_text())
    except Exception:                                       # noqa: BLE001
        cfg = None
    # over watchlist AND bias: a symbol carrying a bias but missing from the
    # watchlist would otherwise keep the model's number.
    syms = list(dict.fromkeys([*d.get("watchlist", []), *(d.get("bias") or {})]))
    d["suggested_size"] = {
        sym: (deploy_fraction(sym, cfg) if _will_open(d, sym) else 0.0)
        for sym in syms
    }


def _clamp_contingencies(d: dict) -> None:
    """Keep only well-formed, not-yet-expired contingencies for symbols
    actually on the watchlist. See risk_filter.py's module docstring for what
    these do (a mechanical, pre-approved override for a flat/short bias) and
    _active_contingency() for how they're evaluated at signal time."""
    contingencies = d.get("contingencies")
    if not isinstance(contingencies, dict):
        d["contingencies"] = {}
        return
    watchlist = set(d.get("watchlist", []))
    now = datetime.now(timezone.utc)
    max_valid_until = now + timedelta(hours=HARD_MAX_CONTINGENCY_HOURS)
    cleaned: dict[str, list[dict]] = {}
    for sym, rules in contingencies.items():
        sym = str(sym).upper()
        if sym not in watchlist or not isinstance(rules, list):
            continue
        kept = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if rule.get("condition") not in ("close_above", "close_below"):
                continue
            if rule.get("action") != "long":
                continue
            try:
                level = float(rule["level"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                valid_until = datetime.fromisoformat(
                    str(rule["valid_until"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=timezone.utc)
            if valid_until <= now:
                continue  # already expired — drop rather than keep silently
            valid_until = min(valid_until, max_valid_until)
            # `size_cap` IS DROPPED, not clamped. A contingency's job is to
            # PERMIT an entry that bias alone would block; resizing it is a
            # different power and this book does not have it — every entry is
            # all-in (operator directive 2026-08-27). Stripping it here means a
            # model that keeps emitting one cannot quietly half-size a live
            # trade: order_manager no longer reads the key either.
            kept.append({
                "condition": rule["condition"],
                "level": level,
                "action": "long",
                "valid_until": valid_until.isoformat(),
                "reason": str(rule.get("reason", ""))[:500],
            })
        if kept:
            cleaned[sym] = kept
    d["contingencies"] = cleaned


def _clean_briefing(d: dict) -> None:
    """Keep the OPTIONAL, VIDEO-ONLY ``briefing`` block well-formed. This NEVER
    touches the trade path — the relay ignores the key entirely; only
    ``briefing/generate.py`` reads it to render richer catalyst/indicator content.
    Best-effort: coerce to ``{sym: {story, catalysts[], indicators[]}}``, drop
    anything malformed, and never raise (a bad briefing must never fail a run)."""
    b = d.get("briefing")
    if not isinstance(b, dict):
        d.pop("briefing", None)
        return

    def _pairs(entry: dict, key: str, kb: str) -> list[dict]:
        out = []
        for it in (entry.get(key) or [])[:8]:
            if isinstance(it, dict):
                a = str(it.get("label", "")).strip()[:40]
                v = str(it.get(kb, "")).strip()[:160]
                if a or v:
                    out.append({"label": a, kb: v})
            elif isinstance(it, str) and it.strip():
                out.append({"label": it.strip()[:40], kb: ""})
        return out

    cleaned: dict[str, dict] = {}
    for sym, entry in b.items():
        if not isinstance(entry, dict):
            continue
        story = str(entry.get("story", ""))[:1400].strip()
        cats = _pairs(entry, "catalysts", "detail")
        inds = _pairs(entry, "indicators", "value")
        if story or cats or inds:
            key = "_" if str(sym) == "_" else str(sym).upper()
            cleaned[key] = {"story": story, "catalysts": cats, "indicators": inds}
    d["briefing"] = cleaned


# NOTE: the weekly cash-secured put's config is NOT here. It lives in
# decisions/csp.json precisely because this file is rewritten wholesale by the
# research harness — see relay.csp_manager.CSP_CONFIG_FILE.


def validate_decision(d: dict, watchlist: list[str] | None = None) -> dict:
    """Clamp and normalize a parsed decision dict in place + return it."""
    watchlist = list(dict.fromkeys(d.get("watchlist") or watchlist or []))
    d["watchlist"] = [s.upper() for s in watchlist]
    d.setdefault("bias", {})
    d.setdefault("priority", {})
    d.setdefault("suggested_size", {})   # rebuilt by _derive_sizes; kept for key order
    d.setdefault("max_position", {})
    d.setdefault("notes", {})
    _clamp_contingencies(d)
    _clean_briefing(d)
    try:
        d["daily_loss_limit"] = min(float(d.get("daily_loss_limit", 0.05)), HARD_DAILY_LOSS_LIMIT)
    except (TypeError, ValueError):
        d["daily_loss_limit"] = 0.05
    d["allow_extended_hours"] = bool(d.get("allow_extended_hours", False))
    try:
        d["max_open_positions"] = min(int(d.get("max_open_positions", 1)), HARD_MAX_OPEN_POSITIONS)
    except (TypeError, ValueError):
        d["max_open_positions"] = 1

    for sym, mp in list(d.get("max_position", {}).items()):
        try:
            d["max_position"][sym] = min(int(mp), HARD_MAX_POSITION)
        except (TypeError, ValueError):
            d["max_position"][sym] = HARD_MAX_POSITION

    # Every watchlist symbol gets a bias (default flat).
    for sym in d["watchlist"]:
        d["bias"].setdefault(sym, "flat")
    # Coerce bias values to the allowed set.
    for sym, b in list(d["bias"].items()):
        d["bias"][sym] = b if b in ("long", "short", "flat") else "flat"
    # Sizes are DERIVED from the final bias, so this must run after it is
    # settled — not before, or a defaulted-to-flat symbol keeps a long's size.
    _derive_sizes(d)

    # Priority: per-symbol int, HIGHER = more preferred. Longs must be > 0
    # (default 1 if missing/invalid); flats/shorts are 0. Drop priorities for
    # symbols no longer in the bias map. Used by the relay to decide whether a
    # new candidate's buy signal should rotate out of the current holding.
    pri = d["priority"]
    for sym in list(pri.keys()):
        if sym not in d["bias"]:
            del pri[sym]
    for sym, b in d["bias"].items():
        try:
            p = int(pri.get(sym, 0))
        except (TypeError, ValueError):
            p = 0 if b != "long" else 1
        pri[sym] = max(p, 0) if b == "long" else 0
    return d

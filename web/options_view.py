"""The weekly cash-secured-put sleeve, as the cloud dashboard shows it. READ-ONLY.

build_portfolio() (web/server.py) drops option positions, because its table is
equity-shaped. The cloud dashboard shows the sleeve as its own snapshot section,
`snapshot.options` (apps/dashboard/src/contract.ts, OptionsSleeve), and this
module builds it.

WHY THIS IS A MIRROR AND NOT AN IMPORT. relay.csp_manager.assignment_state()
already answers "where does the sleeve stand?", but relay.csp_manager imports
relay.options_client (which can sell and buy puts) and relay.logger (which
writes the journal), and sigengine.engine.load_aux() imports the order manager
and can append to the journal. The publisher must not load any of them (see
tests/test_publisher.py). So the few rules that matter for DISPLAY are repeated
here from plain files and ONE broker read, and tests/test_options_view.py runs
both implementations on the same fakes so they cannot drift apart:

  * the policy file is decisions/csp.json, coerced the same way: `enabled` is
    on only if it is literally true, `shadow` stays on unless literally false;
  * the short put is the first option position with a negative quantity;
  * shares count as ASSIGNED only when they are the underlying, a positive
    round multiple of 100, AND the journal has a csp_sell for that underlying
    in the last 30 days (fails closed: an unreadable journal says "no");
  * they are managed when the engine's entry sidecar lists them as opened or
    adopted.

On top of that it adds what the cloud page needs and assignment_state() does
not carry: the option's entry and current price, the underlying's last price,
and how far the underlying can fall before the put is in the money.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from relay.config import DECISIONS_FILE, JOURNAL_FILE, SIGENGINE_STATE_FILE
from relay.risk_filter import EASTERN, _is_equity_position
from web import server as web_server

# relay.csp_manager.CSP_CONFIG_FILE: the operator's policy file, which the
# research cron never rewrites.
CSP_CONFIG_FILE = DECISIONS_FILE.parent / "csp.json"
# sigengine.engine._aux_path(dry_run=False): the entry-bookkeeping sidecar.
AUX_FILE = SIGENGINE_STATE_FILE.with_name(
    SIGENGINE_STATE_FILE.stem + "_entries" + SIGENGINE_STATE_FILE.suffix)
# relay.csp_manager.DEFAULTS["underlying"]
DEFAULT_UNDERLYING = "SOXL"
ASSIGNMENT_LOOKBACK_SEC = 30 * 86400


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def load_policy() -> dict | None:
    """decisions/csp.json, or None when there is no such file. A file that is
    there but unreadable is an EMPTY policy (the sleeve reads as disabled), the
    same direction relay.csp_manager.load_policy() takes."""
    try:
        block = json.loads(CSP_CONFIG_FILE.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return block if isinstance(block, dict) else {}


def policy_view(block: dict | None) -> dict:
    """enabled / shadow / underlying, coerced exactly as csp_manager.config()."""
    block = block or {}
    return {
        "enabled": block.get("enabled") is True,
        "shadow": block.get("shadow") is not False,
        "underlying": str(block.get("underlying") or DEFAULT_UNDERLYING).upper(),
    }


def occ_strike(sym: str) -> float | None:
    """Strike of an OCC PUT symbol, or None (csp_manager._occ_strike)."""
    s = (sym or "").upper()
    if len(s) < 16 or s[-9] != "P" or not s[-8:].isdigit():
        return None
    return int(s[-8:]) / 1000.0


def occ_expiry(sym: str) -> str | None:
    """YYYY-MM-DD from an OCC symbol, or None (csp_manager._occ_expiry)."""
    s = (sym or "").upper()
    if len(s) < 16 or not s[-15:-9].isdigit():
        return None
    return f"20{s[-15:-13]}-{s[-13:-11]}-{s[-11:-9]}"


def _is_option_position(p) -> bool:
    """OptionsClient.option_positions(): asset_class us_option."""
    ac = getattr(p, "asset_class", None)
    return str(getattr(ac, "value", ac) or "").lower() == "us_option"


def _short_put(option_positions) -> dict | None:
    for p in option_positions:
        qty = _f(getattr(p, "qty", 0)) or 0.0
        if qty >= 0:
            continue
        sym = str(getattr(p, "symbol", ""))
        return {
            "symbol": sym,
            "contracts": abs(qty),
            # tolerant, as in assignment_state(): an unparseable symbol loses
            # these two fields but the open put is still reported
            "strike": occ_strike(sym),
            "expiry": occ_expiry(sym),
            "avg_entry_price": _f(getattr(p, "avg_entry_price", None)),
            "current_price": _f(getattr(p, "current_price", None)),
            "market_value": _f(getattr(p, "market_value", None)),
            "unrealized": _f(getattr(p, "unrealized_pl", None)),
        }
    return None


def _assignment_is_corroborated(und: str, now: float) -> bool:
    """A csp_sell on `und` in the last 30 days (csp_manager's rule). Fails
    closed. Reads the journal line by line and never writes it."""
    cutoff = now - ASSIGNMENT_LOOKBACK_SEC
    try:
        with Path(JOURNAL_FILE).open() as f:
            for ln in f:
                if '"csp_sell"' not in ln:
                    continue
                r = json.loads(ln)
                if (str(r.get("ticker", "")).upper() == und
                        and float(r.get("ts", 0) or 0) >= cutoff):
                    return True
    except Exception:                                       # noqa: BLE001 — fail closed
        return False
    return False


def _detect_assignment(equity_positions, und: str, now: float):
    for p in equity_positions:
        if str(getattr(p, "symbol", "")).upper() != und:
            continue
        qty = _f(getattr(p, "qty", 0)) or 0.0
        if qty <= 0 or qty % 100 != 0:
            continue
        if _assignment_is_corroborated(und, now):
            return p
    return None


def _load_aux() -> dict:
    """The engine's entry sidecar, read without sigengine.engine.load_aux()
    (which imports the order path and journals a corrupt file)."""
    try:
        d = json.loads(AUX_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _sell_by(adopted) -> str | None:
    """csp_manager._sell_by: the first Friday after adoption. Display only."""
    try:
        since = datetime.fromisoformat(str((adopted or {}).get("since")).replace("Z", "+00:00"))
        days = (4 - since.weekday()) % 7 or 7
        return (since + timedelta(days=days)).date().isoformat()
    except Exception:                                       # noqa: BLE001
        return None


def _assigned(pos, aux: dict) -> dict:
    sym_u = str(getattr(pos, "symbol", "")).upper()
    adopted = ((aux or {}).get("adopted", {}) or {}).get(sym_u)
    engine_opened = sym_u in set((aux or {}).get("opened", {}) or {})
    basis, anchor = None, None
    if isinstance(adopted, dict):
        basis = _f(adopted.get("basis"))
        anchor = _f(adopted.get("anchor")) or basis
    return {
        "symbol": str(getattr(pos, "symbol", "")),
        "qty": _f(getattr(pos, "qty", None)),
        "avg_price": _f(getattr(pos, "avg_entry_price", None)),
        "unrealized": _f(getattr(pos, "unrealized_pl", None)),
        "unrealized_pct": _f(getattr(pos, "unrealized_plpc", None)),
        # False = nobody manages these shares; the page says so out loud.
        "managed_by_engine": bool(engine_opened or adopted),
        "adopted": bool(adopted),
        "basis": basis,
        "anchor": anchor,
        "sell_by": _sell_by(adopted),
    }


def sleeve_state(positions, policy: dict | None, aux: dict, now: float) -> dict:
    """The assignment_state()-shaped part, from positions already fetched."""
    view = policy_view(policy)
    positions = list(positions or [])
    options = [p for p in positions if _is_option_position(p)]
    equities = [p for p in positions if not _is_option_position(p) and _is_equity_position(p)]
    out = {**view, "short_put": _short_put(options), "assigned": None}
    pos = _detect_assignment(equities, view["underlying"], now)
    if pos is not None:
        out["assigned"] = _assigned(pos, aux)
    return out


def _underlying_price(client, und: str, equity_positions) -> float | None:
    """The underlying's last trade; a held position's mark if that fails."""
    try:
        price, _ts = client.get_latest_trade(und)
        if price and price > 0:
            return float(price)
    except Exception:                                       # noqa: BLE001 — display only
        pass
    for p in equity_positions:
        if str(getattr(p, "symbol", "")).upper() == und:
            return _f(getattr(p, "current_price", None))
    return None


def build_options(now: float | None = None) -> dict | None:
    """snapshot.options: the sleeve, None when it has no policy file and holds
    nothing, or {"error": ...} when the broker cannot be read."""
    now = datetime.now(EASTERN).timestamp() if now is None else now
    policy = load_policy()
    try:
        client = web_server._client()
        positions = list(client.get_all_positions() or [])
    except Exception as exc:                                # noqa: BLE001 — surface in the UI
        return {"error": f"{type(exc).__name__}: {exc}"}
    out = sleeve_state(positions, policy, _load_aux(), now)
    if policy is None and out["short_put"] is None and out["assigned"] is None:
        return None
    put = out["short_put"]
    if put is not None:
        equities = [p for p in positions if not _is_option_position(p)]
        spot = _underlying_price(client, out["underlying"], equities)
        put["underlying_price"] = spot
        put["strike_distance_pct"] = (spot / put["strike"] - 1.0) if (spot and put["strike"]) else None
    return out

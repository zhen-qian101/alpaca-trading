"""THE DESK SLIDE — the briefing turns the camera on the system itself.

Every other slide in the intraday deck is about the market: what a symbol did and
why. This one is about the book — a screenshot of the LIVE dashboard, pushed in
panel by panel, narrated with the account's real equity, its open position, the
fills and blocks since the last edition, and what today's decision board says
happens next.

Three things make it safe to put an account on YouTube unattended:

1. **The capture is read-only and out-of-band.** `briefing/desk_probe.py` runs in
   the trading venv, photographs the dashboard, and reads the same read-only
   builders the dashboard reads. Nothing here can touch an order.

2. **The narrator may only speak numbers it was handed.** Every other slide's
   script was written upstream by Claude from a research note; this is the first
   slide where the local gemma model is given raw account figures and asked to
   talk about them. So `director.desk_narration` runs behind a numeric guard: any
   digit that is not in the fact sheet gets the line rejected, and after the
   retries are spent the deterministic script below ships instead. A rounded
   "roughly five thousand" is a rejection, not a rounding.

3. **The numbers and the picture come from one frozen page.** See desk_probe's
   docstring — the figures are scraped from the same paint the screenshot took,
   so a viewer zoomed into the equity card sees exactly the figure being spoken.

Degradation ladder, because a briefing must never fail over its own vanity slide:
dashboard up -> screenshot slide; dashboard down -> same facts, typographic
slide; capture broken entirely -> no desk slide, deck renders as it always did.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import generate as G

ROOT = G.ROOT


def dcfg(cfg: dict) -> dict:
    """Normalised `desk` config block (mirrors publish_youtube._ycfg)."""
    d = cfg.setdefault("desk", {})
    d.setdefault("enabled", True)
    d.setdefault("url", "http://127.0.0.1:8787")
    d.setdefault("after", "OVERVIEW")       # slide the desk follows; "" = first
    d.setdefault("lines", 4)
    d.setdefault("timeout_s", 240)
    d.setdefault("max_zoom", 2.6)
    d.setdefault("min_zoom", 1.2)
    d.setdefault("shot_width", 1920)
    d.setdefault("shot_height", 1080)
    return d


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------
def capture(cfg: dict, work: Path, log=print) -> dict | None:
    """Run the probe in the trading venv. Returns the payload, or None if even
    the data-only fallback failed (caller then skips the slide)."""
    d = dcfg(cfg)
    work.mkdir(parents=True, exist_ok=True)
    png, js = work / "desk.png", work / "desk.json"
    png.unlink(missing_ok=True)
    js.unlink(missing_ok=True)
    py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        log(f"  [desk] no trading venv at {py} — skipping the desk slide")
        return None
    cmd = [str(py), "-m", "briefing.desk_probe", "--png", str(png), "--json", str(js),
           "--url", d["url"], "--width", str(d["shot_width"]), "--height", str(d["shot_height"])]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=float(d["timeout_s"]))
    except subprocess.TimeoutExpired:
        log("  [desk] capture timed out — skipping the desk slide")
        return None
    for ln in (r.stdout + r.stderr).splitlines():
        if ln.strip():
            log(f"  {ln.strip()}")
    if r.returncode != 0 or not js.exists():
        log("  [desk] capture failed — skipping the desk slide")
        return None
    payload = json.loads(js.read_text())
    if payload.get("image") and png.exists():
        payload["image"]["path"] = str(png)
    else:
        payload["image"] = None
    return payload


# --------------------------------------------------------------------------
# fact sheet
# --------------------------------------------------------------------------
def _usd(v, cents: bool = False) -> str:
    if v is None:
        return "n/a"
    s = f"${abs(v):,.2f}" if cents else f"${abs(v):,.0f}"
    return f"-{s}" if v < 0 else s


def _pct(v, dp: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:+.{dp}f}%"


def _qty(v) -> str:
    """A share count with its trailing space, or nothing at all.

    Position and fill quantities come from the broker and are always numbers, but a
    JOURNAL row's qty can be null: the duplicate-close guard fires before the order
    is sized, so it logs `{"event": "risk_block", "action": "close", "qty": null}`.
    `f"{None:g}"` raises, the whole desk snapshot is caught by one broad except, and
    the briefing silently ships with no desk slide — which is what happened on
    2026-08-13 (`duplicate TSLA close within 60s`). Four such rows exist in the
    journal's history, so this had been waiting since 2026-07-18 to land inside a
    since-last-briefing window.

    Reads naturally either way, because the sentence is SPOKEN: "blocked a close of
    TSLA" when the size is unknown, "blocked a buy of 36 SOXL" when it is not."""
    return "" if v is None else f"{v:g} "


def _contracts(lastd: dict) -> str:
    """"2 contracts of " — or nothing at all when the count is one or unknown.

    The sleeve stopped being a fixed one-lot on 2026-08-28: the count is sized
    from cash and a risk budget, so it moves with the account and it is now a
    fact about the trade rather than a constant the script can assume.
    """
    n = lastd.get("contracts")
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"{n} contracts of " if n > 1 else ""


def _sized_by(lastd: dict) -> str:
    """Which ceiling chose the count. Worth a clause only when it is the RISK
    BUDGET — that says the sleeve is at its deliberate limit rather than at the
    account's, which is a different and more interesting fact."""
    by = str(lastd.get("sized_by") or "")
    if by.startswith("reserve"):
        return " — the size its reserve allows, not what the cash would cover"
    if by.startswith("rail"):
        return " — one contract by the operator's own cap"
    return ""


def _positions_from_table(tsv: str | None) -> list[str]:
    """The positions panel exactly as drawn, one sentence per row. Preferred over
    the API rows because these strings are the pixels in the screenshot."""
    out = []
    for row in (tsv or "").splitlines()[1:]:
        c = [x.strip() for x in row.split("\t")]
        if len(c) < 8 or not c[0]:
            continue
        sym = c[0].split()[0]
        out.append(f"Open position {sym}: {c[1]} shares, entry {c[2]}, last {c[3]}, "
                   f"worth {c[4]}, unrealised {c[5]}. Synthetic stop {c[6]}, "
                   f"{c[7]} above it.")
    return out


def digest(payload: dict, prev: dict | None = None) -> tuple[dict, list[str]]:
    """(digest for meta.json, fact sheet lines for the narrator).

    The digest is written into the edition's meta.json so the NEXT edition can
    diff against it — that diff is the only source of "what changed since the
    last briefing", because nothing else in the system records the board's
    previous state.
    """
    p = payload.get("portfolio") or {}
    b = payload.get("board") or {}
    ex = payload.get("executions") or {}
    j = payload.get("journal") or {}
    fig = payload.get("figures") or {}
    if p.get("error"):
        raise RuntimeError(f"portfolio unavailable: {p['error']}")

    eq = p.get("equity") or 0.0
    invested = sum(x.get("market_value") or 0 for x in p.get("positions", []))
    rows = [r for r in b.get("rows", []) if r.get("symbol") != "_"]
    longs = sorted([r for r in rows if r.get("bias") == "long"],
                   key=lambda r: -(r.get("priority") or 0))
    held = {x["symbol"] for x in p.get("positions", [])}

    since_ts = None
    if prev and prev.get("captured_at"):
        try:
            since_ts = datetime.fromisoformat(prev["captured_at"]).timestamp()
        except ValueError:
            since_ts = None
    fills = [r for r in ex.get("rows", [])
             if since_ts and r.get("ts") and r["ts"] > since_ts]
    blocks = [r for r in (j.get("rows") or [])
              if r.get("event") == "risk_block"
              and (since_ts is None or (r.get("_ts") or 0) > since_ts)][:4]

    d = {
        "captured_at": payload.get("captured_at")
                       or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "equity": eq, "cash": p.get("cash"), "day_pl": p.get("day_pl"),
        "day_pl_pct": p.get("day_pl_pct"), "invested": invested,
        "deploy_pct": (invested / eq) if eq else 0.0,
        "halt": p.get("halt", {}),
        "positions": [{"symbol": x["symbol"], "qty": x["qty"], "entry": x["avg_entry_price"],
                       "last": x["current_price"], "value": x["market_value"],
                       "pl": x["unrealized_pl"], "plpc": x["unrealized_plpc"],
                       "stop_price": x["stop_price"], "stop_dist": x["stop_distance_pct"]}
                      for x in p.get("positions", [])],
        "board": [{"symbol": r["symbol"], "bias": r.get("bias"), "priority": r.get("priority"),
                   "size": r.get("suggested_size")} for r in rows],
        "board_date": b.get("date"), "max_open": b.get("max_open_positions"),
        "realized_pnl": ex.get("realized_pnl"), "round_trips": ex.get("round_trips"),
        "win_rate": ex.get("win_rate"),
        "fills_since": [{"symbol": r["symbol"], "side": r["side"], "qty": r["qty"],
                         "price": r["price"], "pnl": r.get("pnl")} for r in fills],
        "blocks": [{"symbol": r.get("ticker") or r.get("symbol"), "action": r.get("action"),
                    "qty": r.get("qty"), "reason": r.get("reason")} for r in blocks],
        "has_shot": bool(payload.get("image")),
        # THE OPTIONS SLEEVE. Carried on the digest (not only on the fact sheet)
        # so the NEXT edition can diff it the same way it diffs the board — that
        # diff is the only record of "what changed since the last briefing".
        "csp": payload.get("csp"),
        # A NEW ACCOUNT, while it is still news. Without it the recap says
        # "lifetime realised $0.00 over 0 round trips" with a straight face and
        # sounds like a system that has never worked.
        "account": payload.get("account"),
    }

    # --- what CHANGED since the previous edition ---------------------------
    changes: list[str] = []
    if prev:
        pb = {r["symbol"]: r for r in prev.get("board", [])}
        for r in d["board"]:
            old = pb.get(r["symbol"])
            if not old:
                continue
            if old.get("bias") != r.get("bias"):
                changes.append(f"{r['symbol']} moved from {old.get('bias')} to {r.get('bias')} "
                               f"on the board")
            elif (old.get("priority") or 0) != (r.get("priority") or 0):
                changes.append(f"{r['symbol']} went from priority {old.get('priority')} to "
                               f"priority {r.get('priority')}")
        pheld = {x["symbol"] for x in prev.get("positions", [])}
        for s in sorted(held - pheld):
            changes.append(f"the book opened {s}")
        for s in sorted(pheld - held):
            changes.append(f"the book closed {s}")
        # Last, so a board or position change never gets crowded out by it: the
        # editions are ~3h apart, so this is the account's move over the session
        # so far, which the day's P&L alone does not say.
        if prev.get("equity") and abs(prev["equity"] - eq) >= 0.01:
            changes.append(f"equity moved from {_usd(prev['equity'], True)} to "
                           f"{_usd(eq, True)}")
    d["changes"] = changes[:4]

    # --- the sheet: the ONLY numbers the narrator is allowed to say ---------
    # ORDER MATTERS: the deterministic fallback reads this sheet from the top
    # under a character budget, so it runs balance -> book -> decisions first and
    # leaves the supporting detail (cash, the halt meter, lifetime P&L) to be cut.
    L: list[str] = []
    if fig.get("equity"):
        # Straight off the frozen screen — these strings are the pixels.
        L.append(f"Account equity {fig['equity']}, {fig.get('equity_delta', '')}".strip(" ,") + ".")
    else:
        L.append(f"Account equity {_usd(eq, True)}, "
                 f"{'up' if (d['day_pl'] or 0) >= 0 else 'down'} "
                 f"{_usd(abs(d['day_pl'] or 0), True)}, {_pct(d['day_pl_pct'], 2)} on the day.")

    pos_lines = _positions_from_table(fig.get("positions_table"))
    if pos_lines:
        L += pos_lines
    elif d["positions"]:
        for x in d["positions"]:
            L.append(f"Open position {x['symbol']}: {x['qty']:g} shares at {_usd(x['entry'], True)} "
                     f"average, last {_usd(x['last'], True)}, unrealised {_usd(x['pl'], True)} "
                     f"({_pct(x['plpc'])}).")
    else:
        L.append("No open positions — the account is flat and entirely in cash.")

    if longs:
        rank = ", then ".join(f"{r['symbol']} at priority {r['priority']}" for r in longs)
        L.append(f"Decision board dated {d['board_date']}, highest priority first: {rank}. "
                 f"{longs[0]['symbol']} is the top-ranked long, {len(rows) - len(longs)} names "
                 f"are flat, and the engine holds at most {d['max_open']} position at a time, so "
                 f"a new long has to displace the old one.")
    else:
        L.append(f"Decision board dated {d['board_date']}: every one of the {len(rows)} names is "
                 f"flat, so the engine is not allowed to open anything new.")

    if fig.get("equity"):
        L.append(f"Cash {fig.get('cash')}, buying power {fig.get('buying_power')}, "
                 f"{_pct(d['deploy_pct'])[1:]} of the account deployed.")
        L.append(f"The daily loss circuit breaker meter reads {fig.get('halt')} used"
                 + (" and the account is HALTED for new buys."
                    if d["halt"].get("tripped") else ", so buying is still allowed."))
    else:
        L.append(f"Cash {_usd(d['cash'], True)}, {_pct(d['deploy_pct'])[1:]} of the account "
                 f"deployed.")
        L.append(f"Daily loss circuit breaker: {_pct(-(d['halt'].get('limit') or 0))[1:]} limit, "
                 + ("TRIPPED — no new buys." if d["halt"].get("tripped") else "not tripped."))

    for f in d["fills_since"]:
        L.append(f"Filled since the last briefing: {f['side']} {f['qty']:g} {f['symbol']} at "
                 f"{_usd(f['price'], True)}"
                 + (f", realising {_usd(f['pnl'], True)}." if f.get("pnl") is not None else "."))
    if not d["fills_since"]:
        L.append("No fills since the last briefing — the engine stood pat.")
    for bk in d["blocks"]:
        L.append(f"The risk layer blocked a {bk['action']} of {_qty(bk['qty'])}{bk['symbol']}: "
                 f"{bk['reason']}.")
    if d["realized_pnl"] is not None:
        L.append(f"Lifetime realised {_usd(d['realized_pnl'], True)} over {d['round_trips']} "
                 f"round trips"
                 + (f", win rate {d['win_rate'] * 100:.0f}%." if d.get("win_rate") is not None
                    else "."))
        # IMMEDIATELY AFTER the lifetime line, never anywhere else: it is the
        # explanation for that specific number, and a sheet read top-down under a
        # character budget can separate them by pages if it is placed loosely.
        ac = d.get("account") or {}
        if ac.get("switched_days_ago") is not None and not d["round_trips"]:
            ago = ac["switched_days_ago"]
            when = ("today" if ago < 1 else "yesterday" if ago < 2
                    else f"{ago:.0f} days ago")
            prev_eq = ac.get("from_equity")
            L.append(f"That is zero because the account itself is new: the book moved "
                     f"{when} to a fresh {_usd(d['equity'])} paper account"
                     + (f", up from {_usd(prev_eq)}." if prev_eq else ".")
                     + " The track record starts here.")
    # --- the options sleeve, if it is switched on --------------------------
    # Placed AFTER the equity facts on purpose: the sheet is read top-down under
    # a character budget, so the account's own balance and book always survive
    # the cut and the option line is what gets dropped on a crowded day.
    c = d.get("csp") or {}
    if c.get("enabled"):
        und = c.get("underlying", "SOXL")
        sp, asg = c.get("short_put"), c.get("assigned")
        if asg:
            px = f" at an average {_usd(asg['avg_price'])}" if asg.get("avg_price") else ""
            pl = (f", currently {_usd(asg['unrealized'], True)}"
                  if asg.get("unrealized") is not None else "")
            L.append(f"Assigned {_qty(asg.get('qty'))}shares of {und}{px}{pl}. "
                     f"No new puts are sold while these shares are held.")
            if asg.get("adopted"):
                by = asg.get("sell_by")
                L.append(f"The put sleeve sells those {und} shares at its next weekly run"
                         + (f", on {by}" if by else "")
                         + ". There is no stop on them by design: holding them longer, or "
                         "selling calls against them, both tested worse than selling "
                         "within the week.")
            elif asg.get("managed_by_engine") is False:
                # The single most important sentence this sheet can carry: the
                # share strategies only exit positions they opened themselves,
                # so nothing is watching these.
                L.append(f"Those {und} shares were not opened by the trading engine, "
                         f"so no strategy will produce an exit signal for them.")
        elif sp:
            k = f" at the {sp['strike']:g} strike" if sp.get("strike") else ""
            e = f" expiring {sp['expiry']}" if sp.get("expiry") else ""
            pl = (f", marked {_usd(sp['unrealized'], True)}"
                  if sp.get("unrealized") is not None else "")
            L.append(f"Short {_qty(sp.get('contracts'))}{und} put{k}{e}{pl}.")
        elif c.get("last"):
            lastd = c["last"]
            if lastd.get("action") == "shadow":
                # `shortfall` is set only by gate 4, i.e. only when the account
                # genuinely could not pay. Stating "collateral the account does not
                # have" on a $100,000 account would be a false claim about the book.
                if not lastd.get("strike"):
                    L.append(f"The {und} put strategy is in shadow mode and did not trade.")
                elif lastd.get("shortfall"):
                    L.append(f"The {und} put strategy is running in shadow mode and did not "
                             f"trade: it would have sold the {lastd.get('strike'):g} strike "
                             f"for {_usd(lastd.get('credit'))}, which needs "
                             f"{_usd(lastd.get('collateral'))} of collateral the account "
                             f"does not have.")
                else:
                    L.append(f"The {und} put strategy is in shadow mode by choice — the "
                             f"cash is there. It would have sold "
                             f"{_contracts(lastd)}the {lastd.get('strike'):g} strike for "
                             f"{_usd(lastd.get('credit'))} against "
                             f"{_usd(lastd.get('collateral'))} of collateral"
                             + _sized_by(lastd) + ".")
            elif lastd.get("action") == "skip":
                L.append(f"No {und} put was sold: {lastd.get('reason', 'the gates refused it')}.")
        else:
            L.append(f"The {und} put strategy is on but has not made a decision yet.")

    for c2 in d["changes"]:
        L.append("Changed since the last briefing: " + c2 + ".")
    return d, L


def fallback_lines(dg: dict, facts: list[str], n: int = 4) -> list[str]:
    """The deterministic script — what ships when gemma is down or keeps inventing
    numbers, and the reason a desk slide can be scheduled unattended.

    It is BUILT from the digest rather than assembled out of the fact sheet. The
    sheet reads at roughly half the speed of prose because it is number-dense —
    "$5,039.56" is nine characters and two seconds of speech — so reading even a
    trimmed sheet aloud ran 61s against the written slide's 45s. These beats say
    the same four things (balance, book, decisions, what the engine did) in about
    a third of the words.
    """
    out: list[str] = [facts[0] if facts else "The desk snapshot is unavailable."]

    pos = dg.get("positions") or []
    if not pos:
        out.append("The book is flat — no open positions, everything in cash.")
    else:
        bits = ", ".join(f"{x['qty']:g} {x['symbol']} at {_usd(x['pl'], True)} unrealised"
                         for x in pos[:2])
        stop = pos[0].get("stop_price")
        out.append(f"Open: {bits}." + (f" Synthetic stop {_usd(stop, True)}." if stop else ""))

    longs = [r for r in (dg.get("board") or []) if r.get("bias") == "long"]
    longs.sort(key=lambda r: -(r.get("priority") or 0))
    flat = len(dg.get("board") or []) - len(longs)
    if longs:
        out.append(f"The board ranks {longs[0]['symbol']} top"
                   + (f", then {longs[1]['symbol']}" if len(longs) > 1 else "")
                   + f", with {flat} names flat and at most {dg.get('max_open')} position open "
                     f"at a time.")
    else:
        out.append(f"Every one of the {flat} names on the board is flat, so nothing new opens.")

    fills, blocks = dg.get("fills_since") or [], dg.get("blocks") or []
    # Spelled out, not "entry(s)" — this line is SPOKEN, and edge-tts reads the
    # brackets aloud.
    tail = (f"{len(fills)} fill{'s' if len(fills) != 1 else ''} since the last briefing"
            if fills else "No fills since the last briefing")
    if blocks:
        tail += (f", and the risk layer blocked {len(blocks)} "
                 f"{'entries' if len(blocks) != 1 else 'entry'}")
    out.append(tail + ".")
    # THE OPTIONS SLEEVE, if it would otherwise go unmentioned. This function is
    # what ships when the writer is down or keeps inventing numbers — precisely
    # the moment nobody is watching — so an assigned position that nothing is
    # managing must not be the thing that gets cut. Inserted at the FRONT of the
    # tail so it survives the out[:n] trim.
    c = dg.get("csp") or {}
    if c.get("enabled"):
        und = c.get("underlying", "SOXL")
        asg, sp = c.get("assigned"), c.get("short_put")
        if asg:
            worked = (" My share strategy is working them off now."
                      if asg.get("adopted") else "")
            out.insert(min(1, len(out)),
                       f"I was assigned {asg['qty']:g} shares of {und} and I am holding them. "
                       f"No new put goes out while they are on the book.{worked}"
                       if asg.get("qty") is not None else
                       f"I was assigned {und} shares and I am holding them.{worked}")
        elif sp:
            out.insert(min(1, len(out)),
                       f"I am short one {und} put at the {sp['strike']:g} strike."
                       if sp.get("strike") is not None else f"I am short a {und} put.")
        elif (c.get("last") or {}).get("action") == "shadow":
            # INSERTED, NOT APPENDED. Appending put it past the out[:n] trim on
            # every ordinary day (four lines fit, this was the fifth), so the
            # one state the sleeve will actually be in for weeks — shadow — was
            # the one state the fallback never mentioned. Third position: after
            # the balance and the book, before the day's colour.
            out.insert(min(3, len(out)),
                       f"The {und} put strategy is still only shadowing — it picks a "
                       f"trade each week and does not place it.")
    return out[:n]


# --------------------------------------------------------------------------
# camera: which panel each narration beat is about
# --------------------------------------------------------------------------
# Scored, not first-match: a line like "no fills since the last briefing, stop at
# $326" mentions both the journal and the position, and the position is what it
# is actually about. Weights break exactly those ties.
_PANEL_WEIGHTS = {
    "portfolio": {"equity": 3, "cash": 3, "deployed": 3, "buying power": 3, "on the day": 2,
                  "loss limit": 3, "circuit breaker": 3, "halted": 3, "account": 1, "book is": 1},
    "positions": {"open position": 4, "shares": 3, "unrealis": 3, "unrealiz": 3, "stop": 3,
                  "entry": 2, "average": 1, "holding": 2},
    "board": {"decision board": 4, "priority": 3, "top-ranked": 3, "ranked": 2, "rotation": 3,
              "displace": 3, "watchlist": 2, "flat": 1, "bias": 2},
    "executions": {"filled": 3, "fill": 2, "bought": 3, "sold": 3, "round trip": 3,
                   "realised": 3, "realized": 3, "win rate": 3, "closed": 2},
    "signals": {"blocked": 4, "risk layer": 4, "refused": 3, "skip": 3, "stood pat": 2,
                "every decision": 2},
    "health": {"polling": 3, "stale": 3, "current bar": 3, "engine health": 3},
}
_ORDER = ("portfolio", "positions", "board", "signals", "executions", "health", "chart")


def panel_for(line: str, panels: dict, used: list[str]) -> str | None:
    """Pick the dashboard panel a narration beat is talking about."""
    if not panels:
        return None
    low = line.lower()
    best, best_score = None, 0.0
    for name, weights in _PANEL_WEIGHTS.items():
        if name not in panels:
            continue
        score = sum(w for kw, w in weights.items() if kw in low)
        if score and name in used:
            score -= 1.0            # nudge the camera on when two beats tie
        if score > best_score:
            best, best_score = name, score
    if best:
        return best
    for name in _ORDER:             # nothing matched: walk the dashboard in order
        if name in panels and name not in used:
            return name
    return next(iter(panels))


def _fit(iw: int, ih: int, w: int, h: int, view: list | None = None) -> tuple[float, float, float]:
    """The establishing shot frames the dashboard's FOLD (the browser viewport),
    not the whole scrolled page. Fitting all ~1600px of page into a 1080p frame
    shrinks it to 60% and nothing is readable; the fold fits at 90% and reads as
    a screen, with the journal below waiting off-frame for its own push."""
    vw, vh = (view or [iw, ih])[:2]
    s = min(w * 0.94 / vw, h * 0.90 / vh)
    return s, (w - vw * s) / 2, (h - vh * s) / 2


def _zoom(box: dict, w: int, h: int, lo: float, hi: float) -> tuple[float, float, float]:
    s = max(lo, min(min(w * 0.96 / box["width"], h * 0.66 / box["height"]), hi))
    return s, w / 2 - (box["x"] + box["width"] / 2) * s, h / 2 - (box["y"] + box["height"] / 2) * s


# --------------------------------------------------------------------------
# the slide
# --------------------------------------------------------------------------
_ACCENT = "#f2a93b"
_HOLD_S = 1.8          # establishing shot: the whole terminal, before the first push


def build_desk_slide_html(shot_rel: str, iw: int, ih: int, panels: dict, timing: list[dict],
                          duration: float, stamp: str, audio_rel: str, cfg: dict,
                          view: list | None = None) -> str:
    """The screenshot, held wide, then pushed into one panel per narration beat."""
    d = dcfg(cfg)
    w, h = cfg.get("width", 1920), cfg.get("height", 1080)
    s0, x0, y0 = _fit(iw, ih, w, h, view)
    clip = f'class="clip" data-start="0" data-duration="{duration:.2f}"'

    rings, moves, used = "", "", []
    for i, t in enumerate(timing):
        name = panel_for(t["narration"], panels, used)
        if not name:
            continue
        used.append(name)
        box = panels[name]
        s, tx, ty = _zoom(box, w, h, float(d["min_zoom"]), float(d["max_zoom"]))
        at = max(_HOLD_S, t["start"] - 0.45)      # push just ahead of the words
        moves += (f'tl.to("#stage",{{x:{tx:.1f},y:{ty:.1f},scale:{s:.3f},duration:1.25,'
                  f'ease:"power2.inOut"}},{at:.2f});')
        rings += (f'<div id="ring{i}" style="position:absolute;left:{box["x"] - 6}px;'
                  f'top:{box["y"] - 6}px;width:{box["width"] + 12}px;height:{box["height"] + 12}px;'
                  f'border:3px solid {_ACCENT};border-radius:14px;opacity:0;'
                  f'box-shadow:0 0 0 9999px rgba(5,7,13,.55),0 0 34px {_ACCENT}88"></div>')
        moves += (f'tl.to("#ring{i}",{{opacity:1,duration:0.45}},{at + 0.55:.2f});'
                  f'tl.to("#ring{i}",{{opacity:0,duration:0.4}},'
                  f'{at + max(1.8, t["dur"] - 0.3):.2f});')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width={w}, height={h}"/>
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
<style>*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{w}px;height:{h}px;overflow:hidden;background:#05070d;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}}</style>
</head><body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{duration:.2f}"
     data-width="{w}" data-height="{h}">
  <div {clip} data-track-index="0" style="position:absolute;inset:0;
       background:radial-gradient(120% 90% at 50% 0%,#111a2b,#05070d 70%)"></div>
  <div {clip} data-track-index="0" style="position:absolute;inset:0;overflow:hidden">
    <div id="stage" style="position:absolute;left:0;top:0;width:{iw}px;height:{ih}px;
         transform-origin:0 0">
      <img src="{shot_rel}" width="{iw}" height="{ih}" style="display:block;border-radius:10px"/>
      {rings}
    </div>
  </div>
  <div {clip} data-track-index="1" id="hold" style="position:absolute;inset:0;
       pointer-events:none;background:rgba(5,7,13,.62)"></div>
  <div {clip} data-track-index="1" style="position:absolute;inset:0;pointer-events:none;
       background:linear-gradient(to bottom,rgba(5,7,13,.72) 0,rgba(5,7,13,0) 230px,
       rgba(5,7,13,0) 60%,rgba(5,7,13,.88) 100%)"></div>
  <div {clip} data-track-index="2" id="chip" style="position:absolute;top:46px;left:70px;
       display:flex;align-items:center;gap:14px;padding:10px 22px;border-radius:999px;
       background:rgba(9,12,20,.72);border:1px solid #2b3242">
    <span style="width:13px;height:13px;border-radius:50%;background:#2ecc71;
          box-shadow:0 0 14px #2ecc71"></span>
    <span style="font-size:22px;letter-spacing:.22em;color:#dfe5f0;font-weight:700;
          text-transform:uppercase">Live desk · {G._esc(stamp)}</span></div>
  <div {clip} data-track-index="2" id="title" style="position:absolute;top:118px;left:70px">
    <div style="font-size:92px;font-weight:800;color:#fff;letter-spacing:1px;
         text-shadow:0 6px 30px #000c;line-height:1">THE BOOK</div>
    <div style="font-size:25px;letter-spacing:.28em;color:{_ACCENT};font-weight:700;
         text-transform:uppercase;margin-top:12px">What the system is actually doing</div></div>
  <audio src="{audio_rel}" data-start="0"></audio>
</div>
<script>
window.__timelines=window.__timelines||{{}};
const tl=gsap.timeline({{paused:true}});
gsap.set("#stage",{{x:{x0:.1f},y:{y0:.1f},scale:{s0:.3f}}});
tl.fromTo("#chip",{{opacity:0,y:-16}},{{opacity:1,y:0,duration:0.5}},0);
tl.fromTo("#title",{{opacity:0,x:-50}},{{opacity:1,x:0,duration:0.7,ease:"power3.out"}},0.15);
tl.to("#title",{{opacity:0,duration:0.5}},{_HOLD_S - 0.5:.2f});
tl.to("#hold",{{opacity:0,duration:0.8,ease:"power2.out"}},{_HOLD_S - 0.4:.2f});
{moves}
window.__timelines["main"]=tl;
</script></body></html>"""


def desk_cards(dg: dict) -> tuple[list[dict], list[dict]]:
    """(catalyst cards, indicator cards) for the NO-SCREENSHOT fallback, which
    reuses the ordinary bullet-slide layout so the deck still looks like itself."""
    cats = []
    for f in dg.get("fills_since", [])[:2]:
        cats.append({"icon": "🟢" if f["side"] == "buy" else "🔴",
                     "label": f"{f['side'].upper()} {f['qty']:g} {f['symbol']} at "
                              f"{_usd(f['price'], True)}",
                     "detail": (f"realised {_usd(f['pnl'], True)}"
                                if f.get("pnl") is not None else "since the last briefing")})
    for bk in dg.get("blocks", [])[:2]:
        cats.append({"icon": "⛔", "label": f"Risk layer blocked {bk['action']} "
                                           f"{_qty(bk['qty'])}{bk['symbol']}",
                     "detail": (bk.get("reason") or "")[:110]})
    for c in dg.get("changes", [])[:2]:
        cats.append({"icon": "🔀", "label": c[:90], "detail": ""})
    if not cats:
        cats.append({"icon": "⏸️", "label": "No fills since the last briefing",
                     "detail": "the engine stood pat"})

    inds = [{"icon": "💰", "label": "Equity", "value": _usd(dg.get("equity"), True)},
            {"icon": "📈", "label": "Day P&L", "value": _usd(dg.get("day_pl"), True)},
            {"icon": "🧮", "label": "Deployed", "value": f"{dg.get('deploy_pct', 0) * 100:.0f}%"},
            {"icon": "💵", "label": "Cash", "value": _usd(dg.get("cash"), True)}]
    for x in dg.get("positions", [])[:1]:
        inds.append({"icon": "📊", "label": x["symbol"],
                     "value": f"{_usd(x['pl'], True)} ({_pct(x['plpc'])})"})
    if dg.get("realized_pnl") is not None:
        inds.append({"icon": "🏁", "label": "Realised", "value": _usd(dg["realized_pnl"], True)})
    return cats[:4], inds[:6]


def render_desk_slide(payload: dict, dg: dict, lines: list[str], cfg: dict, date: str,
                      dest: Path, voice: str | None = None, rate: str | None = None,
                      log=print) -> dict | None:
    """Narration synth + hyperframes render of the desk slide. Same contract as
    story.render_bullet_slide: returns {"duration", "narration", "lines"} or None.
    `lines` is passed in (not derived) so the Mandarin pass can re-time the very
    same slide to the translated voiceover."""
    from . import story as S           # local: story pulls in the heavy render stack

    work = G.WORK
    assets = work / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for f in assets.glob("*"):
        f.unlink()
    tmp = work / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    beats = [{"n": i + 1, "narration": ln} for i, ln in enumerate(lines)]
    narr, csrt, timing = S.build_narration({"beats": beats}, tmp, cfg, voice=voice, rate=rate)
    shutil.copy(narr, assets / "DESK_narr.mp3")
    duration = G._duration(narr) + 0.4

    img = payload.get("image") or {}
    # is_file(), not exists(): with no image `Path("")` is Path("."), and "." very
    # much exists — that is how the dashboard-is-down branch ended up trying to
    # copy the current directory into the slide.
    shot = Path(img["path"]) if img.get("path") else None
    if shot and shot.is_file():
        shutil.copy(shot, assets / "desk.png")
        stamp = _stamp(dg.get("captured_at"))
        html = build_desk_slide_html("assets/desk.png", img["width"], img["height"],
                                     payload.get("panels") or {}, timing, duration, stamp,
                                     "assets/DESK_narr.mp3", cfg, view=img.get("viewport"))
    else:
        # No picture of the dashboard — say the same things over the deck's own
        # typographic layout rather than dropping the slide.
        cats, inds = desk_cards(dg)
        html = S.build_bullet_slide_html("THE DESK", None, cats, inds, duration, cfg, date)
        html = html.replace("assets/THE_DESK_narr.mp3", "assets/DESK_narr.mp3")
    (work / "index.html").write_text(html)

    renders = work / "renders"
    if renders.exists():
        shutil.rmtree(renders)
    r = subprocess.run(["npm", "run", "render"], cwd=work, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  [desk] hyperframes render failed: {r.stderr[-400:]}")
        return None
    mp4s = sorted(renders.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not mp4s:
        return None
    if cfg.get("subtitles", True) and G.srt_has_cues(csrt):
        G.burn_subtitles(mp4s[-1], csrt, dest, cfg)
    else:
        shutil.copy(mp4s[-1], dest)
    return {"duration": duration, "narration": " ".join(lines), "lines": lines}


def _stamp(iso: str | None) -> str:
    """Capture time in the operator's own timezone — the chip is a timestamp on
    the photograph, so it has to read as wall clock, not UTC."""
    try:
        dt = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        dt = datetime.now().astimezone()
    return dt.strftime("%-d %b %Y · %H:%M %Z")


def previous_digest(cfg: dict, slot: str | None = None) -> dict | None:
    """The most recent archived edition's desk digest — the baseline for "what
    changed". Any slot will do: the question is what the book did since the last
    time this show looked at it, not since the last identical edition."""
    root = Path(cfg.get("archive_dir", str(ROOT / "briefing" / "archive")))
    if not root.exists():
        return None
    metas = []
    for meta in root.glob("*/*/meta.json"):
        if meta.parent.parent.name.startswith("_"):
            continue
        try:
            m = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if m.get("desk") and m.get("generated_at"):
            metas.append((m["generated_at"], m["desk"]))
    if not metas:
        return None
    return max(metas, key=lambda t: t[0])[1]


if __name__ == "__main__":       # ad-hoc: print the fact sheet the narrator gets
    cfg = G.load_config()
    pl = capture(cfg, G.WORK / "tmp")
    if not pl:
        sys.exit("capture failed")
    dg, facts = digest(pl, previous_digest(cfg))
    print("\n".join(f"• {f}" for f in facts))

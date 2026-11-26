"""Capture the LIVE dashboard for the briefing's desk slide — image + data.

Runs in the TRADING venv (`.venv`), never the media env: playwright and the
read-only `web.server` builders both live there, and the briefing renderer talks
to this module only through a subprocess boundary. It writes a PNG of the
dashboard and a JSON payload beside it; `briefing/desk.py` (media side) reads
both back.

Everything comes from ONE page load, in this order:

    load -> freeze -> screenshot -> scrape DOM figures -> fetch the APIs

The freeze (killing every poll timer) is the part that matters. The dashboard
repaints itself every few seconds, so without it the picture and the numbers the
narrator speaks can come from different ticks — the video would then zoom into
an equity figure while the voice says a different one. After the freeze the
pixels are fixed, the scraped strings are literally what is in the picture, and
the API fetches go through the same server (and the same 5s cache) the page
itself drew from.

If the dashboard is not running, `--allow-headless` falls back to calling the
builders directly: no image, but the data is still real, and the desk slide
degrades to its typographic layout instead of disappearing.

DELIBERATELY READ-ONLY, like the dashboard it photographs: GET requests and
`build_*` calls only. Nothing here can place, cancel or size an order.

Usage (from the repo root):
    .venv/bin/python -m briefing.desk_probe --png /tmp/desk.png --json /tmp/desk.json
"""
from __future__ import annotations

import argparse
import json
import time
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Panels the slide can push into, and how the DOM names them. Keep these as
# stable class/id hooks — the zoom targets are looked up at capture time so a
# dashboard re-layout moves the camera with it instead of breaking it.
PANELS = {
    "portfolio": ".card--portfolio",
    "positions": ".card--positions",
    "board": ".card--board",
    "executions": "#execTable",
    "signals": "#journalTable",
    "health": "#health",
    "chart": ".card--chart",
}
# Wide tables read better cropped to their left columns: the zoom is
# width-bound, and the right-hand columns of these two are mostly dashes.
_CROP_W = {"executions": 1120, "signals": 1060}
_CROP_H = {"executions": 240, "signals": 260}

# The live-ticking figures, scraped from the frozen DOM so that every number the
# narrator is allowed to say is a number visible in the screenshot.
FIGURES = {
    "equity": "#equity",
    "equity_delta": "#equityDelta",
    "cash": "#cash",
    "buying_power": "#bp",
    "prev_close_equity": "#lastEquity",
    "halt": "#haltText",
    "positions_table": "#posTable",
}

_APIS = {"portfolio": "/api/portfolio", "board": "/api/board",
         "executions": "/api/executions?limit=60", "journal": "/api/journal?limit=40"}


def _token() -> str:
    tok = os.getenv("DASHBOARD_TOKEN", "").strip()
    if tok:
        return tok
    env = ROOT / ".env"
    if env.exists():
        m = re.search(r"^DASHBOARD_TOKEN=(.+)$", env.read_text(), re.M)
        if m:
            return m.group(1).strip()
    return ""


def capture(png: Path, url: str, width: int, height: int, timeout_ms: int,
            settle_ms: int) -> dict:
    """One page load -> image, panel boxes, on-screen figures, API payloads."""
    from playwright.sync_api import sync_playwright

    tok = _token()
    full_url = f"{url.rstrip('/')}/" + (f"?token={tok}" if tok else "")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": width, "height": height},
                                  device_scale_factor=1)
            pg.goto(full_url, wait_until="networkidle", timeout=timeout_ms)
            pg.wait_for_timeout(settle_ms)          # let the first API polls paint
            # FREEZE — see the module docstring. Everything below this line sees
            # the same tick the screenshot does.
            pg.evaluate("() => { for (let i = 1; i < 100000; i++) clearInterval(i); }")

            page_w, page_h = pg.evaluate(
                "() => [document.body.scrollWidth, document.body.scrollHeight]")
            pg.screenshot(path=str(png), full_page=True)

            panels = {}
            for name, sel in PANELS.items():
                try:
                    el = pg.query_selector(sel)
                    bb = el.bounding_box() if el else None
                except Exception:               # noqa: BLE001 — a missing panel is fine
                    bb = None
                if bb and bb["width"] > 40 and bb["height"] > 40:
                    box = {k: round(v) for k, v in bb.items()}
                    box["width"] = min(box["width"], _CROP_W.get(name, box["width"]))
                    box["height"] = min(box["height"], _CROP_H.get(name, box["height"]))
                    panels[name] = box

            figures = {}
            for name, sel in FIGURES.items():
                try:
                    el = pg.query_selector(sel)
                    figures[name] = el.inner_text().strip() if el else None
                except Exception:               # noqa: BLE001
                    figures[name] = None

            # Fetch through the PAGE so the data comes from the same server (and
            # the same short-lived cache) that drew the pixels above.
            data = pg.evaluate(
                """async (paths) => {
                     const out = {};
                     for (const [k, p] of Object.entries(paths)) {
                       try { out[k] = await (await fetch(p, {credentials: 'same-origin'})).json(); }
                       catch (e) { out[k] = {error: String(e)}; }
                     }
                     return out;
                   }""", _APIS)
        finally:
            browser.close()

    return {"image": {"file": png.name, "width": page_w, "height": page_h,
                      "viewport": [width, height]},
            "panels": panels, "figures": figures, **data}


def headless_data() -> dict:
    """Fallback when the dashboard is not running: the same read-only builders
    the dashboard would have called, minus the picture."""
    from web import server as s

    return {"image": None, "panels": {}, "figures": {},
            "portfolio": s.build_portfolio(), "board": s.build_board(),
            "executions": s.build_executions(limit=60), "journal": s.build_journal(limit=40)}


# How long a fresh account stays worth mentioning. Four weeks is long enough to
# cover the stretch where "lifetime realised $0.00 over 0 round trips" is the
# honest answer and sounds like a broken system, and short enough that the recap
# stops leading with old news once there is a real record to talk about.
_ACCOUNT_NEWS_DAYS = 28


def _account_context() -> dict | None:
    """Is this account NEW, and what came before it?

    WHY THE RECAP NEEDS THIS. Lifetime realised P&L is computed from the
    BROKER's own order history (web.server.build_executions), so the day the
    account changes it correctly resets to "$0.00 over 0 round trips" — an
    accurate sentence that, said aloud with no explanation, describes a system
    that has never traded. The account_switch row in the journal is the missing
    half of that sentence, and it is exactly the kind of fact the journal exists
    to hold.

    Best-effort and silent on failure, like every other block in this file.
    """
    try:
        from relay.config import JOURNAL_FILE
        last = None
        for ln in JOURNAL_FILE.read_text().splitlines():
            if '"account_switch"' not in ln:
                continue
            r = json.loads(ln)
            if str(r.get("event", "")) == "account_switch":
                last = r
        if last is None:
            return None
        age_days = (time.time() - float(last.get("ts", 0) or 0)) / 86400.0
        if age_days > _ACCOUNT_NEWS_DAYS:
            return None                     # old news; the record speaks for itself now
        return {
            "switched_days_ago": round(age_days, 2),
            "from_equity": last.get("from_equity"),
            "to_equity": last.get("to_equity"),
            # deliberately NOT the account numbers: they are credentials-adjacent
            # and a video is the wrong place for them
            "reason": str(last.get("reason") or ""),
        }
    except Exception:                                       # noqa: BLE001
        return None


def _csp_state() -> dict | None:
    """Where the weekly put strategy stands, plus its most recent decision.

    Gathered HERE, in the trading venv, because this is the process that already
    holds the Alpaca credentials — the media-side briefing env has neither the
    keys nor the relay on its path. Reads the journal for the last csp_* event
    rather than keeping a second state file: the journal is already the system's
    append-only record of what was decided and why, and a shadow-mode refusal is
    exactly the kind of decision it exists to hold.

    Entirely best-effort. The desk slide predates options and must still render
    without them, so any failure leaves the key absent and the sheet silent.
    """
    try:
        from relay.alpaca_client import AlpacaClient
        from relay.config import JOURNAL_FILE, get_settings
        from relay.csp_manager import assignment_state, config
        from relay.options_client import OptionsClient
        if not config().get("enabled"):
            return None
        settings = get_settings()
        try:
            from sigengine.engine import load_aux
            aux = load_aux()
        except Exception:                                   # noqa: BLE001
            aux = None
        st = assignment_state(OptionsClient(settings),
                              AlpacaClient(settings).get_all_positions(), aux=aux)
        last = None
        try:
            for ln in JOURNAL_FILE.read_text().splitlines():
                if '"csp_' not in ln:
                    continue
                r = json.loads(ln)
                ev = str(r.get("event", ""))
                if ev.startswith("csp_"):
                    last = {**r, "action": ev[4:]}
        except Exception:                                   # noqa: BLE001
            pass
        # A decision older than ~10 days is not "what the strategy is doing", it
        # is evidence the weekly job stopped running. Narrating it in the
        # present tense would be the recap confidently describing a strategy
        # that has not run in a month.
        if last is not None:
            try:
                if float(last.get("ts", 0) or 0) < (time.time() - 10 * 86400):
                    last = {"action": "stale", "ts": last.get("ts")}
            except (TypeError, ValueError):
                last = None
        st["last"] = last
        return st
    except Exception:                                       # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture the live dashboard for the desk slide")
    ap.add_argument("--png", required=True)
    ap.add_argument("--json", dest="json_out", required=True)
    ap.add_argument("--url", default=os.getenv("DASHBOARD_URL", "http://127.0.0.1:8787"))
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--no-shot", action="store_true",
                    help="skip the browser entirely and read the builders directly")
    args = ap.parse_args(argv)

    png = Path(args.png)
    png.parent.mkdir(parents=True, exist_ok=True)
    payload: dict
    if args.no_shot:
        payload = headless_data()
    else:
        try:
            payload = capture(png, args.url, args.width, args.height,
                              45000, 3500)
        except Exception as exc:                # noqa: BLE001 — degrade, never fail the render
            print(f"[desk] screenshot failed ({type(exc).__name__}: {exc}); "
                  f"falling back to data only", file=sys.stderr)
            png.unlink(missing_ok=True)
            payload = headless_data()

    # The options sleeve. Gathered here, in the trading venv, because this is
    # the process that already holds the Alpaca credentials — the media-side
    # briefing env has neither the keys nor the relay on its path. Best-effort:
    # the desk slide predates options and must still render without them.
    payload["csp"] = _csp_state()
    payload["account"] = _account_context()
    payload["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    Path(args.json_out).write_text(json.dumps(payload, indent=1))
    print(f"[desk] captured {'image + ' if payload.get('image') else ''}"
          f"{len(payload.get('panels') or {})} panel(s) -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

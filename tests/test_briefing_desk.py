"""The desk slide's pure logic: the numeric guard, the fact sheet, the camera.

No network, no browser, no ollama — everything here is the part that decides
what a public video is allowed to SAY about a real account, so it is the part
that has to be tested offline.
"""
from __future__ import annotations

import pytest

from briefing import desk, director
from briefing.publish_youtube import _hashtags, build_snippet


FACTS = [
    "Account equity $5,031.30, +$97.44 (+1.97%) today.",
    "Cash $277.32, buying power $14,420.03, 94.5% of the account deployed.",
    "Open position GOOG: 14 shares, entry $333.04, last $339.56, unrealised +$91.28 (+2.06%).",
]


# --------------------------------------------------------------------------
# the numeric guard
# --------------------------------------------------------------------------
@pytest.mark.parametrize("line", [
    "Equity is $5,031.30 and the book is up +$97.44 today.",
    "14 shares of GOOG, entry $333.04, now +2.06%.",
    "Cash $277.32 against buying power $14,420.03.",
    "The engine stood pat — no fills, nothing to report.",      # no digits at all
])
def test_guard_accepts_numbers_copied_from_the_sheet(line):
    assert director.bad_numbers(line, director._allowed_numbers(FACTS)) == []


@pytest.mark.parametrize("line,bad", [
    ("Equity is roughly $5,000 after a 2% gain.", ["$5,000", "2%"]),   # re-rounded
    ("Cash is about 5.5% of the account.", ["5.5%"]),                  # derived, even if true
    ("The account gained 97 dollars today.", ["97"]),                  # truncated
    ("GOOG is up 15 shares from here.", ["15"]),                       # invented outright
])
def test_guard_rejects_numbers_not_on_the_sheet(line, bad):
    assert director.bad_numbers(line, director._allowed_numbers(FACTS)) == bad


def test_guard_treats_trailing_zeros_as_the_same_number():
    allowed = director._allowed_numbers(["Equity $5,031.30."])
    assert director.bad_numbers("Equity $5,031.3.", allowed) == []


# --------------------------------------------------------------------------
# the fact sheet
# --------------------------------------------------------------------------
def _payload(**over):
    p = {
        "captured_at": "2026-07-31T04:00:00+00:00",
        "image": {"file": "desk.png", "width": 1920, "height": 1600, "viewport": [1920, 1080]},
        "panels": {"portfolio": {"x": 0, "y": 0, "width": 600, "height": 300}},
        "figures": {"equity": "$5,031.30", "equity_delta": "+$97.44 (+1.97%) today",
                    "cash": "$277.32", "buying_power": "$14,420.03", "halt": "0.00% of 5%",
                    "positions_table": "SYMBOL\tQTY\tENTRY\tLAST\tVALUE\tUNREAL. P&L\tSTOP\tTO STOP\n"
                                       "GOOG LONG\t14\t$333.04\t$339.56\t$4,753.84\t"
                                       "+$91.28 (+2.06%)\t$326.38\t3.99%"},
        "portfolio": {"equity": 5031.3, "cash": 277.32, "day_pl": 97.44, "day_pl_pct": 0.0197,
                      "halt": {"limit": 0.05, "tripped": False},
                      "positions": [{"symbol": "GOOG", "qty": 14.0, "avg_entry_price": 333.04,
                                     "current_price": 339.56, "market_value": 4753.84,
                                     "unrealized_pl": 91.28, "unrealized_plpc": 0.0206,
                                     "stop_price": 326.38, "stop_distance_pct": 0.0399}]},
        "board": {"date": "2026-07-30", "max_open_positions": 1,
                  "rows": [{"symbol": "MSFT", "bias": "long", "priority": 2, "suggested_size": 0.97},
                           {"symbol": "GOOG", "bias": "long", "priority": 1, "suggested_size": 0.0},
                           {"symbol": "TSLA", "bias": "flat", "priority": 0}]},
        "executions": {"rows": [{"ts": 1785400000.0, "symbol": "NVDA", "side": "sell", "qty": 23.0,
                                 "price": 198.64, "pnl": -213.01}],
                       "realized_pnl": -60.03, "round_trips": 3, "win_rate": 1 / 3},
        "journal": {"rows": [{"event": "risk_block", "ticker": "AMZN", "action": "buy",
                              "qty": 20.0, "reason": "AMZN bias=flat, blocking new buy entry",
                              "_ts": 1785400000.0}]},
    }
    p.update(over)
    return p


def test_every_number_the_narrator_may_speak_is_on_the_sheet():
    """The guard is only as good as the sheet: the live figures must reach it as
    the exact strings the dashboard drew, or a truthful line gets rejected."""
    _, facts = desk.digest(_payload())
    allowed = director._allowed_numbers(facts)
    for tok in ("5031.30", "97.44", "1.97", "333.04", "339.56", "91.28", "326.38"):
        assert tok in allowed or tok.rstrip("0").rstrip(".") in allowed, tok


def test_facts_prefer_the_on_screen_strings():
    _, facts = desk.digest(_payload())
    assert facts[0] == "Account equity $5,031.30, +$97.44 (+1.97%) today."
    assert any("Open position GOOG: 14 shares, entry $333.04" in f for f in facts)


def test_facts_fall_back_to_the_api_when_there_is_no_screenshot():
    p = _payload(figures={}, image=None)
    dg, facts = desk.digest(p)
    assert dg["has_shot"] is False
    assert "$5,031.30" in facts[0] and "+1.97%" in facts[0]


def test_board_is_ranked_highest_priority_first():
    """MSFT at priority 2 outranks GOOG at 1. Said in board order it reads the
    other way round, which is a factual error the numeric guard cannot catch."""
    _, facts = desk.digest(_payload())
    line = next(f for f in facts if f.startswith("Decision board"))
    assert line.index("MSFT") < line.index("GOOG")
    assert "MSFT is the top-ranked long" in line


def test_fills_and_blocks_are_scoped_to_since_the_last_briefing():
    prev = {"captured_at": "2026-07-31T03:00:00+00:00", "board": [], "positions": []}
    dg, _ = desk.digest(_payload(), prev)          # fill ts is 2026-07-31 ~01:46Z
    assert dg["fills_since"] == []
    prev_old = {"captured_at": "2026-07-20T00:00:00+00:00", "board": [], "positions": []}
    dg2, facts = desk.digest(_payload(), prev_old)
    assert [f["symbol"] for f in dg2["fills_since"]] == ["NVDA"]
    assert any("Filled since the last briefing" in f for f in facts)


def test_changes_are_diffed_against_the_previous_edition():
    prev = {"captured_at": "2026-07-31T03:00:00+00:00",
            "board": [{"symbol": "MSFT", "bias": "flat", "priority": 0},
                      {"symbol": "GOOG", "bias": "long", "priority": 1}],
            "positions": [{"symbol": "NVDA"}]}
    dg, _ = desk.digest(_payload(), prev)
    assert "MSFT moved from flat to long on the board" in dg["changes"]
    assert "the book opened GOOG" in dg["changes"]
    assert "the book closed NVDA" in dg["changes"]


def test_flat_account_still_produces_a_sheet():
    p = _payload(figures={}, image=None)
    p["portfolio"]["positions"] = []
    _, facts = desk.digest(p)
    assert any("No open positions" in f for f in facts)


def test_digest_raises_when_the_broker_call_failed():
    with pytest.raises(RuntimeError):
        desk.digest(_payload(portfolio={"error": "APIError: forbidden"}))


def test_fallback_script_covers_balance_book_and_decisions():
    """Built from the digest, not the sheet: read aloud, the number-dense sheet
    runs ~60s against the written slide's 45s."""
    dg, facts = desk.digest(_payload())
    lines = desk.fallback_lines(dg, facts, 4)
    assert 0 < len(lines) <= 4
    assert all(ln.strip() for ln in lines)
    assert "$5,031.30" in lines[0]                       # balance
    assert "14 GOOG" in lines[1] and "$326.38" in lines[1]   # the book
    assert "ranks MSFT top" in lines[2]                  # decisions
    assert "No fills since the last briefing" in lines[3] and "blocked 1" in lines[3]
    assert sum(len(l) for l in lines) < 420


def test_fallback_script_handles_a_flat_account():
    p = _payload()
    p["portfolio"]["positions"] = []
    p["figures"].pop("positions_table")
    p["board"]["rows"] = [{"symbol": "TSLA", "bias": "flat", "priority": 0}]
    dg, facts = desk.digest(p)
    lines = desk.fallback_lines(dg, facts)
    assert "The book is flat" in lines[1]
    assert "is flat, so nothing new opens" in lines[2]


# --------------------------------------------------------------------------
# the camera
# --------------------------------------------------------------------------
PANELS = {k: {"x": 0, "y": 0, "width": 600, "height": 300}
          for k in ("portfolio", "positions", "board", "executions", "signals")}


@pytest.mark.parametrize("line,panel", [
    ("Account equity is $5,031.30 and the book is up +1.97% on the day.", "portfolio"),
    # mentions "no fills" but is ABOUT the position — scored, not first-match
    ("The only open position is 14 shares of GOOG, no fills since the last briefing, "
     "stop at $326.38.", "positions"),
    ("The risk layer blocked a buy of 20 AMZN because its bias was flat.", "signals"),
    ("MSFT is the top-ranked long on the decision board and must displace GOOG.", "board"),
    ("Lifetime realised -$60.03 over 3 round trips, win rate 33%.", "executions"),
])
def test_camera_pushes_into_the_panel_the_line_is_about(line, panel):
    assert desk.panel_for(line, PANELS, []) == panel


def test_camera_walks_the_dashboard_when_nothing_matches():
    assert desk.panel_for("A quiet session all round.", PANELS, []) == "portfolio"
    assert desk.panel_for("A quiet session all round.", PANELS, ["portfolio"]) == "positions"


def test_camera_stays_put_when_there_is_no_screenshot():
    assert desk.panel_for("Account equity is $5,031.30.", {}, []) is None


def test_zoom_is_clamped_both_ways():
    lo, hi = 1.2, 2.6
    tiny = desk._zoom({"x": 0, "y": 0, "width": 40, "height": 20}, 1920, 1080, lo, hi)
    huge = desk._zoom({"x": 0, "y": 0, "width": 1900, "height": 1500}, 1920, 1080, lo, hi)
    assert tiny[0] == hi and huge[0] == lo


def test_establishing_shot_frames_the_fold_not_the_scrolled_page():
    """A 1600px page fitted whole shrinks to ~60% and reads as nothing."""
    fold, _, _ = desk._fit(1920, 1600, 1920, 1080, [1920, 1080])
    whole, _, _ = desk._fit(1920, 1600, 1920, 1080, None)
    assert fold > whole and fold >= 0.85


# --------------------------------------------------------------------------
# publishing: DESK is a slide, not a ticker
# --------------------------------------------------------------------------
def _meta():
    return {"date": "2026-07-31", "label": "Midday briefing", "title": "Something happened",
            "kind": "slideshow",
            "clips": [{"symbol": "MARKET", "headline": "Broad tape"},
                      {"symbol": "DESK", "headline": "Equity $5,031.30, one position open"},
                      {"symbol": "GOOG", "headline": "GOOG grinds higher"}]}


def test_desk_never_becomes_a_hashtag_or_a_keyword_tag():
    cfg = {"youtube": {"hashtags": ["stocks"], "base_tags": ["market briefing"]}}
    assert "#DESK" not in _hashtags(_meta(), cfg)
    assert "#GOOG" in _hashtags(_meta(), cfg)
    snippet = build_snippet(_meta(), "en", cfg)
    assert "DESK" not in snippet["tags"]
    # …but it still gets its chapter line in the description.
    assert "DESK — Equity $5,031.30" in snippet["description"]

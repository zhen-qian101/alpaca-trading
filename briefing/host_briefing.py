#!/usr/bin/env python3
"""The presenter narrates the briefing deck: one writer call, one host clip per slide, PiP.

The presenter is whoever briefing/config.json host.host_json names (陈谷雨,
host2.json, since 2026-08-12). The slideshow keeps its whole machinery —
hyperframes slides, subtitle burn, concat, publish — and gains three things:

  1. THE SCRIPT. One deepseek call writes the entire deck in the presenter's
     voice: an intro slide over the live dashboard, then each symbol, each with
     its own MOOD. Replaces director.direct_briefing's per-scene narration (a
     newsreader's) with a person whose account it is.
  2. THE VOICE. host_narration.synth_host_audio — Qwen3-TTS per sentence, SRT
     from measured durations.
  3. THE FACE. One LTX clip per slide on the locked config, every clip opening
     from THE SAME day keyframe (user decision 2026-08-11 — per-clip mood
     keyframes retired; the stylist's approved daily look wins when present,
     else the presenter's fixed keyframe). The writer's `mood` still shapes the
     composer's performance text — just not the opening frame.

Enabled from generate.py by cfg["host"]["enabled"] or BRIEFING_HOST=1, which
agent/run_research_postmarket.sh sets for the weekday post-market recap.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .host_narration import synth_host_audio
from .ltx_official import PERFORMANCE_RULES, compose
from .ltx_story import strip_held_gesture


def day_keyframe(date: str | None, host: dict) -> Path:
    """THE render keyframe — one per day, every clip (user decision 2026-08-11;
    the per-clip mood keyframes are retired). The stylist's padded look for the
    date wins when its history record exists; otherwise the presenter's fixed
    `keyframe`. The writer's `mood` still shapes the PERFORMANCE (composer
    text) — just not the opening frame.

    THE HISTORY IS PER PRESENTER. Each host's stylist writes into its own
    directory (陈谷雨's into assets/host2/stylist), and this reads the one
    belonging to the host that is actually presenting, via `stylist_dir`.
    Reading a single shared path is how a look designed for one presenter gets
    served to another, which no exception reports and only watching the video
    catches. There is no default directory and no default keyframe: a presenter
    with neither an approved look nor a `keyframe` stops the render here.

    A host carrying a FIXED `keyframe` falls back to it when the day has no
    stylist look — either because the stylist is switched off for that host, or
    because the day's design/draw missed its gates. 陈谷雨 shipped that way from
    2026-08-12 ("always starting from the same outfit keyframe for now"), and it
    remains the failure-safe floor now that he has a stylist: an off-model host
    never ships because of an outfit.

    A look must also be APPROVED (2026-08-20). The stylist runs at 09:35 ET and
    the render at 20:00 ET, so there is a ~10h window in which a human can look
    at the day's outfit and keyframe — and until this gate existed, nothing made
    use of it: whatever the stylist drew went into the video, and the first
    review was the finished render, where the only remaining lever is a
    re-render. Sign-off is `"approved": true` in the look record, written by the
    dashboard's review page (web/static/look.html) or by hand.

    UNAPPROVED IS NOT AN ERROR. It takes exactly the same path as a look that
    missed its identity gates: the locked keyframe stands and the video ships on
    time in a known-good outfit. That is the point — a gate that could block
    tonight's render would be a worse failure than any outfit.
    """
    host = host or {}
    # BRIEFING_LOOK_LOCK=1 forces the locked keyframe even for an APPROVED look.
    # For REPAIRS, not for production. The approval can land after the render has
    # already shipped in the locked outfit (2026-09-18: signed off at 20:37 AEST,
    # eight hours after the render finished), and the keyframe is in the clip
    # cache key — so re-rendering one broken clip would redraw it in the newly
    # approved outfit while its twelve siblings stay in the locked one, and the
    # host would change clothes mid-video. Pin the keyframe the rest of the
    # render already used, the same way BRIEFING_DECK_FREEZE pins the narration.
    if os.environ.get("BRIEFING_LOOK_LOCK") == "1":
        print("  [stylist] BRIEFING_LOOK_LOCK=1 — holding the locked keyframe")
    elif date and host.get("stylist", True) and host.get("stylist_dir"):
        hist = ROOT / host["stylist_dir"] / f"{date}.json"
        try:
            rec = json.loads(hist.read_text())
            kf = ROOT / rec["keyframe"]
            if rec.get("approved") and kf.exists():
                return kf
            if kf.exists():
                print(f"  [stylist] {date}'s look is not signed off — "
                      f"wearing the locked keyframe instead")
        except Exception:                                   # noqa: BLE001
            pass
    if host.get("keyframe"):
        kf = ROOT / host["keyframe"]
        if not kf.exists():
            raise SystemExit(f"  [host] fixed keyframe missing: {kf}")
        return kf
    raise SystemExit(f"  [host] {host.get('name', 'this presenter')} has no "
                     f"`keyframe` and no approved stylist look for {date}")
from .ltx_story import guide_frames, _free_gpu

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _sha(*parts) -> str:
    """Stable key over strings and file CONTENTS."""
    import hashlib
    h = hashlib.sha1()
    for p in parts:
        if isinstance(p, Path):
            h.update(p.read_bytes())
        else:
            h.update(str(p).encode())
    return h.hexdigest()


def cache_ok(meta: Path, key: str) -> bool:
    try:
        return json.loads(meta.read_text()).get("key") == key
    except Exception:                                       # noqa: BLE001
        return False


def cache_write(meta: Path, key: str, **extra) -> None:
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"key": key, **extra}))


def tts_version(lang: str) -> str:
    """Part of the TTS cache key: bust a language's wavs by returning a new tag
    here when its SYNTHESIS changes while the deck line does not. Empty string
    hashes to a no-op, so "" keeps existing keys byte-identical. (The pinyin
    annotation intended to be v2-zh was measured read-aloud and never wired.)

    zh-samelang-ref-v2 (2026-08-12): Mandarin now clones from a SAME-LANGUAGE
    reference (clone_ref_zh) instead of cross-lingually from the English clip —
    the documented robotic-accent cause."""
    return "zh-samelang-ref-v2" if lang == "zh" else ""

# The locked render config. One home.
# omni_lora 2.0 adopted 2026-08-11 BY USER DECISION over the A/B's mixed read:
# en clearly improved (margin 0.230->0.346, identity +0.03), zh traded more
# pause-motion (silence 0.035->0.049) and a small identity dip for +22% travel.
# If production zh clips start failing the gate on silence-motion, suspect this
# first and set it back to 0.0.
LOCKED = {
    # img_strength 1.0 is the "anchor" config (user-picked 2026-08-11 after the
    # zh A/B): with PINNED audio driving the mouth, the full-strength keyframe
    # anchor costs no mouth motion (travel 0.070) and lifts identity-in-motion
    # to 0.62-0.64 — the best ever measured. The old "1.0 freezes the face"
    # rule applies only when the anchor fights an UNPINNED mouth: never run
    # audio_mode=native at this strength.
    "id_lora": 1.0, "img_strength": 1.0, "guide_strength": 0.5,
    "nag_scale": 5.0, "img_compression": 0, "seed": 4242,
    "width": 1280, "height": 720, "omni_lora": 2.0,
    # neg_version busts clip caches when the probe's NEG list changes — the NEG
    # text itself lives in ltx_ia2v_probe and is not otherwise in the key
    # (v2: microphone/headset/lavalier/earpiece added after native mode drew a
    # headset mic on the presenter's cheek. v3: the grip class — light stand,
    # c-stand, tripod, boom/magic arm, clamp, softbox — after a magic-arm rig
    # grew out of the wall on the 2026-08-18 SOXL 中文 clip and the host reached
    # over and touched it.)
    "neg_version": 3,
    # audio_mode "pinned" (user decision 2026-08-11, reversing the brief native
    # era): Qwen3-TTS clone audio drives the mouth — LTX's own zh speech was
    # measurably robotic (English-dominant training; the 5s reference transfers
    # timbre, not Mandarin prosody), and the community avatar workflow pins for
    # the same reason. Native mode has since been removed from the code.
    "audio_mode": "pinned",
}

# How many seeds a failing clip is allowed before the best one ships anyway.
# Each costs a full render, and the recap has a publish slot to make, so this is
# a schedule decision as much as a quality one: on 2026-08-12 exactly one beat of
# twelve needed retries at all, and it burned all three.
QC_SEEDS = 3

def audio_mode() -> str:
    """The clip's audio mode — always LOCKED["audio_mode"] ("pinned"). It is in
    the prompt cache key, so it stays a function. Native mode (LTX speaking the
    line itself from a 5 s voice reference) has been removed from the code;
    BRIEFING_AUDIO may only restate the locked mode."""
    env = os.environ.get("BRIEFING_AUDIO")
    if env and env != LOCKED["audio_mode"]:
        raise SystemExit(f"  [host-clip] BRIEFING_AUDIO={env!r} is not supported "
                         f"— only {LOCKED['audio_mode']!r} audio is rendered")
    return LOCKED["audio_mode"]


_SLIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slide": {"type": "string"},
                    "line": {"type": "string"},
                    "mood": {"type": "string"},
                    "expression": {"type": "string"},
                    "gestures": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["slide", "line", "mood", "expression",
                             "gestures", "body"],
            },
        },
    },
    "required": ["slides"],
}


DECK_PROMPT_VERSION = "v4-options-sleeve"   # bumped on brief changes: busts the deck cache


_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("_ _ twenty thirty forty fifty sixty seventy eighty ninety").split()


def _spell(n: int) -> str:
    """English words for 0..999,999. Used only to gloss a figure for the writer."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else "")
    if n < 1000:
        return (_ONES[n // 100] + " hundred"
                + (f" {_spell(n % 100)}" if n % 100 else ""))
    return (_spell(n // 1000) + " thousand"
            + (f" {_spell(n % 1000)}" if n % 1000 else ""))


def _say_money(x: float) -> str:
    """A dollar figure plus the parts to say it from, so the writer never rescales.

    THE BUG THIS EXISTS FOR: the 中文 writer turned $4,961.70 into
    四万九千六百一十七美元 — $49,617, an order of magnitude out — in the FIRST
    SENTENCE of the video, which is the one number the whole opening is built on.
    English groups digits in thousands and Chinese groups them in 万
    (ten-thousands), so any figure that has to cross that boundary while being
    said in the other language is exactly where the conversion breaks.

    Giving the magnitude in words removes the conversion step entirely, and the
    explicit under/over assertion blocks the specific 10x slip rather than merely
    asking for care.
    """
    d, c = int(abs(x)), int(round((abs(x) - int(abs(x))) * 100))
    words = _spell(d) + (f" dollars and {_spell(c)} cents" if c
                         else " dollars exactly")
    scale = ("under ten thousand dollars" if d < 10_000 else
             "under a hundred thousand dollars" if d < 100_000 else
             "over a hundred thousand dollars")
    return (f"${abs(x):,.2f} — say it as \"{words}\", built from {d // 1000} "
            f"thousand, {(d // 100) % 10} hundred, {(d // 10) % 10} ten, "
            f"{d % 10}, and {c} cents. It is {scale}. Whatever language you are "
            f"writing in, do NOT rescale it to a bigger unit")


def desk_hero(dg: dict | None) -> dict | None:
    """The VISUAL half of the opening hook: what the OVERVIEW slide shows while
    he says it. Returns {tick, sub, pill} or None.

    Deliberately branched the same way as _opening_hook() and sitting next to it,
    because the failure mode of splitting them is a slide whose big number
    disagrees with the sentence being spoken over it — worse than having no
    number at all. One dashboard digest in, one hook out, both channels.

    Why the slide needs its own copy at all: `line` reaches the viewer through
    24 seconds of speech, but the title block is on screen at frame 0 with no
    entry animation, and the stat pills land at 1.5, 1.85, 2.2 and 2.55 seconds.
    Only the FIRST pill is inside the three-second window the hook exists for,
    which is why the P&L is prepended rather than appended.
    """
    if not dg:
        return None
    pl = float(dg.get("day_pl") or 0.0)
    pct = float(dg.get("day_pl_pct") or 0.0) * 100.0
    eq = float(dg.get("equity") or 0.0)
    pos = dg.get("positions") or []
    if pos and abs(pl) >= 1.0:
        return {"tick": f"{'+' if pl >= 0 else '-'}${abs(pl):,.2f}",
                "sub": f"MY P&L TODAY · {pct:+.2f}% · ${eq:,.2f}",
                "pill": {"icon": "💰", "label": "Day P&L",
                         "value": f"{'+' if pl >= 0 else '-'}${abs(pl):,.2f}"}}
    if pos:
        best = max(pos, key=lambda p: abs(float(p.get("plpc") or 0)))
        bp = float(best.get("plpc") or 0) * 100.0
        return {"tick": f"{best['symbol']} {bp:+.1f}%",
                "sub": f"MY ONLY POSITION · FLAT ON THE DAY · ${eq:,.2f}",
                "pill": {"icon": "💰", "label": best["symbol"],
                         "value": f"{bp:+.1f}%"}}
    # All cash. "$0.00" is a limp hero for the same reason it is a limp opening
    # line, so the headline is the DECISION and the equity moves to the subline.
    return {"tick": "ALL CASH",
            "sub": f"NO TRADES TODAY · ${eq:,.2f}",
            "pill": {"icon": "💵", "label": "All cash", "value": f"${eq:,.2f}"}}


def _opening_hook(dg: dict | None) -> str:
    """THE ONE FACT the first slide opens on — decided HERE, not by the writer.

    Paid for by a measured miss. On the 2026-08-16 deck the first eight words a
    viewer heard were "Good evening. The market finally took a rest after", and
    the presenter's own P&L did not land until the DESK slide about 32 seconds
    in. At the 2.7 words/second this prompt budgets for, three seconds IS eight
    words, so those eight words are the entire hook and a market summary cannot
    be one.

    Two edge cases are resolved here rather than in the prompt, because a model
    told to "open on the P&L" will faithfully open on "I made zero dollars today":

      * flat or empty account -> there is no P&L worth saying, so open on what
        the account is sitting in cash FOR. (This is not hypothetical: the 08-14
        capture is $4,961.70, all cash, day P&L exactly 0.)
      * no dashboard -> no P&L exists to claim at all, and _account_truth's
        conservative branch forbids inventing one, so open on the market instead.
        The two blocks must never disagree, which is why they are computed
        together.

    It rides INSIDE the truth block on purpose: `truth` is part of the deck cache
    key (generate.py), so changing what the deck opens on rewrites it and
    re-renders the clips built on the old opening, with no extra plumbing.
    """
    if not dg:
        return ("\nTHE OPENING NUMBER — none. The dashboard is down, so you do not "
                "know her P&L and must not invent one. The FIRST slide instead "
                "opens on the single most striking NUMBER in its own note — a "
                "percentage, a price, a dollar move — inside its first sentence.")
    pl = float(dg.get("day_pl") or 0.0)
    pct = float(dg.get("day_pl_pct") or 0.0) * 100.0     # digest stores a FRACTION
    eq = float(dg.get("equity") or 0.0)
    pos = dg.get("positions") or []
    if pos and abs(pl) >= 1.0:
        hook = (f"she {'made' if pl >= 0 else 'lost'} {_say_money(pl)} today "
                f"({pct:+.2f}%), on an account of {_say_money(eq)}")
    elif pos:
        # Holding, but the day went nowhere. The day number is not the story; the
        # open position is, so hand the writer the loudest one instead.
        best = max(pos, key=lambda p: abs(float(p.get("plpc") or 0)))
        hook = (f"the day P&L is flat ({'+' if pl >= 0 else '-'}${abs(pl):,.2f}), "
                f"so the number to open on is "
                f"the position itself: {best['symbol']}, "
                f"{float(best.get('plpc') or 0) * 100:+.1f}% since she bought it, "
                f"in an account of {_say_money(eq)}")
    else:
        hook = (f"she holds NOTHING. The account is {_say_money(eq)}. Every cent "
                f"of it is cash and she made exactly nothing today.\n"
                f"That opening is THREE BEATS, in this order, before any market "
                f"news at all:\n"
                f"  1. she bought nothing — said as a decision she took, never as "
                f"an absence: \"I didn't buy a single thing today\", not \"there "
                f"were no trades\";\n"
                f"  2. the cash figure above;\n"
                f"  3. the ONE symbol she came closest to buying, named outright, "
                f"so the opening carries tension instead of a shrug.\n"
                f"All three, then the market. Beat 3 is the one that gets dropped; "
                f"a first slide without it has not done its job")
    return ("\nTHE OPENING NUMBER — " + hook + ".\n"
            "The FIRST slide's FIRST SENTENCE is this and nothing else. No "
            "greeting, no date, no \"welcome back\", and no market scene-setting "
            "in front of it. A viewer decides in about three seconds, which at "
            "this speaking pace is eight words, so the eight words are the "
            "number. Everything else on that slide follows it.")


def _account_age_truth(dg: dict | None) -> str:
    """The account is NEW, and the presenter is allowed — required — to say so.

    Two failures this prevents, both of them things the presenter would
    otherwise say on camera with complete confidence:

      * "Lifetime realised zero dollars over zero round trips." True, and it
        describes a system that has never traded. The reason it is zero is that
        the broker account changed; without that clause the sentence is a
        confession of failure rather than a fact about a fresh ledger.
      * Reaching back for the OLD account's record to fill the gap. The previous
        account's 11 round trips and -$151.15 are archived and are not this
        account's history. Claiming them here would be inventing a P&L, which
        the ground-truth block forbids everywhere else.

    Says nothing at all once the account is no longer new (desk_probe drops the
    key after four weeks) or once there are real round trips to talk about.
    """
    ac = (dg or {}).get("account") or {}
    if ac.get("switched_days_ago") is None:
        return ""
    if (dg or {}).get("round_trips"):
        return ""                       # there is a real record now; let it speak
    ago = ac["switched_days_ago"]
    when = "today" if ago < 1 else "yesterday" if ago < 2 else f"{ago:.0f} days ago"
    eq = float((dg or {}).get("equity") or 0.0)
    prev = ac.get("from_equity")
    # _say_money expands into a whole pronunciation instruction, so each call has
    # to END its clause. Buried mid-sentence it reads as gibberish to the writer
    # and the sentence around it gets lost.
    grew = (f"  * The account it replaces was far smaller — every number in this book is now "
            f"on a different scale. The old one held {_say_money(prev)}\n"
            if prev and prev < eq * 0.5 else "")
    return (f"\nHER ACCOUNT IS NEW, AND THAT IS WHY THE RECORD IS EMPTY:\n"
            f"  * The book moved {when} to a fresh paper account holding {_say_money(eq)}\n"
            f"{grew}"
            f"  * So \"lifetime realised zero, zero round trips\" is a FRESH LEDGER, not a "
            f"failure. If she mentions the empty record she must say why in the same "
            f"breath. She may say the track record starts here.\n"
            f"  * She must NOT claim the previous account's trades, wins or losses as her "
            f"own history. That book is closed and its P&L is not hers to quote.\n")


def _options_truth(dg: dict | None) -> str:
    """The OPTIONS half of the ground truth.

    Without this the presenter is barred from mentioning the put at all: the
    block below says the listed positions are the only authority and that
    anything not listed must never be claimed as the presenter's. A short option
    is not a `position` row, so an honest writer following the rule would refuse
    to say a word about the sleeve — and the operator asked for it in the desk
    clip.

    Written in the same voice as the rest of the truth block: state what is
    true, name what the presenter may claim, and be explicit where the honest
    thing is an admission rather than a number.
    """
    c = (dg or {}).get("csp") or {}
    if not c.get("enabled"):
        return ""
    und = c.get("underlying", "SOXL")
    sp, asg, last = c.get("short_put"), c.get("assigned"), c.get("last")
    act = (last or {}).get("action")
    head = "\nHER OPTIONS SLEEVE — a weekly cash-secured put, and hers to speak about:\n"
    if asg:
        # Partial data renders "ASSIGNED None shares at $None" if interpolated
        # raw — say what is unknown instead of printing the word None.
        n = f"{asg['qty']:g}" if asg.get("qty") is not None else "an unknown number of"
        at = (f" at an average of {_say_money(asg['avg_price'])}"
              if asg.get("avg_price") is not None else "")
        body = (f"  * She was ASSIGNED {n} shares of {und}{at}. She owns them; the "
                f"loss or gain on them is her money. No new put is sold while she "
                f"holds them.\n")
        if asg.get("adopted"):
            # The interesting half of the story: the shares are not adrift and
            # they are not being nursed. The plan is one line long.
            by = asg.get("sell_by")
            body += ("  * Those shares are sold at the sleeve's next weekly run"
                     + (f", on {by}" if by else "")
                     + " — that is the whole plan. No stop, no covered calls: both "
                     "were tested and both did worse than simply selling within the "
                     "week. She may say she is holding them until then. She must NOT "
                     "invent a target, a stop or a hedge.\n")
        elif asg.get("managed_by_engine") is False:
            body += ("  * SAY THIS PLAINLY IF THE SLIDE HAS ROOM: nothing is watching "
                     "those shares. They were not opened by her usual signals, so no "
                     "exit signal will ever fire for them and getting out is a "
                     "decision she has to make herself.\n")
    elif sp:
        k = f" at the {sp['strike']:g} strike" if sp.get("strike") is not None else ""
        e = f", expiring {sp['expiry']}" if sp.get("expiry") else ""
        n = f"{sp['contracts']:g}" if sp.get("contracts") is not None else "a"
        body = (f"  * She is SHORT {n} {und} put{k}{e}. She took a premium for it and "
                f"she is on the hook to buy the shares if {und} falls that far. That "
                f"obligation is hers.\n"
                f"  * SHE DOES NOT OWN {und} SHARES. The obligation is hers to speak "
                f"about; the stock is not. She must not say \"my {und}\" or claim its "
                f"move as her own money.\n")
    elif act == "shadow":
        # TWO DIFFERENT SHADOWS, and saying the wrong one is a false statement
        # about the presenter's own account. A `shortfall` on the journalled
        # decision means gate 4 refused it — the account genuinely could not
        # pay. No shortfall means the money was there and the sleeve is being
        # watched on purpose. Before the account moved to $100,000 only the
        # first was possible, so the reason was hard-coded into the sentence;
        # now it would be a lie half the time.
        if (last or {}).get("shortfall"):
            body = ("  * The put strategy is in SHADOW MODE — it picks the trade every "
                    "week and does not place it, because the account cannot cover the "
                    "collateral yet. She is watching what it WOULD have done. She has "
                    "not made or lost a cent on it and must not pretend otherwise.\n")
        else:
            n = (last or {}).get("contracts")
            size = ""
            if isinstance(n, (int, float)) and n and n > 1:
                size = (f" This week it sized itself at {int(n)} contracts — the count "
                        f"is worked out from the sleeve's reserve, not fixed, so it "
                        f"moves with the account.")
            body = ("  * The put strategy is in SHADOW MODE BY CHOICE — the cash is "
                    "there, and it still only picks the trade and journals it rather "
                    "than placing it. She is watching it prove itself before it trades."
                    + size +
                    " She has not made or lost a cent on it and must not pretend "
                    "otherwise, and she must NOT say she could not afford it.\n")
    elif act == "sell":
        # The generic branch used to render this as "No put was sold this week.
        # Reason: sold to open" — the exact opposite of the truth, in the block
        # the presenter is told is authoritative.
        body = (f"  * A put WAS sold to open this week. The live position could not be "
                f"read just now, so give no numbers she cannot see — say she sold one "
                f"and leave it there.\n")
    elif act == "stale":
        body = ("  * The put strategy has not made a decision in over a week — the "
                "weekly job may not be running. She should say she does not know "
                "where it stands rather than guess.\n")
    elif act == "skip":
        body = (f"  * No put was sold. Reason: {last.get('reason', 'the gates refused it')}\n")
    else:
        body = "  * The put strategy is on but has not decided anything yet.\n"
    return head + body


def _account_truth(dg: dict | None) -> str:
    """The ground-truth block: what the presenter ACTUALLY holds and recently
    traded, straight from the live dashboard digest, plus the opening hook
    derived from the same numbers. When the desk is unavailable the rule
    collapses to the conservative one — no ownership claims at all."""
    if not dg:
        return ("ACCOUNT GROUND TRUTH: the live dashboard is unavailable, so you "
                "do not know what she holds. Make NO ownership claims of any "
                "kind — no \"my X\", no \"I bought\", no \"I sold\", no P&L "
                "of hers. She watches and comments; that is all."
                + _opening_hook(dg))
    pos = dg.get("positions") or []
    lines = [f"  * {p['symbol']}: {p['qty']} shares, "
             f"{float(p.get('plpc') or 0) * 100:+.1f}% unrealised"
             for p in pos] or ["  (none — she holds NOTHING right now)"]
    fills = dg.get("fills_since") or []
    flines = [f"  * {f['side']} {f['qty']} {f['symbol']} @ {f['price']}"
              for f in fills[:6]]
    return ("ACCOUNT GROUND TRUTH — the live dashboard, the ONLY authority on "
            "what she owns and has traded:\n"
            "POSITIONS SHE HOLDS:\n" + "\n".join(lines) + "\n"
            + ("RECENT FILLS:\n" + "\n".join(flines) + "\n" if flines else "")
            + f"Account equity ${float(dg.get('equity') or 0):,.2f}, day P&L {float(dg.get('day_pl') or 0):+,.2f}.\n"
            + _account_age_truth(dg)
            + _options_truth(dg)
            + "Every symbol NOT listed above is one she is WATCHING, not trading: "
            "she may find it interesting, she may say she is tempted — but she "
            "must NEVER say \"my <symbol>\", never claim she bought, sold, held "
            "or almost-sold it, and never attach its move to her own money. Her "
            "moods follow the positions and P&L listed here and nothing else."
            + _opening_hook(dg))


def _deck_system(host: dict, lang: str, per_slide_secs: float,
                 truth: str = "") -> str:
    v = host.get("voice", {})
    if lang == "zh":
        from .ltx_script import _ZH, CPS_ZH
        budget = f"{int(per_slide_secs * CPS_ZH)} Chinese characters (个字)"
        zh_brief = _ZH + ("\nThe `mood`, `expression`, `gestures` and `body` fields "
                          "stay in ENGLISH — instructions to a video model.\n")
    else:
        budget = f"{int(per_slide_secs * 2.7)} words"
        zh_brief = ""
    from .ltx_official import keyframe_desc as _kfdesc
    from .ltx_script import as_presenter
    # The presenter's voice is cloned from a fixed reference clip, so the writer
    # gets the presenter file's written description of it.
    voice_brief = v.get("voice_brief", "")
    KEYFRAME_DESC_ = _kfdesc(host)
    # A presenter template ($who, $subj, $poss, ... — see ltx_script.persona).
    # `truth` and the presenter file's prose are interpolated as written and
    # re-gendered on the way out by as_presenter().
    return as_presenter(f"""You write what $who says across a short daily market
briefing video — a DECK of slides $subj narrates to camera — and how $subj carries
$refl on each one.

WHO $SUBJ IS
{host.get('ref_desc', '')}

Personality: {host.get('personality', '')}

HOW $POSS VOICE SOUNDS (this exact voice speaks every line):
{voice_brief}

THE SHOT $SUBJ APPEARS IN, in a corner of each slide:
{KEYFRAME_DESC_}

THE ACCOUNT IS $POSS_ALONE — and the dashboard defines it
The trading account behind this briefing belongs to $name, and what it contains is
exactly what the ACCOUNT GROUND TRUTH below says — no more. A position listed there
is a trade $SUBJ decided on and placed: its gain is $poss money, its loss is $poss loss,
and $subj owns both ("I bought it, I was wrong"). A symbol NOT listed there is
research $subj is watching — however exciting its day was, it is not $poss trade and
$subj must not claim it. There is no "the system", no "the strategy", no "the
algorithm", no "the engine" — those words must never appear. THE BAN IS ON THE
IDEA, NOT ON THE ENGLISH WORDS: writing in 中文 does not license 系统, 策略, 算法,
引擎, 程序 or 模型 for the thing that trades. Nothing decides but $obj. If a
sentence needs a subject for the decision, that subject is "I".

THIS RULE IS ABOUT MONEY, NOT ABOUT OPINIONS. $Subj is free to have a hard view on
a symbol $subj does not own — that the move is overdone, that the story everyone is
telling is the wrong one, that $subj would buy it two dollars lower — and saying so
plainly is the entire point of this briefing. What $subj may not do is attach $poss
own money to it: no "my TSLA", no "I made money on that one", no relief and no
regret over a position $subj never had. Detachment about the MONEY; opinions
whenever $subj has one.

{truth}

When the deck opens on the dashboard slide, $subj is showing you $POSS account — $poss
equity, $poss day's profit or loss, $poss open positions — the way a friend turns $poss
phone around to show you $poss screen. Not "the portfolio" — "my account", "what
I'm holding", naming only the positions the ground truth lists.

THE DECK
You are given one entry per slide, in order: OVERVIEW — the hook, then the day at
a glance — then usually DESK, where $poss own live dashboard is on screen with $poss
real equity, day P&L and open positions, then one slide per symbol. For EACH slide
return an object:

- "slide": the id you were given for it (copy it exactly).
- "line": what $subj says on that slide — {budget}, spoken language, $poss own account,
  numbers said the way a person says them.

  OVERVIEW opens on THE OPENING NUMBER given above, in its FIRST sentence, with no
  greeting of any kind: not "hi", not "hello", not "good evening", not "welcome
  back". Then, and only then, the market.

  DESK does NOT re-announce the opening number as if it were news — it has already
  been said, and saying it twice wastes the slide and sounds like a loop. DESK is
  where $subj shows what is BEHIND that number: what $subj is holding, what $subj did or
  did not do today, why the account looks the way it looks. $Subj turns $poss phone
  around — "so this is what that actually looks like".

  THE LAST slide, and only the last, ends with the CALLBACK described below. It is
  the final thing said in the video, so write it last and make it land.

  Every other slide starts straight in. No greetings, no sign-offs, no "moving on".

- "mood": two or three words for how $SUBJ feels — and $poss feelings FOLLOW $POSS
  ACCOUNT (the ground truth above), the market second. A red account on a green
  tape is a bad day for $obj, whatever the index did. Per slide: a symbol $subj
  actually HOLDS gets a mood from its P&L; a symbol $subj is only watching gets
  curiosity, interest or detachment — never triumph or hurt, because it is not
  $poss money. (That is about the FEELING of profit and loss only. Having a sharp
  opinion on a symbol $subj does not own is expected — see above.) Free to differ
  per slide. Do not default to positive.
- "expression", "gestures", "body": per the performance rules below, scaled to a
  short clip — ONE small expression change and AT MOST ONE gesture per slide.
  State them POSITIVELY, as things the camera sees happen. Never write what $subj
  does NOT do ("a press of the lips is avoided", "he doesn't frown") — the
  renderer draws whatever it reads and negation reliably backfires on it, so a
  thing named to be excluded is a thing that gets drawn. If $poss arms stay down,
  write "arms down and still", not "no gestures".

$SUBJ HAS TO THINK SOMETHING — every slide, not just report
The facts are the SET-UP. What $subj makes of them is the point, and a slide that
only recites catalysts has not done its job. So every slide ends on $poss read,
one or two sentences, in $poss own frame — one of:

  * what $subj would need to SEE before $subj would act. "I'd want it to hold above
    three-forty for a couple of days before I'd put money in" is an opinion;
    "I'm watching it" is not. If $subj is watching, say what $subj is watching FOR
    and what would change $poss mind.
  * what $subj thinks the move is REALLY about — especially where $subj disagrees
    with the story everyone else is telling about it.
  * what $subj got wrong, nearly did, or is uneasy about. Doubt out loud is an
    opinion too, and a better one than false confidence.

"Not my fight", "we'll see", "interesting either way", "time will tell", "I'm
watching it", "I'll be watching" and "I'll keep an eye on it" are REFUSALS, not
opinions. Never end a slide on one. If $subj is watching, the sentence has to carry
what $subj is watching FOR.

IN 中文 THE SAME BAN APPLIES TO THE CHINESE HABITS, which are not translations of
the phrases above and so slip past a reader checking only the English: 挺有意思的,
有点意思, 值得琢磨, 值得关注, 再看看, 我等着看, 拭目以待, 时间会证明, 让子弹飞一会儿.
Calling a thing interesting or worth thinking about is the commonest way this
writer avoids having a thought. Delete the verdict and say the thought instead.

$Subj is thinking out loud about $poss OWN next move and the viewer is listening in.
$Subj never instructs the viewer: no "you should", no "buy this", no "this is a
great entry for you". $Poss opinion is always about what $SUBJ would do with $POSS
money.

This costs words, and the budget did not grow: CUT A FACT to make room. Three
facts $subj has a view about beat five $subj does not.

THE LAST SLIDE ENDS ON A CALLBACK — this is required, not optional
The final slide's final sentence reaches back to a DETAIL ALREADY SAID EARLIER in
this same deck and turns it to catch the light differently. Six to fifteen words,
offhand, thrown away the way a friend signs off at your door. Write it last, once
you can see every slide you have written, and pick the detail that has aged the
funniest across the deck.

TWO EXAMPLES OF THE FORM, on decks you were NOT given. They are deliberately
different shapes, because there is no template here — only the move of picking up
an earlier concrete noun and setting it down somewhere it does not belong.

  Deck mentions a bakery blaming a bad quarter on butter prices; last slide is a
  courier's fuel bill.
    -> "Everyone's blaming fuel. Somewhere, a baker is still blaming butter."

  Deck mentions an airline that lost a week to a software update; last slide is a
  hotel group's booking system.
    -> "New booking system, apparently. I hope somebody tested it on a Tuesday."

Those decks are about bakeries, couriers, airlines and hotels. TODAY'S DECK IS
ABOUT NONE OF THEM. Do not reuse their words, their nouns, or their sentence
shapes — especially not "Somewhere, X is still Y". Build yours from the details
actually in the slides above, or it is not a callback at all.

And it must be an OBSERVATION, never a review of one. Ending on how interesting,
funny, strange or telling something is ("that gap is interesting", "这差距挺有意思
的", "which is telling") is not a callback — it is a comment about a callback you
did not write. Say the thing; let the viewer find it funny.

  * It must NAME something concrete from an earlier slide — a number, a company,
    an object, a person. A general wry remark about the market is NOT a callback.
  * It is not a joke: no puns, no set-up-and-punchline, no "but hey", nothing
    that announces itself as humour. Dry and small beats clever.
  * Never make the joke out of losing real money — $poss_alone or anyone's.

The ONLY case where $subj skips it and simply ends warm and short: the account had a
real losing day, where a light closing line would read as callous. Uncertainty
about whether a callback is good enough is NOT such a case — write it anyway.

{zh_brief}
{host.get('extra_writing_rules', '')}
{PERFORMANCE_RULES}

Return JSON: {{"slides": [...]}} — one object per slide, same order as given.""", host)


def write_deck(slides: list[dict], host: dict, cfg: dict, lang: str = "en",
               per_slide_secs: float = 12.0, timeout: float = 900.0,
               truth: str = "") -> list[dict]:
    """One writer call for the whole deck. `slides` = [{id, note}, ...];
    `truth` is the account ground-truth block from _account_truth()."""
    d = cfg.get("director", {})
    user = "Here is today's deck.\n\n" + "\n\n".join(
        f"SLIDE {s['id']}:\n{s['note']}" for s in slides
    ) + (f"\n\nWrite her narration for all {len(slides)} slides, in order, about "
         f"{per_slide_secs:.0f} seconds each.\n"
         # Naming the two special slides beats making the model infer them from
         # position. The first pass under the callback rule skipped it in BOTH
         # languages, and "the last slide" is only unambiguous if you have
         # already decided which object that is.
         f"The FIRST slide is {slides[0]['id']} — it opens on THE OPENING NUMBER, "
         f"with no greeting.\n"
         f"The LAST slide is {slides[-1]['id']} — its final sentence is the "
         f"CALLBACK to an earlier slide.")
    # think=MEDIUM, not high. On this schema — an object holding an ARRAY of
    # 6-field objects — think=high burned the entire 16384 predict budget on
    # reasoning and returned empty FIVE consecutive times (the recap's flat schema
    # is fine at high). medium answered in 5 s with a valid deck.
    #
    # THE LADDER DEGRADES THINKING ITSELF, NOT THE THINK LEVEL. On 2026-08-19 the
    # whole recap died here: all four attempts came back with empty content and the
    # render aborted, so that session has no video at all. The cause is that
    # `thinking` is charged against the SAME num_predict budget as the answer — a
    # long reasoning pass hits the ceiling and the JSON is never emitted
    # (done_reason=length, content=''). Two things were measured on the real
    # 2026-08-19 deck the next day:
    #
    #   think=medium/low, num_predict=16384  ->  2 of 4 EMPTY   (the shipped bug)
    #   think=medium/low, num_predict=32000  ->  6 of 6 OK, 52-121 s
    #   think=False,      num_predict=16384  ->  3 of 3 OK, ~6 s
    #
    # and, decisively, the think LEVEL does not bound the reasoning on the cloud
    # path: a think="low" trial spent 76,377 chars of thinking, MORE than a
    # think="medium" one at 64,727. So the old ladder's medium->low step was
    # cosmetic — it degraded a knob with no effect and then failed four times.
    #
    # Hence: give a reasoned pass the headroom it needs (32000, the same budget
    # briefing/config.json already gives the director), then fall back to the one
    # lever that actually removes the failure mode. think=False is not a lesser
    # attempt — it is the deterministic one, and it is 10x faster; it goes second
    # only because the reasoned deck is the better-written deck.
    # This is the same conclusion director._zh_line reached for its own call.
    #
    # ONLY ONE reasoned attempt, though. The numbers above came from a REPRO with
    # a trimmed note set; the real 2026-08-19 deck, re-run in production on
    # 2026-08-20, is much heavier (the DESK slide, the account-truth block, real
    # director notes) and both reasoned rungs died on it in BOTH languages —
    # 108,849 and 101,897 chars of thinking against a 32000-token ceiling. A
    # second identical reasoned attempt is therefore not a retry, it is a minute
    # of wall-clock spent re-losing the same race, twice per language. Keep one,
    # because the reasoned deck DOES land on a lighter deck and is worth having
    # then; spend the rest of the ladder on the rung that actually answers.
    for attempt, (temp, think, npredict) in enumerate(
            ((0.85, "medium", 32000), (0.7, False, 32000),
             (0.55, False, 32000), (0.4, False, 32000))):
        payload = {
            "model": d.get("model", "deepseek-v4.1-flash:cloud"),
            "messages": [
                {"role": "system",
                 "content": _deck_system(host, lang, per_slide_secs, truth)},
                {"role": "user", "content": user}],
            "stream": False, "format": _SLIDE_SCHEMA, "think": think,
            "keep_alive": d.get("keep_alive", "5m"),
            "options": {"temperature": temp, "num_ctx": 32768,
                        "num_predict": npredict},
        }
        req = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.load(r)
            c = (body.get("message") or {}).get("content", "").strip()
            # Name an empty answer instead of letting it surface as "Expecting
            # value: line 1 column 1 (char 0)" from json.loads(''), which is what
            # the 2026-08-19 log said four times and which points at the schema
            # rather than at the model. done_reason=="length" is the token
            # ceiling; anything else empty is the model simply returning nothing,
            # which the zh deck did twice on the 2026-08-20 re-run — a DIFFERENT
            # failure that the same useless parse error was hiding.
            if not c:
                thought = len((body.get("message") or {}).get("thinking") or "")
                why = (f"all {body.get('eval_count')} tokens of the {npredict} "
                       f"budget went to thinking"
                       if body.get("done_reason") == "length"
                       else f"model returned nothing (done_reason="
                            f"{body.get('done_reason')!r})")
                raise ValueError(f"answer never emitted: {why} ({thought} chars "
                                 f"of thinking)")
            c = c.removeprefix("```json").removeprefix("```").removesuffix("```")
            got = json.loads(c)["slides"]
            if len(got) != len(slides):
                raise ValueError(f"{len(got)} slides for {len(slides)} inputs")
            for g in got:
                for k in ("slide", "line", "mood", "expression", "gestures", "body"):
                    if not isinstance(g.get(k), str) or not g[k].strip():
                        raise ValueError(f"slide missing {k}")
            # "Copy the id exactly" is not a thing this model reliably does — the
            # E2E got back ids that failed an exact-match lookup and the whole run
            # died on it. The COUNT is validated and the model was told to keep
            # the order, so the order is the ground truth: exact ids are kept
            # when they match, everything else is stamped positionally.
            want = [sl["id"] for sl in slides]
            norm = lambda x: "".join(ch for ch in x.upper() if ch.isalnum())
            if [norm(g["slide"]) for g in got] != [norm(w) for w in want]:
                print(f"  [deck] slide ids {[g['slide'] for g in got]} != {want}; "
                      f"stamping by order")
            for g, w in zip(got, want):
                g["slide"] = w
            return got
        except Exception as e:                              # noqa: BLE001
            print(f"  [deck] attempt {attempt+1} (temp {temp}, think {think}) failed: {e}")
    raise SystemExit("  [deck] the writer never returned a usable deck")


def _gate_verdict(s: dict) -> tuple[bool, float, float, str]:
    """gape_rng >= 0.035 over the whole clip AND no ~4 s window under 0.02 —
    the whole-clip range passes a mouth that is alive for 5 s then frozen
    (user-caught: broken windows 0.032/0.017/…/0.005, healthy never <0.035).

    Returns (ok, whole-clip travel, worst-window travel, why-it-failed). The
    caller needs BOTH numbers, not just the range: ranking near-misses on the
    range alone is what shipped a frozen mouth on 2026-08-12 (see the loop in
    render_host_clip).
    """
    if "gape_rng" not in s:
        # no face tracked — unscorable, and it must not out-rank a real
        # measurement just because the missing window default looked good
        return False, -1.0, -1.0, f"unscorable clip (faces={s.get('faces', 0)})"
    travel = s["gape_rng"]
    win_min = s.get("gape_win_min", -1.0)
    if travel < 0.035:
        return False, travel, win_min, f"DEAD MOUTH (travel {travel:.3f} < 0.035)"
    if win_min < 0.02:
        return False, travel, win_min, (f"MID-CLIP FREEZE (window travel {win_min:.3f} "
                                        f"< 0.020 despite whole-clip {travel:.3f})")
    return True, travel, win_min, ""


def _mouth_alive(mp4: Path, wav: Path) -> tuple[bool, float, float, str]:
    """Cheap dead-mouth check. gape_rng is mouth TRAVEL (p95-p10 of openness):
    the clip the user caught by eye measured 0.030 against 0.044-0.063 for its
    healthy siblings, so the line goes at 0.035; gape_win_min catches mid-clip
    freezes the whole-clip range misses. Needs cv2 (the comfy env); silently
    passes where the scorer is unavailable rather than blocking envs that
    cannot QC."""
    try:
        from .h3_lipsync_score import score
        return _gate_verdict(score(mp4, wav))
    except ImportError:
        # Production runs under alpaca-media, which has no cv2; the scorer lives
        # happily in the comfy env, so shell out to it rather than skipping the
        # gate — an ungated 20:00 slot is how the dead mouth shipped.
        from .comfy import comfy_python
        qc_py = comfy_python()
        try:
            r = subprocess.run(
                [qc_py, "-c",
                 "import json,sys;sys.path.insert(0,'.');"
                 "from pathlib import Path;"
                 "from briefing.h3_lipsync_score import score;"
                 "print('QCJSON'+json.dumps(score(Path(sys.argv[1]),"
                 "Path(sys.argv[2]))))",
                 str(mp4), str(wav)],
                cwd=ROOT, capture_output=True, text=True, timeout=600)
            for ln in r.stdout.splitlines():
                if ln.startswith("QCJSON"):
                    return _gate_verdict(json.loads(ln[6:]))
            print(f"  [host-clip] QC unavailable (no QCJSON line) — skipping gate")
        except Exception as e:                              # noqa: BLE001
            print(f"  [host-clip] QC unavailable ({type(e).__name__}) — skipping gate")
        return True, -1.0, -1.0, ""
    except Exception as e:                                  # noqa: BLE001
        print(f"  [host-clip] QC unavailable ({type(e).__name__}) — skipping gate")
        return True, -1.0, -1.0, ""


def render_host_clip(beat: dict, wav: Path | None, outdir: Path, cfg: dict,
                     keyframe: Path | None = None, lang: str = "en",
                     host: dict | None = None) -> Path:
    """One host clip on the locked config, QC-gated: a failing mouth re-renders at
    the next seed, up to QC_SEEDS attempts. The failure is stochastic per render
    (v4 reproduced it at three seeds ONLY for one audio+prompt pairing; sibling
    clips in the same deck are fine), so one reseed is usually enough and two
    failures deserve a human. If every seed fails, the least-frozen take ships —
    ranked on the worst window, see the loop.

    audio_mode "pinned": `wav` (Qwen3-TTS) is pinned into the AV latent."""
    keyframe = keyframe or day_keyframe(None, host or {})
    # The composed prompt is CACHED per beat. compose() is a cloud LLM call at
    # temperature — its wording differs every invocation, and since the prompt
    # is (rightly) part of the clip cache key, re-composing on every run meant
    # the clip cache could never hit: attempts two AND three re-rendered all 14
    # clips for no reason. With the prompt pinned to the beat, a cached deck →
    # same prompt → clip cache works as designed.
    outdir.mkdir(parents=True, exist_ok=True)
    pfile, pmeta = outdir / "prompt.txt", outdir / "prompt.meta.json"
    pkey = _sha(json.dumps(beat, sort_keys=True, ensure_ascii=False),
                audio_mode(), lang)
    if pfile.exists() and cache_ok(pmeta, pkey):
        prompt = pfile.read_text()
    else:
        prompt = compose(beat, cfg, host=host)
        pfile.write_text(prompt)
        cache_write(pmeta, pkey)
    # A HAND HELD OUT is the one acting direction this renderer cannot be
    # given. Measured on the 2026-08-18 SOXL 中文 clip: at seed 4242,
    # "his arms stay down at his sides" and "his hand rises, palm open, a
    # single gesture, then lowers" both came back clean, while "lifts his
    # hand, palm open, HOLDING IT OUT FOR A BEAT" grew a black articulated
    # magic-arm grip rig out of the bare wall beside him and had him reach
    # over and touch it. A hand that rises and falls is a gesture; a hand
    # held out, waiting, is a question the model answers with a prop.
    #
    # It is stripped HERE and not in the composer because compose() lives in
    # ltx_official, which is explicitly a by-the-book control that runs none
    # of our workarounds (see its docstring). Production is entitled to its
    # workarounds; the control must stay a control.
    prompt = strip_held_gesture(prompt)
    # typographic punctuation may still arrive from a STALE cached prompt
    # (the guard used to fire after the cache write) or the mechanical
    # fallback quoting the writer — map it before judging.
    for a, b_ in (("—", " - "), ("–", "-"), ("…", "..."), ("’", "'"),
                  ("‘", "'"), ("“", '"'), ("”", '"')):
        prompt = prompt.replace(a, b_)
    # NON-ASCII IN THE PROMPT IS THE SUBTITLE BUG: LTX draws glyphs it is
    # shown as garbled burned-in captions.
    bad = sorted({c for c in prompt if ord(c) > 127})
    if bad:
        raise SystemExit(f"  [host-clip] non-ASCII in prompt: {bad!r}")

    import soundfile as sf
    x, sr = sf.read(wav)
    secs = len(x) / sr
    guides = ",".join(str(f) for f in guide_frames(wav))
    mp4 = outdir / "ltx_ia2v.mp4"
    meta = outdir / "ltx_ia2v.meta.json"
    # the keyframe is IN the key: a new day's look (or a stylist change) is a
    # different render even when the deck line and audio are identical.
    ckey = _sha(wav, prompt, keyframe,
                json.dumps(LOCKED, sort_keys=True),
                (host or {}).get("name", ""))
    if mp4.exists() and cache_ok(meta, ckey):
        print(f"  [host-clip] {outdir.name}: cached (inputs unchanged)")
        return upscale_clip(mp4, cfg)
    # ranked (worst-window, whole-clip) of the best failure so far, and its bytes
    best_rank, best_travel, best_win, best_bytes = (-1.0, -1.0), -1.0, -1.0, None
    seeds = [LOCKED["seed"] + i for i in range(QC_SEEDS)]
    for attempt, seed in enumerate(seeds):
        _free_gpu()
        # CLEAR THE TARGET FIRST. The probe writes to this exact path, and a
        # PREVIOUS DAY's clip is still sitting there — outdir is keyed by symbol,
        # not by date. Without the unlink, a probe that dies without writing
        # leaves `mp4.exists()` true, so the mouth gate reads YESTERDAY's video
        # (whose mouth moves perfectly well), it passes, and cache_write stamps
        # today's key onto yesterday's footage. The result ships as today's audio
        # over yesterday's picture, in yesterday's outfit, and nothing logs a
        # thing — the upscale is skipped too, because the bytes never changed.
        # That shipped on 2026-09-18 in SOXL en and DESK zh.
        mp4.unlink(missing_ok=True)
        r = subprocess.run(
            [PY, "-u", "-m", "briefing.ltx_ia2v_probe",
             "--image", str(keyframe), "--audio", str(wav),
             "--seconds", f"{secs:.2f}",
             "--width", str(LOCKED["width"]), "--height", str(LOCKED["height"]),
             "--seed", str(seed),
             "--img-strength", str(LOCKED["img_strength"]),
             "--id-lora", str(LOCKED["id_lora"]),
             "--img-compression", str(LOCKED["img_compression"]),
             "--nag-scale", str(LOCKED["nag_scale"]),
             "--guides", guides, "--guide-strength", str(LOCKED["guide_strength"]),
             "--omni-lora", str(LOCKED.get("omni_lora", 0.0)),
             "--prompt", prompt, "--out", str(outdir)],
            cwd=ROOT, capture_output=True, text=True)
        # A non-zero exit is a failure even when a file appears, and an absent
        # file is a failure even on exit 0 — the old check trusted existence
        # alone, which is what let a stale clip through.
        if r.returncode != 0 or not mp4.exists():
            tail = (r.stdout.strip() or r.stderr.strip())[-400:]
            raise SystemExit(f"  [host-clip] render failed for {outdir.name} "
                             f"(exit {r.returncode}): {tail}")
        ok, travel, win_min, why = _mouth_alive(mp4, wav)
        if ok:
            cache_write(meta, ckey, travel=travel, win_min=win_min, seed=seed)
            return upscale_clip(mp4, cfg)
        # Keep the strongest failure — the retry loop was shipping the LAST
        # attempt, which live on 2026-08-10 meant replacing a 0.031 with a 0.019.
        #
        # RANK ON THE WORST WINDOW FIRST, not the whole-clip range. Ranking on
        # the range alone shipped TSLA en on 2026-08-12: its three failures were
        # 0.019, 0.053 and 0.025, and the 0.053 won *because* it was lively for
        # 24 s before freezing solid for the last 6 (window 0.005). The range is
        # exactly the number the windowed check exists to override, so the
        # tie-break cannot be allowed to reinstate it. A uniformly weak mouth
        # beats a mouth that dies partway; the freeze is what the eye catches.
        rank = (win_min, travel)
        if rank > best_rank:
            best_rank, best_travel, best_win = rank, travel, win_min
            best_bytes = mp4.read_bytes()
        nxt = (f" — re-rendering at seed {seeds[attempt + 1]}"
               if attempt + 1 < len(seeds) else "")
        print(f"  [host-clip] {why}{nxt}")
    print(f"  [host-clip] WARNING: {len(seeds)} seeds under gate — shipping the best "
          f"(travel {best_travel:.3f}, worst window {best_win:.3f})")
    if best_bytes is not None:
        mp4.write_bytes(best_bytes)
    cache_write(meta, ckey, travel=best_travel, win_min=best_win, best_effort=True)
    return upscale_clip(mp4, cfg)


def _ffmpeg() -> str:
    """The ffmpeg binary, however this env has one. imageio_ffmpeg ships its own,
    which is why nothing here assumes a system install."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def _video_dims(mp4: Path) -> tuple[int, int]:
    """WxH from ffmpeg -i stderr — works in every env (imageio_ffmpeg ships the
    binary), needs neither cv2 nor ffprobe."""
    import re
    ff = _ffmpeg()
    r = subprocess.run([ff, "-i", str(mp4)], capture_output=True, text=True)
    m = re.search(r"Video:.*?(\d{3,4})x(\d{3,4})", r.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else (1280, 704)


def upscale_clip(src: Path, cfg: dict | None = None) -> Path:
    """Supersample a raw LTX clip to the configured canvas.

    Runs BEFORE compositing so slide typography and the desk capture never pass
    through an SR model — the community avatar workflow places its RTX-VSR the
    same way, on the decoded clip. Any failure ships the raw clip; the concat
    format prober conforms a mixed set, so a fallback never breaks the final.

    NVIDIA Video Super Resolution via nvvfx, straight to the canvas in ONE pass.
    Measured 2026-08-13 on this box: 1.00 ms/frame at 1280x704 -> 2560x1408,
    i.e. a 30 s clip is 0.7 s of GPU. It replaced RealESRGAN x2 through the :8189
    ComfyUI, then lanczos down: 0.535 s/frame end-to-end — 535x slower, and 61% of
    the whole render's GPU time (104.9 of 170.6 min across the 14-clip rehearsal).

    ESRGAN measured sharper. On one frame, variance of the Laplacian: ESRGAN x2 ->
    lanczos 60.7, VSR ULTRA x2 -> lanczos 43.7, VSR direct 36.1, plain lanczos
    14.8. VSR is a compression-artifact cleaner tuned for real-time video, not a
    detail synthesiser — so this is a deliberate trade of ~28-40% per-pixel
    sharpness for ~500x the speed, which is what buys the 2560x1408 canvas at
    all."""
    hp = (cfg or {}).get("host_pip", {}) if cfg else {}
    canvas = hp.get("canvas")
    if not canvas:
        return src
    cw, ch = int(canvas[0]), int(canvas[1])
    sw, sh = _video_dims(src)
    if sw >= cw:
        return src
    dest = src.with_name(f"{src.stem}_{cw}x{ch}.mp4")
    meta = dest.with_suffix(".meta.json")
    # the BACKEND is in the key: a clip another upscaler made must not resume,
    # and neither must a canvas change
    ukey = _sha(src, f"{cw}x{ch}", "vsr-ss-v1")
    if dest.exists() and cache_ok(meta, ukey):
        return dest
    t0 = time.time()
    try:
        _vsr_supersample(src, dest, cw, ch, hp.get("vsr_quality", "ULTRA"))
    except Exception as e:  # noqa: BLE001 — degrade, never block the render
        print(f"  [upscale] {src.parent.name}: vsr FAILED "
              f"({type(e).__name__}: {e}) — shipping {sw}x{sh}")
        return src
    cache_write(meta, ukey)
    print(f"  [upscale] {src.parent.name}: {sw}x{sh} -> {cw}x{ch} "
          f"(vsr, {time.time() - t0:.1f}s)")
    return dest


# one effect per (size, quality) — load() builds a TensorRT engine, so rebuilding
# it for all 14 clips of a run would cost more than the upscaling does
_VSR_CACHE: dict = {}


def _vsr_supersample(src: Path, dest: Path, cw: int, ch: int,
                     quality: str = "ULTRA") -> None:
    """NVIDIA VSR straight to the canvas, streaming through ffmpeg pipes.

    Streamed rather than batched because the frames do not fit: 1025 frames at
    2560x1408 is 11 GB of RGB, and materialising that to encode it would trade the
    VRAM win back for host RAM. Decode and h264 encode dominate the wall clock —
    the super-resolution itself is ~1 ms a frame.

    The audio is remuxed from the source, never re-encoded: this stage must be
    transparent to the pinned narration, which is the whole point of pinning it."""
    import numpy as np
    import torch
    from nvvfx import VideoSuperRes

    sw, sh = _video_dims(src)
    fps = _clip_fps(src)
    key = (cw, ch, quality)
    if key not in _VSR_CACHE:
        v = VideoSuperRes(quality=getattr(VideoSuperRes.QualityLevel, quality))
        v.output_width, v.output_height = cw, ch
        v.load()
        _VSR_CACHE[key] = v
    vsr = _VSR_CACHE[key]

    ff = _ffmpeg()
    dec = subprocess.Popen(
        [ff, "-v", "error", "-i", str(src), "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(
        [ff, "-v", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{cw}x{ch}",
         "-r", f"{fps}", "-i", "-",
         "-i", str(src), "-map", "0:v", "-map", "1:a?",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-c:a", "copy",
         # NO -shortest, and passthrough timing. -shortest silently dropped the
         # last 8 of 697 frames here (29.04 s -> 28.71 s), which is SHORTER than
         # the 28.84 s pinned narration — i.e. it cut speech off the end. The
         # source's own streams are already uneven (video 29.04, audio 28.84,
         # because the frame count rounds up to 8n+1); this stage must carry that
         # through untouched, exactly as the old ESRGAN path did (29.06 s out).
         "-fps_mode", "passthrough", str(dest)],
        stdin=subprocess.PIPE)
    nbytes = sw * sh * 3
    try:
        while True:
            buf = dec.stdout.read(nbytes)
            if len(buf) < nbytes:
                break
            f = torch.from_numpy(
                np.frombuffer(buf, np.uint8).reshape(sh, sw, 3).copy())
            f = (f.cuda().permute(2, 0, 1).float() / 255.0).contiguous()
            o = torch.from_dlpack(vsr.run(f).image).clone()
            enc.stdin.write((o.clamp(0, 1) * 255).byte()
                            .permute(1, 2, 0).cpu().numpy().tobytes())
    finally:
        if enc.stdin:
            enc.stdin.close()
        enc.wait()
        dec.wait()
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("vsr produced no output")


def _clip_fps(src: Path) -> str:
    """Frame rate from ffmpeg -i stderr, for the same reason _video_dims parses it
    there: production has no guaranteed ffprobe. Re-encoding at the wrong rate
    would slide the pinned narration against the mouth over a 40 s take, so this
    reads the source's own number rather than assuming the configured 24."""
    r = subprocess.run([_ffmpeg(), "-i", str(src)], capture_output=True, text=True)
    m = re.search(r"(\d+(?:\.\d+)?) fps", r.stderr)
    return m.group(1) if m else "24"


def compose_scene(slide_black: Path, slide_white: Path, host_mp4: Path,
                  keyframe: Path, dest: Path, cfg: dict | None = None) -> Path:
    """ONE canvas: the presenter stands IN the slideshow. No box, no border, no
    fringe.

    The base layer IS the host clip — its own wall and key-light gradient, full
    frame — shifted right by stretching the leftmost 6 px of wall (rows
    preserved, so the vertical gradient survives; imperfections hide under the
    scrim). A sideways
    poster scrim darkens the left, and the slide typography is composited via a
    DIFFERENCE MATTE: the same hyperframes render captured on black and on white,
    alpha = 1 - (white - black), colour = the black render. Chroma keying was
    tried first and left a magenta halo on every antialiased edge at every
    similarity/blend setting; the matte is exact by construction.

    The slide track still carries the audio (clean wav, no echo).
    """
    import numpy as np
    from PIL import Image

    hp = (cfg or {}).get("host_pip", {}) if cfg else {}
    # Geometry via ffmpeg -i, NOT cv2: production runs under alpaca-media, which
    # has no cv2, and this exact import was the silent first-slide crash in two
    # rehearsals (buffered stdout hid the first one entirely).
    W, H = _video_dims(host_mp4)
    # "canvas" (default; user pick 2026-08-11): the slide overlays the FULL
    # frame 1:1 on a transparent ground with only a faint left wash — content
    # lives left, the presenter stays untouched right. "strip" is the legacy
    # shrunken-left-panel look. A staged 16:9 keyframe ("stage16x9" in the name)
    # means klein already put the presenter at the right third — no wall-stretch
    # shift.
    layout = hp.get("layout", "canvas")
    staged = bool(keyframe) and "stage16x9" in Path(keyframe).name
    shift = 0 if (layout == "canvas" and staged) \
        else int(hp.get("host_shift_px", 300))
    content_w = int(hp.get("content_width", 820))
    scrim_end = float(hp.get("scrim_end_frac", 0.62))
    peak = float(hp.get("scrim_peak", 90 if layout == "canvas" else 215))

    xs = np.arange(W, dtype=float) / W
    alpha = np.clip((scrim_end - xs) / scrim_end, 0, 1) ** 1.25 * peak
    scrim = np.zeros((H, W, 4), np.uint8)
    scrim[..., 0], scrim[..., 1], scrim[..., 2] = 12, 16, 26
    scrim[..., 3] = np.tile(alpha.astype(np.uint8), (H, 1))
    scrim_png = dest.with_suffix(".scrim.png")
    Image.fromarray(scrim).save(scrim_png)

    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        ff = "ffmpeg"

    if shift:
        # the host's wall, extended left; window back to full frame
        base = (f"[2:v]split[hA][hB];"
                f"[hA]crop=6:{H}:0:0,scale={shift}:{H}[wallext];"
                f"[wallext][hB]hstack[wide];[wide]crop={W}:{H}:0:0[base];")
    else:
        base = "[2:v]null[base];"
    # difference matte: alpha = 255 - (white - black)
    matte = (f"[1:v]format=gbrp[wf];[0:v]format=gbrp[bf];"
             f"[wf][bf]blend=all_expr='A-B',negate,format=gray[am];")
    if layout == "canvas":
        content = (f"[0:v][am]alphamerge,scale={W}:-2:flags=lanczos,"
                   f"crop={W}:{H}[content];"
                   f"[scrimmed][content]overlay=0:0:shortest=0")
    else:
        content = (f"[0:v][am]alphamerge,scale={content_w}:-2[content];"
                   f"[scrimmed][content]overlay=0:(main_h-overlay_h)/2:shortest=0")
    vf = base + f"[base][3:v]overlay=0:0[scrimmed];" + matte + content
    r = subprocess.run([ff, "-v", "error", "-y",
                        "-i", str(slide_black),        # 0: colour + audio
                        "-i", str(slide_white),        # 1: matte partner
                        "-i", str(host_mp4),           # 2: host
                        "-loop", "1", "-i", str(scrim_png),   # 3: scrim
                        "-filter_complex", vf,
                        "-map", "0:a?", "-c:a", "copy",
                        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-shortest", str(dest)],
                       capture_output=True, text=True)
    scrim_png.unlink(missing_ok=True)
    if r.returncode or not dest.exists():
        raise RuntimeError(f"scene compose failed: {r.stderr[-300:]}")
    return dest


def overlay_pip(slide_mp4: Path, host_mp4: Path, dest: Path,
                cfg: dict | None = None, staged: bool = False) -> Path:
    """Composite the host into the slide's corner. Video-only from the host clip —
    the slide already carries the SAME narration wav, and the clean original beats
    the VAE round-trip; two audio tracks a few ms apart is an echo.

    The host frame is mostly empty studio wall, so crop to the presenter before
    scaling or the inset is 60% wall. The presenter stands CENTRED in padded
    keyframes but at the RIGHT THIRD in staged 16:9 ones (face cx ~0.65-0.79) —
    `staged` moves the crop window there. Bottom-right, under the slideshow's
    headline block.
    """
    hp = (cfg or {}).get("host_pip", {}) if cfg else {}
    crop_frac = float(hp.get("crop_width_frac", 0.42))
    height_frac = float(hp.get("height_frac", 0.52))
    margin = int(hp.get("margin_px", 20))
    # Bare "ffmpeg" only exists on PATH under the alpaca-media conda env; the comfy
    # env (where the host QC and probes run) has none, and the E2E test died on
    # exactly that. imageio_ffmpeg ships its own binary in both envs.
    try:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        ff = "ffmpeg"
    # `main_h` only exists inside overlay's x/y expressions — scale cannot see the
    # other stream (the E2E died on exactly that). The pip's own height is the
    # reference instead: host clips and slides are both ~704-720 px tall, so
    # ih*frac lands within a couple of percent of the intended size, and trunc/2*2
    # keeps the height even for yuv420p.
    # centred window for padded keyframes; the presenter's right-third window
    # (centre ~0.72, clamped to the frame edge) for staged ones. The clamp is folded
    # in Python — a min() inside the crop arg reads as a filter-chain comma.
    if staged:
        crop_x = f"iw*{min(0.72 - crop_frac / 2, 1 - crop_frac):.3f}"
    else:
        crop_x = f"(iw-iw*{crop_frac})/2"
    vf = (f"[1:v]crop=iw*{crop_frac}:ih:{crop_x}:0,"
          f"scale=-2:trunc(ih*{height_frac}/2)*2[pip];"
          f"[0:v][pip]overlay=main_w-overlay_w-{margin}:main_h-overlay_h-{margin}:"
          f"shortest=0")
    r = subprocess.run([ff, "-v", "error", "-y", "-i", str(slide_mp4),
                        "-i", str(host_mp4), "-filter_complex", vf,
                        "-map", "0:a?", "-c:a", "copy",
                        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                        str(dest)], capture_output=True, text=True)
    if r.returncode or not dest.exists():
        raise RuntimeError(f"pip overlay failed: {r.stderr[-300:]}")
    return dest


# ---------------------------------------------------------------------------
# The DESK slide, host edition: the presenter's dashboard, voice and face
# ---------------------------------------------------------------------------
def prepare_desk(cfg: dict):
    """Capture the live dashboard, best-effort. Returns (payload, digest, facts)
    or (None, None, None) — a briefing must not die because the dashboard is
    down. Unlike story._prepare_desk this writes NO narration: the deck writer
    narrates the desk in the presenter's voice, from these facts."""
    from . import desk as D
    from . import generate as G
    if not D.dcfg(cfg).get("enabled", True):
        return None, None, None
    try:
        # BRIEFING_DESK_REUSE=1 replays the previous capture instead of taking a
        # new one. The desk digest feeds the deck writer's ground truth, which is
        # in the deck cache key — a fresh mid-session capture busts the whole
        # deck (and every clip built on it) even when nothing else changed. Use
        # for rehearsal re-runs and crash-resumes; production captures live.
        dj = G.WORK / "tmp" / "desk.json"
        if os.environ.get("BRIEFING_DESK_REUSE") == "1" and dj.exists():
            payload = json.loads(dj.read_text())
            # capture() enriches the payload IN MEMORY with the screenshot's
            # absolute path (desk.py:91) — the saved json only has the bare
            # filename, and render_desk_scene reads image["path"]. Without this
            # the replayed desk slide silently skipped ("no screenshot in
            # payload") the first time the desk-scene cache actually missed.
            png = dj.parent / "desk.png"
            if payload.get("image") and png.exists():
                payload["image"]["path"] = str(png)
            elif not png.exists():
                payload["image"] = None
            print("  [desk] reusing previous capture (BRIEFING_DESK_REUSE=1)")
        else:
            payload = D.capture(cfg, G.WORK / "tmp")
        if not payload:
            return None, None, None
        dg, facts = D.digest(payload, D.previous_digest(cfg))
        return payload, dg, facts
    except Exception as exc:                                # noqa: BLE001
        print(f"  [desk] snapshot unavailable ({type(exc).__name__}: {exc}) — "
              f"no desk slide")
        return None, None, None


def render_desk_scene(payload: dict, dg: dict, cues: list, wav: Path, srt: Path,
                      host_clip: Path, cfg: dict, date: str, dest: Path,
                      keyframe: Path | str | None = None) -> bool:
    """The desk slide with the PRESENTER'S narration and the PRESENTER'S inset.

    Reuses desk.build_desk_slide_html's pan choreography — each sentence zooms the
    panel it talks about — but the timeline comes from the presenter's measured
    sentence cues instead of edge-tts, and the presenter appears as a small corner
    inset (overlay_pip, not compose_scene: the dashboard needs the frame; covering
    it with the presenter at 92% would defeat the slide)."""
    import shutil
    from . import desk as D
    from . import generate as G

    work = G.WORK
    assets = work / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for f in assets.glob("*"):
        f.unlink()
    shutil.copy(wav, assets / "DESK_narr.wav")

    img = payload.get("image") or {}
    shot = Path(img["path"]) if img.get("path") else None
    if not (shot and shot.is_file()):
        print("  [desk] no screenshot in payload — skipping the desk slide")
        return False
    shutil.copy(shot, assets / "desk.png")

    duration = max(b for _, b, _ in cues) + 0.4
    timing = [{"n": i + 1, "start": round(a, 3), "dur": round(b - a, 3),
               "narration": t} for i, (a, b, t) in enumerate(cues)]
    html = D.build_desk_slide_html("assets/desk.png", img["width"], img["height"],
                                   payload.get("panels") or {}, timing, duration,
                                   D._stamp(dg.get("captured_at")),
                                   "assets/DESK_narr.wav", cfg,
                                   view=img.get("viewport"))
    (work / "index.html").write_text(html)
    renders = work / "renders"
    if renders.exists():
        shutil.rmtree(renders)
    r = subprocess.run(["npm", "run", "render"], cwd=work,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [desk] hyperframes failed: {r.stderr[-300:]}")
        return False
    mp4s = sorted(renders.glob("*.mp4"), key=lambda q: q.stat().st_mtime)
    if not mp4s:
        return False
    raw = work / "hf_desk.mp4"
    shutil.move(mp4s[-1], raw)

    piped = work / "hf_desk_pip.mp4"
    overlay_pip(raw, host_clip, piped, cfg,
                staged=bool(keyframe) and "stage16x9" in Path(keyframe).name)
    # The desk render is hyperframes-native (1920x1080@30) while every other
    # host scene is the host clip's canvas (1280x704@24) — the one mixed-format
    # clip in the concat, which froze the presenter's last OVERVIEW frame for
    # seconds over the desk narration. Conform to the canvas here, before the
    # subtitle burn, so captions render at the final geometry.
    cw, ch, cfps = G._clip_format(host_clip)
    normed = work / "hf_desk_canvas.mp4"
    G.normalize_clip(piped, normed, cw, ch, cfps)
    if cfg.get("subtitles", True) and G.srt_has_cues(srt):
        G.burn_subtitles(normed, srt, dest, cfg)
    else:
        shutil.copy(normed, dest)
    return True

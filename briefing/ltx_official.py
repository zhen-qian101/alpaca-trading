#!/usr/bin/env python3
"""The host clip's composer, BY THE BOOK — official LTX guidance only.

Production uses three things from here: compose() (turns a deck beat's
performance into the renderer's prompt, called by host_briefing.render_host_clip),
PERFORMANCE_RULES (shared with host_briefing's deck writer) and keyframe_desc()
(what the presenter's keyframe shows, from the presenter file). It began as a
control, deliberately separate from ltx_story.py so the tuned pipeline is not
disturbed, and everything we discovered the hard way is still switched OFF here —
host_briefing applies its own workarounds (strip_held_gesture, the ASCII guard)
after compose():

  * no lower-body scrub — the writer's lower-body nouns, hand destinations and head
    movement all reach the model untouched. The official guides positively
    ENCOURAGE body language ("shoulders drop", "fingers tracing the rim").
  * no shot splitting, no post stabilisation, no mood-plate library.
  * no negation-avoidance rule of our own; only what the guides say.
  * the official ia2v graph as ComfyUI ships it: no ID LoRA (video_ltx2_3_ia2v.json
    does not contain one) and LTXVPreprocess at 18 (it does contain that).

The prompt follows the published structure. Lightricks' own README orders it: main
action in one sentence, then movements and gestures, then appearance, then
background, then camera, then lighting, then any changes -- one flowing paragraph,
present tense, under 200 words. The one documented override is for image-to-video,
where every guide says NOT to re-describe what the still already shows and to spend
the words on motion instead, so the appearance and background steps are dropped and
the keyframe carries them.
"""
from __future__ import annotations

import json
import urllib.request


# The presenter file's `why_the_name` is DELIBERATELY not passed to the writer.
#
# An entry that explains a name does not stop at the explanation; it tends to end
# on an instruction-shaped sentence ("that is the whole personality in two
# characters") sitting inside the character brief. The model reads that as the
# presenter's defining trait and lands on it as a closer, every time.
#
# Measured against this exact system prompt, five draws each way: WITH it, 3 of 4
# closed on the idea behind the name. WITHOUT it, 0 of 4 — they closed on what
# actually happened instead ("sometimes doing nothing is the trade", "It just sat
# there. Taunting me."). `personality` alone does not produce it, so the character
# survives the removal; only the formulaic sign-off goes.
# The performance rules, factored out so the DECK writer (host_briefing) shares
# them instead of paraphrasing them. Every line here was paid for with a broken
# render; paraphrase is how rules quietly lose their teeth. A presenter
# template: the $-slots are filled by whoever sends it (ltx_script.persona).
PERFORMANCE_RULES = """\
HOW $SUBJ MOVES — $SUBJ IS COMPOSED, NOT BUBBLY

$Poss warmth is already in $poss VOICE. It is in the audio, it is doing its job, and it
does not need to be acted out a second time with $poss body. On camera $subj is still
and unhurried: someone sure of what $subj is saying who does not have to sell it.
A person telling you something true across a table, not a presenter performing to a
room. Understated beats animated every time.

Write these as things that HAPPEN OVER THE SHOT, in order. Physical descriptions,
never mood labels: what a camera would see, not "$subj feels relieved".

- "expression": ONLY $POSS EYES, BROWS, CHEEKS AND CHIN. Never $poss mouth, lips,
  smile, grin or teeth.

  THIS IS LOAD-BEARING, not taste. Whatever you write about the face is applied
  across the WHOLE take, so a mouth shape becomes a mouth shape HELD for thirty
  seconds — and a held mouth physically cannot form words. Measured on this exact
  pipeline: a take written as "a warm smile" rendered with $poss lips shut at the
  loudest moments of $poss own speech. In this shot the mouth has one job, speaking.
  Leave it alone and let the eyes carry the feeling.

  TWO OR THREE small changes across the whole take. Not a reaction to every clause.

- "gestures": what $poss HANDS and ARMS do, starting from $poss sides.

  AT MOST TWO GESTURES IN THE WHOLE HALF-MINUTE, both small — a hand turning over,
  a hand settling. Between and after them $poss arms are down and still, and say so.
  A gesture per clause reads as frantic and cheapens what $subj is saying. Never count
  numbers on $poss fingers: the audio already says the number and it looks like a
  children's programme.

- "body": POSTURE and WEIGHT only, and barely. One weight shift in half a minute is
  plenty; a breath that lifts and drops $poss shoulders is plenty. $Subj stands still.
  Do not choreograph $obj.
"""


def _rg(host: dict | None, text: str) -> str:
    """`text` as sent for the presenter actually rendering: its $-slots filled
    from the presenter file, and the free text inside it (the writer's beats)
    re-gendered. See ltx_script.as_presenter."""
    from .ltx_script import as_presenter
    return as_presenter(text, host or {})


def keyframe_desc(host: dict | None = None) -> str:
    """What the still ACTUALLY shows, for whichever presenter is rendering — the
    presenter file's `keyframe_desc`. The writer is told this so its blocking
    suits the picture: asking for "settles back into the armchair" when there is
    no chair is how a performance stops matching its picture.

    There is deliberately no fallback description, and no hard-coded scene: a
    description that is not of this presenter's own plate is the error. The
    writer blocks a performance against a picture it was told about, so the
    description has to be the picture that will actually be rendered.
    """
    d = (host or {}).get("keyframe_desc")
    if not d:
        raise SystemExit(f"  [host] {(host or {}).get('name', 'this presenter')} "
                         f"has no keyframe_desc in its presenter file")
    return d


MAX_PROMPT_WORDS = 200

# A presenter template (ltx_script.persona has the slots). One slot reads oddly
# on purpose: the quoted failed prompt says "settling on $poss as $subj
# finishes", the possessive where the sense wants the object, because that is
# the exact text the renderer's prompts have always been composed from.
_COMPOSE = """You are writing the PROMPT for an LTX-2 video model. You are not
writing for a person to read; you are writing conditioning text for a renderer.

Follow the official LTX prompting guidance exactly:

- ONE flowing paragraph. Not bullets, not labels, not a list of timestamps.
- PRESENT TENSE throughout ("$subj lifts", never "$subj lifted").
- HARD LIMIT {maxw} WORDS. This is the published ceiling and it is not negotiable.
  Shorter is fine.
- Order: the main action first in a single sentence, then the movements and
  gestures, then the camera, then lighting and colour, then the sound.
- This is IMAGE-to-video and the first frame is already supplied. Do NOT describe
  $poss face, $poss hair, $poss clothes, the wall or the room — every word spent on what
  the picture already shows is a word not spent on motion. Describe only what
  CHANGES over time.
- Physical, observable descriptions. Never an emotion label like "$subj feels
  relieved" — write what a camera would see.
- SAY THAT $SUBJ IS SPEAKING, and say it first: $subj talks continuously to camera from
  the first frame to the last, $poss mouth forming every word. The renderer is driven
  by an audio track it must lip-sync to, and a prompt that never mentions speech
  invites it to animate a face doing something else.
- NEVER WRITE $POSS MOUTH, LIPS, SMILE, GRIN OR TEETH — not once, not even "a small
  smile". Anything you write about the mouth is applied across the entire shot, and
  a held mouth cannot articulate; it is the single most reliable way to destroy the
  lip-sync. Feeling goes in the eyes, brows and cheeks. This holds even if the
  breakdown you are given describes $poss mouth: drop those clauses.
- DO NOT MENTION THE CAMERA AT ALL. Not a shot size, not a lens, not a move, not
  "locked off", not "the framing holds". Say nothing about it whatsoever. The
  published guidance tells you to give a camera an END STATE, and that rule is for
  a shot that is SUPPOSED to move — applied here it produced "the camera slowly
  pushes in, settling on $poss as $subj finishes", and the renderer obeyed perfectly:
  $poss face doubled in size over the take. A camera you never mention is the only
  camera that stays still.
- DO NOT INVENT the setting or the sound. $Subj is alone against a plain studio wall
  in soft daylight. There is no trading floor, no office, no music, no crowd. Write
  only $poss own quiet voice and the room tone.
- Describe the sound as its own short clause at the end.

You will be given a long, beat-by-beat performance breakdown that is far over the
limit. Do NOT truncate it and do NOT drop the ending. COMPRESS it: keep the shape of
the whole performance from first moment to last, merge the small beats into flowing
clauses, and keep the two or three most characterful gestures rather than all
twelve. The quoted line fragments are anchors for you, not for the renderer — the
renderer cannot hear the audio, so turn them into a natural ORDER of actions
("first… then… as $subj finishes…") instead of quoting them.

KEEP AT MOST TWO HAND GESTURES. If the breakdown lists more, choose the two with the
most meaning and drop the rest — do not try to fit them all in. Between and after
them $subj is simply standing still and talking, and say so explicitly. A hand moving
on every clause looks frantic and cheapens what $subj is saying.

$SUBJ IS COMPOSED, NOT ANIMATED. The finished paragraph should read calm: someone
sure of what $subj is saying, not performing it. If your draft is a list of things
happening one after another, it is too busy — cut it back until there is stillness
between the moments that matter.

Return JSON: {{"prompt": "<the paragraph>"}}"""


def compose(beat: dict, cfg: dict, max_words: int = MAX_PROMPT_WORDS,
            timeout: float = 900.0, host: dict | None = None) -> str:
    """Let the WRITER compress its own direction to the official ceiling.

    The first by-the-book run assembled a 466-word prompt out of three beat-by-beat
    fields and so broke the guide's own <=200-word rule while claiming to follow it.
    Cutting it mechanically would just lose the ending, so the model that wrote the
    performance rewrites it instead, and the word count is CHECKED rather than
    trusted — on a miss it is told the number it actually produced and tries again.
    """
    d = cfg.get("director", {})
    brief = _rg(host, f"LINE $SUBJ SPEAKS (for your understanding of the order of events only; "
                      f"do not put these words in the prompt):\n{beat['line']}\n\n"
                      f"FACE:\n{beat['expression']}\n\nHANDS AND ARMS:\n{beat['gestures']}\n\n"
                      f"BODY, POSTURE AND WEIGHT:\n{beat.get('body', '')}")
    # The composer writes the RENDERER's prompt, so its own pronouns end up in
    # the conditioning text: the instructions are filled for the presenter too,
    # not just the brief.
    system = _rg(host, _COMPOSE.format(maxw=max_words))
    note = ""
    # think ladder, medium first: the empty-answer runaway (all 16384 predict
    # tokens spent reasoning) hit compose 4/4 on one slide of the E2E deck even on
    # this flat one-field schema. medium has never failed it; high has.
    for attempt, _think in enumerate(("medium", "medium", "low", "low")):
        payload = {
            "model": d.get("model", "deepseek-v4.1-flash:cloud"),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": brief + note}],
            "stream": False,
            "format": {"type": "object", "properties": {"prompt": {"type": "string"}},
                       "required": ["prompt"]},
            "think": _think,
            "keep_alive": d.get("keep_alive", "5m"),
            "options": {"temperature": 0.7 - 0.1 * attempt, "num_ctx": 32768,
                        "num_predict": 16384},
        }
        req = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.load(r)
            c = (body.get("message") or {}).get("content", "").strip()
            c = c.removeprefix("```json").removeprefix("```").removesuffix("```")
            text = json.loads(c)["prompt"].strip()
        except Exception as e:                              # noqa: BLE001
            print(f"  [compose] attempt {attempt+1} failed: {e}")
            continue
        # The cloud writer salts typographic punctuation into otherwise-English
        # prose (an em-dash killed a pinned-mode production run 2026-08-11 at
        # render_host_clip's ASCII guard) — the same mapping that guard applies.
        for a, b_ in (("—", " - "), ("–", "-"), ("…", "..."), ("’", "'"),
                      ("‘", "'"), ("“", '"'), ("”", '"')):
            text = text.replace(a, b_)
        n = len(text.split())
        print(f"  [compose] attempt {attempt+1}: {n} words")
        if n <= max_words:
            return text
        note = (f"\n\nYour previous attempt was {n} words, which is over the "
                f"{max_words}-word limit. Compress it further — merge more beats "
                f"into flowing clauses and keep fewer gestures — while still "
                f"covering the performance from the first moment to the last.")
    # Four model attempts are gone. Degrade loudly, never die: the 20:00 slot
    # publishes something composed from the writer's own words instead of nothing.
    # Filled like the composer's own instructions: its "$Subj talks… $poss
    # mouth…" skeleton is the renderer's conditioning text.
    out = _rg(host, _mechanical_compose(beat, max_words))
    print(f"  [compose] MODEL UNAVAILABLE — mechanical fallback, "
          f"{len(out.split())} words")
    return out




def _mechanical_compose(beat: dict, max_words: int = MAX_PROMPT_WORDS) -> str:
    """compose() without a model: the deterministic floor under the cloud.

    The empty-answer fault is per-request and has taken out four attempts in a
    row (twice), and a daily 20:00 video cannot die on a cloud mood. This builds
    the paragraph from the writer's own sentences in the same skeleton the model
    composer uses. To fit the 200-word ceiling it drops WHOLE trailing sentences,
    body first (posture minutiae), then gestures — never the speech clause, never
    the expression, so the mouth-drives-the-shot framing and the face always
    survive. Quoted fragments are stripped for the usual reason: text shown to
    LTX is text it may draw.

    The skeleton is a presenter template: compose() sends the result through
    _rg() to fill it.
    """
    import re as _re

    def sentences(t: str) -> list[str]:
        t = _re.sub(r"""\s*(?:at|on|during)?\s*['"\u201c\u201d\u2018\u2019][^'"\u201c\u201d\u2018\u2019]{1,60}['"\u201c\u201d\u2018\u2019]""", "", t)
        t = _re.sub(r"\s+,", ",", t)
        return [x.strip().rstrip(".") for x in _re.split(r"(?<=[.;])\s+", t.strip())
                if x.strip(" .;")]

    expr = sentences(beat.get("expression", ""))
    gest = sentences(beat.get("gestures", ""))
    body = sentences(beat.get("body", ""))
    head = ("$Subj talks continuously to camera from the first frame to the last, "
            "$poss mouth forming every word.")
    tail = ("Soft daylight falls on $obj, and $poss quiet voice carries the room tone.")

    def build(e: int, g: int, b: int) -> str:
        mid = expr[:e] + gest[:g] + body[:b]
        return " ".join([head] + [m + "." for m in mid] + [tail])

    e, g, b = len(expr), len(gest), len(body)
    out = build(e, g, b)
    while len(out.split()) > max_words and (b or g or e > 2):
        if b:
            b -= 1
        elif g:
            g -= 1
        else:
            e -= 1          # never below two expression sentences
        out = build(e, g, b)
    return out

#!/usr/bin/env python3
"""陈谷雨's stylist — his outfit chosen daily from the New York weather.

It runs every weekday at 09:35 ET (agent/run_stylist.sh, the cron entry) and
again from generate.py as a subprocess when the day's look is missing. It writes
the look into assets/host2/stylist/<date>.json, which host_briefing.day_keyframe()
reads by date — and only an APPROVED look (web/static/look.html) reaches the
video.

It was ported from an earlier stylist, and the design choices that differ
are deliberate:

  * HE HAS NO HAIR SLOT. His is a crew cut, and the bible records that
    klein kept re-lengthening it on its own across three passes until the fringe
    was removed as a variable entirely. It is an identity anchor now, not a
    styling slot, so the stylist never touches it.
  * NO JEWELLERY SLOT. His bible ends with
    "bare wrists - no watch, no bracelet, no necklace, no jewellery at all",
    arrived at by removing accessories one pass at a time. The stylist gets the
    garments and nothing else.
  * MINIMALISM IS THE BRIEF, not a suggestion (user directive 2026-08-18:
    "casual and in minimalism style"). That happens to align with klein's one
    reliable wardrobe failure: any logo, slogan or chest graphic re-rolls as
    invented glyphs on every seed — on the best plate of host2's round 3 it drew
    a legible but WRONG gold 「美」. Plain solid garments are both the style the
    user asked for and the only kind that reproduces.

Hard rules the stylist cannot break:
  * The GLASSES are untouchable — host2.json calls the thin round pale-silver
    wire-rims "the single most recognisable thing about him, and the stylist may
    never change them".
  * The crew cut stays, and nothing may cover it: no cap, no beanie, no hood UP.
    A hood down on the shoulders is fine and is why hoodies are allowed at all.
  * Every plate passes the 0.68 identity gate or the day falls back to the
    locked white-tee keyframe. An off-model host never ships because of an
    outfit.

CLI:
    python -m briefing.host2_stylist --date 2026-08-18
"""
from __future__ import annotations

import argparse
import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST_JSON = ROOT / "briefing" / "host2.json"
OUT = ROOT / "briefing" / "assets" / "host2"
WARDROBE_DIR = OUT / "wardrobe"
HISTORY_DIR = OUT / "stylist"
KEYFRAME_DIR = OUT / "keyframes"
GATE = 0.68
NYC = (40.71, -74.01)

# Open-Meteo WMO weather codes, folded to the words a stylist thinks in.
_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
        45: "foggy", 48: "foggy", 51: "drizzle", 53: "drizzle", 55: "drizzle",
        61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow",
        73: "snow", 75: "heavy snow", 80: "rain showers", 81: "rain showers",
        82: "heavy showers", 95: "thunderstorm", 96: "thunderstorm",
        99: "thunderstorm"}


def nyc_weather(date: str) -> dict | None:
    """Open-Meteo daily forecast for New York. None on any failure — the render
    must never block on a weather API."""
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={NYC[0]}&longitude={NYC[1]}"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
           "weather_code&timezone=America%2FNew_York"
           f"&start_date={date}&end_date={date}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.load(r)["daily"]
        month = int(date.split("-")[1])
        season = ("winter" if month in (12, 1, 2) else
                  "spring" if month in (3, 4, 5) else
                  "summer" if month in (6, 7, 8) else "autumn")
        return {"date": date, "tmax_c": d["temperature_2m_max"][0],
                "tmin_c": d["temperature_2m_min"][0],
                "precip_mm": d["precipitation_sum"][0],
                "sky": _WMO.get(d["weather_code"][0], "changeable"),
                "season": season}
    except Exception as exc:                                # noqa: BLE001
        print(f"  [stylist] weather unavailable ({type(exc).__name__}: {exc})")
        return None


_STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "outfit": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["slug", "outfit", "rationale"],
}

# Validation bans, each with a measured or documented reason.
#   footwear  -> measured: "ankle boots" pulled klein to a head-to-boots
#                full-body shot at face-scale 0.113.
#   headwear  -> covers the crew cut, which is an identity anchor.
#   graphics  -> klein re-rolls invented glyphs; the 「美」 lesson.
#   jewellery -> the bible ends on bare wrists.
_BANNED = {
    "footwear": ("shoe", "boot", "sneaker", "heel", "sandal", "sock",
                 "loafer", "footwear", "slipper", "trainer"),
    "headwear": ("hat", "cap", "beanie", "hood up", "headband", "sunglasses"),
    "graphics": ("logo", "slogan", "graphic print", "lettering", "wordmark",
                 "crest", "printed text", "monogram", "embroidery"),
    "jewellery": ("watch", "bracelet", "necklace", "earring",
                  "jewellery", "jewelry"),
}

# The check must be NEGATION-AWARE, and this is not a nicety — a plain substring
# test rejected 6 of 6 fabricated days on the first run. The example outfit the
# system prompt hands the model is host2.WARDROBE, which itself reads "completely
# plain with no logo, no graphic, no print, no embroidery, no lettering" and
# "bare wrists, no watch, no bracelet, no necklace and no jewellery of any kind".
# The model dutifully copies that register, so the well-behaved answer is exactly
# the one full of banned words — every one of them negated. What is actually
# banned is a garment that HAS the thing, so a hit only counts when no negator
# governs it.
_NEGATORS = {"no", "not", "without", "never", "free", "nor", "none", "bare",
             "sans", "minus", "lacking", "devoid", "neither"}


# Four tokens, and the width is load-bearing in BOTH directions. Six reached
# back across a comma and read "...with no logo, and a navy baseball cap" as
# negated (a real cap, missed). Three breaks the other way on shared negations
# like "no logo, graphic or lettering", where the negator governs a list. Four
# is the window that got every case in the fixture right; a false positive here
# only costs one rung of the retry ladder, a false negative puts a cap on him.
_NEG_WINDOW = 4


def _affirmative_hits(text: str, words) -> list[str]:
    """Banned words appearing WITHOUT a negation governing them."""
    import re
    low = text.lower()
    hits = []
    for w in words:
        start = 0
        while True:
            i = low.find(w, start)
            if i < 0:
                break
            start = i + len(w)
            pre = re.findall(r"[a-z]+", low[:i])[-_NEG_WINDOW:]
            if not (_NEGATORS & set(pre)):
                hits.append(w)
                break
    return hits


def _history(days: int = 7) -> list[dict]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(HISTORY_DIR.glob("*.json"))[-days:]:
        try:
            j = json.loads(f.read_text())
            out.append({"date": f.stem, "outfit": j["style"]["outfit"][:110]})
        except Exception:                                   # noqa: BLE001
            continue
    return out


def _system(host: dict) -> str:
    from . import host2 as H2
    return (
        "You are the personal stylist for 陈谷雨 (Rain Chen), the 25-year-old "
        "host of a nightly market-recap video. His personality, from his "
        f"writer's bible: \"{host.get('personality', '')}\". He presents "
        "standing, framed mid-thigh, in a bright studio against a warm "
        "light-grey wall.\n\n"
        "Design TODAY'S OUTFIT from the New York weather given by the user.\n\n"
        "THE HOUSE STYLE IS CASUAL MINIMALISM. Quality plain basics in a "
        "restrained, tonal palette: think a well-cut tee, a henley, a crewneck "
        "sweatshirt, a hoodie, a merino or lambswool sweater, an overshirt, a "
        "cardigan, a denim or harrington or coach jacket, a light unstructured "
        "blazer, over straight-leg jeans or chinos. One or two colours, clean "
        "lines, good fabric, nothing busy. Muted and natural — ecru, oatmeal, "
        "stone, charcoal, navy, olive, slate, warm grey, off-white, faded "
        "indigo, rust. He is easygoing and unhurried and his clothes should "
        "read that way: comfortable, unfussy, quietly well chosen.\n\n"
        "HARD RULES — every one of these is a validation failure, not a "
        "preference:\n"
        "* NEVER mention his face, his hair or his glasses. His crew cut and "
        "his thin round pale-silver wire-rim glasses are fixed identity and are "
        "drawn from a reference plate; naming them lets the drawing model "
        "re-roll them.\n"
        "* NOTHING ON HIS HEAD. No cap, no beanie, no hat, no headband, no "
        "sunglasses. If you choose a hoodie the hood is DOWN on his shoulders — "
        "say so explicitly.\n"
        "* NO footwear of any kind — no shoes, boots, sneakers, sandals or "
        "socks. He is photographed from mid-thigh UP; naming footwear makes the "
        "photographer pull back to a full-body shot that ruins the framing.\n"
        "* NO logo, wordmark, slogan, chest graphic, printed lettering, "
        "monogram or embroidery on ANY garment. The drawing model cannot render "
        "text and invents a different wrong glyph on every attempt. Garments "
        "are plain, or at most a simple stripe, marl or subtle texture.\n"
        "* NO watch, bracelet, necklace, ring or jewellery. His wrists are "
        "bare. Do not add a bag, a backpack or anything held in his hands.\n"
        "* Keep his hands and forearms visible and unobstructed where the "
        "garment allows — sleeves may be long, but not draped over his hands.\n\n"
        "* Write `outfit` as PROMPT TEXT for an image model, in the detailed "
        "physical register of this example —\n"
        f"  \"{H2.WARDROBE}\"\n"
        "  Name fabrics, colours, fit, length and how each garment sits, and "
        "name the trousers too. If the look replaces his usual white t-shirt, "
        "SAY he is not wearing a white t-shirt under it (the drawing model "
        "layers garments unless told otherwise). If a layer is open, say what "
        "is underneath.\n"
        "* It must be weather-appropriate and DIFFERENT from the recent looks "
        "the user lists.\n"
        "* `slug`: 2-4 lowercase words joined by dashes naming the look.\n"
        "* `rationale`: one sentence, in his voice, on why today.\n"
        "Answer ONLY the JSON object."
    )


def design(weather: dict, host: dict, cfg: dict, timeout: float = 300.0) -> dict:
    """One stylist call — deck-writer plumbing: deepseek cloud, think medium,
    ladder retries against the empty-answer flakiness."""
    d = cfg.get("director", {})
    hist = _history()
    user = (
        f"New York today ({weather['date']}, {weather['season']}): high "
        f"{weather['tmax_c']:.0f}C, low {weather['tmin_c']:.0f}C, "
        f"{weather['sky']}, precipitation {weather['precip_mm']:.1f}mm.\n\n"
        "Recent looks (do not repeat):\n"
        + ("\n".join(f"- {h['date']}: {h['outfit']}" for h in hist)
           if hist else "- none on record")
        + "\n\nDesign today's outfit.")
    for attempt, (temp, think) in enumerate(
            ((0.9, "medium"), (0.75, "medium"), (0.55, "low"), (0.4, "low"))):
        payload = {
            "model": d.get("model", "deepseek-v4.1-flash:cloud"),
            "messages": [{"role": "system", "content": _system(host)},
                         {"role": "user", "content": user}],
            "stream": False, "format": _STYLE_SCHEMA, "think": think,
            "keep_alive": d.get("keep_alive", "5m"),
            "options": {"temperature": temp, "num_ctx": 32768,
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
            got = json.loads(c)
            for k in ("slug", "outfit", "rationale"):
                if not isinstance(got.get(k), str) or not got[k].strip():
                    raise ValueError(f"missing {k}")
            for kind, words in _BANNED.items():
                hit = _affirmative_hits(got["outfit"], words)
                if hit:
                    raise ValueError(f"outfit puts {kind} on him ({hit[0]!r})")
            got["slug"] = "".join(ch if (ch.isalnum() or ch == "-") else "-"
                                  for ch in got["slug"].lower()).strip("-")[:40]
            return got
        except Exception as e:                              # noqa: BLE001
            print(f"  [stylist2] attempt {attempt+1} (temp {temp}, "
                  f"think {think}) failed: {e}")
    raise RuntimeError("stylist never returned a usable look")


# ==========================================================================
# DRAWING
# ==========================================================================
# Two stages, and the reason is the reference set. A 16:9 keyframe drawn
# straight from his two locked plates inherits their WHITE TEE — both plates
# wear it, and klein follows references over words (the same mechanism that
# made "closed-lip smile" produce a grin). So the outfit is established
# first in a portrait, and that portrait then becomes the wardrobe reference for
# the 16:9 frame. The portrait is also what the cover poster wants.
#
# BOTH prompts restate host2.HAIR in full, and that is deliberate even though the
# stylist never chooses hair. The first draft left it out — the stylist styles
# clothes, so why spend tokens on hair? — and the very first plate came back with
# the crew cut grown out, textured on top and carrying blond highlights at the
# crown (identity still 0.959, because the FACE was fine; the hair is not what
# the recogniser measures). host2.py records the same failure from the character
# sheet: klein "kept re-lengthening it on its own" until the fringe was removed
# as a variable. An unstated attribute is not a held attribute — it is a free one.

_IDENTITY_FACE = (
    "Redraw the exact same man as the reference image and keep his face "
    "strictly unchanged: the same youthful face, the same warm dark-brown eyes, "
    "the same thin round pale-silver wire-rim glasses, the same small nose and "
    "the same clean crew cut. Do not age him and do not alter his facial "
    "features, and do not change his hair"
)

# Backdrop is CHANNEL IDENTITY, not a stylist variable, and it is IMPORTED from
# host2.py rather than copied. The first draft copied it, which is how the two
# keyframe paths silently diverge: the staging text had to be rewritten for the
# supersample canvas, and a copy here would have left every styled day on the
# old wording — his outfit changing daily while his position in frame quietly
# did too. One definition, both callers.
def _staging() -> str:
    from . import host2 as H2
    return H2._STAGING


def _gates(img, e, pe, box, need_cx: bool):
    """Shared verdict for both stages. Bands are host2.py's, not re-derived."""
    from . import host2 as H2
    from . import host_identity_score as HID
    sim = HID._cos(e, pe)
    scale = float(box[3]) / img.shape[0]
    cx = (float(box[0]) + float(box[2]) / 2) / img.shape[1]
    bad = []
    if not (H2.MIN_FACE_SCALE <= scale <= H2.MAX_FACE_SCALE):
        bad.append("TOO TIGHT" if scale > H2.MAX_FACE_SCALE else "TOO WIDE")
    if need_cx and cx < H2.MIN_CX:
        bad.append("NOT RIGHT-THIRD")
    return sim, scale, cx, bad


def draw_plate(style: dict, slug: str, best_of: int = 12) -> tuple[Path, float] | None:
    """klein draws him in the designed look, as a mid-thigh portrait."""
    import cv2

    from . import comfy
    from . import host2 as H2
    from . import host_identity_score as HID

    cfg = H2._cfg()
    if not comfy.ensure_server(cfg):
        print("  [stylist2] klein server unreachable")
        return None
    plate = OUT / "host2_front.png"
    refs = [plate, OUT / "host2_three_quarter.png"]
    prompt = (f"{_IDENTITY_FACE}. {H2.STYLES['photoreal']}. {H2.FRAMING}, both "
              f"hands fully visible at his sides and his legs visible from "
              f"mid-thigh up. He has {H2.HAIR}. He is {style['outfit']}. "
              f"{H2.KEYFRAME_EXPRESSION} There is no text, no lettering and no "
              f"digits anywhere in the frame.")
    pe, _ = HID.embed(cv2.imread(str(plate)))
    WARDROBE_DIR.mkdir(parents=True, exist_ok=True)
    cand_dir = WARDROBE_DIR / ".cand"
    cand_dir.mkdir(exist_ok=True)
    best, best_sim = None, -1.0
    for i in range(best_of):
        seed = 7 + 104729 * i
        cand = cand_dir / f"stylist2_{slug}_s{seed}.png"
        if not cand.exists():
            if not comfy.klein_storyboard_multi(cfg, refs, prompt, cand,
                                                width=896, height=1216,
                                                seed=seed):
                continue
        img = cv2.imread(str(cand))
        e, box = HID.embed(img)
        if e is None:
            print(f"  [stylist2] plate seed {seed:>8} no face — rejected")
            continue
        sim, scale, _, bad = _gates(img, e, pe, box, need_cx=False)
        print(f"  [stylist2] plate seed {seed:>8} identity {sim:.3f} "
              f"face-scale {scale:.3f}"
              f"{'  rejected: ' + ','.join(bad) if bad else ''}")
        if not bad and sim > best_sim:
            best, best_sim = cand, sim
    result = None
    if best is None or best_sim < GATE:
        print(f"  [stylist2] no plate passed the {GATE} gate "
              f"(best {best_sim:.3f}) — falling back to the locked look")
    else:
        dest = WARDROBE_DIR / f"stylist2_{slug}.png"
        dest.write_bytes(best.read_bytes())
        print(f"  [stylist2] plate -> {dest.name} (identity {best_sim:.3f})")
        result = (dest, best_sim)
    # candidates are working files, not assets (host asset hygiene: only the
    # locked winner lives in assets, and cached candidates block redraws)
    for c in cand_dir.glob(f"stylist2_{slug}_s*.png"):
        c.unlink(missing_ok=True)
    return result


def draw_keyframe(style: dict, slug: str, wardrobe_plate: Path,
                  best_of: int = 12) -> tuple[Path, float] | None:
    """The day's RENDER KEYFRAME, drawn natively at 16:9. References: his
    identity plate + the day's OWN wardrobe plate, so the outfit carries over
    instead of the locked tee coming back."""
    import cv2

    from . import comfy
    from . import host2 as H2
    from . import host_identity_score as HID

    cfg = H2._cfg()
    if not comfy.ensure_server(cfg):
        print("  [stylist2] klein server unreachable for the 16:9 keyframe")
        return None
    plate = OUT / "host2_front.png"
    refs = [plate, wardrobe_plate]
    prompt = (f"{_IDENTITY_FACE}. {_staging()} He has {H2.HAIR}. He is "
              f"{style['outfit']}. {H2.KEYFRAME_EXPRESSION} There is no text, "
              f"no lettering and no digits anywhere in the frame.")
    pe, _ = HID.embed(cv2.imread(str(plate)))
    KEYFRAME_DIR.mkdir(parents=True, exist_ok=True)
    cand_dir = KEYFRAME_DIR / ".cand"
    cand_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(best_of):
        seed = 11 + 104729 * i                      # the locked keyframe's stride
        cand = cand_dir / f"kf2_{slug}_s{seed}.png"
        if not cand.exists():
            # supersampled, same as the locked keyframe — see host2.SUPERSAMPLE for
            # the measurements. A styled day must not ship a softer face than an
            # unstyled one.
            if not H2._draw_supersampled(cfg, refs, prompt, cand, seed):
                continue
        img = cv2.imread(str(cand))
        e, box = HID.embed(img)
        if e is None:
            print(f"  [stylist2] 16:9 seed {seed:>8} no face — rejected")
            continue
        sim, scale, cx, bad = _gates(img, e, pe, box, need_cx=True)
        if sim < GATE:
            bad = bad + ["IDENTITY"]
        detail = H2.face_detail(img)
        # The empty wall carries the slide cards — a drawn corner there is a
        # line down the finished video. See host2.WALL_SEAM_MAX.
        seam, seam_x = H2.wall_seam(img)
        if seam > H2.WALL_SEAM_MAX:
            bad = bad + [f"WALL SEAM at x={seam_x:.2f}"]
        print(f"  [stylist2] 16:9 seed {seed:>8} identity {sim:.3f} "
              f"scale {scale:.3f} cx {cx:.2f} detail {detail:.3f} seam {seam:.1f}"
              f"{'  rejected: ' + ','.join(bad) if bad else ''}")
        if not bad:
            rows.append({"path": cand, "identity": sim, "detail": detail})
    # Sharpest among the identity-competitive, NOT simply the highest identity —
    # see host2.pick_keyframe. Picking on identity alone is why three looks drawn
    # the same day gained 1.56x, 1.29x and NOTHING from the supersample.
    win = H2.pick_keyframe(rows)
    if win is None:
        print(f"  [stylist2] no 16:9 keyframe passed the {GATE} gate")
        for c in cand_dir.glob(f"kf2_{slug}_s*.png"):
            c.unlink(missing_ok=True)
        return None
    # "stage16x9" in the name is the signal to compose_scene that the subject is
    # ALREADY at the right third and needs no wall-stretch shift.
    dest = KEYFRAME_DIR / f"stylist2_{slug}_stage16x9.png"
    shutil.copy(win["path"], dest)
    for c in cand_dir.glob(f"kf2_{slug}_s*.png"):
        c.unlink(missing_ok=True)
    print(f"  [stylist2] keyframe -> {dest.name} "
          f"(identity {win['identity']:.3f} detail {win['detail']:.3f})")
    return dest, win["identity"]


def build_look(weather: dict, cfg: dict, best_of: int = 12) -> dict | None:
    """The full day: design -> wardrobe plate -> 16:9 keyframe -> history.
    Returns the record, or None (= fall back to the locked white-tee keyframe).
    """
    host = json.loads(HOST_JSON.read_text())
    style = design(weather, host, cfg)
    slug = style["slug"]
    print(f"  [stylist2] look '{slug}': {style['outfit'][:110]}...")
    plate = draw_plate(style, slug, best_of)
    if plate is None:
        return None
    staged = draw_keyframe(style, slug, plate[0], best_of)
    if staged is None:
        return None
    record = {"weather": weather, "style": style,
              "plate_identity": round(plate[1], 3),
              "keyframe_identity": round(staged[1], 3),
              "plate": str(plate[0].relative_to(ROOT)),
              "keyframe": str(staged[0].relative_to(ROOT))}
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    (HISTORY_DIR / f"{weather['date']}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1))
    return {"slug": slug, **record}


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily weather stylist for 陈谷雨")
    ap.add_argument("--date", help="real Open-Meteo fetch for this date")
    args = ap.parse_args()

    from . import generate as G
    cfg = G.load_config()
    weather = nyc_weather(args.date) if args.date else None
    if not weather:
        raise SystemExit("pass --date (and weather must resolve)")
    result = build_look(weather, cfg)
    if result is None:
        raise SystemExit("stylist day failed its gates — locked look stands")
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

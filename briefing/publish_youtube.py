"""Publish a rendered briefing edition to YouTube (Data API v3).

Reads an edition's `meta.json` out of the archive and uploads its English cut,
sets the cover as the thumbnail, and writes the returned video id back into
`meta.json` — that record is the idempotency guard, so a retried cron or a
re-run can never double-post the same edition. The cron uploads only the
English cut (--lang en, the default); the zh cut is not published.

    python -m briefing.publish_youtube auth                       # one-time OAuth
    python -m briefing.publish_youtube test-lock                  # does this project force private?
    python -m briefing.publish_youtube publish --date 2026-07-24 --slot slideshow-midday-briefing
    python -m briefing.publish_youtube publish --date 2026-07-24 --all-slideshows --lang en

AI DISCLOSURE
-------------
Every upload sets ``status.containsSyntheticMedia = True``. The narration is TTS
and the visuals are diffusion-generated, so YouTube's altered/synthetic content
disclosure applies to all of it. There is deliberately no flag to switch this
off — see ``_DISCLOSURE`` for the matching human-readable line stamped into the
description.

THE PRIVATE LOCK
----------------
Videos uploaded via ``videos.insert`` from an API project that has not passed a
YouTube compliance audit are force-locked to private, and locked videos cannot
be appealed. ``test-lock`` determines empirically whether this project is
affected: it uploads a throwaway clip requesting ``public``, reads the status
back, and deletes it. Run that before trusting this for a live schedule.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import generate as G

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]

# Stamped into every description, alongside the API-level synthetic-media flag.
_DISCLOSURE = ("\n\nThis video is generated automatically. The narration is synthetic "
               "speech and the visuals are AI-generated.")
_DISCLAIMER = ("\n\nNot investment advice. Automated market commentary for information "
               "only; do your own research.")

_SLIDESHOW_SLOTS = ["slideshow-market-open-briefing",
                    "slideshow-midday-briefing",
                    "slideshow-pre-close-briefing"]

_TITLE_MAX, _DESC_MAX, _TAGS_MAX = 100, 5000, 500

# YouTube renders the FIRST THREE hashtags found in the description above the video
# title, and — the trap worth knowing — ignores ALL hashtags on a video that carries
# more than 15. Cap well inside that so a long watchlist can never silently void the
# lot. `tags` (the keyword field above) and hashtags are separate surfaces; both are
# weak ranking signals, hashtags mostly add a browsable link and a topic cue.
_HASHTAG_MAX = 12


# --------------------------------------------------------------------------
# config + auth
# --------------------------------------------------------------------------
def _ycfg(cfg: dict) -> dict:
    y = dict(cfg.get("youtube") or {})
    y.setdefault("client_secret", "briefing/.youtube/client_secret.json")
    y.setdefault("token", "briefing/.youtube/token.json")
    y.setdefault("category_id", "25")          # News & Politics
    y.setdefault("privacy", "private")
    y.setdefault("made_for_kids", False)
    y.setdefault("base_tags", ["stocks", "stock market", "finance", "investing",
                               "market news", "trading"])
    return y


def _paths(cfg: dict) -> tuple[Path, Path]:
    y = _ycfg(cfg)
    return G.ROOT / y["client_secret"], G.ROOT / y["token"]


def _credentials(cfg: dict, *, interactive: bool = False, port: int = 8080):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret, token = _paths(cfg)
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token.write_text(creds.to_json())
        return creds
    if not interactive:
        sys.exit(f"no usable YouTube credentials at {token}\n"
                 f"run:  python -m briefing.publish_youtube auth")
    if not secret.exists():
        sys.exit(f"missing OAuth client secret at {secret}\n"
                 f"Create one in Google Cloud Console (OAuth client ID -> Desktop app)\n"
                 f"and save the downloaded JSON there.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    # This box is headless/remote. Google killed the out-of-band flow in 2022, so
    # the redirect must reach a loopback port — bind a FIXED one and let the user
    # SSH-forward it from the laptop running the browser:
    #     ssh -L 8080:localhost:8080 <this-server>
    # A "Desktop app" OAuth client accepts http://localhost on any port, so no
    # redirect URI needs registering.
    print(f"  [auth] waiting on http://localhost:{port} — from your laptop run:")
    print(f"         ssh -L {port}:localhost:{port} <this-server>")
    creds = flow.run_local_server(port=port, open_browser=False,
                                  authorization_prompt_message=
                                  "\nOpen this URL in the browser on that laptop:\n\n{url}\n")
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    token.chmod(0o600)
    print(f"  [auth] saved {token}")
    return creds


def _manual_auth(cfg: dict, response: Optional[str] = None, port: int = 8080) -> None:
    """Two-step OAuth that needs no SSH tunnel.

    ``run_local_server`` expects Google's redirect to land on a loopback port on
    THIS machine, which it can't when the browser is on someone's laptop. Instead
    print the consent URL, let the browser fail to load the localhost redirect,
    and take the ``code`` straight out of the address bar.
    """
    import os

    # A loopback redirect is http:// by design, which oauthlib otherwise refuses;
    # and Google returns the granted scopes reordered/expanded, which it also
    # treats as a mismatch. Both are expected for an installed-app flow.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    from google_auth_oauthlib.flow import Flow

    secret, token = _paths(cfg)
    if not secret.exists():
        sys.exit(f"missing OAuth client secret at {secret}")
    redirect = f"http://localhost:{port}"
    flow = Flow.from_client_secrets_file(str(secret), SCOPES, redirect_uri=redirect)
    # The two halves run as separate processes, so the PKCE code_verifier that
    # authorization_url() generates must outlive step 1 — without it Google
    # rejects the exchange with invalid_grant.
    pending = secret.parent / ".auth_pending.json"

    if not response:
        url, _ = flow.authorization_url(access_type="offline", prompt="consent",
                                        include_granted_scopes="true")
        pending.write_text(json.dumps({"code_verifier": flow.code_verifier}))
        pending.chmod(0o600)
        print("\n1. Open this URL in a browser signed in to the target YouTube channel:\n")
        print(url)
        print(f"\n2. Approve. The browser will then fail to load {redirect}/?code=…")
        print("   That failure is expected — the code is in the address bar.")
        print("\n3. Copy the WHOLE address-bar URL and re-run:\n")
        print('   python -m briefing.publish_youtube auth --manual --response "<paste>"\n')
        return

    resp = response.strip()
    if pending.exists():
        flow.code_verifier = json.loads(pending.read_text()).get("code_verifier")
    try:
        if resp.startswith("http"):
            flow.fetch_token(authorization_response=resp)
        else:
            flow.fetch_token(code=resp)
    except Exception as e:                                  # noqa: BLE001
        sys.exit(f"token exchange failed: {e}\n"
                 "Codes are single-use and expire quickly — re-run `auth --manual` "
                 "for a fresh URL.")
    creds = flow.credentials
    if not creds.refresh_token:
        sys.exit("no refresh_token returned — re-run `auth --manual` (the URL forces "
                 "prompt=consent, which is what grants one).")
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    token.chmod(0o600)
    pending.unlink(missing_ok=True)
    print(f"  [auth] saved {token} (refresh token present)")


def _service(cfg: dict, *, interactive: bool = False):
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_credentials(cfg, interactive=interactive),
                 cache_discovery=False)


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------
def _yt_safe(text: str) -> str:
    """Strip characters the API rejects outright in titles/descriptions.

    `<` and `>` cause a 400 invalidDescription / invalidTitle. Your notes use
    them for thresholds ("FY >$25B", "RSI <30"), so map to the fullwidth forms —
    they render as real comparison signs and keep the meaning, where dropping
    them would silently invert it.
    """
    return (text or "").replace("<", "＜").replace(">", "＞")


def _clip(text: str, limit: int) -> str:
    text = _yt_safe(text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tidy(text: str) -> str:
    """Round a headline off at a clean boundary.

    meta.json stores `headline` already hard-cut at 150 chars, so it usually ends
    mid-word ("...surged almost four percent to a reco"). Fall back to the last
    sentence end, else the last word, so the description reads properly.
    """
    text = (text or "").strip()
    if not text:
        return ""
    end = max(text.rfind(". "), text.rfind("! "), text.rfind("? "))
    if end >= len(text) * 0.6:                 # a sentence break late enough to keep
        return text[: end + 1]
    cut = text.rfind(" ")
    return (text[:cut].rstrip(" ,;:—-") + "…") if cut > 0 else text


def _hashtag(text: str) -> str:
    """'stock market' -> '#stockmarket'.

    A hashtag ends at the first space or punctuation, so 'stock market' pasted in
    raw would publish as '#stock' followed by the bare word 'market'. Strip to
    alphanumerics rather than let YouTube silently truncate it.
    """
    s = re.sub(r"[^0-9A-Za-z]", "", text or "")
    return f"#{s}" if s else ""


# Clip "symbols" that are not tickers: they must never become a #hashtag or a
# keyword tag. MARKET is the overview slide, DESK is the live-dashboard slide.
_NON_TICKER = {"MARKET", "DESK"}


def _hashtags(meta: dict, cfg: dict) -> list[str]:
    """Description hashtags: the configured topic tags first, then the edition's
    own tickers.

    Order is deliberate — the first three are the ones YouTube surfaces above the
    title, so those come from config (a stable channel-level topic cue) rather than
    from whichever symbols happened to make today's cut.

    The topic set is chosen per render KIND: a post-market recap and a midday
    slideshow are different formats searched for in different words, so a recap
    leads with #marketrecap rather than the generic channel set. Falls back to
    `hashtags` for any kind without its own list.
    """
    y = _ycfg(cfg)
    out: list[str] = []
    topics = (y.get("hashtags_by_kind") or {}).get(meta.get("kind") or "",
                                                   y.get("hashtags", []))
    syms = [c.get("symbol") for c in meta.get("clips", [])
            if c.get("symbol") and c["symbol"] not in _NON_TICKER]
    for t in [*topics, *syms]:
        h = _hashtag(t)
        if h and h.lower() not in {o.lower() for o in out}:
            out.append(h)
    return out[:_HASHTAG_MAX]


def build_snippet(meta: dict, lang: str, cfg: dict) -> dict:
    """Map an edition's meta.json onto a YouTube snippet."""
    y = _ycfg(cfg)
    zh = lang == "zh"
    raw = (meta.get("title_zh") if zh else meta.get("title")) or ""
    # Prefix the edition so the three daily slideshows are distinguishable at a
    # glance in a subscriptions feed ("Midday Briefing | NVDA slides to …").
    # Clip the HEADLINE, never the label — the label is the part that identifies
    # which of the day's editions this is.
    label = _yt_safe((meta.get("label") or "").strip()).title()
    fmt = y.get("title_format", "{label} | {title}")
    if label and "{label}" in fmt:
        room = _TITLE_MAX - len(fmt.format(label=label, title=""))
        title = fmt.format(label=label, title=_clip(raw, max(room, 0))) if raw else label
    else:
        title = _clip(raw, _TITLE_MAX)
    if not title:
        title = _clip(f"{meta.get('label', 'Market briefing')} — {meta['date']}", _TITLE_MAX)

    head = f"{meta.get('label', 'Market briefing')} · {meta['date']}"
    lines = [head, ""]
    for c in meta.get("clips", []):
        sym = "MARKET" if c.get("symbol") == "MARKET" else c.get("symbol", "")
        hl = _tidy(c.get("headline"))
        if hl:
            lines.append(f"{sym} — {hl}")
    tag_line = " ".join(_hashtags(meta, cfg))
    body = ("\n".join(lines)
            + (f"\n\n{tag_line}" if tag_line else "")
            + _DISCLOSURE + _DISCLAIMER)

    syms = [c.get("symbol") for c in meta.get("clips", [])
            if c.get("symbol") and c["symbol"] not in _NON_TICKER]
    tags, used = [], 0
    for t in [*syms, *y["base_tags"]]:
        if t and t not in tags and used + len(t) + 1 <= _TAGS_MAX:
            tags.append(t)
            used += len(t) + 1

    return {
        "title": title,
        "description": _clip(body, _DESC_MAX),
        "tags": tags,
        "categoryId": str(y["category_id"]),
        "defaultLanguage": "zh-CN" if zh else "en",
        "defaultAudioLanguage": "zh-CN" if zh else "en",
    }


def build_localizations(meta: dict, primary: str, cfg: dict) -> dict:
    """Metadata for the OTHER language, attached to the same video.

    YouTube then shows the title/description in the viewer's own locale — one
    video, one URL, two languages of *text*. The audio track is NOT covered:
    multi-language audio is YouTube Studio-only with no API surface, so this
    localises the page, not the narration.
    """
    other = "zh" if primary == "en" else "en"
    s = build_snippet(meta, other, cfg)
    return {"zh-CN" if other == "zh" else "en":
            {"title": s["title"], "description": s["description"]}}


def build_status(cfg: dict, *, privacy: Optional[str] = None,
                 publish_at: Optional[str] = None) -> dict:
    y = _ycfg(cfg)
    st = {
        "privacyStatus": privacy or y["privacy"],
        "selfDeclaredMadeForKids": bool(y["made_for_kids"]),
        # AI disclosure — non-negotiable for this pipeline.
        "containsSyntheticMedia": True,
    }
    if publish_at:
        st["publishAt"] = publish_at        # requires privacyStatus=private
        st["privacyStatus"] = "private"
    return st


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------
def _upload(svc, video: Path, snippet: dict, status: dict, log=print,
            localizations: Optional[dict] = None) -> str:
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    body = {"snippet": snippet, "status": status}
    part = "snippet,status"
    if localizations:
        body["localizations"] = localizations
        part += ",localizations"
    req = svc.videos().insert(part=part, body=body, media_body=media)
    resp, last = None, -1
    while resp is None:
        prog, resp = req.next_chunk()
        if prog:
            pct = int(prog.progress() * 100)
            if pct >= last + 20:
                log(f"    … {pct}%")
                last = pct
    return resp["id"]


_THUMB_MAX = 2 * 1024 * 1024          # YouTube's hard limit on thumbnail bytes


def _fit_thumbnail(cover: Path, log=print) -> Path:
    """Return a thumbnail within YouTube's 2 MB limit.

    The klein cover sheets land just over it (~2.1 MB PNG), so re-encode to JPEG
    rather than let the upload fail. Quality steps down until it fits.
    """
    if cover.stat().st_size <= _THUMB_MAX:
        return cover
    from PIL import Image
    import tempfile
    im = Image.open(cover).convert("RGB")
    out = Path(tempfile.gettempdir()) / f"yt_thumb_{cover.stem}.jpg"
    for q in (92, 85, 78, 70, 60):
        im.save(out, "JPEG", quality=q, optimize=True)
        if out.stat().st_size <= _THUMB_MAX:
            log(f"    [thumb] {cover.stat().st_size // 1024}KB PNG -> "
                f"{out.stat().st_size // 1024}KB JPEG (q{q})")
            return out
    return out


def _set_thumbnail(svc, vid: str, cover: Path, log=print) -> bool:
    from googleapiclient.http import MediaFileUpload
    try:
        img = _fit_thumbnail(cover, log=log)
        svc.thumbnails().set(videoId=vid,
                             media_body=MediaFileUpload(str(img))).execute()
        return True
    except Exception as e:                                   # noqa: BLE001
        # Never fatal: the video is already up and recorded. Custom thumbnails
        # also need a phone-verified channel, which fails the same way.
        log(f"    [warn] thumbnail not set: {type(e).__name__}: {e}")
        return False


def _readback(svc, vid: str) -> dict:
    r = svc.videos().list(part="status,snippet,processingDetails", id=vid).execute()
    items = r.get("items") or []
    return items[0] if items else {}


# --------------------------------------------------------------------------
# publish one edition
# --------------------------------------------------------------------------
def _meta_path(cfg: dict, date: str, slot: str) -> Path:
    return Path(cfg["archive_dir"]) / date / slot / "meta.json"


def latest_edition(cfg: dict, *, kind: Optional[str] = None,
                   label: Optional[str] = None) -> tuple[str, str]:
    """(date, slot) of the most recently generated edition in the archive.

    Lets the cron hook publish whatever the render just produced without having
    to recompute the date (which can roll over UTC vs local) or slugify the
    label the same way generate.briefing_slot does.
    """
    root = Path(cfg["archive_dir"])
    best = None
    for mp in root.glob("*/*/meta.json"):
        if mp.parent.parent.name.startswith("_"):
            continue
        try:
            m = json.loads(mp.read_text())
        except (OSError, ValueError):
            continue
        if kind and m.get("kind", "") != kind:
            continue
        if label and (m.get("label") or "").lower() != label.lower():
            continue
        stamp = m.get("generated_at") or ""
        if best is None or stamp > best[0]:
            best = (stamp, m.get("date") or mp.parent.parent.name, mp.parent.name)
    if best is None:
        sys.exit(f"no archived edition found (kind={kind!r} label={label!r})")
    return best[1], best[2]


def _confirm_flag(video: Path, t: float, dur: float, cfg: dict, tmp: Path,
                  log=print) -> bool:
    """Re-test a neighbouring frame before believing a flag.

    A lone flag on the assembled video is usually NOT an extra limb. Shots are
    cross-dissolved into each other (`story.transition_s`), so for ~0.25s at every
    beat boundary the character is legitimately drawn TWICE, ghosted over itself —
    and the anatomy reviewer reads that as extra limbs. The first run of this gate
    blocked a test upload on exactly such a frame (68.3s, mid-dissolve between two
    GOOG beats, two Gigis and two subtitle lines on screen at once).

    A real defect is a property of the SHOT, so it persists for the ~5s the shot
    lasts; a dissolve lasts a quarter of a second. Sampling 0.8s away therefore
    lands outside any transition but inside the same shot. Requiring one neighbour
    to agree also damps the reviewer's own run-to-run variance.
    """
    from . import director
    for off in (0.8, -0.8):
        t2 = t + off
        if not (0.2 <= t2 <= dur - 0.2):
            continue
        fp = tmp / f"yt_qc_confirm_{video.stem}_{off}.png"
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t2:.2f}",
                            "-i", str(video), "-frames:v", "1", str(fp)],
                           check=True, capture_output=True, text=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        agrees = director.anatomy_ok(fp, cfg) is False
        fp.unlink(missing_ok=True)
        if agrees:
            return True
    log(f"    [qc] {t:.1f}s flagged but neighbours are clean — treating as a "
        f"crossfade/variance artifact, not a defect")
    return False


def preflight_qc(video: Path, cfg: dict, log=print) -> tuple[bool, list[str]]:
    """Sample frames across a FINISHED video and run the anatomy reviewer on each.

    WHY a second pass when the render already QCs. The render checks each klein
    storyboard, then 5 sampled frames per Wan shot — and defects still reach the
    archive: the 2026-07-28 recap shipped a three-legged anchor and a three-handed
    Pip through both gates, and they were found by eye afterwards. Those gates
    protect the RENDER, which is cheap to redo. This one protects the UPLOAD, which
    is not: once a video is public, fixing it means deleting and re-posting under a
    new URL and losing whatever the old one had accumulated.

    It samples the ASSEMBLED video rather than the raws, so it also covers anything
    introduced after the per-shot check — the stitch, the crossfades, the HUD.

    Returns (ok, failures) where each failure is a human-readable "at 42.0s" note.
    A reviewer that cannot decide counts as a pass, matching `director.anatomy_ok`'s
    contract: this gate must not block an upload because ollama was busy.
    """
    import tempfile

    from . import comfy, director
    from .ltx_story import _free_gpu
    q = _ycfg(cfg).get("preflight_qc", {})
    n = max(1, int(q.get("frames", 24)))
    dur = G._duration(video)
    if dur <= 0:
        log("    [qc] cannot read duration — skipping frame review")
        return True, []

    # Same VRAM discipline as the render: the reviewer is a 32B vision model and
    # cannot share the GPU with whatever klein/Wan left resident. Getting this wrong
    # does not fail loudly, it just makes ollama spill to CPU and crawl.
    #
    # comfy.free_memory is NOT enough, and 2026-08-14 is what proved it: it posts to
    # _base(c), i.e. only the CONFIGURED instance (comfyui.port, 8188). The host recap
    # is rendered by a SECOND ComfyUI on :8189, and that one is never asked to free.
    # That day it sat on 17 GB after the render while :8188 held 764 MB, so the freed
    # port was the empty one; only 37% of the 30.4 GB reviewer fit on the GPU, 63%
    # spilled to system RAM, and a 171 s review was still grinding at 979% CPU after
    # 30 minutes. _free_gpu covers ltx_story.PORTS = (8188, 8189), so both servers
    # release. Harmless while the reviewer is cloud-hosted, and the thing that makes
    # reverting to a local model safe again.
    _free_gpu()
    comfy.free_memory(cfg)
    director.unload(cfg)

    tmp = Path(tempfile.gettempdir())
    fails: list[str] = []
    shots: list[tuple[float, Path, bool]] = []
    log(f"    [qc] reviewing {n} frame(s) across {dur:.0f}s before upload …")
    for i in range(n):
        t = dur * (i + 0.5) / n          # midpoints: never the very first/last frame
        fp = tmp / f"yt_qc_{video.stem}_{i}.png"
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}",
                            "-i", str(video), "-frames:v", "1", str(fp)],
                           check=True, capture_output=True, text=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        flagged = director.anatomy_ok(fp, cfg) is False
        if flagged and _confirm_flag(video, t, dur, cfg, tmp, log=log):
            fails.append(f"at {t:.1f}s")
            log(f"    [qc] FLAGGED {t:.1f}s")
            shots.append((t, fp, True))
        else:
            shots.append((t, fp, False))
    director.unload(cfg)

    sheet = _contact_sheet(video, shots, cfg, log=log) if q.get("contact_sheet", True) else None
    for _, fp, _f in shots:
        fp.unlink(missing_ok=True)

    if fails:
        log(f"    [qc] {len(fails)}/{n} frame(s) flagged for extra limbs")
    else:
        log(f"    [qc] no blocking defect in {n} frame(s) reviewed")
    if sheet:
        # Deliberately loud even on a pass. The reviewer cannot judge HANDS at all
        # (director._BLOCK_ON), so for that class of defect this sheet is the only
        # check there is — a ten-second glance, not a formality.
        log(f"    [qc] contact sheet -> {sheet}  (eyeball it: extra HANDS are not "
            f"machine-detectable)")
    return not fails, fails


def _contact_sheet(video: Path, shots: list, cfg: dict, log=print) -> Optional[Path]:
    """Tile the reviewed frames into one annotated image for a human glance.

    This exists because the automated half is provably partial: banded counting caught
    a three-legged character but is inverted on hands (see director._BLOCK_ON), so a
    clean machine verdict is not evidence the frames are clean. Every frame the gate
    looked at goes on the sheet with its timestamp, and anything the model did flag
    gets a red border, so the timestamps map straight onto shots to re-render.
    """
    if not shots:
        return None
    try:
        from PIL import Image, ImageDraw
        q = _ycfg(cfg).get("preflight_qc", {})
        out_dir = Path(q.get("sheet_dir") or (G.WORK / "qc_review"))
        out_dir.mkdir(parents=True, exist_ok=True)
        cols = int(q.get("sheet_cols", 4))
        tw = int(q.get("sheet_tile_w", 480))
        first = Image.open(shots[0][1])
        th = round(tw * first.height / first.width)
        rows = (len(shots) + cols - 1) // cols
        pad, head = 8, 52
        sheet = Image.new("RGB", (cols * (tw + pad) + pad, head + rows * (th + pad) + pad),
                          (24, 26, 32))
        d = ImageDraw.Draw(sheet)
        flagged_n = sum(1 for _t, _p, f in shots if f)
        # PIL's default bitmap font is unreadable on a ~2000px canvas AND cannot render
        # an em-dash (it draws a tofu box), so borrow the project's real font and keep
        # this text ASCII.
        hf, tf = G._cover_font("A", 24), G._cover_font("0", 20)
        d.text((pad + 2, 14),
               f"{video.name} - {len(shots)} frames reviewed, {flagged_n} flagged by the "
               f"model. Extra HANDS are NOT machine-detectable: check those by eye.",
               font=hf, fill=(235, 235, 240))
        for i, (t, fp, flag) in enumerate(shots):
            try:
                im = Image.open(fp).convert("RGB").resize((tw, th), Image.LANCZOS)
            except OSError:
                continue
            x = pad + (i % cols) * (tw + pad)
            y = head + (i // cols) * (th + pad)
            sheet.paste(im, (x, y))
            if flag:
                d.rectangle([x, y, x + tw - 1, y + th - 1], outline=(228, 60, 48), width=5)
            d.rectangle([x, y + th - 28, x + 104, y + th], fill=(12, 14, 20))
            d.text((x + 8, y + th - 25), f"{t:.1f}s", font=tf,
                   fill=(228, 60, 48) if flag else (240, 200, 120))
        dest = out_dir / f"{video.stem}_qc.png"
        sheet.save(dest, "PNG")
        return dest
    except Exception as exc:  # noqa: BLE001 — a missing sheet must never block a publish
        log(f"    [qc] contact sheet skipped ({type(exc).__name__}: {exc})")
        return None


def publish(cfg: dict, date: str, slot: str, langs: list[str], *,
            privacy: Optional[str] = None, publish_at: Optional[str] = None,
            dry_run: bool = False, force: bool = False, localize: bool = False,
            skip_qc: bool = False, log=print) -> dict:
    mp = _meta_path(cfg, date, slot)
    if not mp.exists():
        sys.exit(f"no edition at {mp}")
    meta = json.loads(mp.read_text())
    day = mp.parent
    posted = dict(meta.get("youtube") or {})

    svc = None if dry_run else _service(cfg)
    for lang in langs:
        key = "en" if lang == "en" else "zh"
        if posted.get(key) and not force:
            log(f"  [skip] {slot} {key} already posted as {posted[key]['id']}")
            continue
        vf = meta.get("briefing_file" if key == "en" else "briefing_file_zh")
        if not vf or not (day / vf).exists():
            log(f"  [skip] {slot} {key}: no video file")
            continue
        # Frame review before the irreversible step. Gated to the Wan recap: a
        # slideshow is typographic and has no cast in it at all, so there are no
        # limbs to count and no reason to spend the GPU.
        q = _ycfg(cfg).get("preflight_qc", {})
        if (q.get("enabled", True) and not skip_qc and not dry_run
                and meta.get("kind") != "slideshow"):
            ok_qc, fails = preflight_qc(day / vf, cfg, log=log)
            if not ok_qc and q.get("block", True):
                log(f"  [BLOCKED] {slot} {key}: anatomy QC flagged {len(fails)} frame(s) "
                    f"({', '.join(fails[:6])}) — NOT uploading. Re-render the edition, "
                    f"or pass --skip-qc to override.")
                continue
            if not ok_qc:
                log(f"  [warn] anatomy QC flagged {len(fails)} frame(s); "
                    f"youtube.preflight_qc.block=false so uploading anyway")

        snippet = build_snippet(meta, key, cfg)
        status = build_status(cfg, privacy=privacy, publish_at=publish_at)
        loc = build_localizations(meta, key, cfg) if localize else None
        log(f"  [{key}] {snippet['title']}")
        log(f"       tags={','.join(snippet['tags'][:6])}… cat={snippet['categoryId']} "
            f"privacy={status['privacyStatus']} synthetic={status['containsSyntheticMedia']}"
            + (f" +localized={','.join(loc)}" if loc else ""))
        if dry_run:
            continue

        vid = _upload(svc, day / vf, snippet, status, log=log, localizations=loc)

        # Record BEFORE anything else can throw. The video exists on YouTube the
        # moment insert returns; if we defer this and the thumbnail step fails,
        # the id is lost and a re-run silently double-posts.
        def _record(**extra) -> None:
            posted[key] = {"id": vid, "url": f"https://youtu.be/{vid}",
                           "posted_at": datetime.now(timezone.utc)
                           .isoformat(timespec="seconds"), **extra}
            meta["youtube"] = posted
            mp.write_text(json.dumps(meta, indent=2))

        _record(privacy=status["privacyStatus"])

        cover = meta.get("cover_file" if key == "en" else "cover_file_zh")
        if cover and (day / cover).exists():
            _set_thumbnail(svc, vid, day / cover, log=log)
        back = _readback(svc, vid).get("status", {})
        log(f"    -> https://youtu.be/{vid}  privacy={back.get('privacyStatus')} "
            f"upload={back.get('uploadStatus')}")
        _record(privacy=back.get("privacyStatus") or status["privacyStatus"])

    return posted


def set_privacy(cfg: dict, date: str, slot: str, langs: list[str], to: str,
                log=print) -> None:
    """Change privacy on an already-published edition and re-record it.

    videos.update REPLACES the parts you name, and containsSyntheticMedia is
    write-only (it never comes back on read), so it has to be re-asserted here —
    otherwise flipping a video to public would silently drop its AI disclosure.
    """
    mp = _meta_path(cfg, date, slot)
    if not mp.exists():
        sys.exit(f"no edition at {mp}")
    meta = json.loads(mp.read_text())
    posted = dict(meta.get("youtube") or {})
    svc = _service(cfg)
    for key in langs:
        rec = posted.get(key)
        if not rec:
            log(f"  [skip] {slot} {key}: not published")
            continue
        vid = rec["id"]
        cur = _readback(svc, vid).get("status", {})
        body = {"id": vid, "status": {
            "privacyStatus": to,
            "selfDeclaredMadeForKids": bool(cur.get("selfDeclaredMadeForKids", False)),
            "license": cur.get("license", "youtube"),
            "embeddable": cur.get("embeddable", True),
            "containsSyntheticMedia": True,          # re-assert: write-only field
        }}
        # Trust the UPDATE response, not a follow-up videos.list: reads are
        # eventually consistent and a read issued immediately after the write
        # returns the OLD privacy, which would get recorded as if it had failed.
        resp = svc.videos().update(part="status", body=body).execute()
        got = resp.get("status", {}).get("privacyStatus")
        rec["privacy"] = got
        posted[key] = rec
        meta["youtube"] = posted
        mp.write_text(json.dumps(meta, indent=2))
        flag = resp.get("status", {}).get("containsSyntheticMedia")
        log(f"  [{key}] {vid} -> {got}  (synthetic={flag})  https://youtu.be/{vid}")


# --------------------------------------------------------------------------
# the private-lock probe
# --------------------------------------------------------------------------
def test_lock(cfg: dict, *, keep: bool = False, log=print) -> None:
    """Upload a throwaway clip requesting PUBLIC and see what comes back.

    If the project has not passed a compliance audit, YouTube silently forces
    privacyStatus to 'private'. Requesting public is the only way to find out.
    """
    import shutil, tempfile
    ff = shutil.which("ffmpeg")
    if not ff:
        sys.exit("ffmpeg not on PATH (activate the alpaca-media env)")
    tmp = Path(tempfile.mkdtemp(prefix="ytlock_"))
    clip = tmp / "lock_probe.mp4"
    subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=navy:s=640x360:d=5:r=15",
                    "-vf", "drawtext=text='API test - please ignore':"
                           "fontcolor=white:fontsize=28:x=(w-tw)/2:y=(h-th)/2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    log(f"  [probe] built {clip.name} ({clip.stat().st_size} bytes)")

    svc = _service(cfg)
    snippet = {"title": f"API lock probe {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}",
               "description": "Throwaway upload to test API upload privacy behaviour."
                              + _DISCLOSURE,
               "categoryId": str(_ycfg(cfg)["category_id"]),
               "tags": ["test"]}
    status = {"privacyStatus": "public",          # <- the actual probe
              "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": True}
    log("  [probe] uploading with privacyStatus=public …")
    vid = _upload(svc, clip, snippet, status, log=log)
    info = _readback(svc, vid)
    st = info.get("status", {})
    got = st.get("privacyStatus")

    log("")
    log(f"  video id        : {vid}   https://youtu.be/{vid}")
    log(f"  requested       : public")
    log(f"  actual privacy  : {got}")
    log(f"  uploadStatus    : {st.get('uploadStatus')}")
    for k in ("failureReason", "rejectionReason"):
        if st.get(k):
            log(f"  {k:15s} : {st[k]}")
    log("")
    if got == "public":
        log("  ==> NOT LOCKED. This project can publish public videos. No audit needed.")
    elif got == "private":
        log("  ==> LOCKED TO PRIVATE. This project is un-audited: every API upload will be")
        log("      forced private and cannot be appealed. Submit the YouTube API Services")
        log("      Audit and Quota Extension Form before scheduling live uploads.")
    else:
        log(f"  ==> Unexpected privacyStatus {got!r} — inspect manually.")

    if keep:
        log(f"  [probe] keeping {vid} (--keep)")
    else:
        svc.videos().delete(id=vid).execute()
        log(f"  [probe] deleted {vid}")
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Publish briefing editions to YouTube")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("auth", help="run the one-time OAuth flow and store the refresh token")
    pa.add_argument("--port", type=int, default=8080,
                    help="loopback port for the OAuth redirect; SSH-forward it (default 8080)")
    pa.add_argument("--manual", action="store_true",
                    help="no SSH tunnel: print the consent URL, then paste the "
                         "redirected address back via --response")
    pa.add_argument("--response", help="the localhost URL (or bare code) from the browser")

    pl = sub.add_parser("test-lock", help="probe whether this project force-locks uploads to private")
    pl.add_argument("--keep", action="store_true", help="don't delete the probe video")

    ps = sub.add_parser("set-privacy", help="change privacy on an already-published edition")
    ps.add_argument("--to", required=True, choices=["public", "unlisted", "private"])
    ps.add_argument("--date")
    ps.add_argument("--slot")
    ps.add_argument("--latest", action="store_true")
    ps.add_argument("--kind", choices=["slideshow", "video"])
    ps.add_argument("--label")
    ps.add_argument("--lang", default="en", choices=["en", "zh", "both"])

    pp = sub.add_parser("publish", help="upload an edition")
    pp.add_argument("--date", help="YYYY-MM-DD (omit with --latest)")
    pp.add_argument("--slot", help="e.g. slideshow-midday-briefing, or video")
    pp.add_argument("--all-slideshows", action="store_true",
                    help="publish all three slideshow slots for --date")
    pp.add_argument("--latest", action="store_true",
                    help="resolve date+slot from the most recently generated edition "
                         "(what the render just produced) — used by the cron hook")
    pp.add_argument("--kind", choices=["slideshow", "video"],
                    help="restrict --latest to this render kind")
    pp.add_argument("--label", help="restrict --latest to this edition label")
    pp.add_argument("--lang", default="en", choices=["en", "zh", "both"])
    pp.add_argument("--privacy", choices=["public", "unlisted", "private"])
    pp.add_argument("--publish-at", help="RFC3339 UTC, e.g. 2026-07-29T13:00:00Z")
    pp.add_argument("--dry-run", action="store_true", help="print metadata, upload nothing")
    pp.add_argument("--force", action="store_true", help="re-post even if already recorded")
    pp.add_argument("--skip-qc", action="store_true",
                    help="upload without the pre-flight anatomy frame review "
                         "(recap only; slideshows have no cast so are never reviewed)")
    pp.add_argument("--localize", action="store_true",
                    help="one page, two languages of TEXT: attach the other language's "
                         "title/description as a localization so viewers see their own "
                         "locale. Audio stays single-track (no API for audio tracks).")

    a = ap.parse_args()
    cfg = G.load_config()

    if a.cmd == "auth":
        if a.manual or a.response:
            _manual_auth(cfg, response=a.response, port=a.port)
        else:
            _credentials(cfg, interactive=True, port=a.port)
            print("  [auth] ok")
        return
    if a.cmd == "test-lock":
        test_lock(cfg, keep=a.keep)
        return
    if a.cmd == "set-privacy":
        if a.latest:
            date, slot = latest_edition(cfg, kind=a.kind, label=a.label)
        elif a.date and a.slot:
            date, slot = a.date, a.slot
        else:
            sys.exit("give --latest, or both --date and --slot")
        print(f"=== {date} / {slot} -> {a.to} ===")
        set_privacy(cfg, date, slot, ["en", "zh"] if a.lang == "both" else [a.lang], a.to)
        return

    if a.latest:
        date, slot = latest_edition(cfg, kind=a.kind, label=a.label)
        slots = [slot]
    elif a.slot or a.all_slideshows:
        if not a.date:
            sys.exit("--date is required unless --latest is given")
        date, slots = a.date, (_SLIDESHOW_SLOTS if a.all_slideshows else [a.slot])
    else:
        sys.exit("give --latest, --slot or --all-slideshows")

    langs = ["en", "zh"] if a.lang == "both" else [a.lang]
    for slot in slots:
        print(f"\n=== {date} / {slot} ===")
        publish(cfg, date, slot, langs, privacy=a.privacy,
                publish_at=a.publish_at, dry_run=a.dry_run, force=a.force,
                localize=a.localize, skip_qc=a.skip_qc)


if __name__ == "__main__":
    main()

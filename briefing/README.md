# Research → video briefing

Turns the daily Claude research notes (`decisions/decisions.json` → `notes`)
into a narrated MP4 "briefing" that the dashboard serves on a **Briefing** page
(`/briefing.html`, linked from the terminal header).

**What runs today.** Two scheduled renders: the weekday **post-market recap**
(`agent/run_research_postmarket.sh`, 20:00 ET) is the photoreal presenter's hosted
slideshow — `host_briefing.py`, with the presenter named by `config.json`
`host.host_json` (陈谷雨, `host2.json`; persona and assets are local-only, see the
root README) and the day's look from `host2_stylist.py` (09:35 ET,
`agent/run_stylist.sh`). The Sunday **weekend recap**
(`agent/run_research_weekend.sh`) is the cartoon story video below, its shots
rendered by MiniMax H3.

**Fully offline and out-of-band from trading.** `generate.py` only *reads*
`decisions/decisions.json` and *writes* `web/static/media/`. It never imports the
relay/order path. The read-only dashboard serves the resulting files as plain
static assets (no `web/server.py` change was needed).

## The cast (fixed, reusable)

Each ticker has ONE fixed cartoon character chosen to match the company's
temperament, in `briefing/characters.json` with a transparent sprite under
`briefing/assets/characters/<SYM>.png` (both version-controlled). The cast is
fixed and committed; a ticker with no character gets a generic cartoon mascot.

Current cast: NVDA=Cheetah, TSLA=Peacock, GOOG=Professor Owl, MSFT=Butler(human),
AMZN=Ant, AAPL=Swan, META=Chameleon, ORCL=Elephant, SOXL=Grasshopper,
TQQQ=Kangaroo, BTC=Wolf.

## Modes

`config.json` → `"mode"` selects the briefing format:

- **`story`** (default) — an animated cartoon briefing: ONE scene per symbol,
  narrated with the REAL briefing facts (bias, price move, catalyst, mechanics).
  Two renderers (`story.renderer`):
  - **`hyperframes`** — the fast typographic bullet slideshow: one slide per
    symbol (catalyst cards + a key-numbers grid), no characters and no image
    generation.
  - **`wan`** — vivid generative render (the weekend recap). The name is
    historical: the shots are rendered by **MiniMax H3**
    (`story.video_engine: "h3"`, the only engine — Wan2.2 was retired as a
    renderer), conditioned on the fixed character sprite plus a klein
    storyboard per beat, then voiceover + subtitles + ticker are added. Falls
    back to `hyperframes` on any failure before the shots exist.
  Scenes are stitched into the briefing; each is a chapter in the rundown.
- **`factual`** — static per-symbol cards (ticker + bias + callouts). The automatic
  fallback if story generation fails.

**GPU note:** Gemma (~23GB with think=high) and SDXL (~8GB) don't fit together, so
scenes render in two passes — plan every scene with Gemma, unload it, then render
with SDXL. The director model defaults to local `gemma4:31b`; switch to
`gemma4:31b-cloud` if the GPU is busy.

## Pipeline

```
raw note  ──Gemma (THE DIRECTOR, ollama)──> screenplay: narration + shot plan   (optional)
narration ──edge-tts──────────────────────> .mp3 + .srt (word-timed subtitles)
bg + icon prompts ──SDXL (diffusers)──────> background + small element thumbnails (optional)
the above ──hyperframes────────────────────> clip .mp4  (HTML+GSAP → headless Chrome + ffmpeg)
subtitles ──ffmpeg─────────────────────────> burned into each clip
all clips ──ffmpeg concat──────────────────> briefing_<date>.mp4
                                           └> web/static/media/{manifest,history}.json
```

**THE DIRECTOR** (`briefing/director.py`, optional, on by default) runs a local
ollama Gemma model — default the **cloud** `gemma4:31b-cloud` so it uses **no
local VRAM** and never competes with SDXL or other GPU jobs. Per scene it rewrites
the dry note into a wry, briefing-ready spoken script and a shot plan: an
on-screen tagline, a background prompt, and 1-3 animated element cards (each a
label, a punchy value, an SDXL icon prompt, and a motion verb). Hard prompt
guardrails keep every ticker/number/%/level/bias/date straight from the note — it
restyles, it never fabricates. It's **video-only**: the trade path reads
decisions.json directly and never sees this output. On any failure (ollama down,
timeout, OOM, bad JSON) each scene silently falls back to the deterministic
text/callouts — a render never fails because of the director. Config lives under
`"director"` in config.json (`enabled`, `model`, `think`, `humor`,
`target_seconds`, `max_elements`, …).

Each scene also gets an ambient Ken-Burns background and **burned subtitles**
(from the narration) synced to the voiceover.

## The desk slide (the live dashboard, on camera)

The hosted recap (and any bullets slideshow) opens on the market and then, as
the **second slide**, turns the camera on the system itself: a screenshot of the **live
dashboard**, held wide for a beat and then pushed panel by panel — equity, the
open position, what the risk layer blocked, today's rotation board — narrated
from the account's own numbers.

```
.venv  briefing/desk_probe.py ── load → FREEZE → screenshot → scrape → fetch APIs
                                        │
media  briefing/desk.py ── fact sheet ──┴─> director.desk_narration (gemma + NUMERIC GUARD)
                                                    │            └─ fails → deterministic script
                                        camera ─────┴─> hyperframes slide → clip_DESK.mp4
```

**One frozen page.** The probe runs in the trading `.venv` (playwright and the
dashboard's read-only builders both live there), loads the dashboard once, kills
every poll timer, and only then shoots. The screenshot, the panel geometry, the
on-screen figures and the API payloads all come from that one paint — so when the
video zooms into the equity card, the figure being spoken is the figure in the
picture. Panel boxes are read from the DOM at capture time (`.card--portfolio`,
`#execTable`, …), so a dashboard re-layout moves the camera with it.

**The numeric guard is the point.** Every other slide's script was written
upstream by Claude from a research note. This is the only slide where the local
gemma model is handed raw account figures, so the output is checked rather than
trusted: `director.desk_narration` scans each take for digits that are not on the
fact sheet and throws the whole take away if it finds one. Deliberately strict —
a re-rounded number ("roughly $5,000"), a derived one ("cash is 5.5% of the
account") and a truncated one ("up 97 dollars") are all rejections even when
arithmetically true, because the model is not the thing that gets to do
arithmetic about a real account. After three tries the deterministic fact-sheet
script ships instead, which is what makes the slide safe to schedule unattended.

**The camera follows the words.** Each narration beat is scored against the panel
vocabulary and the push targets the winner, so a line about a blocked entry lands
on the signal journal even though it also mentions the position. Scored, not
first-match: "the only open position is 14 shares of GOOG, no fills since the
last briefing" is about the position, not the fills.

**"What changed" needs a baseline.** Each edition writes its digest to
`meta["desk"]`; the next edition diffs against the most recent archived one, which
is where "MSFT moved from flat to long", "the book opened GOOG" and the
fills/blocks window come from. The first edition after this shipped has no
baseline and simply says less.

**Nothing here can fail a render.** Dashboard up → screenshot slide. Dashboard
down → the same facts on the deck's ordinary typographic layout. Capture broken →
no desk slide at all and the deck renders exactly as it did before.

> **This puts a real account on the internet.** The recaps publish to YouTube
> automatically, so equity, holdings and P&L go public with them. It is the paper
> account, and the dashboard's own PAPER · ACTIVE badge is in frame — but if that
> is not wanted, set `desk.enabled: false` in `config.json` and the slide stops
> being rendered.

Check the fact sheet and the capture by hand (media env):

```bash
python -m briefing.desk                       # capture + print the fact sheet gemma gets
.venv/bin/python -m briefing.desk_probe \
    --png /tmp/desk.png --json /tmp/desk.json # just the capture (trading venv)
.venv/bin/python -m briefing.desk_probe --no-shot ...   # data only, no browser
```

Config lives under `"desk"` (`enabled`, `url`, `after`, `lines`, `min_zoom` /
`max_zoom`, `timeout_s`). `DESK` is a slide, not a ticker: `publish_youtube`
keeps it out of the hashtags and keyword tags while still giving it its chapter
line in the description.

## Storage / history

- **Current** briefing + clips + `manifest.json` + `history.json` → `out_dir`
  (`web/static/media/`, the system SSD) — served locally, fast.
- **Every** briefing is archived to `archive_dir`
  (`/mnt/sandisk/alpaca/briefings/<date>/`, with a `meta.json`) — the durable
  history. `out_dir` is pruned to the current date each run; older briefings live
  only in the archive.
- The dashboard's **Briefing / Research Desk** page (`/briefing.html`) reads
  `history.json` and offers a **session date-picker**; past sessions stream from
  the server's read-only `/archive/<date>/…` route (`web/server.py`,
  `MEDIA_ARCHIVE_DIR`, env `BRIEFING_ARCHIVE_DIR`). The current session streams
  from `/media/…`.

- **edge-tts** — neural TTS, no API key.
- **Stability AI** via `diffusers` — local image gen on the GPU. Backend is a
  single config value (`image_model`). Default `stabilityai/sdxl-turbo`
  (~1.4 s/image, 4 steps). If the GPU/model is unavailable or `--no-images` is
  passed, every scene falls back to a procedural gradient — no hard GPU dependency.
- **hyperframes** (HeyGen, Apache-2.0) — deterministic HTML→MP4.

## Run

Everything runs in the dedicated **`alpaca-media`** conda env
(edge-tts + torch/diffusers + node/ffmpeg), **not** the trading `.venv`:

```bash
./briefing/run_briefing.sh                 # SDXL backgrounds, all defaults
./briefing/run_briefing.sh --no-images     # gradient backgrounds, no GPU
./briefing/run_briefing.sh --symbols NVDA,AMZN
./briefing/run_briefing.sh --max-symbols 3
```

Cron (after the research run):

```
20 13 * * 1-5  cd <repo> && ./briefing/run_briefing.sh >> briefing/briefing.log 2>&1
```

## Publishing to YouTube

`briefing/publish_youtube.py` uploads an archived edition via the YouTube Data
API v3. It reads the edition's `meta.json`, so titles/description/cover/tags all
come from what the render already produced — nothing is re-derived.

**This is automatic for every scheduled render.** The weekday post-market recap
and the weekend recap (`run_research_postmarket.sh` and `run_research_weekend.sh`,
`--kind video`) upload right after they're archived — English audio only, the
edition's own cover as the thumbnail. Each sets `BRIEFING_PUBLISH=1`; set it to `0`
to render without uploading. A bare `run_briefing.sh` — an ad-hoc or test render —
still publishes nothing. `--kind slideshow` remains for a bullets slideshow
published by hand.

The two tiers differ in more than bookkeeping. A recap gets its own hashtag set
(`youtube.hashtags_by_kind.video`, leading with `#marketrecap`), and it is the only
tier that runs the pre-upload anatomy QC — see **Pre-upload frame review** below.
A `bullets` slideshow is typographic and has no cast, so there is nothing to count.

```bash
python -m briefing.publish_youtube auth --manual             # one-time OAuth (see below)
python -m briefing.publish_youtube test-lock                 # see "the private lock" below
python -m briefing.publish_youtube publish --latest --kind slideshow --lang en
python -m briefing.publish_youtube publish --latest --kind video --lang en
python -m briefing.publish_youtube publish --date 2026-07-24 \
       --all-slideshows --lang en --dry-run                  # print metadata, upload nothing
python -m briefing.publish_youtube set-privacy --latest --kind slideshow --to public
```

Slideshows publish **public** by default (`youtube.privacy`). `set-privacy`
re-asserts `containsSyntheticMedia` on every change, because `videos.update`
replaces the whole `status` part and that field is write-only — updating privacy
without it would silently drop the AI disclosure. It also trusts the update
*response* rather than a follow-up read: YouTube's reads are eventually
consistent, and one issued straight after a write returns the OLD privacy.

`--latest` resolves the date+slot from the most recently generated edition, which
is what the cron hook uses — no date arithmetic, no UTC/local rollover bug.

### Titles

`youtube.title_format` (default `"{label} | {title}"`) prefixes the edition label
so editions are distinguishable in a subscriptions feed:

```
Post-Market Recap | Microsoft gains 15.5% in record market value surge
```

The *headline* is clipped to fit YouTube's 100-char limit, never the label.

### Hashtags

Two different surfaces, easily confused. `youtube.base_tags` is the invisible
keyword field; `youtube.hashtags` are appended to the **description** and are what
a viewer sees. YouTube renders the **first three** above the video title, and — the
trap — **ignores every hashtag on a video carrying more than 15**, so the combined
list (topics + the edition's tickers) is capped at 12 in code.

`youtube.hashtags_by_kind` overrides the topic set per `meta.kind`, because a
recap and a midday slideshow are searched for in different words:

```
slideshow  #stocks #stockmarket #investing …            + tickers
video      #marketrecap #stockmarket #closingbell …     + tickers
```

### Pre-upload frame review

**Recap only.** Before the irreversible step, `preflight_qc` samples 24 frames
across the *assembled* video and runs the anatomy reviewer on each — a separate
gate from the render's own, and for a different reason: the render's gates protect
something cheap to redo, this one protects a public URL. A flagged frame is
re-tested 0.8s away before it is believed, since a crossfade legitimately draws the
character twice and reads as extra limbs.

What it can and cannot do — **the automated half is partial, and weaker than it
first measured**:

- **Extra legs/feet: sometimes.** Banded counting (`director` v4) blocks the
  generated three-legged anchor that the earlier reviewer passed (2026-07-30), and
  `block: true` then refuses the upload. But on 2026-07-31, three fixtures built by
  compositing a real trouser-leg-and-shoe into a *published* frame — twice beside
  the character's own pair, once standing alone on open floor — were reported as
  **exactly two feet by every band that saw them**, and all three frames passed.
  Raw points were 4 (the same real pair, seen by two bands) and deduping was not
  the cause: the third shoe was never detected at any tolerance. So the foot
  channel shares the two-limb prior the hand channel has. It has caught a real
  defect; it is a backstop, not a guarantee.
- **Extra hands: NOT machine-detectable.** The hand channel is *inverted* on real
  frames — it missed a genuine third hand and fired on a clean frame — at every
  tolerance tested, so `director._BLOCK_ON` blocks on feet only.
- **A broken check reads as a pass.** `anatomy_ok` returns `None` when the model
  errors or answers with something that is not JSON (observed live while measuring
  the above), and callers must treat `None` as a pass — otherwise one bad response
  blocks a good upload. Transient model noise therefore skips the gate silently.

Which is why **every run writes a contact sheet** to
`briefing/.hf_work/qc_review/<video>_qc.png` — every reviewed frame, timestamped,
model-flagged tiles bordered red. Its path is logged even when the run passes,
because a clean machine verdict is not evidence the frames are clean. Ten seconds
of eyeballing it is the only check for hands and the real check for feet.
`--skip-qc` overrides the gate.

### Language

Only the **English** cut is uploaded. The zh cut is rendered and archived
locally, shown only on the owner's private dashboard, and never published.

**AI disclosure is always on.** Every upload sets `status.containsSyntheticMedia
= true` (TTS narration + generated visuals) and stamps a matching line into the
description. There is no flag to disable it.

**Idempotency:** the returned video id is written back to the edition's
`meta.json` under `youtube.en`, and an already-posted edition is skipped.
A retried cron cannot double-post. Use `--force` to deliberately re-upload.

### One-time setup

1. Google Cloud Console → new project → enable **YouTube Data API v3**
2. **OAuth consent screen** → External. Set publishing status to **In production** —
   while it is "Testing", refresh tokens expire after **7 days** and the cron dies weekly.
3. **Credentials → OAuth client ID → Desktop app** → download the JSON to
   `briefing/.youtube/client_secret.json` (gitignored)
4. `python -m briefing.publish_youtube auth --manual`

Step 4 needs a browser, and this box is headless. Google removed the out-of-band
flow in 2022, so `--manual` prints a consent URL, you approve it in any browser,
the redirect to `localhost:8080` **fails as expected**, and you paste that whole
address back:

```bash
python -m briefing.publish_youtube auth --manual --response "http://localhost:8080/?code=..."
```

(`auth --port N` instead runs a real local server if you'd rather SSH-forward.)
Expect a **"Google hasn't verified this app"** warning — you are the developer of
your own single-user client; click **Advanced → Go to …**. The resulting
`briefing/.youtube/token.json` is full channel access — treat it like a password.

### The private lock

Videos uploaded through `videos.insert` from an API project that has **not passed
a YouTube compliance audit** are force-locked to private, and locked videos
cannot be appealed. This is a property of the API *project*, not the channel —
uploading to your own channel is not an exemption.

`test-lock` settles it empirically: it builds a 5-second throwaway clip, uploads
it requesting `public`, reads `status.privacyStatus` back, reports the verdict,
and deletes the video (`--keep` to retain it).

> **Verified 2026-07-27 on this project: NOT locked.** A probe requesting
> `public` stayed public through processing to `succeeded`, so no audit is
> needed here. Re-run `test-lock` if you ever move to a different Cloud project.

If it ever reports LOCKED, submit the [YouTube API Services Audit and Quota
Extension Form](https://support.google.com/youtube/contact/yt_api_form) before
scheduling anything live; expect a multi-week turnaround.

### Gotchas already handled

- **`<` / `>` in text** → hard 400 `invalidDescription`. Your notes use them for
  thresholds ("FY ＞$25B"), so they're mapped to fullwidth forms rather than dropped.
- **Cover ≥ 2 MB** → thumbnails are capped at 2 MB; oversized PNGs are
  re-encoded to JPEG automatically.
- **Thumbnail failures are never fatal** — the video id is written to `meta.json`
  *immediately* after insert, before any follow-up step can throw. Without that
  ordering a thumbnail error orphans an uploaded video and the next run
  double-posts it.
- A brand-new channel may 403 on `thumbnails.set` for a while; it starts working
  once the channel settles.

### Quota

`videos.insert` sits in its own allowance (~100 uploads/day) and
`thumbnails.set` costs 50 units of the 10,000/day pool, so one upload a day is a
rounding error. Older guides quoting 1,600 units per upload predate the
Dec 2025 reduction. Caption tracks are *not* uploaded — subtitles are already
burned in, and no whole-edition SRT is archived; adding `captions.insert` (400
units each) would need the render to archive one first.

## Config — `briefing/config.json`

Notable knobs: `image_model` / `image_steps` / `image_guidance` (swap Turbo for
`stable-diffusion-xl-base-1.0` at 30 steps / guidance 7 for a quality pass, or a
gated SD 3.5 model), `voice`, `rate`, `max_symbols`, `max_note_chars`,
`max_overview_chars`, `width`/`height`/`fps`.

Scene selection (when `symbols` is null): the overview (`notes["_"]`) plus the
top `max_symbols` names ranked non-flat-first, then by priority, then watchlist
order.

## One-time env setup

```bash
conda create -y -n alpaca-media -c conda-forge python=3.11 nodejs=22 ffmpeg
conda activate alpaca-media
pip install edge-tts diffusers transformers accelerate safetensors numpy
pip install rembg onnxruntime scipy            # story-mode sprite cutouts
pip install torch --index-url https://download.pytorch.org/whl/cu128   # Blackwell/CUDA 12.8
# story mode also needs ollama running with a gemma model (default gemma4:31b-cloud)
```

Rendered videos and `.hf_work/` are gitignored (regenerated each run).

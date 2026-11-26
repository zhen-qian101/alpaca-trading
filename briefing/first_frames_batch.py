"""Generate the Wan I2V first frames + number icons in a SEPARATE process.

Runs the text-to-image model (SD 3.5 / SDXL) in its own process so that when it
EXITS its CUDA context is fully released. If the first frames were instead made
in the main render process, that process keeps a CUDA context alive while the
Wan I2V child runs — two contexts on one GPU time-slice against each other and
Wan runs ~10x slower (measured: ~4.6 s/step clean vs a multi-minute stall). See
story.render_scenes_wan phases (3)/(4).

Input JSON (argv[1]):
  {"cfg": {...}, "tmp": "/abs/tmp",
   "scenes": [{"sym": "NVDA", "setting": "...", "character": {...},
               "icons": [{"i": 0, "prompt": "..."}, ...]}, ...]}
Writes  ff_<safe>.png  per scene and  prop_<safe>_<i>.png  per kept icon into tmp.
Prints FF_DONE/ICON_DONE (or *_FAIL) per artifact so the caller can log progress.
"""
import json
import shutil
import sys
from pathlib import Path


def main() -> None:
    spec = json.load(open(sys.argv[1]))
    from briefing import story as S
    from briefing import generate as G
    import numpy as np
    from PIL import Image

    cfg = spec["cfg"]
    tmp = Path(spec["tmp"])
    pipe = S.load_pipe(cfg)
    if pipe is None:
        print("FF_FAIL no image model", flush=True)
        return
    seg = S.load_seg(cfg)

    for sc in spec["scenes"]:
        sym = sc["sym"]
        if sc.get("skip_ff"):                   # klein storyboards cover this scene;
            print(f"FF_SKIP {sym}", flush=True)  # only its icons are needed here.
        else:
            ff = tmp / f"ff_{G._safe(sym)}.png"
            try:
                S._first_frame(sym, sc["character"], sc.get("setting", ""), cfg, pipe, ff)
                print(f"FF_DONE {sym}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"FF_FAIL {sym} {type(exc).__name__}: {exc}", flush=True)
                continue
        for ic in sc.get("icons", []):
            i = ic["i"]
            raw = tmp / f"propraw_{G._safe(sym)}_{i}.png"
            cut = tmp / f"prop_{G._safe(sym)}_{i}.png"
            kr = ic.get("klein_raw")
            if kr and Path(kr).exists():        # klein already drew it in the crayon
                shutil.copy(kr, raw)            # style — just cut out the background.
            elif not S.make_sprite(pipe, None, ic["prompt"], raw, raw, cfg, suffix=S._PROP_SUFFIX):
                continue
            chosen = raw
            if seg is not None:
                try:
                    S._cut_out(raw, cut, seg)
                    if (np.array(Image.open(cut).convert("RGBA"))[:, :, 3] > 10).mean() > 0.06:
                        chosen = cut
                except Exception:  # noqa: BLE001
                    pass
            if chosen != cut:                       # canonicalise: parent looks for prop_<sym>_<i>.png
                shutil.copy(chosen, cut)
            print(f"ICON_DONE {sym} {i}", flush=True)


if __name__ == "__main__":
    main()

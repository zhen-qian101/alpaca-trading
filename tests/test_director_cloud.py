"""_chat_json: the client-side schema guard the cloud path made necessary.

Local llama.cpp grammar-constrains `format`, so schema keys always land. The
ollama cloud path only *requests* the schema — measured 2026-08-03,
deepseek-v4-flash returned {"visual_prop": …} through a schema requiring
{"prop": …}, plus markdown fences. These tests pin the three behaviours that
make a cloud director safe: required keys are enforced client-side, a
mis-keyed answer gets one corrective retry, and a dead/misbehaving cloud model
falls back to the local model instead of to the deterministic text.
"""
from __future__ import annotations

import io
import json

import pytest

from briefing import director


SCHEMA = {"type": "object", "properties": {"prop": {"type": "string"}},
          "required": ["prop", "action"]}


def _payload(model="deepseek-v4.1-flash:cloud", think="max"):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "stream": False, "format": SCHEMA, "think": think,
            "options": {"num_predict": 100}}


class _Ollama:
    """Scripted /api/chat endpoint: pops one canned reply per call and records
    every request body it saw."""

    def __init__(self, monkeypatch, replies):
        self.replies, self.requests = list(replies), []
        monkeypatch.setattr(director.urllib.request, "urlopen", self)

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(json.dumps(
            {"message": {"content": json.dumps(r) if isinstance(r, dict) else r}}).encode())


def test_a_correct_answer_passes_through(monkeypatch):
    ol = _Ollama(monkeypatch, [{"prop": "a spring", "action": "bounce"}])
    out = director._chat_json(_payload(), {}, 30, "t")
    assert out["prop"] == "a spring" and len(ol.requests) == 1


def test_missing_keys_get_one_corrective_retry(monkeypatch):
    """The measured failure: right idea, wrong key names. Retry must NAME the
    required keys so the model can comply."""
    ol = _Ollama(monkeypatch, [{"visual_prop": "x"},
                               {"prop": "x", "action": "y"}])
    out = director._chat_json(_payload(), {}, 30, "t")
    assert out == {"prop": "x", "action": "y"}
    correction = ol.requests[1]["messages"][-1]["content"]
    assert "prop, action" in correction


def test_fenced_json_is_stripped(monkeypatch):
    """The second measured failure mode: ```json fences around the object."""
    _Ollama(monkeypatch, ['```json\n{"prop": "x", "action": "y"}\n```'])
    assert director._chat_json(_payload(), {}, 30, "t")["action"] == "y"


def test_cloud_failure_falls_back_to_the_local_model(monkeypatch):
    ol = _Ollama(monkeypatch, [OSError("cloud unreachable"),
                               OSError("cloud unreachable"),
                               {"prop": "x", "action": "y"}])
    d = {"fallback_model": "gemma4:31b"}
    out = director._chat_json(_payload(), d, 30, "t")
    assert out["prop"] == "x"
    assert ol.requests[-1]["model"] == "gemma4:31b"


def test_fallback_maps_cloud_think_levels_to_local_ones(monkeypatch):
    """gemma's local runner has no 'max' level; sending it one on the fallback
    leg would trade a cloud outage for a local 500."""
    ol = _Ollama(monkeypatch, [OSError("down"), OSError("down"),
                               {"prop": "x", "action": "y"}])
    director._chat_json(_payload(think="max"), {"fallback_model": "gemma4:31b"}, 30, "t")
    assert ol.requests[-1]["think"] == "high"


def test_everything_failing_raises_for_the_caller(monkeypatch):
    _Ollama(monkeypatch, [OSError("down")] * 4)
    with pytest.raises(ValueError, match="t:"):
        director._chat_json(_payload(), {"fallback_model": "gemma4:31b"}, 30, "t")


def test_no_fallback_configured_keeps_the_old_shape(monkeypatch):
    """Without director.fallback_model the behaviour matches today's: try the
    one model (with its retry), then raise. Live config stays valid unchanged."""
    ol = _Ollama(monkeypatch, [OSError("down"), OSError("down")])
    with pytest.raises(ValueError):
        director._chat_json(_payload(model="gemma4:31b", think="high"), {}, 30, "t")
    assert all(r["model"] == "gemma4:31b" for r in ol.requests)


def test_unload_skips_cloud_models(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        if isinstance(req, str):                       # the /api/ps probe
            return io.BytesIO(json.dumps({"models": []}).encode())
        calls.append(json.loads(req.data)["model"])
        return io.BytesIO(b"{}")

    monkeypatch.setattr(director.urllib.request, "urlopen", fake_urlopen)
    director.unload({"director": {"model": "deepseek-v4.1-flash:cloud"}})
    assert calls == []                                 # nothing local to evict


# --------------------------------------------------------------------------
# anatomy_ok: one flaky band must not void the frame
# --------------------------------------------------------------------------
def test_a_failed_band_is_skipped_not_fatal(tmp_path, monkeypatch, capsys):
    """Before the guard, one unparseable qwen reply aborted the whole check
    (None -> treated as a pass), discarding the bands that DID answer — the QC
    contributed nothing exactly when the model was flakiest."""
    from PIL import Image
    img = tmp_path / "frame.png"
    Image.new("RGB", (640, 640), "white").save(img)

    replies = iter([
        {"bbox": [0, 0, 1000, 1000]},                      # stage 1: bbox
        ValueError("no JSON object in response"),          # band 1: flaky
    ])
    def scripted(model, prompt, png, rv):
        r = next(replies, {"hands": [{"x": 100, "y": 100}, {"x": 900, "y": 100},
                                     {"x": 500, "y": 500}],
                           "feet": [{"x": 300, "y": 950}, {"x": 700, "y": 950}]})
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(director, "_vqc_chat", scripted)
    verdict = director.anatomy_ok(img, {"story": {"review": {"block_on": ["hands", "feet"]}}})
    assert verdict is False                    # 3 hands counted from surviving bands
    assert "skipping this band" in capsys.readouterr().out


# --------------------------------------------------------------------------
# MiniMax H3 screenplay: camera fields + the clip prompt grammar
# --------------------------------------------------------------------------
def test_h3_screenplay_validates_camera_fields(monkeypatch):
    """Same beats as the wan screenplay, plus per-beat camera; junk camera
    values normalize to the guide's defaults instead of leaking into prompts."""
    good = {"setting": "a desk", "beats": [
        {"line": "one two three four five six", "action": "walks", "framing": "wide shot",
         "angle": "low angle", "movement": "push in"},
        {"line": "six five four three two one", "action": "sits"}]}
    monkeypatch.setattr(director, "_chat_json", lambda *a, **k: good)
    sp = director.scene_screenplay_h3("MSFT", "long", "note", {"name": "Windy"},
                                      {"director": {}, "story": {}})
    assert sp["beats"][0]["movement"] == "push in"
    assert sp["beats"][1]["framing"] == "medium shot"      # default, not KeyError


def test_h3_clip_prompt_places_cuts_at_line_boundaries():
    beats = [{"line": "a", "action": "picks up the block", "framing": "wide shot",
              "angle": "eye-level", "movement": "static shot"},
             {"line": "b", "action": "stacks the block", "framing": "close-up",
              "angle": "low angle", "movement": "push in"}]
    p = director.h3_clip_prompt({"name": "Windy", "kind": "boy"}, "a study",
                                beats, [6.2, 5.5], "Crayon style.")
    assert "[Shot 1] Crayon style." in p
    assert "exactly as established in <Picture 1>" in p
    assert "[Shot 2] At 00:06.200, the camera cuts to" in p       # = beat 1's duration
    assert "close-up" in p and "the camera pushes in" in p
    assert "overall_soundscape" in p


def test_h3_clip_prompt_uses_the_official_i2va_field_structure():
    """references/base-en §2: the alignment instruction is the first line, then a
    blank line, then the three named fields in order. This graph passes only
    first_frame (h3.i2v), so the instruction is I2VA's, not FL2VA's two-picture
    form — promising a last frame the model never receives."""
    p = director.h3_clip_prompt({"name": "Windy"}, "a study", _BEATS2, [6.0, 6.0],
                                "Crayon.")
    lines = p.split("\n")
    assert lines[0] == ("For the target video, at 0.00 seconds into the target "
                        "video, <Picture 1> (from [Shot 1]) is fully referenced.")
    assert lines[1] == ""
    assert lines[2].startswith("integrated_multimodal_description: [Shot 1]")
    assert p.index("integrated_multimodal_description") < p.index("overall_soundscape")
    assert p.index("overall_soundscape") < p.index("non_diegetic_music")
    assert "aligns with the" not in p          # the FL2VA instruction must not appear


def test_h3_camera_motion_is_a_verb_not_a_stacked_label():
    """base-en §4.3: motion is written as a natural English action, and amplitude
    /speed are omitted unless meaningful. The pre-2026-08-06 builder hardcoded
    'does a static shot at slow speed with small amplitude' on every shot."""
    still = [{"line": "x", "action": "nods", "framing": "wide shot",
              "angle": "eye-level", "movement": "static shot"}]
    p = director.h3_clip_prompt({"name": "W"}, "a desk", still, [5.0], "Crayon.")
    assert "the camera holds a static shot" in p
    assert "does a" not in p
    assert "amplitude" not in p and "speed" not in p   # never on a static shot

    moved = [dict(still[0], movement="push in", amplitude="large", speed="fast")]
    q = director.h3_clip_prompt({"name": "W"}, "a desk", moved, [5.0], "Crayon.")
    assert "the camera pushes in with large amplitude at fast speed" in q


def test_h3_clip_prompt_contains_no_quoted_strings():
    """The official guide treats double-quoted text as a request to DRAW that
    text on screen — one stray quote puts writing into the frame."""
    beats = [{"line": "x", "action": "nods", "framing": "medium shot",
              "angle": "eye-level", "movement": "static shot"}]
    p = director.h3_clip_prompt({"name": "Zippy"}, "a dock", beats, [5.0], "Crayon.")
    assert '"' not in p


# --------------------------------------------------------------------------
# H3 fixes: duration-aware grouping, set restatement, unlabeled-chart clause
# --------------------------------------------------------------------------
def test_h3_grouping_pairs_short_beats_and_splits_long_ones():
    """The 2026-08-04 overflow: two ~9s lines crammed into one 15.08s clip left
    ~3s of narration on a frozen frame. 9+9 must split; 6+6 must pair."""
    assert director.h3_group_beats([9.05, 9.02]) == [
        ([0], director.h3_frames(9.05)), ([1], director.h3_frames(9.02))]
    g = director.h3_group_beats([6.0, 6.0])
    assert g == [([0, 1], director.h3_frames(12.0))]


def test_h3_grouping_covers_every_beat_in_order():
    g = director.h3_group_beats([6.0, 6.0, 7.0, 8.0])
    assert [i for grp, _ in g for i in grp] == [0, 1, 2, 3]
    assert g[0][0] == [0, 1]                    # 12s pairs
    assert g[1][0] == [2, 3]                    # 15s pairs exactly at the cap


def test_h3_a_single_overlong_beat_is_clamped_not_dropped():
    g = director.h3_group_beats([22.0])
    assert g == [([0], 362)]


def test_h3_frames_snaps_to_the_grid():
    assert director.h3_frames(10.0) % 17 == 5
    assert director.h3_frames(99.0) == 362      # capped at the trained max


_BEATS2 = [{"line": "x", "action": "stacks a block", "framing": "wide shot",
            "angle": "eye-level", "movement": "static shot"},
           {"line": "y", "action": "pats the tower", "framing": "close-up",
            "angle": "low angle", "movement": "push in"}]


def test_h3_prompt_restates_the_room_in_every_shot():
    """Set drift: stated once, the model kept the character but invented a new
    room after the first cut. The room description must appear per shot."""
    room = "a study with a wooden desk and a stack of toy blocks"
    p = director.h3_clip_prompt({"name": "Windy"}, room, _BEATS2, [6.0, 6.0], "Crayon.")
    assert p.count(room) == 2
    assert "very same room as the previous shot" in p


def test_h3_prompt_says_what_charts_show_instead_of_only_banning_text():
    p = director.h3_clip_prompt({"name": "Windy"}, "a desk", _BEATS2[:1], [5.0], "Crayon.")
    assert "unlabeled" in p and "no text" in p


# --------------------------------------------------------------------------
# ref2va production wiring
# --------------------------------------------------------------------------
def test_h3_prompt_ref_pictures_binds_identity_and_compositions():
    """<Picture 1> = the cast sprite (identity, no attribute text — enumerated
    features that contradicted the sprite made the model follow the words);
    shot k's composition = <Picture k+1>, its klein storyboard."""
    p = director.h3_clip_prompt({"name": "Windy"}, "a study", _BEATS2, [6.0, 6.0],
                                "Crayon.", ref_pictures=True)
    assert "EXACTLY the character shown in <Picture 1>" in p
    assert "a composition matching <Picture 2>" in p
    assert "a composition matching <Picture 3>" in p
    assert '"' not in p
    assert "square glasses" not in p          # no attribute enumeration


def test_h3_ref2va_graph_uses_the_nested_autogrow_form(tmp_path, monkeypatch):
    """The measured API gotcha: flat ref_image_N inputs pass validation then
    throw inside execute(); only the nested dict form works."""
    import io as _io
    import json as _json
    from briefing import h3 as h3mod

    sent = {}

    def fake_urlopen(req, timeout=None):
        if isinstance(req, str) or "history" in getattr(req, "full_url", str(req)):
            return _io.BytesIO(_json.dumps(
                {"x": {"status": {"completed": True}}}).encode())
        sent.update(_json.loads(req.data))
        return _io.BytesIO(b'{"prompt_id": "x"}')

    monkeypatch.setattr(h3mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(h3mod.shutil, "copy", lambda a, b: None)
    monkeypatch.setattr(h3mod.shutil, "move", lambda a, b: None)
    cfg = {"h3": {"dir": str(tmp_path), "gen_timeout_s": 1}}
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "h3_x.mp4").write_bytes(b"v")
    monkeypatch.setattr(h3mod.Path, "glob", lambda self, pat: iter([tmp_path / "output" / "h3_x.mp4"]), raising=False)
    h3mod.ref2va(cfg, "p", [tmp_path / "a.png", tmp_path / "b.png"], 147, 1,
                 tmp_path / "out.mp4", log=lambda *a: None)
    cond = sent["prompt"]["cond"]["inputs"]
    assert cond["ref_images"] == {"ref_image_1": ["img0", 0], "ref_image_2": ["img1", 0]}
    assert "ref_image_1" not in cond          # never the flat form


def test_split_beats_boundaries(tmp_path, monkeypatch):
    """Cuts land at the narration boundaries; the last beat keeps the grid-snap
    remainder (trimmed later by _beat_clip's -t)."""
    from briefing import h3 as h3mod
    calls = []
    monkeypatch.setattr(h3mod.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd))
    raws = [tmp_path / "r0.mp4", tmp_path / "r1.mp4"]
    assert h3mod.split_beats({}, tmp_path / "clip.mp4", [6.2, 5.5], raws)
    assert "-ss" in calls[0] and calls[0][calls[0].index("-ss") + 1] == "0.000"
    assert calls[0][calls[0].index("-t") + 1] == "6.200"
    assert calls[1][calls[1].index("-ss") + 1] == "6.200"
    assert "-t" not in calls[1]               # last beat keeps the remainder
    assert "scale=1280:704:flags=lanczos" in " ".join(calls[0])


def test_h3_variant_defaults_to_fl2va_and_pins_the_checkpoint():
    """fl2va is production (pixel-anchored identity; ref2va drifted clothing
    between clips). Each generator pins its own checkpoint regardless of the
    variant-following default."""
    from briefing import h3 as h3mod
    h = h3mod.hcfg({})
    assert h["variant"] == "fl2va"
    assert h["unet"] == "minimax_h3_fl2va_pruned_int8_convrot.safetensors"


def test_h3_i2v_graph_anchors_the_first_frame(tmp_path, monkeypatch):
    from briefing import h3 as h3mod
    sent = {}
    monkeypatch.setattr(h3mod.shutil, "copy", lambda a, b: None)
    monkeypatch.setattr(h3mod, "_queue_and_wait",
                        lambda cfg, g, prefix, dest, log: sent.update(g) or True)
    assert h3mod.i2v({"h3": {"dir": str(tmp_path)}}, "p", tmp_path / "sb.png",
                     243, 7, tmp_path / "o.mp4")
    cond = sent["cond"]["inputs"]
    assert sent["cond"]["class_type"] == "MiniMaxH3ImageToVideo"
    assert cond["first_frame"] == ["img", 0]
    assert sent["u"]["inputs"]["unet_name"].startswith("minimax_h3_fl2va")


def _h3_graph(tmp_path, monkeypatch, cfg):
    from briefing import h3 as h3mod
    sent = {}
    monkeypatch.setattr(h3mod.shutil, "copy", lambda a, b: None)
    monkeypatch.setattr(h3mod, "_queue_and_wait",
                        lambda cfg, g, prefix, dest, log: sent.update(g) or True)
    assert h3mod.i2v(cfg, "p", tmp_path / "sb.png", 243, 7, tmp_path / "o.mp4")
    return sent


def test_h3_easycache_code_default_is_off(tmp_path, monkeypatch):
    """config.json opts IN (measured 1.82x, no visible quality loss), but the
    CODE default stays off so a cfg without the key renders unaccelerated
    rather than silently caching. Graph must then be byte-identical to the
    pre-EasyCache one: no cache node, sampler reads UNETLoader directly."""
    g = _h3_graph(tmp_path, monkeypatch, {"h3": {"dir": str(tmp_path)}})
    assert "ec" not in g
    assert g["sch"]["inputs"]["model"] == ["u", 0]
    assert g["gd"]["inputs"]["model"] == ["u", 0]


def test_h3_easycache_when_enabled_feeds_both_scheduler_and_guider(tmp_path, monkeypatch):
    """EasyCache returns a cloned patcher. If only the guider reads it and the
    scheduler still reads ["u",0], the run is silently un-accelerated."""
    g = _h3_graph(tmp_path, monkeypatch, {"h3": {
        "dir": str(tmp_path),
        "easycache": {"enabled": True, "reuse_threshold": 0.25}}})
    assert g["ec"]["class_type"] == "EasyCache"
    assert g["ec"]["inputs"]["model"] == ["u", 0]
    assert g["ec"]["inputs"]["reuse_threshold"] == 0.25
    assert g["ec"]["inputs"]["start_percent"] == 0.15      # node default preserved
    assert g["sch"]["inputs"]["model"] == ["ec", 0]
    assert g["gd"]["inputs"]["model"] == ["ec", 0]


# --------------------------------------------------------------------------
# FL2VA: real first+last keyframes (the mode the fl2va checkpoint is named for)
# --------------------------------------------------------------------------
def test_h3_fl2va_prompt_uses_the_two_picture_instruction():
    """base-en §2.1: with a real last_frame the instruction is FL2VA's two-picture
    form, S.SS to exactly two decimals, and §3.2 requires the final shot to
    converge onto <Picture 2>."""
    p = director.h3_clip_prompt({"name": "Windy"}, "a study", _BEATS2, [6.0, 6.0],
                                "Crayon.", last_picture=True, clip_seconds=10.125)
    lines = p.split("\n")
    assert lines[0] == ("How the reference pictures align with the target video — "
                        "Picture 1 (from Shot 1) aligns with the 0.00-second mark of "
                        "the target video; Picture 2 (from Shot 2) aligns with the "
                        "10.12-second mark of the target video.")
    assert lines[1] == ""
    assert "converge exactly onto the arrangement established by <Picture 2>" in p
    # the I2VA instruction must NOT also appear
    assert "is fully referenced" not in p
    assert '"' not in p


def test_h3_i2va_prompt_never_mentions_a_last_picture():
    """Default stays I2VA: promising <Picture 2> the node never receives is the
    exact bug the 2026-08-06 audit found."""
    p = director.h3_clip_prompt({"name": "Windy"}, "a study", _BEATS2, [6.0, 6.0],
                                "Crayon.")
    assert "<Picture 2>" not in p
    assert "aligns with the" not in p


def test_h3_fl2va_graph_passes_and_resizes_the_last_frame(tmp_path, monkeypatch):
    """The node stretches first_frame but cover-CROPS last_frame, and klein
    renders 1280x704 (1.818) against a 960x544 (1.765) canvas — an unresized
    pair arrives framed ~3% apart and the model renders a phantom zoom. The
    last frame must be resized to the canvas so the crop is a no-op."""
    from briefing import h3 as h3mod
    sent, resized = {}, {}
    monkeypatch.setattr(h3mod, "_resize_image",
                        lambda src, dst, size: resized.update(size=size, dst=str(dst)))
    monkeypatch.setattr(h3mod.shutil, "copy", lambda a, b: None)
    monkeypatch.setattr(h3mod, "_queue_and_wait",
                        lambda cfg, g, prefix, dest, log: sent.update(g) or True)
    assert h3mod.i2v({"h3": {"dir": str(tmp_path)}}, "p", tmp_path/"a.png", 243, 7,
                     tmp_path/"o.mp4", last_frame=tmp_path/"b.png")
    assert sent["cond"]["inputs"]["last_frame"] == ["img2", 0]
    assert sent["img2"]["class_type"] == "LoadImage"
    assert resized["size"] == (960, 544)          # exactly the H3 canvas


def test_h3_i2va_graph_still_has_no_last_frame(tmp_path, monkeypatch):
    g = _h3_graph(tmp_path, monkeypatch, {"h3": {"dir": str(tmp_path)}})
    assert "img2" not in g
    assert "last_frame" not in g["cond"]["inputs"]


# --- the zh track: one bad line must not cost the whole 中文 variant ----------
# 2026-08-06, opening-hour deck: line 7/16 failed and took all seven slides with
# it. Cause was NOT a cloud outage — the ollama journal shows every attempt
# returning 200 in ~1-2s. deepseek's cloud path returned the finished Chinese
# sentence as BARE TEXT instead of {"lines": [...]} on 12 of 32 measured calls,
# and the misses are content-correlated, so the retry ladder could not recover.

_ZH_CFG = {"director": {"model": "deepseek-v4.1-flash:cloud"}, "zh": {}}


def test_zh_salvage_accepts_a_bare_chinese_sentence(monkeypatch):
    """The measured cloud failure: a correct translation, no JSON envelope."""
    _Ollama(monkeypatch, ["英伟达涨了百分之一点三，报二百二十二美元。"])
    assert director.translate_lines(["Nvidia is up 1.3% at $222."], _ZH_CFG) == \
        ["英伟达涨了百分之一点三，报二百二十二美元。"]


def test_zh_salvage_refuses_a_botched_json_object(monkeypatch):
    """A wrong-keyed object is a schema miss, not a bare answer — it must still
    go back for the corrective retry rather than be guessed at."""
    assert director._zh_salvage('{"line": "英伟达涨了。"}') is None
    assert director._zh_salvage("Nvidia rose 1.3 percent.") is None    # no CJK
    assert director._zh_salvage("第一句。\n第二句。") is None            # not one line


def test_zh_salvage_still_lets_a_proper_envelope_win(monkeypatch):
    ol = _Ollama(monkeypatch, [{"lines": ["英伟达涨了百分之一点三。"]}])
    assert director.translate_lines(["Nvidia is up 1.3%."], _ZH_CFG) == \
        ["英伟达涨了百分之一点三。"]
    assert len(ol.requests) == 1


def _nosleep(monkeypatch):
    """translate_lines backs off 4s then 8s per failing line; the tests below
    exercise that ladder deliberately and must not pay for it."""
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *_: None)


def test_one_dead_line_no_longer_kills_the_other_lines(monkeypatch):
    """The 2026-08-06 regression, pinned. Line 2 exhausts all 6 shots (3 attempts
    x _chat_json's own retry); lines 1 and 3 must survive as themselves."""
    _nosleep(monkeypatch)
    _Ollama(monkeypatch, [{"lines": ["第一句。"]}]
                         + ["nope, still English"] * 6
                         + [{"lines": ["第三句。"]}])
    out = director.translate_lines(["one", "two", "three"], _ZH_CFG)
    assert out == ["第一句。", None, "第三句。"]


def test_translate_lines_returns_none_only_when_nothing_landed(monkeypatch):
    _nosleep(monkeypatch)
    _Ollama(monkeypatch, [OSError("down")] * 12)
    assert director.translate_lines(["one", "two"], _ZH_CFG) is None


def test_the_guard_rejection_reports_why(monkeypatch):
    """A long digit run is a TTS problem, not a translation problem. The reason
    must reach the caller instead of being swallowed into 'failed to translate'."""
    _Ollama(monkeypatch, [{"lines": ["纳斯达克收在23145点。"]}])
    fix, why = director._translate_one("Nasdaq closed at 23,145.", "sys", {},
                                       _ZH_CFG["director"])
    assert fix is None and why.startswith("guard rejected")


def test_a_deck_missing_one_slide_still_ships_in_chinese():
    from briefing import story
    assert story._zh_enough(6, 7)          # one slide lost -> ship it
    assert not story._zh_enough(2, 7)      # most of the deck lost -> English-only
    assert not story._zh_enough(1, 1)      # never ship a one-slide "briefing"

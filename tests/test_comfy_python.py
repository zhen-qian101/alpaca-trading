"""The comfy env's python is found the same way everywhere it is spawned.

generate.py (the stylist), host_briefing._mouth_alive (the lip-sync gate) and
comfy.ensure_server all run code that needs cv2 in the comfy conda env, and
agent/run_stylist.sh does the same from bash. None of them may carry one
machine's home directory.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from briefing import comfy

ROOT = Path(__file__).resolve().parent.parent


def test_default_is_the_comfy_env_under_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("COMFY_PYTHON", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert comfy.comfy_python() == str(
        tmp_path / "anaconda3" / "envs" / "comfy" / "bin" / "python")


def test_comfy_python_overrides_the_default(monkeypatch):
    monkeypatch.setenv("COMFY_PYTHON", "/opt/envs/comfy/bin/python")
    assert comfy.comfy_python() == "/opt/envs/comfy/bin/python"


def test_run_stylist_resolves_the_same_default():
    """The bash copy of the default must not drift from comfy_python()."""
    sh = (ROOT / "agent" / "run_stylist.sh").read_text()
    m = re.search(r'"\$\{COMFY_PYTHON:-\$HOME/([^}]+)\}" -u -m "\$MODULE"', sh)
    assert m, "run_stylist.sh no longer launches the stylist via $COMFY_PYTHON"
    assert m.group(1) == "anaconda3/envs/comfy/bin/python"


def test_the_server_python_in_config_expands_the_home_directory(monkeypatch, tmp_path):
    """config.json's comfyui.python is written with ~, so ensure_server must
    expand it before checking that it exists."""
    monkeypatch.setenv("HOME", str(tmp_path))
    py = tmp_path / "anaconda3" / "envs" / "comfy" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        raise RuntimeError("stop here")

    comfy_dir = tmp_path / "ComfyUI"
    comfy_dir.mkdir()
    monkeypatch.setattr(comfy, "_server_up", lambda c: False)
    monkeypatch.setattr(comfy, "_models_present", lambda c: True)
    monkeypatch.setattr(comfy.subprocess, "Popen", fake_popen)
    cfg = {"comfyui": {"dir": str(comfy_dir),
                       "python": "~/anaconda3/envs/comfy/bin/python"}}
    with pytest.raises(RuntimeError, match="stop here"):
        comfy.ensure_server(cfg, verbose=False)
    assert seen["cmd"][0] == str(py)

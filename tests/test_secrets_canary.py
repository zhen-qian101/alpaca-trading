"""No real secret from .env may appear in any file git tracks.

The repo has a GitHub remote, and a credential committed once lives in history
for good. This already happened: tests/test_restart_gateway.py once spelled out
the IBKR login as literals. Generic secret scanners match on shape and missed
it, because a username and a short password look like ordinary strings.

So this test compares the EXACT values on this machine. It reads .env and its
dated backups, then searches every tracked file for each secret-like value. It
skips where there is no .env (CI, a fresh clone), because then there is nothing
to compare against. Failure messages name only the variable and the file, never
the value.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASS|USERID|CREDENTIAL)", re.I)
_MIN_LEN = 8          # shorter values match too much ordinary text to be useful
_MAX_BYTES = 8 << 20  # skip big media; a pasted secret lives in text files


def _env_secrets() -> dict[str, str]:
    found: dict[str, str] = {}
    for env in sorted(ROOT.glob(".env*")):
        if env.name == ".env.example" or not env.is_file():
            continue
        for line in env.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.removeprefix("export ").strip()
            value = value.strip().strip("'\"")
            if _SECRET_NAME.search(key) and len(value) >= _MIN_LEN:
                found[f"{env.name}:{key}"] = value
    return found


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                             capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.decode().split("\0") if p]


def test_no_env_secret_is_tracked():
    secrets = _env_secrets()
    if not secrets:
        pytest.skip("no .env with secrets in this checkout (CI / fresh clone)")
    needles = {name: value.encode() for name, value in secrets.items()}
    hits: list[str] = []
    for path in _tracked_files():
        try:
            if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                continue
            blob = path.read_bytes()
        except OSError:
            continue
        for name, needle in needles.items():
            if needle in blob:
                hits.append(f"{name} in {path.relative_to(ROOT)}")
    assert not hits, f"real secret value(s) tracked by git: {hits}"

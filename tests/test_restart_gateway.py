"""deploy/restart_gateway.sh — the Gateway restart lever.

THE BUG THESE EXIST FOR. The script hunts for the running IBC chain with
`pgrep -f`, then kills its process group. The hunt pattern said `xvfb-run -a`;
the launch, further down the SAME FILE, had moved to `xvfb-run -n <display>`
(stale Xvfb servers squat :99-:109, so -a hands out a squatted one). Nothing
connected the two, so they drifted.

The consequence was not a no-op. From its second run onward the script could not
find the chain it had itself started: it announced "no running gateway chain
found, just launching" and started a SECOND Gateway beside the wedged one — two
java processes sharing one TWS_SETTINGS_PATH and racing for port 4002. Observed
live 2026-08-20 (:130 and :131 both up) and untangled by hand.

So the first test here derives BOTH halves from the file and asserts they still
agree. A shell script cannot assert that about itself; this is the only place
the invariant can live.

The process-level tests aim the stop logic at throwaway `sleep` processes via
IBC_MATCH, never at the real Gateway — a test that could kill the live Gateway
would be worse than the bug.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "restart_gateway.sh"
SRC = SCRIPT.read_text()

#: Real command lines, copied from `ps` on the live box, with the home
#: directory shown as /home/op. The first is what the current launch produces;
#: the second is what the ORIGINAL -a launch produced, and chains started by the
#: old script can still be running.
LAUNCHED_N = (
    "bash -c xvfb-run -n 130 env   IBC_PATH='/home/op/IBC'   "
    "TWS_PATH='/home/op/Jts'   TWS_MAJOR_VRSN='1045'   "
    "'/home/op/IBC/scripts/displaybannerandlaunch.sh'"
)
LAUNCHED_A = (
    "bash -c xvfb-run -a env   IBC_PATH='/home/op/IBC'   "
    "TWS_PATH='/home/op/Jts'   APP='GATEWAY'"
)
XVFB_CHILD = (
    "/bin/sh /usr/bin/xvfb-run -n 130 env IBC_PATH=/home/op/IBC "
    "TWS_PATH=/home/op/Jts APP=GATEWAY"
)


def _pattern() -> str:
    """The IBC_MATCH default, as the script defines it."""
    m = re.search(r'IBC_MATCH="\$\{IBC_MATCH:-(.+?)\}"', SRC)
    assert m, "could not find the IBC_MATCH default in the script"
    return m.group(1)


def _launch_flag() -> str:
    """The xvfb-run flag the script actually launches with."""
    m = re.search(r'setsid bash -c "xvfb-run (-\w+)', SRC)
    assert m, "could not find the xvfb-run launch in the script"
    return m.group(1)


# ------------------------------------------------------- the drift invariant --
def test_the_detection_pattern_matches_this_script_s_own_launch():
    """THE regression. Both halves come from the same file, so if the launch
    flag is ever changed again without the pattern, this fails immediately
    instead of silently starting a second Gateway weeks later."""
    flag = _launch_flag()                       # e.g. "-n"
    rendered = LAUNCHED_N.replace("xvfb-run -n", f"xvfb-run {flag}")

    assert re.search(_pattern(), rendered), (
        f"the script launches with 'xvfb-run {flag}' but its detection pattern "
        f"{_pattern()!r} does not match that command line — this is exactly the "
        "drift that started a second Gateway on 2026-08-20")


def test_a_chain_started_by_the_OLD_script_is_still_found():
    """The -a form has to keep matching: a chain launched by the previous script
    may still be running when this one is used to clear it."""
    assert re.search(_pattern(), LAUNCHED_A)


def test_every_process_in_a_chain_matches_not_just_the_wrapper():
    """A chain is several processes; the group is derived from whichever are
    found, so the xvfb-run child must match too."""
    assert re.search(_pattern(), XVFB_CHILD)


@pytest.mark.parametrize("cmdline", [
    "bash /home/op/alpaca/deploy/restart_gateway.sh",
    "-bash",
    "python -m sigengine.engine --log-level INFO",
    "tail -f /mnt/sandisk/alpaca/logs/ibc/gateway.log",
])
def test_unrelated_processes_are_not_matched(cmdline):
    """pgrep -f matches command lines, and this pattern is used to pick targets
    for `kill -TERM -<pgid>`. The script's own invocation must not match."""
    assert not re.search(_pattern(), cmdline), cmdline


# -------------------------------------------------------------- the secrets --
def _dotenv_values(*names: str) -> dict[str, str]:
    """The named values from this checkout's untracked .env, or {} without one.

    Read at test time so that the real credentials are compared against, never
    written down: an earlier version of the test below spelled them out as
    literals, which put the very password it guards into git.
    """
    env = ROOT / ".env"
    if not env.exists():
        return {}
    out: dict[str, str] = {}
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if key in names:
            out[key] = value.strip().strip("'\"")
    return out


def test_no_credentials_are_hardcoded():
    """The copy this replaced carried the paper password in clear text. This one
    lives in git, so the credentials must come from .env and nowhere else."""
    assert re.search(r"TWSUSERID='\$\{TWS_USERID\}'", SRC), \
        "credentials must come from .env, as deploy/run_ibgateway.sh does"
    assert re.search(r"TWSPASSWORD='\$\{TWS_PASSWORD\}'", SRC), \
        "credentials must come from .env, as deploy/run_ibgateway.sh does"
    literal = [m.group(0).split("=", 1)[0]
               for m in re.finditer(r"TWS(?:USERID|PASSWORD)=(['\"]?)([^'\"\s\\]+)\1", SRC)
               if not m.group(2).startswith("${")]
    assert not literal, f"literal credential assignment(s) in {SCRIPT.name}: {literal}"
    # Names only in the failure message: the values themselves must never reach
    # pytest's assertion output.
    leaked = [k for k, v in _dotenv_values("TWS_USERID", "TWS_PASSWORD").items()
              if v and v in SRC]
    assert not leaked, f"{leaked} from .env appear(s) in {SCRIPT.name}"


def test_it_refuses_to_launch_without_credentials(tmp_path):
    """A missing .env must be a clear error, not a Gateway that silently fails
    to log in and leaves the API port shut.

    THE SCRIPT IS COPIED INTO A THROWAWAY TREE, and that is not incidental. The
    first version of this test ran the REAL deploy/restart_gateway.sh with
    cwd=tmp_path and blanked the two credential variables. But the script
    deliberately cds to its own repo root (readlink -f "$0") so it can find .env
    from anywhere — so it found the REAL .env, the credentials were present, the
    check it was supposed to trip never fired, and the script ran on to LAUNCH A
    GATEWAY. It exited immediately and left nothing behind, but "the test suite
    starts a second Gateway" is precisely the failure this whole file exists to
    prevent, and it slipped through because the worktree it was written in had
    no .env while main does.

    So the script gets its own root here: `.. ` from the copy is tmp_path, which
    has no .env. IBC_PATH and LOG_PATH are redirected too, so even a regression
    that got past the credential check could not touch the real Gateway.
    """
    root = tmp_path / "deploy"
    root.mkdir()
    copy = root / SCRIPT.name
    copy.write_text(SRC)
    copy.chmod(0o755)
    assert not (tmp_path / ".env").exists()

    env = {**os.environ,
           "IBC_MATCH": "restart-gateway-test-nothing-matches-this",
           "TWS_USERID": "", "TWS_PASSWORD": "",
           "IBC_PATH": str(tmp_path / "fake-ibc"),
           "LOG_PATH": str(tmp_path / "fake-logs"),
           "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(copy)], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)

    assert r.returncode == 1, f"expected the credential guard to fire: {r.stdout}"
    assert "TWS_USERID" in r.stderr
    assert "relaunching" not in r.stdout, "the test launched a Gateway"


def test_no_test_in_this_file_can_reach_the_launch():
    """The structural guard behind the docstring above. Every subprocess run
    here must either pass a mode flag that exits before the launch, or use the
    copied-tree fixture. A future test that calls the script bare, in the repo,
    would launch a real Gateway on a developer's box."""
    body = Path(__file__).read_text()
    # every invocation of the real SCRIPT path must carry a mode flag
    bare = re.findall(r"subprocess\.run\(\[\"bash\", str\(SCRIPT\)\](?![^)]*--)",
                      body)
    assert not bare, "a test runs the real script with no mode flag — it will launch"


# -------------------------------------------------------- the stop mechanics --
@pytest.fixture
def fakes():
    """Two throwaway chains, each in its OWN process group, matching a marker
    that cannot collide with the real Gateway."""
    marker = "restart-gateway-pytest-marker"
    procs = [subprocess.Popen(["bash", "-c", f"exec -a '{marker} chain{i}' sleep 120"],
                              start_new_session=True) for i in (1, 2)]
    time.sleep(0.5)
    yield marker, procs
    for p in procs:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        p.wait(timeout=5)


def _run(marker, *args, timeout=60):
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT,
                          env={**os.environ, "IBC_MATCH": marker},
                          capture_output=True, text=True, timeout=timeout)


def test_detect_only_reports_without_killing(fakes):
    """--detect-only must be safe to run on a live box while diagnosing."""
    marker, procs = fakes
    r = _run(marker, "--detect-only")

    assert "found" in r.stdout and "chain(s)" in r.stdout
    assert all(p.poll() is None for p in procs), "--detect-only killed something"


def test_stop_only_kills_EVERY_chain_not_just_the_first(fakes):
    """The old script took `| head -1`, so a box left with two chains — which is
    what the drift bug produced — kept one alive after a 'restart'."""
    marker, procs = fakes
    r = _run(marker, "--stop-only")

    time.sleep(1)
    assert all(p.poll() is not None for p in procs), (
        f"a chain survived --stop-only: {r.stdout}")
    assert "gateway processes still running after stop: 0" in r.stdout


def test_stop_only_does_not_relaunch(fakes):
    marker, _ = fakes
    r = _run(marker, "--stop-only")
    assert "relaunching" not in r.stdout


def test_nothing_running_is_not_an_error():
    r = _run("restart-gateway-test-nothing-matches-this", "--detect-only")
    assert r.returncode == 0
    assert "no running gateway chain found" in r.stdout


def test_an_unknown_flag_is_rejected():
    """A typo must not fall through to killing and relaunching the Gateway."""
    r = _run("restart-gateway-test-nothing-matches-this", "--dry-run")
    assert r.returncode == 2
    assert "usage" in r.stderr


def test_it_still_finds_the_repo_when_run_through_a_symlink(tmp_path):
    """~/IBC/restart_gateway.sh is a symlink to this file — that path
    is in the runbooks and in a year of notes. A plain `dirname "$0"` resolves
    to the LINK's directory, so the script would cd to the wrong place and fail
    its credential check on a perfectly configured box."""
    link = tmp_path / "restart_gateway.sh"
    link.symlink_to(SCRIPT)
    r = subprocess.run(["bash", str(link), "--detect-only"], cwd=tmp_path,
                       env={**os.environ,
                            "IBC_MATCH": "restart-gateway-test-nothing-matches"},
                       capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stderr
    assert "no running gateway chain found" in r.stdout

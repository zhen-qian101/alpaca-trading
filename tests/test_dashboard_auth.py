"""The dashboard's token gate as an outsider sees it, and the token's own safety.

Three properties, each a fix with a concrete failure behind it:

  1. A request without a valid token learns nothing about this server. The 401
     is a bare "401 Unauthorized" on every verb, with no Server header, no
     WWW-Authenticate realm and no HTML page. Internet scanners index response
     bodies and headers, so any distinctive text there makes every host running
     this server searchable.
  2. A token containing non-ASCII characters is refused with a 401. With two
     str arguments hmac.compare_digest raises TypeError on non-ASCII, and every
     channel (a %-escaped query value, a header or cookie decoded as latin-1)
     can carry one; the TypeError dropped the connection with a traceback.
  3. The token never appears in startup output. deploy/run_dashboard.sh tees
     that output into a log other accounts can read. A token generated at
     startup goes to a file created with mode 0600, and only its path is shown.

    .venv/bin/python -m pytest -q tests/test_dashboard_auth.py
"""
from __future__ import annotations

import re
import socket
import stat
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from web import server as web_server

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "s3cret-Tok_en"


@pytest.fixture
def raw(monkeypatch):
    """Serve the real Handler on loopback and speak raw HTTP to it.

    Raw bytes on purpose: urllib re-encodes and validates headers on the way
    out, and the point of several tests is exactly what bytes arrive.
    """
    monkeypatch.setattr(web_server, "build_portfolio", lambda: {"equity": 1.0})
    monkeypatch.setattr(web_server, "_CACHE", web_server._Cache())
    monkeypatch.setattr(web_server.Handler, "token", TOKEN)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def exchange(request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(request)
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
        data = b"".join(chunks)
        # An empty reply is what the old crash looked like from the client.
        assert data, "server dropped the connection without a response"
        return data

    def send(request: bytes) -> tuple[int, dict, bytes]:
        data = exchange(request)
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        return status, headers, body

    def req(method: str, target: str, *headers: bytes, body: bytes = b"") -> bytes:
        lines = [f"{method} {target} HTTP/1.1".encode(), b"Host: 127.0.0.1",
                 b"Connection: close", *headers]
        if body:
            lines.append(f"Content-Length: {len(body)}".encode())
        return b"\r\n".join(lines) + b"\r\n\r\n" + body

    send.exchange = exchange          # raw bytes, for replies with no status line
    yield send, req
    httpd.shutdown()
    httpd.server_close()


# --- 1. nothing distinctive for the unauthenticated ----------------------
DISTINCTIVE = ("append", "token", "dashboard", "alpaca", "python", "basehttp",
               "<title", "<html", "error response")


def _assert_bland(headers: dict, body: bytes):
    assert "server" not in headers, headers.get("server")
    assert "www-authenticate" not in headers
    text = body.decode("latin-1").lower()
    for word in DISTINCTIVE:
        assert word not in text, f"{word!r} in {text!r}"
    for value in headers.values():
        for word in DISTINCTIVE:
            assert word not in value.lower(), f"{word!r} in header {value!r}"


@pytest.mark.parametrize("target", ["/", "/api/portfolio", "/index.html",
                                    "/no/such/page", "/api/portfolio?token=nope"])
def test_get_without_a_token_is_a_bare_401(raw, target):
    send, req = raw
    status, headers, body = send(req("GET", target))
    assert status == 401
    assert body == b"401 Unauthorized\n"
    assert headers["content-type"].startswith("text/plain")
    _assert_bland(headers, body)


def test_head_and_post_without_a_token_say_the_same(raw):
    send, req = raw
    status, headers, body = send(req("HEAD", "/"))
    assert status == 401 and body == b""
    _assert_bland(headers, body)
    status, headers, body = send(req("POST", "/api/look/approve",
                                     b"Content-Type: application/json", body=b"{}"))
    assert status == 401 and body == b"401 Unauthorized\n"
    _assert_bland(headers, body)


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
def test_the_stdlib_error_page_for_other_verbs_is_bland_too(raw, method):
    """Unsupported verbs are answered by the stdlib before the token check."""
    send, req = raw
    status, headers, body = send(req(method, "/"))
    assert status == 501
    assert headers["content-type"].startswith("text/plain")
    _assert_bland(headers, body)


def test_a_malformed_request_line_gets_a_bland_400(raw):
    send, _ = raw
    status, headers, body = send(b"GET / extra HTTP/1.1\r\n\r\n")
    assert status == 400
    _assert_bland(headers, body)


def test_an_unparseable_version_gets_a_bland_body(raw):
    """With no usable version the stdlib answers HTTP/0.9 style: the error
    body alone, no status line or headers. That body must be bland as well."""
    send, _ = raw
    data = send.exchange(b"GET / HTTP/9.9.9\r\n\r\n")
    assert data.startswith(b"400 ")
    for word in DISTINCTIVE:
        assert word.encode() not in data.lower(), word


def test_an_authorised_response_carries_no_server_header_either(raw):
    send, req = raw
    status, headers, _ = send(req("GET", f"/api/portfolio?token={TOKEN}"))
    assert status == 200
    assert "server" not in headers


# --- 2. non-ASCII tokens -------------------------------------------------
@pytest.mark.parametrize("channel", ["query", "header", "cookie"])
@pytest.mark.parametrize("value", ["sécret", "ÿ" * 13, TOKEN[:-1] + "é"])
def test_a_non_ascii_token_is_a_401_not_a_crash(raw, capfd, channel, value):
    send, req = raw
    raw_value = value.encode("latin-1")
    if channel == "query":
        # %-escaped UTF-8 in the URL; parse_qs hands back the non-ASCII str.
        from urllib.parse import quote
        request = req("GET", "/api/portfolio?token=" + quote(value))
    elif channel == "header":
        # The stdlib decodes header bytes as latin-1.
        request = req("GET", "/api/portfolio", b"X-Dashboard-Token: " + raw_value)
    else:
        request = req("GET", "/api/portfolio", b"Cookie: dash_token=" + raw_value)
    status, _, body = send(request)
    assert status == 401
    assert body == b"401 Unauthorized\n"
    assert "Traceback" not in capfd.readouterr().err


def test_non_ascii_never_reaches_compare_digest_as_str(monkeypatch):
    """The unit-level guarantee behind the test above, on every channel."""
    h = web_server.Handler.__new__(web_server.Handler)
    monkeypatch.setattr(web_server.Handler, "token", TOKEN)
    for headers in ({"X-Dashboard-Token": "é"}, {"Cookie": "dash_token=é"}, {}):
        h.headers = headers
        query = {} if headers else {"token": ["é"]}
        assert h._authorized(query) is False
    h.headers = {"X-Dashboard-Token": TOKEN}
    assert h._authorized({}) is True


# --- 3. the token stays out of startup output ----------------------------
class _NoServe:
    """Stands in for ThreadingHTTPServer: binds nothing, returns at once."""

    def __init__(self, *_a, **_k):
        pass

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


@pytest.fixture
def start(monkeypatch, tmp_path):
    """Run web.server.main() with the given argv/env; return (stdout, token)."""
    monkeypatch.setattr(web_server, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(web_server, "ThreadingHTTPServer", _NoServe)
    monkeypatch.setattr(web_server.Handler, "token", None)   # restored after
    token_file = tmp_path / "cfg" / "dashboard_token"
    monkeypatch.setenv("DASHBOARD_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)

    def run(capsys, *argv, env_token=None):
        if env_token is not None:
            monkeypatch.setenv("DASHBOARD_TOKEN", env_token)
        monkeypatch.setattr(sys, "argv", ["web.server", *argv])
        web_server.main()
        return capsys.readouterr(), web_server.Handler.token, token_file

    return run


def test_a_generated_token_is_never_printed(start, capsys):
    out, token, token_file = start(capsys, "--host", "0.0.0.0", "--port", "1")
    assert token and len(token) >= 24
    assert token not in out.out and token not in out.err
    assert "?token=" + token not in out.out
    # ...but the operator is told where it is.
    assert str(token_file) in out.out
    assert token_file.read_text().strip() == token


def test_the_generated_token_file_is_mode_0600(start, capsys):
    _, _, token_file = start(capsys, "--host", "0.0.0.0", "--port", "1")
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_a_looser_file_already_there_is_replaced_at_0600(start, capsys, tmp_path):
    target = tmp_path / "cfg" / "dashboard_token"
    target.parent.mkdir()
    target.write_text("old\n")
    target.chmod(0o644)
    _, token, _ = start(capsys, "--host", "0.0.0.0", "--port", "1")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text().strip() == token
    leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == [], leftovers


def test_a_symlink_at_the_token_path_is_not_written_through(start, capsys, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n")
    target = tmp_path / "cfg" / "dashboard_token"
    target.parent.mkdir()
    target.symlink_to(victim)
    _, token, _ = start(capsys, "--host", "0.0.0.0", "--port", "1")
    assert victim.read_text() == "untouched\n"
    assert not target.is_symlink() and target.read_text().strip() == token


def test_a_token_from_env_is_never_printed_and_no_file_is_written(start, capsys):
    out, token, token_file = start(capsys, "--host", "0.0.0.0", "--port", "1",
                                   env_token="from-env-Zq9")
    assert token == "from-env-Zq9"
    assert "from-env-Zq9" not in out.out and "from-env-Zq9" not in out.err
    assert not token_file.exists()


def test_a_loopback_bind_without_a_token_prints_none_and_writes_nothing(start, capsys):
    out, token, token_file = start(capsys, "--host", "127.0.0.1", "--port", "1")
    assert token is None
    assert not token_file.exists()
    assert re.search(r"^  token:\s+none", out.out, re.M)


def test_an_unwritable_token_file_stops_startup_without_the_token(start, capsys,
                                                                  monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("DASHBOARD_TOKEN_FILE", str(blocker / "dashboard_token"))
    seen = {}
    real = web_server.secrets.token_urlsafe

    def spy(n):
        seen["token"] = real(n)
        return seen["token"]

    monkeypatch.setattr(web_server.secrets, "token_urlsafe", spy)
    with pytest.raises(SystemExit) as exc:
        start(capsys, "--host", "0.0.0.0", "--port", "1")
    out = capsys.readouterr()
    assert seen["token"] not in str(exc.value)
    assert seen["token"] not in out.out + out.err
    assert "DASHBOARD_TOKEN" in str(exc.value)


# --- deploy/run_dashboard.sh reads back only the banner's token line ------
def test_run_dashboard_reads_back_the_token_source_line_main_prints(start, capsys):
    """The launcher shows the server's "  token:" banner line, and that line
    names where the token is, never the token. Tie the two together so a
    rename on either side is noticed."""
    script = (ROOT / "deploy" / "run_dashboard.sh").read_text()
    assert subprocess.run(["bash", "-n", str(ROOT / "deploy" / "run_dashboard.sh")]
                          ).returncode == 0
    assert 'grep -m1 "^  token:"' in script
    assert '"dashboard:"' not in script        # the old line carried ?token=<token>
    out, token, _ = start(capsys, "--host", "0.0.0.0", "--port", "1")
    lines = [ln for ln in out.out.splitlines() if ln.startswith("  token:")]
    assert len(lines) == 1 and token not in lines[0]

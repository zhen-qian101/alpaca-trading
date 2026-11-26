"""The dashboard must not dump a traceback when a browser disconnects mid-response.

The UI polls every few seconds and is routinely reloaded/closed while a response
is still being written, aborting the socket. Those raise BrokenPipeError /
ConnectionResetError out of the send path; the stdlib only catches socket
timeouts, so the rest reach socketserver.handle_error and print a full traceback
each (50 in dashboard.log over ~24h, all benign). A log full of routine
tracebacks is one a real error hides in — see Handler.handle_one_request.

    .venv/bin/pytest -q tests/test_web_broken_pipe.py
"""
from __future__ import annotations

import os

os.environ.setdefault("ALPACA_KEY", "PKtest")
os.environ.setdefault("ALPACA_SECRET", "secrettest")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret")
os.environ.setdefault("PAPER_TRADING", "true")

import pytest  # noqa: E402

from web.server import Handler  # noqa: E402


class _Stub(Handler):
    """Bypasses __init__ (which would want a live socket) so handle_one_request
    can be driven directly."""

    def __init__(self, boom):
        self._boom = boom
        self.close_connection = False
        self.calls = 0

    def _parent(self):
        self.calls += 1
        if self._boom:
            raise self._boom


@pytest.fixture
def patched(monkeypatch):
    """Point super().handle_one_request() at the stub's _parent."""
    import http.server as hs
    monkeypatch.setattr(hs.BaseHTTPRequestHandler, "handle_one_request",
                        lambda self: self._parent())
    yield


@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"),
                                 ConnectionResetError(104, "Connection reset by peer")])
def test_client_disconnect_is_swallowed_not_raised(patched, exc):
    """The client is already gone — there is nothing to recover, and nothing
    worth a traceback."""
    h = _Stub(exc)
    h.handle_one_request()          # must not raise
    assert h.close_connection is True


def test_normal_request_is_untouched(patched):
    h = _Stub(None)
    h.handle_one_request()
    assert h.calls == 1
    assert h.close_connection is False


def test_real_errors_still_propagate(patched):
    """Only disconnect races are swallowed. A genuine fault must still surface
    as a traceback — that is the whole point of quieting the benign ones."""
    h = _Stub(ValueError("a real bug"))
    with pytest.raises(ValueError, match="a real bug"):
        h.handle_one_request()

"""Regression coverage for sigengine.ibkr's connection lifecycle (2026-07-06,
iterated twice the same day):

1. `mark_suspect()`: connect() originally only checked isConnected() (the TCP
   socket) before reusing a connection — never accounting for a PREVIOUSLY
   abandoned call possibly leaving the connection's higher-level request-
   tracking state confused even though the socket itself is still fine.

2. LATER THE SAME DAY, superseding #1's premise: connect() now NEVER reuses a
   connection at all, even a healthy one — every call disconnects any
   existing connection for that symbol and creates a genuinely fresh one.
   This was forced by a confirmed live pattern: every connection's FIRST-ever
   use succeeded reliably, but the SAME connection's reuse ~208s later (one
   poll cycle) hung uniformly, every restart, every time — while fresh
   connections (isolated tests, and this fix) succeeded consistently. See
   connect()'s own docstring for the full research/reasoning (IBKR pacing
   violations and other server-side session staleness are documented to
   cause silent hangs with no client-visible error).

Since the same-day per-symbol rewrite (see sigengine.ibkr's module
docstring), connections/suspect-flags/RequestTimeout are all keyed by symbol
(_CONNECTIONS dict) rather than a single shared _IB global.
"""
from __future__ import annotations

import sigengine.ibkr as ibkr_mod


class FakeIB:
    def __init__(self, label="new"):
        self.label = label
        self.connected = True
        self.disconnect_called = False
        self.RequestTimeout = 0

    def isConnected(self):
        return self.connected

    def connect(self, host, port, clientId, timeout, readonly=False):
        pass

    def reqMarketDataType(self, t):
        pass

    def disconnect(self):
        self.disconnect_called = True
        self.connected = False


def test_connect_always_disconnects_and_replaces_even_a_healthy_connection(monkeypatch):
    """The 2026-07-06 "always fresh" fix: even a connection that's perfectly
    healthy (isConnected() True) must be torn down and replaced on every
    connect() call — never reused, since reuse was the confirmed trigger for
    silent hangs on a connection's second use."""
    monkeypatch.setattr(ibkr_mod, "IB", FakeIB)
    monkeypatch.setattr(ibkr_mod, "_SUSPECT", {})
    existing = FakeIB("original")
    monkeypatch.setattr(ibkr_mod, "_CONNECTIONS", {"AAPL": existing})

    result = ibkr_mod.connect("AAPL")

    assert result is not existing
    assert existing.disconnect_called


def test_connect_does_not_affect_a_different_symbols_connection(monkeypatch):
    monkeypatch.setattr(ibkr_mod, "IB", FakeIB)
    monkeypatch.setattr(ibkr_mod, "_SUSPECT", {})
    aapl_conn = FakeIB("aapl")
    monkeypatch.setattr(ibkr_mod, "_CONNECTIONS", {"AAPL": aapl_conn})

    msft_conn = ibkr_mod.connect("MSFT")

    assert msft_conn is not aapl_conn
    assert not aapl_conn.disconnect_called
    assert ibkr_mod._CONNECTIONS["AAPL"] is aapl_conn


def test_mark_suspect_sets_flag_for_that_symbol_only(monkeypatch):
    monkeypatch.setattr(ibkr_mod, "_SUSPECT", {})
    ibkr_mod.mark_suspect("AAPL")
    assert ibkr_mod._SUSPECT.get("AAPL") is True
    assert ibkr_mod._SUSPECT.get("MSFT") is not True


def test_connect_forces_fresh_reconnect_when_suspect(monkeypatch):
    monkeypatch.setattr(ibkr_mod, "IB", FakeIB)
    existing = FakeIB("original")
    monkeypatch.setattr(ibkr_mod, "_CONNECTIONS", {"AAPL": existing})
    monkeypatch.setattr(ibkr_mod, "_SUSPECT", {"AAPL": True})

    result = ibkr_mod.connect("AAPL")

    # The old (suspect) connection must have been disconnected, and a
    # DIFFERENT (freshly created) IB object returned instead of blindly
    # reusing the one flagged suspect.
    assert existing.disconnect_called
    assert result is not existing
    assert isinstance(result, FakeIB)
    # The suspect flag must be cleared so subsequent calls reuse the new
    # connection normally instead of forcing a reconnect every time.
    assert ibkr_mod._SUSPECT.get("AAPL") is not True


def test_connect_sets_request_timeout_on_fresh_connection(monkeypatch):
    """The actual root-cause fix for qualifyContracts/reqHistoricalData
    hanging forever (2026-07-06): ib_async ships IB.RequestTimeout=0 (wait
    indefinitely) by default for every synchronous call. connect() must set
    a real positive value so a hang becomes a clean asyncio.TimeoutError
    instead of blocking forever — see connect()'s own docstring for the full
    investigation (ruled out ReadOnlyApi, confirmed via ib_async's own
    IB.RequestTimeout docstring and community reports)."""
    monkeypatch.setattr(ibkr_mod, "IB", FakeIB)
    monkeypatch.setattr(ibkr_mod, "_CONNECTIONS", {})
    monkeypatch.setattr(ibkr_mod, "_SUSPECT", {})

    result = ibkr_mod.connect("AAPL")

    assert result.RequestTimeout == 12.0


def test_client_id_for_assigns_distinct_stable_ids(monkeypatch):
    monkeypatch.setattr(ibkr_mod, "_SYMBOL_CLIENT_IDS", {})
    monkeypatch.setenv("IBKR_CLIENT_ID", "1")

    first = ibkr_mod._client_id_for("AAPL")
    second = ibkr_mod._client_id_for("MSFT")
    again = ibkr_mod._client_id_for("AAPL")

    assert first != second
    assert again == first  # stable across repeated calls for the same symbol

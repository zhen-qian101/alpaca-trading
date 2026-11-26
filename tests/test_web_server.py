"""Tests for web/server.py — the read-only dashboard's auth gate and safety rails.

This server can be bound to a non-loopback address to reach the dashboard from
another machine, which puts a live brokerage account one HTTP request away. The
two properties that matter are locked here:

  1. The token gate never lets an unauthenticated request through, and — the
     regression these tests exist for — it *fails cleanly* rather than crashing
     when no credential is supplied at all. The original `_authorized` read
     `.value` off `SimpleCookie.get("dash_token", SimpleCookie())`; a missing
     cookie returned the default SimpleCookie (a dict, with no `.value`) and
     raised AttributeError out of the handler, so a browser hitting the URL with
     no token got a dropped connection instead of a 401. A *wrong* token
     short-circuited the `or` chain before that line, so the bug hid behind the
     one case that looked tested.
  2. The dashboard cannot trade. It never imports relay.order_manager and has
     no route that reaches the broker, so a compromised or misused page still
     cannot place, size, or cancel an order.

     This used to be stated as "there are no write routes", which stopped being
     literally true on 2026-08-20: POST /api/look/approve signs off the
     presenter's daily outfit. The trading property is unchanged — that route's
     entire reach is one boolean in one cosmetic JSON file — but the weaker,
     more honest statement is what is tested now, plus a test that POST answers
     404 everywhere else so it cannot grow into a general write surface.

    .venv/bin/python -m pytest -q tests/test_web_server.py

(Run via `python -m pytest`, as the rest of this suite is: the project is not
installed into the venv, so it is `-m`'s cwd-on-sys.path that makes `web`,
`relay` et al importable. Bare `.venv/bin/pytest` fails to collect these — and
the existing tests — for the same reason.)
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from web import server as web_server


@pytest.fixture
def serve(monkeypatch):
    """Run the real Handler on an ephemeral port with the panels stubbed out.

    The builders are stubbed so these tests exercise routing/auth without
    touching Alpaca or the bar cache — the auth gate runs before any builder,
    which is exactly the layer under test.
    """
    monkeypatch.setattr(web_server, "build_portfolio", lambda: {"equity": 1.0})
    monkeypatch.setattr(web_server, "build_board", lambda: {"rows": []})
    monkeypatch.setattr(web_server, "build_journal", lambda: {"rows": []})
    web_server._CACHE = web_server._Cache()  # don't serve another test's values

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path, headers=None, method=None, body=None):
        req = urllib.request.Request(base + path, headers=headers or {},
                                     data=body, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    yield get
    httpd.shutdown()
    httpd.server_close()


# --- the regression -------------------------------------------------------
def test_no_credential_at_all_gets_a_clean_401(serve, monkeypatch):
    """The crash case: token required, request carries no token anywhere.

    Must be a 401, not a dropped connection. urlopen surfaces the original bug
    as a RemoteDisconnected/AttributeError rather than an HTTPError.
    """
    monkeypatch.setattr(web_server.Handler, "token", "secret")
    status, body, _ = serve("/api/portfolio")
    assert status == 401
    # Deliberately generic: no hint at the ?token= channel (see
    # tests/test_dashboard_auth.py for why the body must not be distinctive).
    assert body == "401 Unauthorized\n"


def test_unrelated_cookie_present_but_no_dash_token(serve, monkeypatch):
    """A browser carrying some other cookie still has no dash_token morsel —
    the same missing-morsel path, reached with a non-empty Cookie header."""
    monkeypatch.setattr(web_server.Handler, "token", "secret")
    status, _, _ = serve("/api/portfolio", {"Cookie": "foo=bar; baz=qux"})
    assert status == 401


# --- the gate ------------------------------------------------------------
def test_wrong_token_is_rejected(serve, monkeypatch):
    monkeypatch.setattr(web_server.Handler, "token", "secret")
    assert serve("/api/portfolio?token=nope")[0] == 401


@pytest.mark.parametrize(
    "path,headers",
    [
        ("/api/portfolio?token=secret", None),                    # query
        ("/api/portfolio", {"Cookie": "dash_token=secret"}),      # cookie
        ("/api/portfolio", {"X-Dashboard-Token": "secret"}),      # header
    ],
)
def test_correct_token_is_accepted_by_every_channel(serve, monkeypatch, path, headers):
    monkeypatch.setattr(web_server.Handler, "token", "secret")
    status, body, _ = serve(path, headers)
    assert status == 200
    assert json.loads(body)["equity"] == 1.0


def test_query_token_is_parked_in_a_cookie(serve, monkeypatch):
    """So a plain bookmark keeps working after the first ?token= load."""
    monkeypatch.setattr(web_server.Handler, "token", "secret")
    _, _, headers = serve("/?token=secret")
    assert "dash_token=secret" in headers.get("Set-Cookie", "")


def test_no_token_configured_means_no_gate(serve, monkeypatch):
    """Loopback default: DASHBOARD_TOKEN unset, so the gate is off."""
    monkeypatch.setattr(web_server.Handler, "token", None)
    assert serve("/api/portfolio")[0] == 200


# --- resilience when the broker misbehaves --------------------------------
def test_a_hung_broker_call_does_not_block_the_file_backed_panels():
    """The outage that motivated this (2026-07-14): Alpaca stopped answering,
    and because the cache took ONE global lock around the producer, the hung
    /api/portfolio call also froze /api/board and /api/journal — panels that
    read local files and need no network at all. Per-key locks fix that.
    """
    cache = web_server._Cache()
    started = threading.Event()
    release = threading.Event()

    def hangs():
        started.set()
        release.wait(timeout=5)
        return {"slow": True}

    t = threading.Thread(target=lambda: cache.get("portfolio", 5.0, hangs), daemon=True)
    t.start()
    assert started.wait(timeout=2), "the slow producer never started"

    # While 'portfolio' is stuck, another key must still be served promptly.
    done = []
    t2 = threading.Thread(target=lambda: done.append(cache.get("board", 5.0, lambda: {"rows": []})),
                          daemon=True)
    t2.start()
    t2.join(timeout=2)
    assert done == [{"rows": []}], "a hung key blocked an unrelated key"

    release.set()
    t.join(timeout=5)


def test_failed_results_are_cached_longer_than_good_ones():
    """The backoff. Re-polling a timing-out broker every 10s per tab is what got
    the API key throttled: each attempt hung holding a connection, so the retries
    were themselves the outage."""
    cache = web_server._Cache()
    calls = []

    def failing():
        calls.append(1)
        return {"error": "ReadTimeout"}

    for _ in range(5):
        cache.get("portfolio", 0.01, failing)   # success TTL long expired each time
    assert len(calls) == 1, "a failing producer was retried instead of backed off"

    assert web_server._Cache._ttl_for({"error": "x"}, 5.0) == web_server.ERROR_TTL
    assert web_server._Cache._ttl_for({"equity": 1.0}, 5.0) == 5.0


def test_alpaca_calls_carry_a_timeout():
    """alpaca-py calls requests with no timeout, so a stalled connection hangs
    forever — the dashboard sat on an SSL read indefinitely. The adapter injects
    a default at the one place every call funnels through."""
    adapter = web_server._TimeoutAdapter()
    seen = {}

    class _Base:
        def send(self, request, **kwargs):
            seen.update(kwargs)
            return "sent"

    # Exercise the injection without opening a socket.
    adapter.__class__.__mro__  # sanity: it really is an HTTPAdapter subclass
    import requests.adapters
    assert isinstance(adapter, requests.adapters.HTTPAdapter)

    orig = requests.adapters.HTTPAdapter.send
    try:
        requests.adapters.HTTPAdapter.send = _Base.send
        adapter.send(object())
    finally:
        requests.adapters.HTTPAdapter.send = orig
    assert seen["timeout"] == web_server.ALPACA_TIMEOUT


# --- executions: entry/exit pairing --------------------------------------
def test_executions_pair_entries_to_exits_with_realized_pnl(monkeypatch):
    """The journal records the DECISION and never a price (its only keys are
    action/event/qty/reason/status/strategy/symbol/ticker/ts), and order_ok is
    written at submit time when a market order has no fill price yet. Prices
    therefore come from the broker's closed orders, paired here into round-trips.
    """
    class _Order:
        def __init__(self, sym, side, qty, price, when):
            self.symbol, self.filled_qty, self.filled_avg_price = sym, qty, price
            self.side, self.filled_at = side, when
            self.order_type = "market"

    base = dt.datetime(2026, 7, 7, 15, 30, tzinfo=dt.UTC)
    orders = [
        _Order("META", "buy", "8", "605.94", base),
        _Order("META", "sell", "8", "656", base + dt.timedelta(days=7)),
    ]

    class _FakeTrading:
        def get_orders(self, req):
            return orders

    monkeypatch.setattr(web_server, "_client", lambda: type("C", (), {"client": _FakeTrading()})())
    d = web_server.build_executions()

    assert d["round_trips"] == 1
    assert d["win_rate"] == 1.0
    exit_row = d["rows"][0]          # newest first
    assert exit_row["side"] == "sell"
    assert exit_row["entry_price"] == 605.94
    assert exit_row["exit_price"] == 656.0
    assert round(exit_row["pnl"], 2) == 400.48
    assert round(exit_row["pnl_pct"], 4) == round(656 / 605.94 - 1, 4)


def test_executions_ignore_orders_that_never_filled(monkeypatch):
    """Closed != filled: cancelled/expired orders come back on the same endpoint
    with filled_qty 0 and no price, and must not be reported as executions."""
    class _Order:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    orders = [
        _Order(symbol="AAPL", side="buy", filled_qty="0", filled_avg_price=None,
               filled_at=None, order_type="limit"),
        _Order(symbol="AAPL", side="buy", filled_qty="5", filled_avg_price="200",
               filled_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC), order_type="market"),
    ]

    class _FakeTrading:
        def get_orders(self, req):
            return orders

    monkeypatch.setattr(web_server, "_client", lambda: type("C", (), {"client": _FakeTrading()})())
    d = web_server.build_executions()
    assert len(d["rows"]) == 1
    assert d["rows"][0]["entry_price"] == 200.0


def test_executions_average_cost_across_a_scaled_entry(monkeypatch):
    """Two buys then one sell: the exit must report the BLENDED entry, matching
    how the strategy scales in and how Alpaca reports position cost."""
    class _Order:
        def __init__(self, sym, side, qty, price, when):
            self.symbol, self.filled_qty, self.filled_avg_price = sym, qty, price
            self.side, self.filled_at, self.order_type = side, when, "market"

    base = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    orders = [
        _Order("AAPL", "buy", "10", "100", base),
        _Order("AAPL", "buy", "10", "120", base + dt.timedelta(days=1)),
        _Order("AAPL", "sell", "20", "130", base + dt.timedelta(days=2)),
    ]

    class _FakeTrading:
        def get_orders(self, req):
            return orders

    monkeypatch.setattr(web_server, "_client", lambda: type("C", (), {"client": _FakeTrading()})())
    d = web_server.build_executions()
    sell = d["rows"][0]
    assert sell["entry_price"] == 110.0            # blended, not 100 or 120
    assert round(sell["pnl"], 2) == 400.0          # (130-110)*20
    assert sell["held_after"] == 0.0


# --- executions: every closed order, not the first 500 ----------------------
class _PagedTrading:
    """A trading client that pages like Alpaca's GET /v2/orders: at most
    `limit` (<= 500) orders, sorted by submission time in `direction`, only
    those submitted strictly before `until` / after `after`."""

    def __init__(self, orders, fail_on_call: int | None = None):
        self.orders = orders
        self.calls: list = []
        self.fail_on_call = fail_on_call

    def get_orders(self, req):
        self.calls.append(req)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise ConnectionError("orders page timed out")
        assert str(getattr(req.status, "value", req.status)) == "closed"
        assert req.limit is not None and req.limit <= 500
        direction = str(getattr(req.direction, "value", req.direction) or "desc")
        rows = sorted(self.orders, key=lambda o: o.submitted_at, reverse=direction == "desc")
        if req.until is not None:
            rows = [o for o in rows if o.submitted_at < req.until]
        if req.after is not None:
            rows = [o for o in rows if o.submitted_at > req.after]
        return rows[:req.limit]


def _paged_order(n, sym, side, qty, price, when, *, filled=True, submitted=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=f"00000000-0000-4000-8000-{n:012d}", symbol=sym, side=f"OrderSide.{side.upper()}",
        filled_qty=str(qty if filled else 0), filled_avg_price=str(price) if filled else None,
        filled_at=when if filled else None, submitted_at=submitted or when,
        created_at=submitted or when, order_type="OrderType.MARKET")


def _account_with_rejected_retries(trips: int, rejects_per_trip: int):
    """`trips` buy -> sell round trips in ACME, each followed by
    `rejects_per_trip` rejected retries (closed, never filled), like the
    reconcile spam that filled the order history on 2026-08-18."""
    base = dt.datetime(2026, 8, 28, 14, tzinfo=dt.UTC)
    orders, n = [], 0
    for i in range(trips):
        t = base + dt.timedelta(hours=6 * i)
        entry, exit_ = 100.0 + i, 101.0 + i if i % 4 else 99.0 + i     # every 4th trip loses
        orders.append(_paged_order(n := n + 1, "ACME", "buy", 10, entry, t))
        orders.append(_paged_order(n := n + 1, "ACME", "sell", 10, exit_, t + dt.timedelta(hours=1)))
        for k in range(rejects_per_trip):
            orders.append(_paged_order(n := n + 1, "ACME", "sell", 10, None,
                                       t + dt.timedelta(hours=1, minutes=1 + k), filled=False))
    return orders


def _use_trading(monkeypatch, trading):
    monkeypatch.setattr(web_server, "_client", lambda: type("C", (), {"client": trading})())


def test_executions_read_every_closed_order_not_the_oldest_500(monkeypatch):
    """P9: one get_orders(limit=500, direction=asc) call returned the account's
    OLDEST 500 closed orders. Rejected retries count toward the 500, so past
    that the blotter, the round-trip stats and the public exporter (which calls
    build_executions too) froze on old fills. Every page is read now."""
    orders = _account_with_rejected_retries(trips=100, rejects_per_trip=11)   # 1,300 orders
    trading = _PagedTrading(orders)
    _use_trading(monkeypatch, trading)

    d = web_server.build_executions(limit=1_000_000)

    assert "error" not in d
    assert d["round_trips"] == 100
    assert len(d["rows"]) == 200                     # every fill, no rejected retry
    newest_sell = max((o for o in orders if o.filled_at), key=lambda o: o.filled_at)
    assert d["rows"][0]["ts"] == newest_sell.filled_at.timestamp()   # newest first, and present
    assert d["rows"][0]["exit_price"] == float(newest_sell.filled_avg_price)
    assert d["win_rate"] == 0.75
    assert round(d["realized_pnl"], 6) == round(75 * 10.0 - 25 * 10.0, 6)
    assert len(trading.calls) == 3                   # 500 + 500 + 300
    assert all(str(getattr(c.direction, "value", c.direction)) == "desc" for c in trading.calls)
    assert trading.calls[0].until is None


def test_executions_keep_orders_that_share_a_time_across_a_page_boundary(monkeypatch):
    """`until` is exclusive. Four orders submitted at the same instant straddle
    the first page's end; paging from exactly that instant would lose the two
    that did not fit, one of them the BUY the newest sell closes against."""
    base = dt.datetime(2026, 9, 1, 14, tzinfo=dt.UTC)
    same = base + dt.timedelta(minutes=5)
    # The fake breaks ties in list order, so the buy is the one cut off.
    orders = [
        _paged_order(2, "NOVA", "sell", 5, None, same, filled=False),
        _paged_order(3, "NOVA", "sell", 5, None, same, filled=False),
        _paged_order(4, "NOVA", "sell", 5, None, same, filled=False),
        _paged_order(1, "NOVA", "buy", 5, 50.0, base + dt.timedelta(minutes=6), submitted=same),
    ]
    # 497 newer orders, the last of them the sell that closes the NOVA buy.
    for k in range(496):
        orders.append(_paged_order(10 + k, "ACME", "sell", 1, None,
                                   base + dt.timedelta(hours=1, seconds=k), filled=False))
    sell_at = base + dt.timedelta(hours=3)
    orders.append(_paged_order(9999, "NOVA", "sell", 5, 55.0, sell_at))
    # Older history, so the walk needs a second page.
    for k in range(30):
        orders.append(_paged_order(20000 + k, "ACME", "buy", 1, None,
                                   base - dt.timedelta(days=1, seconds=k), filled=False))
    trading = _PagedTrading(orders)
    first_page = trading.get_orders(type("R", (), {"status": "closed", "limit": 500,
                                                   "direction": "desc", "until": None,
                                                   "after": None})())
    trading.calls.clear()
    at_same = [o.id for o in first_page if o.submitted_at == same]
    assert 0 < len(at_same) < 4, "the fixture must split the tie across the boundary"
    assert orders[3].id not in at_same, "the buy must be the order the first page cut off"
    _use_trading(monkeypatch, trading)

    d = web_server.build_executions()

    sell = d["rows"][0]
    assert (sell["symbol"], sell["side"], sell["entry_price"], sell["exit_price"]) == (
        "NOVA", "sell", 50.0, 55.0)
    assert round(sell["pnl"], 6) == 25.0
    assert d["round_trips"] == 1
    assert len(trading.calls) == 2


def test_executions_fail_whole_when_a_page_fails(monkeypatch):
    """A history with a hole pairs exits against the wrong entries. A failed
    page is an error for the whole build, which the cache then holds for
    ERROR_TTL (the backoff), not the 5 s PORTFOLIO_TTL."""
    trading = _PagedTrading(_account_with_rejected_retries(trips=60, rejects_per_trip=11),
                            fail_on_call=2)
    _use_trading(monkeypatch, trading)

    d = web_server.build_executions()

    assert d == {"error": "ConnectionError: orders page timed out"}
    assert web_server._Cache._ttl_for(d, web_server.PORTFOLIO_TTL) == web_server.ERROR_TTL
    good = {"rows": [], "realized_pnl": 0.0, "round_trips": 0, "win_rate": None}
    assert web_server._Cache._ttl_for(good, web_server.PORTFOLIO_TTL) == web_server.PORTFOLIO_TTL


def test_executions_stop_at_the_page_bound_keeping_the_newest(monkeypatch, caplog):
    """The bound drops the OLDEST orders: paging runs newest first, so the
    latest fills are always in, and the stop is logged."""
    monkeypatch.setattr(web_server, "ORDERS_MAX_PAGES", 2)
    orders = _account_with_rejected_retries(trips=100, rejects_per_trip=11)   # 3 pages' worth
    trading = _PagedTrading(orders)
    _use_trading(monkeypatch, trading)

    with caplog.at_level("WARNING", logger=web_server.__name__):
        d = web_server.build_executions(limit=1_000_000)

    assert len(trading.calls) == 2
    newest = max((o for o in orders if o.filled_at), key=lambda o: o.filled_at)
    assert d["rows"][0]["ts"] == newest.filled_at.timestamp()
    assert 0 < d["round_trips"] < 100
    assert "bound of 2 pages" in caplog.text


def test_executions_walk_fills_in_fill_order_not_page_order(monkeypatch):
    """Pages arrive newest first. The average-cost walk must still run oldest
    fill first, or a sell would close against a buy that came after it."""
    base = dt.datetime(2026, 9, 2, 14, tzinfo=dt.UTC)
    orders = [_paged_order(1, "ORBX", "buy", 4, 20.0, base),
              _paged_order(2, "ORBX", "sell", 4, 22.0, base + dt.timedelta(hours=1)),
              _paged_order(3, "ORBX", "buy", 4, 30.0, base + dt.timedelta(hours=2))]
    _use_trading(monkeypatch, _PagedTrading(orders))

    d = web_server.build_executions()

    assert [r["side"] for r in d["rows"]] == ["buy", "sell", "buy"]     # newest first
    sell = d["rows"][1]
    assert sell["entry_price"] == 20.0 and round(sell["pnl"], 6) == 8.0
    assert d["rows"][0]["held_after"] == 4.0


# --- safety rails --------------------------------------------------------
def test_path_traversal_is_refused(serve, monkeypatch):
    """The static root must not hand out .env (which holds the Alpaca keys)."""
    monkeypatch.setattr(web_server.Handler, "token", None)
    for attack in ("/../.env", "/../../.env", "/..%2f.env", "/static/../../.env"):
        status, body, _ = serve(attack)
        assert status == 404, attack
        assert "ALPACA" not in body


def test_the_dashboard_has_no_write_verbs_but_one(serve, monkeypatch):
    """PUT/DELETE/PATCH remain absent — the stdlib answers 501 Unsupported.

    POST exists as of 2026-08-20, for exactly one route: signing off the
    presenter's daily look before the render bakes it into video. That is a
    real narrowing of the old "no write routes at all" property, so it is
    pinned precisely rather than deleted — the *trading* safety argument does
    not rest on the verb table anyway, it rests on the import graph and the
    route list, both of which are tested below.
    """
    monkeypatch.setattr(web_server.Handler, "token", None)
    for verb in ("PUT", "DELETE", "PATCH"):
        assert not hasattr(web_server.Handler, f"do_{verb}")
    assert hasattr(web_server.Handler, "do_POST")


def test_the_only_post_route_is_look_approval(serve, monkeypatch):
    """A POST anywhere else must 404, not fall through to anything. The risk of
    adding a write verb is that it quietly becomes a general write surface; this
    is the test that notices when it does."""
    monkeypatch.setattr(web_server.Handler, "token", None)
    for path in ("/api/board", "/api/portfolio", "/", "/api/look", "/api/orders"):
        status, _, _ = serve(path, method="POST", body=b"{}")
        assert status == 404, f"POST {path} answered {status}, expected 404"


def test_server_module_never_imports_the_order_path():
    """A viewer that imports order_manager is one typo from placing an order;
    keep the dependency absent rather than merely unused.

    Checked against the parsed import graph, not the raw text — the module
    docstring names relay.order_manager to explain why it is absent, and a
    substring scan would flag that prose as if it were a real import.
    """
    tree = ast.parse(Path(web_server.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{a.name}" for a in node.names)

    forbidden = {"relay.order_manager", "relay.OrderManager", "relay.models"}
    assert not (imported & forbidden), f"dashboard imports the order path: {imported & forbidden}"
    assert not any("order_manager" in name for name in imported)

    # And no call site named like an order submission, anywhere in the module.
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not ({"submit_order", "market_order", "limit_order", "close_position"} & calls)

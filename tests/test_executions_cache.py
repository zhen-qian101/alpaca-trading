"""Tests for the closed-order read behind web.server.build_executions.

build_executions is the producer behind a 5 s cache entry (the dashboard's
executions panel, the chart's fill marks, the cloud publisher's snapshot), and
it reads with the same Alpaca key the engine trades with: about 200 requests a
minute for the whole account. It used to walk every closed order, up to 20
pages, on every refresh. Now the walk is kept (web.server._ClosedOrders) and a
refresh asks only for orders submitted after the newest one held, less
ORDERS_REFRESH_OVERLAP, with a full walk at most every ORDERS_FULL_REFRESH_SEC.

These tests drive it with a fake trading client that pages like Alpaca's
GET /v2/orders and counts every request, and a fake clock.

    .venv/bin/python -m pytest -q tests/test_executions_cache.py
"""
from __future__ import annotations

import datetime as dt
import itertools
from types import SimpleNamespace

import pytest

from web import server as web_server

BASE = dt.datetime(2026, 9, 1, 14, tzinfo=dt.UTC)
_IDS = itertools.count(1)


def order(sym, side, qty, price, submitted, *, filled_at=None, filled=True):
    """A closed order as alpaca-py returns it (the fields build_executions
    reads). A fill defaults to its submission time."""
    return SimpleNamespace(
        id=f"00000000-0000-4000-8000-{next(_IDS):012d}", symbol=sym,
        side=f"OrderSide.{side.upper()}", filled_qty=str(qty if filled else 0),
        filled_avg_price=str(price) if filled else None,
        filled_at=(filled_at or submitted) if filled else None,
        submitted_at=submitted, created_at=submitted, order_type="OrderType.MARKET")


def book(trips: int, rejects: int = 0, start: dt.datetime = BASE) -> list:
    """`trips` ACME buy -> sell round trips six hours apart, each followed by
    `rejects` rejected retries (closed, never filled)."""
    out = []
    for i in range(trips):
        t = start + dt.timedelta(hours=6 * i)
        out.append(order("ACME", "buy", 10, 100.0 + i, t))
        out.append(order("ACME", "sell", 10, (101.0 if i % 4 else 99.0) + i,
                         t + dt.timedelta(hours=1)))
        for k in range(rejects):
            out.append(order("ACME", "sell", 10, None,
                             t + dt.timedelta(hours=1, minutes=1 + k), filled=False))
    return out


class Broker:
    """GET /v2/orders: CLOSED orders only, at most `limit`, sorted by
    submission time in `direction`, only those submitted strictly after
    `after` and strictly before `until`. Orders that are still OPEN are kept
    apart and do not appear until close() moves them over."""

    def __init__(self, orders):
        self.orders = list(orders)
        self.open: list = []
        self.calls: list = []
        self.fail = False

    def close(self, o):
        self.open.remove(o)
        self.orders.append(o)

    def get_orders(self, req):
        self.calls.append(req)
        if self.fail:
            raise ConnectionError("orders timed out")
        assert str(getattr(req.status, "value", req.status)) == "closed"
        assert req.limit is not None and req.limit <= 500
        desc = str(getattr(req.direction, "value", req.direction) or "desc") == "desc"
        rows = sorted(self.orders, key=lambda o: o.submitted_at, reverse=desc)
        if req.until is not None:
            rows = [o for o in rows if o.submitted_at < req.until]
        if req.after is not None:
            rows = [o for o in rows if o.submitted_at > req.after]
        return rows[:req.limit]

    def full_walks(self) -> int:
        """Walks that started from the newest order with no `after`."""
        return sum(1 for c in self.calls if c.after is None and c.until is None)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(web_server, "_ORDERS", web_server._ClosedOrders(clock=c))
    return c


def use(monkeypatch, trading):
    """The module's own client is built once and reused; so is this one."""
    client = SimpleNamespace(client=trading)
    monkeypatch.setattr(web_server, "_client", lambda: client)


def full_read(monkeypatch, trading) -> dict:
    """build_executions for the same broker from an empty cache: one full walk."""
    calls = len(trading.calls)
    with monkeypatch.context() as m:
        m.setattr(web_server, "_ORDERS", web_server._ClosedOrders(clock=Clock()))
        out = web_server.build_executions(limit=1_000_000)
    del trading.calls[calls:]
    return out


# --- the request budget -----------------------------------------------------
def test_a_refresh_is_one_request_however_long_the_history(monkeypatch, clock):
    """1,300 closed orders are three pages. The first read walks them; the
    next one asks only for what came after the newest, in one request."""
    broker = Broker(book(trips=100, rejects=11))
    use(monkeypatch, broker)

    first = web_server.build_executions(limit=1_000_000)
    assert len(broker.calls) == 3

    clock.t += 5
    second = web_server.build_executions(limit=1_000_000)

    assert len(broker.calls) == 4
    req = broker.calls[-1]
    newest = max(o.submitted_at for o in broker.orders)
    assert req.after == newest - web_server.ORDERS_REFRESH_OVERLAP
    assert req.until is None
    assert second == first


def test_thirty_minutes_of_five_second_refreshes(monkeypatch, clock):
    """One request a refresh, plus a full walk every ORDERS_FULL_REFRESH_SEC:
    366 requests in 30 minutes, where walking every page each time took
    1,080, more than the whole key's allowance."""
    broker = Broker(book(trips=100, rejects=11))
    use(monkeypatch, broker)

    for _ in range(360):
        clock.t += 5
        web_server.build_executions()

    assert web_server.ORDERS_FULL_REFRESH_SEC == 600.0
    assert broker.full_walks() == 3                       # minutes 0, 10 and 20
    assert len(broker.calls) == 3 * 3 + 357


# --- the same output for the same orders ------------------------------------
def test_new_fills_read_incrementally_match_a_full_read(monkeypatch, clock):
    """New fills, rejected retries, and orders submitted at the same instant
    as each other and as the newest order already held (the overlap's repeats,
    dropped by id): the result is exactly what a full walk gives."""
    broker = Broker(book(trips=100, rejects=11))
    use(monkeypatch, broker)
    web_server.build_executions(limit=1_000_000)

    last = max(o.submitted_at for o in broker.orders)
    same = last + dt.timedelta(minutes=30)
    broker.orders += [
        order("ACME", "buy", 10, 140.0, last),               # the newest instant, again
        order("NOVA", "buy", 7, 20.0, same),
        order("NOVA", "sell", 7, 21.0, same),                # same submit AND fill time
        order("NOVA", "sell", 7, None, same, filled=False),
        order("ACME", "sell", 10, 141.5, same + dt.timedelta(minutes=5)),
    ]
    clock.t += 5
    calls = len(broker.calls)

    got = web_server.build_executions(limit=1_000_000)

    assert len(broker.calls) == calls + 1
    assert got == full_read(monkeypatch, broker)
    assert got["rows"][0]["exit_price"] == 141.5
    assert sum(1 for r in got["rows"] if r["symbol"] == "NOVA") == 2   # no repeats


def test_a_refresh_with_nothing_new_changes_nothing(monkeypatch, clock):
    broker = Broker(book(trips=30, rejects=2))
    use(monkeypatch, broker)
    first = web_server.build_executions()
    for _ in range(5):
        clock.t += 5
        assert web_server.build_executions() == first
    assert len(broker.calls) == 1 + 5


# --- late fills -------------------------------------------------------------
def test_a_late_fill_inside_the_overlap_shows_at_once(monkeypatch, clock):
    """A limit exit placed 5 minutes before the newest order held, still
    resting at the last read, fills later. The refresh starts
    ORDERS_REFRESH_OVERLAP back, so it is caught on the next read."""
    broker = Broker(book(trips=10))
    last = max(o.submitted_at for o in broker.orders)
    resting = order("ACME", "sell", 10, 115.0, last - dt.timedelta(minutes=5),
                    filled_at=last + dt.timedelta(minutes=40))
    broker.open.append(resting)
    use(monkeypatch, broker)
    web_server.build_executions()

    broker.close(resting)
    clock.t += 5
    got = web_server.build_executions()

    assert broker.full_walks() == 1
    assert got["rows"][0]["exit_price"] == 115.0
    assert got == full_read(monkeypatch, broker)


def test_a_late_fill_older_than_the_overlap_waits_for_the_full_walk(monkeypatch, clock):
    """An order placed an hour before the newest order held falls outside the
    refresh, and shows at the next full walk: never later than
    ORDERS_FULL_REFRESH_SEC."""
    broker = Broker(book(trips=10))
    last = max(o.submitted_at for o in broker.orders)
    resting = order("ACME", "buy", 3, 90.0, last - dt.timedelta(hours=1),
                    filled_at=last + dt.timedelta(hours=2))
    broker.open.append(resting)
    use(monkeypatch, broker)
    web_server.build_executions()

    broker.close(resting)
    clock.t += web_server.ORDERS_FULL_REFRESH_SEC - 1
    missed = web_server.build_executions()
    assert all(r["price"] != 90.0 for r in missed["rows"])

    clock.t += 1
    got = web_server.build_executions()

    assert broker.full_walks() == 2
    assert got["rows"][0]["price"] == 90.0
    assert got == full_read(monkeypatch, broker)


# --- failures ---------------------------------------------------------------
def test_a_failed_refresh_is_an_error_and_keeps_what_was_read(monkeypatch, clock):
    """A failed request is still an error for the whole build (held for
    ERROR_TTL by the cache, the backoff). The orders already read stay, so
    the read after the outage is again a single request."""
    broker = Broker(book(trips=40, rejects=11))           # 520 orders, two pages
    use(monkeypatch, broker)
    web_server.build_executions()

    broker.fail = True
    clock.t += 5
    err = web_server.build_executions()
    assert err == {"error": "ConnectionError: orders timed out"}
    assert web_server._Cache._ttl_for(err, web_server.PORTFOLIO_TTL) == web_server.ERROR_TTL

    broker.fail = False
    broker.orders.append(order("ACME", "buy", 5, 50.0,
                               max(o.submitted_at for o in broker.orders) + dt.timedelta(hours=1)))
    clock.t += web_server.ERROR_TTL
    calls = len(broker.calls)
    got = web_server.build_executions(limit=1_000_000)

    assert len(broker.calls) == calls + 1
    assert got == full_read(monkeypatch, broker)


def test_a_failed_full_walk_is_tried_again_next_time(monkeypatch, clock):
    """When the full walk is due and fails, the next read walks in full again
    rather than settling for the refresh."""
    broker = Broker(book(trips=40, rejects=11))
    use(monkeypatch, broker)
    web_server.build_executions()

    broker.fail = True
    clock.t += web_server.ORDERS_FULL_REFRESH_SEC
    assert "error" in web_server.build_executions()

    broker.fail = False
    clock.t += web_server.ERROR_TTL
    web_server.build_executions()
    assert broker.full_walks() == 3                       # first, failed, retried


def test_a_first_walk_that_fails_keeps_nothing(monkeypatch, clock):
    broker = Broker(book(trips=5))
    broker.fail = True
    use(monkeypatch, broker)
    assert "error" in web_server.build_executions()
    broker.fail = False
    clock.t += 5
    web_server.build_executions()
    assert broker.full_walks() == 2


# --- what is never mixed ------------------------------------------------------
def test_another_client_starts_with_a_full_walk(monkeypatch, clock):
    """Orders kept for one client are never shown for another."""
    a = Broker(book(trips=5))
    use(monkeypatch, a)
    web_server.build_executions()

    b = Broker([order("ORBX", "buy", 2, 10.0, BASE)])
    use(monkeypatch, b)
    clock.t += 5
    got = web_server.build_executions()

    assert b.full_walks() == 1
    assert {r["symbol"] for r in got["rows"]} == {"ORBX"}


def test_orders_without_ids_are_never_kept(monkeypatch, clock):
    """A refresh finds its repeats by id. Orders that carry none (no real
    Alpaca order, but a fake might) are read in full every time, so a fill is
    never counted twice."""
    def bare(side, qty, price, hours):
        return SimpleNamespace(symbol="ACME", side=side, filled_qty=str(qty),
                               filled_avg_price=str(price), order_type="market",
                               filled_at=BASE + dt.timedelta(hours=hours),
                               submitted_at=BASE + dt.timedelta(hours=hours))

    class Plain:
        calls = 0

        def get_orders(self, req):
            Plain.calls += 1
            return [bare("buy", 10, 100.0, 0), bare("sell", 10, 103.0, 30)]

    use(monkeypatch, Plain())
    first = web_server.build_executions()
    clock.t += 5
    second = web_server.build_executions()

    assert first == second and second["round_trips"] == 1 and len(second["rows"]) == 2
    assert Plain.calls == 2


def test_more_new_orders_than_the_bound_keep_the_newest(monkeypatch, clock):
    """A refresh that hits the page bound is the newest ORDERS_MAX_PAGES
    pages, which is what a full walk gives too."""
    monkeypatch.setattr(web_server, "ORDERS_PAGE", 5)
    monkeypatch.setattr(web_server, "ORDERS_MAX_PAGES", 3)
    broker = Broker(book(trips=2))
    use(monkeypatch, broker)
    web_server.build_executions()

    broker.orders += book(trips=10, start=BASE + dt.timedelta(days=30))
    clock.t += 5
    got = web_server.build_executions(limit=1_000_000)

    assert broker.full_walks() == 1
    assert got == full_read(monkeypatch, broker)


def test_two_readers_at_once_share_one_walk(monkeypatch, clock):
    """The dashboard's cache key and the exporter's direct call can land
    together; the second waits for the first instead of walking again."""
    import threading

    broker = Broker(book(trips=40, rejects=11))
    gate, entered = threading.Event(), threading.Event()
    real = broker.get_orders

    def slow(req):
        entered.set()
        gate.wait(timeout=5)
        return real(req)

    broker.get_orders = slow
    use(monkeypatch, broker)
    out: list = []
    t1 = threading.Thread(target=lambda: out.append(web_server.build_executions()))
    t1.start()
    assert entered.wait(timeout=5)
    t2 = threading.Thread(target=lambda: out.append(web_server.build_executions()))
    t2.start()
    gate.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(out) == 2 and out[0] == out[1]
    assert broker.full_walks() == 1
    assert len(broker.calls) == 2 + 1                      # one walk, one refresh

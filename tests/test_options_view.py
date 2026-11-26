"""web/options_view.py: the put sleeve as the cloud dashboard shows it.

options_view is a READ-ONLY mirror of relay.csp_manager.assignment_state(),
because the publisher may not import csp_manager (it can place orders). These
tests run BOTH on the same fakes, so the mirror cannot drift from the original
without a failure here, and then pin what the mirror adds on top (the option's
prices, the underlying's last trade, the distance to the strike).

Everything is synthetic: invented underlyings and fakes for the broker. No
client is constructed and nothing is sent.
"""
from __future__ import annotations

import json
import time

import pytest

import relay.csp_manager as csp
from web import options_view
from web import server as web_server


def _occ(strike: float, root: str = "ORBT", yymmdd: str = "260320", right: str = "P") -> str:
    return f"{root}{yymmdd}{right}{int(round(strike * 1000)):08d}"


def _equity(symbol, qty="100", **kw):
    attrs = {"symbol": symbol, "qty": qty, "side": "long", "asset_class": "us_equity",
             "avg_entry_price": "40.0", "current_price": "41.5", "unrealized_pl": "150.0",
             "unrealized_plpc": "0.0375"}
    attrs.update(kw)
    return type("Pos", (), attrs)()


def _option(symbol=None, qty="-1", **kw):
    attrs = {"symbol": symbol or _occ(40), "qty": qty, "side": "short",
             "asset_class": type("AC", (), {"value": "us_option"})(),
             "avg_entry_price": "1.35", "current_price": "0.62", "unrealized_pl": "73.0",
             "market_value": "-62.0"}
    attrs.update(kw)
    return type("OptPos", (), attrs)()


class FakeOptions:
    """What csp.assignment_state() reads: option_positions(). Sends nothing."""

    def __init__(self, positions):
        self._positions = positions

    def option_positions(self):
        return [p for p in self._positions
                if str(getattr(getattr(p, "asset_class", None), "value", "")) == "us_option"]


class FakeClient:
    """web_server._client() stand-in: the two reads build_options makes."""

    def __init__(self, positions, trade=44.85, fail=None):
        self.positions, self.trade, self.fail = positions, trade, fail
        self.calls = []

    def get_all_positions(self):
        self.calls.append("positions")
        if self.fail:
            raise self.fail
        return self.positions

    def get_latest_trade(self, symbol):
        self.calls.append(("trade", symbol))
        if self.trade is None:
            raise RuntimeError("no trade")
        return self.trade, time.time()


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    journal = tmp_path / "trades.jsonl"
    policy = tmp_path / "csp.json"
    for mod in (csp, options_view):
        monkeypatch.setattr(mod, "JOURNAL_FILE", journal)
        monkeypatch.setattr(mod, "CSP_CONFIG_FILE", policy)
    monkeypatch.setattr(csp, "DECISIONS_FILE", tmp_path / "decisions.json")
    monkeypatch.setattr(options_view, "AUX_FILE", tmp_path / "state_entries.json")
    return tmp_path


def _policy(tmp_path, **block):
    (tmp_path / "csp.json").write_text(json.dumps({"enabled": True, "shadow": False,
                                                   "underlying": "ORBT", **block}))


def _sold_a_put(tmp_path, und="ORBT", ago_days=3.0):
    with (tmp_path / "trades.jsonl").open("a") as f:
        f.write(json.dumps({"event": "csp_sell", "ticker": und,
                            "ts": time.time() - ago_days * 86400}) + "\n")


def _parity(positions, aux):
    """Both implementations on the same inputs; the mirror's extra fields aside."""
    equities = [p for p in positions if not options_view._is_option_position(p)]
    want = csp.assignment_state(FakeOptions(positions), equities, aux)
    got = options_view.sleeve_state(positions, options_view.load_policy(), aux, time.time())
    if got["short_put"] is not None:
        got["short_put"] = {k: got["short_put"][k] for k in want["short_put"]}
    assert got == want
    return got


SCENARIOS = {
    "flat": lambda t: ([], {}),
    "short put": lambda t: ([_option(_occ(40), qty="-1")], {}),
    "two contracts": lambda t: ([_option(_occ(37.5), qty="-2")], {}),
    "long option ignored": lambda t: ([_option(_occ(40), qty="2")], {}),
    "unparseable OCC": lambda t: ([_option("ORBT-WEIRD", qty="-1")], {}),
    "assigned, unmanaged": lambda t: (_sold_a_put(t) or [_equity("ORBT", "100")], {"opened": {"ACME": {}}}),
    "assigned, adopted": lambda t: (_sold_a_put(t) or [_equity("ORBT", "200")],
                                    {"adopted": {"ORBT": {"basis": 40, "anchor": 38.8,
                                                          "since": "2026-03-18T20:00:00+00:00"}}}),
    "assigned, engine opened": lambda t: (_sold_a_put(t) or [_equity("ORBT", "100")], {"opened": {"ORBT": {}}}),
    "shares without a put": lambda t: ([_equity("ORBT", "100")], {}),
    "odd lot": lambda t: (_sold_a_put(t) or [_equity("ORBT", "47")], {}),
    "old put": lambda t: (_sold_a_put(t, ago_days=45) or [_equity("ORBT", "100")], {}),
    "other underlying": lambda t: (_sold_a_put(t, und="NOVA") or [_equity("ORBT", "100")], {}),
    "short shares": lambda t: (_sold_a_put(t) or [_equity("ORBT", "-100")], {}),
    "put and shares": lambda t: (_sold_a_put(t) or [_option(), _equity("orbt", "100")], {}),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_mirror_matches_assignment_state(sandbox, name):
    _policy(sandbox)
    positions, aux = SCENARIOS[name](sandbox)
    _parity(positions, aux)


@pytest.mark.parametrize("block", [
    {}, {"enabled": "true"}, {"enabled": True, "shadow": None}, {"shadow": "false"},
    {"shadow": False}, {"underlying": "nova"}, {"underlying": None}, {"underlying": ""},
])
def test_policy_coercion_matches_csp_config(sandbox, block):
    (sandbox / "csp.json").write_text(json.dumps(block))
    _parity([_option()], {})


def test_missing_and_corrupt_policy_files_match_too(sandbox):
    _parity([], {})                                         # no file: the defaults
    (sandbox / "csp.json").write_text("{not json")
    _parity([], {})                                         # corrupt: disabled, defaults
    assert options_view.policy_view(options_view.load_policy())["enabled"] is False


def test_default_underlying_matches_the_manager():
    assert options_view.DEFAULT_UNDERLYING == csp.DEFAULTS["underlying"]


def test_policy_file_is_the_managers():
    assert options_view.CSP_CONFIG_FILE.name == "csp.json"


def test_build_options_adds_prices_and_distance_to_strike(sandbox, monkeypatch):
    _policy(sandbox)
    client = FakeClient([_option(_occ(40)), _equity("ACME", "12")], trade=44.85)
    monkeypatch.setattr(web_server, "_client", lambda: client)
    out = options_view.build_options()
    put = out["short_put"]
    assert put["symbol"] == _occ(40) and put["contracts"] == 1.0
    assert put["strike"] == 40.0 and put["expiry"] == "2026-03-20"
    assert put["avg_entry_price"] == 1.35 and put["current_price"] == 0.62
    assert put["market_value"] == -62.0 and put["unrealized"] == 73.0
    assert put["underlying_price"] == 44.85
    assert put["strike_distance_pct"] == pytest.approx(44.85 / 40 - 1)
    assert out["assigned"] is None and out["enabled"] is True and out["shadow"] is False
    assert client.calls == ["positions", ("trade", "ORBT")]


def test_build_options_falls_back_to_a_held_mark_for_the_underlying(sandbox, monkeypatch):
    _policy(sandbox)
    client = FakeClient([_option(_occ(40)), _equity("ORBT", "7", current_price="39.0")], trade=None)
    monkeypatch.setattr(web_server, "_client", lambda: client)
    put = options_view.build_options()["short_put"]
    assert put["underlying_price"] == 39.0
    assert put["strike_distance_pct"] == pytest.approx(39.0 / 40 - 1)   # in the money: negative


def test_no_put_means_no_quote_request(sandbox, monkeypatch):
    _policy(sandbox)
    client = FakeClient([_equity("ACME", "12")])
    monkeypatch.setattr(web_server, "_client", lambda: client)
    out = options_view.build_options()
    assert out["short_put"] is None and client.calls == ["positions"]


def test_no_policy_and_nothing_held_is_null(sandbox, monkeypatch):
    monkeypatch.setattr(web_server, "_client", lambda: FakeClient([_equity("ACME", "12")]))
    assert options_view.build_options() is None


def test_no_policy_but_an_open_put_is_still_reported(sandbox, monkeypatch):
    """Reporting "no sleeve" while a put is live is the misleading direction."""
    monkeypatch.setattr(web_server, "_client", lambda: FakeClient([_option(_occ(40))]))
    out = options_view.build_options()
    assert out["short_put"]["strike"] == 40.0 and out["enabled"] is False


def test_a_broker_failure_is_an_error_section(sandbox, monkeypatch):
    _policy(sandbox)
    monkeypatch.setattr(web_server, "_client", lambda: FakeClient([], fail=OSError("timed out")))
    assert options_view.build_options() == {"error": "OSError: timed out"}


def test_a_corrupt_entry_sidecar_reads_as_unmanaged(sandbox, monkeypatch):
    _policy(sandbox)
    _sold_a_put(sandbox)
    (sandbox / "state_entries.json").write_text("{oops")
    monkeypatch.setattr(web_server, "_client", lambda: FakeClient([_equity("ORBT", "100")]))
    assigned = options_view.build_options()["assigned"]
    assert assigned["managed_by_engine"] is False and assigned["qty"] == 100.0


def test_the_mirror_never_writes(sandbox, monkeypatch):
    _policy(sandbox)
    _sold_a_put(sandbox)
    (sandbox / "state_entries.json").write_text("{oops")
    before = {p: p.stat().st_mtime_ns for p in sandbox.iterdir()}
    monkeypatch.setattr(web_server, "_client", lambda: FakeClient([_equity("ORBT", "100"), _option()]))
    options_view.build_options()
    assert {p: p.stat().st_mtime_ns for p in sandbox.iterdir()} == before

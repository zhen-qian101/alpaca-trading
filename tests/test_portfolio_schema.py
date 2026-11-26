"""The public portfolio's schema and its synthetic fixture.

apps/portfolio/schema/portfolio.schema.json is the leak gate for everything
the public site shows: web/public_export.py refuses to upload a portfolio.json
that does not validate, and CI checks the fixture against it. So:

  * every object in the schema closes itself (additionalProperties: false);
  * the schema uses only keywords web/schema_gate.py implements, and that
    validator agrees with the reference `jsonschema` package wherever that
    package is installed;
  * the committed fixture (invented numbers, fake YouTube ids) validates;
  * the account is DAILY equity only: no intraday series can pass;
  * a JSON text is read strictly: a duplicate key (json.loads would keep the
    last copy and validate that, while the bytes still carry the first), NaN
    or Infinity is refused before anything is validated.

    .venv/bin/python -m pytest -q tests/test_portfolio_schema.py
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from web import schema_gate

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "apps" / "portfolio" / "schema" / "portfolio.schema.json"
FIXTURE_FILE = ROOT / "apps" / "portfolio" / "fixtures" / "portfolio.json"
SCHEMA = schema_gate.load_schema(SCHEMA_FILE)
FIXTURE = json.loads(FIXTURE_FILE.read_text())


def subschemas(node, where="#"):
    """(location, subschema) for every schema object in the tree."""
    if isinstance(node, dict):
        yield where, node
        for key in ("properties", "$defs"):
            for name, sub in (node.get(key) or {}).items():
                yield from subschemas(sub, f"{where}/{key}/{name}")
        if "items" in node:
            yield from subschemas(node["items"], f"{where}/items")
        for key in ("anyOf", "oneOf"):
            for i, sub in enumerate(node.get(key) or []):
                yield from subschemas(sub, f"{where}/{key}/{i}")


# --------------------------------------------------------------------------
# the schema
# --------------------------------------------------------------------------
def test_every_object_in_the_schema_is_closed():
    objects = [(w, n) for w, n in subschemas(SCHEMA)
               if n.get("type") == "object" or "properties" in n]
    assert len(objects) > 20
    loose = [w for w, n in objects if n.get("additionalProperties") is not False]
    assert loose == []
    # ...and every declared property is required, so a field is never
    # silently absent (null says "unknown" instead)
    optional = [w for w, n in objects
                if set(n.get("properties", {})) - set(n.get("required", []))
                and not w.startswith("#/$defs/strategy_")]
    assert optional == []


def test_the_schema_declares_no_market_data_or_private_fields():
    names = set()
    for _, n in subschemas(SCHEMA):
        names.update((n.get("properties") or {}).keys())
    assert not names & {
        "current_price", "market_value", "last_price", "price", "bid", "ask", "quote", "bars",
        "candles", "open", "high", "low", "close", "volume", "underlying_price", "notes",
        "reason", "account_number", "id", "order_id", "path", "host", "email", "log", "desk",
        "look", "title", "title_zh", "briefing_zh", "voice", "cash", "buying_power"}


def test_the_fixture_validates():
    assert schema_gate.validate(FIXTURE, SCHEMA) == []


def test_the_fixture_is_synthetic_and_complete():
    acct = FIXTURE["account"]
    assert 100 <= len(acct["daily"]) <= 140
    assert "intraday" not in acct
    # One position from the signals (the rotation holds one at a time). The
    # put is still open, so no assigned shares sit beside it.
    assert len(FIXTURE["positions"]["equities"]) == 1
    assert FIXTURE["positions"]["options"]
    assert 15 <= len(FIXTURE["trades"]["round_trips"]) <= 30
    assert len(FIXTURE["signals"]["rows"]) == 10
    assert len(FIXTURE["recaps"]) == 4
    assert all(r["youtube_id"].startswith("FAKE") for r in FIXTURE["recaps"])
    assert FIXTURE["meta"]["unavailable"] == []


def test_the_fixtures_account_figures_come_from_completed_sessions():
    """As web/public_export.py account_section builds them, and as the
    Overview says: every figure is the closing equity of a completed session
    (the last point of daily is before the as_of date), and the returns and
    drawdown are worked out from that curve alone."""
    acct = FIXTURE["account"]
    curve = [p["equity"] for p in acct["daily"]]
    assert acct["daily"][-1]["date"] < FIXTURE["meta"]["as_of"][:10]
    assert acct["start_date"] == acct["daily"][0]["date"]
    assert acct["start_equity"] == curve[0] and acct["equity"] == curve[-1]
    assert acct["total_return_pct"] == round(curve[-1] / curve[0] - 1, 6)
    peak, drawdown = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        drawdown = min(drawdown, e / peak - 1)
    assert acct["max_drawdown_pct"] == round(drawdown, 6)
    assert acct["day_pl_pct"] == round(curve[-1] / curve[-2] - 1, 6)
    changes = [(acct["daily"][i]["date"], round(curve[i] / curve[i - 1] - 1, 6))
               for i in range(1, len(curve))]
    assert acct["best_day"] == dict(zip(("date", "return_pct"), max(changes, key=lambda x: x[1])))
    assert acct["worst_day"] == dict(zip(("date", "return_pct"), min(changes, key=lambda x: x[1])))


# --------------------------------------------------------------------------
# the validator
# --------------------------------------------------------------------------
def mutated(path: str, value=None, *, delete=False):
    """A copy of the fixture with the value at a dotted path replaced."""
    doc = copy.deepcopy(FIXTURE)
    parts = [int(p) if p.isdigit() else p for p in path.split(".")]
    parent = doc
    for p in parts[:-1]:
        parent = parent[p]
    if delete:
        del parent[parts[-1]]
    else:
        parent[parts[-1]] = value
    return doc


def first_trade(**changes):
    doc = copy.deepcopy(FIXTURE)
    doc["trades"]["round_trips"][0].update(changes)
    return doc


CASES = {
    # name: (document, valid?)
    "fixture": (FIXTURE, True),
    "extra top-level key": (mutated("bars", [[1, 2, 3, 4]]), False),
    "extra nested key": (mutated("positions.equities.0.current_price", 101.0), False),
    "extra key in a nullable object": (mutated("account.best_day.reason", "x"), False),
    "an intraday series": (mutated("account.intraday", {
        "date": "2026-06-12", "points": [{"t": "2026-06-12T13:30:00Z", "equity": 1.0}]}), False),
    "an intraday series, null": (mutated("account.intraday", None), False),
    "max_open_positions (enforced nowhere)": (mutated("strategy.risk.max_open_positions", 1), False),
    "extended hours unknown": (mutated("strategy.risk.allow_extended_hours", None), False),
    "extra key in a strategy": (mutated("strategy.symbols.0.strategies.0.note", "x"), False),
    "unknown strategy type": (mutated("strategy.symbols.0.strategies.0", {"type": "magic"}), False),
    "a strategy type the engine no longer runs": (mutated(
        "strategy.symbols.0.strategies.0", {"type": "donchian", "entry_window": 55, "exit_window": 20}),
        False),
    "a round trip from a retired strategy": (first_trade(strategy="donchian"), False),
    "a round trip from a strategy": (first_trade(strategy="macd_sma"), True),
    "missing required": (mutated("account.equity", delete=True), False),
    "null where not allowed": (mutated("account.equity", None), False),
    "nullable field null": (mutated("account.best_day", None), True),
    "whole section null": (mutated("trades", None), True),
    "bool is not an integer": (mutated("trades.stats.round_trips", True), False),
    "1.0 is an integer": (mutated("trades.stats.round_trips", 20.0), True),
    "string is not a number": (mutated("account.equity", "103827.51"), False),
    "bad youtube id": (mutated("recaps.0.youtube_id", "short"), False),
    "bad date": (mutated("account.daily.0.date", "2026/01/05"), False),
    "bad timestamp": (first_trade(entry_at="2026-05-19T14:30:00+00:00"), False),
    "bad symbol": (mutated("positions.equities.0.symbol", "../etc"), False),
    "enum": (mutated("signals.rows.0.bias", "maybe"), False),
    "const": (mutated("meta.schema", 2), False),
    "unique items": (mutated("meta.unavailable", ["trades", "trades"]), False),
    "unknown section name": (mutated("meta.unavailable", ["logs"]), False),
    "too many recaps": (mutated("recaps", FIXTURE["recaps"] * 4), False),
    "zero fill price": (first_trade(entry_price=0), False),
    "positive drawdown": (mutated("account.max_drawdown_pct", 0.1), False),
    "fraction above one": (mutated("signals.rows.0.suggested_size", 1.5), False),
    # A language model writes the recap titles, so the file has no field for one.
    "a title on a recap": (mutated("recaps.0.title", "Model headline"), False),
    "a null title on a recap": (mutated("recaps.0.title", None), False),
    "short option": (mutated("positions.options.0.side", "long"), False),
    "array where object": (mutated("strategy.risk", []), False),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_gate(name):
    doc, valid = CASES[name]
    assert schema_gate.is_valid(doc, SCHEMA) is valid, schema_gate.validate(doc, SCHEMA)


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_gate_agrees_with_the_reference_validator(name):
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(SCHEMA)
    doc, valid = CASES[name]
    assert validator.is_valid(doc) is valid


def test_the_reference_validator_accepts_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def test_the_schema_has_no_intraday_series():
    names = set()
    for _, n in subschemas(SCHEMA):
        names.update((n.get("properties") or {}).keys())
    assert not names & {"intraday", "points", "t"}
    assert SCHEMA["$defs"]["account"]["required"][-1] == "daily"


FIXTURE_TEXT = FIXTURE_FILE.read_text()


def test_loads_reads_the_fixture_as_json_does():
    assert schema_gate.loads(FIXTURE_TEXT) == FIXTURE
    assert schema_gate.validate_text(FIXTURE_TEXT, SCHEMA) == []
    assert schema_gate.validate_text(FIXTURE_TEXT.encode(), SCHEMA) == []


SMUGGLED = "CANARY-SECRET account PA9CANARY0001 /home/canary/x"


@pytest.mark.parametrize("text", [
    # the first copy is the one a reader that keeps the FIRST key would see
    FIXTURE_TEXT.replace('"meta": {', '"meta": {"data_delay": "%s", ' % SMUGGLED, 1),
    FIXTURE_TEXT.replace('"account": {', '"account": {"equity": 1, ', 1),
    FIXTURE_TEXT.replace('"recaps": [', '"recaps": null, "recaps": [', 1),
    '{"a": 1, "a": 1}',
    '[{"x": {"k": 1, "k": 2}}]',
])
def test_a_duplicate_key_is_refused_anywhere(text):
    assert json.loads(text) is not None                         # json.loads would take it
    with pytest.raises(schema_gate.JSONTextError) as caught:
        schema_gate.loads(text)
    assert "CANARY" not in str(caught.value) and '"k"' not in str(caught.value)
    errors = schema_gate.validate_text(text, SCHEMA)
    assert errors == ["$: an object has the same key twice"]
    assert not any(SMUGGLED in e for e in errors)


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": Infinity}', '[-Infinity]', "", "{", "nul",
                                  '{"a": 1} {"b": 2}', b"\xff\xfe{}"])
def test_loads_refuses_what_is_not_strict_json(text):
    with pytest.raises(schema_gate.JSONTextError):
        schema_gate.loads(text)
    assert len(schema_gate.validate_text(text, SCHEMA)) == 1


def test_a_schema_with_a_duplicate_key_is_refused(tmp_path):
    path = tmp_path / "dup.schema.json"
    path.write_text('{"type": "object", "additionalProperties": false, '
                    '"additionalProperties": true, "properties": {}}')
    with pytest.raises(ValueError):
        schema_gate.load_schema(path)


def test_nan_and_infinity_are_refused():
    # Python's json writes them, JavaScript's JSON.parse rejects the file.
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert not schema_gate.is_valid(mutated("account.equity", bad), SCHEMA)


def test_errors_name_the_path_and_never_the_value():
    doc = mutated("signals.rows.0.bias", "TOP-SECRET")
    doc["positions"]["equities"][0]["symbol"] = "SECRET VALUE 123"
    errors = schema_gate.validate(doc, SCHEMA)
    assert errors
    assert any(e.startswith("$.positions.equities") for e in errors)
    assert any(e.startswith("$.signals.rows[0].bias") for e in errors)
    assert not any("SECRET" in e for e in errors)


def test_a_nullable_field_reports_the_real_problem():
    errors = schema_gate.validate(mutated("account.best_day.extra", 1), SCHEMA)
    assert "$.account.best_day: unexpected property 'extra'" in errors


# --------------------------------------------------------------------------
# the gate refuses a schema it cannot enforce
# --------------------------------------------------------------------------
@pytest.mark.parametrize("schema, why", [
    ({"type": "object", "properties": {"a": {"type": "string"}}}, "additionalProperties"),
    ({"type": "object", "additionalProperties": True, "properties": {}}, "additionalProperties"),
    ({"type": "object", "additionalProperties": False, "properties": {
        "a": {"type": "object", "properties": {"b": {}}}}}, "additionalProperties"),
    ({"type": "string", "format": "date"}, "unsupported keyword"),
    ({"type": "object", "additionalProperty": False}, "unsupported keyword"),
    ({"$ref": "#/$defs/missing"}, "does not name"),
    ({"$ref": "https://example.com/remote.json"}, "does not name"),
    ({"type": "string", "pattern": "("}, "does not compile"),
    ({"type": "object", "additionalProperties": False, "required": ["a"], "properties": {}},
     "required names"),
    ({"type": "strnig"}, "unknown type"),
    ({"anyOf": []}, "non-empty"),
    ({"enum": []}, "non-empty"),
])
def test_the_gate_refuses_a_schema_it_cannot_enforce(schema, why):
    with pytest.raises(schema_gate.SchemaError, match=why):
        schema_gate.check_schema(schema)

"""A small, dependency-free JSON Schema validator: the public export's leak gate.

web/public_export.py validates every portfolio.json against
apps/portfolio/schema/portfolio.schema.json and uploads nothing that fails, so
this module decides what can leave the box. It implements the subset of JSON
Schema 2020-12 that schema uses, and nothing else, ON PURPOSE:

  * check_schema() REFUSES a schema that uses any keyword outside that subset.
    A keyword this validator silently ignored (say `format`, or a typo such as
    `additionalProperty`) would make the gate look stricter than it is.
  * check_schema() also refuses any object schema that does not set
    `additionalProperties: false`, so an undeclared field can never pass
    because someone forgot the line on one nested object.
  * error messages name the JSON path and the rule that failed, never the
    offending VALUE: they go to the exporter's log, and the value that failed
    is exactly the one that must not be published.
  * loads() is the only way a JSON TEXT should reach validate(). It refuses a
    duplicate key in any object, and NaN or Infinity. json.loads keeps the
    LAST of two equal keys, so a file with `"data_delay"` twice would validate
    on the second copy while its bytes still carried the first: whatever is
    validated must be what ships, so a document that could parse two ways is
    refused outright.

No third-party package: the trading box's venv is not changed for this, and
the same file runs in CI. tests/test_portfolio_schema.py cross-checks it
against the `jsonschema` package wherever that is importable.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


class JSONTextError(ValueError):
    """A JSON text the gate will not read (its message never quotes the text)."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            # The key is data: the message names neither it nor its value.
            raise JSONTextError("an object has the same key twice")
        out[key] = value
    return out


def _no_constant(_name: str) -> Any:
    raise JSONTextError("NaN and Infinity are not JSON")


def loads(text: str | bytes) -> Any:
    """Parse one JSON document strictly: a duplicate key anywhere, NaN,
    Infinity or anything json.loads refuses raises JSONTextError. The message
    never quotes the text (json's own error would)."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except JSONTextError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise JSONTextError("not a JSON document") from None

# Keywords that carry no validation (annotations and the $defs container).
_ANNOTATIONS = frozenset({"$schema", "$id", "$comment", "title", "description", "examples", "$defs"})
# Keywords this validator implements.
_ASSERTIONS = frozenset({
    "$ref", "type", "enum", "const",
    "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "uniqueItems",
    "pattern", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "anyOf", "oneOf",
})
_TYPES = frozenset({"null", "boolean", "integer", "number", "string", "array", "object"})
_REF_PREFIX = "#/$defs/"
MAX_ERRORS = 50


class SchemaError(ValueError):
    """The schema itself is unusable as a gate."""


# --------------------------------------------------------------------------
# the schema
# --------------------------------------------------------------------------
def load_schema(path: Path | str) -> dict:
    """Read and check a schema file; raises OSError, ValueError (JSONTextError
    included: a schema with a duplicate key is refused too) or SchemaError."""
    schema = loads(Path(path).read_bytes())
    check_schema(schema)
    return schema


def _types(node: dict) -> set[str]:
    t = node.get("type")
    if t is None:
        return set()
    return {t} if isinstance(t, str) else set(t) if isinstance(t, list) else {"?"}


def check_schema(schema: Any) -> None:
    """Raise SchemaError unless `schema` uses only supported keywords, every
    $ref resolves, every pattern compiles, and every object schema closes
    itself with additionalProperties: false."""
    if not isinstance(schema, dict):
        raise SchemaError("the schema is not a JSON object")
    defs = schema.get("$defs", {})
    if not isinstance(defs, dict):
        raise SchemaError("$defs is not an object")

    def walk(node: Any, where: str) -> None:
        if not isinstance(node, dict):
            raise SchemaError(f"{where}: a subschema must be an object")
        unknown = set(node) - _ANNOTATIONS - _ASSERTIONS
        if unknown:
            raise SchemaError(f"{where}: unsupported keyword(s) {sorted(unknown)}")
        types = _types(node)
        if not types <= _TYPES:
            raise SchemaError(f"{where}: unknown type {node.get('type')!r}")
        if ("object" in types or "properties" in node or "required" in node) \
                and node.get("additionalProperties") is not False:
            raise SchemaError(f"{where}: an object schema must set additionalProperties: false")
        if "$ref" in node:
            ref = node["$ref"]
            if not (isinstance(ref, str) and ref.startswith(_REF_PREFIX)
                    and ref[len(_REF_PREFIX):] in defs):
                raise SchemaError(f"{where}: $ref {ref!r} does not name an entry of $defs")
        if "pattern" in node:
            try:
                re.compile(node["pattern"])
            except (re.error, TypeError) as exc:
                raise SchemaError(f"{where}: pattern does not compile ({exc})") from None
        if "required" in node and not (isinstance(node["required"], list)
                                       and set(node["required"]) <= set(node.get("properties", {}))):
            raise SchemaError(f"{where}: required names a property that is not declared")
        if "enum" in node and not (isinstance(node["enum"], list) and node["enum"]):
            raise SchemaError(f"{where}: enum must be a non-empty list")
        for name, sub in (node.get("properties") or {}).items():
            walk(sub, f"{where}.properties.{name}")
        if "items" in node:
            walk(node["items"], f"{where}.items")
        for key in ("anyOf", "oneOf"):
            if key in node:
                if not (isinstance(node[key], list) and node[key]):
                    raise SchemaError(f"{where}: {key} must be a non-empty list")
                for i, sub in enumerate(node[key]):
                    walk(sub, f"{where}.{key}[{i}]")

    walk(schema, "#")
    for name, sub in defs.items():
        walk(sub, f"#/$defs/{name}")


# --------------------------------------------------------------------------
# instances
# --------------------------------------------------------------------------
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _type_ok(v: Any, t: str) -> bool:
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return _is_number(v) and float(v).is_integer()
    if t == "number":
        return _is_number(v)
    if t == "string":
        return isinstance(v, str)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    return False


def _canon(v: Any) -> Any:
    """A value that compares the way JSON Schema compares: true != 1, 1 == 1.0."""
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return (type(v).__name__, v)
    if _is_number(v):
        return ("number", float(v))
    if isinstance(v, list):
        return ("array", tuple(_canon(x) for x in v))
    if isinstance(v, dict):
        return ("object", tuple(sorted((k, _canon(x)) for k, x in v.items())))
    return ("other", repr(v))


def validate(instance: Any, schema: dict) -> list[str]:
    """Every rule `instance` breaks, as "<json path>: <rule>" strings (at most
    MAX_ERRORS). An empty list means valid. Messages never contain values."""
    errors: list[str] = []
    _check(instance, schema, schema, "$", errors)
    return errors[:MAX_ERRORS]


def is_valid(instance: Any, schema: dict) -> bool:
    return not validate(instance, schema)


def validate_text(text: str | bytes, schema: dict) -> list[str]:
    """validate() for a JSON TEXT, read with loads(): a duplicate key, NaN or
    text that is not JSON is one error at "$", worded without the text."""
    try:
        instance = loads(text)
    except JSONTextError as exc:
        return [f"$: {exc}"]
    return validate(instance, schema)


def _check(v: Any, node: dict, root: dict, path: str, errors: list[str]) -> None:
    if len(errors) >= MAX_ERRORS:
        return
    if "$ref" in node:
        target = root["$defs"][node["$ref"][len(_REF_PREFIX):]]
        _check(v, target, root, path, errors)

    types = _types(node)
    if types and not any(_type_ok(v, t) for t in types):
        errors.append(f"{path}: expected {'/'.join(sorted(types))}")
        return
    if "const" in node and _canon(v) != _canon(node["const"]):
        errors.append(f"{path}: not the required constant")
    if "enum" in node and _canon(v) not in {_canon(e) for e in node["enum"]}:
        errors.append(f"{path}: not one of the allowed values")

    if _is_number(v):
        if "minimum" in node and v < node["minimum"]:
            errors.append(f"{path}: below the minimum")
        if "maximum" in node and v > node["maximum"]:
            errors.append(f"{path}: above the maximum")
        if "exclusiveMinimum" in node and v <= node["exclusiveMinimum"]:
            errors.append(f"{path}: not above the exclusive minimum")
        if "exclusiveMaximum" in node and v >= node["exclusiveMaximum"]:
            errors.append(f"{path}: not below the exclusive maximum")
    elif isinstance(v, float) and not isinstance(v, bool):
        errors.append(f"{path}: not a finite number")          # NaN/Infinity are not JSON

    if isinstance(v, str):
        if "minLength" in node and len(v) < node["minLength"]:
            errors.append(f"{path}: shorter than minLength")
        if "maxLength" in node and len(v) > node["maxLength"]:
            errors.append(f"{path}: longer than maxLength")
        if "pattern" in node and not re.search(node["pattern"], v):
            errors.append(f"{path}: does not match the pattern")

    if isinstance(v, list):
        if "minItems" in node and len(v) < node["minItems"]:
            errors.append(f"{path}: fewer items than minItems")
        if "maxItems" in node and len(v) > node["maxItems"]:
            errors.append(f"{path}: more items than maxItems")
        if node.get("uniqueItems") and len({_canon(x) for x in v}) != len(v):
            errors.append(f"{path}: items are not unique")
        if "items" in node:
            for i, item in enumerate(v):
                _check(item, node["items"], root, f"{path}[{i}]", errors)

    if isinstance(v, dict):
        props = node.get("properties") or {}
        for name in node.get("required") or []:
            if name not in v:
                errors.append(f"{path}: missing required property {name!r}")
        if node.get("additionalProperties") is False:
            for name in v:
                if name not in props:
                    errors.append(f"{path}: unexpected property {str(name)[:40]!r}")
        for name, sub in props.items():
            if name in v:
                _check(v[name], sub, root, f"{path}.{name}", errors)

    if "anyOf" in node:
        branches = [_sub_errors(v, s, root, path) for s in node["anyOf"]]
        if all(branches):
            errors.append(f"{path}: matches none of anyOf")
            # The closest branch says what is actually wrong (for a nullable
            # field, that is the non-null branch).
            errors.extend(_closest(branches, path))
    if "oneOf" in node:
        branches = [_sub_errors(v, s, root, path) for s in node["oneOf"]]
        matched = sum(1 for b in branches if not b)
        if matched != 1:
            errors.append(f"{path}: matches {matched} of oneOf (exactly 1 required)")


def _closest(branches: list[list[str]], path: str) -> list[str]:
    """The failing branch most worth reporting: one whose type matched (its
    errors are about the content), then the one with the fewest errors."""
    def rank(errs: list[str]) -> tuple[bool, int]:
        return (len(errs) == 1 and errs[0].startswith(f"{path}: expected "), len(errs))
    return min(branches, key=rank)


def _sub_errors(v: Any, node: dict, root: dict, path: str) -> list[str]:
    out: list[str] = []
    _check(v, node, root, path, out)
    return out

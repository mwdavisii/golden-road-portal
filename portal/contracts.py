"""Validate documents against the schemas vendored from src/contracts/schemas (decision D4; a repo test keeps them equal).

Schemas that refer to each other resolve through a registry of the vendored files."""

import functools
import datetime as dt
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS = Path(__file__).resolve().parent / "schemas"


@functools.cache
def _registry() -> Registry:
    """Every vendored schema under its $id, so one can $ref another (re-review-event/v1 names $defs in scorecard/v1, scorecard/v2 and
    finding/v2) with no network lookup, as src/contracts/schema.py does. Only the referenced $defs are ever reached, so the schemas
    those three refer to in turn need not be vendored."""
    pairs = []
    for path in sorted(SCHEMAS.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        pairs.append((schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012)))
    return Registry().with_resources(pairs)


@functools.cache
def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=_registry(), format_checker=Draft202012Validator.FORMAT_CHECKER)


# `format: date-time` is checked only when an optional package is installed, so these fields are parsed here as well: the
# schema's pattern fixes the shape, this proves the instant exists (no month 99, no February 30).
DATE_TIME_FIELDS = {"intake.v2": ("approved_at", "floor_override.at"), "identity-link.v1": ("created_at",), "binding.v1": ("linked_at",),
                   "binding.v2": ("linked_at",), "re-review-event.v1": ("created",)}


def _at(document, dotted):
    for part in dotted.split("."):
        document = document.get(part) if isinstance(document, dict) else None
    return document


def _bad_instants(name: str, document) -> list[str]:
    errors = []
    for field in DATE_TIME_FIELDS.get(name, ()):
        value = _at(document, field)
        if isinstance(value, str):
            try:
                dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                errors.append(f"{field}: {value!r} is not a real UTC date and time")
    return errors


# JSON Schema can't say "unique by one key", so these are checked here: each name is a list of objects whose key must not repeat.
UNIQUE_KEYS = {"binding.v2": (("secrets", "env", "secrets: env names must be unique"),)}


def _repeats(name: str, document) -> list[str]:
    errors = []
    for field, key, message in UNIQUE_KEYS.get(name, ()):
        items = document.get(field) if isinstance(document, dict) else None
        keys = [i.get(key) for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        if len(keys) != len(set(map(repr, keys))):
            errors.append(message)
    return errors


def validate(name: str, document) -> list[str]:
    # A $ref to a schema that isn't vendored (scorecard/v1 names assessment/v1, for one) raises referencing's Unresolvable: the
    # registry has no retrieve, so nothing is ever fetched, and validating a document that would need it raises instead of passing. Only the
    # $defs that re-review-event/v1 uses are reached from the vendored scorecard schemas (a test pins both).
    errors = sorted(_validator(name).iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors] + _bad_instants(name, document) + _repeats(name, document)

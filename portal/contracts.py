"""Validate documents against the schemas vendored from src/contracts/schemas (decision D4; a repo test keeps them equal)."""

import functools
import datetime as dt
import json
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMAS = Path(__file__).resolve().parent / "schemas"


@functools.cache
def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


# `format: date-time` is checked only when an optional package is installed, so these fields are parsed here as well: the
# schema's pattern fixes the shape, this proves the instant exists (no month 99, no February 30).
DATE_TIME_FIELDS = {"intake.v2": ("approved_at", "floor_override.at"), "identity-link.v1": ("created_at",)}


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


def validate(name: str, document) -> list[str]:
    errors = sorted(_validator(name).iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path])
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors] + _bad_instants(name, document)

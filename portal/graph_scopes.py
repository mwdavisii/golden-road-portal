"""The platform's Microsoft Graph application permissions (spec 2026-11-16 S1). mock_graph_scopes.json (mock-graph-scopes/v1) lists each
one an app may ask for, with its tier: approved (a service principal with only these is created with no person in the loop) or
requestable (any of them turns the create into a ServiceNow request; its risk sentence is shown). The platform owns the file, a reviewed
change; the form can't send a scope that isn't listed. The web loads it with the directory (PORTAL_DIRECTORY=mock); the collector reads it
for the task's description."""

import dataclasses as dc
import json
import unicodedata
from pathlib import Path

from .store.base import SCOPE_ID

MOCK = Path(__file__).resolve().parent / "mock_graph_scopes.json"
TIERS = ("approved", "requestable")
MAX_LABEL, MAX_TEXT = 80, 300
KEYS = {"id", "label", "description", "tier"}


class CatalogError(ValueError):
    pass


@dc.dataclass(frozen=True)
class Scope:
    id: str
    label: str
    description: str
    tier: str  # approved or requestable
    risk: str | None = None  # a requestable scope's one sentence; None for an approved one


@dc.dataclass(frozen=True)
class Catalog:
    scopes: tuple[Scope, ...]

    def scope(self, scope_id) -> Scope | None:
        return next((s for s in self.scopes if s.id == scope_id), None)

    @property
    def approved(self) -> tuple[Scope, ...]:
        return tuple(s for s in self.scopes if s.tier == "approved")

    @property
    def requestable(self) -> tuple[Scope, ...]:
        return tuple(s for s in self.scopes if s.tier == "requestable")


def _line(value, limit, what):
    if (not isinstance(value, str) or not 1 <= len(value) <= limit or value.strip() != value
            or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value)):
        raise CatalogError(f"the scope catalog's {what} must be one line of 1 to {limit} characters")
    return value


def parse(doc) -> Catalog:
    if not isinstance(doc, dict) or set(doc) != {"schema", "scopes"}:
        raise CatalogError("the scope catalog has missing or unknown keys")
    if doc["schema"] != "mock-graph-scopes/v1":
        raise CatalogError("the scope catalog is not a mock-graph-scopes/v1")
    if not isinstance(doc["scopes"], list) or not doc["scopes"]:
        raise CatalogError("the scope catalog lists no scopes")
    scopes = []
    for s in doc["scopes"]:
        if not isinstance(s, dict) or not KEYS <= set(s) or set(s) - KEYS - {"risk"}:
            raise CatalogError("a scope in the catalog has missing or unknown keys")
        if not isinstance(s["id"], str) or not SCOPE_ID.fullmatch(s["id"]):
            raise CatalogError("a scope's id must be a Graph permission's name, such as Calendars.Read")
        if s["tier"] not in TIERS:
            raise CatalogError(f"{s['id']}'s tier must be approved or requestable")
        if (s["tier"] == "requestable") != ("risk" in s):
            raise CatalogError(f"{s['id']}: a requestable scope says its risk, and only a requestable one")
        scopes.append(Scope(s["id"], _line(s["label"], MAX_LABEL, "label"), _line(s["description"], MAX_TEXT, "description"), s["tier"],
                            _line(s["risk"], MAX_TEXT, "risk") if "risk" in s else None))
    ids = [s.id.lower() for s in scopes]
    if len(set(ids)) != len(ids):
        raise CatalogError("the scope catalog lists a scope twice")
    return Catalog(tuple(scopes))


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise CatalogError("the scope catalog names a key twice in one object")
    return dict(pairs)


def load_mock() -> Catalog:
    try:
        return parse(json.loads(MOCK.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys))
    except CatalogError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise CatalogError(f"the scope catalog can't be loaded: {type(exc).__name__}") from None

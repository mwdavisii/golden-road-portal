"""The directory the app page picks from (spec 2026-10-29 app bindings, B2/B3). Only a mock exists: mock_directory.json stands in
for what security's process (service principals, each tagged for the repo it was made for) and analytics' process (SQL warehouses
granted to principals) produce. The real directory answers the same two questions, principals_for and warehouses_for, from Azure
and Databricks. Repos are named without the org: they live under the portal's own org."""

import dataclasses as dc
import json
import re
import unicodedata
from pathlib import Path

MOCK = Path(__file__).resolve().parent / "mock_directory.json"
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ENTRY_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
HOST = re.compile(r"adb-[0-9]{1,20}\.[0-9]{1,4}\.azuredatabricks\.net")
HTTP_PATH = re.compile(r"/sql/1\.0/warehouses/[A-Za-z0-9]{1,64}")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
MAX_NAME = 200


class DirectoryError(ValueError):
    pass


@dc.dataclass(frozen=True)
class Principal:
    id: str
    display_name: str
    client_id: str
    tagged_repo_name: str


@dc.dataclass(frozen=True)
class Warehouse:
    id: str
    display_name: str
    host: str
    http_path: str
    catalog: str | None
    schema: str | None
    granted_to: tuple[str, ...]


@dc.dataclass(frozen=True)
class Directory:
    name: str  # what binding/v1 records as verified_by
    tenant_id: str
    principals: tuple[Principal, ...]
    warehouses: tuple[Warehouse, ...]

    def principals_for(self, org: str, repo: str) -> list[Principal]:
        """The principals tagged for this repo: only those under the portal's org."""
        return [p for p in self.principals if f"{org}/{p.tagged_repo_name}".lower() == repo.lower()]

    def warehouses_for(self, principal_id: str) -> list[Warehouse]:
        return [w for w in self.warehouses if principal_id in w.granted_to]

    def principal(self, principal_id: str) -> Principal | None:
        return next((p for p in self.principals if p.id == principal_id), None)

    def warehouse(self, warehouse_id: str) -> Warehouse | None:
        return next((w for w in self.warehouses if w.id == warehouse_id), None)


def _text(value, pattern, what):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DirectoryError(f"the directory's {what} is not valid")
    return value


def _name(value, what):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NAME or any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
        raise DirectoryError(f"the directory's {what} must be 1 to {MAX_NAME} characters with no control or format characters")
    return value


def _keys(entry, required, optional, what):
    if not isinstance(entry, dict) or not required <= set(entry) or set(entry) - required - optional:
        raise DirectoryError(f"the directory's {what} has missing or unknown keys")


def parse(doc) -> Directory:
    _keys(doc, {"schema", "tenant_id", "principals", "databricks"}, set(), "document")
    if doc["schema"] != "mock-directory/v1":
        raise DirectoryError("the directory is not a mock-directory/v1")
    principals, warehouses = [], []
    for p in doc["principals"] if isinstance(doc["principals"], list) else [None]:
        _keys(p, {"id", "display_name", "client_id", "tagged_repo_name"}, set(), "principal")
        principals.append(Principal(_text(p["id"], ENTRY_ID, "principal id"), _name(p["display_name"], "principal name"),
                                    _text(p["client_id"], GUID, "client id"), _text(p["tagged_repo_name"], REPO_NAME, "tagged repo name")))
    ids = [p.id for p in principals]
    if len({p.client_id for p in principals}) != len(principals):
        raise DirectoryError("two principals in the directory share a client id")
    for w in doc["databricks"] if isinstance(doc["databricks"], list) else [None]:
        _keys(w, {"id", "display_name", "host", "http_path", "granted_to"}, {"catalog", "schema"}, "warehouse")
        granted = w["granted_to"]
        if not isinstance(granted, list) or not granted or not all(isinstance(g, str) for g in granted) or not set(granted) <= set(ids):
            raise DirectoryError("a warehouse must be granted to principals the directory lists")
        warehouses.append(Warehouse(_text(w["id"], ENTRY_ID, "warehouse id"), _name(w["display_name"], "warehouse name"),
                                    _text(w["host"], HOST, "workspace host"), _text(w["http_path"], HTTP_PATH, "HTTP path"),
                                    _text(w["catalog"], IDENTIFIER, "catalog") if "catalog" in w else None,
                                    _text(w["schema"], IDENTIFIER, "schema") if "schema" in w else None, tuple(granted)))
    all_ids = ids + [w.id for w in warehouses]
    if len(set(all_ids)) != len(all_ids):
        raise DirectoryError("the directory lists an id twice")
    return Directory("mock-directory", _text(doc["tenant_id"], GUID, "tenant id"), tuple(principals), tuple(warehouses))


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise DirectoryError("the directory names a key twice in one object")
    return dict(pairs)


def load_mock() -> Directory:
    try:
        return parse(json.loads(MOCK.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys))
    except (OSError, ValueError) as exc:
        raise DirectoryError(f"the mock directory can't be loaded: {exc}") from None

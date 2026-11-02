"""The mock ServiceNow (spec 2026-10-31 Part 3, "The mock"): an httpx.MockTransport that answers exactly the requests in the client's
table (portal.servicenow), in ServiceNow's REST API response shapes, over the dummy data in mock_servicenow.json.

- Two users, found by email in any case. Basic auth as the integration user with PASSWORD is accepted; anything else is 401.
- An in-memory table of business applications keyed by correlation_id, as the class's identification rule would be: the first
  reconcile of a correlation_id is an INSERT with a new sys_id (the first 32 hex of the SHA-256 of the correlation id) and the next
  number (APM0001001 and up), the same values again are NO_CHANGE, other values an UPDATE of the same CI.
- seed() loads the CIs the portal recorded (store.cmdb_records()) at collector start, so a restart keeps their sys_ids and numbers, as
  the real instance would. The portal keeps no copy of the values it sent, so a CI seeded without them answers its first reconcile
  with an UPDATE (the real engine compares the stored attributes) and NO_CHANGE after that.
- Anything outside the table answers 404: another method, path, table or host, and a CI the table doesn't hold."""

import base64
import hashlib
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .servicenow import CLASS, DATA_SOURCE, EMAIL, SYS_ID, Client, email_ok

MOCK = Path(__file__).resolve().parent / "mock_servicenow.json"
PASSWORD = "mock-servicenow-password"  # the mock's integration user's; the real one is in Secret Server
INSTANCE = re.compile(r"https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # no ":" (basic auth splits there)
PREFIX = re.compile(r"[A-Z]{2,8}")
NUMBER = re.compile(r"[A-Z]{2,8}[0-9]{4,12}")
CORRELATION = re.compile(r"[\x21-\x7e]{1,200}")
HASH = re.compile(r"[0-9a-f]{64}")
MAX_NUMBER = 9_999_999  # numbers are f"{prefix}{n:07d}"
DOC_KEYS = {"schema", "instance", "integration_user", "number_prefix", "first_number", "users"}
USER_KEYS = {"sys_id", "user_name", "email"}
TABLE = "/api/now/table"


class MockServiceNowError(ValueError):
    pass


def values_hash(values) -> str:
    """What the mock compares to answer NO_CHANGE: the SHA-256 of the values' canonical JSON."""
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _need(ok, what):
    if not ok:
        raise MockServiceNowError(f"the mock ServiceNow's {what} is not valid")


def parse(doc) -> "MockServiceNow":
    _need(isinstance(doc, dict) and set(doc) == DOC_KEYS, "document (missing or unknown keys)")
    _need(doc["schema"] == "mock-servicenow/v1", "schema")
    _need(isinstance(doc["instance"], str) and INSTANCE.fullmatch(doc["instance"]) is not None, "instance (an https:// host, no path)")
    _need(isinstance(doc["integration_user"], str) and USER.fullmatch(doc["integration_user"]) is not None, "integration_user")
    _need(isinstance(doc["number_prefix"], str) and PREFIX.fullmatch(doc["number_prefix"]) is not None, "number_prefix")
    first = doc["first_number"]
    _need(type(first) is int and 1 <= first <= MAX_NUMBER, "first_number (a whole number from 1)")
    _need(isinstance(doc["users"], list), "users")
    users = []
    for u in doc["users"]:
        _need(isinstance(u, dict) and set(u) == USER_KEYS, "user (missing or unknown keys)")
        _need(isinstance(u["sys_id"], str) and SYS_ID.fullmatch(u["sys_id"]) is not None, "user's sys_id")
        _need(isinstance(u["user_name"], str) and USER.fullmatch(u["user_name"]) is not None, "user's user_name")
        _need(email_ok(u["email"]) and EMAIL.fullmatch(u["email"]) is not None, "user's email")
        users.append(dict(u))
    for key in ("sys_id", "user_name", "email"):
        values = [u[key].lower() for u in users]
        _need(len(set(values)) == len(values), f"users (a {key} named twice)")
    return MockServiceNow(doc["instance"], doc["integration_user"], doc["number_prefix"], first, tuple(users))


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise MockServiceNowError("the mock ServiceNow names a key twice in one object")
    return dict(pairs)


def load_mock() -> "MockServiceNow":
    try:
        return parse(json.loads(MOCK.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys))
    except MockServiceNowError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise MockServiceNowError(f"the mock ServiceNow can't be loaded: {type(exc).__name__}") from None


def _error(status, message, detail=""):
    """ServiceNow's REST error shape."""
    return httpx.Response(status, json={"error": {"message": message, "detail": detail}, "status": "failure"})


class MockServiceNow:
    name = "mock-servicenow"

    def __init__(self, instance, integration_user, number_prefix, first_number, users):
        self.instance, self.integration_user, self.number_prefix = instance, integration_user, number_prefix
        self.first_number, self.users = first_number, users
        self.host = urlsplit(instance).hostname
        self._lock = threading.Lock()
        self._cis = {}  # correlation_id -> {"sys_id", "number", "hash" (None: values not known)}
        self._next = first_number

    def client(self) -> Client:
        return Client(self.instance, http=httpx.Client(transport=self.transport()), auth=(self.integration_user, PASSWORD))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def seed(self, records) -> None:
        """records: (correlation_id, sys_id, number, values_hash or None) for each CI the portal recorded. All or nothing."""
        cis, seen = {}, set()
        for record in records:
            if not (isinstance(record, (tuple, list)) and len(record) == 4):
                raise MockServiceNowError("a seeded CI is (correlation_id, sys_id, number, values_hash)")
            correlation, sys_id, number, digest = record
            if not (isinstance(correlation, str) and CORRELATION.fullmatch(correlation) and isinstance(sys_id, str) and SYS_ID.fullmatch(sys_id)
                    and isinstance(number, str) and NUMBER.fullmatch(number) and (digest is None or (isinstance(digest, str) and HASH.fullmatch(digest)))):
                raise MockServiceNowError("a seeded CI names its correlation_id, a 32-hex sys_id, its number and its values hash or none")
            if correlation in cis or sys_id in seen or number in {c["number"] for c in cis.values()}:
                raise MockServiceNowError("a seeded CI is named twice")
            cis[correlation] = {"sys_id": sys_id, "number": number, "hash": digest}
            seen.add(sys_id)
        with self._lock:
            self._cis.update(cis)
            ours = [int(c["number"][len(self.number_prefix):]) for c in self._cis.values() if c["number"].startswith(self.number_prefix)
                    and c["number"][len(self.number_prefix):].isascii() and c["number"][len(self.number_prefix):].isdigit()]
            self._next = max([self._next, *(n + 1 for n in ours)])

    # ---- the transport ----

    def _authorized(self, request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return given == f"{self.integration_user}:{PASSWORD}"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if request.url.host != self.host:
            return _error(404, "Requested URI does not represent any resource")
        if method == "GET" and path == f"{TABLE}/sys_user":
            route = self._users
        elif method == "POST" and path == "/api/now/identifyreconcile":
            route = self._reconcile
        elif method == "GET" and path.startswith(f"{TABLE}/{CLASS}/") and SYS_ID.fullmatch(path[len(f"{TABLE}/{CLASS}/"):]):
            route = self._ci
        else:
            return _error(404, "Requested URI does not represent any resource")
        if not self._authorized(request):
            return _error(401, "User Not Authenticated", "Required to provide Auth information")
        return route(request)

    def _users(self, request):
        query = request.url.params.get("sysparm_query", "")
        email = query[len("email="):] if query.startswith("email=") else None
        if email is None or not email_ok(email):
            return _error(400, "the mock answers one query: email=<one address>")
        limit = request.url.params.get("sysparm_limit", "10000")
        if not (limit.isascii() and limit.isdigit() and int(limit) >= 1):
            return _error(400, "sysparm_limit must be a whole number from 1")
        found = [dict(u) for u in self.users if u["email"].lower() == email.lower()][:int(limit)]
        return httpx.Response(200, json={"result": found})

    def _reconcile(self, request):
        if request.url.params.get("sysparm_data_source") != DATA_SOURCE:
            return _error(400, f"the mock reconciles only the {DATA_SOURCE} data source")
        try:
            body = json.loads(request.content)
        except ValueError:
            return _error(400, "the body is not JSON")
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
            return _error(400, "the body names no items")
        return httpx.Response(200, json={"result": {"items": [self._item(i) for i in items], "relations": []}})

    def _item(self, item):
        cls, values = item.get("className"), item.get("values")
        if cls != CLASS:
            return {"className": cls, "errors": [{"error": "INVALID_CLASS_NAME", "message": "the mock holds only business applications"}]}
        correlation = values.get("correlation_id") if isinstance(values, dict) else None
        if not isinstance(correlation, str) or not CORRELATION.fullmatch(correlation):
            return {"className": cls, "errors": [{"error": "MISSING_MATCHING_ATTRIBUTES",
                                                  "message": "no identification rule matched: correlation_id is required"}]}
        digest = values_hash(values)
        with self._lock:
            ci = self._cis.get(correlation)
            if ci is None:
                if self._next > MAX_NUMBER:
                    return {"className": cls, "errors": [{"error": "NUMBER_EXHAUSTED", "message": "the mock has no numbers left"}]}
                ci = {"sys_id": hashlib.sha256(correlation.encode()).hexdigest()[:32], "number": f"{self.number_prefix}{self._next:07d}",
                      "hash": digest}
                self._next += 1
                self._cis[correlation], operation = ci, "INSERT"
            elif ci["hash"] == digest:
                operation = "NO_CHANGE"
            else:
                ci["hash"], operation = digest, "UPDATE"
        return {"className": cls, "operation": operation, "sysId": ci["sys_id"],
                "identifierEntrySysId": hashlib.sha256(f"identifier:{correlation}".encode()).hexdigest()[:32]}

    def _ci(self, request):
        sys_id = request.url.path.rsplit("/", 1)[1]
        with self._lock:
            ci = next((c for c in self._cis.values() if c["sys_id"] == sys_id), None)
        if ci is None:
            return _error(404, "No Record found", "Record doesn't exist or ACL restricts the record retrieval")
        return httpx.Response(200, json={"result": {"number": ci["number"], "sys_id": ci["sys_id"]}})

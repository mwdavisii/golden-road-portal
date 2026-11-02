"""A ServiceNow CMDB client for the collector's CMDB part (spec 2026-10-31 Part 3), written against ServiceNow's REST API: the Table API
(GET /api/now/table/sys_user, GET /api/now/table/cmdb_ci_business_app/{sys_id}) and the Identification and Reconciliation API
(POST /api/now/identifyreconcile). It authenticates with basic auth as the platform's integration user (its password is in Secret
Server; the mock's is a constant). In the mock its transport is portal.servicenow_mock. Against a real instance, confirm the marked
details against the instance's REST API Explorer, the class's identification rule on correlation_id, and a reconciliation rule for the
golden_road data source. No error message carries the credentials or the instance's own text."""

import dataclasses as dc
import re
import unicodedata

import httpx

CLASS = "cmdb_ci_business_app"
DATA_SOURCE = "golden_road"
TIMEOUT = 10.0  # seconds, for connect, read, write and pool alike
OPERATIONS = ("INSERT", "UPDATE", "NO_CHANGE")
SYS_ID = re.compile(r"[0-9a-f]{32}")
NUMBER = re.compile(r"[A-Z]{2,8}[0-9]{4,12}")
ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")  # the IRE's codes, e.g. MISSING_MATCHING_ATTRIBUTES; anything else is UNKNOWN
# One address and nothing an encoded query reads as an operator or a script: an ASCII atext local part without quotes, parentheses, ":",
# "^", "," or "=" (^ joins conditions, = and , are operators' parts, and "javascript:" is evaluated in an encoded query), and a host name
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&*+/?_{|}~-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
MAX_EMAIL = 254
BASE = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/?")
CREDENTIAL = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no space; the user may not hold ":" (basic auth splits there)


class ServiceNowError(Exception):
    pass


@dc.dataclass(frozen=True)
class Reconciled:
    sys_id: str | None  # the CI's; None only when the item came back with errors and no sysId
    operation: str | None  # INSERT, UPDATE or NO_CHANGE; None only with errors
    errors: tuple  # the item's error codes; () when it reconciled


def email_ok(email) -> bool:
    return (isinstance(email, str) and len(email) <= MAX_EMAIL and EMAIL.fullmatch(email) is not None
            and not email.casefold().startswith("javascript")
            and not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in email))


class Client:
    def __init__(self, base: str, *, http: httpx.Client, auth: tuple):
        if not isinstance(base, str) or not BASE.fullmatch(base):
            raise ServiceNowError("the ServiceNow instance must be an https:// URL with no path")
        if (not isinstance(auth, tuple) or len(auth) != 2 or not all(isinstance(a, str) and CREDENTIAL.fullmatch(a) for a in auth)
                or ":" in auth[0]):
            raise ServiceNowError("the ServiceNow credentials must be the integration user and its password (printable ASCII)")
        self.base, self.http, self._auth = base.rstrip("/"), http, auth

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Close the httpx client this one sends through."""
        close = getattr(self.http, "close", None)
        if callable(close):
            close()

    def _send(self, method, path, what, *, params, json=None):
        """The answer's JSON body for a 200, else ServiceNowError naming the status and what was asked: never the instance's text,
        never the request (it carries the credentials). Never redirected."""
        failure = None
        try:
            r = self.http.request(method, f"{self.base}{path}", params=params, json=json, auth=self._auth,
                                  headers={"Accept": "application/json"}, timeout=TIMEOUT, follow_redirects=False)
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:  # never the exception's text, nor the exception itself
            failure = type(exc).__name__
        if failure is not None:  # raised outside the except block, so the httpx exception is not its __context__
            raise ServiceNowError(f"ServiceNow could not be reached for {what}: {failure}")
        if r.status_code != 200:
            raise ServiceNowError(f"ServiceNow answered {r.status_code} for {what}")
        try:
            return r.json()
        except ValueError:
            raise ServiceNowError(f"ServiceNow's answer for {what} is not JSON") from None

    def user_sys_id(self, email: str) -> str | None:
        """The sys_id of the one ServiceNow user with this email, or None when no user or more than one has it, or the one user the
        query matched has another email. An email that isn't one plain address (a query operator or a script in it) is refused before any
        request."""
        if not email_ok(email):
            raise ServiceNowError("an owner's email must be one address, with no query operator, space or control character")
        what = "the owner's user"
        doc = self._send("GET", "/api/now/table/sys_user", what,
                         params={"sysparm_query": f"email={email}", "sysparm_fields": "sys_id,user_name,email", "sysparm_limit": "2"})
        result = doc.get("result") if isinstance(doc, dict) else None
        if not isinstance(result, list) or not all(isinstance(u, dict) for u in result):
            raise ServiceNowError(f"ServiceNow's answer for {what} is not a list of users")
        if len(result) != 1:
            return None
        found = result[0].get("email")
        if not isinstance(found, str) or found.casefold() != email.casefold():
            return None  # whatever the query matched, it is not the user with this email: the owner is not known
        sys_id = result[0].get("sys_id")
        if not isinstance(sys_id, str) or not SYS_ID.fullmatch(sys_id):
            raise ServiceNowError(f"ServiceNow's answer for {what} has no valid sys_id")
        return sys_id

    def reconcile(self, values: dict) -> Reconciled:
        """Create or update the business application identified by values["correlation_id"], as the golden_road data source.
        confirm against the instance: some versions answer {"result": {"items": ...}}, others {"items": ...}; both are read."""
        what = "the business application"
        doc = self._send("POST", "/api/now/identifyreconcile", what, params={"sysparm_data_source": DATA_SOURCE},
                         json={"items": [{"className": CLASS, "values": dict(values)}]})
        body = doc.get("result") if isinstance(doc, dict) and isinstance(doc.get("result"), dict) else doc
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ServiceNowError(f"ServiceNow's answer for {what} does not have the one item sent")
        item = items[0]
        errors = item.get("errors", [])
        if not isinstance(errors, list) or not all(isinstance(e, dict) for e in errors):
            raise ServiceNowError(f"ServiceNow's answer for {what} has errors of the wrong shape")
        if item.get("className", CLASS) != CLASS:
            raise ServiceNowError(f"ServiceNow's answer for {what} is for another class")
        codes = tuple(e["error"] if isinstance(e.get("error"), str) and ERROR_CODE.fullmatch(e["error"]) else "UNKNOWN" for e in errors)
        sys_id, operation = item.get("sysId"), item.get("operation")
        if codes:
            return Reconciled(sys_id if isinstance(sys_id, str) and SYS_ID.fullmatch(sys_id) else None,
                              operation if operation in OPERATIONS else None, codes)
        if operation not in OPERATIONS or not isinstance(sys_id, str) or not SYS_ID.fullmatch(sys_id):
            raise ServiceNowError(f"ServiceNow's answer for {what} has no valid operation and sysId")
        return Reconciled(sys_id, operation, ())

    def number(self, sys_id: str) -> str:
        """The CI's number (APM0001001), for the app page."""
        if not isinstance(sys_id, str) or not SYS_ID.fullmatch(sys_id):
            raise ServiceNowError("a CI's sys_id is 32 lowercase hex characters")
        what = "the business application's number"
        doc = self._send("GET", f"/api/now/table/{CLASS}/{sys_id}", what, params={"sysparm_fields": "number,sys_id"})
        result = doc.get("result") if isinstance(doc, dict) else None
        if not isinstance(result, dict) or result.get("sys_id") != sys_id or not isinstance(result.get("number"), str) \
                or not NUMBER.fullmatch(result["number"]):
            raise ServiceNowError(f"ServiceNow's answer for {what} has no valid number")
        return result["number"]

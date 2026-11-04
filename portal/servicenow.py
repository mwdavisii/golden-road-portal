"""A ServiceNow CMDB client for the collector's CMDB part (spec 2026-10-31 Part 3), written against ServiceNow's REST API: the Table API
(GET /api/now/table/sys_user, GET /api/now/table/cmdb_ci_business_app/{sys_id}) and the Identification and Reconciliation API
(POST /api/now/identifyreconcile) and, for the lifecycle (spec 2026-11-02), the Table API's sc_task (GET by number; PATCH for a work
note and for the close) and the CI's lifecycle fields. It authenticates with basic auth as the platform's integration user (its
password is in Secret Server; the mock's is a constant). In the mock its transport is portal.servicenow_mock. Against a real instance,
confirm the marked details against the instance's REST API Explorer, the class's identification rule on correlation_id, and a
reconciliation rule for the golden_road data source. No error message carries the credentials or the instance's own text."""

import dataclasses as dc
import json as _json
import re
import time
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
TASK_TABLE = "sc_task"
TASK_NUMBER = re.compile(r"SCTASK[0-9]{7}")
OPEN_TASK_STATES = ("1", "2")  # sc_task's Open and Work in Progress; 3 is Closed Complete (confirm the choice list on the instance)
CLOSED_COMPLETE = "3"
CHOICE = re.compile(r"-?[0-9]{1,4}")  # a choice field's value, as the Table API answers it (a string)
# The CI's lifecycle fields (spec 2026-11-02 "ServiceNow write-back"): the platform's own u_ fields to confirm with the ServiceNow admins
LIFECYCLE_FIELDS = ("operational_status", "install_status", "u_golden_road_state", "u_golden_road_task")
GOLDEN_ROAD_STATES = ("active", "suspended", "decommissioned")  # u_golden_road_state's values: the store's LIFECYCLE_STATES
UNEXPECTED = "unexpected"  # a lifecycle field the CI reports outside its values: never the instance's own text, never a portal value
MAX_ANSWER = 1024 * 1024  # bytes: a larger answer is refused, unread past the cap
ANSWER_DEADLINE = 30.0  # seconds for the whole answer (TIMEOUT bounds each read; a trickling body is cut off here)


class ServiceNowError(Exception):
    """status: the HTTP status ServiceNow answered with (not 200), or None when it didn't answer, its answer wasn't one the client reads,
    or the request was refused before it was sent."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class _TooLarge(Exception):
    pass


class _TooSlow(Exception):
    pass


def _read_capped(response) -> bytes:
    """The body. _TooLarge when it is (or says it is) larger than MAX_ANSWER: reading stops at the first chunk past the cap; _TooSlow
    when it is still arriving ANSWER_DEADLINE seconds after the answer began."""
    declared = response.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_ANSWER:
        raise _TooLarge
    start, chunks, size = time.monotonic(), [], 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_ANSWER:
            raise _TooLarge
        if time.monotonic() - start > ANSWER_DEADLINE:
            raise _TooSlow
        chunks.append(chunk)
    return b"".join(chunks)


@dc.dataclass(frozen=True)
class Reconciled:
    sys_id: str | None  # the CI's; None only when the item came back with errors and no sysId
    operation: str | None  # INSERT, UPDATE or NO_CHANGE; None only with errors
    errors: tuple  # the item's error codes; () when it reconciled


@dc.dataclass(frozen=True)
class Task:
    sys_id: str
    number: str
    state: str  # the choice value, a string ("1")
    ci: str | None  # cmdb_ci's sys_id; None when the task names no CI

    @property
    def open(self) -> bool:
        return self.state in OPEN_TASK_STATES


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
        never the request (it carries the credentials). Never redirected. The body is streamed and refused past MAX_ANSWER bytes (a
        declared Content-Length over it is refused unread) or still arriving after ANSWER_DEADLINE, so a huge or slow answer can't hold
        the worker."""
        failure, status, body = None, None, None
        try:
            with self.http.stream(method, f"{self.base}{path}", params=params, json=json, auth=self._auth,
                                  headers={"Accept": "application/json"}, timeout=TIMEOUT, follow_redirects=False) as r:
                status = r.status_code
                if status == 200:
                    body = _read_capped(r)
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:  # never the exception's text, nor the exception itself
            failure = f"ServiceNow could not be reached for {what}: {type(exc).__name__}"
        except _TooLarge:
            failure = f"ServiceNow's answer for {what} is larger than {MAX_ANSWER} bytes"
        except _TooSlow:
            failure = f"ServiceNow's answer for {what} took longer than {ANSWER_DEADLINE:g} seconds"
        if failure is not None:  # raised outside the except block, so the httpx exception is not its __context__
            raise ServiceNowError(failure)
        if status != 200:
            raise ServiceNowError(f"ServiceNow answered {status} for {what}", status=status)
        try:
            return _json.loads(body)
        except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested too deep to parse
            pass
        raise ServiceNowError(f"ServiceNow's answer for {what} is not JSON")  # outside the except: no __context__

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

    def task(self, number: str) -> Task | None:
        """The sc_task with this number, or None. A number that isn't SCTASK and 7 digits is refused before any request (no query
        operator can ride along). cmdb_ci is a reference: {"link", "value"} or "" when empty (confirm sysparm_exclude_reference_link)."""
        if not isinstance(number, str) or not TASK_NUMBER.fullmatch(number):
            raise ServiceNowError("a task number is SCTASK followed by 7 digits")
        what = "the task"
        doc = self._send("GET", f"/api/now/table/{TASK_TABLE}", what, params={
            "sysparm_query": f"number={number}", "sysparm_fields": "sys_id,number,state,cmdb_ci", "sysparm_limit": "1"})
        result = doc.get("result") if isinstance(doc, dict) else None
        if not isinstance(result, list) or len(result) > 1 or not all(isinstance(r, dict) for r in result):
            raise ServiceNowError(f"ServiceNow's answer for {what} is not a list of at most one task")
        if not result:
            return None
        row = result[0]
        ci = row.get("cmdb_ci")
        value = ci.get("value") if isinstance(ci, dict) else ci
        if (row.get("number") != number or not isinstance(row.get("sys_id"), str) or not SYS_ID.fullmatch(row["sys_id"])
                or not isinstance(row.get("state"), str) or not CHOICE.fullmatch(row["state"])
                or not (value in ("", None) or (isinstance(value, str) and SYS_ID.fullmatch(value)))):
            raise ServiceNowError(f"ServiceNow's answer for {what} is not a task with a sys_id, a state and a CI or none")
        return Task(row["sys_id"], number, row["state"], value or None)

    def _patch_task(self, sys_id, body, what) -> None:
        if not isinstance(sys_id, str) or not SYS_ID.fullmatch(sys_id):
            raise ServiceNowError("a task's sys_id is 32 lowercase hex characters")
        doc = self._send("PATCH", f"/api/now/table/{TASK_TABLE}/{sys_id}", what, params={"sysparm_fields": "sys_id,number"}, json=body)
        result = doc.get("result") if isinstance(doc, dict) else None
        if not isinstance(result, dict) or result.get("sys_id") != sys_id:
            raise ServiceNowError(f"ServiceNow's answer for {what} is not the task")

    def add_work_note(self, sys_id: str, note: str) -> None:
        """One work note on the task (journal field work_notes: each PATCH appends one)."""
        self._patch_task(sys_id, {"work_notes": note}, "the task's work note")

    def close_task(self, sys_id: str, summary: str) -> None:
        """Close the task as Closed Complete with the summary as its close notes."""
        self._patch_task(sys_id, {"state": CLOSED_COMPLETE, "close_notes": summary}, "the task's close")

    def ci_lifecycle(self, sys_id: str) -> dict:
        """The CI's lifecycle fields it reports, {field: value} (a field it leaves out is not in the dict): rule 9 compares them with the
        portal's state. A value outside the field's own (a choice value; active, suspended or decommissioned; "" or a task number) is
        UNEXPECTED: still a difference, never the instance's own text."""
        if not isinstance(sys_id, str) or not SYS_ID.fullmatch(sys_id):
            raise ServiceNowError("a CI's sys_id is 32 lowercase hex characters")
        what = "the business application's lifecycle fields"
        doc = self._send("GET", f"/api/now/table/{CLASS}/{sys_id}", what, params={"sysparm_fields": "sys_id," + ",".join(LIFECYCLE_FIELDS)})
        result = doc.get("result") if isinstance(doc, dict) else None
        if not isinstance(result, dict) or result.get("sys_id") != sys_id:
            raise ServiceNowError(f"ServiceNow's answer for {what} is not the CI")
        return {k: result[k] if _lifecycle_value_ok(k, result[k]) else UNEXPECTED for k in LIFECYCLE_FIELDS if k in result}


def _lifecycle_value_ok(field, value) -> bool:
    """Each lifecycle field's own values (rule 9 may put them in a note, so nothing else is passed on as it is)."""
    if not isinstance(value, str):
        return False
    if field in ("operational_status", "install_status"):
        return CHOICE.fullmatch(value) is not None
    if field == "u_golden_road_state":
        return value in GOLDEN_ROAD_STATES
    return value == "" or TASK_NUMBER.fullmatch(value) is not None

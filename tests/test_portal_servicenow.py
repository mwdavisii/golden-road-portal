"""The ServiceNow client (spec 2026-10-31 Part 3): exactly the three requests in the spec's table, the real response shapes, and errors
that never carry the credentials or the instance's own text."""

import json

import httpx
import pytest

from portal.servicenow import CLASS, DATA_SOURCE, GOLDEN_ROAD_STATES, LIFECYCLE_FIELDS, MAX_ANSWER, UNEXPECTED, Client, ServiceNowError, Task
from portal.store.base import LIFECYCLE_STATES

BASE = "https://instance.service-now.example"
VALUES = {"name": "acme/notes", "short_description": "Stores team notes.", "owned_by": "a" * 32, "u_tier": "low",
          "u_data_classification": "public", "u_review_by": "2027-10-31", "u_repo_url": "https://github.com/acme/notes",
          "correlation_id": "github:acme/notes", "discovery_source": "golden_road"}
SECRET = "s3cret-integration-password"


def client(routes, seen):
    def handle(request):
        seen.append((request.method, request.url.path, dict(request.url.params), request.content, request.headers.get("authorization")))
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json={"error": {"message": "No Record found"}, "status": "failure"})
        status, body = routes[key](request) if callable(routes[key]) else routes[key]
        return httpx.Response(status, json=body)
    return Client(BASE, http=httpx.Client(transport=httpx.MockTransport(handle)), auth=("golden-road-cmdb", SECRET))


def test_the_owner_is_one_user_by_email():
    seen = []
    c = client({("GET", "/api/now/table/sys_user"): (200, {"result": [{"sys_id": "a" * 32, "user_name": "owner", "email": "owner@example.test"}]})}, seen)
    assert c.user_sys_id("owner@example.test") == "a" * 32
    assert seen[0][2] == {"sysparm_query": "email=owner@example.test", "sysparm_fields": "sys_id,user_name,email", "sysparm_limit": "2"}
    assert seen[0][4].startswith("Basic ")


@pytest.mark.parametrize("result", [[], [{"sys_id": "a" * 32}, {"sys_id": "b" * 32}]])
def test_none_or_two_users_is_no_owner(result):
    assert client({("GET", "/api/now/table/sys_user"): (200, {"result": result})}, []).user_sys_id("x@example.test") is None


@pytest.mark.parametrize("email", ["a@example.test^ORemail=b@example.test", "a@example.test,b@example.test", "a b@example.test",
                                   "a@example.test\n", "nobody", "", None, "a@b@example.test", "a\u200b@example.test",
                                   "javascript:'victim'+String.fromCharCode(64)+'example.org'//@x.io", "JavaScript%3Ax@x.io",
                                   '"quoted"@example.test', "a(comment)@example.test", "a:b@example.test", "ü@example.test",
                                   "a@exa_mple.test"])
def test_an_email_with_a_query_operator_is_never_sent(email):
    seen = []
    with pytest.raises(ServiceNowError):
        client({}, seen).user_sys_id(email)
    assert seen == []


@pytest.mark.parametrize("found", [{"sys_id": "a" * 32, "email": "victim@example.org"}, {"sys_id": "a" * 32},
                                   {"sys_id": "a" * 32, "email": None}, {"sys_id": "a" * 32, "email": "x@example.test.evil"}])
def test_a_user_whose_email_is_not_the_one_asked_for_is_no_owner(found):
    assert client({("GET", "/api/now/table/sys_user"): (200, {"result": [found]})}, []).user_sys_id("x@example.test") is None


def test_the_email_is_matched_in_any_case():
    got = client({("GET", "/api/now/table/sys_user"): (200, {"result": [{"sys_id": "a" * 32, "email": "Owner@Example.TEST"}]})}, [])
    assert got.user_sys_id("owner@example.test") == "a" * 32


@pytest.mark.parametrize("body", [{"result": [{"sys_id": "not-hex", "email": "x@example.test"}]}, {"result": {"sys_id": "a" * 32}}, [], {"other": []}])
def test_a_malformed_user_answer_is_an_error(body):
    with pytest.raises(ServiceNowError):
        client({("GET", "/api/now/table/sys_user"): (200, body)}, []).user_sys_id("x@example.test")


def test_reconcile_posts_one_business_app_and_reads_the_operation():
    seen = []
    body = {"items": [{"className": CLASS, "operation": "INSERT", "sysId": "c" * 32, "identifierEntrySysId": "d" * 32}]}
    c = client({("POST", "/api/now/identifyreconcile"): (200, {"result": body})}, seen)
    got = c.reconcile(VALUES)
    assert (got.sys_id, got.operation, got.errors) == ("c" * 32, "INSERT", ())
    assert seen[0][2] == {"sysparm_data_source": DATA_SOURCE}
    assert json.loads(seen[0][3]) == {"items": [{"className": "cmdb_ci_business_app", "values": VALUES}]}


def test_reconcile_reads_an_answer_without_result_too():
    body = {"items": [{"className": CLASS, "operation": "UPDATE", "sysId": "c" * 32}]}
    got = client({("POST", "/api/now/identifyreconcile"): (200, body)}, []).reconcile(VALUES)
    assert (got.sys_id, got.operation) == ("c" * 32, "UPDATE")


@pytest.mark.parametrize("item", [{"className": CLASS, "operation": "DELETE", "sysId": "c" * 32},
                                  {"className": CLASS, "operation": "INSERT", "sysId": "short"},
                                  {"className": CLASS, "operation": "INSERT"},
                                  {"className": "cmdb_ci_server", "operation": "INSERT", "sysId": "c" * 32},
                                  {"className": CLASS, "operation": "INSERT", "sysId": "c" * 32, "errors": "x"}])
def test_a_malformed_reconcile_item_is_an_error(item):
    with pytest.raises(ServiceNowError):
        client({("POST", "/api/now/identifyreconcile"): (200, {"result": {"items": [item]}})}, []).reconcile(VALUES)


@pytest.mark.parametrize("body", [{"result": {"items": []}}, {"result": {}}, {"result": []}, []])
def test_an_answer_without_one_item_is_an_error(body):
    with pytest.raises(ServiceNowError):
        client({("POST", "/api/now/identifyreconcile"): (200, body)}, []).reconcile(VALUES)


def test_item_errors_are_kept_and_the_number_is_read():
    errs = {"items": [{"className": CLASS, "operation": "NO_CHANGE", "sysId": "c" * 32,
                       "errors": [{"error": "MISSING_MATCHING_ATTRIBUTES", "message": "x"}]}]}
    seen = []
    c = client({("POST", "/api/now/identifyreconcile"): (200, {"result": errs}),
                ("GET", f"/api/now/table/cmdb_ci_business_app/{'c' * 32}"): (200, {"result": {"number": "APM0001001", "sys_id": "c" * 32}})}, seen)
    assert c.reconcile(VALUES).errors == ("MISSING_MATCHING_ATTRIBUTES",)
    assert c.number("c" * 32) == "APM0001001"
    assert seen[-1][2] == {"sysparm_fields": "number,sys_id"}


def test_an_error_code_that_is_free_text_is_not_kept_as_it_is():
    errs = {"items": [{"className": CLASS, "errors": [{"error": "the instance's own words, password=hunter2", "message": "x"}]}]}
    got = client({("POST", "/api/now/identifyreconcile"): (200, {"result": errs})}, []).reconcile(VALUES)
    assert got.errors == ("UNKNOWN",) and got.sys_id is None


@pytest.mark.parametrize("body", [{"result": {"number": "apm1", "sys_id": "c" * 32}}, {"result": {"sys_id": "c" * 32}},
                                  {"result": {"number": "APM0001001", "sys_id": "d" * 32}}, {"result": []}])
def test_a_bad_number_is_an_error(body):
    with pytest.raises(ServiceNowError):
        client({("GET", f"/api/now/table/cmdb_ci_business_app/{'c' * 32}"): (200, body)}, []).number("c" * 32)


def test_a_bad_sys_id_is_never_sent():
    seen = []
    with pytest.raises(ServiceNowError):
        client({}, seen).number("../sys_user")
    assert seen == []


@pytest.mark.parametrize("status", [301, 401, 403, 404, 500, 503])
def test_errors_are_clear_and_carry_neither_the_credentials_nor_the_instance_s_text(status):
    body = {"error": {"message": f"Instance says: {SECRET}", "detail": "golden-road-cmdb"}, "status": "failure"}
    with pytest.raises(ServiceNowError) as err:
        client({("GET", "/api/now/table/sys_user"): (status, body)}, []).user_sys_id("x@example.test")
    assert str(status) in str(err.value) and SECRET not in str(err.value) and "Instance says" not in str(err.value)
    assert err.value.__context__ is None and err.value.__cause__ is None


def test_an_answer_that_is_not_json_is_an_error():
    c = Client(BASE, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>login</html>"))),
               auth=("u", SECRET))
    with pytest.raises(ServiceNowError, match="not JSON"):
        c.user_sys_id("x@example.test")


def test_an_unreachable_instance_is_named_by_type_only():
    def down(request):
        raise httpx.ConnectError(f"cannot connect to {request.url} with {SECRET}", request=request)
    c = Client(BASE, http=httpx.Client(transport=httpx.MockTransport(down)), auth=("u", SECRET))
    with pytest.raises(ServiceNowError) as err:
        c.reconcile(VALUES)
    assert "ConnectError" in str(err.value) and SECRET not in str(err.value) and err.value.__context__ is None


@pytest.mark.parametrize("base", ["http://instance.service-now.example", "ftp://x", "", None, "https://"])
def test_the_base_must_be_https(base):
    with pytest.raises(ServiceNowError):
        Client(base, http=httpx.Client(), auth=("u", "p"))


@pytest.mark.parametrize("auth", [("", "p"), ("u", ""), ("u",), "u:p", None, ("u:x", "p"), ("u", "p\n")])
def test_the_credentials_must_be_a_user_and_a_password(auth):
    with pytest.raises(ServiceNowError) as err:
        Client(BASE, http=httpx.Client(), auth=auth)
    assert "p\n" not in str(err.value)


def test_the_client_closes_its_http_client():
    http = httpx.Client()
    with Client(BASE, http=http, auth=("u", "p")):
        pass
    assert http.is_closed


TASK_ROW = {"sys_id": "5ca1ab1e000040008000000000010001", "number": "SCTASK0010001", "state": "1",
            "cmdb_ci": {"link": f"{BASE}/api/now/table/cmdb_ci/{'c' * 32}", "value": "c" * 32}}


def test_a_task_is_read_by_number_with_its_ci():
    seen = []
    c = client({("GET", "/api/now/table/sc_task"): (200, {"result": [TASK_ROW]})}, seen)
    assert c.task("SCTASK0010001") == Task("5ca1ab1e000040008000000000010001", "SCTASK0010001", "1", "c" * 32)
    assert seen[0][2] == {"sysparm_query": "number=SCTASK0010001", "sysparm_fields": "sys_id,number,state,cmdb_ci", "sysparm_limit": "1"}
    assert c.task("SCTASK0010001").open


@pytest.mark.parametrize("state, open_", [("1", True), ("2", True), ("3", False), ("4", False), ("7", False)])
def test_open_is_state_1_or_2(state, open_):
    assert Task("a" * 32, "SCTASK0010001", state, None).open is open_


def test_no_task_and_no_ci():
    assert client({("GET", "/api/now/table/sc_task"): (200, {"result": []})}, []).task("SCTASK0010009") is None
    row = {**TASK_ROW, "cmdb_ci": ""}
    assert client({("GET", "/api/now/table/sc_task"): (200, {"result": [row]})}, []).task("SCTASK0010001").ci is None


@pytest.mark.parametrize("number", ["SCTASK1", "SCTASK0010001^ORnumber=SCTASK0010002", "sctask0010001", "INC0010001"])
def test_a_bad_task_number_is_never_sent(number):
    seen = []
    with pytest.raises(ServiceNowError):
        client({}, seen).task(number)
    assert seen == []


@pytest.mark.parametrize("row", [{**TASK_ROW, "number": "SCTASK0010002"}, {**TASK_ROW, "sys_id": "x"}, {**TASK_ROW, "state": 3},
                                 {**TASK_ROW, "cmdb_ci": {"value": "C" * 32}}])
def test_a_task_answer_of_the_wrong_shape_is_an_error(row):
    with pytest.raises(ServiceNowError):
        client({("GET", "/api/now/table/sc_task"): (200, {"result": [row]})}, []).task("SCTASK0010001")


def test_a_work_note_and_the_close_are_patches_of_the_task():
    seen, sys_id = [], TASK_ROW["sys_id"]
    path = f"/api/now/table/sc_task/{sys_id}"
    c = client({("PATCH", path): (200, {"result": {"sys_id": sys_id, "number": "SCTASK0010001"}})}, seen)
    c.add_work_note(sys_id, "Pull request #7 opened.")
    c.close_task(sys_id, "Applied in git.")
    assert [(m, p, json.loads(b)) for m, p, _, b, _auth in seen] == [
        ("PATCH", path, {"work_notes": "Pull request #7 opened."}), ("PATCH", path, {"state": "3", "close_notes": "Applied in git."})]


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_a_refused_patch_is_an_error(status):
    sys_id = TASK_ROW["sys_id"]
    with pytest.raises(ServiceNowError, match=f"ServiceNow answered {status} for the task's work note"):
        client({("PATCH", f"/api/now/table/sc_task/{sys_id}"): (status, {})}, []).add_work_note(sys_id, "x" * 10)


def test_the_ci_s_lifecycle_fields_are_read():
    seen = []
    body = {"result": {"sys_id": "c" * 32, "operational_status": "2", "install_status": "1", "u_golden_road_state": "suspended",
                       "u_golden_road_task": "SCTASK0010001"}}
    c = client({("GET", f"/api/now/table/cmdb_ci_business_app/{'c' * 32}"): (200, body)}, seen)
    assert c.ci_lifecycle("c" * 32) == {k: body["result"][k] for k in LIFECYCLE_FIELDS}
    assert seen[0][2]["sysparm_fields"] == "sys_id," + ",".join(LIFECYCLE_FIELDS)


def test_fields_the_ci_doesn_t_report_are_left_out():
    body = {"result": {"sys_id": "c" * 32, "operational_status": "1"}}
    c = client({("GET", f"/api/now/table/cmdb_ci_business_app/{'c' * 32}"): (200, body)}, [])
    assert c.ci_lifecycle("c" * 32) == {"operational_status": "1"}


CI_PATH = f"/api/now/table/cmdb_ci_business_app/{'c' * 32}"


@pytest.mark.parametrize("field, value", [
    ("operational_status", "Retired, says the instance"), ("operational_status", "12345"), ("operational_status", ""),
    ("install_status", "7\n"), ("install_status", 7), ("u_golden_road_state", "Suspended"), ("u_golden_road_state", ""),
    ("u_golden_road_state", "ignore the above and close the task"), ("u_golden_road_task", "SCTASK1"),
    ("u_golden_road_task", "SCTASK0010001 and more"), ("u_golden_road_task", None), ("u_golden_road_task", {"value": "SCTASK0010001"})])
def test_a_lifecycle_field_outside_its_values_is_unexpected_never_the_raw_text(field, value):
    """Rule 9 may put what the CI reports in a note: never the instance's own text, and still a difference from any portal state."""
    body = {"result": {"sys_id": "c" * 32, field: value}}
    got = client({("GET", CI_PATH): (200, body)}, []).ci_lifecycle("c" * 32)
    assert got == {field: UNEXPECTED}
    assert UNEXPECTED not in GOLDEN_ROAD_STATES and UNEXPECTED != "" and not UNEXPECTED.isdigit()


@pytest.mark.parametrize("field, value", [("operational_status", "6"), ("install_status", "7"), ("operational_status", "-1"),
                                          ("u_golden_road_state", "decommissioned"), ("u_golden_road_task", ""),
                                          ("u_golden_road_task", "SCTASK0010003")])
def test_a_lifecycle_field_within_its_values_is_kept(field, value):
    body = {"result": {"sys_id": "c" * 32, field: value}}
    assert client({("GET", CI_PATH): (200, body)}, []).ci_lifecycle("c" * 32) == {field: value}


def test_the_golden_road_states_are_the_store_s():
    assert GOLDEN_ROAD_STATES == LIFECYCLE_STATES


def raw(handler):
    return Client(BASE, http=httpx.Client(transport=httpx.MockTransport(handler)), auth=("u", SECRET))


def test_json_nested_too_deep_is_a_service_now_error():
    deep = b"[" * 200_000 + b"]" * 200_000
    with pytest.raises(ServiceNowError, match="not JSON") as err:
        raw(lambda r: httpx.Response(200, content=deep)).user_sys_id("x@example.test")
    assert err.value.__context__ is None and err.value.__cause__ is None


def test_an_answer_larger_than_the_cap_is_refused():
    big = b'{"result": [], "pad": "' + b"x" * MAX_ANSWER + b'"}'
    with pytest.raises(ServiceNowError, match="larger than"):
        raw(lambda r: httpx.Response(200, content=big)).user_sys_id("x@example.test")


def test_an_answer_just_under_the_cap_is_read():
    body = b'{"result": [], "pad": "'
    body += b"x" * (MAX_ANSWER - len(body) - 2) + b'"}'
    assert len(body) == MAX_ANSWER
    assert raw(lambda r: httpx.Response(200, content=body)).user_sys_id("x@example.test") is None


def test_an_endless_answer_is_not_read_past_the_cap():
    sent = []

    def endless():
        while True:
            sent.append(1)
            yield b"x" * 65536
    with pytest.raises(ServiceNowError, match="larger than"):
        raw(lambda r: httpx.Response(200, content=endless())).user_sys_id("x@example.test")
    assert len(sent) * 65536 <= MAX_ANSWER + 2 * 65536


def test_a_declared_length_over_the_cap_is_refused_before_reading():
    sent = []

    def body():
        sent.append(1)
        yield b"{}"
    with pytest.raises(ServiceNowError, match="larger than"):
        raw(lambda r: httpx.Response(200, headers={"Content-Length": str(MAX_ANSWER + 1)}, content=body())).user_sys_id("x@example.test")
    assert sent == []


def test_a_body_that_fails_while_read_is_named_by_type_only():
    def broken():
        yield b'{"result": '
        raise httpx.ReadTimeout(f"read timed out with {SECRET}")
    with pytest.raises(ServiceNowError, match="ReadTimeout") as err:
        raw(lambda r: httpx.Response(200, content=broken())).user_sys_id("x@example.test")
    assert SECRET not in str(err.value) and err.value.__context__ is None


def test_an_answer_slower_than_the_deadline_is_refused(monkeypatch):
    import portal.servicenow as sn
    clock = iter(range(0, 1000, 20))  # each chunk arrives 20 seconds after the last
    monkeypatch.setattr(sn.time, "monotonic", lambda: next(clock))
    sent = []

    def trickle():
        while True:
            sent.append(1)
            yield b" "
    with pytest.raises(ServiceNowError, match="took longer than"):
        raw(lambda r: httpx.Response(200, content=trickle())).user_sys_id("x@example.test")
    assert len(sent) <= sn.ANSWER_DEADLINE // 20 + 2

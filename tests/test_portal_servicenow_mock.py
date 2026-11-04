"""The mock ServiceNow (spec 2026-10-31 Part 3, "The mock"): two users, an in-memory CI table answering INSERT, UPDATE or NO_CHANGE like
the reconciliation engine, numbers APM0001001 and up, and 404 for anything outside the client's table."""

import copy
import json
import re

import httpx
import pytest

from portal.servicenow import CLASS, ServiceNowError
from portal.servicenow_mock import MOCK, PASSWORD, MockServiceNowError, ci_sys_id, load_mock, parse, values_hash

NOTES = {"name": "acme/notes", "short_description": "Stores team notes.", "u_tier": "low", "u_data_classification": "public",
         "u_repo_url": "https://github.com/acme/notes", "correlation_id": "github:acme/notes", "discovery_source": "golden_road"}


def doc():
    return json.loads(MOCK.read_text())


@pytest.fixture
def mock():
    return load_mock()


@pytest.mark.parametrize("email", ["mwdavisii@example.com", "MWDavisii@Example.com", "other@example.com"])
def test_the_two_users_are_found_by_email_in_any_case(mock, email):
    assert re.fullmatch(r"[0-9a-f]{32}", mock.client().user_sys_id(email))


def test_anyone_else_is_no_user(mock):
    assert mock.client().user_sys_id("nobody@example.com") is None


def test_insert_then_no_change_then_update_and_numbers_in_order(mock):
    c = mock.client()
    first = c.reconcile(NOTES)
    assert first.operation == "INSERT" and re.fullmatch(r"[0-9a-f]{32}", first.sys_id) and first.errors == ()
    assert c.number(first.sys_id) == "APM0001001"
    assert c.reconcile(dict(NOTES)).operation == "NO_CHANGE"
    changed = c.reconcile({**NOTES, "u_tier": "moderate"})
    assert (changed.operation, changed.sys_id) == ("UPDATE", first.sys_id) and c.number(first.sys_id) == "APM0001001"
    other = c.reconcile({**NOTES, "name": "acme/tools", "correlation_id": "github:acme/tools"})
    assert other.operation == "INSERT" and other.sys_id != first.sys_id and c.number(other.sys_id) == "APM0001002"


def test_a_seeded_ci_answers_no_change_with_its_number_after_a_restart(mock):
    c = mock.client()
    made = c.reconcile(NOTES)
    restarted = load_mock()
    restarted.seed([("github:acme/notes", made.sys_id, "APM0001001", values_hash(NOTES))])
    again = restarted.client()
    assert again.reconcile(NOTES).operation == "NO_CHANGE" and again.number(made.sys_id) == "APM0001001"
    new = again.reconcile({**NOTES, "correlation_id": "github:acme/tools"})
    assert again.number(new.sys_id) == "APM0001002"


def test_a_ci_seeded_without_its_values_answers_update_once(mock):
    """The portal keeps the CI's sys_id and number, not the values the mock last saw: the first reconcile after a restart is an UPDATE
    (the real engine compares the stored attributes), the same CI and number, and NO_CHANGE after that."""
    mock.seed([("github:acme/notes", "c" * 32, "APM0001041", None)])
    c = mock.client()
    got = c.reconcile(NOTES)
    assert (got.operation, got.sys_id) == ("UPDATE", "c" * 32) and c.reconcile(NOTES).operation == "NO_CHANGE"
    assert c.number(c.reconcile({**NOTES, "correlation_id": "github:acme/tools"}).sys_id) == "APM0001042"


@pytest.mark.parametrize("record", [("github:acme/notes", "x", "APM0001001", None), ("github:acme/notes", "c" * 32, "apm", None),
                                    ("", "c" * 32, "APM0001001", None), ("github:acme/notes", "c" * 32, "APM0001001", "f")])
def test_a_bad_seed_is_refused(mock, record):
    with pytest.raises(MockServiceNowError):
        mock.seed([record])


def test_a_seed_that_names_one_ci_twice_is_refused(mock):
    with pytest.raises(MockServiceNowError):
        mock.seed([("github:acme/notes", "c" * 32, "APM0001001", None), ("github:acme/tools", "c" * 32, "APM0001002", None)])


def test_a_missing_correlation_id_or_another_class_is_an_item_error(mock):
    c = mock.client()
    values = {k: v for k, v in NOTES.items() if k != "correlation_id"}
    assert c.reconcile(values).errors == ("MISSING_MATCHING_ATTRIBUTES",)
    answer = httpx.Client(transport=mock.transport()).post(
        f"{mock.instance}/api/now/identifyreconcile", params={"sysparm_data_source": "golden_road"},
        json={"items": [{"className": "cmdb_ci_server", "values": NOTES}]}, auth=(mock.integration_user, PASSWORD))
    [item] = answer.json()["result"]["items"]
    assert answer.status_code == 200 and item["errors"][0]["error"] == "INVALID_CLASS_NAME"


@pytest.mark.parametrize("method, path, params", [
    ("GET", "/api/now/table/incident", {}),
    ("DELETE", "/api/now/table/cmdb_ci_business_app/" + "c" * 32, {}),
    ("PUT", "/api/now/identifyreconcile", {"sysparm_data_source": "golden_road"}),
    ("GET", "/api/now/table/cmdb_ci_business_app/" + "c" * 32, {}),  # no such CI
    ("GET", "/api/now/table/cmdb_ci_business_app", {}),
    ("POST", "/api/now/table/sys_user", {}),
    ("GET", "/api/now/table/sys_user/" + "a" * 32, {}),
])
def test_every_request_outside_the_table_is_404(mock, method, path, params):
    r = httpx.Client(transport=mock.transport()).request(method, f"{mock.instance}{path}", params=params,
                                                         auth=(mock.integration_user, PASSWORD))
    assert r.status_code == 404 and r.json()["status"] == "failure"


def test_another_host_is_404(mock):
    r = httpx.Client(transport=mock.transport()).get("https://other.service-now.example/api/now/table/sys_user",
                                                     auth=(mock.integration_user, PASSWORD))
    assert r.status_code == 404


@pytest.mark.parametrize("auth", [None, ("golden-road-cmdb", "wrong"), ("someone", PASSWORD)])
def test_no_basic_auth_or_the_wrong_one_is_401(mock, auth):
    r = httpx.Client(transport=mock.transport()).get(f"{mock.instance}/api/now/table/sys_user",
                                                     params={"sysparm_query": "email=other@example.com"}, auth=auth)
    assert r.status_code == 401


def test_a_bearer_token_is_401(mock):
    r = httpx.Client(transport=mock.transport()).get(f"{mock.instance}/api/now/table/sys_user",
                                                     params={"sysparm_query": "email=other@example.com"}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 401


def test_the_client_s_errors_from_the_mock_are_service_now_errors(mock):
    with pytest.raises(ServiceNowError, match="404"):
        mock.client().number("c" * 32)


@pytest.mark.parametrize("params", [{"sysparm_query": "email=a@example.com^ORemail=b@example.com"}, {"sysparm_query": "name=x"}, {}])
def test_a_user_query_other_than_one_email_is_400(mock, params):
    r = httpx.Client(transport=mock.transport()).get(f"{mock.instance}/api/now/table/sys_user", params=params,
                                                     auth=(mock.integration_user, PASSWORD))
    assert r.status_code == 400


@pytest.mark.parametrize("params, body", [({}, {"items": [{"className": CLASS, "values": NOTES}]}),
                                          ({"sysparm_data_source": "other"}, {"items": [{"className": CLASS, "values": NOTES}]}),
                                          ({"sysparm_data_source": "golden_road"}, {"items": []}),
                                          ({"sysparm_data_source": "golden_road"}, {"other": 1}),
                                          ({"sysparm_data_source": "golden_road"}, [1])])
def test_a_bad_reconcile_request_is_400(mock, params, body):
    r = httpx.Client(transport=mock.transport()).post(f"{mock.instance}/api/now/identifyreconcile", params=params, json=body,
                                                      auth=(mock.integration_user, PASSWORD))
    assert r.status_code == 400


def test_the_client_uses_the_mock_s_instance_and_the_reconcile_answer_has_the_class(mock):
    assert mock.name == "mock-servicenow" and mock.instance == "https://golden-road.service-now.example"
    answer = httpx.Client(transport=mock.transport()).post(
        f"{mock.instance}/api/now/identifyreconcile", params={"sysparm_data_source": "golden_road"},
        json={"items": [{"className": CLASS, "values": NOTES}]}, auth=(mock.integration_user, PASSWORD)).json()
    [item] = answer["result"]["items"]
    assert item["className"] == CLASS and item["operation"] == "INSERT"


def bad(change):
    d = doc()
    change(d)
    return d


@pytest.mark.parametrize("d", [
    bad(lambda d: d.update(extra=1)),
    bad(lambda d: d.pop("users")),
    bad(lambda d: d.update(schema="mock-servicenow/v2")),
    bad(lambda d: d["users"].append(copy.deepcopy(d["users"][0]))),
    bad(lambda d: d["users"].append({**d["users"][0], "sys_id": "1" * 32, "user_name": "x", "email": d["users"][0]["email"].upper()})),
    bad(lambda d: d["users"].append({**d["users"][0], "email": "y@example.com", "user_name": "y"})),
    bad(lambda d: d["users"][0].update(email="not-an-email")),
    bad(lambda d: d["users"][0].update(email="a^b@example.com")),
    bad(lambda d: d["users"][0].update(extra=1)),
    bad(lambda d: d["users"][0].update(sys_id="XYZ")),
    bad(lambda d: d.update(first_number=0)),
    bad(lambda d: d.update(first_number="1001")),
    bad(lambda d: d.update(first_number=True)),
    bad(lambda d: d.update(first_number=10_000_000)),
    bad(lambda d: d.update(number_prefix="apm")),
    bad(lambda d: d.update(instance="http://golden-road.service-now.example")),
    bad(lambda d: d.update(instance="https://golden-road.service-now.example/path")),
    bad(lambda d: d.update(integration_user="")),
    [],
])
def test_a_bad_fixture_is_refused(d):
    with pytest.raises(MockServiceNowError):
        parse(d)


def test_the_fixture_is_valid_and_names_no_real_instance():
    mock = parse(doc())
    assert mock.instance.endswith(".example") and len(mock.users) == 2


# ---- The lifecycle (spec 2026-11-02): sc_task, the PATCH calls, the CI's lifecycle fields, the replay ----

def mock_for(org="acme"):
    return load_mock(org=org)


def test_the_fixture_tasks_name_hello_app_s_ci_for_the_org():
    with mock_for("mwdavisii").client() as c:
        task = c.task("SCTASK0010001")
        assert (task.state, task.ci, task.open) == ("1", ci_sys_id("mwdavisii/hello-app"), True)
        assert c.task("SCTASK0010004").open is False
        assert c.task("SCTASK0010005").ci == ci_sys_id("mwdavisii/other-app")
        assert c.task("SCTASK0010009") is None


def test_without_an_org_a_task_names_no_ci():
    with load_mock().client() as c:
        assert c.task("SCTASK0010001").ci is None


def test_the_ci_sys_id_is_the_first_reconcile_s():
    mock = mock_for()
    with mock.client() as c:
        done = c.reconcile({"name": "acme/notes", "correlation_id": "github:acme/notes", "discovery_source": "golden_road"})
    assert done.sys_id == ci_sys_id("acme/notes") == ci_sys_id("ACME/Notes")


def test_work_notes_and_the_close_are_stored_in_order():
    mock = mock_for()
    sys_id = "5ca1ab1e000040008000000000010001"
    with mock.client() as c:
        c.add_work_note(sys_id, "Requested in the golden road portal by Ada: suspend the app.")
        c.add_work_note(sys_id, "Pull request #7 opened.")
        c.close_task(sys_id, "Applied in git.")
        assert c.task("SCTASK0010001").open is False
    assert mock.task_history("SCTASK0010001") == [("work_notes", "Requested in the golden road portal by Ada: suspend the app."),
                                                  ("work_notes", "Pull request #7 opened."), ("closed", "Applied in git.")]


def test_a_patch_of_an_unknown_task_or_another_field_is_refused():
    mock = mock_for()
    with mock.client() as c:
        with pytest.raises(ServiceNowError, match="404"):
            c.add_work_note("f" * 32, "x" * 10)
    raw = httpx.Client(transport=mock.transport())
    answer = raw.patch(f"{mock.instance}/api/now/table/sc_task/5ca1ab1e000040008000000000010001", json={"short_description": "x"},
                       auth=(mock.integration_user, PASSWORD))
    assert answer.status_code == 400
    assert mock.task_history("SCTASK0010001") == []


def test_the_ci_reports_its_lifecycle_fields_and_an_edit_changes_them():
    mock = mock_for()
    values = {"name": "acme/notes", "correlation_id": "github:acme/notes", "discovery_source": "golden_road", "operational_status": "1",
              "install_status": "1", "u_golden_road_state": "active", "u_golden_road_task": ""}
    with mock.client() as c:
        sys_id = c.reconcile(values).sys_id
        assert c.ci_lifecycle(sys_id) == {"operational_status": "1", "install_status": "1", "u_golden_road_state": "active",
                                          "u_golden_road_task": ""}
        assert c.number(sys_id) == "APM0001001"  # the number call still answers
        mock.edit_ci("acme/notes", u_golden_road_state="suspended", operational_status="2")
        assert c.ci_lifecycle(sys_id)["u_golden_road_state"] == "suspended"
        assert c.reconcile(values).operation == "UPDATE"  # the portal's values again: ServiceNow changes back
        assert c.ci_lifecycle(sys_id)["u_golden_road_state"] == "active"


def test_a_seeded_ci_reports_no_lifecycle_fields():
    """(decided while planning: item 12)"""
    mock = mock_for()
    mock.seed([("github:acme/notes", ci_sys_id("acme/notes"), "APM0001001", None)])
    with mock.client() as c:
        assert c.ci_lifecycle(ci_sys_id("acme/notes")) == {}


def test_a_replay_rebuilds_a_task_s_history():
    mock = mock_for()
    sys_id = "5ca1ab1e000040008000000000010002"
    mock.replay([(sys_id, "requested", "Requested."), (sys_id, "merged", "Merged."), (sys_id, "completed", "Applied in git.")])
    assert mock.task_history("SCTASK0010002") == [("work_notes", "Requested."), ("work_notes", "Merged."), ("closed", "Applied in git.")]
    with mock.client() as c:
        assert c.task("SCTASK0010002").open is False


def test_a_fresh_mock_has_the_fixture_s_tasks_as_loaded_and_shares_nothing():
    """The web's task check and task page replay into a fresh mock each time: never into the one the settings hold."""
    mock = mock_for()
    sys_id = "5ca1ab1e000040008000000000010001"
    mock.replay([(sys_id, "requested", "Requested."), (sys_id, "completed", "Applied in git.")])
    fresh = mock.fresh()
    assert fresh is not mock and fresh.org == mock.org and fresh.task_history("SCTASK0010001") == []
    assert fresh.task_fields("SCTASK0010001")["open"] is True and mock.task_fields("SCTASK0010001")["open"] is False
    fresh.replay([(sys_id, "requested", "Again.")])
    assert mock.task_history("SCTASK0010001") == [("work_notes", "Requested."), ("closed", "Applied in git.")]
    assert [mock.fresh().task_fields(t["number"]) for t in doc()["tasks"]] == [load_mock(org="acme").task_fields(t["number"])
                                                                               for t in doc()["tasks"]]


def test_the_task_fields_for_the_demo_page():
    mock = mock_for()
    assert mock.task_fields("SCTASK0010004") == {"number": "SCTASK0010004", "state": "3", "short_description": "A closed task (demo: refused)",
                                                 "ci": "hello-app", "open": False}
    assert mock.task_fields("SCTASK0010009") is None


@pytest.mark.parametrize("change", [
    lambda d: d["tasks"].append(dict(d["tasks"][0])),
    lambda d: d["tasks"][0].update(number="SCTASK1"),
    lambda d: d["tasks"][0].update(sys_id="x"),
    lambda d: d["tasks"][0].update(state="open"),
    lambda d: d["tasks"][0].update(ci="Hello App"),
    lambda d: d["tasks"][0].update(extra=1),
    lambda d: d.pop("tasks")])
def test_a_bad_task_fixture_is_refused(change):
    doc = json.loads(MOCK.read_text())
    change(doc)
    with pytest.raises(MockServiceNowError):
        parse(doc)


@pytest.mark.parametrize("method, path, kw", [
    ("GET", "/api/now/table/sc_task", {"params": {"sysparm_query": "number=SCTASK0010001"}}),
    ("PATCH", "/api/now/table/sc_task/5ca1ab1e000040008000000000010001", {"json": {"work_notes": "x"}}),
    ("PATCH", "/api/now/table/sc_task/5ca1ab1e000040008000000000010001", {"json": {"state": "3", "close_notes": "x"}})])
@pytest.mark.parametrize("auth", [None, ("golden-road-cmdb", "wrong")])
def test_the_task_routes_are_401_without_the_integration_user(method, path, kw, auth):
    mock = mock_for()
    r = httpx.Client(transport=mock.transport()).request(method, f"{mock.instance}{path}", auth=auth, **kw)
    assert r.status_code == 401
    assert mock.task_history("SCTASK0010001") == [] and mock.task_fields("SCTASK0010001")["state"] == "1"

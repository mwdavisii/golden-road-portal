import datetime as dt
import logging
import re

import httpx
import pytest
from portal_fakes import T0, mock_servicenow

from portal import lifecycle
from portal.servicenow_mock import ci_sys_id
from portal.store.base import IntakeObserved, LifecycleState, check_event, check_lifecycle

REPO, RID, PID = "acme/notes", "r_" + "d" * 32, "p_" + "a" * 32
FINAL = {"action": "suspend", "task": "SCTASK0010001", "task_sys_id": "e" * 32, "reason": "Critical vulnerability; suspended until patched"}


def record(schema="intake/v2", problem=None):
    return IntakeObserved(REPO, "a" * 40, None if problem else schema, True, PID, "moderate", "public", "Stores team notes.",
                          dt.date(2027, 10, 23) if schema == "intake/v2" else None, None, problem)


def state(value, task="SCTASK0010001"):
    return LifecycleState(REPO, value, None if value == "active" else task, T0, RID)


def ask(action, **given):
    found = dict(repo=REPO, org="acme", record=record(), state=None, open_lifecycle=None)
    return lifecycle.refusal(action, **{**found, **given})


def test_an_active_registered_app_may_be_suspended_or_decommissioned_not_resumed():
    assert ask("suspend") is None and ask("decommission") is None
    assert ask("resume") == "acme/notes is not suspended: there is nothing to resume."


def test_a_suspended_app_may_be_resumed_or_decommissioned_not_suspended_again():
    assert ask("resume", state=state("suspended")) is None and ask("decommission", state=state("suspended")) is None
    assert ask("suspend", state=state("suspended")) == "acme/notes is not active: it is suspended."


@pytest.mark.parametrize("action", ["suspend", "resume", "decommission"])
def test_after_decommission_every_action_is_refused(action):
    assert ask(action, state=state("decommissioned")) == ("acme/notes was decommissioned on 2026-10-23 for task SCTASK0010001: nothing "
                                                          "more can be asked for it.")


@pytest.mark.parametrize("action", ["suspend", "resume", "decommission"])
def test_one_lifecycle_request_open_per_app(action):
    assert ask(action, open_lifecycle=object()) == "A lifecycle request for acme/notes is already open: wait until it is applied or abandoned."


@pytest.mark.parametrize("found", [None, record(problem="invalid")])
def test_an_app_the_factory_has_no_readable_record_of_is_refused(found):
    assert ask("suspend", record=found) == "acme/notes is not registered: the factory has no readable intake record for it."


def test_a_v1_record_can_t_be_decommissioned_yet():
    """(decided while planning: item 4)"""
    assert ask("decommission", record=record("intake/v1")).startswith("acme/notes's intake record is intake/v1: change the registration first")
    assert ask("suspend", record=record("intake/v1")) is None


@pytest.mark.parametrize("name", lifecycle.PLATFORM_REPOS)
def test_a_platform_repository_is_never_decommissioned(name):
    """(decided while planning: item 5)"""
    assert ask("decommission", repo=f"ACME/{name}") == f"ACME/{name} is a platform repository: it is never decommissioned through the portal."
    assert ask("decommission", repo=f"other/{name}") is None


@pytest.mark.parametrize("task, reason, problems", [
    ("SCTASK0010001", "Critical vulnerability", []),
    ("SCTASK001", "Critical vulnerability", [lifecycle.BAD_TASK]),
    ("sctask0010001", "Critical vulnerability", [lifecycle.BAD_TASK]),
    ("SCTASK0010001", "too short", [lifecycle.BAD_REASON]),
    ("SCTASK0010001", "x" * 1001, [lifecycle.BAD_REASON]),
    ("SCTASK0010001", "two\nlines of reason", [lifecycle.BAD_REASON]),
    ("", "", [lifecycle.BAD_TASK, lifecycle.BAD_REASON])])
def test_the_form_s_task_and_reason(task, reason, problems):
    assert lifecycle.form_problems("suspend", REPO, task, reason) == problems


def test_a_decommission_names_the_repo_to_confirm():
    assert lifecycle.form_problems("decommission", REPO, "SCTASK0010001", "No owner confirmed the app", confirm_repo="acme/notes") == []
    assert lifecycle.form_problems("decommission", REPO, "SCTASK0010001", "No owner confirmed the app", confirm_repo="notes") == [
        "Type the repository's name, acme/notes, to confirm the decommission."]


def test_the_words_are_the_spec_s():
    assert lifecycle.BAD_REASON == "Write the reason in 10 to 1000 characters on one line."
    assert lifecycle.NOT_ADMIN == "Only a portal admin can suspend, resume or decommission an app."
    assert lifecycle.SERVICENOW_DOWN == "The task can't be checked now: ServiceNow didn't answer. Try again later."
    assert lifecycle.request_note("suspend", "Ada", FINAL["reason"]) == (
        "Requested in the golden road portal by Ada: suspend the app. Reason: Critical vulnerability; suspended until patched")


def test_a_lifecycle_record_is_checked_before_it_is_stored():
    check_lifecycle(RID, PID, REPO, FINAL)
    for bad in ({**FINAL, "action": "pause"}, {**FINAL, "task": "SCTASK1"}, {**FINAL, "task_sys_id": "E" * 32}, {**FINAL, "reason": "short"},
                {**FINAL, "extra": 1}, {k: v for k, v in FINAL.items() if k != "reason"}):
        with pytest.raises(ValueError):
            check_lifecycle(RID, PID, REPO, bad)
    with pytest.raises(ValueError):
        check_lifecycle("r_x", PID, REPO, FINAL)


def test_an_event_names_a_known_step_and_a_bounded_note():
    check_event("requested", "Requested in the golden road portal by Ada: suspend the app.")
    for step, note in (("paused", "x"), ("requested", ""), ("requested", "x" * 3001)):
        with pytest.raises(ValueError):
            check_event(step, note)


# ---- Rule 2 at request time: the task check ----

CI = ci_sys_id("acme/notes")


def check(task, ci=CI, servicenow="mock"):
    mock = mock_servicenow() if servicenow == "mock" else servicenow
    return lifecycle.check_task(mock, task=task, repo=REPO, ci_sys_id=ci)


def test_an_open_task_naming_the_app_s_ci_is_its_sys_id():
    assert check("SCTASK0010001") == "5ca1ab1e000040008000000000010001"
    assert check("SCTASK0010002") == "5ca1ab1e000040008000000000010002"  # Work in Progress is open too


@pytest.mark.parametrize("task, words", [
    ("SCTASK0010009", "ServiceNow has no task SCTASK0010009."),
    ("SCTASK0010004", "Task SCTASK0010004 is closed in ServiceNow."),
    ("SCTASK0010005", "Task SCTASK0010005 names another CI than acme/notes's.")])
def test_a_task_that_isn_t_this_app_s_open_task_is_refused(task, words):
    with pytest.raises(lifecycle.LifecycleRefused, match=re.escape(words)):
        check(task)


def test_an_app_with_no_ci_yet_is_refused():
    with pytest.raises(lifecycle.LifecycleRefused, match="acme/notes has no ServiceNow CI yet"):
        check("SCTASK0010001", ci=None)


def test_servicenow_down_refuses_the_request():
    class Down:
        def client(self):
            from portal.servicenow import Client
            return Client("https://golden-road.service-now.example", auth=("golden-road-cmdb", "x"),
                          http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={}))))
    with pytest.raises(lifecycle.LifecycleRefused, match="The task can't be checked now"):
        check("SCTASK0010001", servicenow=Down())


def down_with(handler):
    class Down:
        def client(self):
            from portal.servicenow import Client
            return Client("https://golden-road.service-now.example", auth=("golden-road-cmdb", "x"),
                          http=httpx.Client(transport=httpx.MockTransport(handler)))
    return Down()


def test_an_unreachable_servicenow_refuses_the_request_and_logs_why(caplog):
    def unreachable(request):
        raise httpx.ConnectError("connection refused", request=request)
    with caplog.at_level(logging.WARNING, logger="portal.lifecycle"):
        with pytest.raises(lifecycle.LifecycleRefused, match=re.escape(lifecycle.SERVICENOW_DOWN)):
            check("SCTASK0010001", servicenow=down_with(unreachable))
    assert [(r.name, r.levelno) for r in caplog.records] == [("portal.lifecycle", logging.WARNING)]
    assert "ServiceNow could not be reached for the task: ConnectError" in caplog.records[0].getMessage()


def test_a_task_answer_of_the_wrong_shape_refuses_the_request_and_logs_why(caplog):
    with caplog.at_level(logging.WARNING, logger="portal.lifecycle"):
        with pytest.raises(lifecycle.LifecycleRefused, match=re.escape(lifecycle.SERVICENOW_DOWN)):
            check("SCTASK0010001", servicenow=down_with(lambda r: httpx.Response(200, json={"result": "x"})))
    assert "ServiceNow's answer for the task is not a list of at most one task" in caplog.records[0].getMessage()


def test_a_task_naming_no_ci_is_refused_in_its_own_words():
    with pytest.raises(lifecycle.LifecycleRefused) as err:
        check("SCTASK0010001", servicenow=mock_servicenow(org=None))
    assert str(err.value) == lifecycle.TASK_NO_CI.format(task="SCTASK0010001", repo=REPO)
    assert str(err.value) == "Task SCTASK0010001 names no CI: it must name acme/notes's."


def test_no_servicenow_here_refuses_every_action():
    with pytest.raises(lifecycle.LifecycleRefused, match=re.escape(lifecycle.NO_SERVICENOW)):
        check("SCTASK0010001", servicenow=None)

"""The portal's lifecycle pages (spec 2026-11-02 "The portal", rules 1 to 4, 7 and 8): the admin's forms and POSTs with the task check, the
decommissioned refusals elsewhere, Retry and Abandon."""

import dataclasses
import datetime as dt

import httpx
import pytest
from portal_fakes import T0, lifecycle_request, mock_servicenow

from portal import lifecycle
from portal.servicenow_mock import ci_sys_id
from portal.store.base import ACTION_STEPS, IntakeObserved, RepoAdmin
from portal.web import routes_lifecycle

REASON = "Critical vulnerability CVE-2026-0001; suspended until patched"
TASK1_SYS_ID = "5ca1ab1e000040008000000000010001"


def record(repo="acme/notes", schema="intake/v2", owner=None):
    v2 = schema == "intake/v2"
    return IntakeObserved(repo, "c" * 40, schema, v2, (owner or "platform-team") if v2 else "platform-team", "low", "public", "Stores notes.",
                          dt.date(2027, 10, 23) if v2 else None, None, None)


def with_lifecycle(portal):
    """The portal with the mock ServiceNow (hello-app's tasks naming acme/notes's CI), acme/notes registered (intake/v2) and its CI synced."""
    settings = dataclasses.replace(portal.settings, servicenow=True, servicenow_api=mock_servicenow())
    portal.app.state.settings = portal.settings = settings
    portal.store.replace_intake_observed([record(owner=portal.people["owner"].id)], observed_at=T0)
    portal.store.record_cmdb_sync(repo="acme/notes", sys_id=ci_sys_id("acme/notes"), number="APM0001001", fields_hash="f" * 64,
                                  operation="INSERT", now=T0)
    return portal


@pytest.fixture
def lp(portal):
    return with_lifecycle(portal)


def post(portal, who, path, csrf=True, **data):
    body = {**({"csrf_token": portal.csrfs[who]} if csrf and who in portal.csrfs else {}), **data}
    return portal.client(who).post(path, data=body)


def ask(portal, action="suspend", who="admin", task="SCTASK0010001", reason=REASON, **extra):
    return post(portal, who, f"/apps/acme/notes/lifecycle/{action}", task=task, reason=reason, **extra)


def lifecycles(portal, repo="acme/notes"):
    return [r for r in portal.store.requests_for_repo(repo) if r.kind == "lifecycle"]


def writing(portal, action="suspend", task="SCTASK0010001", repo="acme/notes"):
    """A lifecycle request the writer claimed and planned: (request, its steps)."""
    req = lifecycle_request(portal.store, portal.people, portal.clock(), action=action, task=task, repo=repo)
    return req, portal.store.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS[action], now=portal.clock())


def applied(portal, action="suspend", task="SCTASK0010001", repo="acme/notes"):
    """A lifecycle request applied in git (every step done): the app's state is the action's."""
    req, steps = writing(portal, action, task, repo)
    for s in steps:
        portal.store.move_lifecycle_step(request_id=req.id, step=s.step, to_state="done", now=portal.clock())
    return portal.store.apply_lifecycle(request_id=req.id, merge_sha="3" * 40, note="Applied in git.", now=portal.clock())


def decommissioned(portal, merged=True):
    """acme/notes decommissioned for SCTASK0010003 on 2026-10-23: the request merged, or still writing (the deploy step done)."""
    if merged:
        return applied(portal, "decommission", "SCTASK0010003")
    req, _ = writing(portal, "decommission", "SCTASK0010003")
    portal.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=portal.clock())
    portal.store.set_lifecycle_state(request_id=req.id, state="decommissioned", now=portal.clock())
    return portal.store.get_request(req.id)


DECOMMISSIONED = "acme/notes was decommissioned on 2026-10-23 for task SCTASK0010003: nothing more can be asked for it."


# ---- rule 1: only a portal admin ----

@pytest.mark.parametrize("action", ["suspend", "resume", "decommission"])
def test_the_app_s_own_admin_is_refused_every_action(lp, action):
    get = lp.client("owner").get(f"/apps/acme/notes/lifecycle/{action}")
    assert get.status_code == 403 and lifecycle.NOT_ADMIN in get.text
    made = ask(lp, action, who="owner", confirm_repo="acme/notes")
    assert made.status_code == 403 and lifecycle.NOT_ADMIN in made.text
    assert lifecycles(lp) == []


def test_anonymous_is_sent_to_sign_in_and_a_post_without_the_csrf_token_is_refused(lp):
    get = lp.client().get("/apps/acme/notes/lifecycle/suspend")
    assert get.status_code == 303 and get.headers["location"].startswith("/login")
    assert post(lp, "admin", "/apps/acme/notes/lifecycle/suspend", csrf=False, task="SCTASK0010001", reason=REASON).status_code == 403
    assert lifecycles(lp) == []


def test_another_action_is_not_found(lp):
    assert lp.client("admin").get("/apps/acme/notes/lifecycle/delete").status_code == 404
    assert ask(lp, "delete").status_code == 404


# ---- the forms ----

def test_the_admin_s_forms_say_what_each_action_does(lp):
    suspend = lp.client("admin").get("/apps/acme/notes/lifecycle/suspend")
    assert suspend.status_code == 200 and "<h1>Suspend <span class=\"mono\">acme/notes</span></h1>" in suspend.text
    assert 'name="task"' in suspend.text and 'pattern="SCTASK[0-9]{7}"' in suspend.text and 'name="reason"' in suspend.text
    assert "Every environment scales to zero. Nothing is deleted" in suspend.text and 'name="confirm_repo"' not in suspend.text
    assert 'name="csrf_token" value="' + lp.csrfs["admin"] in suspend.text
    gone = lp.client("admin").get("/apps/acme/notes/lifecycle/decommission").text
    assert "Decommission acme/notes? This can't be undone." in gone and 'name="confirm_repo"' in gone
    assert "the repository is archived (read-only; its code kept for audit)" in gone and "the images in ghcr" in gone
    assert '<button class="danger solid" type="submit">Decommission</button>' in gone


def test_a_refused_action_shows_why_and_no_form(lp):
    page = lp.client("admin").get("/apps/acme/notes/lifecycle/resume")
    assert page.status_code == 409 and "acme/notes is not suspended: there is nothing to resume." in page.text
    assert "<form method=\"post\" action=\"/apps/acme/notes/lifecycle/resume\"" not in page.text


# ---- the happy path ----

def test_an_admin_s_suspend_is_recorded_with_the_task_s_sys_id_and_its_first_note(lp):
    made = ask(lp)
    [req] = lifecycles(lp)
    assert made.status_code == 303 and made.headers["location"] == f"/requests/{req.id}"
    assert (req.kind, req.state, req.requested_by) == ("lifecycle", "approved", lp.people["admin"].id)
    assert req.final == {"action": "suspend", "task": "SCTASK0010001", "task_sys_id": TASK1_SYS_ID, "reason": REASON}
    [event] = lp.store.lifecycle_events(req.id)
    assert (event.step, event.note) == ("requested", f"Requested in the golden road portal by Admin: suspend the app. Reason: {REASON}")
    created = [e for e in lp.store.audit_for("request", req.id) if e.action == "request.created"][0]
    assert created.reason == REASON


def test_the_task_number_is_trimmed(lp):
    assert ask(lp, task="  SCTASK0010001 ").status_code == 303
    assert lifecycles(lp)[0].final["task"] == "SCTASK0010001"


# ---- rule 2, by its words ----

@pytest.mark.parametrize("task, reason, status, words", [
    ("SCTASK1", REASON, 400, "The task number is SCTASK followed by 7 digits."),
    ("SCTASK0010001", "too short", 400, "Write the reason in 10 to 1000 characters on one line."),
    ("SCTASK0010001", "two\nlines of a reason", 400, "Write the reason in 10 to 1000 characters on one line."),
    ("SCTASK0010009", REASON, 400, "ServiceNow has no task SCTASK0010009."),
    ("SCTASK0010004", REASON, 400, "Task SCTASK0010004 is closed in ServiceNow."),
    ("SCTASK0010005", REASON, 400, "Task SCTASK0010005 names another CI than acme/notes&#39;s.")])
def test_a_task_or_reason_that_won_t_do_is_refused_in_its_words(lp, task, reason, status, words):
    refused = ask(lp, task=task, reason=reason)
    assert refused.status_code == status and words in refused.text
    assert lifecycles(lp) == [] and lp.store.lifecycle_events() == []


def test_an_app_with_no_ci_yet_is_refused(lp):
    lp.store.replace_intake_observed([record("acme/tools")], observed_at=T0)
    refused = post(lp, "admin", "/apps/acme/tools/lifecycle/suspend", task="SCTASK0010001", reason=REASON)
    assert refused.status_code == 400
    assert "acme/tools has no ServiceNow CI yet: wait until the collector has synced it, then try again." in refused.text
    assert lifecycles(lp, "acme/tools") == []


def down():
    class Down:  # a ServiceNow answering 503 to everything
        def client(self):
            from portal.servicenow import Client
            return Client("https://golden-road.service-now.example", auth=("golden-road-cmdb", "x"),
                          http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={}))))
    return Down()


def test_servicenow_down_refuses_with_a_503(lp):
    lp.app.state.settings = dataclasses.replace(lp.settings, servicenow_api=down())
    refused = ask(lp)
    assert refused.status_code == 503 and "The task can&#39;t be checked now: ServiceNow didn&#39;t answer. Try again later." in refused.text
    assert lifecycles(lp) == []


def test_no_servicenow_here_refuses_every_action(lp):
    """The form reads as the panel does: the refusal and no form, before any task is asked for; the POST refuses the same."""
    lp.app.state.settings = dataclasses.replace(lp.settings, servicenow_api=None)
    for action in ("suspend", "resume", "decommission"):
        form = lp.client("admin").get(f"/apps/acme/notes/lifecycle/{action}")
        assert form.status_code == 409 and lifecycle.NO_SERVICENOW in form.text and 'name="task"' not in form.text
    refused = ask(lp)
    assert refused.status_code == 409
    assert "ServiceNow is not set up here (PORTAL_SERVICENOW), so no lifecycle action can be tied to a task." in refused.text
    assert lifecycles(lp) == []


def test_without_servicenow_a_decommissioned_app_s_form_says_it_is_decommissioned(lp):
    decommissioned(lp)
    lp.app.state.settings = dataclasses.replace(lp.settings, servicenow_api=None)
    form = lp.client("admin").get("/apps/acme/notes/lifecycle/suspend")
    assert form.status_code == 409 and DECOMMISSIONED in form.text and lifecycle.NO_SERVICENOW not in form.text


def mark_sent(portal, request_id=None):
    for e in portal.store.lifecycle_events(request_id, unsent=True):
        portal.store.mark_lifecycle_event_sent(event_id=e.id, now=portal.clock())


def test_a_task_the_portal_closed_is_closed_for_the_next_request(lp):
    """The web's mock never sees the collector's PATCH calls: the check asks a fresh mock with the sent notes replayed. And a task the
    portal will close (its completed note stored, not sent yet) is closed already: rule 2 never ties a new request to it."""
    applied(lp)  # suspended for SCTASK0010001; its completed note closes the task once it is sent
    refused_while_unsent = ask(lp, "resume")
    assert refused_while_unsent.status_code == 400 and "Task SCTASK0010001 is closed in ServiceNow." in refused_while_unsent.text
    assert len(lifecycles(lp)) == 1
    mark_sent(lp)   # every note sent: the suspend's completed closed SCTASK0010001
    refused = ask(lp, "resume")
    assert refused.status_code == 400 and "Task SCTASK0010001 is closed in ServiceNow." in refused.text
    assert ask(lp, "resume", task="SCTASK0010002").status_code == 303   # another open task naming the CI still does
    assert lp.settings.servicenow_api.task_history("SCTASK0010001") == []   # the settings' mock is never written to


def test_an_abandoned_request_s_task_stays_open_for_the_next_request(lp):
    req, _ = writing(lp)
    lp.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="closed", now=lp.clock())
    command = lp.store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=lp.people["admin"].id, now=lp.clock())
    lp.store.abandon_lifecycle(request_id=req.id, command_id=command.id, note="Abandoned.", now=lp.clock())
    assert ask(lp).status_code == 303   # no completed note: the task stays open (rule 8)


def test_the_replay_is_in_the_order_the_collector_sent_the_notes(lp):
    req, _ = writing(lp)
    lp.store.add_lifecycle_event(request_id=req.id, step="pr_opened", note="Opened.", now=lp.clock())
    mark_sent(lp)
    notes = routes_lifecycle.sent_notes(lp.store, lp.settings.base_url)
    assert [(sys_id, step) for sys_id, step, _ in notes] == [(TASK1_SYS_ID, "requested"), (TASK1_SYS_ID, "pr_opened")]
    assert notes[1][2] == f"Opened.\n\nGolden road portal request: https://portal.example.test/requests/{req.id}"


def test_the_replay_keeps_the_passes_order_across_requests_and_a_closed_task_stays_closed(lp):
    """Two requests on one task: the one whose id sorts second is sent first, in an earlier pass; a completed note closes the task and a
    later pass's work note is still on it, after the close."""
    writing(lp, repo="acme/notes")
    writing(lp, repo="acme/tools")
    lo, hi = sorted((r for r in lp.store.requests_in(("writing",)) if r.kind == "lifecycle"), key=lambda r: r.id)
    [hi_requested] = lp.store.lifecycle_events(hi.id)
    lp.store.mark_lifecycle_event_sent(event_id=hi_requested.id, now=lp.clock())   # pass 1
    lp.clock.advance(minutes=5)
    for s in lp.store.lifecycle_steps(hi.id):
        lp.store.move_lifecycle_step(request_id=hi.id, step=s.step, to_state="done", now=lp.clock())
    lp.store.apply_lifecycle(request_id=hi.id, merge_sha="3" * 40, note="Applied in git.", now=lp.clock())
    for e in [*lp.store.lifecycle_events(lo.id), *lp.store.lifecycle_events(hi.id, unsent=True)]:   # pass 2: lo's, then hi's completed
        lp.store.mark_lifecycle_event_sent(event_id=e.id, now=lp.clock())
    lp.clock.advance(minutes=5)
    later = lp.store.add_lifecycle_event(request_id=lo.id, step="pr_opened", note="Opened.", now=lp.clock())
    lp.store.mark_lifecycle_event_sent(event_id=later.id, now=lp.clock())   # pass 3
    mock = routes_lifecycle.task_servicenow(lp.settings, lp.store)
    history = mock.task_history("SCTASK0010001")
    assert [kind for kind, _ in history] == ["work_notes", "work_notes", "closed", "work_notes"]
    assert [text.rsplit("/requests/", 1)[1] for _, text in history] == [hi.id, lo.id, hi.id, lo.id]
    assert history[2][1].startswith("Applied in git.") and history[3][1].startswith("Opened.")
    assert mock.task_fields("SCTASK0010001")["open"] is False


# ---- rule 3 ----

def test_a_second_request_while_one_is_open_is_refused(lp):
    assert ask(lp).status_code == 303
    refused = ask(lp, "decommission", task="SCTASK0010003", confirm_repo="acme/notes")
    assert refused.status_code == 409
    assert "A lifecycle request for acme/notes is already open: wait until it is applied or abandoned." in refused.text
    assert len(lifecycles(lp)) == 1


# ---- rule 4 ----

def test_resume_of_an_active_app_and_suspend_of_a_suspended_one_are_refused(lp):
    refused = ask(lp, "resume")
    assert refused.status_code == 409 and "acme/notes is not suspended: there is nothing to resume." in refused.text
    applied(lp)
    refused = ask(lp, "suspend", task="SCTASK0010002")
    assert refused.status_code == 409 and "acme/notes is not active: it is suspended." in refused.text


@pytest.mark.parametrize("action", ["suspend", "resume", "decommission"])
def test_every_action_on_a_decommissioned_app_is_refused(lp, action):
    decommissioned(lp)
    refused = ask(lp, action, task="SCTASK0010002", confirm_repo="acme/notes")
    assert refused.status_code == 409 and DECOMMISSIONED in refused.text
    assert len(lifecycles(lp)) == 1


def test_a_decommission_needs_the_repo_typed_again(lp):
    refused = ask(lp, "decommission", task="SCTASK0010003", confirm_repo="acme/other")
    assert refused.status_code == 400 and "Type the repository&#39;s name, acme/notes, to confirm the decommission." in refused.text
    assert ask(lp, "decommission", task="SCTASK0010003", confirm_repo="acme/notes").status_code == 303


def test_a_decommission_of_a_v1_record_or_a_platform_repo_is_refused(lp):
    lp.store.replace_intake_observed([record(schema="intake/v1")], observed_at=T0)
    refused = ask(lp, "decommission", task="SCTASK0010003", confirm_repo="acme/notes")
    assert refused.status_code == 409 and lifecycle.V1_RECORD.format(repo="acme/notes").replace("'", "&#39;") in refused.text
    lp.store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, T0), RepoAdmin("acme/golden-road-portal", "owner-gh", 1001, T0)],
                                 observed_at=T0)
    lp.store.replace_intake_observed([record("acme/golden-road-portal")], observed_at=T0)
    refused = post(lp, "admin", "/apps/acme/golden-road-portal/lifecycle/decommission", task="SCTASK0010003", reason=REASON,
                   confirm_repo="acme/golden-road-portal")
    assert refused.status_code == 409 and lifecycle.PLATFORM_REPO.format(repo="acme/golden-road-portal") in refused.text
    assert lifecycles(lp) == [] and lifecycles(lp, "acme/golden-road-portal") == []


def test_an_unregistered_app_is_refused(lp):
    lp.store.replace_intake_observed([], observed_at=T0)
    refused = ask(lp)
    assert refused.status_code == 409 and "acme/notes is not registered: the factory has no readable intake record for it." in refused.text


# ---- after decommission: nothing more for the repo (rule 4, item 15) ----

def test_after_decommission_a_binding_and_a_registration_are_refused(lp):
    decommissioned(lp)
    before = len(lp.store.requests_for_repo("acme/notes"))
    binding = post(lp, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    assert binding.status_code == 409 and DECOMMISSIONED in binding.text
    registration = post(lp, "owner", "/register", repo="acme/notes", purpose="Stores short team notes; no email, no files.",
                        data_classification="public", ownership_acknowledged="yes")
    assert registration.status_code == 409 and DECOMMISSIONED in registration.text
    assert len(lp.store.requests_for_repo("acme/notes")) == before


def test_a_register_post_for_a_repo_the_person_isn_t_admin_of_never_says_its_decommission(lp):
    """The decommission (its date and task) is looked up only for one of the person's own fresh admin repos: anyone else learns
    nothing from the refusal."""
    decommissioned(lp)
    lp.store.replace_repo_admins([], observed_at=lp.clock())
    for repo in ("acme/notes", "Acme/Notes"):
        refused = post(lp, "owner", "/register", repo=repo, purpose="Stores short team notes; no email, no files.",
                       data_classification="public", ownership_acknowledged="yes")
        assert refused.status_code == 400 and "You are not an admin of that repository" in refused.text
        assert "SCTASK0010003" not in refused.text and "decommissioned" not in refused.text


def test_the_register_form_for_a_decommissioned_repo_says_so_and_has_no_submit_button(lp):
    decommissioned(lp)
    page = lp.client("owner").get("/register?repo=acme/notes").text
    assert DECOMMISSIONED in page and "Submit for review" not in page
    assert "Submit for review" in lp.client("owner").get("/register?repo=acme/tools").text


def test_the_store_s_refusal_answers_the_same_words(lp, monkeypatch):
    """Decommissioned between the page's check and the insert: the store's Conflict("decommissioned") is said the same way."""
    from portal.web import routes_register
    decommissioned(lp)
    monkeypatch.setattr(routes_register, "decommissioned", lambda store, repo: None)
    registration = post(lp, "owner", "/register", repo="acme/notes", purpose="Stores short team notes; no email, no files.",
                        data_classification="public", ownership_acknowledged="yes")
    assert registration.status_code == 409 and DECOMMISSIONED in registration.text
    real = lp.store.lifecycle_state
    calls = []

    def first_active(repo):
        calls.append(repo)
        return None if len(calls) == 1 else real(repo)
    monkeypatch.setattr(lp.store, "lifecycle_state", first_active)
    binding = post(lp, "owner", "/apps/acme/notes/azure-identity", principal_id="sp-notes")
    assert binding.status_code == 409 and DECOMMISSIONED in binding.text


# ---- Retry and Abandon ----

def failed(portal, action="suspend"):
    req, _ = writing(portal, action)
    portal.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="failed", failure_reason="check_failed: deploy-check",
                                     now=portal.clock())
    return req


def test_retry_is_asked_for_a_failed_step(lp):
    req = failed(lp)
    asked = post(lp, "admin", f"/requests/{req.id}/retry")
    assert asked.status_code == 303 and asked.headers["location"] == f"/requests/{req.id}"
    command = lp.store.open_lifecycle_command(req.id)
    assert (command.command, command.requested_by) == ("retry", lp.people["admin"].id)


def test_retry_without_a_failed_step_is_refused(lp):
    req, _ = writing(lp)
    refused = post(lp, "admin", f"/requests/{req.id}/retry")
    assert refused.status_code == 409 and "Nothing to retry: no step of this request has failed." in refused.text
    assert lp.store.open_lifecycle_command(req.id) is None


def test_abandon_before_the_deploy_merge_and_not_after(lp):
    req, _ = writing(lp, "decommission", "SCTASK0010003")
    assert post(lp, "admin", f"/requests/{req.id}/abandon").status_code == 303
    assert lp.store.open_lifecycle_command(req.id).command == "abandon"
    lp.store.finish_lifecycle_command(command_id=lp.store.open_lifecycle_command(req.id).id, now=lp.clock())
    lp.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=lp.clock())
    refused = post(lp, "admin", f"/requests/{req.id}/abandon")
    assert refused.status_code == 409 and routes_lifecycle.TOO_LATE.replace("'", "&#39;") in refused.text
    assert lp.store.open_lifecycle_command(req.id) is None


def test_a_second_command_while_one_is_open_is_refused(lp):
    req = failed(lp)
    assert post(lp, "admin", f"/requests/{req.id}/retry").status_code == 303
    for command in ("retry", "abandon"):
        refused = post(lp, "admin", f"/requests/{req.id}/{command}")
        assert refused.status_code == 409 and routes_lifecycle.COMMAND_OPEN.replace("'", "&#39;") in refused.text


def test_only_an_admin_asks_and_only_for_an_open_lifecycle_request(lp):
    req = failed(lp)
    for command in ("retry", "abandon"):
        assert post(lp, "owner", f"/requests/{req.id}/{command}").status_code == 403
        assert post(lp, "admin", f"/requests/{lp.seeds['request']}/{command}").status_code == 404   # a registration
        assert post(lp, "admin", f"/requests/r_{'0' * 32}/{command}").status_code == 404
    assert lp.store.open_lifecycle_command(req.id) is None
    done = applied(lp, "resume", "SCTASK0010002", repo="acme/tools")
    refused = post(lp, "admin", f"/requests/{done.id}/abandon")
    assert refused.status_code == 409 and "This request is no longer open." in refused.text


# ---- the app page: the admin's Lifecycle panel, the banner for everyone (Task 19) ----

def panel_buttons(text):
    """{words: (enabled, the hint or href)} of the Lifecycle panel's actions."""
    import re
    part = text.split('id="lifecycle-title"', 1)[1].split("</section>", 1)[0]
    found = {}
    for m in re.finditer(r'<a class="button [a-z]+" href="([^"]+)">(\w+)</a>|<button type="button" class="[a-z]+" disabled>(\w+)</button>'
                         r'<span class="hint">([^<]*)</span>', part):
        found[m.group(2) or m.group(3)] = (True, m.group(1)) if m.group(2) else (False, m.group(4))
    return found


def test_only_an_admin_sees_the_lifecycle_panel(lp):
    assert 'id="lifecycle-title"' not in lp.client("owner").get("/apps/acme/notes").text
    admin = lp.client("admin").get("/apps/acme/notes").text
    assert '<h2 id="lifecycle-title">Lifecycle</h2><span class="badge">Active</span>' in admin
    assert panel_buttons(admin) == {"Suspend": (True, "/apps/acme/notes/lifecycle/suspend"),
                                    "Resume": (False, "acme/notes is not suspended: there is nothing to resume."),
                                    "Decommission": (True, "/apps/acme/notes/lifecycle/decommission")}


SUSPENDED = "Suspended by the platform for ServiceNow task SCTASK0010001 on 2026-10-23. It comes back when the platform resumes it."


def test_a_suspended_app_s_banner_and_panel(lp):
    applied(lp)
    owner, admin = lp.client("owner").get("/apps/acme/notes").text, lp.client("admin").get("/apps/acme/notes").text
    assert f'<p class="callout warn" role="status">{SUSPENDED}</p>' in owner and SUSPENDED in admin
    assert '<span class="badge">Suspended since 2026-10-23 by task SCTASK0010001</span>' in admin
    buttons = panel_buttons(admin)
    assert buttons["Resume"] == (True, "/apps/acme/notes/lifecycle/resume")
    assert buttons["Suspend"] == (False, "acme/notes is not active: it is suspended.") and buttons["Decommission"][0] is True


def test_a_decommissioned_app_s_banner_panel_and_connections(lp):
    req = decommissioned(lp, merged=False)
    writing_text = lp.client("owner").get("/apps/acme/notes").text
    assert ('<p class="callout error" role="status">Decommissioned on 2026-10-23 for task SCTASK0010003. The platform is finishing it: '
            "the archive, then the intake record.</p>") in writing_text
    lp.store.move_lifecycle_step(request_id=req.id, step="archive", to_state="done", now=lp.clock())
    archived = lp.client("owner").get("/apps/acme/notes").text
    assert ('<p class="callout error" role="status">Decommissioned on 2026-10-23 for task SCTASK0010003. The repository is archived: '
            "the platform is finishing the intake record.</p>") in archived
    lp.store.move_lifecycle_step(request_id=req.id, step="factory", to_state="done", now=lp.clock())
    lp.store.apply_lifecycle(request_id=req.id, merge_sha="3" * 40, note="Applied in git.", now=lp.clock())
    owner = lp.client("owner").get("/apps/acme/notes").text
    assert "Decommissioned on 2026-10-23 for task SCTASK0010003. The repository is archived." in owner
    assert '<button class="secondary" type="submit" disabled>Associate</button><span class="hint">The app is decommissioned.</span>' in owner
    assert "Change or renew the registration" not in owner
    admin = lp.client("admin").get("/apps/acme/notes").text
    assert '<span class="badge">Decommissioned on 2026-10-23 (task SCTASK0010003)</span>' in admin
    assert {words: hint for words, (_, hint) in panel_buttons(admin).items()} == dict.fromkeys(("Suspend", "Resume", "Decommission"),
                                                                                               DECOMMISSIONED)


def test_an_open_lifecycle_request_is_linked_and_holds_every_action(lp):
    assert ask(lp).status_code == 303
    [req] = lifecycles(lp)
    admin = lp.client("admin").get("/apps/acme/notes").text
    assert f'A lifecycle request is open: <a href="/requests/{req.id}">suspend for SCTASK0010001</a>.' in admin
    one_open = "A lifecycle request for acme/notes is already open: wait until it is applied or abandoned."
    assert {w: hint for w, (_, hint) in panel_buttons(admin).items()} == dict.fromkeys(("Suspend", "Resume", "Decommission"), one_open)


def test_without_servicenow_every_action_says_so(lp):
    lp.app.state.settings = dataclasses.replace(lp.settings, servicenow_api=None)
    admin = lp.client("admin").get("/apps/acme/notes").text
    assert {w: hint for w, (_, hint) in panel_buttons(admin).items()} == dict.fromkeys(("Suspend", "Resume", "Decommission"),
                                                                                       lifecycle.NO_SERVICENOW)


# ---- the lifecycle request page and the mock task page (Task 20) ----

PR = dict(branch="portal/lifecycle-x-deploy-1", pr_number=1, pr_url="https://github.com/acme/golden-road-deploy/pull/1", head_sha="2" * 40,
          base_sha="1" * 40)
WAITING = "Waiting: 2 attempts, last error ServiceNow answered 503 for the task&#39;s work note"


def test_the_request_page_shows_the_timeline_the_pr_and_what_servicenow_was_told(lp):
    req, _ = writing(lp)
    lp.store.move_lifecycle_step_with_event(request_id=req.id, step="deploy", to_state="pr_open", now=lp.clock(), event="pr_opened",
                                            note="The deploy repo pull request is open.", **PR)
    [requested, opened] = lp.store.lifecycle_events(req.id)
    lp.store.mark_lifecycle_event_sent(event_id=requested.id, now=lp.clock())
    for _ in range(2):
        lp.store.mark_lifecycle_event_failed(event_id=opened.id, error="ServiceNow answered 503 for the task's work note", now=lp.clock())
    page = lp.client("admin").get(f"/requests/{req.id}")
    text = page.text
    assert page.status_code == 200 and "<h1>Suspend <span class=\"mono\">acme/notes</span></h1>" in text
    assert '<span class="step-label">Deploy repo pull request</span><span class="step-state">Now</span><span class="step-detail">#1</span>' in text
    assert '<a href="https://github.com/acme/golden-road-deploy/pull/1" rel="noopener noreferrer">#1</a>' in text
    assert "What ServiceNow was told" in text and "Sent at <time" in text and WAITING in text
    assert '<a href="/mock/servicenow/tasks/SCTASK0010001">SCTASK0010001</a>' in text
    assert "Abandon this request? The writer closes its pull requests. The ServiceNow task stays open." in text
    assert 'action="/requests/' + req.id + '/abandon"' in text and 'action="/requests/' + req.id + '/retry"' not in text
    assert "the writer ignores the abandon" in text


@pytest.mark.parametrize("url", ["javascript:alert(1)//https://github.com/acme/golden-road-deploy/pull/1",
                                 "http://github.com/acme/golden-road-deploy/pull/1", "https://github.example/acme/golden-road-deploy/pull/1",
                                 "https://user@github.com/acme/golden-road-deploy/pull/1", "https://github.com.evil.example/acme/x/pull/1",
                                 "//github.com/acme/golden-road-deploy/pull/1"])
def test_a_pull_request_link_is_only_an_https_link_on_the_configured_github_host(lp, url):
    req, [step] = writing(lp)
    odd = dataclasses.replace(step, state="pr_open", pr_number=1, pr_url=url)
    assert routes_lifecycle.pr_steps([odd], lp.settings.github_host) == []
    good = dataclasses.replace(odd, pr_url=PR["pr_url"])
    assert routes_lifecycle.pr_steps([good], lp.settings.github_host) == [{"label": "Deploy repo pull request", "url": PR["pr_url"],
                                                                          "number": 1, "attempt": 1}]
    assert routes_lifecycle.pr_steps([good], "github.example") == []
    real = lp.store.lifecycle_steps
    lp.store.lifecycle_steps = lambda request_id: [odd]
    try:
        text = lp.client("admin").get(f"/requests/{req.id}").text
    finally:
        lp.store.lifecycle_steps = real
    assert "Pull requests</h3>" not in text and url.replace("&", "&amp;") not in text


def test_retry_shows_only_with_a_failed_step_and_abandon_only_before_the_deploy_step_is_done(lp):
    req = failed(lp, "decommission")
    text = lp.client("admin").get(f"/requests/{req.id}").text
    assert f'action="/requests/{req.id}/retry"' in text and f'action="/requests/{req.id}/abandon"' in text
    lp.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="planned", now=lp.clock())
    lp.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=lp.clock())
    lp.store.move_lifecycle_step(request_id=req.id, step="archive", to_state="failed", failure_reason="archive_refused", now=lp.clock())
    text = lp.client("admin").get(f"/requests/{req.id}").text
    assert f'action="/requests/{req.id}/retry"' in text and f'action="/requests/{req.id}/abandon"' not in text
    lp.store.move_lifecycle_step(request_id=req.id, step="archive", to_state="planned", now=lp.clock())
    text = lp.client("admin").get(f"/requests/{req.id}").text
    assert "/retry" not in text and "/abandon" not in text


def test_a_pending_command_hides_both_and_says_the_writer_carries_it_out(lp):
    req = failed(lp)
    assert post(lp, "admin", f"/requests/{req.id}/retry").status_code == 303
    text = lp.client("admin").get(f"/requests/{req.id}").text
    assert "Retry asked by Admin at <time" in text and "the writer carries it out on its next pass." in text
    assert f'action="/requests/{req.id}/retry"' not in text and f'action="/requests/{req.id}/abandon"' not in text


def test_the_owner_can_t_open_an_admin_s_lifecycle_request(lp):
    req, _ = writing(lp)
    assert lp.client("owner").get(f"/requests/{req.id}").status_code == 404


def sent_notes_for(portal, task="SCTASK0010001", completed=False, note="Opened the deploy repo pull request."):
    req, _ = writing(portal, task=task)
    portal.store.add_lifecycle_event(request_id=req.id, step="pr_opened", note=note, now=portal.clock())
    if completed:
        portal.store.move_lifecycle_step(request_id=req.id, step="deploy", to_state="done", now=portal.clock())
        portal.store.apply_lifecycle(request_id=req.id, merge_sha="3" * 40, note="Suspended in git. The task is closed.", now=portal.clock())
    mark_sent(portal)
    return req


def test_the_mock_task_page_shows_the_sent_notes_in_order(lp):
    req = sent_notes_for(lp)
    page = lp.client("admin").get("/mock/servicenow/tasks/SCTASK0010001")
    text = page.text
    assert page.status_code == 200 and "<h1 class=\"mono\">SCTASK0010001</h1><span class=\"badge\">Open</span>" in text
    assert "This is the mock ServiceNow's task, rebuilt from the notes the collector sent." in text
    link = f"Golden road portal request: https://portal.example.test/requests/{req.id}"
    first, second = text.index("Requested in the golden road portal by Admin"), text.index("Opened the deploy repo pull request.")
    assert first < second and text.count(link) == 2
    assert "Suspend hello-app: critical vulnerability (demo)" in text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert lp.settings.servicenow_api.task_history("SCTASK0010001") == []   # replayed into a fresh mock, never the shared one


def test_an_unsent_note_is_not_on_the_mock_task_page(lp):
    req, _ = writing(lp)
    text = lp.client("admin").get("/mock/servicenow/tasks/SCTASK0010001").text
    assert "No work notes yet." in text and req.id not in text


def test_a_completed_note_closes_the_mock_task(lp):
    sent_notes_for(lp, completed=True)
    text = lp.client("admin").get("/mock/servicenow/tasks/SCTASK0010001").text
    assert '<span class="badge">Closed Complete</span>' in text and "<h3>Close notes</h3>" in text
    assert text.count("<h3>Work note</h3>") == 2


def test_a_note_s_text_is_escaped_on_the_mock_task_page(lp):
    sent_notes_for(lp, note='A note with <script>alert("x")</script> in it.')
    page = lp.client("admin").get("/mock/servicenow/tasks/SCTASK0010001")
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in page.text and "<script>alert" not in page.text
    from test_portal_csp_pages import problems
    assert problems(page.text) == []
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp


@pytest.mark.parametrize("number", ["SCTASK0010009", "SCTASK1", "sctask0010001", "SCTASK00100011"])
def test_an_unknown_or_malformed_task_is_not_found(lp, number):
    assert lp.client("admin").get(f"/mock/servicenow/tasks/{number}").status_code == 404


def test_the_mock_task_page_is_an_admin_s_and_only_in_the_mock(lp):
    assert lp.client("owner").get("/mock/servicenow/tasks/SCTASK0010001").status_code == 403
    assert lp.client().get("/mock/servicenow/tasks/SCTASK0010001").status_code == 303
    lp.app.state.settings = dataclasses.replace(lp.settings, servicenow_api=None)
    assert lp.client("admin").get("/mock/servicenow/tasks/SCTASK0010001").status_code == 404
    lp.app.state.settings = dataclasses.replace(lp.settings, servicenow_api=down())   # a ServiceNow that isn't the mock
    assert lp.client("admin").get("/mock/servicenow/tasks/SCTASK0010001").status_code == 404


# ---- the final review: Stop (I-1), a command on a refused request (M-1) ----

def stalled(portal):
    """A decommission whose deploy step is done and whose archive failed: too late to abandon, so Stop is the way out."""
    req = decommissioned(portal, merged=False)
    portal.store.move_lifecycle_step(request_id=req.id, step="archive", to_state="failed", failure_reason="archive: github: 404",
                                     now=portal.clock())
    return req


def test_stop_is_asked_only_for_a_decommission_whose_deploy_step_is_done(lp):
    early, _ = writing(lp, "decommission", "SCTASK0010003", repo="acme/tools")
    refused = post(lp, "admin", f"/requests/{early.id}/stop")
    assert refused.status_code == 409 and routes_lifecycle.NOT_STOPPABLE.replace("'", "&#39;") in refused.text
    suspend = failed(lp)
    assert post(lp, "admin", f"/requests/{suspend.id}/stop").status_code == 409
    assert lp.store.open_lifecycle_command(early.id) is None and lp.store.open_lifecycle_command(suspend.id) is None
    lp.store.mark_failed(request_id=suspend.id, reason="recheck: not_admin", now=lp.clock())  # one open request per app
    req = stalled(lp)
    asked = post(lp, "admin", f"/requests/{req.id}/stop")
    assert asked.status_code == 303 and asked.headers["location"] == f"/requests/{req.id}"
    assert lp.store.open_lifecycle_command(req.id).command == "stop"
    assert post(lp, "owner", f"/requests/{req.id}/stop").status_code == 403


def test_the_request_page_offers_stop_where_abandon_is_too_late(lp):
    req = stalled(lp)
    text = lp.client("admin").get(f"/requests/{req.id}").text
    assert f'action="/requests/{req.id}/stop"' in text and f'action="/requests/{req.id}/abandon"' not in text
    assert f'action="/requests/{req.id}/retry"' in text
    assert "Stop this request? The writer closes the steps still to do and does no more. The ServiceNow task stays open." in text
    early, _ = writing(lp, "decommission", "SCTASK0010003", repo="acme/tools")
    assert "/stop" not in lp.client("admin").get(f"/requests/{early.id}").text


def test_a_stopped_request_ends_stopped_and_its_banner_says_so(lp):
    req = stalled(lp)
    command = lp.store.add_lifecycle_command(request_id=req.id, command="stop", actor_id=lp.people["admin"].id, now=lp.clock())
    lp.store.stop_lifecycle(request_id=req.id, command_id=command.id, note="Stopped by Admin.", now=lp.clock())
    text = lp.client("admin").get(f"/requests/{req.id}").text
    assert '<span class="step-label">Stopped</span><span class="step-state">Closed</span>' in text and "/stop" not in text
    owner = lp.client("owner").get("/apps/acme/notes").text
    assert ("Decommissioned on 2026-10-23 for task SCTASK0010003. The platform stopped before finishing it: the repository is not "
            "archived, and the intake record is not marked.") in owner


def test_a_command_left_on_a_request_that_is_no_longer_open_is_not_shown_as_waiting(lp):
    """M-1: a request refused with a command still open never says the writer carries it out."""
    req = lifecycle_request(lp.store, lp.people, lp.clock(), claim=False)
    lp.store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=lp.people["admin"].id, now=lp.clock())
    lp.store.claim_approved(now=lp.clock())
    lp.store.mark_failed(request_id=req.id, reason="recheck: nothing_to_suspend", now=lp.clock())
    text = lp.client("admin").get(f"/requests/{req.id}").text
    assert "the writer carries it out on its next pass" not in text


@pytest.mark.parametrize("action", ["suspend", "decommission"])
def test_the_form_says_the_reason_is_committed_to_git(lp, action):
    """M-3: the reason is in the PRs (and a suspend's lifecycle.yaml), public in the PoC: name the task, not the vulnerability."""
    text = lp.client("admin").get(f"/apps/acme/notes/lifecycle/{action}").text
    assert ("it is committed to git (the pull requests, and a suspend's lifecycle.yaml), where anyone who can read the repo sees it. "
            "Name the task; leave a vulnerability's details out.") in text

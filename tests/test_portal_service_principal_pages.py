"""Creating a service principal from the app page (spec 2026-11-16 "What the person sees", Errors): the form, its POST, and what the
server decides from the catalog."""

import dataclasses

import pytest
from portal_fakes import mock_servicenow

from portal import service_principals as sp

NEW = "/apps/acme/notes/service-principals/new"


def with_servicenow(portal):
    portal.app.state.settings = portal.settings = dataclasses.replace(portal.settings, servicenow=True, servicenow_api=mock_servicenow())
    return portal


def ask(portal, who="owner", name="sp-notes-calendar", scopes=("Calendars.Read",), justification="", csrf=True):
    data = {"csrf_token": portal.csrfs[who]} if csrf and who in portal.csrfs else {}
    data |= {"name": name, "justification": justification, "scope": list(scopes)}   # httpx sends a list as the field repeated
    return portal.client(who).post(NEW, data=data)


def sp_requests(portal):
    return portal.store.service_principal_requests(repo="acme/notes")


def test_the_form_shows_both_boxes_the_risks_and_the_prefilled_name(portal):
    page = portal.client("owner").get(NEW)
    assert page.status_code == 200
    assert 'value="sp-notes-"' in page.text and "Pre-approved scopes" in page.text and "Scopes that need approval" in page.text
    assert 'value="Calendars.Read" data-tier="approved"' in page.text and 'value="Calendars.ReadWrite" data-tier="requestable"' in page.text
    assert "The app can change or delete anyone&#39;s meetings, not only read them." in page.text
    assert ">Create or request</button>" in page.text and '<script src="/static/service_principal.js" defer></script>' in page.text
    assert "ServiceNow isn't set up here, so these can't be requested." in page.text   # the fixture has no ServiceNow (template text: not escaped)


def test_pre_approved_scopes_only_are_recorded_approved_and_shown_on_the_request_page(portal):
    response = ask(portal, scopes=("Calendars.Read", "Contacts.Read"))
    [req] = sp_requests(portal)
    assert response.status_code == 303 and response.headers["location"] == f"/requests/{req.id}"
    assert (req.state, req.scopes, req.justification, req.requested_by) == ("approved", ("Calendars.Read", "Contacts.Read"), None,
                                                                            portal.people["owner"].id)


def test_a_requestable_scope_is_a_pending_request_with_its_justification(portal):
    with_servicenow(portal)
    assert ask(portal, scopes=("Calendars.ReadWrite",), justification="Books meeting rooms.").status_code == 303
    [req] = sp_requests(portal)
    assert (req.state, req.justification) == ("pending", "Books meeting rooms.")


def test_a_portal_admin_who_isn_t_the_repo_s_admin_may_create_one(portal):
    assert ask(portal, who="admin").status_code == 303   # decided while planning: item 12
    assert sp_requests(portal)[0].requested_by == portal.people["admin"].id


@pytest.mark.parametrize("kwargs, words", [
    ({"scopes": ()}, sp.NO_SCOPE),
    ({"scopes": ("Directory.ReadWrite.All",)}, "Directory.ReadWrite.All is not a scope the platform offers."),
    ({"scopes": ("Calendars.Read", "Calendars.Read")}, "Calendars.Read is ticked twice."),
    ({"name": "sp-other-x"}, "The name is sp-notes- and then lowercase letters"),
    ({"scopes": ("Calendars.ReadWrite",), "justification": "Rooms."}, sp.NO_SERVICENOW.replace("'", "&#39;")),
])
def test_each_problem_is_a_400_with_the_form_again(portal, kwargs, words):
    response = ask(portal, **kwargs)
    assert response.status_code == 400 and words in response.text and 'name="scope"' in response.text and sp_requests(portal) == []


def test_a_requestable_scope_without_a_justification_says_why_it_is_needed(portal):
    with_servicenow(portal)
    response = ask(portal, scopes=("Calendars.ReadWrite",))
    assert response.status_code == 400 and "Say why this app needs Calendars.ReadWrite." in response.text


def test_a_name_in_use_is_refused_by_the_directory_the_store_and_any_case(portal):
    assert ask(portal).status_code == 303
    again = ask(portal)
    assert again.status_code == 400 and "sp-notes-calendar is already used: choose another name." in again.text
    assert len(sp_requests(portal)) == 1


def test_the_form_keeps_what_was_typed(portal):
    response = ask(portal, name="sp-notes-Bad", scopes=("Contacts.Read",), justification="kept")
    assert 'value="sp-notes-Bad"' in response.text and 'value="Contacts.Read" data-tier="approved" checked' in response.text
    assert ">kept</textarea>" in response.text


def test_what_was_typed_is_escaped_on_the_form_again(portal):
    response = ask(portal, name='sp-notes-"><script>x</script>', scopes=("<b>Mail</b>",), justification="</textarea><script>y</script>")
    assert response.status_code == 400 and "<script>" not in response.text and "<b>Mail</b>" not in response.text
    assert "&lt;b&gt;Mail&lt;/b&gt; is not a scope the platform offers." in response.text
    assert "&lt;/textarea&gt;&lt;script&gt;y&lt;/script&gt;</textarea>" in response.text


def test_without_the_directory_there_is_no_form(portal):
    portal.app.state.settings = dataclasses.replace(portal.settings, directory=None, graph_scopes=None)
    assert portal.client("owner").get(NEW).status_code == 404 and ask(portal).status_code == 404


def test_someone_who_isn_t_the_app_s_admin_gets_404_and_a_post_needs_its_csrf_token(portal):
    assert portal.client("other").get(NEW).status_code == 404 and ask(portal, who="other").status_code == 404
    assert ask(portal, csrf=False).status_code == 403 and sp_requests(portal) == []


from portal_fakes import SP_PENDING, sp_request  # noqa: E402


def pending_with_task(portal, who="owner"):
    req = sp_request(portal.store, portal.people[who].id, portal.clock(), SP_PENDING)
    portal.store.record_service_principal_task(request_id=req.id, task_number="SCTASK0010007", task_sys_id="7" * 32, now=portal.clock())
    return req


def test_the_request_page_shows_the_scopes_the_state_and_the_task(portal):
    req = pending_with_task(portal)
    page = portal.client("owner").get(f"/requests/{req.id}")
    assert page.status_code == 200 and '<h1 class="mono">sp-notes-rooms</h1>' in page.text
    assert "Waiting for approval in SCTASK0010007" in page.text and "Books meeting rooms for the team." in page.text
    assert '<span class="mono">Calendars.ReadWrite</span> (Read and write calendars) <span class="tag">needs approval</span>' in page.text
    assert '<span class="mono">Calendars.Read</span> (Read calendars)</li>' in page.text
    assert "/mock/servicenow/tasks/" not in page.text   # the mock's page is an admin's
    with_servicenow(portal)
    assert 'href="/mock/servicenow/tasks/SCTASK0010007"' in portal.client("admin").get(f"/requests/{req.id}").text
    assert portal.client("other").get(f"/requests/{req.id}").status_code == 404


def test_a_rejected_request_says_why_and_a_created_one_its_client_id(portal):
    req = pending_with_task(portal)
    portal.store.decide_service_principal_request(request_id=req.id, approved=False, close_notes="Use Calendars.Read.", now=portal.clock())
    assert "Rejected: Use Calendars.Read." in portal.client("owner").get(f"/requests/{req.id}").text
    done = sp_request(portal.store, portal.people["owner"].id, portal.clock())
    made = portal.store.create_service_principal(request_id=done.id, principal_id="sp-" + "a" * 24,
                                                 client_id="0e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f", now=portal.clock())
    page = portal.client("owner").get(f"/requests/{done.id}").text
    assert made.client_id in page and "None: every scope is pre-approved" in page and '<span class="badge state-created">Created</span>' in page


def test_a_failure_is_shown_while_the_collector_retries(portal):
    req = pending_with_task(portal)
    portal.store.record_service_principal_failure(request_id=req.id, error="ServiceNow answered 503 for the task", now=portal.clock())
    assert "The collector tried 1 time and will try again: ServiceNow answered 503 for the task" in portal.client("owner").get(
        f"/requests/{req.id}").text


def test_my_requests_names_the_kind_and_the_approvals_page_never_lists_it(portal):
    pending_with_task(portal)
    mine = portal.client("owner").get("/requests").text
    assert "Service principal (sp-notes-rooms)" in mine and '<span class="badge state-pending">Pending</span>' in mine
    approvals = portal.client("admin").get("/admin/approvals").text
    assert "sp-notes-rooms" not in approvals   # decided while planning: item 22


def test_an_admin_can_t_send_back_or_approve_a_service_principal_request(portal):
    """Only a registration is decided on the Approvals page (decided while planning: item 22; review I2): send-back answers 404 for
    any other kind (routes_admin._pending), approve keeps its 409, and the store refuses both (wrong_role) whatever the web does."""
    req = pending_with_task(portal)
    admin = portal.client("admin")
    assert admin.post(f"/admin/requests/{req.id}/send-back", data={"csrf_token": portal.csrfs["admin"],
                                                                   "justification": "Say what it does not do."}).status_code == 404
    assert admin.post(f"/admin/requests/{req.id}/approve", data={"csrf_token": portal.csrfs["admin"], "submission": "1"}).status_code == 409
    assert portal.store.service_principal_request(req.id).state == "pending"


def test_the_request_page_escapes_the_justification_and_the_close_notes(portal):
    req = sp_request(portal.store, portal.people["owner"].id, portal.clock(), {**SP_PENDING, "justification": "<script>a</script>"})
    portal.store.record_service_principal_task(request_id=req.id, task_number="SCTASK0010007", task_sys_id="7" * 32, now=portal.clock())
    portal.store.decide_service_principal_request(request_id=req.id, approved=False, close_notes="<b>no</b>", now=portal.clock())
    page = portal.client("owner").get(f"/requests/{req.id}").text
    assert "<script>a" not in page and "<b>no" not in page
    assert "&lt;script&gt;a&lt;/script&gt;" in page and "Rejected: &lt;b&gt;no&lt;/b&gt;" in page


def test_the_card_links_the_form_for_the_app_s_admin_and_a_portal_admin(portal):
    for who in ("owner", "admin"):
        assert f'href="{NEW}">Create a service principal</a>' in portal.client(who).get("/apps/acme/notes").text, who
    portal.app.state.settings = dataclasses.replace(portal.settings, graph_scopes=None)
    assert "Create a service principal" not in portal.client("owner").get("/apps/acme/notes").text


def test_the_card_lists_created_principals_and_the_other_requests_with_their_state(portal):
    from portal_fakes import CREATED_CLIENT_ID, created_principal
    created_principal(portal.store, portal.people["owner"].id, portal.clock())
    waiting = pending_with_task(portal)
    rejected = sp_request(portal.store, portal.people["admin"].id, portal.clock(), {**SP_PENDING, "display_name": "sp-notes-mail"})
    portal.store.record_service_principal_task(request_id=rejected.id, task_number="SCTASK0010008", task_sys_id="8" * 32, now=portal.clock())
    portal.store.decide_service_principal_request(request_id=rejected.id, approved=False, close_notes="Not for this app.", now=portal.clock())
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "Created in the portal" in page and CREATED_CLIENT_ID in page and "<td class=\"mono\">Calendars.Read</td>" in page
    assert f'<a href="/requests/{waiting.id}">sp-notes-rooms</a>' in page and "Waiting for approval in SCTASK0010007" in page
    assert "Rejected: Not for this app." in page and f'href="/requests/{rejected.id}"' not in page   # the admin's request: plain text
    assert f'href="/requests/{rejected.id}"' in portal.client("admin").get("/apps/acme/notes").text


def test_the_card_escapes_a_rejected_request_s_close_notes(portal):
    req = pending_with_task(portal)
    portal.store.decide_service_principal_request(request_id=req.id, approved=False, close_notes="<i>never</i>", now=portal.clock())
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "<i>never" not in page and "Rejected: &lt;i&gt;never&lt;/i&gt;" in page


from portal.collectors.service_principals import collect_service_principals  # noqa: E402
from portal.directory_client import MockDirectoryClient  # noqa: E402

TASK_PAGE = "/mock/servicenow/tasks/SCTASK0010007"


def collector_pass(portal):
    """One collector pass as the collector process runs it: its own mock, fresh, with the store replayed."""
    settings = portal.settings
    return collect_service_principals(portal.store, sp.Replayed(settings.servicenow_api, portal.store, settings.base_url, settings.graph_scopes),
                                      MockDirectoryClient(), base_url=settings.base_url, now=portal.clock(), catalog=settings.graph_scopes)


def opened_by_the_collector(portal, who="owner"):
    req = sp_request(portal.store, portal.people[who].id, portal.clock(), SP_PENDING)
    assert collector_pass(portal)["opened"] == 1
    return req


def decide(portal, action, who="admin", close_notes="", number="SCTASK0010007", csrf=True):
    data = {"close_notes": close_notes, **({"csrf_token": portal.csrfs[who]} if csrf and who in portal.csrfs else {})}
    return portal.client(who).post(f"/mock/servicenow/tasks/{number}/{action}", data=data)


def test_the_task_page_shows_the_portal_s_task_and_the_approver_s_buttons(portal):
    opened_by_the_collector(with_servicenow(portal))
    page = portal.client("admin").get(TASK_PAGE).text
    assert '<h1 class="mono">SCTASK0010007</h1><span class="badge">Open</span>' in page
    assert "Service principal for acme/notes: sp-notes-rooms" in page and "Calendars.ReadWrite (needs approval)" in page
    assert "Mock: acting as the ServiceNow approver" in page and 'action="/mock/servicenow/tasks/SCTASK0010007/approve"' in page
    assert 'action="/mock/servicenow/tasks/SCTASK0010007/reject"' in page


def test_approve_then_the_collector_creates_it_and_it_is_in_the_link_list(portal):
    req = opened_by_the_collector(with_servicenow(portal))
    response = decide(portal, "approve")
    assert response.status_code == 303 and response.headers["location"] == TASK_PAGE
    assert '<span class="badge">Closed Complete</span>' in portal.client("admin").get(TASK_PAGE).text
    counts = collector_pass(portal)
    assert (counts["approved"], counts["created"], counts["noted"]) == (1, 1, 1)
    [made] = portal.store.service_principals("acme/notes")
    assert f'value="{made.id}"' in portal.client("owner").get("/apps/acme/notes").text
    page = portal.client("admin").get(TASK_PAGE).text
    assert f"client ID {made.client_id}" in page and "Mock: acting as the ServiceNow approver" not in page
    assert portal.store.service_principal_request(req.id).state == "created"


def test_reject_needs_close_notes_and_the_request_says_them(portal):
    req = opened_by_the_collector(with_servicenow(portal))
    assert decide(portal, "reject").status_code == 400 and portal.store.mock_task_decisions() == []
    assert decide(portal, "reject", close_notes="Use Calendars.Read instead.").status_code == 303
    assert '<span class="badge">Closed Incomplete</span>' in portal.client("admin").get(TASK_PAGE).text
    assert collector_pass(portal)["rejected"] == 1
    assert "Rejected: Use Calendars.Read instead." in portal.client("owner").get(f"/requests/{req.id}").text


def test_the_requester_can_t_decide_their_own_task_even_as_an_admin(portal):
    opened_by_the_collector(with_servicenow(portal), who="admin")
    assert "another admin decides its task" in portal.client("admin").get(TASK_PAGE).text
    assert decide(portal, "approve").status_code == 403 and portal.store.mock_task_decisions() == []


def test_a_task_is_decided_once_and_only_a_service_principal_task_is_decided_here(portal):
    opened_by_the_collector(with_servicenow(portal))
    assert decide(portal, "approve").status_code == 303
    assert decide(portal, "reject", close_notes="Too late.").status_code == 409
    assert decide(portal, "approve", number="SCTASK0010001").status_code == 404   # a lifecycle fixture task
    assert decide(portal, "approve", number="SCTASK7").status_code == 404
    assert decide(portal, "approve", who="owner").status_code == 403   # ADMIN routes
    assert decide(portal, "reject", close_notes="x" * 2001).status_code == 409   # decided: 409 before any length check


@pytest.mark.parametrize("notes", ["Use\u0000 Calendars.Read.", "Use \u202eCalendars.Read.", "Use\u200b Calendars.Read."])
def test_close_notes_with_a_control_or_format_character_are_a_400_and_nothing_is_stored(portal, notes):
    """Final review M2 (a lone surrogate can't come through a form: the rule's own test has it). Postgres refuses a NUL: a 500 before."""
    opened_by_the_collector(with_servicenow(portal))
    response = decide(portal, "reject", close_notes=notes)
    assert response.status_code == 400 and "control or invisible formatting characters" in response.text
    assert portal.store.mock_task_decisions() == []


def test_a_justification_with_a_nul_is_a_400_and_nothing_is_stored(portal):
    with_servicenow(portal)
    response = ask(portal, scopes=("Calendars.ReadWrite",), justification="Books\u0000 rooms.")
    assert response.status_code == 400 and "control or invisible formatting characters" in response.text and sp_requests(portal) == []


def test_without_the_mock_service_now_the_buttons_are_404(portal):
    assert decide(portal, "approve").status_code == 404


def test_approve_takes_optional_close_notes_and_reject_refuses_notes_too_long(portal):
    opened_by_the_collector(with_servicenow(portal))
    assert decide(portal, "reject", close_notes="x" * 2001).status_code == 400 and portal.store.mock_task_decisions() == []
    assert decide(portal, "approve", close_notes="Checked with security.").status_code == 303
    [decision] = portal.store.mock_task_decisions()
    assert (decision.state, decision.close_notes, decision.decided_by) == ("3", "Checked with security.", portal.people["admin"].id)


def test_the_task_page_escapes_the_justification_and_the_close_notes(portal):
    with_servicenow(portal)
    sp_request(portal.store, portal.people["owner"].id, portal.clock(), {**SP_PENDING, "justification": "<script>j</script>"})
    assert collector_pass(portal)["opened"] == 1
    assert decide(portal, "reject", close_notes="<img src=x>").status_code == 303
    page = portal.client("admin").get(TASK_PAGE).text
    assert "<script>j" not in page and "&lt;script&gt;j&lt;/script&gt;" in page
    assert "<img src=x>" not in page and "&lt;img src=x&gt;" in page


def test_the_mock_task_pages_render_with_a_request_whose_task_is_a_fixture_s(portal):
    """Round 5 review: a stored service principal request naming a fixture task never breaks the mock's task pages."""
    with_servicenow(portal)
    req = sp_request(portal.store, portal.people["owner"].id, portal.clock(), SP_PENDING)
    portal.store.record_service_principal_task(request_id=req.id, task_number="SCTASK0010001", task_sys_id="8" * 32, now=portal.clock())
    for number in ("SCTASK0010001", "SCTASK0010002"):
        assert portal.client("admin").get(f"/mock/servicenow/tasks/{number}").status_code == 200, number


def test_close_notes_count_a_crlf_line_break_as_one_character(portal):
    """Round 5 review: as the justification, close notes are normalised (CRLF to LF) before they are trimmed and measured."""
    opened_by_the_collector(with_servicenow(portal))
    notes = "\r\n".join(["x" * 99] * 20)   # 2018 characters as posted, 1999 with LF line breaks
    assert len(notes) > 2000 and decide(portal, "reject", close_notes=notes).status_code == 303
    [decision] = portal.store.mock_task_decisions()
    assert decision.close_notes == notes.replace("\r\n", "\n")

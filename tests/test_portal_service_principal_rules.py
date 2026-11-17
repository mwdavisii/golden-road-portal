"""The service principal form's rules (spec 2026-11-16 S1, S3, S11, Errors), pure: the server decides the tier from the catalog."""

import pytest

from portal import service_principals as sp
from portal.graph_scopes import load_mock

CATALOG = load_mock()
REPO = "acme/notes"


def rules(name="sp-notes-calendar", scopes=("Calendars.Read",), justification="", taken=(), servicenow=True, repo=REPO):
    return sp.form_problems(CATALOG, repo, name, list(scopes), justification, taken=set(taken), servicenow=servicenow)


@pytest.mark.parametrize("repo, prefix", [("acme/notes", "sp-notes-"), ("acme/Hello_App.v2", "sp-hello-app-v2-"), ("Acme/x", "sp-x-")])
def test_the_name_s_prefix_is_the_repo_s_name_shaped(repo, prefix):
    assert sp.name_prefix(repo) == prefix


def test_pre_approved_scopes_only_are_approved_with_no_justification():
    draft, state, problems = rules(scopes=("Contacts.Read", "Calendars.Read"), justification="ignored: nothing needs approval")
    assert problems == [] and state == "approved"
    assert draft == {"schema": "service-principal-request/v1", "repo": REPO, "display_name": "sp-notes-calendar",
                     "scopes": ["Calendars.Read", "Contacts.Read"]}


def test_a_requestable_scope_makes_it_a_pending_request_with_its_justification():
    draft, state, problems = rules(scopes=("Calendars.ReadWrite", "Calendars.Read"), justification="  Books rooms for the team.\n")
    assert problems == [] and state == "pending"
    assert draft["scopes"] == ["Calendars.Read", "Calendars.ReadWrite"] and draft["justification"] == "Books rooms for the team."


@pytest.mark.parametrize("kwargs, problem", [
    ({"scopes": ()}, sp.NO_SCOPE),
    ({"scopes": ("Calendars.Read", "Calendars.Read")}, "Calendars.Read is ticked twice."),
    ({"scopes": ("Directory.ReadWrite.All",)}, "Directory.ReadWrite.All is not a scope the platform offers."),
    ({"scopes": ("Calendars.ReadWrite",)}, "Say why this app needs Calendars.ReadWrite."),
    ({"scopes": ("Calendars.ReadWrite", "Mail.Send"), "justification": "   "}, "Say why this app needs Calendars.ReadWrite and Mail.Send."),
    ({"scopes": ("Calendars.ReadWrite",), "justification": "x" * 2001}, sp.TOO_LONG),
    ({"scopes": ("Calendars.ReadWrite",), "justification": "Books rooms.", "servicenow": False}, sp.NO_SERVICENOW),
    ({"scopes": ("Calendars.Read",) * 65}, sp.TOO_MANY),
    ({"name": "sp-notes-"}, sp.BAD_NAME.format(prefix="sp-notes-")),
    ({"name": "sp-other-calendar"}, sp.BAD_NAME.format(prefix="sp-notes-")),
    ({"name": "sp-notes-Calendar"}, sp.BAD_NAME.format(prefix="sp-notes-")),
    ({"name": "sp-notes-" + "x" * 60}, sp.BAD_NAME.format(prefix="sp-notes-")),   # 65 characters after sp-
    ({"name": "sp-notes-calendar", "taken": {"SP-Notes-Calendar"}}, "sp-notes-calendar is already used: choose another name."),
])
def test_each_problem_is_named(kwargs, problem):
    draft, state, problems = rules(**kwargs)
    assert (draft, state) == (None, None) and problem in problems


def test_pre_approved_creation_needs_no_servicenow():
    assert rules(servicenow=False)[2] == []


def test_the_longest_name_is_64_characters_after_sp():
    assert rules(name="sp-notes-" + "x" * 58)[2] == []


def test_an_unknown_scope_is_named_cut_short():
    problems = rules(scopes=("A." + "b" * 500,))[2]
    assert all(len(p) < 200 for p in problems)


def test_the_store_checks_the_draft_the_same_way():
    from portal.store.base import check_service_principal_request
    draft, state, _ = rules(scopes=("Calendars.ReadWrite",), justification="Books rooms.")
    check_service_principal_request("r_" + "a" * 32, "p_" + "b" * 32, REPO, draft, state)
    for bad, bad_state in (({**draft, "scopes": ["Calendars.ReadWrite", "Calendars.Read"]}, "pending"),   # not sorted
                           ({k: v for k, v in draft.items() if k != "justification"}, "pending"),
                           (draft, "approved"),                                                       # a justification while approved
                           ({**draft, "repo": "acme/other"}, "pending"), ({**draft, "display_name": "SP-x"}, "pending"),
                           ({**draft, "scopes": []}, "pending"), ({**draft, "extra": 1}, "pending"), (draft, "created")):
        with pytest.raises(ValueError):
            check_service_principal_request("r_" + "a" * 32, "p_" + "b" * 32, REPO, bad, bad_state)


# ---- final review M2: a justification with a control, format or surrogate character is refused, never stored ----

@pytest.mark.parametrize("why", ["Books\u0000 rooms.", "Books ‮rooms.", "Books \ud800 rooms.", "Books​ rooms.", "Books\x1b[31m"])
def test_a_justification_with_a_control_format_or_surrogate_character_is_refused(why):
    draft, state, problems = rules(scopes=("Calendars.ReadWrite",), justification=why)
    assert (draft, state) == (None, None) and sp.BAD_CHARACTERS in problems


def test_a_justification_keeps_its_line_breaks_and_tabs():
    draft, _, problems = rules(scopes=("Calendars.ReadWrite",), justification="Books rooms.\n\tFor the team.\r\nDaily.")
    assert problems == [] and draft["justification"] == "Books rooms.\n\tFor the team.\nDaily."


@pytest.mark.parametrize("text, bad", [("a\nb\tc", False), ("a\u0000", True), ("‮", True), ("\ud800", True), ("é", False)])
def test_unsafe_text_is_a_control_but_newline_and_tab_a_format_or_a_surrogate(text, bad):
    assert sp.unsafe_text(text) is bad

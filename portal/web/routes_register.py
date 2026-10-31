import re

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from ..binding_record import environment_of
from ..registration import CLASSIFICATIONS, form_draft
from ..store.base import Conflict
from ..timeline import timeline
from .access import PUBLIC, REQUESTER, REQUESTER_OR_ADMIN, SIGNED_IN
from .ownership import admin_repos_of, fresh_admin_repos, stale_message  # noqa: F401 (tests import them from here)
from .flash import redirect
from .render import page

router = APIRouter()
EMPTY = {"repo": "", "purpose": "", "data_classification": ""}
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def evidence_context(store, req) -> dict:
    """What the evidence macro shows for a request's current submission: the evidence (or why it waits), the classification as submitted,
    the effective values (the floor's tier, since nobody claims one, and the higher of the claimed and the floor classification), what
    the floor raised, and whether the evidence names a retired level (computed before builder 0.5.0: it can't be approved, and sending
    the request back gets fresh evidence). A claim the levels don't know (a draft from before 0.5.0) is shown as recorded and compared
    with nothing."""
    found = store.evidence_for(req.id, req.submission)
    claimed = req.draft.get("data_classification")
    known = claimed if claimed in CLASSIFICATIONS else None
    stale = bool(found) and found.floor_classification not in CLASSIFICATIONS
    if found and not stale:
        floor_cls = found.floor_classification
        effective = (found.floor_tier, max((known or floor_cls, floor_cls), key=CLASSIFICATIONS.index))
        mismatch = ["data_classification"] if known and CLASSIFICATIONS.index(known) < CLASSIFICATIONS.index(floor_cls) else []
    else:
        effective, mismatch = (found.floor_tier if found else None, known), []
    return {"evidence": found, "problem": None if found else store.evidence_problem_for(req.id, req.submission), "claim": (None, claimed),
            "effective": effective, "mismatch": mismatch, "stale": stale}


def _record_kind(record) -> str:
    if record["schema"] == "intake/v1":
        return "intake/v1, registered outside the portal"
    return "intake/v2, written by the portal" if record.get("by_portal") else "intake/v2, written by hand"


def change_context(store, req, evidence) -> dict | None:
    """Current → proposed for a request whose evidence names the record it replaces (spec 2026-10-31 C4, C2, C3), or None for a first
    registration. Proposed is what the request says; the tier and the review date are the admin's to decide, as for any registration."""
    if evidence is None or not evidence.record_sha:
        return None
    record = evidence.record
    if record.get("schema") == "invalid":
        return {"invalid": True, "scorecard": record.get("scorecard")}

    def who(value):
        person = store.get_person(value) if isinstance(value, str) and value.startswith("p_") else None
        return person.display_name if person else value

    claimed = req.draft.get("data_classification")
    same_purpose, same_class = record.get("purpose") == req.draft.get("purpose"), record.get("data_classification") == claimed
    rows = [{"label": "Record", "current": record["schema"], "proposed": "intake/v2", "changed": record["schema"] != "intake/v2"},
            {"label": "Owner", "current": who(record.get("owner")), "proposed": who(req.requested_by),
             "changed": record.get("owner") != req.requested_by},
            {"label": "Purpose", "current": "the same" if same_purpose else "changed: the current text is below",
             "proposed": "as submitted", "changed": not same_purpose},
            {"label": "Data classification", "current": record.get("data_classification"),
             "proposed": f"{claimed} (claimed; an admin decides)", "changed": not same_class},
            {"label": "Tier", "current": record.get("tier"), "proposed": "an admin decides", "changed": False},
            {"label": "Review by", "current": record.get("review_by") or "none (intake/v1)", "proposed": "an admin decides", "changed": False}]
    if record.get("scorecard"):  # the approval keeps it
        rows.append({"label": "Scorecard", "current": record["scorecard"], "proposed": f"{record['scorecard']} (kept)", "changed": False})
    return {"invalid": False, "kind": _record_kind(record), "rows": rows, "renewal": same_purpose and same_class,
            "owner_changes": record.get("owner") != req.requested_by, "owner_from": who(record.get("owner")), "owner_to": who(req.requested_by),
            "current_purpose": None if same_purpose else record.get("purpose", "")}


def admin_repos(request, principal):
    """The repos the signed-in person may register: admins of them as the collector saw them recently."""
    if not principal.signed_in:
        return []
    state = request.app.state
    return fresh_admin_repos(state.store, principal.person.id, state.clock(), state.settings.repo_admins_stale_after)[0]


def change_target(store, repo, fresh):
    """The observed intake record of `repo` when it is one of the person's fresh admin repos (spec 2026-10-31 C2, C3), else None."""
    names = {r.repo.lower(): r.repo for r in fresh}
    if not isinstance(repo, str) or repo.lower() not in names:
        return None
    found = store.intake_observed(names[repo.lower()])
    return found[0] if found else None


def register_page(request, principal, status=200, form=None, errors=(), chosen=""):
    """The register form. For a repo that is already registered (its intake record is observed) the page is "Change or renew the
    registration", pre-filled from the record when it is readable; the request it submits is the same registration request."""
    notes, change, repos = list(errors), None, []
    if principal.signed_in:
        state = request.app.state
        fresh, stale = fresh_admin_repos(state.store, principal.person.id, state.clock(), state.settings.repo_admins_stale_after)
        repos = fresh
        if stale and not fresh and not notes:
            notes.append(stale_message(stale))
        change = change_target(state.store, (form or {}).get("repo") or chosen, fresh)
        if form is None:  # the repo as the admin list spells it, so the option is selected whatever case the link used
            repo = next((r.repo for r in fresh if r.repo.lower() == (chosen or "").lower()), "")
            form = {**EMPTY, "repo": repo}
            if change is not None and change.problem is None:
                form.update(purpose=change.purpose, data_classification=change.data_classification)
    back = "/register" + (f"?repo={chosen}" if REPO.fullmatch(chosen or "") else "")  # where sign-in comes back to
    return page(request, "register.html", status, repos=repos, form=form or EMPTY, errors=notes, change=change, back=back)


@router.get("/register")
def register(request: Request, repo: str = Query(""), principal=PUBLIC):
    return register_page(request, principal, chosen=repo)


@router.post("/register")
def submit(request: Request, repo: str = Form(""), purpose: str = Form(""), data_classification: str = Form(""), principal=SIGNED_IN):
    form = {"repo": repo, "purpose": purpose, "data_classification": data_classification}
    draft, errors = form_draft(repo, purpose, data_classification)
    if draft:
        state = request.app.state
        fresh, stale = fresh_admin_repos(state.store, principal.person.id, state.clock(), state.settings.repo_admins_stale_after)
        if draft["repo"] not in {r.repo for r in fresh}:
            if draft["repo"] in {r.repo for r in stale}:
                return register_page(request, principal, 503, form, [stale_message(stale)])
            draft, errors = None, ["You are not an admin of that repository, as the portal last saw it. Ask an admin if this is wrong."]
    if draft is None:
        return register_page(request, principal, 400, form, errors)
    try:
        req = request.app.state.store.create_registration(person_id=principal.person.id, draft=draft, now=request.app.state.clock())
    except Conflict:
        return register_page(request, principal, 409, form, ["That repository already has an open registration."])
    return redirect(f"/requests/{req.id}", "registered")


FILTERS = {"all": None, "attention": ("pending", "sent_back"), "merged": ("merged",)}  # spec 2026-10-30 R4


@router.get("/requests")
def my_requests(request: Request, filter_: str = Query("all", alias="filter"), principal=SIGNED_IN):
    if filter_ not in FILTERS:
        return page(request, "error.html", 400, message="Choose a filter: All, Needs attention or Merged.")
    mine = request.app.state.store.requests_by(principal.person.id)
    states = FILTERS[filter_]
    return page(request, "requests.html", requests=[r for r in mine if states is None or r.state in states], any_requests=bool(mine),
                filter=filter_, apps=admin_repos(request, principal), environment_of=lambda r: environment_of(r) or "test")


@router.get("/requests/{request_id}")
def show(request: Request, request_id: str, principal=REQUESTER_OR_ADMIN):
    found = request.state.found_request
    if found.kind == "binding":  # request.html is for registrations: a connection change is shown on its app's page
        return RedirectResponse(f"/apps/{found.repo}?environment={environment_of(found) or 'test'}", status_code=303)
    store = request.app.state.store
    context = evidence_context(store, found)
    return page(request, "request.html", req=found, mine=found.requested_by == principal.person.id, errors=[],
                steps=timeline(found, store.audit_for("request", found.id)), change=change_context(store, found, context["evidence"]), **context)


@router.post("/requests/{request_id}")
def resubmit(request: Request, request_id: str, purpose: str = Form(""), data_classification: str = Form(""), principal=REQUESTER):
    found = request.state.found_request
    if found.state != "sent_back":
        return page(request, "error.html", 409, message="Only a request that was sent back can be changed.")
    draft, errors = form_draft(found.repo, purpose, data_classification)
    if draft is None:
        store = request.app.state.store
        context = evidence_context(store, found)
        return page(request, "request.html", 400, req=found, mine=True, errors=errors, steps=timeline(found, store.audit_for("request", found.id)),
                    change=change_context(store, found, context["evidence"]), **context)
    try:
        request.app.state.store.resubmit(request_id=found.id, person_id=principal.person.id, draft=draft, now=request.app.state.clock())
    except Conflict:
        return page(request, "error.html", 409, message="Only a request that was sent back can be changed.")
    return redirect(f"/requests/{found.id}", "resubmitted")

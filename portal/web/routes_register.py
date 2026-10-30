from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..binding_record import environment_of
from ..floor import below_floor, effective
from ..registration import CLASSIFICATIONS, TIERS, form_draft
from ..store.base import Conflict
from .access import PUBLIC, REQUESTER, REQUESTER_OR_ADMIN, SIGNED_IN
from .ownership import admin_repos_of, fresh_admin_repos, stale_message  # noqa: F401 (tests import them from here)
from .render import page

router = APIRouter()
EMPTY = {"repo": "", "purpose": "", "data_classification": "", "proposed_tier": ""}


def evidence_context(store, req) -> dict:
    """What the evidence macro shows for a request's current submission: the evidence (or why it waits), the claim as submitted, the
    effective values (the higher of the claim and the floor) and what the floor raised."""
    found = store.evidence_for(req.id, req.submission)
    claim = (req.draft.get("proposed_tier"), req.draft.get("data_classification"))
    return {"evidence": found, "problem": None if found else store.evidence_problem_for(req.id, req.submission), "claim": claim,
            "effective": effective(*claim, found.floor_tier, found.floor_classification) if found else claim,
            "mismatch": below_floor(*claim, found.floor_tier, found.floor_classification) if found else []}


def admin_repos(request, principal):
    """The repos the signed-in person may register: admins of them as the collector saw them recently."""
    if not principal.signed_in:
        return []
    state = request.app.state
    return fresh_admin_repos(state.store, principal.person.id, state.clock(), state.settings.repo_admins_stale_after)[0]


def register_page(request, principal, status=200, form=None, errors=()):
    notes = list(errors)
    if principal.signed_in:
        state = request.app.state
        fresh, stale = fresh_admin_repos(state.store, principal.person.id, state.clock(), state.settings.repo_admins_stale_after)
        if stale and not fresh and not notes:
            notes.append(stale_message(stale))
    return page(request, "register.html", status, repos=admin_repos(request, principal), form=form or EMPTY, errors=notes,
                classifications=CLASSIFICATIONS, tiers=TIERS)


@router.get("/register")
def register(request: Request, principal=PUBLIC):
    return register_page(request, principal)


@router.post("/register")
def submit(request: Request, repo: str = Form(""), purpose: str = Form(""), data_classification: str = Form(""),
           proposed_tier: str = Form(""), principal=SIGNED_IN):
    form = {"repo": repo, "purpose": purpose, "data_classification": data_classification, "proposed_tier": proposed_tier}
    draft, errors = form_draft(repo, purpose, data_classification, proposed_tier)
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
    return RedirectResponse(f"/requests/{req.id}", status_code=303)


@router.get("/requests")
def my_requests(request: Request, principal=SIGNED_IN):
    return page(request, "requests.html", requests=request.app.state.store.requests_by(principal.person.id), apps=admin_repos(request, principal),
                environment_of=lambda r: environment_of(r) or "test")


@router.get("/requests/{request_id}")
def show(request: Request, request_id: str, principal=REQUESTER_OR_ADMIN):
    found = request.state.found_request
    if found.kind == "binding":  # request.html is for registrations: a connection change is shown on its app's page
        return RedirectResponse(f"/apps/{found.repo}?environment={environment_of(found) or 'test'}", status_code=303)
    return page(request, "request.html", req=found, mine=found.requested_by == principal.person.id, errors=[],
                classifications=CLASSIFICATIONS, tiers=TIERS, **evidence_context(request.app.state.store, found))


@router.post("/requests/{request_id}")
def resubmit(request: Request, request_id: str, purpose: str = Form(""), data_classification: str = Form(""),
             proposed_tier: str = Form(""), principal=REQUESTER):
    found = request.state.found_request
    if found.state != "sent_back":
        return page(request, "error.html", 409, message="Only a request that was sent back can be changed.")
    draft, errors = form_draft(found.repo, purpose, data_classification, proposed_tier)
    if draft is None:
        return page(request, "request.html", 400, req=found, mine=True, errors=errors, classifications=CLASSIFICATIONS, tiers=TIERS,
                    **evidence_context(request.app.state.store, found))
    try:
        request.app.state.store.resubmit(request_id=found.id, person_id=principal.person.id, draft=draft, now=request.app.state.clock())
    except Conflict:
        return page(request, "error.html", 409, message="Only a request that was sent back can be changed.")
    return RedirectResponse(f"/requests/{found.id}", status_code=303)

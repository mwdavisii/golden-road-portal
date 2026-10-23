from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..registration import CLASSIFICATIONS, TIERS, form_draft
from ..store.base import Conflict
from .access import PUBLIC, REQUESTER, REQUESTER_OR_ADMIN, SIGNED_IN
from .render import page

router = APIRouter()
EMPTY = {"repo": "", "purpose": "", "data_classification": "", "proposed_tier": ""}


def admin_repos_of(store, person_id):
    """The repos this person is an admin of, as the collector last saw it, through their linked GitHub logins (by user ID where the
    link has one, by login where an admin made the link without it). One row per repo, sorted."""
    links = store.links_for(person_id)
    rows = store.admin_repos([link.github_user_id for link in links if link.github_user_id is not None])
    rows += store.admin_repos_by_login([link.github_login.lower() for link in links if link.github_user_id is None])
    unique = {r.repo: r for r in rows}
    return [unique[k] for k in sorted(unique, key=str.lower)]


def fresh_admin_repos(store, person_id, now, stale_after):
    """(fresh, stale): the person's admin repos split by whether the collector saw them within stale_after. A repo seen both ways
    is fresh. Nothing is decided on a stale observation: the collector may be down, and a person's rights may have changed."""
    rows = admin_repos_of(store, person_id)
    fresh = [r for r in rows if now - r.observed_at <= stale_after]
    stale = [r for r in rows if now - r.observed_at > stale_after]
    return fresh, stale


def stale_message(stale) -> str:
    last = max(r.observed_at for r in stale).strftime("%Y-%m-%d %H:%M")
    return (f"The portal last saw your repositories at {last} UTC, which is too old to rely on: the collector that reads them from GitHub "
            "may be down. Try again later, or ask an admin.")


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
    return page(request, "requests.html", requests=request.app.state.store.requests_by(principal.person.id))


@router.get("/requests/{request_id}")
def show(request: Request, request_id: str, principal=REQUESTER_OR_ADMIN):
    found = request.state.found_request
    return page(request, "request.html", req=found, mine=found.requested_by == principal.person.id, errors=[],
                classifications=CLASSIFICATIONS, tiers=TIERS)


@router.post("/requests/{request_id}")
def resubmit(request: Request, request_id: str, purpose: str = Form(""), data_classification: str = Form(""),
             proposed_tier: str = Form(""), principal=REQUESTER):
    found = request.state.found_request
    if found.state != "sent_back":
        return page(request, "error.html", 409, message="Only a request that was sent back can be changed.")
    draft, errors = form_draft(found.repo, purpose, data_classification, proposed_tier)
    if draft is None:
        return page(request, "request.html", 400, req=found, mine=True, errors=errors, classifications=CLASSIFICATIONS, tiers=TIERS)
    try:
        request.app.state.store.resubmit(request_id=found.id, person_id=principal.person.id, draft=draft, now=request.app.state.clock())
    except Conflict:
        return page(request, "error.html", 409, message="Only a request that was sent back can be changed.")
    return RedirectResponse(f"/requests/{found.id}", status_code=303)

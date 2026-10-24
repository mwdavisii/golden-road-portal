import datetime as dt
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..auth.github_link import LOGIN
from ..contracts import validate
from ..registration import CLASSIFICATIONS, NO_EVIDENCE, TIERS, add_months, intake_record
from ..store.base import Conflict
from .access import ADMIN
from .render import page
from .routes_register import evidence_context, fresh_admin_repos, stale_message

router = APIRouter()


def reason_ok(text: str) -> bool:
    return 10 <= len(text) <= 1000 and not re.search(r"[\r\n]", text)


def people_page(request, status=200, errors=()):
    store = request.app.state.store
    people = [(p, store.links_for(p.id)) for p in store.list_people()]
    return page(request, "admin_people.html", status, people=people, errors=list(errors))


@router.get("/admin/people")
def people(request: Request, principal=ADMIN):
    return people_page(request)


@router.post("/admin/people/{person_id}/links")
def add_link(request: Request, person_id: str, github_login: str = Form(""), reason: str = Form(""), principal=ADMIN):
    login, reason = github_login.strip(), reason.strip()
    errors = ([] if LOGIN.fullmatch(login) else ["That is not a GitHub login."]) + ([] if reason_ok(reason) else ["Give a reason of 10 to 1000 characters on one line."])
    if errors:
        return people_page(request, 400, errors)
    store = request.app.state.store
    if person_id == principal.person.id:  # nobody vouches for their own login: the store and the database refuse it too
        return people_page(request, 403, ["You can't link a GitHub login to yourself: another admin must."])
    # When the collector has seen this login, keep the numeric user ID: a login can be renamed or reused, an ID can't.
    ids = {row.github_user_id for row in store.admin_repos_by_login([login.lower()])}
    user_id = next(iter(ids)) if len(ids) == 1 else None
    try:
        store.add_link(person_id=person_id, github_login=login, github_user_id=user_id, source="admin",
                       actor_id=principal.person.id, reason=reason, now=request.app.state.clock())
    except Conflict as exc:
        status, message = {"not_found": (404, "No such person."), "self_link": (403, "You can't link a GitHub login to yourself: another admin must.")}.get(
            exc.code, (409, "That login is already linked to someone."))
        return people_page(request, status, [message])
    return RedirectResponse("/admin/people", status_code=303)


@router.post("/admin/links/{link_id}/remove")
def remove_link(request: Request, link_id: str, reason: str = Form(""), principal=ADMIN):
    reason = reason.strip()
    if not reason_ok(reason):
        return people_page(request, 400, ["Give a reason of 10 to 1000 characters on one line."])
    try:
        request.app.state.store.remove_link(link_id=link_id, actor_id=principal.person.id, reason=reason, now=request.app.state.clock())
    except Conflict:
        return people_page(request, 404, ["No such link."])
    return RedirectResponse("/admin/people", status_code=303)


@router.get("/admin/audit")
def audit(request: Request, principal=ADMIN):
    return page(request, "admin_audit.html", entries=request.app.state.store.audit_entries(200))


@router.get("/admin/identity-links.json")
def export_links(request: Request, principal=ADMIN):
    docs = [{"schema": "identity-link/v1", "person": link.person_id, "github_login": link.github_login, "source": link.source,
             "created_by": link.created_by, "created_at": link.created_at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
            for link in request.app.state.store.active_links()]
    for doc in docs:
        errors = validate("identity-link.v1", doc)
        if errors:
            raise ValueError(f"internal error: an identity link doesn't export as identity-link/v1: {errors[0]}")
    return JSONResponse(docs)


def approvals_page(request, principal, status=200, errors=()):
    store, settings, today = request.app.state.store, request.app.state.settings, request.app.state.clock().date()
    items = []
    for r in store.requests_in(("pending",)):
        fresh, stale = fresh_admin_repos(store, r.requested_by, request.app.state.clock(), settings.repo_admins_stale_after)
        items.append({"request": r, "requester": store.get_person(r.requested_by), "links": store.links_for(r.requested_by),
                      "admin_seen": next((a for a in fresh if a.repo == r.repo), None),
                      "unverified": bool(store.links_for(r.requested_by)) and all(link.github_user_id is None for link in store.links_for(r.requested_by)),
                      "stale_note": stale_message(stale) if any(a.repo == r.repo for a in stale) and not any(a.repo == r.repo for a in fresh) else None,
                      "mine": r.requested_by == principal.person.id, **evidence_context(store, r)})
    return page(request, "admin_approvals.html", status, items=items, errors=list(errors), tiers=TIERS, classifications=CLASSIFICATIONS,
                review_by=add_months(today, settings.review_months).isoformat())


@router.get("/admin/approvals")
def approvals(request: Request, principal=ADMIN):
    return approvals_page(request, principal)


def _pending(request, principal, request_id):
    found = request.app.state.store.get_request(request_id)
    if found is None:
        return None, approvals_page(request, principal, 404, ["No such request."])
    if found.requested_by == principal.person.id:
        return None, approvals_page(request, principal, 403, ["You requested this, so another admin must decide it."])
    if found.state != "pending":
        return None, approvals_page(request, principal, 409, ["That request was already decided."])
    return found, None


DECIDE_CONFLICT = {
    "no_evidence": NO_EVIDENCE,
    "evidence_mismatch": "The platform's evidence changed under this approval: reload the page and decide again.",
    "below_floor": "That tier or data classification is below the platform's floor, and the reason to approve it anyway is missing or not valid.",
}


@router.post("/admin/requests/{request_id}/approve")
def approve(request: Request, request_id: str, tier: str = Form(""), data_classification: str = Form(""), review_by: str = Form(""),
            justification: str = Form(""), floor_override_reason: str = Form(""), submission: str = Form(""), principal=ADMIN):
    store, now = request.app.state.store, request.app.state.clock()
    found = store.get_request(request_id)
    if found is not None and found.requested_by == principal.person.id:
        return approvals_page(request, principal, 403, ["You requested this, so another admin must decide it."])
    if found is None:
        return approvals_page(request, principal, 404, ["No such request."])
    if not (submission.isascii() and submission.isdigit() and int(submission) >= 1):
        return approvals_page(request, principal, 400, ["Reload the page: the form didn't say which submission it was loaded for."])
    if int(submission) != found.submission:  # sent back and submitted again since the page loaded: what the admin judged is gone
        return approvals_page(request, principal, 409, ["This registration changed since you loaded this page (it was sent back and submitted again). "
                                                        "Reload the page and look at it again."])
    record, errors = intake_record(found, approver_id=principal.person.id, tier=tier, classification=data_classification,
                                   review_by=review_by, justification=justification, now=now,
                                   evidence=store.evidence_for(found.id, found.submission), override_reason=floor_override_reason)
    if errors:
        return approvals_page(request, principal, 409 if errors == [NO_EVIDENCE] else 400, errors)
    found, refused = _pending(request, principal, request_id)
    if refused:
        return refused
    fresh, stale = fresh_admin_repos(store, found.requested_by, now, request.app.state.settings.repo_admins_stale_after)
    if not any(a.repo == found.repo for a in fresh):
        if any(a.repo == found.repo for a in stale):
            return approvals_page(request, principal, 503, [stale_message(stale)])
        return approvals_page(request, principal, 409, ["The requester is no longer an admin of that repository, as the portal last saw it."])
    try:
        store.decide(request_id=found.id, actor_id=principal.person.id, actor_groups=principal.session.groups, approve=True,
                     note=record["approval_justification"], final=record, floor_override=record.get("floor_override"), now=now)
    except Conflict as exc:
        return approvals_page(request, principal, 409, [DECIDE_CONFLICT.get(exc.code, "That request was already decided.")])
    return RedirectResponse("/admin/approvals", status_code=303)


@router.post("/admin/requests/{request_id}/send-back")
def send_back(request: Request, request_id: str, justification: str = Form(""), principal=ADMIN):
    note = justification.strip()
    if not reason_ok(note):
        return approvals_page(request, principal, 400, ["Say what to change, in 10 to 1000 characters on one line."])
    found, refused = _pending(request, principal, request_id)
    if refused:
        return refused
    try:
        request.app.state.store.decide(request_id=found.id, actor_id=principal.person.id, actor_groups=principal.session.groups, approve=False,
                                       note=note, final=None, now=request.app.state.clock())
    except Conflict:
        return approvals_page(request, principal, 409, ["That request was already decided."])
    return RedirectResponse("/admin/approvals", status_code=303)

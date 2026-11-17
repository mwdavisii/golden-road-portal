"""The collector's service principal step (spec 2026-11-16 S4, S5, S10, "Each collector pass"), one pass at a time (the store's
service_principal_lock, an advisory lock as the write-back's):
1. Open tasks: for each pending request with none, first look for a task whose correlation_id is the request's id (a pass that stopped
   after the POST never opens a second), else open one; then record its number and sys_id.
2. Read tasks: Closed Complete (3) approves the request, if the task names who closed it and that is not the requester's own ServiceNow
   user (else a failure, retried: final review I2); Closed Incomplete (4) or Closed Skipped (7) rejects it, keeping the close notes
   (cut to 2000 characters); open (or any other state) leaves it.
3. Create: for each approved request, the scopes are checked against the catalog again (decided while planning: item 28): an unknown
   scope, or a requestable one on a request with no task, is never created (a failure, with the reason). Otherwise the directory client
   makes the principal; the store records it and the request created in one transaction.
4. Note: each created request with a task gets one work note with the new client ID (decided while planning: item 6), at least once:
   the note is marked sent after ServiceNow took it, so a pass that stops between the two sends it again on the next pass (as the
   write-back's notes).
A ServiceNow, directory or store refusal keeps the request as it is, with one more attempt and the client's own error (never the
instance's text), and the next pass tries again, with no attempt cap (no budgets): an outage delays a request and never loses it. Anything
else one request raises is recorded the same way by its type only (its message may carry what was read), so one bad item never stalls
the others or the later steps (Round 3 review I3). Without ServiceNow (PORTAL_SERVICENOW unset), or when this pass's ServiceNow can't be
built (the mock's replay failed: "servicenow" names the error's type), steps 1, 2 and 4 wait; pre-approved requests are still created."""

import contextlib

from ..directory_client import DirectoryClientError
from ..outbox import neutral
from ..service_principals import creation_note, task_description, task_short_description
from ..servicenow import CLOSED_COMPLETE, ServiceNowError
from ..store.base import MAX_CLOSE_NOTES, MAX_STEP_REASON, Conflict

REJECTED_STATES = ("4", "7")  # Closed Incomplete, Closed Skipped (S5; confirm the choice list on the instance)
COUNTS = ("created", "opened", "approved", "rejected", "waiting", "noted", "failed")


def collect_service_principals(store, servicenow, directory_client, *, base_url, now, catalog=None) -> dict:
    """One pass: COUNTS, "servicenow" (None, or the type of the error that kept this pass from building its ServiceNow), and "busy":
    True when another pass holds the lock (nothing done). servicenow: anything with client() (the collector's Replayed mock), or None."""
    with store.service_principal_lock() as held:
        counts = {**dict.fromkeys(COUNTS, 0), "servicenow": None}
        if not held:
            return {**counts, "busy": True}
        session = contextlib.nullcontext(None)
        if servicenow is not None:
            try:
                session = servicenow.client()
            except Exception as exc:  # noqa: BLE001 - by type only; the pre-approved requests are still created
                counts["servicenow"] = type(exc).__name__
        with session as client:
            if client is None:
                counts["waiting"] += len(store.service_principal_requests(states=("pending",)))
            else:
                _each(store, store.service_principal_requests(states=("pending",)), counts, now,
                      lambda req: _open_task(store, client, req, counts, base_url=base_url, now=now, catalog=catalog))
                _each(store, store.service_principal_requests(states=("pending",)), counts, now,
                      lambda req: _read_task(store, client, req, counts, now=now))
            _each(store, store.service_principal_requests(states=("approved",)), counts, now,
                  lambda req: _create(store, directory_client, req, counts, now=now, catalog=catalog))
            if client is not None:
                principals = {p.request_id: p for p in store.service_principals()}
                _each(store, store.service_principal_requests(states=("created",)), counts, now,
                      lambda req: _note(store, client, req, principals, counts, base_url=base_url, now=now))
        return counts


def _each(store, requests, counts, now, one) -> None:
    """one(req) for each request; whatever one request raises is its failure (by type: its text may be the instance's), never the
    pass's."""
    for req in requests:
        try:
            one(req)
        except Exception as exc:  # noqa: BLE001 - recorded on the request and retried next pass
            _failed(store, req, f"the step stopped on {type(exc).__name__}: will retry", counts, now)


def _failed(store, req, error, counts, now) -> None:
    try:
        store.record_service_principal_failure(request_id=req.id, error=(error or "unknown error")[:MAX_STEP_REASON], now=now)
    except Conflict:
        return  # another pass moved it meanwhile: nothing failed here
    counts["failed"] += 1


def _open_task(store, client, req, counts, *, base_url, now, catalog):
    if req.task_number is not None:
        return
    cmdb = store.cmdb_record(req.repo)
    try:
        ref = client.find_task(req.id) or client.create_task(
            short_description=task_short_description(req),
            description=task_description(req, store.person_email(req.requested_by), base_url, catalog),
            correlation_id=req.id, ci=cmdb.sys_id if cmdb is not None and cmdb.sys_id else None)
    except ServiceNowError as exc:
        _failed(store, req, str(exc), counts, now)
        return
    try:
        store.record_service_principal_task(request_id=req.id, task_number=ref.number, task_sys_id=ref.sys_id, now=now)
    except Conflict as exc:
        if exc.code != "wrong_state":  # wrong_state: another pass recorded it
            _failed(store, req, f"the store refused the task: {exc.code}", counts, now)
        return
    counts["opened"] += 1


def _read_task(store, client, req, counts, *, now):
    if req.task_number is None:
        return
    try:
        task = client.task(req.task_number)
    except ServiceNowError as exc:
        _failed(store, req, str(exc), counts, now)
        return
    if task is None or task.sys_id != req.task_sys_id:
        _failed(store, req, f"ServiceNow has no task {req.task_number}", counts, now)
        return
    if task.state != CLOSED_COMPLETE and task.state not in REJECTED_STATES:
        counts["waiting"] += 1
        return
    approved = task.state == CLOSED_COMPLETE
    if approved:
        try:
            refused = _closer_problem(store, client, req, task)
        except ServiceNowError as exc:  # the requester's user couldn't be looked up: never approved meanwhile
            refused = str(exc)
        if refused:
            _failed(store, req, refused, counts, now)
            return
    # close notes are the instance's text: no control, format or surrogate character (a NUL or a lone surrogate can't be stored), and
    # no [code] tag, then cut (S5)
    notes = None if approved else neutral(task.close_notes)[:MAX_CLOSE_NOTES]
    try:
        store.decide_service_principal_request(request_id=req.id, approved=approved, close_notes=notes, now=now)
    except Conflict:
        return
    counts["approved" if approved else "rejected"] += 1


def _closer_problem(store, client, req, task) -> str | None:
    """Why a task closed complete doesn't approve the request (None: it does), final review I2: the task names who closed it, and that
    is not the requester's own ServiceNow user (found by their email, as the CMDB part finds an owner). A requester with no ServiceNow
    user by that email can't be told apart from the closer: the closer stands. ServiceNow's ACLs (or an approval record,
    sysapproval_approver) must still keep other people who mustn't approve from closing the task. Raises ServiceNowError (a failure,
    retried) when the user can't be looked up."""
    if task.closed_by is None:
        return "the task names no one who closed it: not approved"
    email = store.person_email(req.requested_by)
    if not email:
        return "the requester has no email to check against the task's closer: not approved"
    if client.user_sys_id(email) == task.closed_by:
        return "the requester closed the task: not approved"
    return None


def creation_problem(req, catalog, lifecycle=None) -> str | None:
    """Why an approved request must not be created (None: it may), from the catalog, never the web's word (decided while planning: item
    28): the database doesn't know the tiers, so a request recorded approved with a requestable scope and no task, or with a scope the
    catalog doesn't list, is the web's mistake or worse. lifecycle: the app's LifecycleState (or None): an app decommissioned after its
    request gets no principal (final review I1; the store refuses it too)."""
    if lifecycle is not None and lifecycle.state == "decommissioned":
        return f"{req.repo} is decommissioned: not created"
    if catalog is None:
        return "the collector has no scope catalog: not created"
    for scope_id in req.scopes:
        scope = catalog.scope(scope_id)
        if scope is None:
            return f"{scope_id} is not in the scope catalog: not created"
        if scope.tier == "requestable" and req.task_number is None:
            return f"{scope_id} needs approval and the request has no ServiceNow task: not created"
    return None


def _create(store, directory_client, req, counts, *, now, catalog):
    problem = creation_problem(req, catalog, store.lifecycle_state(req.repo))
    if problem:
        _failed(store, req, problem, counts, now)
        return
    try:
        principal_id, client_id = directory_client.create_principal(req.display_name, req.scopes)
    except DirectoryClientError as exc:
        _failed(store, req, str(exc), counts, now)
        return
    try:
        store.create_service_principal(request_id=req.id, principal_id=principal_id, client_id=client_id, now=now)
    except Conflict as exc:
        if exc.code != "wrong_state":
            _failed(store, req, f"the store refused the principal: {exc.code}", counts, now)
        return
    counts["created"] += 1


def _note(store, client, req, principals, counts, *, base_url, now):
    if req.task_sys_id is None or req.note_sent_at is not None or req.id not in principals:
        return
    try:
        client.add_work_note(req.task_sys_id, creation_note(req, principals[req.id], base_url))
    except ServiceNowError as exc:
        _failed(store, req, str(exc), counts, now)
        return
    try:
        store.mark_service_principal_note_sent(request_id=req.id, now=now)
    except Conflict:
        return
    counts["noted"] += 1

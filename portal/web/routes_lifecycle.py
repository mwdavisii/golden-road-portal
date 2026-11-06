"""Suspend, resume and decommission (spec 2026-11-02): a portal admin enters the ServiceNow task and a reason, and the portal checks the task
(rule 2) and every rule the web can see (rules 1, 3, 4) before it records the request; the writer applies it. Retry, Abandon and Stop are
asked here and carried out by the writer (it holds the GitHub credentials). The owner sees the state on the app page, never these forms.

Rule 1 is checked on every route here, whatever the page shows: the forms and their POSTs answer 403 to anyone but a portal admin (the
app's own admin gets past APP_ADMIN, not past this), and Retry, Abandon, Stop and the mock task page are ADMIN routes.

In the mock (decided while planning: item 9) the web's MockServiceNow never sees the collector's PATCH calls (another process's memory),
so the task check and the task page ask a fresh mock with the notes the collector marked sent replayed into it: a task the portal closed
is closed here too. The settings' own mock is never written to."""

from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request

from .. import lifecycle
from ..outbox import note_text
from ..ids import new_id
from ..servicenow_mock import MockServiceNow
from ..store.base import LIFECYCLE_ACTIONS, OPEN_STATES, TASK_NUMBER, Conflict
from ..timeline import STEP_LABELS, lifecycle_timeline
from .access import ADMIN, APP_ADMIN
from .flash import redirect
from .render import page

router = APIRouter()
NOTHING_TO_RETRY = "Nothing to retry: no step of this request has failed."
TOO_LATE = "The deploy repo PR has merged, so this can't be abandoned: retry the stalled step instead."
NOT_STOPPABLE = ("Only a decommission whose deploy repo PR has merged, with a step still to do, can be stopped: abandon it instead, or "
                 "wait for the writer to apply it.")
COMMAND_OPEN = "The writer hasn't carried out the last Retry, Abandon or Stop yet: wait for its next pass."
NOT_OPEN = "This request is no longer open."


def facts(store, repo):
    """(the factory's record of the repo or None, its LifecycleState or None, its open lifecycle Request or None)."""
    records = store.intake_observed(repo)
    open_one = next((r for r in store.requests_for_repo(repo) if r.kind == "lifecycle" and r.state in OPEN_STATES), None)
    return (records[0] if records else None), store.lifecycle_state(repo), open_one


def sent_notes(store, base_url) -> list:
    """(task_sys_id, step, text) for every note the collector marked sent, in the order it sent them: by pass (sent_at), then as a pass
    sends them, per request in id order."""
    sent = sorted((e for e in store.lifecycle_events() if e.sent_at is not None), key=lambda e: (e.sent_at, e.request_id, e.id))
    return [(e.task_sys_id, e.step, note_text(e, base_url)) for e in sent]


def closing(store, task_sys_id) -> bool:
    """Whether a request's completed note is stored for this task, sent or not: the portal closes (or has closed) the task, though
    ServiceNow may show it open until the collector's next pass."""
    return any(e.step == "completed" and e.task_sys_id == task_sys_id for e in store.lifecycle_events())


def task_servicenow(settings, store):
    """The ServiceNow the web asks about a task: in the mock, a fresh one with the sent notes replayed (the shared one never changes);
    otherwise the settings' own (or None: not set up here)."""
    api = settings.servicenow_api
    if not isinstance(api, MockServiceNow):
        return api
    mock = api.fresh()
    mock.replay(sent_notes(store, settings.base_url))
    return mock


def banner(store, state) -> dict | None:
    """What everyone who can see the app page reads (spec "The portal"): suspended or decommissioned, with the task and the date. A
    decommission is finished (archived, the intake record marked) once the request that set the state is merged."""
    if state is None or state.state == "active":
        return None
    when = state.since.date().isoformat()
    if state.state == "suspended":
        return {"kind": "suspended", "text": f"Suspended by the platform for ServiceNow task {state.task} on {when}. It comes back when the "
                                             "platform resumes it."}
    setter = store.get_request(state.request)
    finished = setter is not None and setter.state == "merged"
    stopped = setter is not None and setter.state == "failed" and setter.failure_reason == "stopped"
    archived = setter is not None and any(s.step == "archive" and s.state == "done" for s in store.lifecycle_steps(setter.id))
    return {"kind": "decommissioned", "text": f"Decommissioned on {when} for task {state.task}. "
                                              + ("The repository is archived." if finished
                                                 else "The platform stopped before finishing it: the repository is "
                                                      f"{'archived' if archived else 'not archived'}, and the intake record is not marked."
                                                 if stopped
                                                 else "The repository is archived: the platform is finishing the intake record." if archived
                                                 else "The platform is finishing it: the archive, then the intake record.")}


def refused_action(settings, action, repo, record, state, open_one) -> str | None:
    """Why the action can't be asked (None: it may be), the same on the panel, the form and its POST: no ServiceNow here (but for a
    decommissioned app, which says so), then rules 3 and 4."""
    if settings.servicenow_api is None and (state is None or state.state != "decommissioned"):
        return lifecycle.NO_SERVICENOW
    return lifecycle.refusal(action, repo=repo, org=settings.org, record=record, state=state, open_lifecycle=open_one)


def panel(store, settings, repo, principal) -> dict | None:
    """The admin's Lifecycle panel (None for anyone else): the state, the open request, and each action with its refusal (None: it may
    be asked). The form checks the same again, and its POST once more."""
    if not principal.is_admin:
        return None
    record, state, open_one = facts(store, repo)
    current = state.state if state is not None else "active"
    label = ("Active" if current == "active"
             else f"Suspended since {state.since.date().isoformat()} by task {state.task}" if current == "suspended"
             else f"Decommissioned on {state.since.date().isoformat()} (task {state.task})")
    actions = [{"action": a, "words": lifecycle.ACTION_WORDS[a], "refused": refused_action(settings, a, repo, record, state, open_one)}
               for a in LIFECYCLE_ACTIONS]
    return {"label": label, "open": open_one, "actions": actions}


def lifecycle_form(request, action, status=200, errors=(), form=None):
    repo = request.state.app_repo
    owner_name, repo_name = repo.split("/")
    return page(request, "lifecycle_new.html", status, repo=repo, action=action, words=lifecycle.ACTION_WORDS[action], errors=list(errors),
                form=form or {}, owner_name=owner_name, repo_name=repo_name, refused=status == 409 and form is None)


def _gate(request, principal, action):
    """The page to answer instead, or None when the admin may go on."""
    if action not in LIFECYCLE_ACTIONS:
        return page(request, "error.html", 404, message="Not found.")
    if not principal.is_admin:  # rule 1: the app's own admin passed APP_ADMIN, and is refused here
        return page(request, "error.html", 403, message=lifecycle.NOT_ADMIN)
    store, repo = request.app.state.store, request.state.app_repo
    record, state, open_one = facts(store, repo)
    refused = refused_action(request.app.state.settings, action, repo, record, state, open_one)
    if refused:
        return lifecycle_form(request, action, 409, [refused])
    return None


@router.get("/apps/{owner}/{repo}/lifecycle/{action}")
def show(request: Request, owner: str, repo: str, action: str, principal=APP_ADMIN):
    return _gate(request, principal, action) or lifecycle_form(request, action)


@router.post("/apps/{owner}/{repo}/lifecycle/{action}")
def ask(request: Request, owner: str, repo: str, action: str, task: str = Form(""), reason: str = Form(""), confirm_repo: str = Form(""),
        principal=APP_ADMIN):
    stop = _gate(request, principal, action)
    if stop is not None:
        return stop
    state, repo = request.app.state, request.state.app_repo
    task = task.strip()
    form = {"task": task, "reason": reason, "confirm_repo": confirm_repo}
    problems = lifecycle.form_problems(action, repo, task, reason, confirm_repo=confirm_repo)
    if problems:
        return lifecycle_form(request, action, 400, problems, form)
    cmdb = state.store.cmdb_record(repo)
    try:
        task_sys_id = lifecycle.check_task(task_servicenow(state.settings, state.store), task=task, repo=repo,
                                           ci_sys_id=cmdb.sys_id if cmdb else None)
    except lifecycle.LifecycleRefused as exc:
        return lifecycle_form(request, action, 503 if str(exc) == lifecycle.SERVICENOW_DOWN else 400, [str(exc)], form)
    if closing(state.store, task_sys_id):  # rule 2: the portal closes it once the collector sends the note, so it is closed already
        return lifecycle_form(request, action, 400, [lifecycle.TASK_CLOSED.format(task=task)], form)
    final = {"action": action, "task": task, "task_sys_id": task_sys_id, "reason": reason}
    note = lifecycle.request_note(action, principal.person.display_name, reason)
    try:
        req = state.store.create_lifecycle(request_id=new_id("r"), person_id=principal.person.id, repo=repo, final=final, note=note,
                                           now=state.clock())
    except Conflict as exc:
        if exc.code == "open_request_exists":
            return lifecycle_form(request, action, 409, [lifecycle.ONE_OPEN.format(repo=repo)], form)
        if exc.code == "decommissioned":
            return lifecycle_form(request, action, 409, [lifecycle.decommissioned_words(state.store.lifecycle_state(repo))], form)
        raise
    return redirect(f"/requests/{req.id}", "lifecycle")


def stoppable(req, steps) -> bool:
    """A decommission whose deploy step is done (too late to abandon) with a step still to do."""
    return (req.final["action"] == "decommission" and any(s.step == "deploy" and s.state == "done" for s in steps)
            and any(s.state != "done" for s in steps))


def commands_allowed(req, steps, command) -> dict:
    """Which of Retry, Abandon and Stop the request page offers (rules 7 and 8), the same checks the POSTs make: {"retry", "abandon",
    "stop"}. Each needs an open request with no command waiting for the writer; Retry a failed step; Abandon the deploy step not done
    yet; Stop a decommission whose deploy step is done, with a step still to do."""
    open_now = req.state in OPEN_STATES and command is None
    return {"retry": open_now and any(s.state == "failed" for s in steps),
            "abandon": open_now and not any(s.step == "deploy" and s.state == "done" for s in steps),
            "stop": open_now and stoppable(req, steps)}


def _command(request, principal, request_id, command):
    store = request.app.state.store
    found = store.get_request(request_id)
    if found is None or found.kind != "lifecycle":
        return page(request, "error.html", 404, message="Not found.")
    if found.state not in OPEN_STATES:
        return page(request, "error.html", 409, message=NOT_OPEN)
    steps = store.lifecycle_steps(found.id)
    if command == "retry" and not any(s.state == "failed" for s in steps):
        return page(request, "error.html", 409, message=NOTHING_TO_RETRY)
    if command == "abandon" and any(s.step == "deploy" and s.state == "done" for s in steps):
        return page(request, "error.html", 409, message=TOO_LATE)
    if command == "stop" and not stoppable(found, steps):
        return page(request, "error.html", 409, message=NOT_STOPPABLE)
    try:
        store.add_lifecycle_command(request_id=found.id, command=command, actor_id=principal.person.id, now=request.app.state.clock())
    except Conflict as exc:
        if exc.code != "command_open":
            raise
        return page(request, "error.html", 409, message=COMMAND_OPEN)
    return redirect(f"/requests/{found.id}", command)


@router.post("/requests/{request_id}/retry")
def retry(request: Request, request_id: str, principal=ADMIN):
    return _command(request, principal, request_id, "retry")


@router.post("/requests/{request_id}/abandon")
def abandon(request: Request, request_id: str, principal=ADMIN):
    return _command(request, principal, request_id, "abandon")


@router.post("/requests/{request_id}/stop")
def stop(request: Request, request_id: str, principal=ADMIN):
    return _command(request, principal, request_id, "stop")


def _pr_link_ok(url, github_host) -> bool:
    """An https link to the configured GitHub host itself: no other scheme, host, user info or port."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == github_host.lower() and port is None and not parts.username
            and not parts.password and "@" not in parts.netloc)


def pr_steps(steps, github_host) -> list:
    """The request's pull requests for its page: a step's PR link only when it is an https link on the configured GitHub host."""
    return [{"label": STEP_LABELS[s.step], "url": s.pr_url, "number": s.pr_number, "attempt": s.attempt}
            for s in steps if s.pr_number and _pr_link_ok(s.pr_url, github_host)]


def request_page(request, req, principal):
    """The lifecycle request's page (spec 2026-11-02 "Requests"): its timeline, its pull requests, Retry, Abandon and Stop for an admin
    where rules 7 and 8 allow them (the POSTs check again), a command waiting for the writer (only while the request is open), and what
    ServiceNow was told (the outbox)."""
    store, settings = request.app.state.store, request.app.state.settings
    steps, events, command = store.lifecycle_steps(req.id), store.lifecycle_events(req.id), store.open_lifecycle_command(req.id)
    if req.state not in OPEN_STATES:
        command = None  # the writer finishes a command with the request; one left open is never "carried out on its next pass"
    allowed = commands_allowed(req, steps, command)
    by = store.get_person(command.requested_by) if command else None
    return page(request, "lifecycle_request.html", req=req, words=lifecycle.ACTION_WORDS[req.final["action"]],
                timeline=lifecycle_timeline(req, steps, events), events=events, pr_steps=pr_steps(steps, settings.github_host),
                command=command,
                command_by=by.display_name if by else "an admin", can_act=principal.is_admin, can_retry=allowed["retry"],
                can_abandon=allowed["abandon"], can_stop=allowed["stop"], mock_task=principal.is_admin and isinstance(settings.servicenow_api, MockServiceNow))


@router.get("/mock/servicenow/tasks/{number}")
def mock_task(request: Request, number: str, principal=ADMIN):
    """The demo's view of a task in the mock ServiceNow (decided while planning: item 9): the fixture task, with the notes the collector
    sent replayed in order into a fresh mock (the collector's mock is another process's memory). Not found unless the mock is set up."""
    settings, store = request.app.state.settings, request.app.state.store
    if not isinstance(settings.servicenow_api, MockServiceNow) or not TASK_NUMBER.fullmatch(number):
        return page(request, "error.html", 404, message="Not found.")
    mock = task_servicenow(settings, store)
    task = mock.task_fields(number)
    if task is None:
        return page(request, "error.html", 404, message="Not found.")
    return page(request, "mock_servicenow_task.html", task=task, history=mock.task_history(number))

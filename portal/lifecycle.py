"""The lifecycle rules (spec 2026-11-02 rules 1 to 4): who may ask, what each action needs, and the words each refusal says. Pure but for
check_task (rule 2, which asks the ServiceNow it is given): the web reads the store and passes what it found here; the writer's recheck
asks the same questions before it plans a step.
Rule 4's "at least one environment in the deploy repo" is the writer's (it reads the deploy repo): nothing_to_suspend (decided while
planning: item 3)."""

import logging

from .servicenow import ServiceNowError
from .store.base import LIFECYCLE_ACTIONS, MAX_REASON, MIN_REASON, TASK_NUMBER

log = logging.getLogger(__name__)

# (decided while planning: item 5) the platform's own repositories, in PORTAL_ORG: never decommissioned (archived) through the portal
PLATFORM_REPOS = ("platform-pipeline", "scorecard-factory", "golden-road-deploy", "golden-road-portal", "golden-road-agents", "security-scans")
ACTION_WORDS = {"suspend": "Suspend", "resume": "Resume", "decommission": "Decommission"}

NOT_ADMIN = "Only a portal admin can suspend, resume or decommission an app."
NO_SERVICENOW = "ServiceNow is not set up here (PORTAL_SERVICENOW), so no lifecycle action can be tied to a task."
BAD_TASK = "The task number is SCTASK followed by 7 digits."
BAD_REASON = f"Write the reason in {MIN_REASON} to {MAX_REASON} characters on one line."
CONFIRM_REPO = "Type the repository's name, {repo}, to confirm the decommission."
TASK_NOT_FOUND = "ServiceNow has no task {task}."
TASK_CLOSED = "Task {task} is closed in ServiceNow."
TASK_OTHER_CI = "Task {task} names another CI than {repo}'s."
TASK_NO_CI = "Task {task} names no CI: it must name {repo}'s."
NO_CI = "{repo} has no ServiceNow CI yet: wait until the collector has synced it, then try again."
SERVICENOW_DOWN = "The task can't be checked now: ServiceNow didn't answer. Try again later."
ONE_OPEN = "A lifecycle request for {repo} is already open: wait until it is applied or abandoned."
NOT_REGISTERED = "{repo} is not registered: the factory has no readable intake record for it."
NOT_ACTIVE = "{repo} is not active: it is {state}."
NOT_SUSPENDED = "{repo} is not suspended: there is nothing to resume."
DECOMMISSIONED = "{repo} was decommissioned on {date} for task {task}: nothing more can be asked for it."
V1_RECORD = ("{repo}'s intake record is intake/v1: change the registration first, so the record is intake/v2 and can say it is "
             "decommissioned.")
PLATFORM_REPO = "{repo} is a platform repository: it is never decommissioned through the portal."


class LifecycleRefused(Exception):
    """A refusal in the words above, already filled in."""


def decommissioned_words(state) -> str:
    return DECOMMISSIONED.format(repo=state.repo, date=state.since.date().isoformat(), task=state.task)


def is_platform_repo(repo: str, org: str) -> bool:
    owner, name = repo.split("/")
    return owner.lower() == org.lower() and name.lower() in PLATFORM_REPOS


def refusal(action, *, repo, org, record, state, open_lifecycle) -> str | None:
    """Rules 3 and 4 for `action` on `repo`, or None when it may be asked. record: the factory's IntakeObserved for the repo (or None);
    state: its LifecycleState (None is active); open_lifecycle: its open lifecycle Request (or None). Checked in this order: decommissioned,
    an open request, not registered, then each action's own need."""
    if action not in LIFECYCLE_ACTIONS:
        raise ValueError(f"not a lifecycle action: {action!r}")
    current = state.state if state is not None else "active"
    if current == "decommissioned":
        return decommissioned_words(state)
    if open_lifecycle is not None:
        return ONE_OPEN.format(repo=repo)
    if record is None or record.problem is not None:
        return NOT_REGISTERED.format(repo=repo)
    if action == "suspend" and current != "active":
        return NOT_ACTIVE.format(repo=repo, state=current)
    if action == "resume" and current != "suspended":
        return NOT_SUSPENDED.format(repo=repo)
    if action == "decommission":
        if is_platform_repo(repo, org):
            return PLATFORM_REPO.format(repo=repo)
        if record.schema != "intake/v2":
            return V1_RECORD.format(repo=repo)
    return None


def reason_ok(reason) -> bool:
    """(decided while planning: item 14) one line, 10 to 1000 characters."""
    return isinstance(reason, str) and MIN_REASON <= len(reason) <= MAX_REASON and "\n" not in reason and "\r" not in reason


def form_problems(action, repo, task, reason, *, confirm_repo=None) -> list[str]:
    """The form's own problems, before anything is asked of ServiceNow: the task's shape, the reason, and for a decommission the repo
    typed again."""
    problems = []
    if not isinstance(task, str) or not TASK_NUMBER.fullmatch(task):
        problems.append(BAD_TASK)
    if not reason_ok(reason):
        problems.append(BAD_REASON)
    if action == "decommission" and confirm_repo != repo:
        problems.append(CONFIRM_REPO.format(repo=repo))
    return problems


def request_note(action, requester_name, reason) -> str:
    """The first work note (rule 2: the reason goes into the request, the audit log and this note)."""
    return f"Requested in the golden road portal by {requester_name}: {action} the app. Reason: {reason}"


def check_task(servicenow, *, task, repo, ci_sys_id) -> str:
    """Rule 2 at request time: the task (already SCTASK and 7 digits) is in ServiceNow, open, and names exactly the app's CI. Returns the
    task's sys_id (stored on the request: every note goes to it). Raises LifecycleRefused: no ServiceNow here, no CI yet (the collector
    hasn't synced the app), ServiceNow down (the platform doesn't apply an action it can't tie to a task; why is logged), not found,
    closed, no CI, another CI."""
    if servicenow is None:
        raise LifecycleRefused(NO_SERVICENOW)
    if ci_sys_id is None:
        raise LifecycleRefused(NO_CI.format(repo=repo))
    try:
        with servicenow.client() as client:
            found = client.task(task)
    except ServiceNowError as exc:  # its text names the status or the exception's type, never the credentials
        log.warning("the task check for %s could not ask ServiceNow: %s", repo, exc)
        raise LifecycleRefused(SERVICENOW_DOWN) from None
    if found is None:
        raise LifecycleRefused(TASK_NOT_FOUND.format(task=task))
    if not found.open:
        raise LifecycleRefused(TASK_CLOSED.format(task=task))
    if found.ci is None:
        raise LifecycleRefused(TASK_NO_CI.format(task=task, repo=repo))
    if found.ci != ci_sys_id:
        raise LifecycleRefused(TASK_OTHER_CI.format(task=task, repo=repo))
    return found.sys_id

"""The request page's timeline (spec 2026-10-30 R5): from the request's own state and its audit entries; no new data. The steps are
Submitted; Sent back, only when it was; Reviewed; then Recorded (done, being recorded, or failed). Each has a status word as well as a color (R10).

Where ServiceNow is set up (PORTAL_SERVICENOW, spec 2026-10-31 "What owning the app means"), a last step, Registered in ServiceNow, follows
Recorded: from the collector's CMDB record of the repo, done with the CI's number once a sync at or after the merge succeeded, failed with
the error's word when the last attempt after the merge failed, and now while the collector's next pass hasn't synced it."""

import dataclasses as dc
import datetime as dt

STATUS_WORDS = {"done": "Done", "now": "Now", "todo": "Not yet", "failed": "Failed", "closed": "Closed"}
RECORDING = ("approved", "writing", "pr_open")  # approved and on its way into the factory
OFF = object()  # timeline(cmdb=OFF): ServiceNow is not set up here, so there is no CMDB step (None: set up, nothing recorded yet)


@dc.dataclass(frozen=True)
class Step:
    label: str
    status: str  # done, now, todo, failed or closed (a lifecycle step an abandon or a stop closed)
    at: dt.datetime | None
    detail: str | None = None  # the CI's number, or the error's word (Registered in ServiceNow)


def _last(entries, action):
    return max((e.at for e in entries if e.action == action), default=None)


def _cmdb_step(request, entries, cmdb) -> Step:
    merged = (_last(entries, "request.merged") or request.updated_at) if request.state == "merged" else None
    if merged is None:
        return Step("Registered in ServiceNow", "todo", None)
    if cmdb is not None and cmdb.number and cmdb.last_synced_at is not None and cmdb.last_synced_at >= merged:
        return Step("Registered in ServiceNow", "done", cmdb.last_synced_at, cmdb.number)
    if cmdb is not None and cmdb.last_error and cmdb.last_error_at is not None and cmdb.last_error_at >= merged:
        return Step("Registered in ServiceNow", "failed", cmdb.last_error_at, cmdb.last_error)
    return Step("Registered in ServiceNow", "now", None)


def timeline(request, entries, cmdb=OFF) -> list[Step]:
    """`entries` are the request's audit entries (store.audit_for("request", id)), oldest first. `cmdb` is the repo's CmdbRecord, or
    None when the collector has recorded none yet; OFF (the default) leaves the CMDB step out."""
    submitted = min((e.at for e in entries if e.action == "request.created"), default=request.created_at)
    steps = [Step("Submitted", "done", submitted)]
    sent_back = _last(entries, "request.sent_back")
    if sent_back is not None:
        steps.append(Step("Sent back", "now" if request.state == "sent_back" else "done", sent_back))
    approved = _last(entries, "request.approved")
    if approved is not None:
        steps.append(Step("Reviewed", "done", approved))
    else:
        steps.append(Step("Reviewed", "now" if request.state == "pending" else "todo", None))
    if request.state == "failed":  # "Recorded" with the status word Failed, so the word shows once
        steps.append(Step("Recorded", "failed", _last(entries, "request.failed") or request.updated_at))
    else:
        status = "done" if request.state == "merged" else "now" if request.state in RECORDING else "todo"
        steps.append(Step("Recorded", status, _last(entries, "request.merged")))
    if cmdb is not OFF:
        steps.append(_cmdb_step(request, entries, cmdb))
    return steps


# ---- a lifecycle request (spec 2026-11-02 "Requests") ----

STEP_LABELS = {"deploy": "Deploy repo pull request", "factory": "Factory record pull request", "archive": "Repository archived"}
STEP_STATUS = {"planned": "todo", "pr_open": "now", "done": "done", "failed": "failed", "closed": "closed"}
ENDED = {"abandoned": "Abandoned", "stopped": "Stopped"}  # a request an admin ended (its failure reason): its last step


def lifecycle_timeline(request, steps, events) -> list[Step]:
    """A lifecycle request's timeline: Requested (with its task), each step with its state (a PR step's detail is its PR number, a failed
    one's its reason), then Applied in git, or Abandoned, or Stopped, or Refused (a request the writer failed before any step, with its reason).
    While the request is writing, its first step not done is Now."""
    requested = next((e.created_at for e in events if e.step == "requested"), request.created_at)
    out = [Step("Requested", "done", requested, request.final["task"])]
    current = next((s.step for s in steps if s.state not in ("done", "closed")), None)
    for s in steps:
        status = STEP_STATUS[s.state]
        if s.state == "planned" and s.step == current and request.state == "writing":
            status = "now"
        detail = s.failure_reason if s.state == "failed" else (f"#{s.pr_number}" if s.pr_number else None)
        out.append(Step(STEP_LABELS[s.step], status, s.updated_at if s.state != "planned" else None, detail))
    if request.state == "merged":
        out.append(Step("Applied in git", "done", request.updated_at))
    elif request.state == "failed":
        ended = ENDED.get(request.failure_reason)
        # an abandon or a stop closes the request as it closes its steps (the status word Closed); a refusal is a failure
        out.append(Step(ended or "Refused", "closed" if ended else "failed", request.updated_at, None if ended else request.failure_reason))
    else:
        out.append(Step("Applied in git", "todo", None))
    return out

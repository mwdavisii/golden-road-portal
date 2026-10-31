"""The request page's timeline (spec 2026-10-30 R5): from the request's own state and its audit entries; no new data. The steps are
Submitted; Sent back, only when it was; Reviewed; then Recorded (done, being recorded, or failed). Each has a status word as well as a color (R10)."""

import dataclasses as dc
import datetime as dt

STATUS_WORDS = {"done": "Done", "now": "Now", "todo": "Not yet", "failed": "Failed"}
RECORDING = ("approved", "writing", "pr_open")  # approved and on its way into the factory


@dc.dataclass(frozen=True)
class Step:
    label: str
    status: str  # done, now, todo or failed
    at: dt.datetime | None


def _last(entries, action):
    return max((e.at for e in entries if e.action == action), default=None)


def timeline(request, entries) -> list[Step]:
    """`entries` are the request's audit entries (store.audit_for("request", id)), oldest first."""
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
    return steps

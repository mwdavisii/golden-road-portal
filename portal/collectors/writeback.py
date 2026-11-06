"""The collector's write-back (spec 2026-11-02 L10, L11): each pass sends the outbox (lifecycle_events) to the ServiceNow tasks, per request in
event order, so a note is never sent before an earlier one. Every note ends with the portal's request link. A completed event closes the
task (Closed Complete, its note the close notes); every other event is one work note (an abandoned request has no completed event: its
task stays open, rule 8). A failure keeps the event unsent, with one more attempt and the client's error (the client's own words, never
the instance's), and stops that request's notes for this pass; the next pass tries again, with no attempt cap (no budgets). A ServiceNow
outage delays the notes, never loses them, and never blocks the GitHub side: the writer doesn't wait on the outbox.
A note is marked sent after ServiceNow took it: a pass that stops between the two sends it again on the next pass (at least once).
One pass sends at a time (the store's writeback_lock: Postgres's advisory lock): a pass that can't take it sends nothing ("busy"). A mark
the store refuses (another pass marked the note first) stops that request's notes for this pass, never the pass."""

import itertools

from ..outbox import note_text
from ..servicenow import ServiceNowError
from ..store.base import MAX_STEP_REASON, Conflict


def collect_writeback(store, servicenow, *, base_url, now) -> dict:
    """One pass: {"sent", "failed", "waiting"} (waiting: later notes of a request whose earlier one failed this pass), and "busy": True
    when another pass holds the write-back lock (nothing sent)."""
    with store.writeback_lock() as held:
        if not held:
            return {"sent": 0, "failed": 0, "waiting": 0, "busy": True}
        return _send(store, servicenow, base_url=base_url, now=now)


def _send(store, servicenow, *, base_url, now) -> dict:
    events = sorted(store.lifecycle_events(unsent=True), key=lambda e: (e.request_id, e.id))
    counts = {"sent": 0, "failed": 0, "waiting": 0}
    with servicenow.client() as client:
        for _, group in itertools.groupby(events, key=lambda e: e.request_id):
            group = list(group)
            for i, event in enumerate(group):
                text = note_text(event, base_url)
                try:
                    if event.step == "completed":
                        client.close_task(event.task_sys_id, text)
                    else:
                        client.add_work_note(event.task_sys_id, text)
                except ServiceNowError as exc:
                    try:
                        store.mark_lifecycle_event_failed(event_id=event.id, error=str(exc)[:MAX_STEP_REASON] or "ServiceNowError",
                                                          now=now)
                    except Conflict:  # another pass sent it meanwhile: nothing failed here, and its request's rest are that pass's
                        break
                    counts["failed"] += 1
                    counts["waiting"] += len(group) - i - 1
                    break
                try:
                    store.mark_lifecycle_event_sent(event_id=event.id, now=now)
                except Conflict:  # another pass marked it first (a race the lock didn't cover): its request's rest are that pass's
                    break
                counts["sent"] += 1
    return counts

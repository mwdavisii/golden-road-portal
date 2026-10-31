"""The Re-review page (spec 2026-10-31 R1, R6 to R8, R10): the open items, oldest first, with what opened each; an admin closes one with
a note, never one for an app they own (the store and the database refuse it too). The collector opens items and adds events to them; this
page only reads them and closes them. A close names how many events the admin saw (the form's events_seen): a later one sends the page
back, so the decision is about what was seen (R6)."""

import datetime as dt

from fastapi import APIRouter, Form, Request

from ..rereview import is_stale
from ..store.base import Conflict
from .access import ADMIN
from .flash import redirect
from .render import page
from .routes_admin import reason_ok

router = APIRouter()
NOTE_NEEDED = "Write what you decided, in 10 to 1000 characters on one line."
RECORD_UNREADABLE = "This app's intake record can't be read, so the portal can't tell who owns it: close the item once the record is fixed."
OWN_APP = "You own this app, so another admin must close its item."
OUT_OF_DATE = "The page was out of date: look at the item again and close it from there."
CLOSE_CONFLICT = {"not_found": (404, "No such item."), "closed": (409, "That item was already closed."),
                  "own_app": (403, OWN_APP),
                  "item_changed": (409, "A scorecard event joined this item after you opened the page: read it, then close the item again.")}
PART_WORDS = {"repo_admins": "repo admins", "evidence": "registration evidence", "intake": "intake records", "events": "scorecard events"}
DEFAULT_INTERVAL = 300  # the collector's default PORTAL_COLLECT_INTERVAL, until it has recorded its own
REGISTERED = ("approved", "writing", "pr_open", "merged")


def _owners(store, repo, record) -> set:
    """Who owns the app: the observed record's owner, and the requester of its last approved registration (the record may not show a
    change yet). A v1 record's owner is a team name, which matches nobody."""
    owners = {record.owner} if record is not None and isinstance(record.owner, str) else set()
    last = next((r for r in store.requests_for_repo(repo) if r.kind == "registration" and r.state in REGISTERED), None)
    return owners | ({last.requested_by} if last else set())


def _ago(delta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"{hours // 24} days ago"


# Why an observed record isn't an item (IntakeObserved.problem), in words written here
PROBLEM_WORDS = {"invalid": "is not a valid intake record (the scorecard refuses it too)",
                 "unreadable": "is too large or can't be decoded",
                 "not_a_file": "is a symlink or a submodule, which the portal never follows",
                 "case_mismatch": "is in the factory under two spellings of its name (GitHub's paths are case-sensitive): rename or remove the "
                                  "extra one in the factory"}


def rereview_page(request, principal, status=200, errors=()):
    state = request.app.state
    store, now, days = state.store, state.clock(), state.settings.rereview_stale_days
    observed = {o.repo.lower(): o for o in store.intake_observed()}
    entries = []
    for item in store.rereview_items():
        record = observed.get(item.repo.lower())
        owner = record.owner if record else None
        person = store.get_person(owner) if isinstance(owner, str) and owner.startswith("p_") else None
        entries.append({"item": item, "stale": is_stale(item, now, days), "days": (now - item.opened_at).days,
                        "mine": principal.person.id in _owners(store, item.repo, record), "owner": person.display_name if person else owner,
                        "unreadable": record is not None and record.problem is not None})
    counts, sources = store.rereview_run_counts(), store.rereview_status()
    interval = (sources.get("repo_admins").counts.get("interval_seconds") if sources.get("repo_admins") else None) or DEFAULT_INTERVAL
    parts = [{"name": PART_WORDS[name], "status": s, "checked": _ago(now - s.checked_at),
              "worked": _ago(now - s.last_ok_at) if s.last_ok_at else None,
              "stopped": s.last_ok_at is None or now - s.last_ok_at > dt.timedelta(seconds=3 * interval)}
             for name, s in sorted(sources.items(), key=lambda kv: list(PART_WORDS).index(kv[0])) if name in PART_WORDS]
    runs = [{"repo": w.repo, "run_id": w.run_id, "age": _ago(now - w.updated_at), "rechecked": len(store.failed_runs(w.repo))}
            for w in store.rereview_watermarks()]
    return page(request, "admin_rereview.html", status, items=entries, errors=list(errors), stale_days=days,
                stale_count=sum(1 for e in entries if e["stale"]), sources=sources, problem_words=PROBLEM_WORDS, parts=parts, runs=runs,
                pending=len(store.pending_events()), skipped=sum(n for outcome, n in counts.items() if outcome not in ("event", "duplicate")),
                outside=[o for o in observed.values() if o.schema == "intake/v1"], unreadable=[o for o in observed.values() if o.problem])


@router.get("/admin/rereview")
def rereview(request: Request, principal=ADMIN):
    return rereview_page(request, principal)


@router.post("/admin/rereview/{item_id}/close")
def close(request: Request, item_id: str, note: str = Form(""), events_seen: str = Form(""), principal=ADMIN):
    note = note.strip()
    if not reason_ok(note):
        return rereview_page(request, principal, 400, [NOTE_NEEDED])
    if not (events_seen.isascii() and events_seen.isdigit() and len(events_seen) <= 6):  # the form always sends it: a page from before, or not the form
        return rereview_page(request, principal, 400, [OUT_OF_DATE])
    store = request.app.state.store
    item = next((i for i in store.rereview_items(open_only=False) if i.id == item_id), None)
    if item is not None:
        record = next(iter(store.intake_observed(item.repo)), None)
        if record is not None and record.problem is not None:
            return rereview_page(request, principal, 409, [RECORD_UNREADABLE])
        if principal.person.id in _owners(store, item.repo, record):
            return rereview_page(request, principal, 403, [OWN_APP])
    try:
        request.app.state.store.close_rereview_item(item_id=item_id, actor_id=principal.person.id, note=note, now=request.app.state.clock(),
                                                    events_seen=int(events_seen))
    except Conflict as exc:
        status, message = CLOSE_CONFLICT.get(exc.code, (409, "That item can't be closed."))
        return rereview_page(request, principal, status, [message])
    return redirect("/admin/rereview", "rereview_closed")

"""Scorecard re-review events (spec 2026-10-31 R4 to R6, R10), read through the read App. Every pass lists the whole run list of the
factory's scorecard-assess (central mode) and of each per-app repo's scorecard workflow (an observed record not marked central),
newest first, down to the source's watermark: every run above it is done. Only a run still going holds the watermark below it; one going
for more than a day, and one that finished without success, are kept and re-checked by id (at most hourly after their first day) until a
later attempt succeeds (then they are read), their artifacts are gone, or GitHub would no longer re-run them (30 days); a cancelled
central run is dropped, since the poller dispatches anew. A walk cut short by GitHub being busy doesn't move the watermark, and a run
GitHub never answers for is recorded unreadable after twelve passes, so the runs below it are read. A trusted run's valid event whose app
isn't registered yet waits, stored, and is read again each pass without another download.

A run is trusted only when GitHub says it is the factory's own assessment of that head (central: the workflow file, the default branch
main, the factory itself, a title shaped as the workflow's run-name and naming the event's repo, PR and head, and a head that is the
branch's commit or behind it) or the app's own pull request run calling the factory's reusable workflow from main (per-app: the event,
the head repository, the head SHA); anything else is untrusted_run. Each trusted run's `scorecard` artifact is downloaded once
(artifacts.event_from_zip reads it); a download redirected to a host the collector won't fetch from records nothing and is said on the
Re-review page (artifact_host_refused). A valid event is stored once per source, repo and head; one whose signals match the configured
set opens a scorecard_signal item for its app, or joins the open one. Every run is recorded with its outcome, so nothing is downloaded
twice and a failure is counted, never shown. Whatever the store refuses of one event is that run's outcome (invalid): one bad input
never stops the pass."""

import collections
import datetime as dt
import re

import psycopg

from ..github.client import GitHubError, HostRefused, TooLarge
from ..github.shapes import BRANCH, SHA
from ..rereview import matching
from ..store.base import Conflict
from ..writer.allowlist import factory
from .artifacts import ARTIFACT_MAX_BYTES, event_from_zip
from .repo_files import Transient, is_transient

CENTRAL_WORKFLOW, APP_WORKFLOW, ARTIFACT = "scorecard-assess.yml", "scorecard.yml", "scorecard"
CENTRAL_PATH = ".github/workflows/scorecard-assess.yml"
CENTRAL_BRANCH = "main"  # scorecard-assess runs only from refs/heads/main (its jobs' `if`), so the factory's default branch must be main
LONG_RUN = dt.timedelta(hours=24)  # a run still going after this no longer holds the watermark: it is re-checked by id instead
PENDING_DAYS = 90  # an event waiting this long for its app's record is recorded not_registered (its artifact is gone by then anyway)
REUSABLE = ".github/workflows/scorecard.yml"  # the factory's reusable workflow every per-app caller uses, from main
ARTIFACT_HOSTS = ("*.blob.core.windows.net",)  # where github.com's Actions artifacts are stored (PORTAL_ARTIFACT_HOSTS)
PAGE = 100


class ArtifactHostRefused(Transient):
    """GitHub redirected a download to a host the collector won't fetch from (PORTAL_ARTIFACT_HOSTS, or the egress): nothing is recorded,
    and the page says so, until it is fixed."""


def _call(gh, path, params=None):
    try:
        return gh.call("GET", path, params=params).json()
    except GitHubError as exc:
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        raise
    except ValueError as exc:
        raise Transient("unexpected answer") from exc


def _runs(gh, repo, workflow, watermark):
    """Every listed run above the watermark, newest first, however many pages that takes."""
    page = 1
    while True:
        doc = _call(gh, f"/repos/{repo}/actions/workflows/{workflow}/runs", {"per_page": PAGE, "page": page})
        listed = doc.get("workflow_runs") if isinstance(doc, dict) else None
        if not isinstance(listed, list):
            raise Transient("unexpected answer")
        for run in listed:
            if not (isinstance(run, dict) and type(run.get("id")) is int and run["id"] > 0):
                continue
            if watermark is not None and run["id"] <= watermark:
                return
            yield run
        if len(listed) < PAGE:
            return
        page += 1


def _same(a, b) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


TITLE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+ [1-9][0-9]{0,8} [0-9a-f]{40}")  # scorecard-assess's run-name: repo, PR, head


def _central_ok(gh, run, factory_repo, default, branch_sha) -> bool:
    """The factory's own dispatched assessment: its workflow file, from its default branch, in the factory, titled as the workflow's
    run-name titles it (a run from before it, or anyone else's, is untrusted without another call), and a head that is on that branch.
    GitHub reports head_branch "main" for a dispatch from a tag named main too, so the head must be the branch's commit (resolved by
    ref, never by the ambiguous name) or behind it. The title's values are only a consistency check (_speaks_for)."""
    head = run.get("head_sha")
    if not (run.get("event") == "workflow_dispatch" and run.get("path") == CENTRAL_PATH and run.get("head_branch") == default
            and _same((run.get("head_repository") or {}).get("full_name"), factory_repo) and isinstance(head, str) and SHA.fullmatch(head)
            and isinstance(run.get("display_title"), str) and TITLE.fullmatch(run["display_title"])):
        return False
    if head == branch_sha:
        return True
    try:
        compared = _call(gh, f"/repos/{factory_repo}/compare/{branch_sha}...{head}", {"per_page": 1})
    except GitHubError:
        return False  # not a commit of the factory
    return isinstance(compared, dict) and compared.get("status") in ("identical", "behind")


def _app_ok(run, repo, factory_repo) -> bool:
    refs = run.get("referenced_workflows")
    return (run.get("event") == "pull_request" and _same((run.get("head_repository") or {}).get("full_name"), repo) and isinstance(refs, list)
            and any(isinstance(w, dict) and isinstance(w.get("path"), str) and _same(w["path"].split("@")[0], f"{factory_repo}/{REUSABLE}")
                    and w.get("ref") == "refs/heads/main" for w in refs))


def _speaks_for(run, doc, own) -> bool:
    """The run is about this event's head: a central run's title is the event's repo, PR and head; an app's run is for that head."""
    if own is None:
        return _same(run.get("display_title"), f"{doc['repo']} {doc['pr']} {doc['head_sha']}")
    return run.get("head_sha") == doc["head_sha"]


def _file(store, repo, head_sha, matched, now, source="central"):
    """The event onto the app's open scorecard item, or a new one. Read again once when an admin closed (or another pass opened) the item
    meanwhile; an event already on its item is done."""
    for attempt in (1, 2):
        open_item = next((i for i in store.rereview_items() if i.reason == "scorecard_signal" and i.repo.lower() == repo.lower()), None)
        try:
            if open_item is not None:
                store.add_rereview_item_event(item_id=open_item.id, head_sha=head_sha, matched=matched, now=now, source=source)
            else:
                store.open_rereview_item(repo=repo, reason="scorecard_signal", key=head_sha, now=now, event=(head_sha, matched, source))
            return
        except Conflict as exc:
            if exc.code not in ("closed", "item_open") or attempt == 2:
                return  # event_exists, item_exists: filed already


def _filed(store, repo, head_sha, source) -> bool:
    """An item, open or closed, already lists this event: it was decided on, or is waiting to be."""
    return any(e.event.head_sha == head_sha and e.event.source == source for i in store.rereview_items(open_only=False)
               if i.repo.lower() == repo.lower() for e in i.events)


def _store_event(store, doc, record, run_repo, run_id, source, signals, now) -> str:
    """event or duplicate: a valid event of a registered app, stored once per source, repo and head, and filed when its signals match."""
    stored = store.add_rereview_event(doc={**doc, "repo": record.repo}, run_repo=run_repo, run_id=run_id, now=now, source=source)
    if stored is None:
        stored = store.rereview_event(record.repo, doc["head_sha"], source)
        if stored is None or not (_same(stored.run_repo, run_repo) and stored.run_id == run_id):
            return "duplicate"
        if _filed(store, record.repo, stored.head_sha, source):
            return "event"  # this run's event, from a pass that stopped after filing it: nothing more to do
        # this very run's event, stored by a pass that stopped before it filed it and recorded the run: file it now
    matched = matching(stored.signals, signals)
    if matched:
        _file(store, record.repo, stored.head_sha, matched, now, source)
    return "event"


def _take(gh, store, repo, run, own, records, signals, now, hosts) -> str:
    try:
        listed = _call(gh, f"/repos/{repo}/actions/runs/{run['id']}/artifacts", {"name": ARTIFACT}).get("artifacts")
    except (GitHubError, AttributeError):
        return "unreadable"
    found = [a for a in listed if isinstance(a, dict) and a.get("name") == ARTIFACT] if isinstance(listed, list) else []
    if not found:
        return "no_artifact"
    artifact = found[0]
    if artifact.get("expired") is True:
        return "expired"
    if type(artifact.get("id")) is not int or type(artifact.get("size_in_bytes")) is not int:
        return "unreadable"
    if artifact["size_in_bytes"] > ARTIFACT_MAX_BYTES:
        return "too_large"
    try:
        data = gh.download(f"/repos/{repo}/actions/artifacts/{artifact['id']}/zip", cap=ARTIFACT_MAX_BYTES, hosts=hosts)
    except TooLarge:
        return "too_large"
    except HostRefused as exc:
        raise ArtifactHostRefused(str(exc)) from exc
    except GitHubError as exc:
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        return "unreadable"
    doc = event_from_zip(data)
    if isinstance(doc, str):
        return doc
    if not _speaks_for(run, doc, own):
        return "untrusted_run"
    if own is not None and doc["repo"].lower() != own.lower():
        return "repo_mismatch"  # an app's own run speaks only for that app
    source = "central" if own is None else "per-app"
    record = records.get(doc["repo"].lower())
    if record is None:  # kept, not recorded, and holding nothing back: read again each pass once the app's record is observed
        store.hold_pending_event(source=source, run_repo=repo, run_id=run["id"], doc=doc, now=now)
        return "not_registered"
    return _store_event(store, doc, record, repo, run["id"], source, signals, now)


RETENTION_DAYS = 90  # GitHub's default artifact retention: a failed run older than its artifacts' is not re-checked (scope, not a budget)
RERUN_WINDOW = dt.timedelta(days=30)  # GitHub re-runs a run only within 30 days of its creation
RECHECK_AFTER_A_DAY = dt.timedelta(hours=1)  # after its first day, a kept run is checked at most hourly
STUCK_PASSES = 12  # passes GitHub couldn't answer for one run before it is recorded unreadable (a liveness bound, not a budget)


def _time(value):
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except (TypeError, ValueError):
        return None


def _retention(gh, repo, run_id) -> int:
    """How long this run's artifacts are kept, from the artifacts GitHub lists for it (expires_at - created_at), else 90 days."""
    try:
        listed = _call(gh, f"/repos/{repo}/actions/runs/{run_id}/artifacts").get("artifacts")
    except (GitHubError, AttributeError):
        return RETENTION_DAYS
    spans = [(_time(a.get("expires_at")) - _time(a.get("created_at"))).days for a in listed or []
             if isinstance(a, dict) and _time(a.get("expires_at")) and _time(a.get("created_at"))] if isinstance(listed, list) else []
    return min(max(max(spans), 1), 400) if spans else RETENTION_DAYS


def _walk(gh, store, repo, workflow, own, trusted, records, signals, now, hosts, counts):
    """One source: re-check the runs that had failed or ran long, then walk down to the watermark, and move it (or note when the walk
    last finished) when the walk did. Raises Transient, GitHubError or Conflict."""
    stuck = store.stuck_runs(repo)

    def process(run) -> str:
        if not trusted(run):
            outcome = "untrusted_run"
        else:
            try:
                outcome = _take(gh, store, repo, run, own, records, signals, now, hosts)
            except (ValueError, psycopg.DataError):
                outcome = "invalid"  # what the store refuses of this event is this run's outcome; an operational error still stops the pass
            except ArtifactHostRefused:
                raise  # a setting or the egress, not this run
            except Transient:
                if store.note_stuck_run(repo=repo, run_id=run["id"], now=now) < STUCK_PASSES:
                    raise
                outcome = "unreadable"  # a liveness bound on one run, not a budget: the runs below it are read
                counts["unreadable_after_retries"] += 1
        if run["id"] in stuck:
            store.clear_stuck_run(repo=repo, run_id=run["id"])
        counts[outcome] += 1
        if outcome != "not_registered":
            store.record_rereview_run(repo=repo, run_id=run["id"], outcome=outcome, now=now)
        return outcome

    for failed in store.failed_runs(repo):  # each until a later attempt succeeds, its artifacts go, or GitHub would no longer re-run it
        if failed.seen_at and failed.checked_at and now - failed.seen_at > dt.timedelta(days=1) and now - failed.checked_at < RECHECK_AFTER_A_DAY:
            continue
        try:
            run = _call(gh, f"/repos/{repo}/actions/runs/{failed.run_id}")
        except GitHubError as exc:
            if exc.status != 404:
                raise
            store.drop_failed_run(repo=repo, run_id=failed.run_id)  # the run was deleted
            continue
        if not isinstance(run, dict) or run.get("id") != failed.run_id:
            continue
        last = _time(run.get("updated_at")) or failed.last_attempt_at
        attempt = run.get("run_attempt") if type(run.get("run_attempt")) is int and run["run_attempt"] >= 1 else failed.attempt
        if run.get("status") != "completed":  # a re-run still going, or a run going for days: next time
            store.record_failed_run(repo=repo, run_id=failed.run_id, attempt=attempt, last_attempt_at=failed.last_attempt_at,
                                    retention_days=failed.retention_days, now=now)
            continue
        if run.get("conclusion") == "success":
            if not store.rereview_runs_seen(repo, [failed.run_id]):
                process(run)  # a not_registered event now waits with the pending events
            store.drop_failed_run(repo=repo, run_id=failed.run_id)
            continue
        created = _time(run.get("created_at")) or failed.seen_at or last
        gone = min(last + dt.timedelta(days=failed.retention_days), created + RERUN_WINDOW)
        if (own is None and run.get("conclusion") == "cancelled") or now > gone:
            store.drop_failed_run(repo=repo, run_id=failed.run_id)  # the poller dispatches a cancelled assessment anew
        else:
            store.record_failed_run(repo=repo, run_id=failed.run_id, attempt=attempt, last_attempt_at=last,
                                    retention_days=failed.retention_days, now=now)

    mark = store.rereview_watermark(repo)
    known_failed = {f.run_id for f in store.failed_runs(repo)}
    newest, held = None, None
    for run in _runs(gh, repo, workflow, mark):
        newest = max(newest or 0, run["id"])
        if run["id"] in known_failed and run.get("status") != "completed":
            continue  # re-checked by id
        if run.get("status") != "completed":
            started = _time(run.get("run_started_at")) or _time(run.get("created_at"))
            if started is not None and now - started > LONG_RUN:  # still going after a day: re-checked by id, holding nothing back
                attempt = run.get("run_attempt") if type(run.get("run_attempt")) is int and run["run_attempt"] >= 1 else 1
                store.record_failed_run(repo=repo, run_id=run["id"], attempt=attempt, last_attempt_at=started, retention_days=RETENTION_DAYS,
                                        now=now)
                continue
            held = min(held or run["id"], run["id"])  # still going: read it once it finishes
            continue
        if store.rereview_runs_seen(repo, [run["id"]]):
            continue
        if run.get("conclusion") != "success":
            if own is None and run.get("conclusion") == "cancelled":
                continue  # the poller dispatches a cancelled assessment anew
            if run["id"] not in known_failed:  # re-checked from now on, so the watermark may pass it
                attempt = run.get("run_attempt") if type(run.get("run_attempt")) is int and run["run_attempt"] >= 1 else 1
                store.record_failed_run(repo=repo, run_id=run["id"], attempt=attempt, last_attempt_at=_time(run.get("updated_at")) or now,
                                        retention_days=_retention(gh, repo, run["id"]), now=now)
            continue
        process(run)
        if run["id"] in known_failed:
            store.drop_failed_run(repo=repo, run_id=run["id"])
    top = newest if held is None else held - 1
    target = top if top is not None and top >= 1 else mark
    if target is not None:  # moved, or the walk finished again at the same place: the page shows when it last did
        store.set_rereview_watermark(repo=repo, run_id=max(target, mark or 0), now=now)


def collect_events(gh, store, *, org, signals, now, actions_readable=True, artifact_hosts=ARTIFACT_HOSTS) -> dict:
    """One pass. {"stored": n, "skipped": m} plus "problem" (actions_unreadable) or "transient" when there was one. actions_readable is
    False when the read App's token says it can't read Actions, None when that isn't known yet (the calls tell)."""
    if actions_readable is False:
        store.set_rereview_status(source="events", problem="actions_unreadable", counts={}, now=now)
        return {"stored": 0, "skipped": 0, "problem": "actions_unreadable"}
    records = {r.repo.lower(): r for r in store.intake_observed() if r.problem is None}
    factory_repo = factory(org)
    counts, problem, transient = collections.Counter(), None, False
    for pending in store.pending_events():  # events waiting for their app's record
        record = records.get(str(pending.doc.get("repo", "")).lower())
        try:
            if record is not None:
                outcome = _store_event(store, pending.doc, record, pending.run_repo, pending.run_id, pending.source, signals, now)
            elif now - pending.seen_at > dt.timedelta(days=PENDING_DAYS):
                outcome = "not_registered"
            else:
                counts["not_registered"] += 1
                continue
            counts[outcome] += 1
            store.record_rereview_run(repo=pending.run_repo, run_id=pending.run_id, outcome=outcome, now=now)
        except Conflict:
            pass  # recorded already
        except (ValueError, psycopg.DataError):
            store.record_rereview_run(repo=pending.run_repo, run_id=pending.run_id, outcome="invalid", now=now)
        store.drop_pending_event(run_repo=pending.run_repo, run_id=pending.run_id)
    sources = []
    try:
        default = _call(gh, f"/repos/{factory_repo}").get("default_branch")
        if not isinstance(default, str) or not BRANCH.fullmatch(default):
            raise Transient("unexpected answer")
        if default != CENTRAL_BRANCH:
            problem = "factory_not_main"  # scorecard-assess runs only from main: say so rather than read nothing
        else:  # the whole list: the filtered endpoint stops at 1000 runs, so what is trusted is decided here, run by run
            branch_sha = (_call(gh, f"/repos/{factory_repo}/git/ref/heads/{default}").get("object") or {}).get("sha")
            if not isinstance(branch_sha, str) or not SHA.fullmatch(branch_sha):
                raise Transient("unexpected answer")
            sources = [(factory_repo, CENTRAL_WORKFLOW, None, lambda run: _central_ok(gh, run, factory_repo, default, branch_sha))]
    except Transient:
        transient = True
    except GitHubError as exc:
        problem = "factory_unreadable" if exc.status in (403, 404) else problem
        transient = transient or exc.status not in (403, 404)
    except AttributeError:
        transient = True
    sources += [(r.repo, APP_WORKFLOW, r.repo, lambda run, repo=r.repo: _app_ok(run, repo, factory_repo))
                for r in sorted(records.values(), key=lambda r: r.repo.lower()) if r.scorecard != "central"]
    for repo, workflow, own, trusted in sources:
        try:
            _walk(gh, store, repo, workflow, own, trusted, records, signals, now, artifact_hosts, counts)
        except ArtifactHostRefused:
            transient, problem = True, problem or "artifact_host_refused"
        except Transient:
            transient = True
        except Conflict:
            transient = True  # a concurrent pass recorded the same run
        except GitHubError as exc:
            if own is None and exc.status == 403:
                problem = "actions_unreadable"
            elif own is not None and exc.status in (403, 404):
                counts["unreadable_repos"] += 1
            elif exc.status != 404:
                transient = True
    walked = {repo.lower() for repo, *_ in sources} | {factory_repo.lower()}  # the factory's are kept even when it couldn't be walked
    for failed in store.failed_runs():
        if failed.repo.lower() not in walked:
            store.drop_failed_run(repo=failed.repo, run_id=failed.run_id)  # no longer a source: not registered, or now central
    status = problem or ("github_busy" if transient else None)  # a busy pass is not a good one: last_ok_at stays
    store.set_rereview_status(source="events", problem=status, counts=dict(counts), now=now)
    result = {"stored": counts["event"], "skipped": sum(v for k, v in counts.items() if k not in ("event", "unreadable_repos"))}
    if problem:
        result["problem"] = problem
    if transient:
        result["transient"] = True
    return result

"""Scorecard events onto the re-review list (spec 2026-10-31 R4 to R6, R10), against a fake GitHub and a fake artifact storage."""

import datetime as dt
import json

import httpx
import pytest
from portal_fakes import READ_PERMISSIONS, READ_WITH_ACTIONS, T0, FakeGitHub, FakeStorage, transport, zipped

from portal.collectors.events import collect_events
from portal.github.client import GitHub
from portal.rereview import DEFAULT_SIGNALS, parse_signals
from portal.store.base import IntakeObserved
from portal.store.memory import MemoryStore

FACTORY = "acme/scorecard-factory"
T17 = {"id": "T-17", "kind": "trigger", "level": "elevated", "summary": "resources_tier raised", "count": 1}
T5 = {"id": "T-5", "kind": "trigger", "level": "elevated", "summary": "docs changed", "count": 1}


def event(repo="acme/notes", head="a", signals=(T17,), pr=7):
    return {"schema": "re-review-event/v1", "repo": repo, "pr": pr, "head_sha": head * 40, "tree_sha": "b" * 40, "scorecard_version": "0.5.1",
            "created": "2026-10-31T12:00:00Z", "level": "elevated", "floor": "standard", "complete": True, "signals": list(signals)}


def registered(store, *repos, central=("acme/notes",)):
    store.replace_intake_observed([IntakeObserved(r, "c" * 40, "intake/v2", True, "p_" + "1" * 32, "low", "public", "Stores notes.",
                                                  dt.date(2027, 1, 1), "central" if r in central else None, None) for r in repos], observed_at=T0)


HOSTS = ("*.storage.example.test",)  # the fake storage's host


def trusted(repo, run_id, doc, workflow, **changes):
    """A run as GitHub lists it, with what the collector checks before it trusts its artifact ."""
    head = (doc or {}).get("head_sha", "a" * 40)
    if workflow == "scorecard-assess.yml":   # the factory's central review: dispatched on main, titled with what it assessed
        run = {"event": "workflow_dispatch", "path": ".github/workflows/scorecard-assess.yml", "head_branch": "main", "head_sha": "c" * 40,
               "display_title": f"{(doc or {}).get('repo', 'acme/notes')} {(doc or {}).get('pr', 7)} {head}"}
    else:                                    # an app's own caller of the factory's reusable workflow
        run = {"event": "pull_request", "path": ".github/workflows/scorecard.yml", "head_branch": "feature", "head_sha": head,
               "referenced_workflows": [{"path": "acme/scorecard-factory/.github/workflows/scorecard.yml@main", "ref": "refs/heads/main",
                                         "sha": "c" * 40}]}
    return {"id": run_id, "status": "completed", "conclusion": "success", "head_repository": {"full_name": repo}, "run_attempt": 1,
            "updated_at": "2026-10-23T11:00:00Z", **run, **changes}


class World:
    def __init__(self, permissions=READ_WITH_ACTIONS):
        self.gh, self.storage = FakeGitHub(permissions=permissions), FakeStorage()
        self.client = GitHub("https://api.github.com", httpx.Client(transport=transport(self.gh, self.storage)), lambda: "ghs_x")
        self.gh.on("GET", f"/repos/{FACTORY}", (200, {"default_branch": "main"}))
        self.gh.on("GET", f"/repos/{FACTORY}/git/ref/heads/main", (200, {"object": {"sha": "c" * 40}}))   # the branch's commit, by ref
        self.gh.on_any("GET", rf"/repos/{FACTORY}/compare/{'c' * 40}\.\.\.[0-9a-f]{{40}}", (200, {"status": "behind"}))  # on main
        self.runs = {}

    def run(self, repo, run_id, doc=None, *, workflow="scorecard-assess.yml", size=None, expired=False, artifact=True, raw=None, **changes):
        self.runs.setdefault((repo, workflow), []).insert(0, trusted(repo, run_id, doc, workflow, **changes))
        self.listing(repo, workflow)
        data = raw if raw is not None else zipped({"re-review-event.json": json.dumps(doc).encode()}) if doc is not None else zipped({})
        listed = [{"id": run_id * 10, "name": "scorecard", "size_in_bytes": len(data) if size is None else size, "expired": expired}] if artifact else []
        self.gh.on("GET", f"/repos/{repo}/actions/runs/{run_id}/artifacts", (200, {"artifacts": listed}))
        self.gh.on("GET", f"/repos/{repo}/actions/artifacts/{run_id * 10}/zip", (302, {}, {"location": self.storage.put(f"z/{run_id}.zip", data)}))
        return self

    def listing(self, repo, workflow):
        runs = sorted(self.runs[(repo, workflow)], key=lambda r: -r["id"])
        self.gh.routes[("GET", "api.github.com", f"/repos/{repo}/actions/workflows/{workflow}/runs")] = [
            (200, {"total_count": len(runs), "workflow_runs": runs})]

    def no_runs(self, repo, workflow="scorecard.yml", status=200):
        self.gh.on("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs", (status, {"total_count": 0, "workflow_runs": []}))
        return self

    def collect(self, store, signals=DEFAULT_SIGNALS, readable=True):
        return collect_events(self.client, store, org="acme", signals=signals, now=T0, actions_readable=readable, artifact_hosts=HOSTS)


def test_a_matching_event_is_stored_and_opens_a_scorecard_item():
    store = MemoryStore()
    registered(store, "acme/notes")
    assert World().run(FACTORY, 1, event()).collect(store) == {"stored": 1, "skipped": 0}
    [item] = store.rereview_items()
    assert (item.repo, item.reason, item.key) == ("acme/notes", "scorecard_signal", "a" * 40)
    assert [(e.event.pr, e.matched) for e in item.events] == [(7, ("T-17",))]


def test_an_event_that_matches_nothing_is_kept_but_opens_nothing():
    store = MemoryStore()
    registered(store, "acme/notes")
    assert World().run(FACTORY, 1, event(signals=(T5,))).collect(store) == {"stored": 1, "skipped": 0}
    assert store.rereview_items() == []


def test_a_configured_set_replaces_the_default():
    store = MemoryStore()
    registered(store, "acme/notes")
    World().run(FACTORY, 1, event()).collect(store, signals=parse_signals("T-19"))
    assert store.rereview_items() == []


def test_a_later_match_joins_the_open_item_and_after_it_closes_a_new_one_opens():
    store = MemoryStore()
    admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=[], roles=["admin"], now=T0,
                                max_age=dt.timedelta(hours=8))
    registered(store, "acme/notes")
    world = World().run(FACTORY, 1, event(head="a"))
    world.collect(store)
    world.run(FACTORY, 2, event(head="d", pr=8)).collect(store)
    [item] = store.rereview_items()
    assert [e.event.head_sha[0] for e in item.events] == ["a", "d"]
    store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Reviewed both changes with the owner.", now=T0, events_seen=2)
    world.run(FACTORY, 3, event(head="e", pr=9)).collect(store)
    [again] = store.rereview_items()
    assert again.id != item.id and again.key == "e" * 40


def test_each_run_is_downloaded_once():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event()).collect(store)
    downloads = len(world.storage.calls)
    assert world.collect(store) == {"stored": 0, "skipped": 0} and len(world.storage.calls) == downloads
    assert not world.gh.called("GET", f"/repos/{FACTORY}/actions/runs/1/artifacts")[1:]


def test_what_fails_is_counted_by_outcome_and_never_stored():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, {**event(), "level": "high"})                   # invalid
    world.run(FACTORY, 2, None)                                           # no re-review-event.json
    world.run(FACTORY, 3, event(head="d"), size=17 * 1024 * 1024)         # listed too large: never downloaded
    world.run(FACTORY, 4, event(head="e"), expired=True)
    world.run(FACTORY, 5, event(head="f"), artifact=False)
    world.run(FACTORY, 6, event(repo="acme/unknown", head="9"))           # not registered
    assert world.collect(store) == {"stored": 0, "skipped": 6}
    assert store.rereview_run_counts() == {"invalid": 1, "no_event": 1, "too_large": 1, "expired": 1, "no_artifact": 1}  # not_registered: counted
    assert store.rereview_status()["events"].counts["not_registered"] == 1                                               # only, read again later
    assert not world.gh.called("GET", f"/repos/{FACTORY}/actions/artifacts/30/zip")
    assert store.rereview_items() == [] and store.rereview_status()["events"].counts["invalid"] == 1


def test_per_app_runs_are_read_for_apps_not_marked_central_and_must_name_their_own_repo():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes", "acme/tools")   # notes is central, tools per-app
    world.no_runs(FACTORY, "scorecard-assess.yml")
    world.run("acme/tools", 1, event(repo="acme/tools"), workflow="scorecard.yml")
    world.run("acme/tools", 2, event(repo="acme/notes", head="d"), workflow="scorecard.yml")
    assert world.collect(store) == {"stored": 1, "skipped": 1}
    assert store.rereview_run_counts() == {"event": 1, "repo_mismatch": 1}
    assert not world.gh.called("GET", "/repos/acme/notes/actions/workflows/scorecard.yml/runs")


def test_an_app_the_read_app_can_t_list_is_counted_not_failed():
    store, world = MemoryStore(), World()
    registered(store, "acme/tools", central=())
    world.no_runs(FACTORY, "scorecard-assess.yml", status=404).no_runs("acme/tools", status=403)
    assert world.collect(store) == {"stored": 0, "skipped": 0}
    assert store.rereview_status()["events"].counts == {"unreadable_repos": 1} and store.rereview_status()["events"].problem is None


def test_without_actions_read_nothing_is_asked_and_the_page_will_say_so():
    store, world = MemoryStore(), World(permissions=READ_PERMISSIONS)
    registered(store, "acme/notes")
    assert world.collect(store, readable=False) == {"stored": 0, "skipped": 0, "problem": "actions_unreadable"}
    assert world.gh.calls == [] and store.rereview_status()["events"].problem == "actions_unreadable"
    assert World(permissions=READ_PERMISSIONS).collect(store)["problem"] == "actions_unreadable"   # a 403 on the factory says the same


def test_a_download_that_fails_for_now_is_retried_next_pass():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event())
    zip_route = ("GET", "api.github.com", f"/repos/{FACTORY}/actions/artifacts/10/zip")
    good, world.gh.routes[zip_route] = world.gh.routes[zip_route], [(502, {"message": "busy"})]
    assert world.collect(store)["transient"] is True and store.rereview_runs_seen(FACTORY, [1]) == set()
    world.gh.routes[zip_route] = good
    assert world.collect(store) == {"stored": 1, "skipped": 0}


# ---- more cases ----

def walked(world, repo=FACTORY, workflow="scorecard-assess.yml"):
    return world.gh.called("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs")


def test_runs_are_listed_whole_and_judged_here_because_a_filtered_list_stops_at_1000():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event()).collect(store)
    assert walked(world)[0]["query"] == {"per_page": "100", "page": "1"}
    assert not [c for c in world.gh.calls if "/compare/" in c["path"]]   # the head is the branch's commit: nothing to compare


@pytest.mark.parametrize("changes", [{"path": ".github/workflows/other.yml"}, {"head_branch": "feature"},
                                     {"head_repository": {"full_name": "someone/scorecard-factory"}}, {"event": "push"},
                                     {"display_title": "acme/notes 7 " + "d" * 40}, {"display_title": "acme/tools 7 " + "a" * 40}])
def test_a_central_run_that_is_not_the_factory_s_own_assessment_of_that_head_is_untrusted(changes):
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    assert world.run(FACTORY, 1, event(), **changes).collect(store) == {"stored": 0, "skipped": 1}
    assert store.rereview_run_counts() == {"untrusted_run": 1} and store.rereview_event("acme/notes", "a" * 40) is None


@pytest.mark.parametrize("changes", [{"event": "push"}, {"head_repository": {"full_name": "fork/tools"}}, {"head_sha": "d" * 40},
                                     {"referenced_workflows": []},
                                     {"referenced_workflows": [{"path": "evil/scorecard-factory/.github/workflows/scorecard.yml@main",
                                                                "ref": "refs/heads/main"}]},
                                     {"referenced_workflows": [{"path": "acme/scorecard-factory/.github/workflows/scorecard.yml@dev",
                                                                "ref": "refs/heads/dev"}]}])
def test_an_app_run_that_is_not_its_pull_request_calling_the_factory_s_workflow_is_untrusted(changes):
    store, world = MemoryStore(), World()
    registered(store, "acme/tools", central=())
    world.no_runs(FACTORY, "scorecard-assess.yml")
    assert world.run("acme/tools", 1, event(repo="acme/tools"), workflow="scorecard.yml", **changes).collect(store) == {"stored": 0, "skipped": 1}
    assert store.rereview_run_counts() == {"untrusted_run": 1}


def test_the_walk_goes_down_to_the_watermark_however_far_and_then_only_to_it():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    for run_id in range(1, 251):   # three pages, no limit on how far
        world.runs.setdefault((FACTORY, "scorecard-assess.yml"), []).append(trusted(FACTORY, run_id, None, "scorecard-assess.yml"))
    runs = sorted(world.runs[(FACTORY, "scorecard-assess.yml")], key=lambda r: -r["id"])
    route = ("GET", "api.github.com", f"/repos/{FACTORY}/actions/workflows/scorecard-assess.yml/runs")
    world.gh.routes[route] = [(200, {"workflow_runs": runs[i:i + 100]}) for i in (0, 100, 200)]
    for run_id in range(1, 251):
        world.gh.on("GET", f"/repos/{FACTORY}/actions/runs/{run_id}/artifacts", (200, {"artifacts": []}))
    world.collect(store)
    assert store.rereview_watermark(FACTORY) == 250 and store.rereview_run_counts() == {"no_artifact": 250}
    world.gh.routes[route] = [(200, {"workflow_runs": [trusted(FACTORY, 251, None, "scorecard-assess.yml")] + runs[:99]})]
    world.gh.on("GET", f"/repos/{FACTORY}/actions/runs/251/artifacts", (200, {"artifacts": []}))
    before = len(walked(world))
    world.collect(store)
    assert len(walked(world)) == before + 1 and store.rereview_watermark(FACTORY) == 251


def test_a_run_still_going_holds_the_watermark_below_it_so_it_is_read_when_it_finishes():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(head="d", pr=8)).run(FACTORY, 2, event(), status="in_progress", conclusion=None).run(FACTORY, 3, event(head="e", pr=9))
    world.collect(store)
    assert store.rereview_watermark(FACTORY) == 1 and store.rereview_run_counts() == {"event": 2}
    for r in world.runs[(FACTORY, "scorecard-assess.yml")]:
        if r["id"] == 2:
            r.update(status="completed", conclusion="success")
    world.listing(FACTORY, "scorecard-assess.yml")
    world.collect(store)
    assert store.rereview_watermark(FACTORY) == 3 and store.rereview_run_counts() == {"event": 3}


def test_a_transient_walk_does_not_move_the_watermark():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event())
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/artifacts/10/zip")] = [(502, {"message": "busy"})]
    world.collect(store)
    assert store.rereview_watermark(FACTORY) is None


def test_an_event_for_an_app_not_registered_yet_waits_without_holding_the_watermark_and_is_never_downloaded_again():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(repo="acme/later", head="d")).run(FACTORY, 2, event())
    assert world.collect(store) == {"stored": 1, "skipped": 1}
    assert store.rereview_runs_seen(FACTORY, [1, 2]) == {2} and store.rereview_watermark(FACTORY) == 2
    assert [p.run_id for p in store.pending_events()] == [1]
    downloads = len(world.storage.calls)
    registered(store, "acme/notes", "acme/later", central=("acme/notes", "acme/later"))
    assert world.collect(store)["stored"] == 1 and len(world.storage.calls) == downloads
    assert store.pending_events() == [] and store.rereview_runs_seen(FACTORY, [1]) == {1}
    assert store.rereview_event("acme/later", "d" * 40) is not None


def test_one_bad_event_is_recorded_invalid_and_the_pass_goes_on(monkeypatch):
    """whatever the store refuses of a valid-looking event (ValueError, a database DataError) is that run's final
    outcome; only an operational error stops the pass."""
    import psycopg
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(head="d", pr=8)).run(FACTORY, 2, event())
    real = store.add_rereview_event

    def refuse_d(*, doc, **kw):
        if doc["head_sha"] == "d" * 40:
            raise psycopg.errors.UntranslatableCharacter("unsupported Unicode escape sequence")
        return real(doc=doc, **kw)

    monkeypatch.setattr(store, "add_rereview_event", refuse_d)
    assert world.collect(store) == {"stored": 1, "skipped": 1}
    assert store.rereview_run_counts() == {"invalid": 1, "event": 1}


def test_a_pass_interrupted_after_the_event_was_stored_files_it_on_the_next(monkeypatch):
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event())
    real = store.open_rereview_item
    monkeypatch.setattr(store, "open_rereview_item", lambda **kw: (_ for _ in ()).throw(RuntimeError("the process stopped")))
    with pytest.raises(RuntimeError):
        world.collect(store)
    assert store.rereview_event("acme/notes", "a" * 40) is not None and store.rereview_items() == [] and store.rereview_runs_seen(FACTORY, [1]) == set()
    monkeypatch.setattr(store, "open_rereview_item", real)
    assert world.collect(store) == {"stored": 1, "skipped": 0}
    [item] = store.rereview_items()
    assert item.key == "a" * 40 and store.rereview_run_counts() == {"event": 1}


def test_an_item_closed_while_an_event_is_filed_gets_a_new_one():
    from portal.collectors.events import _file
    store = MemoryStore()
    admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=[], roles=["admin"], now=T0,
                                max_age=dt.timedelta(hours=8))
    for head, run_id in (("a", 1), ("d", 2)):
        store.add_rereview_event(doc=event(head=head), run_repo=FACTORY, run_id=run_id, now=T0)
    item = store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",)))
    real = store.rereview_items
    calls = []

    def stale_then_real(**kw):
        calls.append(1)
        found = real(**kw)
        if len(calls) == 1:   # the collector read the item open; an admin closes it before the event is added
            store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Reviewed with the owner.", now=T0, events_seen=1)
        return found

    store.rereview_items = stale_then_real
    _file(store, "acme/notes", "d" * 40, ("T-17",), T0)
    store.rereview_items = real
    assert [i.key for i in store.rereview_items()] == ["d" * 40]


def test_without_a_token_yet_the_permission_is_unknown_and_the_calls_say():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event())
    assert world.collect(store, readable=None) == {"stored": 1, "skipped": 0}


# ---- a failed run is re-checked until a later attempt succeeds or its artifacts are gone ----

def rerun(world, repo, run_id, workflow="scorecard-assess.yml", **changes):
    """What GET /actions/runs/{id} says now: the run's latest attempt."""
    run = next(r for r in world.runs[(repo, workflow)] if r["id"] == run_id)
    run.update(changes)
    world.gh.routes[("GET", "api.github.com", f"/repos/{repo}/actions/runs/{run_id}")] = [(200, dict(run))]


def test_a_failed_run_re_run_to_success_after_the_watermark_passed_it_is_read():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(head="d", pr=8), conclusion="failure").run(FACTORY, 2, event())
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/1/artifacts")] = [(200, {"artifacts": []})]
    world.collect(store)
    assert store.rereview_watermark(FACTORY) == 2 and [f.run_id for f in store.failed_runs(FACTORY)] == [1]
    rerun(world, FACTORY, 1, conclusion="failure", run_attempt=1)
    world.collect(store)   # still failed: kept
    assert [f.run_id for f in store.failed_runs(FACTORY)] == [1] and store.rereview_event("acme/notes", "d" * 40) is None
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/1/artifacts")] = [
        (200, {"artifacts": [{"id": 10, "name": "scorecard", "size_in_bytes": 100, "expired": False}]})]
    rerun(world, FACTORY, 1, conclusion="success", run_attempt=2, updated_at="2026-10-23T11:30:00Z")
    assert world.collect(store)["stored"] == 1
    assert store.failed_runs(FACTORY) == [] and store.rereview_event("acme/notes", "d" * 40) is not None
    assert store.rereview_runs_seen(FACTORY, [1]) == {1}


def test_a_failed_run_older_than_its_artifacts_retention_is_dropped():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(), conclusion="failure", updated_at="2026-07-01T00:00:00Z")   # 114 days before T0
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/1/artifacts")] = [(200, {"artifacts": []})]  # 90 days
    world.collect(store)
    assert [f.run_id for f in store.failed_runs(FACTORY)] == [1]   # kept, then re-checked
    rerun(world, FACTORY, 1)
    world.collect(store)
    assert store.failed_runs(FACTORY) == []


def test_the_retention_comes_from_the_run_s_artifacts_when_it_can():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(), conclusion="failure", updated_at="2026-10-01T00:00:00Z")   # 22 days before T0
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/1/artifacts")] = [(200, {"artifacts": [
        {"id": 10, "name": "logs", "created_at": "2026-10-01T00:00:00Z", "expires_at": "2026-10-15T00:00:00Z", "expired": True}]})]
    world.collect(store)
    [failed] = store.failed_runs(FACTORY)
    assert failed.retention_days == 14
    rerun(world, FACTORY, 1)
    world.collect(store)
    assert store.failed_runs(FACTORY) == []   # 22 days > 14


# ---- more cases ----

@pytest.mark.parametrize("status", ["ahead", "diverged"])
def test_a_dispatch_from_a_tag_named_main_whose_head_is_not_on_main_is_untrusted(status):
    """C1: GitHub reports head_branch "main" for a run dispatched from a tag named main; the head must be the branch's, or behind it."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/compare/{'c' * 40}...{'9' * 40}")] = [(200, {"status": status})]
    world.run(FACTORY, 1, event(), head_sha="9" * 40)
    assert world.collect(store) == {"stored": 0, "skipped": 1} and store.rereview_run_counts() == {"untrusted_run": 1}


def test_a_per_app_event_never_hides_the_central_one_of_the_same_head():
    """I1: an app's own run is trusted less (its author could overwrite the artifact); the central event is kept beside it."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes", central=())
    world.run("acme/notes", 1, event(), workflow="scorecard.yml").run(FACTORY, 2, event())
    assert world.collect(store)["stored"] == 2
    assert store.rereview_event("acme/notes", "a" * 40, "per-app").run_id == 1 and store.rereview_event("acme/notes", "a" * 40).run_id == 2
    [item] = store.rereview_items()
    assert sorted(e.event.source for e in item.events) == ["central", "per-app"]


def test_a_run_going_for_more_than_a_day_is_re_checked_instead_of_holding_the_watermark():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(), status="in_progress", conclusion=None, run_started_at="2026-10-21T00:00:00Z").run(FACTORY, 2, event(head="d", pr=8))
    world.collect(store)
    assert store.rereview_watermark(FACTORY) == 2 and [f.run_id for f in store.failed_runs(FACTORY)] == [1]
    rerun(world, FACTORY, 1, status="completed", conclusion="success")
    assert world.collect(store)["stored"] == 1 and store.failed_runs(FACTORY) == []


@pytest.mark.parametrize("answer, problem", [((403, {"message": "no"}), "factory_unreadable"), ((200, {"default_branch": "develop"}), "factory_not_main")])
def test_a_factory_the_collector_can_t_use_for_central_runs_is_a_source_problem(answer, problem):
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}")] = [answer]
    assert world.collect(store)["problem"] == problem and store.rereview_status()["events"].problem == problem


def test_an_interrupted_pass_does_not_file_again_an_event_an_item_already_lists():
    """M6: the pass stopped after filing (and an admin closed the item since): the next pass records the run, nothing more."""
    store, world = MemoryStore(), World()
    admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=[], roles=["admin"], now=T0,
                                max_age=dt.timedelta(hours=8))
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event())
    store.add_rereview_event(doc=event(), run_repo=FACTORY, run_id=1, now=T0)
    item = store.open_rereview_item(repo="acme/notes", reason="scorecard_signal", key="a" * 40, now=T0, event=("a" * 40, ("T-17",)))
    store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Reviewed with the owner.", now=T0, events_seen=1)
    world.collect(store)
    assert store.rereview_items() == [] and store.rereview_run_counts() == {"event": 1}


# ---- final review ----

def test_a_head_behind_the_branch_is_compared_by_the_branch_s_commit_never_by_the_name_main():
    """C1: `main` may name a tag too; the branch is resolved to its commit by ref, and only that commit is compared."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(), head_sha="7" * 40)   # an older commit of the branch
    assert world.collect(store)["stored"] == 1
    [compare] = [c for c in world.gh.calls if "/compare/" in c["path"]]
    assert compare["path"] == f"/repos/{FACTORY}/compare/{'c' * 40}...{'7' * 40}" and compare["query"] == {"per_page": "1"}
    assert world.gh.called("GET", f"/repos/{FACTORY}/git/ref/heads/main")


@pytest.mark.parametrize("title", ["acme/notes 7", "anything at all", "acme/notes seven " + "a" * 40, ""])
def test_a_central_run_whose_title_is_not_repo_pr_head_is_untrusted_before_anything_is_asked(title):
    """Runs from before the workflow's run-name (or anyone else's) are recorded untrusted_run without a compare or a download."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(), display_title=title)
    assert world.collect(store) == {"stored": 0, "skipped": 1} and store.rereview_run_counts() == {"untrusted_run": 1}
    assert not [c for c in world.gh.calls if "/compare/" in c["path"] or "/artifacts" in c["path"]] and world.storage.calls == []


def test_a_download_redirected_to_a_host_not_allowed_records_nothing_until_the_hosts_are_fixed():
    """I1: the run and the watermark wait (and the page says why), so fixing PORTAL_ARTIFACT_HOSTS or the egress reads it."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event())
    result = collect_events(world.client, store, org="acme", signals=DEFAULT_SIGNALS, now=T0, actions_readable=True,
                            artifact_hosts=("*.blob.core.windows.net",))
    assert result["problem"] == "artifact_host_refused" and result.get("transient") is True
    assert store.rereview_runs_seen(FACTORY, [1]) == set() and store.rereview_watermark(FACTORY) is None
    assert store.rereview_status()["events"].problem == "artifact_host_refused"
    assert world.collect(store)["stored"] == 1 and store.rereview_status()["events"].problem is None


def at(world, store, when, **kw):
    return collect_events(world.client, store, org="acme", signals=DEFAULT_SIGNALS, now=when, actions_readable=True, artifact_hosts=HOSTS, **kw)


def test_a_failed_run_is_dropped_once_github_would_no_longer_re_run_it():
    """I2: GitHub re-runs a run only within 30 days of its creation, so a failure is dropped then, or when its artifacts go."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(), conclusion="failure", created_at="2026-09-20T00:00:00Z", updated_at="2026-10-23T11:00:00Z")
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/1/artifacts")] = [(200, {"artifacts": []})]
    world.collect(store)
    rerun(world, FACTORY, 1)
    world.collect(store)
    assert store.failed_runs(FACTORY) == []   # created 33 days before T0


def test_after_its_first_day_a_failed_run_is_checked_at_most_hourly():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(), conclusion="failure")
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/1/artifacts")] = [(200, {"artifacts": []})]
    world.collect(store)
    rerun(world, FACTORY, 1)
    day = T0 + dt.timedelta(days=2)
    at(world, store, day)
    checks = len(world.gh.called("GET", f"/repos/{FACTORY}/actions/runs/1"))
    at(world, store, day + dt.timedelta(minutes=30))
    assert len(world.gh.called("GET", f"/repos/{FACTORY}/actions/runs/1")) == checks
    at(world, store, day + dt.timedelta(hours=2))
    assert len(world.gh.called("GET", f"/repos/{FACTORY}/actions/runs/1")) == checks + 1


def test_a_cancelled_central_run_is_not_kept_the_poller_dispatches_anew():
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(), conclusion="cancelled")
    world.collect(store)
    assert store.failed_runs(FACTORY) == [] and store.rereview_watermark(FACTORY) == 1


def test_a_busy_walk_is_said_and_does_not_count_as_a_good_pass():
    """M1."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    store.set_rereview_status(source="events", problem=None, counts={}, now=T0 - dt.timedelta(hours=1))
    world.run(FACTORY, 1, event())
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/artifacts/10/zip")] = [(502, {"message": "busy"})]
    world.collect(store)
    status = store.rereview_status()["events"]
    assert status.problem == "github_busy" and status.last_ok_at == T0 - dt.timedelta(hours=1)


def test_a_run_github_never_answers_for_is_recorded_unreadable_after_twelve_passes_and_older_runs_are_read():
    """M2: a liveness bound on one run, not a budget: the walk goes on to the runs below it."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(head="d", pr=8)).run(FACTORY, 2, event())
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/artifacts/20/zip")] = [(502, {"message": "busy"})]
    for _ in range(11):
        world.collect(store)
    assert store.rereview_runs_seen(FACTORY, [1, 2]) == set()
    world.collect(store)
    assert store.rereview_run_counts() == {"unreadable": 1, "event": 1} and store.stuck_runs(FACTORY) == {}
    assert store.rereview_status()["events"].counts["unreadable_after_retries"] == 1


def test_a_re_checked_run_whose_app_is_not_registered_yet_moves_to_the_waiting_events():
    """M3: the event waits there; the failed row is dropped."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")
    world.run(FACTORY, 1, event(repo="acme/later", head="d"), conclusion="failure")
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/1/artifacts")] = [(200, {"artifacts": []})]
    world.collect(store)
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/1/artifacts")] = [
        (200, {"artifacts": [{"id": 10, "name": "scorecard", "size_in_bytes": 100, "expired": False}]})]
    rerun(world, FACTORY, 1, conclusion="success", run_attempt=2)
    world.collect(store)
    assert store.failed_runs(FACTORY) == [] and [p.run_id for p in store.pending_events()] == [1]


def test_the_failed_runs_of_a_repo_no_longer_walked_are_dropped():
    """M4: an app no longer per-app (or no longer registered) is no longer a source."""
    store, world = MemoryStore(), World()
    registered(store, "acme/notes")   # central: acme/notes is not walked as a per-app source
    store.record_failed_run(repo="acme/gone", run_id=4, attempt=1, last_attempt_at=T0, retention_days=90, now=T0)
    store.record_failed_run(repo=FACTORY, run_id=3, attempt=1, last_attempt_at=T0, retention_days=90, now=T0)
    world.gh.routes[("GET", "api.github.com", f"/repos/{FACTORY}/actions/runs/3")] = [(200, {"id": 3, "status": "in_progress"})]
    world.no_runs(FACTORY, "scorecard-assess.yml")
    world.collect(store)
    assert [(f.repo, f.run_id) for f in store.failed_runs()] == [(FACTORY, 3)]


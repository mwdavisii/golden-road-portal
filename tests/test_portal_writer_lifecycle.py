"""The writer's lifecycle requests end to end against RepoWorld (spec 2026-11-02): suspend and resume PRs merged by the writer inside
L5, and a decommission's deploy and factory PRs, which wait for a person."""

import httpx
import pytest
import yaml
from portal_fakes import INTAKE_V2, LIFECYCLE_REASON, T0, RepoWorld, adapter_yaml, deploy_world, lifecycle_app, lifecycle_request, transport, writer_config

from portal.github.client import GitHub
from portal.writer import lifecycle as writer_lifecycle
from portal.writer.lifecycle import Planned
from portal.writer.run import run_once


def client_for(world):
    return GitHub("https://api.github.com", httpx.Client(transport=transport(world)), lambda: "ghs_" + "w" * 36)


def passes(portal, world, n, cfg=None):
    done = []
    for _ in range(n):
        done += run_once(portal.store, client_for(world), cfg or writer_config(), T0)
    return done


def chain_of(world, step):
    """The commits of the step's PR, oldest first."""
    shas, sha = [], step.head_sha
    while sha != step.base_sha:
        shas.insert(0, sha)
        sha = world.commits[sha]["parent"]
    return shas


def deployment(composed):
    return next(o for o in yaml.safe_load_all(composed) if o and o["kind"] == "Deployment")


def steps(portal, req):
    return portal.store.lifecycle_steps(req.id)


def events(portal, req):
    return [e.step for e in portal.store.lifecycle_events(req.id)]


def test_a_suspend_opens_one_pr_for_every_environment_and_merges_itself(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    [step] = steps(portal, req)
    assert (step.state, step.branch, step.pr_number) == ("pr_open", f"portal/lifecycle-{req.id}-deploy-1", 1)
    assert [c["files"] for c in (world.commits[s] for s in chain_of(world, step))] == [
        [{"filename": "apps/acme/notes/test/lifecycle.yaml", "status": "added"}], [{"filename": "apps/acme/notes/test/composed.yaml", "status": "modified"}],
        [{"filename": "apps/acme/notes/prod/lifecycle.yaml", "status": "added"}], [{"filename": "apps/acme/notes/prod/composed.yaml", "status": "modified"}]]
    passes(portal, world, 1)
    assert steps(portal, req)[0].state == "done" and world.pull(deploy, 1)["merged"]
    for env in ("test", "prod"):
        assert deployment(world.files(deploy)[f"apps/acme/notes/{env}/composed.yaml"])["spec"]["replicas"] == 0
        life = yaml.safe_load(world.files(deploy)[f"apps/acme/notes/{env}/lifecycle.yaml"])
        assert (life["request"], life["task"], life["environment"], life["requested_at"]) == (req.id, "SCTASK0010001", env, "2026-10-23T12:00:00Z")
    state = portal.store.lifecycle_state("acme/notes")
    assert (state.state, state.task, state.request) == ("suspended", "SCTASK0010001", req.id)
    assert portal.store.get_request(req.id).state == "writing", "the state is set when the deploy step is done; the request completes next"
    passes(portal, world, 1)
    after = portal.store.get_request(req.id)
    assert (after.state, after.merge_sha) == ("merged", world.pull(deploy, 1)["merge_commit_sha"])
    assert events(portal, req) == ["requested", "pr_opened", "merged", "completed"]
    assert "scales to zero" in portal.store.lifecycle_events(req.id)[2].note
    assert "SCTASK0010001" in portal.store.lifecycle_events(req.id)[3].note
    assert passes(portal, world, 1) == [], "nothing more to do"


def test_a_resume_deletes_lifecycle_yaml_and_composed_returns_to_what_was_reviewed(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    reviewed = world.files(deploy)
    suspend = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 3)
    assert portal.store.get_request(suspend.id).state == "merged"
    resume = lifecycle_request(portal.store, portal.people, T0, action="resume", task="SCTASK0010002")
    passes(portal, world, 3)
    after = world.files(deploy)
    assert portal.store.get_request(resume.id).state == "merged"
    assert after == reviewed, "lifecycle.yaml gone and each composed.yaml byte for byte what was reviewed"
    [step] = steps(portal, resume)
    assert [c["files"][0]["status"] for c in (world.commits[s] for s in chain_of(world, step))] == ["removed", "modified", "removed", "modified"]
    state = portal.store.lifecycle_state("acme/notes")
    assert (state.state, state.task, state.request) == ("active", None, resume.id)
    assert events(portal, resume) == ["requested", "pr_opened", "merged", "completed"]
    assert "brings the app back" in portal.store.lifecycle_events(resume.id)[2].note


def test_a_resume_of_an_environment_pinned_after_the_suspend_leaves_it_running(portal):
    """(decided while planning: item 19) only the environments with a lifecycle.yaml are resumed."""
    world = RepoWorld()
    deploy, _ = deploy_world(world, environments=("test",))
    lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 3)
    both = RepoWorld()
    deploy_world(both)
    for path, raw in both.files(deploy).items():  # bump pins prod after the suspend: it runs
        if "/prod/" in path or path.endswith("-prod.yaml"):
            world.advance_main(deploy, path, raw)
    resume = lifecycle_request(portal.store, portal.people, T0, action="resume", task="SCTASK0010002")
    passes(portal, world, 3)
    assert portal.store.get_request(resume.id).state == "merged"
    [step] = steps(portal, resume)
    assert {f["filename"] for s in chain_of(world, step) for f in world.commits[s]["files"]} == {
        "apps/acme/notes/test/lifecycle.yaml", "apps/acme/notes/test/composed.yaml"}


def test_a_suspend_with_no_environment_is_refused_and_noted(portal):
    """(decided while planning: item 3) at least one environment in the deploy repo."""
    world = RepoWorld()
    deploy, _ = deploy_world(world, environments=())
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "recheck: nothing_to_suspend") and steps(portal, req) == []
    last = portal.store.lifecycle_events(req.id)[-1]
    assert last.step == "failed" and "Nothing was applied" in last.note and world.branches(deploy) == set()


def test_a_resume_of_an_app_the_portal_has_not_suspended_is_refused(portal):
    world = RepoWorld()
    deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0, action="resume", task="SCTASK0010002")
    passes(portal, world, 1)
    assert portal.store.get_request(req.id).failure_reason == "recheck: not_suspended"


def test_a_requester_who_is_no_longer_an_admin_is_refused(portal):
    world = RepoWorld()
    deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    portal.store.sign_in(issuer="https://sso.example.test/application/o/portal/", subject="sub-admin", display_name="Admin",
                         email="admin@example.test", groups=[], roles=(), now=T0, max_age=portal.settings.session_max)
    passes(portal, world, 1)
    assert portal.store.get_request(req.id).failure_reason == "recheck: not_admin" and world.calls == []


def test_a_suspend_of_an_environment_already_suspended_in_git_fails_the_step(portal):
    from test_portal_writer_l5 import suspended
    world = RepoWorld()
    deploy, _ = deploy_world(world, lifecycle={"prod": suspended("prod")})
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    [step] = steps(portal, req)
    assert (step.state, step.failure_reason) == ("failed", "already_suspended") and world.branches(deploy) == set()
    assert portal.store.get_request(req.id).state == "writing", "a failed step never fails the request: an admin retries or abandons"
    assert events(portal, req) == ["requested", "failed"]
    assert passes(portal, world, 1) == [f"{req.id}: deploy failed; waiting for Retry or Abandon"]


def test_the_pr_body_says_who_why_and_who_merges(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    pull = world.pull(deploy, 1)
    assert pull["title"] == f"Suspend acme/notes (ServiceNow SCTASK0010001, portal request {req.id})"
    body = pull["body"]
    assert "`Admin`" in body and f"```text\n{LIFECYCLE_REASON}\n```" in body
    for env in ("test", "prod"):
        assert f"- `apps/acme/notes/{env}/lifecycle.yaml`" in body and f"- `apps/acme/notes/{env}/composed.yaml`" in body
    assert ("The writer merges it once deploy-check passes, and only if it changes nothing but lifecycle.yaml and the Deployment's "
            "replicas (spec L5).") in body
    note = portal.store.lifecycle_events(req.id)[1]
    assert note.step == "pr_opened" and pull["html_url"] in note.note


def test_a_failed_deploy_check_fails_the_step_and_closes_the_pr(portal):
    """(decided while planning: item 7) the reason is the check's name; the note links the PR, where its summary is."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = "failure"
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 2)
    [step] = steps(portal, req)
    assert (step.state, step.failure_reason) == ("failed", "check_failed: deploy-check")
    pull = world.pull(deploy, 1)
    assert pull["state"] == "closed" and not pull["merged"]
    assert world.files(deploy) == world.files(deploy, pull["base_sha"]), "main is untouched"
    last = portal.store.lifecycle_events(req.id)[-1]
    assert last.step == "failed" and pull["html_url"] in last.note and "check_failed: deploy-check" in last.note
    assert any("check_failed: deploy-check" in c for _, n, c in world.comments if n == 1)


def test_checks_still_running_wait_and_time_out(portal):
    import datetime as dt
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    assert passes(portal, world, 1) == [f"{req.id}: deploy waiting for checks"]
    run_once(portal.store, client_for(world), writer_config(), T0 + dt.timedelta(hours=1))
    assert steps(portal, req)[0].failure_reason == "checks_timeout" and not world.pull(deploy, 1)["merged"]


def test_a_commit_pushed_to_the_branch_is_never_merged(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    [step] = steps(portal, req)
    world.push(deploy, step.branch, "apps/acme/notes/prod/bindings.yaml", b"schema: binding/v2\n")
    passes(portal, world, 1)
    [step] = steps(portal, req)
    assert (step.state, step.failure_reason) == ("failed", "pr_head_changed")
    assert world.pull(deploy, 1)["state"] == "closed" and not world.pull(deploy, 1)["merged"]


def test_a_pr_a_person_closed_fails_the_step(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    world.pull(deploy, 1)["state"] = "closed"
    passes(portal, world, 1)
    assert steps(portal, req)[0].failure_reason == "pr_closed"


def test_an_l5_refusal_lists_the_files_it_found(portal, monkeypatch):
    """A crafted PR the writer itself wrote (as a bug in the planner would): its chain and files match the plan, and L5 still refuses it."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    real = writer_lifecycle._plan

    def plan(gh, cfg, req, step, base_sha, store):
        return real(gh, cfg, req, step, base_sha, store) + [Planned("apps/acme/notes/prod/bindings.yaml", b"schema: binding/v2\n", None)]
    monkeypatch.setattr(writer_lifecycle, "_plan", plan)
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 2)
    [step] = steps(portal, req)
    assert (step.state, step.failure_reason) == ("failed", "l5_refused: apps/acme/notes/prod/bindings.yaml (added) is not a suspend's file")
    assert not world.pull(deploy, 1)["merged"] and world.pull(deploy, 1)["state"] == "closed"


def test_an_interrupted_pass_resumes_on_the_branch(portal):
    """The pass stops after the second commit (the third contents-API write raises once); the next pass picks up the branch."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    handle, writes = world.handle, []

    def crashing(request):
        if request.method in ("PUT", "DELETE") and "/contents/" in request.url.path:
            writes.append(request.url.path)
            if len(writes) == 3:
                raise RuntimeError("the writer's pod stopped")
        return handle(request)
    world.handle = crashing
    with pytest.raises(RuntimeError):
        passes(portal, world, 1)
    assert steps(portal, req)[0].state == "planned" and world.pulls == {}
    passes(portal, world, 1)
    [step] = steps(portal, req)
    assert step.state == "pr_open" and len(chain_of(world, step)) == 4 and list(world.pulls) == [(deploy, 1)]
    assert len(writes) == 5, "two written before the stop, the failed third, then only the last two"
    passes(portal, world, 2)
    assert portal.store.get_request(req.id).state == "merged"


def test_a_branch_holding_someone_else_s_commit_fails_the_step(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    world.repos[deploy][f"portal/lifecycle-{req.id}-deploy-1"] = world.head(deploy)
    world.push(deploy, f"portal/lifecycle-{req.id}-deploy-1", "README.md", b"not the writer's\n")
    passes(portal, world, 1)
    assert steps(portal, req)[0].failure_reason == "branch_not_ours" and world.pulls == {}


def test_the_writer_s_calls_stay_in_the_deploy_repo_for_a_suspend(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 3)
    assert {c["path"].split("/")[3] for c in world.calls} == {"golden-road-deploy"}
    assert not [c for c in world.calls if c["method"] == "PATCH" and c["path"] == f"/repos/{deploy}"], "a suspend never archives"
    writes = {c["body"]["branch"] for c in world.calls if c["method"] in ("PUT", "DELETE") and "/contents/" in c["path"]}
    assert all(b.startswith("portal/lifecycle-") for b in writes), "the writer never writes to main"


# ---- the decommission's deploy and factory PRs (Task 13), with the archive between them (item 21) by the lifecycle App (Task 14) ----

BINDING_TEST = yaml.safe_dump({"schema": "binding/v2", "repo": "acme/notes", "environment": "test", "linked_by": "p_" + "a" * 32,
                               "linked_at": "2026-10-29T15:00:00Z", "request": "r_" + "b" * 32,
                               "azure_identity": {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                                                  "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"}},
                              sort_keys=False).encode()


def dcfg(world):
    """The writer's config with the real lifecycle App talking to world (one per world)."""
    if not hasattr(world, "decommission_cfg"):
        world.decommission_cfg = writer_config(lifecycle_app=lifecycle_app(world))
    return world.decommission_cfg


def decommission_world(**changes):
    from test_portal_writer_l5 import suspended
    world = RepoWorld()
    deploy, factory = deploy_world(world, **{"bindings": {"test": BINDING_TEST}, "lifecycle": {"prod": suspended("prod")}, **changes})
    world.installed["acme/notes"] = 77  # the lifecycle App, on the app's repository
    return world, deploy, factory


APP_FILES = [f"apps/acme/notes/{env}/{name}" for env, names in (
    ("prod", ("base/manifests.yaml", "composed.yaml", "kustomization.yaml", "lifecycle.yaml", "release.yaml")),
    ("test", ("base/manifests.yaml", "bindings.yaml", "composed.yaml", "kustomization.yaml", "release.yaml"))) for name in names]
ADAPTERS = ["clusters/home/flux/acme-notes-test.yaml", "clusters/office/argo/acme-notes-test.yaml",
            "clusters/home/flux/acme-notes-prod.yaml", "clusters/office/argo/acme-notes-prod.yaml"]


def to_the_factory_pr(portal, world, deploy):
    """A decommission's deploy PR opened, merged by a person, the archive recorded, and the factory PR opened."""
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    world.merge(deploy, 1)
    passes(portal, world, 3, dcfg(world))
    return req


def test_a_decommission_deletes_the_app_and_its_adapters_in_one_pr_and_waits_for_a_person(portal):
    world, deploy, factory = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert [s.step for s in steps(portal, req)] == ["deploy", "archive", "factory"], "rule 7's order, item 21"
    deploy_step = steps(portal, req)[0]
    assert (deploy_step.state, deploy_step.branch, list(world.pulls)) == ("pr_open", f"portal/lifecycle-{req.id}-deploy-1", [(deploy, 1)])
    assert world.pull(deploy, 1)["title"] == f"Decommission acme/notes: remove it from the deploy repo (ServiceNow SCTASK0010003, portal request {req.id})"
    assert [c["files"] for c in (world.commits[s] for s in chain_of(world, deploy_step))] == [
        [{"filename": p, "status": "removed"}] for p in APP_FILES + ADAPTERS], "one commit per file: the app's files by path, then the adapters"
    assert sorted(f["filename"] for f in world._diff(deploy, world.pull(deploy, 1))) == sorted(APP_FILES + ADAPTERS)
    assert "A platform reviewer reviews and merges it" in world.pull(deploy, 1)["body"]
    assert passes(portal, world, 3, dcfg(world)) == [f"{req.id}: deploy waiting for review"] * 3
    assert not world.pull(deploy, 1)["merged"] and portal.store.lifecycle_state("acme/notes") is None
    world.merge(deploy, 1)
    passes(portal, world, 1, dcfg(world))
    assert [s.state for s in steps(portal, req)] == ["done", "planned", "planned"]
    state = portal.store.lifecycle_state("acme/notes")
    assert (state.state, state.task, state.request) == ("decommissioned", "SCTASK0010003", req.id)
    assert "removes the workloads" in portal.store.lifecycle_events(req.id)[-1].note
    passes(portal, world, 1, dcfg(world))
    assert [s.state for s in steps(portal, req)] == ["done", "done", "planned"] and events(portal, req)[-1] == "archived"
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[2].state == "pr_open" and list(world.pulls) == [(deploy, 1), (factory, 1)]
    left = world.files(deploy)
    assert not [p for p in left if p.startswith("apps/acme/notes/")] and not set(ADAPTERS) & set(left)
    assert set(left) == {"clusters/home/flux/gitrepository.yaml", "apps/.gitkeep"}


def test_the_factory_pr_adds_only_the_decommissioned_key(portal):
    from portal.contracts import validate
    world, deploy, factory = decommission_world()
    base_record = yaml.safe_load(world.files(factory)["intake/acme/notes.yaml"])
    req = to_the_factory_pr(portal, world, deploy)
    factory_step = steps(portal, req)[2]
    assert factory_step.branch == f"portal/lifecycle-{req.id}-factory-1"
    assert world._diff(factory, world.pull(factory, 1)) == [{"filename": "intake/acme/notes.yaml", "status": "modified"}]
    raw = world.files(factory, factory_step.branch)["intake/acme/notes.yaml"]
    before = world.files(factory)["intake/acme/notes.yaml"]
    assert raw.startswith(before), "byte for byte: the header (the registration's request) and every other key are as they were"
    assert raw[len(before):].startswith(b"decommissioned:\n") and raw.count(b"# Written by the golden road portal") == 1
    record = yaml.safe_load(raw)
    deploy_done = steps(portal, req)[0].updated_at
    assert record == {**base_record, "decommissioned": {"at": deploy_done.strftime("%Y-%m-%dT%H:%M:%SZ"), "task": "SCTASK0010003",
                                                        "by": portal.people["admin"].id, "request": req.id}}
    assert validate("intake.v2", record) == []
    assert "A security code owner of `intake/` reviews and merges it" in world.pull(factory, 1)["body"]
    assert world.pull(factory, 1)["title"].startswith("Decommission acme/notes: mark its intake record decommissioned")
    assert passes(portal, world, 2, dcfg(world)) == [f"{req.id}: factory waiting for review"] * 2
    merge = world.merge(factory, 1)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[2].state == "done" and events(portal, req)[-1] == "factory_merged"
    passes(portal, world, 1, dcfg(world))
    after = portal.store.get_request(req.id)
    assert (after.state, after.merge_sha) == ("merged", merge)
    assert events(portal, req) == ["requested", "pr_opened", "merged", "archived", "pr_opened", "factory_merged", "completed"]
    assert "Kept: the images in ghcr, the Delinea secret values" in portal.store.lifecycle_events(req.id)[-1].note


def test_a_v1_record_refuses_the_decommission_before_any_change(portal):
    """(decided while planning: item 4) decommissioned is an intake/v2 key: change the registration first."""
    v1 = yaml.safe_dump({"schema": "intake/v1", "repo": "acme/notes", "tier": "moderate", "purpose": "Notes."}).encode()
    world, deploy, _ = decommission_world(intake=v1)
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "recheck: record_not_intake_v2") and steps(portal, req) == []
    last = portal.store.lifecycle_events(req.id)[-1]
    assert last.step == "failed" and "Nothing was applied" in last.note
    assert world.branches(deploy) == set() and world.pulls == {} and portal.store.lifecycle_state("acme/notes") is None


def test_a_decommission_with_no_intake_record_is_refused_too(portal):
    world = RepoWorld()
    deploy_world(world)
    world.repos["acme/scorecard-factory"]["main"] = world._new_commit({}, None, [])
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert portal.store.get_request(req.id).failure_reason == "recheck: record_not_intake_v2"


def test_a_platform_repo_is_refused(portal):
    """(decided while planning: item 5) the web refuses it; the writer refuses it again before any GitHub call."""
    world = RepoWorld()
    deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", repo="acme/golden-road-portal", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert portal.store.get_request(req.id).failure_reason == "recheck: platform_repo" and world.calls == []


def test_no_lifecycle_app_refuses_a_decommission_but_not_a_suspend(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1)
    assert portal.store.get_request(req.id).failure_reason == "recheck: no_lifecycle_app" and world.pulls == {}
    suspend = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    assert steps(portal, suspend)[0].state == "pr_open" and list(world.pulls) == [(deploy, 1)]


def test_nothing_in_the_deploy_repo_is_a_done_step_with_a_note(portal):
    """(decided while planning: item 17) registered, never deployed: no deploy PR, then the archive and the factory PR."""
    world = RepoWorld()
    deploy, factory = deploy_world(world, environments=())
    world.installed["acme/notes"] = 77
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    deploy_step = steps(portal, req)[0]
    assert (deploy_step.state, deploy_step.pr_number, deploy_step.merge_sha) == ("done", None, None) and world.branches(deploy) == set()
    assert portal.store.lifecycle_events(req.id)[-1].note == "Nothing of acme/notes was in the deploy repo, so no pull request was needed there."
    assert portal.store.lifecycle_state("acme/notes").state == "decommissioned"
    passes(portal, world, 1, dcfg(world))
    assert events(portal, req)[-1] == "archived" and world.pulls == {}
    passes(portal, world, 1, dcfg(world))
    assert list(world.pulls) == [(factory, 1)]
    world.merge(factory, 1)
    passes(portal, world, 2, dcfg(world))
    after = portal.store.get_request(req.id)
    assert (after.state, after.merge_sha) == ("merged", world.pull(factory, 1)["merge_commit_sha"])


def test_an_adapter_left_without_its_app_directory_is_still_removed(portal):
    """L6: each environment's two adapters by compose's names, whether or not apps/<owner>/<repo>/ is still there."""
    world = RepoWorld()
    deploy, _ = deploy_world(world, environments=())
    world.advance_main(deploy, "clusters/office/argo/acme-notes-prod.yaml", adapter_yaml("clusters/office/argo", "apps/acme/notes/prod", "notes-prod"))
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert world._diff(deploy, world.pull(deploy, 1)) == [{"filename": "clusters/office/argo/acme-notes-prod.yaml", "status": "removed"}]
    assert steps(portal, req)[0].state == "pr_open"


def test_a_symlink_in_the_app_directory_fails_the_deploy_step(portal, monkeypatch):
    world, deploy, _ = decommission_world()
    tree = world._tree

    def with_a_link(sha):
        doc = tree(sha)
        return {**doc, "tree": [{**e, "mode": "120000"} if e["path"] == "release.yaml" else e for e in doc["tree"]]}
    monkeypatch.setattr(world, "_tree", with_a_link)
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[0].failure_reason == "not_a_file" and world.pulls == {}


def test_a_merged_decommission_that_left_a_file_fails_the_step(portal):
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    world.merge(deploy, 1)
    world.advance_main(deploy, "apps/acme/notes/test/release.yaml", b"# back\n")
    world.pull(deploy, 1)["merge_commit_sha"] = world.head(deploy)  # as if the merge itself had kept the file
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[0].failure_reason == "merged_file_differs" and portal.store.lifecycle_state("acme/notes") is None


def test_a_record_that_moved_fails_the_factory_step(portal):
    """The factory's record changes on main after the factory PR opens: the step is rebuilt on the new main (item 2), and the rebuilt
    PR's record carries the new bytes plus the key."""
    from portal.writer.render import intake_yaml
    world, deploy, factory = decommission_world()
    req = to_the_factory_pr(portal, world, deploy)
    moved = {**yaml.safe_load(world.files(factory)["intake/acme/notes.yaml"]), "purpose": "Stores short team notes and their tags; no email."}
    world.advance_main(factory, "intake/acme/notes.yaml", intake_yaml(moved, "r_" + "c" * 32).encode())
    passes(portal, world, 1, dcfg(world))
    factory_step = steps(portal, req)[2]
    assert (factory_step.state, factory_step.attempt) == ("planned", 2)
    assert world.pull(factory, 1)["state"] == "closed" and any("rebuilds this change on the new main" in c for _, _, c in world.comments)
    passes(portal, world, 1, dcfg(world))
    factory_step = steps(portal, req)[2]
    assert (factory_step.state, factory_step.pr_number, factory_step.branch) == ("pr_open", 2, f"portal/lifecycle-{req.id}-factory-2")
    record = yaml.safe_load(world.files(factory, factory_step.branch)["intake/acme/notes.yaml"])
    assert record["purpose"] == moved["purpose"] and record["decommissioned"]["request"] == req.id
    assert "(attempt 2)" in portal.store.lifecycle_events(req.id)[-1].note


def test_a_record_another_request_decommissioned_fails_the_factory_step(portal):
    from portal.writer.render import intake_yaml
    world, deploy, factory = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    world.merge(deploy, 1)
    record = {**yaml.safe_load(world.files(factory)["intake/acme/notes.yaml"]),
              "decommissioned": {"at": "2026-10-01T00:00:00Z", "task": "SCTASK0010009", "by": "p_" + "a" * 32, "request": "r_" + "e" * 32}}
    world.advance_main(factory, "intake/acme/notes.yaml", intake_yaml(record, "r_" + "e" * 32).encode())
    passes(portal, world, 3, dcfg(world))
    assert steps(portal, req)[2].failure_reason == "already_decommissioned" and world.branches(factory) == set()


def test_a_factory_record_this_request_already_marked_needs_no_pr(portal):
    """A record that already carries this request's key (a person marked it by hand, or a rebuilt step finds its own merge): the
    factory step is done with no PR."""
    from portal.writer.render import intake_yaml
    world, deploy, factory = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    world.merge(deploy, 1)
    record = {**yaml.safe_load(world.files(factory)["intake/acme/notes.yaml"]),
              "decommissioned": {"at": "2026-10-23T12:00:00Z", "task": "SCTASK0010003", "by": portal.people["admin"].id, "request": req.id}}
    world.advance_main(factory, "intake/acme/notes.yaml", intake_yaml(record, req.id).encode())
    passes(portal, world, 4, dcfg(world))
    assert portal.store.get_request(req.id).state == "merged" and world.branches(factory) == set()
    assert events(portal, req) == ["requested", "pr_opened", "merged", "archived", "factory_merged", "completed"]


# ---- the archive and the lifecycle App (Task 14; decided while planning: items 5 and 6) ----

def to_the_archive(portal, world, deploy):
    """A decommission's deploy PR opened, merged by a person and recorded: the next pass is the archive's."""
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    world.merge(deploy, 1)
    passes(portal, world, 1, dcfg(world))
    assert [s.state for s in steps(portal, req)] == ["done", "planned", "planned"]
    return req


def as_lifecycle_token(world):
    return [(c["method"], c["path"]) for c in world.calls if c["auth"] == f"Bearer {world.lifecycle_token}"]


# decided item 22: one read-only GET of the repository before the PATCH; M2: the token is revoked after use
TOKEN_CALLS = [("GET", "/repos/acme/notes"), ("PATCH", "/repos/acme/notes"), ("DELETE", "/installation/token")]


def test_a_decommission_ends_with_the_archive_and_the_task_closed(portal):
    world, deploy, factory = decommission_world()
    req = to_the_archive(portal, world, deploy)
    assert world.archived == set() and world.token_requests == []
    passes(portal, world, 1, dcfg(world))
    assert "acme/notes" in world.archived and events(portal, req)[-1] == "archived"
    assert world.token_requests == [(77, {"repositories": ["notes"], "permissions": {"administration": "write"}})]
    assert as_lifecycle_token(world) == TOKEN_CALLS, "the token reads the repository, archives it, and is revoked"
    app_calls = [(c["method"], c["path"]) for c in world.calls if c["path"].endswith("/installation") or c["path"].startswith("/app/")]
    assert app_calls == [("GET", f"/repos/acme/{p}/installation") for p in ("platform-pipeline", "scorecard-factory", "golden-road-deploy",
                                                                            "golden-road-portal", "golden-road-agents", "security-scans")] + [
        ("GET", "/repos/acme/notes/installation"), ("POST", "/app/installations/77/access_tokens")]
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[2].state == "pr_open" and list(world.pulls) == [(deploy, 1), (factory, 1)]
    merge = world.merge(factory, 1)
    passes(portal, world, 2, dcfg(world))
    after = portal.store.get_request(req.id)
    assert (after.state, after.merge_sha) == ("merged", merge)
    assert events(portal, req) == ["requested", "pr_opened", "merged", "archived", "pr_opened", "factory_merged", "completed"]
    assert "Kept: the images in ghcr, the Delinea secret values" in portal.store.lifecycle_events(req.id)[-1].note
    assert as_lifecycle_token(world) == TOKEN_CALLS, "the archive happened once"
    other = [c for c in world.calls if (c["method"], c["path"]) == ("PATCH", "/repos/acme/notes") and c["auth"] != f"Bearer {world.lifecycle_token}"]
    assert other == [], "only the lifecycle token archives"


def test_the_app_not_installed_fails_the_archive_and_the_factory_pr_waits(portal):
    world, deploy, factory = decommission_world()
    del world.installed["acme/notes"]
    req = to_the_archive(portal, world, deploy)
    passes(portal, world, 1, dcfg(world))
    archive = steps(portal, req)[1]
    assert (archive.state, archive.failure_reason) == ("failed", "archive: github: 404 GET /repos/acme/notes/installation")
    assert [s.state for s in steps(portal, req)] == ["done", "failed", "planned"] and world.branches(factory) == set()
    assert world.archived == set() and world.token_requests == []
    last = portal.store.lifecycle_events(req.id)[-1]
    assert last.step == "failed" and "archive: github: 404 GET /repos/acme/notes/installation" in last.note and "pull request" not in last.note
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: archive failed; waiting for Retry or Abandon"]
    assert portal.store.get_request(req.id).state == "writing"


def test_an_app_installed_on_a_platform_repo_is_refused(portal):
    world, deploy, _ = decommission_world()
    world.installed["acme/scorecard-factory"] = 5
    req = to_the_archive(portal, world, deploy)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].failure_reason == "lifecycle_app_on_platform_repo: acme/scorecard-factory"
    assert world.archived == set() and world.token_requests == []


def test_github_refusing_the_archive_fails_the_step(portal):
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    handle = world.handle

    def minted_then_revoked(request):
        answer = handle(request)
        if request.url.path.endswith("/access_tokens"):
            world.lifecycle_token = "ghs_" + "R" * 36  # the token GitHub minted no longer archives
        return answer
    world.handle = minted_then_revoked
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].failure_reason == "archive: github: 403 PATCH /repos/acme/notes" and world.archived == set()


@pytest.mark.parametrize("answer", [{"token": ""}, {"token": 7}, [], {}])
def test_a_token_answer_without_a_token_fails_the_step(portal, answer):
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    handle = world.handle
    world.handle = lambda request: httpx.Response(201, json=answer) if request.url.path.endswith("/access_tokens") else handle(request)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].failure_reason == "archive: github: unexpected response" and world.archived == set()
    assert as_lifecycle_token(world) == []


def test_a_patch_that_does_not_archive_fails_the_step(portal):
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    handle = world.handle
    world.handle = lambda request: (httpx.Response(200, json={"full_name": "acme/notes", "archived": False})
                                    if request.method == "PATCH" and request.url.path == "/repos/acme/notes" else handle(request))
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].failure_reason == "archive: github: unexpected response"


def test_the_archive_refuses_a_platform_repo_and_a_missing_app_without_a_call(portal):
    """The recheck refuses both before any step; the archive holds them again on its own, before any GitHub call."""
    from portal.writer.lifecycle_archive import archive
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    step = steps(portal, req)[1]
    before = len(world.calls)
    assert archive(portal.store, writer_config(), req, step, T0) == f"{req.id}: archive failed (no_lifecycle_app)"
    assert steps(portal, req)[1].failure_reason == "no_lifecycle_app"
    platform = dc_replace(req, repo="acme/security-scans")
    portal.store.move_lifecycle_step(request_id=req.id, step="archive", to_state="planned", now=T0)
    assert archive(portal.store, dcfg(world), platform, steps(portal, req)[1], T0) == f"{req.id}: archive failed (platform_repo)"
    assert len(world.calls) == before and world.archived == set()


def dc_replace(obj, **changes):
    import dataclasses
    return dataclasses.replace(obj, **changes)


def test_the_lifecycle_app_never_shows_its_key():
    world = RepoWorld()
    app = lifecycle_app(world)
    assert "PRIVATE" not in repr(app) and "PRIVATE" not in repr(writer_config(lifecycle_app=app))


# ---- retry, abandon, rebuild and completion (Task 15; spec rules 7 and 8) ----

def command(portal, req, what):
    return portal.store.add_lifecycle_command(request_id=req.id, command=what, actor_id=portal.people["admin"].id, now=T0)


def note(portal, req, step):
    return next(e.note for e in reversed(portal.store.lifecycle_events(req.id)) if e.step == step)


def test_retry_rebuilds_a_failed_pr_from_main(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = "failure"
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 2)
    assert steps(portal, req)[0].state == "failed"
    world.advance_main(deploy, "README.md", b"main moved meanwhile\n")
    world.checks.clear()
    made = command(portal, req, "retry")
    passes(portal, world, 1)
    [step] = steps(portal, req)
    assert (step.state, step.attempt, step.pr_number, step.branch) == ("pr_open", 2, 2, f"portal/lifecycle-{req.id}-deploy-2")
    assert step.base_sha == world.head(deploy) and world.pull(deploy, 2)["base_sha"] == world.head(deploy), "from the current main"
    assert portal.store.open_lifecycle_command(req.id) is None and made.id
    assert events(portal, req) == ["requested", "pr_opened", "failed", "retried", "pr_opened"]
    assert note(portal, req, "retried") == "Retried by Admin: the deploy step starts again (attempt 2)."
    assert "(attempt 2)" in note(portal, req, "pr_opened")
    passes(portal, world, 2)
    assert portal.store.get_request(req.id).state == "merged" and world.pull(deploy, 2)["merged"]


def test_a_retry_with_nothing_failed_changes_nothing(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    command(portal, req, "retry")
    assert passes(portal, world, 1) == [f"{req.id}: deploy waiting for checks"]
    assert portal.store.open_lifecycle_command(req.id) is None and "retried" not in events(portal, req)
    assert steps(portal, req)[0].attempt == 1


def test_main_moving_rebuilds_the_pr(portal):
    """(decided while planning: item 2) the PR is closed, the step planned again on a new attempt, then opened from the new main."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    world.advance_main(deploy, "README.md", b"x")
    assert passes(portal, world, 1) == [f"{req.id}: deploy rebuilt on the new main next pass"]
    [step] = steps(portal, req)
    assert (step.state, step.attempt, step.pr_number) == ("planned", 2, None)
    assert world.pull(deploy, 1)["state"] == "closed" and not world.pull(deploy, 1)["merged"]
    assert any(n == 1 and "rebuilds this change on the new main" in c for _, n, c in world.comments)
    world.checks.clear()
    passes(portal, world, 1)
    [step] = steps(portal, req)
    assert (step.state, step.pr_number, step.branch) == ("pr_open", 2, f"portal/lifecycle-{req.id}-deploy-2")
    assert "(attempt 2)" in note(portal, req, "pr_opened")
    passes(portal, world, 2)
    assert portal.store.get_request(req.id).state == "merged" and world.files(deploy)["README.md"] == b"x"


def test_a_binding_merging_first_is_recomposed_under_the_suspend(portal):
    """The spec's first error case: a binding merges to main while the suspend PR is open; the suspend is rebuilt on it, and test's
    composed.yaml holds both the binding and replicas: 0."""
    from portal.writer.run import compose_bytes
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    d = "apps/acme/notes/test"
    files = world.files(deploy)
    world.advance_main(deploy, f"{d}/bindings.yaml", BINDING_TEST)
    world.advance_main(deploy, f"{d}/composed.yaml", compose_bytes("acme/notes", "test", files[f"{d}/release.yaml"],
                                                                   files[f"{d}/base/manifests.yaml"], BINDING_TEST, None, None))
    world.checks.clear()
    assert passes(portal, world, 4) == [f"{req.id}: deploy rebuilt on the new main next pass", f"{req.id}: deploy pull request #2 opened",
                                        f"{req.id}: deploy done", f"{req.id}: applied"]
    assert portal.store.get_request(req.id).state == "merged" and world.pull(deploy, 2)["merged"]
    composed = world.files(deploy)[f"{d}/composed.yaml"]
    assert deployment(composed)["spec"]["replicas"] == 0
    assert b"AZURE_CLIENT_ID" in composed and b"11111111-1111-4111-8111-111111111111" in composed
    assert world.files(deploy)[f"{d}/bindings.yaml"] == BINDING_TEST


@pytest.mark.parametrize("action", ["suspend", "resume", "decommission"])
def test_abandon_before_the_merge_closes_every_pr(portal, action):
    world, deploy, _ = decommission_world(lifecycle={}) if action == "decommission" else (RepoWorld(), None, None)
    if action != "decommission":
        deploy, _ = deploy_world(world)
        world.checks[(deploy, "deploy-check")] = None  # the writer would merge a suspend or resume itself once it passes
    if action == "resume":
        lifecycle_request(portal.store, portal.people, T0)
        world.checks.clear()
        passes(portal, world, 3)
        world.checks[(deploy, "deploy-check")] = None
    before = portal.store.lifecycle_state("acme/notes")
    task = {"suspend": "SCTASK0010001", "resume": "SCTASK0010002", "decommission": "SCTASK0010003"}[action]
    req = lifecycle_request(portal.store, portal.people, T0, action=action, task=task)
    cfg = dcfg(world)
    passes(portal, world, 1, cfg)
    number = steps(portal, req)[0].pr_number
    assert steps(portal, req)[0].state == "pr_open"
    command(portal, req, "abandon")
    assert passes(portal, world, 1, cfg) == [f"{req.id}: abandoned"]
    assert world.pull(deploy, number)["state"] == "closed" and not world.pull(deploy, number)["merged"]
    assert any(n == number and "Abandoned in the golden road portal by Admin" in c for _, n, c in world.comments)
    assert {s.state for s in steps(portal, req)} == {"closed"}
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "abandoned")
    assert events(portal, req)[-1] == "abandoned" and "completed" not in events(portal, req)
    assert "The task stays open" in note(portal, req, "abandoned") and "Admin" in note(portal, req, "abandoned")
    assert portal.store.lifecycle_state("acme/notes") == before, "nothing applied"
    assert portal.store.open_lifecycle_command(req.id) is None and world.archived == set()
    assert passes(portal, world, 1, cfg) == [], "an abandoned request is no longer the writer's"


def test_abandon_of_a_failed_step_closes_it_with_no_pr_to_close(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = "failure"
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 2)
    comments = len(world.comments)
    branch = steps(portal, req)[0].branch
    assert branch in world.branches(deploy)
    command(portal, req, "abandon")
    assert passes(portal, world, 1) == [f"{req.id}: abandoned"]
    assert steps(portal, req)[0].state == "closed" and len(world.comments) == comments, "the failed step's PR was closed already"
    assert world.branches(deploy) == set(), "I2: its branch is deleted too"
    assert portal.store.get_request(req.id).failure_reason == "abandoned"


def test_abandon_after_the_deploy_pr_merged_does_nothing(portal):
    """Rule 8: a person merged the decommission's deploy PR before the pass: the command is done, the merge recorded in the same pass,
    and the decommission goes on (it can't be undone)."""
    world, deploy, factory = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    world.merge(deploy, 1)
    command(portal, req, "abandon")
    passes(portal, world, 1, dcfg(world))
    assert portal.store.open_lifecycle_command(req.id) is None
    assert [s.state for s in steps(portal, req)] == ["done", "planned", "planned"]
    assert portal.store.get_request(req.id).state == "writing" and portal.store.lifecycle_state("acme/notes").state == "decommissioned"
    assert "abandoned" not in events(portal, req)
    passes(portal, world, 1, dcfg(world))
    assert world.archived == {"acme/notes"}
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[2].state == "pr_open" and list(world.pulls) == [(deploy, 1), (factory, 1)]


def test_abandon_after_the_deploy_step_is_done_does_nothing(portal):
    world, deploy, factory = decommission_world()
    del world.installed["acme/notes"]
    req = to_the_archive(portal, world, deploy)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].state == "failed"
    command(portal, req, "abandon")
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: archive failed; waiting for Retry or Abandon"]
    assert portal.store.open_lifecycle_command(req.id) is None and portal.store.get_request(req.id).state == "writing"
    assert [s.state for s in steps(portal, req)] == ["done", "failed", "planned"] and "abandoned" not in events(portal, req)


def test_abandon_of_a_merged_deploy_pr_that_failed_its_confirmation_does_nothing(portal):
    """A deploy step failed after its PR merged (merged_file_differs): the PR merged, so the decommission can't be abandoned."""
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    world.merge(deploy, 1)
    world.advance_main(deploy, "apps/acme/notes/test/release.yaml", b"# back\n")
    world.pull(deploy, 1)["merge_commit_sha"] = world.head(deploy)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[0].failure_reason == "merged_file_differs"
    command(portal, req, "abandon")
    passes(portal, world, 1, dcfg(world))
    assert portal.store.open_lifecycle_command(req.id) is None and portal.store.get_request(req.id).state == "writing"
    assert steps(portal, req)[0].state == "failed" and "abandoned" not in events(portal, req)


def test_abandon_waits_when_github_does_not_answer(portal):
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    command(portal, req, "abandon")
    handle = world.handle
    world.handle = lambda request: (httpx.Response(502, json={}) if request.url.path.endswith("/pulls") and request.method == "GET"
                                    else handle(request))
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: abandon waits: GitHub didn't answer"]
    assert portal.store.open_lifecycle_command(req.id) is not None and steps(portal, req)[0].state == "pr_open"
    world.handle = handle
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: abandoned"]


def test_a_failed_archive_is_retried_from_the_archive(portal):
    world, deploy, factory = decommission_world()
    del world.installed["acme/notes"]
    req = to_the_archive(portal, world, deploy)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].failure_reason == "archive: github: 404 GET /repos/acme/notes/installation"
    world.installed["acme/notes"] = 77
    command(portal, req, "retry")
    passes(portal, world, 1, dcfg(world))
    assert world.archived == {"acme/notes"} and [s.state for s in steps(portal, req)] == ["done", "done", "planned"]
    assert steps(portal, req)[1].attempt == 2 and list(world.pulls) == [(deploy, 1)], "no new deploy PR, and no factory PR yet"
    assert note(portal, req, "retried") == "Retried by Admin: the archive step starts again (attempt 2)."
    passes(portal, world, 1, dcfg(world))
    assert list(world.pulls) == [(deploy, 1), (factory, 1)]


def drive(portal, world, req, repos):
    """Passes, a person merging each PR the writer says waits for review, until the request is no longer writing or a step failed."""
    for _ in range(12):
        for line in passes(portal, world, 1, dcfg(world)):
            if line.endswith("waiting for review"):
                s = next(s for s in steps(portal, req) if s.state == "pr_open")
                world.merge(repos[s.step], s.pr_number)
        if portal.store.get_request(req.id).state != "writing" or any(s.state == "failed" for s in steps(portal, req)):
            return


def fail_deploy(world, deploy, factory):
    world.checks[(deploy, "deploy-check")] = "failure"
    return world.checks.clear


def fail_archive(world, deploy, factory):
    del world.installed["acme/notes"]
    return lambda: world.installed.update({"acme/notes": 77})


def fail_factory(world, deploy, factory):
    world.checks[(factory, "factory-check")] = "failure"
    return world.checks.clear


@pytest.mark.parametrize("step, fail, reason", [
    ("deploy", fail_deploy, "check_failed: deploy-check"),
    ("archive", fail_archive, "archive: github: 404 GET /repos/acme/notes/installation"),
    ("factory", fail_factory, "check_failed: factory-check")])
def test_each_decommission_step_fails_and_is_retried(portal, step, fail, reason):
    world, deploy, factory = decommission_world()
    repos = {"deploy": deploy, "factory": factory}
    fix = fail(world, deploy, factory)
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    drive(portal, world, req, repos)
    failed = next(s for s in steps(portal, req) if s.state == "failed")
    assert (failed.step, failed.failure_reason) == (step, reason)
    assert events(portal, req)[-1] == "failed" and reason in note(portal, req, "failed")
    assert [s.state for s in steps(portal, req)][[s.step for s in steps(portal, req)].index(step) + 1:] in ([], ["planned"], ["planned", "planned"])
    fix()
    command(portal, req, "retry")
    drive(portal, world, req, repos)
    assert portal.store.get_request(req.id).state == "merged" and world.archived == {"acme/notes"}
    assert events(portal, req)[-1] == "completed" and events(portal, req).count("retried") == 1
    assert {s.step: s.attempt for s in steps(portal, req)} == {"deploy": 1, "archive": 1, "factory": 1, step: 2}


# ---- review fixes (Round 4) ----

def test_every_step_moves_with_its_note(portal, monkeypatch):
    """L11: a step's move and its note are one write (move_lifecycle_step_with_event), so a crash between two writes can't lose a note.
    Here the separate writes crash: a suspend that fails, is retried and is applied never needs them."""
    def crash(**kw):
        raise RuntimeError("a separate write: a crash here would lose the note or the move")
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = "failure"
    req = lifecycle_request(portal.store, portal.people, T0)
    monkeypatch.setattr(portal.store, "add_lifecycle_event", crash)
    passes(portal, world, 2)
    assert steps(portal, req)[0].state == "failed" and events(portal, req) == ["requested", "pr_opened", "failed"]
    world.checks.clear()
    command(portal, req, "retry")
    monkeypatch.setattr(portal.store, "move_lifecycle_step", crash)
    passes(portal, world, 3)
    assert portal.store.get_request(req.id).state == "merged"
    assert events(portal, req) == ["requested", "pr_opened", "failed", "retried", "pr_opened", "merged", "completed"]


def test_a_person_merging_a_pr_whose_head_changed_fails_after_the_merge_and_says_so(portal):
    """The merged check comes first: a PR a person merged with a commit the writer didn't write is merged_with_other_commits (not
    pr_head_changed, and the PR isn't "closed" by the writer), and the note says plainly that it merged."""
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    [deploy_step, *_] = steps(portal, req)
    world.push(deploy, deploy_step.branch, "README.md", b"someone else's commit\n")
    world.merge(deploy, 1)
    passes(portal, world, 1, dcfg(world))
    deploy_step = steps(portal, req)[0]
    assert (deploy_step.state, deploy_step.failure_reason) == ("failed", "merged_with_other_commits")
    last = note(portal, req, "failed")
    assert "was merged with commits the writer didn't write" in last and world.pull(deploy, 1)["merged"]
    assert not any("closes this pull request" in c for _, n, c in world.comments if n == 1)


def test_a_pr_whose_base_commit_is_not_main_is_rebuilt(portal):
    """As run._advance: the PR's own base.sha must be the current main too, not only the branch's fork point."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    handle = world.handle

    def stale_base(request):
        answer = handle(request)
        if request.method == "GET" and request.url.path == f"/repos/{deploy}/pulls/1":
            doc = answer.json()
            return httpx.Response(200, json={**doc, "base": {**doc["base"], "sha": "9" * 40}})
        return answer
    world.handle = stale_base
    assert passes(portal, world, 1) == [f"{req.id}: deploy rebuilt on the new main next pass"]


def test_a_pr_on_the_writer_s_branch_opened_by_someone_else_is_not_adopted(portal):
    """The pass stopped before POST /pulls; a person opened a PR from the writer's branch meanwhile: never adopted."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    handle = world.handle

    def crash_at_the_pr(request):
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            raise RuntimeError("the writer's pod stopped")
        return handle(request)
    world.handle = crash_at_the_pr
    with pytest.raises(RuntimeError):
        passes(portal, world, 1)
    world.handle = handle
    world.writer_login = "mallory"
    client_for(world).call("POST", f"/repos/{deploy}/pulls", ok=(201,), json={
        "title": "x", "head": f"portal/lifecycle-{req.id}-deploy-1", "base": "main", "body": "x"})
    world.writer_login = "golden-road-portal-writer[bot]"
    passes(portal, world, 1)
    assert steps(portal, req)[0].failure_reason == "pr_not_ours" and not world.pull(deploy, 1)["merged"]


def test_the_requester_is_still_an_admin_before_the_writer_merges(portal):
    """Rule 1 again before each merge, not only before the first step."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    portal.store.sign_in(issuer="https://sso.example.test/application/o/portal/", subject="sub-admin", display_name="Admin",
                         email="admin@example.test", groups=[], roles=(), now=T0, max_age=portal.settings.session_max)
    passes(portal, world, 1)
    [step] = steps(portal, req)
    assert (step.state, step.failure_reason) == ("failed", "not_admin") and not world.pull(deploy, 1)["merged"]
    assert world.pull(deploy, 1)["state"] == "closed"


@pytest.mark.parametrize("status", [405, 409])
def test_a_merge_github_refuses_waits_then_times_out(portal, status):
    import datetime as dt
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    handle = world.handle
    world.handle = lambda request: (httpx.Response(status, json={"message": "not now"}) if request.method == "PUT"
                                    and request.url.path.endswith("/merge") else handle(request))
    assert passes(portal, world, 1) == [f"{req.id}: deploy merge not allowed yet ({status})"]
    assert steps(portal, req)[0].state == "pr_open"
    run_once(portal.store, client_for(world), writer_config(), T0 + dt.timedelta(hours=1))
    [step] = steps(portal, req)
    assert (step.state, step.failure_reason) == ("failed", f"github: {status} PUT /repos/{deploy}/pulls/1/merge")
    assert world.pull(deploy, 1)["state"] == "closed" and not world.pull(deploy, 1)["merged"]


def test_main_moving_under_an_open_resume_pr_rebuilds_it(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    reviewed = world.files(deploy)
    lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 3)
    world.checks[(deploy, "deploy-check")] = None
    resume = lifecycle_request(portal.store, portal.people, T0, action="resume", task="SCTASK0010002")
    passes(portal, world, 1)
    number = steps(portal, resume)[0].pr_number
    world.advance_main(deploy, "README.md", b"main moved\n")
    with pytest.raises(AssertionError):
        world.merge(deploy, number)  # the strict ruleset: not even a person merges it now
    assert passes(portal, world, 1) == [f"{resume.id}: deploy rebuilt on the new main next pass"]
    world.checks.clear()
    passes(portal, world, 3)
    assert portal.store.get_request(resume.id).state == "merged" and steps(portal, resume)[0].attempt == 2
    assert {p: b for p, b in world.files(deploy).items() if p != "README.md"} == reviewed


def test_main_moving_under_a_suspend_pr_with_green_checks_rebuilds_it_and_never_merges_the_old_one(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    world.advance_main(deploy, "README.md", b"main moved\n")
    passes(portal, world, 1)
    assert world.pull(deploy, 1)["state"] == "closed" and not world.pull(deploy, 1)["merged"]
    passes(portal, world, 3)
    assert portal.store.get_request(req.id).state == "merged" and world.pull(deploy, 2)["merged"]


def crash_after_the_pr_opens(world):
    """The pass stops right after GitHub opened the PR, before the step records it."""
    handle = world.handle

    def crashing(request):
        answer = handle(request)
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            world.handle = handle
            raise RuntimeError("the writer's pod stopped")
        return answer
    world.handle = crashing


def test_a_crash_after_the_pr_opened_then_main_moving_closes_it_and_rebuilds(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    crash_after_the_pr_opens(world)
    with pytest.raises(RuntimeError):
        passes(portal, world, 1)
    assert steps(portal, req)[0].state == "planned" and world.pull(deploy, 1)["state"] == "open"
    world.advance_main(deploy, "README.md", b"main moved\n")
    passes(portal, world, 1)
    assert world.pull(deploy, 1)["state"] == "closed" and not world.pull(deploy, 1)["merged"], "the stranded PR is closed"
    passes(portal, world, 1)
    [step] = steps(portal, req)
    assert (step.state, step.attempt, step.pr_number, step.branch) == ("pr_open", 2, 2, f"portal/lifecycle-{req.id}-deploy-2")
    passes(portal, world, 2)
    assert portal.store.get_request(req.id).state == "merged" and world.pull(deploy, 2)["merged"]
    assert portal.store.lifecycle_state("acme/notes").state == "suspended"


def test_a_stranded_pr_a_person_merged_is_recorded_as_applied(portal):
    """I-1: the crash, then a person merges the PR the step never recorded (main moves with it). The next pass finds the merged PR on
    the step's branch before it plans, checks it as a recorded one (the planned chain, from the deploy repo, the files at the merge
    commit) and records its merge commit, with the merge's note (not "already in the deploy repo")."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    crash_after_the_pr_opens(world)
    with pytest.raises(RuntimeError):
        passes(portal, world, 1)
    merge = world.merge(deploy, 1)
    world.advance_main(deploy, "README.md", b"main moved\n")
    passes(portal, world, 2)
    assert portal.store.get_request(req.id).state == "merged" and list(world.pulls) == [(deploy, 1)]
    assert portal.store.lifecycle_state("acme/notes").state == "suspended"
    assert steps(portal, req)[0].merge_sha == merge and portal.store.get_request(req.id).merge_sha == merge
    assert f"merge commit {merge}" in note(portal, req, "merged") and "already in the deploy repo" not in note(portal, req, "merged")
    assert "#1" in note(portal, req, "merged") and "before the writer recorded it" in note(portal, req, "merged")


def test_a_stranded_pr_someone_else_opened_on_the_branch_is_not_closed(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    crash_after_the_pr_opens(world)
    with pytest.raises(RuntimeError):
        passes(portal, world, 1)
    world.pull(deploy, 1)["user"] = {"login": "mallory", "type": "User"}
    world.advance_main(deploy, "README.md", b"main moved\n")
    passes(portal, world, 1)
    assert steps(portal, req)[0].failure_reason == "branch_not_ours" and world.pull(deploy, 1)["state"] == "open"


def test_a_resume_already_in_git_is_done_with_no_pr(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 3)
    resume = lifecycle_request(portal.store, portal.people, T0, action="resume", task="SCTASK0010002")
    for env in ("test", "prod"):  # a person removed both lifecycle.yaml files by hand meanwhile
        tree = world.files(deploy)
        del tree[f"apps/acme/notes/{env}/lifecycle.yaml"]
        world.repos[deploy]["main"] = world._new_commit(tree, world.head(deploy), [])
    passes(portal, world, 2)
    assert portal.store.get_request(resume.id).state == "merged" and steps(portal, resume)[0].pr_number is None


def test_an_adapter_by_the_same_name_for_another_app_is_never_deleted(portal):
    """adapter_name isn't one-to-one (acme/my_app and acme/my-app are both acme-my-app-<env>, and so would be acme-my/app outside the
    org): an adapter is the decommissioned app's only if its path is ./apps/<owner>/<repo>/<env> (Flux) or apps/<owner>/<repo>/<env>
    (Argo CD), as compose writes it."""
    world = RepoWorld()
    deploy, factory = deploy_world(world, repo="acme/my-app")
    world.add_repo("acme/my_app", {"README.md": b"# app\n"})
    world.advance_main(factory, "intake/acme/my_app.yaml", INTAKE_V2("acme/my_app"))
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", repo="acme/my_app", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[0].failure_reason == "adapter_not_ours" and world.pulls == {} and world.branches(deploy) == set()


def test_an_adapter_that_does_not_read_as_one_is_never_deleted(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world, environments=())
    world.advance_main(deploy, "clusters/office/argo/acme-notes-prod.yaml", b"# left behind, not an adapter\n")
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[0].failure_reason == "adapter_not_ours" and world.pulls == {}


@pytest.mark.parametrize("stored", ["Acme/Notes", "acme/Notes", "ACME/notes"])
def test_an_app_directory_in_another_case_refuses_the_decommission(portal, stored):
    """The deploy repo holds the app under another spelling: never "nothing was in the deploy repo" while it runs."""
    world = RepoWorld()
    deploy, factory = deploy_world(world, repo=stored)
    world.advance_main(factory, "intake/acme/notes.yaml", INTAKE_V2("acme/notes"))
    world.add_repo("acme/notes", {"README.md": b"# app\n"})
    world.installed["acme/notes"] = 77
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    deploy_step = steps(portal, req)[0]
    assert (deploy_step.state, deploy_step.failure_reason) == ("failed", "case_mismatch") and world.pulls == {}
    assert portal.store.lifecycle_state("acme/notes") is None


def test_an_archive_that_happened_before_a_crash_is_done_without_a_second_patch(portal):
    """Decided item 22: the PATCH archived the repository and the pass stopped before it recorded the step; the next pass reads
    archived: true and records the step done with no second PATCH."""
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    handle = world.handle

    def crash_after_the_patch(request):
        answer = handle(request)
        if request.method == "PATCH" and request.url.path == "/repos/acme/notes":
            world.handle = handle
            raise RuntimeError("the writer's pod stopped")
        return answer
    world.handle = crash_after_the_patch
    with pytest.raises(RuntimeError):
        passes(portal, world, 1, dcfg(world))
    assert world.archived == {"acme/notes"} and steps(portal, req)[1].state == "planned"
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].state == "done" and events(portal, req)[-1] == "archived"
    patches = [c for c in world.calls if (c["method"], c["path"]) == ("PATCH", "/repos/acme/notes")]
    assert len(patches) == 1
    assert as_lifecycle_token(world)[-2:] == [("GET", "/repos/acme/notes"), ("DELETE", "/installation/token")]


@pytest.mark.parametrize("answer", [
    {"permissions": {"administration": "write", "contents": "write"}, "repositories": [{"name": "notes"}], "repository_selection": "selected"},
    {"permissions": {"administration": "write"}, "repositories": [{"name": "notes"}, {"name": "other"}], "repository_selection": "selected"},
    {"permissions": {"administration": "write"}, "repositories": [{"name": "notes"}], "repository_selection": "all"},
    {"permissions": {"administration": "read"}, "repositories": [{"name": "notes"}], "repository_selection": "selected"}])
def test_a_token_wider_or_narrower_than_asked_is_revoked_unused(portal, answer):
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    handle = world.handle

    def minted(request):
        if request.url.path.endswith("/access_tokens"):
            handle(request)
            return httpx.Response(201, json={"token": world.lifecycle_token, "expires_at": "2026-11-03T00:00:00Z", **answer})
        return handle(request)
    world.handle = minted
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].failure_reason == "archive: github: unexpected response" and world.archived == set()
    assert as_lifecycle_token(world) == [("DELETE", "/installation/token")]


def test_a_token_with_metadata_read_too_is_taken(portal):
    world, deploy, _ = decommission_world()
    world.token_permissions = {"administration": "write", "metadata": "read"}
    req = to_the_archive(portal, world, deploy)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].state == "done"


def test_a_failed_archive_still_revokes_its_token(portal):
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    handle = world.handle
    world.handle = lambda request: (httpx.Response(422, json={}) if request.method == "PATCH" and request.url.path == "/repos/acme/notes"
                                    else handle(request))
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].failure_reason == "archive: github: 422 PATCH /repos/acme/notes"
    assert as_lifecycle_token(world)[-1] == ("DELETE", "/installation/token")


@pytest.mark.parametrize("answer", [httpx.Response(502, json={}), httpx.Response(503, json={}),
                                    httpx.Response(403, json={}, headers={"x-ratelimit-remaining": "0"}),
                                    httpx.Response(429, json={}), "unreachable"])
def test_github_not_answering_the_archive_waits_for_the_next_pass(portal, answer):
    """M3: status 0 (unreachable), 5xx and a rate limit are not a failure: the step stays planned and the next pass tries again."""
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    handle = world.handle

    def flaky(request):
        if request.method == "PATCH" and request.url.path == "/repos/acme/notes":
            if answer == "unreachable":
                raise httpx.ConnectError("no route", request=request)
            return answer
        return handle(request)
    world.handle = flaky
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: archive waits: GitHub didn't answer"]
    assert steps(portal, req)[1].state == "planned" and world.archived == set()
    world.handle = handle
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].state == "done" and world.archived == {"acme/notes"}


def test_a_repo_outside_the_org_is_refused(portal):
    """M1: the recheck and the archive refuse an app whose owner isn't PORTAL_ORG (any case), before any GitHub call."""
    from portal.writer.lifecycle_archive import archive
    world = RepoWorld()
    deploy_world(world, repo="other/notes")
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", repo="other/notes", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert portal.store.get_request(req.id).failure_reason == "recheck: not_in_org" and world.calls == []
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    before = len(world.calls)
    assert archive(portal.store, dcfg(world), dc_replace(req, repo="other/notes"), steps(portal, req)[1], T0) == \
        f"{req.id}: archive failed (not_in_org)"
    assert len(world.calls) == before


def test_an_owner_in_another_case_is_in_the_org(portal):
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    from portal.writer.lifecycle_archive import archive
    world.installed["ACME/notes"] = 77
    world.add_repo("ACME/notes", {"README.md": b"# app\n"})
    assert archive(portal.store, dcfg(world), dc_replace(req, repo="ACME/notes"), steps(portal, req)[1], T0) == f"{req.id}: archive done"


@pytest.mark.parametrize("platform", ["ACME/Scorecard-Factory", "Acme/golden-road-DEPLOY"])
def test_the_platform_guard_holds_in_any_case(portal, platform):
    """M7: a platform repository in another spelling is still refused, by the recheck and by the archive."""
    from portal.writer.lifecycle_archive import archive
    world = RepoWorld()
    deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", repo=platform, task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    assert portal.store.get_request(req.id).failure_reason == "recheck: platform_repo" and world.calls == []
    world, deploy, _ = decommission_world()
    req = to_the_archive(portal, world, deploy)
    assert archive(portal.store, dcfg(world), dc_replace(req, repo=platform), steps(portal, req)[1], T0) == f"{req.id}: archive failed (platform_repo)"


# ---- the commands, after the T14/T15 review (C1, C2, I1, I2, M4) ----

def test_abandon_after_a_retry_of_a_merged_deploy_pr_is_too_late(portal):
    """C1: the deploy PR of attempt 1 merged (and failed its confirmation); a Retry opened attempt 2. The abandon looks at every
    attempt's PR on GitHub, finds #1 merged, and changes nothing: the decommission can't be undone."""
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    world.merge(deploy, 1)
    world.advance_main(deploy, "apps/acme/notes/test/release.yaml", b"# back\n")
    world.pull(deploy, 1)["merge_commit_sha"] = world.head(deploy)
    passes(portal, world, 1, dcfg(world))
    command(portal, req, "retry")
    passes(portal, world, 1, dcfg(world))
    assert (steps(portal, req)[0].attempt, steps(portal, req)[0].pr_number) == (2, 2)
    command(portal, req, "abandon")
    passes(portal, world, 1, dcfg(world))
    assert portal.store.get_request(req.id).state == "writing" and "abandoned" not in events(portal, req)
    assert portal.store.open_lifecycle_command(req.id) is None
    assert "came too late" in note(portal, req, "command") and world.pull(deploy, 2)["state"] == "open"


def test_a_merge_between_the_abandon_s_check_and_its_close_is_recorded_as_a_merge(portal):
    """C2: a reviewer merges right after the writer read the PR: the writer closes first, reads again, and finds it merged."""
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    command(portal, req, "abandon")
    handle, seen = world.handle, []

    def merge_after_the_first_read(request):  # the PR's own read: the list never says whether a PR merged (C-1)
        answer = handle(request)
        if request.method == "GET" and request.url.path == f"/repos/{deploy}/pulls/1" and not seen:
            seen.append(1)
            world.merge(deploy, 1)
        return answer
    world.handle = merge_after_the_first_read
    passes(portal, world, 1, dcfg(world))
    assert portal.store.get_request(req.id).state == "writing" and "abandoned" not in events(portal, req)
    assert steps(portal, req)[0].state == "done" and portal.store.lifecycle_state("acme/notes").state == "decommissioned"
    assert "came too late" in note(portal, req, "command")


def test_a_command_by_someone_no_longer_an_admin_is_refused_with_a_note(portal):
    """I1: 0012 lets the web insert a command; the writer holds rule 1 for it as for the request."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    portal.store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=portal.people["owner"].id, now=T0)
    passes(portal, world, 1)
    assert portal.store.open_lifecycle_command(req.id) is None and portal.store.get_request(req.id).state == "writing"
    assert steps(portal, req)[0].state == "pr_open" and world.pull(deploy, 1)["state"] == "open"
    assert note(portal, req, "command") == "Refused: Owner asked to abandon this request but is not a portal admin (rule 1), so nothing changed."


def test_a_retry_with_nothing_failed_is_noted(portal):
    """M4: never silence."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    command(portal, req, "retry")
    passes(portal, world, 1)
    assert note(portal, req, "command") == "Retry asked by Admin, but no step had failed: nothing changed."


def test_abandon_closes_and_deletes_every_attempt_s_branch(portal):
    """I2: a rebuild left attempt 1's branch; the abandon deletes both attempts' branches and closes the open PR."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    world.advance_main(deploy, "README.md", b"x")
    passes(portal, world, 2)
    assert len(world.branches(deploy)) == 2
    command(portal, req, "abandon")
    assert passes(portal, world, 1) == [f"{req.id}: abandoned"]
    assert world.branches(deploy) == set() and all(p["state"] == "closed" and not p["merged"] for p in world.pulls.values())


# ---- the re-review of the writer (C-1, I-1, I-2, minors) ----

def test_the_list_of_pull_requests_never_says_merged_so_each_is_read_on_its_own(portal):
    """C-1: GET /pulls?head= answers the pull-request-simple shape (no "merged"); the abandon reads each PR to see the merge."""
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    listed = client_for(world).call("GET", f"/repos/{deploy}/pulls", params={"head": f"acme:{steps(portal, req)[0].branch}", "state": "all"})
    assert "merged" not in listed.json()[0] and "merged_at" in listed.json()[0], "the fake answers as GitHub does"
    world.merge(deploy, 1)
    command(portal, req, "abandon")
    passes(portal, world, 1, dcfg(world))
    assert "came too late" in note(portal, req, "command") and "abandoned" not in events(portal, req)
    assert any(c["path"] == f"/repos/{deploy}/pulls/1" and c["method"] == "GET" for c in world.calls)


def test_a_pull_request_that_does_not_say_whether_it_merged_makes_the_abandon_wait(portal):
    """C-1: a missing "merged" is unknown, never unmerged: the abandon waits and changes nothing."""
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    passes(portal, world, 1, dcfg(world))
    command(portal, req, "abandon")
    handle = world.handle

    def without_merged(request):
        answer = handle(request)
        if request.method == "GET" and request.url.path == f"/repos/{deploy}/pulls/1":
            return httpx.Response(200, json={k: v for k, v in answer.json().items() if k != "merged"})
        return answer
    world.handle = without_merged
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: abandon waits: GitHub didn't answer"]
    assert portal.store.open_lifecycle_command(req.id) is not None and world.pull(deploy, 1)["state"] == "open"


def test_a_retry_is_one_write(portal, monkeypatch):
    """I-3: the step's move, its retried note and the command finished are one store call (retry_lifecycle): a crash can't leave a
    retried step under an open command, which the next pass would retry again with a second, contradictory note."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = "failure"
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 2)
    world.checks.clear()
    command(portal, req, "retry")

    def crash(**_):
        raise RuntimeError("the writer's pod stopped")
    for name in ("finish_lifecycle_command", "move_lifecycle_step", "move_lifecycle_step_with_event"):
        monkeypatch.setattr(portal.store, name, crash)
    real = portal.store.retry_lifecycle
    with pytest.raises(RuntimeError):  # the pass stops at the PR it opens next; the retry was written first, whole
        monkeypatch.setattr(portal.store, "retry_lifecycle", lambda **kw: (real(**kw), crash())[0])
        passes(portal, world, 1)
    assert steps(portal, req)[0].state == "planned" and portal.store.open_lifecycle_command(req.id) is None
    assert events(portal, req).count("retried") == 1


def test_an_abandon_closes_its_steps_in_its_own_transaction(portal, monkeypatch):
    """The steps are closed by abandon_lifecycle, with the note and the request failed: never closed steps under a writing request."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    command(portal, req, "abandon")
    monkeypatch.setattr(portal.store, "move_lifecycle_step", lambda **_: (_ for _ in ()).throw(RuntimeError("a second transaction")))
    assert passes(portal, world, 1) == [f"{req.id}: abandoned"]
    assert steps(portal, req)[0].state == "closed" and portal.store.get_request(req.id).failure_reason == "abandoned"


def open_pr_as(world, repo, branch, login):
    """Someone opens a PR from the writer's branch (GitHub lets anyone with push access)."""
    pr = client_for(world).call("POST", f"/repos/{repo}/pulls", ok=(201,), json={"title": "mine", "head": branch, "base": "main", "body": "."}).json()
    world.pull(repo, pr["number"])["user"] = {"login": login, "type": "User"}
    return pr["number"]


def test_a_branch_deletion_github_refuses_makes_the_abandon_wait(portal):
    """I-2: a 422 is "already gone" only when the branch reads as 404 afterwards; a branch still there (a rule protecting it) waits."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    command(portal, req, "abandon")
    handle = world.handle
    world.handle = lambda request: (httpx.Response(422, json={"message": "Cannot delete this protected branch"})
                                    if request.method == "DELETE" and "/git/refs/heads/" in request.url.path else handle(request))
    assert passes(portal, world, 1) == [f"{req.id}: abandon waits: a branch could not be deleted"]
    assert portal.store.open_lifecycle_command(req.id) is not None and portal.store.get_request(req.id).state == "writing"
    assert steps(portal, req)[0].branch in world.branches(deploy)
    world.handle = handle
    assert passes(portal, world, 1) == [f"{req.id}: abandoned"] and world.branches(deploy) == set()


def test_an_open_pr_someone_else_opened_on_the_writer_s_branch_makes_the_abandon_wait(portal):
    """I-2: the final read counts every open PR on the request's branches, not only the writer's; the writer never closes theirs."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    theirs = open_pr_as(world, deploy, steps(portal, req)[0].branch, "mallory")
    command(portal, req, "abandon")
    assert passes(portal, world, 1) == [f"{req.id}: abandon waits: a pull request is still open"]
    assert world.pull(deploy, theirs)["state"] == "open" and world.pull(deploy, 1)["state"] == "closed"
    assert portal.store.open_lifecycle_command(req.id) is not None
    world.pull(deploy, theirs)["state"] = "closed"
    assert passes(portal, world, 1) == [f"{req.id}: abandoned"]


def test_an_abandon_with_no_write_login_waits(portal):
    """Fail closed: with the write App's login unknown, no PR reads as the writer's, so none would be closed: the abandon waits."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    command(portal, req, "abandon")
    assert passes(portal, world, 1, writer_config(write_login=None)) == [f"{req.id}: abandon waits: the write App's login is unknown"]
    assert world.pull(deploy, 1)["state"] == "open" and portal.store.open_lifecycle_command(req.id) is not None



def test_a_decommission_pr_a_person_merged_before_the_writer_recorded_it_is_checked_and_recorded(portal):
    """I-1: never "no pull request was needed" for a merged decommission: the merge commit is recorded with its note."""
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    crash_after_the_pr_opens(world)
    with pytest.raises(RuntimeError):
        passes(portal, world, 1, dcfg(world))
    merge = world.merge(deploy, 1)
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: deploy done"]
    assert steps(portal, req)[0].merge_sha == merge and "Nothing of" not in note(portal, req, "merged")
    assert f"merge commit {merge}" in note(portal, req, "merged") and portal.store.lifecycle_state("acme/notes").state == "decommissioned"


def test_an_unrecorded_pr_merged_with_a_commit_the_writer_did_not_write_fails_the_step(portal):
    """I-1: as for a recorded PR, a merge holding a commit beyond the planned chain is merged_with_other_commits."""
    world, deploy, _ = decommission_world()
    req = lifecycle_request(portal.store, portal.people, T0, action="decommission", task="SCTASK0010003")
    crash_after_the_pr_opens(world)
    with pytest.raises(RuntimeError):
        passes(portal, world, 1, dcfg(world))
    world.push(deploy, f"portal/lifecycle-{req.id}-deploy-1", "clusters/home/flux/other.yaml", b"# someone else's\n")
    world.merge(deploy, 1)
    passes(portal, world, 1, dcfg(world))
    [deploy_step, *_] = steps(portal, req)
    assert (deploy_step.state, deploy_step.failure_reason) == ("failed", "merged_with_other_commits")
    assert "Its pull request: https://github.com/acme/golden-road-deploy/pull/1." in note(portal, req, "failed")
    assert portal.store.lifecycle_state("acme/notes") is None, "nothing recorded as applied"
    command(portal, req, "retry")  # judged once: a Retry plans at main and is not failed again for the same merge
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[0].state == "done" and events(portal, req).count("failed") == 1


def test_a_stranded_pr_merged_between_the_writer_s_read_and_its_close_is_recorded(portal):
    """I-1: the race in a rebuild: the stranded PR merges after the writer read it and before its close (which GitHub then refuses).
    The next pass, on the new attempt, finds the merge on the earlier attempt's branch and records it."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    crash_after_the_pr_opens(world)
    with pytest.raises(RuntimeError):
        passes(portal, world, 1)
    world.advance_main(deploy, "README.md", b"main moved\n")
    handle, merged = world.handle, []

    def merge_before_the_close(request):
        if request.method == "PATCH" and request.url.path == f"/repos/{deploy}/pulls/1" and not merged:
            pull = world.pull(deploy, 1)  # a person merged it (no strict ruleset here): main gets the branch's tree with main's change
            tree = {**world.files(deploy, pull["head"]["ref"]), "README.md": b"main moved\n"}
            sha = world._new_commit(tree, world.head(deploy), world._diff(deploy, pull))
            world.repos[deploy]["main"] = sha
            pull.update(state="closed", merged=True, merge_commit_sha=sha, merged_at="2026-11-02T23:00:00Z",
                        last_tip=world.head(deploy, pull["head"]["ref"]))
            merged.append(sha)
        return handle(request)
    world.handle = merge_before_the_close
    assert passes(portal, world, 1) == [f"{req.id}: deploy rebuilt on the new main next pass"]
    assert merged and steps(portal, req)[0].attempt == 2
    world.handle = handle
    passes(portal, world, 2)
    assert steps(portal, req)[0].merge_sha == merged[0] and portal.store.get_request(req.id).state == "merged"
    assert list(world.pulls) == [(deploy, 1)], "no second PR for a change already merged"


def test_a_rebuild_closes_only_the_writer_s_prs_on_the_stranded_branch(portal, monkeypatch):
    """A PR someone opened on the stranded branch after the writer's check (the race) is never closed by the writer."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    crash_after_the_pr_opens(world)
    with pytest.raises(RuntimeError):
        passes(portal, world, 1)
    theirs = open_pr_as(world, deploy, f"portal/lifecycle-{req.id}-deploy-1", "mallory")
    world.advance_main(deploy, "README.md", b"main moved\n")
    monkeypatch.setattr(writer_lifecycle, "_stranded", lambda *a: True)  # their PR opened after the writer's check
    assert passes(portal, world, 1) == [f"{req.id}: deploy rebuilt on the new main next pass"]
    assert world.pull(deploy, 1)["state"] == "closed" and world.pull(deploy, theirs)["state"] == "open"


def test_every_rebuild_is_noted_for_servicenow(portal):
    """A rebuild says which PR the writer closed and why, in the step's move ("rebuilt")."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    world.advance_main(deploy, "README.md", b"x")
    passes(portal, world, 1)
    assert events(portal, req) == ["requested", "pr_opened", "rebuilt"]
    assert note(portal, req, "rebuilt") == ("Pull request #1 (https://github.com/acme/golden-road-deploy/pull/1) closed: the deploy repo's "
                                            "default branch moved, so the writer rebuilds the deploy step on the new main (attempt 2).")


def test_a_stranded_rebuild_is_noted_too(portal):
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    crash_after_the_pr_opens(world)
    with pytest.raises(RuntimeError):
        passes(portal, world, 1)
    world.advance_main(deploy, "README.md", b"main moved\n")
    passes(portal, world, 1)
    assert events(portal, req)[-1] == "rebuilt"
    assert note(portal, req, "rebuilt") == (f"Pull request #1 closed: the deploy repo's default branch moved before the writer recorded it "
                                            f"(branch portal/lifecycle-{req.id}-deploy-1), so the writer rebuilds the deploy step on the "
                                            "new main (attempt 2).")


def test_no_write_login_opens_no_pr(portal):
    """Fail closed: with the write App's login unknown, _our_pr would take a PR by anyone: the step waits instead."""
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    req = lifecycle_request(portal.store, portal.people, T0)
    assert passes(portal, world, 1, writer_config(write_login=None)) == [f"{req.id}: deploy waits: the write App's login is unknown"]
    assert world.pulls == {} and steps(portal, req)[0].state == "planned"


def test_rule_4_is_read_again_before_the_writer_merges_a_suspend(portal, monkeypatch):
    """Rule 4 before every writer merge: the app was decommissioned meanwhile (another request): the suspend is never merged."""
    from portal.store.base import LifecycleState
    world = RepoWorld()
    deploy, _ = deploy_world(world)
    world.checks[(deploy, "deploy-check")] = None
    req = lifecycle_request(portal.store, portal.people, T0)
    passes(portal, world, 1)
    world.checks.clear()
    gone = LifecycleState("acme/notes", "decommissioned", "SCTASK0010009", T0, "r_" + "9" * 32)
    monkeypatch.setattr(portal.store, "lifecycle_state", lambda repo: gone)
    passes(portal, world, 1)
    assert (steps(portal, req)[0].state, steps(portal, req)[0].failure_reason) == ("failed", "decommissioned")
    assert not world.pull(deploy, 1)["merged"] and world.pull(deploy, 1)["state"] == "closed"


# ---- the final review: a decommission brought back, and Stop (I-1, M-1) ----

def brought_back(portal, req):
    """Platform-team procedure 18 by hand: the state back to active, the same request (0012 lets only portal_owner do it)."""
    import dataclasses
    key = req.repo.lower()
    portal.store._lifecycle[key] = dataclasses.replace(portal.store._lifecycle[key], state="active", task=None)


def test_a_retry_after_a_bring_back_never_archives_the_repository_again(portal):
    world, deploy, factory = decommission_world()
    del world.installed["acme/notes"]
    req = to_the_archive(portal, world, deploy)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].state == "failed"
    brought_back(portal, req)
    world.installed["acme/notes"] = 77
    command(portal, req, "retry")
    passes(portal, world, 1, dcfg(world))
    archive = steps(portal, req)[1]
    assert (archive.state, archive.failure_reason) == ("failed", "not_decommissioned_by_this_request")
    assert world.archived == set() and world.token_requests == []
    assert portal.store.lifecycle_state("acme/notes").state == "active", "the bring-back stands"


def test_a_retry_after_a_bring_back_never_marks_the_factory_record_again(portal):
    world, deploy, factory = decommission_world()
    world.checks[(factory, "factory-check")] = "failure"
    req = to_the_factory_pr(portal, world, deploy)
    passes(portal, world, 1, dcfg(world))
    assert (steps(portal, req)[2].state, steps(portal, req)[2].failure_reason) == ("failed", "check_failed: factory-check")
    brought_back(portal, req)
    world.checks.clear()
    command(portal, req, "retry")
    passes(portal, world, 2, dcfg(world))
    factory_step = steps(portal, req)[2]
    assert (factory_step.state, factory_step.failure_reason) == ("failed", "not_decommissioned_by_this_request")
    assert list(world.pulls) == [(deploy, 1), (factory, 1)], "no second factory PR"
    assert b"decommissioned" not in world.files(factory)["intake/acme/notes.yaml"]


def test_an_open_factory_pr_is_closed_once_the_app_is_brought_back(portal):
    world, deploy, factory = decommission_world()
    req = to_the_factory_pr(portal, world, deploy)
    assert steps(portal, req)[2].state == "pr_open"
    brought_back(portal, req)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[2].failure_reason == "not_decommissioned_by_this_request"
    assert world.pull(factory, 1)["state"] == "closed" and not world.pull(factory, 1)["merged"]


def test_a_request_brought_back_before_it_completes_is_never_applied(portal):
    """Every step done, then the bring-back before the pass that completes it: the state is never set back to decommissioned."""
    world, deploy, factory = decommission_world()
    req = to_the_factory_pr(portal, world, deploy)
    world.merge(factory, 1)
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: factory done"]
    assert [s.state for s in steps(portal, req)] == ["done", "done", "done"]
    brought_back(portal, req)
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: failed (not_decommissioned_by_this_request)"]
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "not_decommissioned_by_this_request")
    assert portal.store.lifecycle_state("acme/notes").state == "active" and "completed" not in events(portal, req)
    assert events(portal, req)[-1] == "failed" and "The task stays open" in note(portal, req, "failed")


def test_stop_after_the_deploy_merge_closes_the_remaining_steps_and_leaves_the_task_open(portal):
    world, deploy, factory = decommission_world()
    del world.installed["acme/notes"]
    req = to_the_archive(portal, world, deploy)
    passes(portal, world, 1, dcfg(world))
    assert steps(portal, req)[1].state == "failed"
    command(portal, req, "stop")
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: stopped"]
    assert [s.state for s in steps(portal, req)] == ["done", "closed", "closed"]
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "stopped") and portal.store.open_lifecycle_command(req.id) is None
    assert events(portal, req)[-1] == "stopped" and "completed" not in events(portal, req)
    assert note(portal, req, "stopped") == (
        "Stopped by Admin after the deploy repo PR merged: the writer does no more steps (archive, factory closed). acme/notes stays "
        "removed from the deploy repo and decommissioned in the portal. The repository is not archived. The writer did not mark the "
        "factory's intake record decommissioned. The task stays open for ServiceNow to decide.")
    state = portal.store.lifecycle_state("acme/notes")
    assert (state.state, state.request) == ("decommissioned", req.id), "nothing is undone"
    assert passes(portal, world, 1, dcfg(world)) == [], "a stopped request is no longer the writer's"


def test_stop_closes_an_open_factory_pr_and_deletes_its_branch(portal):
    world, deploy, factory = decommission_world()
    req = to_the_factory_pr(portal, world, deploy)
    command(portal, req, "stop")
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: stopped"]
    assert world.pull(factory, 1)["state"] == "closed" and not world.pull(factory, 1)["merged"]
    assert any(n == 1 and "Stopped in the golden road portal by Admin" in c for _, n, c in world.comments)
    assert world.branches(factory) == set() and [s.state for s in steps(portal, req)] == ["done", "done", "closed"]
    assert "The repository is archived." in note(portal, req, "stopped")


def test_stop_after_the_factory_pr_merged_is_too_late(portal):
    world, deploy, factory = decommission_world()
    req = to_the_factory_pr(portal, world, deploy)
    world.merge(factory, 1)
    command(portal, req, "stop")
    passes(portal, world, 2, dcfg(world))
    assert portal.store.get_request(req.id).state == "merged" and "stopped" not in events(portal, req)
    assert "came too late" in note(portal, req, "command")


def test_stop_after_a_bring_back_still_stops_whatever_merged(portal):
    """Brought back: the request can't complete, so a merged factory PR doesn't make the Stop too late."""
    world, deploy, factory = decommission_world()
    req = to_the_factory_pr(portal, world, deploy)
    brought_back(portal, req)
    world.merge(factory, 1)
    command(portal, req, "stop")
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: stopped"]
    assert portal.store.get_request(req.id).failure_reason == "stopped"


@pytest.mark.parametrize("action", ["suspend", "decommission"])
def test_stop_before_the_deploy_merge_changes_nothing(portal, action):
    world, deploy, _ = decommission_world(lifecycle={})
    world.checks[(deploy, "deploy-check")] = None
    task = {"suspend": "SCTASK0010001", "decommission": "SCTASK0010003"}[action]
    req = lifecycle_request(portal.store, portal.people, T0, action=action, task=task)
    passes(portal, world, 1, dcfg(world))
    command(portal, req, "stop")
    passes(portal, world, 1, dcfg(world))
    assert portal.store.open_lifecycle_command(req.id) is None and portal.store.get_request(req.id).state == "writing"
    assert steps(portal, req)[0].state == "pr_open" and world.pull(deploy, 1)["state"] == "open"
    assert note(portal, req, "command") == ("Stop asked by Admin, but only a decommission whose deploy repo PR merged can be stopped: "
                                            "nothing changed. Abandon it instead.")


def test_stop_waits_when_github_does_not_answer(portal):
    world, deploy, factory = decommission_world()
    req = to_the_factory_pr(portal, world, deploy)
    command(portal, req, "stop")
    handle = world.handle
    world.handle = lambda request: (httpx.Response(502, json={}) if request.url.path.endswith("/pulls") and request.method == "GET"
                                    else handle(request))
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: stop waits: GitHub didn't answer"]
    assert portal.store.open_lifecycle_command(req.id) is not None
    world.handle = handle
    assert passes(portal, world, 1, dcfg(world)) == [f"{req.id}: stopped"]


def test_a_command_open_when_the_recheck_refuses_the_request_is_finished_with_a_note(portal):
    """M-1: an Abandon asked while the request is still approved, then the recheck refuses it: the command is finished, noted."""
    world = RepoWorld()
    deploy_world(world, environments=())
    req = lifecycle_request(portal.store, portal.people, T0, claim=False)
    portal.store.add_lifecycle_command(request_id=req.id, command="abandon", actor_id=portal.people["admin"].id, now=T0)
    passes(portal, world, 1)
    assert portal.store.get_request(req.id).failure_reason == "recheck: nothing_to_suspend"
    assert portal.store.open_lifecycle_command(req.id) is None
    assert note(portal, req, "command") == ("Abandon asked by Admin was not carried out: the platform refused the request before "
                                            "changing anything (nothing_to_suspend).")

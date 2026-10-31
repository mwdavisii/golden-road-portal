import base64
import dataclasses as dc
import datetime as dt
import json

import httpx
import pytest
import yaml
from portal_fakes import T0, FakeGitHub, approved_request, binding_request, fake_directory, intake_trees, transport, writer_config

from portal.github.client import GitHub
from portal.store.base import Conflict
from portal.store.base import RepoAdmin
from portal.writer.render import binding_yaml, intake_yaml
from portal.writer.run import run_once

R = "/repos/acme/scorecard-factory"
FILE = R + "/contents/intake/acme/notes.yaml"
BASE, HEAD, FOREIGN = "1" * 40, "2" * 40, "9" * 40


def branch_ref(req):
    return R + f"/git/ref/heads/portal/request-{req.id}"


def gh_for_write():
    gh = FakeGitHub()
    gh.on("GET", R, (200, gh.fixture("repo_factory")))
    gh.on("GET", R + "/git/ref/heads/main", (200, gh.fixture("ref_main")))
    gh.on("GET", FILE, (404, {"message": "Not Found"}))
    intake_trees(gh, "acme/scorecard-factory", BASE, [])  # the base commit's tree: no record in any spelling
    gh.on("POST", R + "/git/refs", (201, {"ref": "refs/heads/portal/request-x"}))
    gh.on("PUT", FILE, (201, {"content": {"sha": "f" * 40}, "commit": {"sha": HEAD}}))
    gh.on("GET", R + "/pulls", (200, []))
    gh.on("POST", R + "/pulls", (201, gh.fixture("pull_created")))
    gh.on("PATCH", R + "/pulls/12", (200, {}))  # an adopted PR gets the current body
    return gh


def client(gh):
    return GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_" + "w" * 36)


def open_pr(portal):
    req = approved_request(portal.store, portal.people, T0)
    run_once(portal.store, client(gh_for_write()), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "pr_open"
    return req


def landed(req, content=None):
    """The target file as it reads on the default branch after the merge: ours, unless a test says otherwise."""
    raw = intake_yaml(req.final, req.id).encode() if content is None else content
    return (200, {"sha": "f" * 40, "encoding": "base64", "content": base64.b64encode(raw).decode()})


def pr_ref(number, base="main"):
    """A pull request as the REST API lists it inside a check run or a workflow run."""
    return FakeGitHub.fixture("workflow_runs")["workflow_runs"][0]["pull_requests"][0] | {"number": number, "base": {"ref": base, "sha": BASE}}


def workflow_runs(req, prs=None, **changes):
    """The workflow run behind the check suite, as GET /actions/runs?check_suite_id=... answers: ours unless a test changes it."""
    doc = FakeGitHub.fixture("workflow_runs")
    doc["workflow_runs"][0].update({"head_branch": f"portal/request-{req.id}", **({"pull_requests": prs} if prs is not None else {}), **changes})
    return doc


def advancing(pull=None, files=None, commit=None, checks="check_runs_success", merge=True, req=None, on_default=None, workflow=None, pulls=None,
              permissions=None, main=BASE):
    """What GitHub says when the writer comes back to a PR it opened. With req, the check's workflow run and the landed file are there too.
    `main` is the default branch's commit now (the base the PR was opened on, unless a test moves it)."""
    gh = FakeGitHub(permissions=permissions)
    gh.on("GET", R, (200, gh.fixture("repo_factory")))
    gh.on("GET", R + "/git/ref/heads/main", (200, {"object": {"sha": main}}))
    gh.on("GET", R + "/pulls/12", *(pulls or [(200, pull or gh.fixture("pull_open"))]))
    gh.on("GET", R + "/pulls/12/files", (200, files if files is not None else gh.fixture("pull_files")))
    gh.on("GET", R + f"/commits/{HEAD}", (200, commit or gh.fixture("commit_head")))
    gh.on("GET", R + f"/commits/{HEAD}/check-runs", *([(200, gh.fixture(checks))] if isinstance(checks, str) else checks))
    if merge:
        gh.on("PUT", R + "/pulls/12/merge", (200, gh.fixture("merge_ok")))
    if req is not None:
        gh.on("GET", R + "/actions/runs", *(workflow if isinstance(workflow, list) else [(200, workflow or workflow_runs(req))]))
        gh.on("GET", FILE, *(on_default if isinstance(on_default, list) else [on_default or landed(req)]))
    return gh


def failed_with(portal, req, gh, reason):
    run_once(portal.store, client(gh), writer_config(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", reason) and not gh.called("PUT", R + "/pulls/12/merge")


def test_an_approved_registration_becomes_a_pr_with_the_file_and_the_pinned_commits(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    run_once(portal.store, client(gh), writer_config(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.pr_number, after.branch) == ("pr_open", 12, f"portal/request-{req.id}")
    assert (after.head_sha, after.base_sha) == (HEAD, BASE), "the commit the PUT returned, and the base it branched from"
    put = json.loads(gh.called("PUT", FILE)[0]["body"])
    written = yaml.safe_load(base64.b64decode(put["content"]))
    assert written == req.final and put["branch"] == f"portal/request-{req.id}"
    assert "sha" not in put, "a new file: nothing to replace"
    pr = json.loads(gh.called("POST", R + "/pulls")[0]["body"])
    assert pr["base"] == "main" and req.id in pr["body"] and portal.people["admin"].id in pr["body"]
    assert "Purpose and classification checked." in pr["body"]
    # only the allowlisted file (and the base commit's root tree, read for another spelling of it)
    assert {c["path"] for c in gh.calls} <= {R, R + "/git/ref/heads/main", FILE, R + f"/git/trees/{BASE}", R + "/git/refs", R + "/pulls"}


def test_the_pr_merges_the_pinned_commit_once_the_required_check_passes(portal):
    req = open_pr(portal)
    gh = advancing(checks=[(200, {"total_count": 0, "check_runs": []}), (200, FakeGitHub.fixture("check_runs_success"))], req=req)
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "pr_open" and not gh.called("PUT", R + "/pulls/12/merge")  # check still pending
    run_once(portal.store, client(gh), writer_config(), T0)
    merged = portal.store.get_request(req.id)
    assert (merged.state, merged.merge_sha) == ("merged", "3" * 40)
    body = json.loads(gh.called("PUT", R + "/pulls/12/merge")[0]["body"])
    assert body["sha"] == HEAD and body["merge_method"] == "squash"
    assert gh.called("GET", FILE)[-1]["query"]["ref"] == "3" * 40, "the file is read at the merge commit, which can't move, not at a branch tip"
    assert gh.called("GET", R + "/actions/runs")[0]["query"]["check_suite_id"] == "5001", "the check run is traced to its workflow run"


def test_a_commit_pushed_onto_the_branch_after_the_pr_opened_is_never_merged(portal):
    req = open_pr(portal)
    pull = FakeGitHub.fixture("pull_open")
    pull["head"]["sha"] = FOREIGN
    failed_with(portal, req, advancing(pull=pull), "pr_head_changed")


def test_a_pr_someone_else_merged_with_another_commit_is_not_recorded_as_ours(portal):
    req = open_pr(portal)
    pull = FakeGitHub.fixture("pull_merged")
    pull["head"]["sha"] = FOREIGN
    failed_with(portal, req, advancing(pull=pull), "pr_head_changed")


def test_an_extra_file_in_the_pr_stops_the_merge(portal):
    req = open_pr(portal)
    files = FakeGitHub.fixture("pull_files") + [{"filename": ".github/workflows/x.yml", "status": "added"}]
    failed_with(portal, req, advancing(files=files), "pr_files_unexpected")


@pytest.mark.parametrize("files", [
    [{"filename": "intake/acme/notes.yaml", "status": "modified"}], [{"filename": "intake/acme/other.yaml", "status": "added"}], [],
    [{"filename": "intake/acme/notes.yaml", "status": "renamed"}]])
def test_the_pr_must_add_exactly_the_one_target_file(portal, files):
    req = open_pr(portal)
    failed_with(portal, req, advancing(files=files), "pr_files_unexpected")


def test_a_pr_whose_base_branch_changed_is_never_merged(portal):
    req = open_pr(portal)
    pull = FakeGitHub.fixture("pull_open")
    pull["base"]["ref"] = "release"
    failed_with(portal, req, advancing(pull=pull), "pr_base_changed")


def test_a_pinned_commit_that_is_not_one_commit_on_the_base_it_branched_from_is_never_merged(portal):
    req = open_pr(portal)
    commit = FakeGitHub.fixture("commit_head")
    commit["parents"] = [{"sha": "8" * 40}]
    failed_with(portal, req, advancing(commit=commit), "pr_commit_not_ours")


def test_a_merge_commit_with_two_parents_is_never_merged(portal):
    req = open_pr(portal)
    commit = FakeGitHub.fixture("commit_head")
    commit["parents"] = [{"sha": BASE}, {"sha": "8" * 40}]
    failed_with(portal, req, advancing(commit=commit), "pr_commit_not_ours")


def test_a_failed_required_check_fails_the_request_without_merging(portal):
    req = open_pr(portal)
    failed_with(portal, req, advancing(checks="check_runs_failure", req=req), "check_failed: factory-check")


def test_a_check_from_another_app_does_not_count(portal):
    req = open_pr(portal)
    forged = FakeGitHub.fixture("check_runs_success")
    forged["check_runs"][0]["app"]["id"] = 999
    gh = advancing(checks=[(200, forged)], req=req)
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "pr_open" and not gh.called("PUT", R + "/pulls/12/merge")


def test_an_existing_record_fails_closed(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    gh.routes[("GET", "api.github.com", FILE)] = [(200, {"sha": "e" * 40, "content": ""})]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason.startswith("already_registered")
    assert not gh.called("PUT", FILE)


def test_a_recheck_failure_makes_no_github_call(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = FakeGitHub()
    run_once(portal.store, client(gh), writer_config(role_groups={"admin": frozenset({"someone-else"})}), T0)
    assert portal.store.get_request(req.id).failure_reason == "recheck: approver_not_admin" and gh.calls == []


def test_a_record_below_the_floor_makes_no_github_call(portal):
    import dataclasses as dc
    req = approved_request(portal.store, portal.people, T0)
    row = portal.store._evidence[(req.id, req.submission)]
    portal.store._evidence[(req.id, req.submission)] = dc.replace(row, floor_tier="high")  # the stored floor rose after the approval
    portal.store._requests[req.id] = dc.replace(portal.store._requests[req.id], final={**req.final, "floor_tier": "high"})
    gh = FakeGitHub()
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "recheck: below_floor" and gh.calls == []


def test_a_github_error_fails_the_request_with_a_code_written_reason(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    gh.routes[("POST", "api.github.com", R + "/pulls")] = [(422, {"message": "<script>Validation Failed</script>"})]
    run_once(portal.store, client(gh), writer_config(), T0)
    reason = portal.store.get_request(req.id).failure_reason
    assert reason == f"github: 422 POST {R}/pulls" and "Validation" not in reason


# ---- a branch or a PR that already exists (an interrupted run, or someone getting there first) ----

def test_a_branch_that_exists_at_the_base_commit_is_used(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    gh.routes[("POST", "api.github.com", R + "/git/refs")] = [(422, {"message": "Reference already exists"})]
    gh.on("GET", branch_ref(req), (200, {"object": {"sha": BASE}}))
    run_once(portal.store, client(gh), writer_config(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.head_sha) == ("pr_open", HEAD) and gh.called("PUT", FILE)


def our_commit(req, files=None, parents=None):
    return {"sha": HEAD, "parents": parents or [{"sha": BASE}], "files": files if files is not None else [{"filename": "intake/acme/notes.yaml", "status": "added"}]}


def test_an_interrupted_write_resumes_from_the_writer_s_own_commit(portal):
    req = approved_request(portal.store, portal.people, T0)  # left in "writing", as if the writer stopped after its PUT
    content = intake_yaml(req.final, req.id).encode()
    gh = gh_for_write()
    gh.routes[("POST", "api.github.com", R + "/git/refs")] = [(422, {"message": "Reference already exists"})]
    gh.on("GET", branch_ref(req), (200, {"object": {"sha": HEAD}}))
    gh.on("GET", R + f"/commits/{HEAD}", (200, our_commit(req)))
    gh.routes[("GET", "api.github.com", FILE)] = [(404, {}), (200, {"sha": "f" * 40, "encoding": "base64", "content": base64.b64encode(content).decode()})]
    run_once(portal.store, client(gh), writer_config(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.head_sha, after.base_sha) == ("pr_open", HEAD, BASE) and not gh.called("PUT", FILE)


@pytest.mark.parametrize("commit_edit", [
    lambda c: c.update(parents=[{"sha": "8" * 40}]),
    lambda c: c.update(files=[{"filename": "intake/acme/notes.yaml", "status": "added"}, {"filename": ".github/workflows/x.yml", "status": "added"}]),
    lambda c: c.update(files=[{"filename": "intake/acme/notes.yaml", "status": "modified"}]),
    lambda c: c.update(parents=[{"sha": BASE}, {"sha": "8" * 40}]),
], ids=["foreign-parent", "extra-file", "modified", "merge-commit"])
def test_a_branch_that_already_holds_a_foreign_commit_fails_closed(portal, commit_edit):
    req = approved_request(portal.store, portal.people, T0)
    commit = our_commit(req)
    commit_edit(commit)
    gh = gh_for_write()
    gh.routes[("POST", "api.github.com", R + "/git/refs")] = [(422, {"message": "Reference already exists"})]
    gh.on("GET", branch_ref(req), (200, {"object": {"sha": FOREIGN}}))
    gh.on("GET", R + f"/commits/{FOREIGN}", (200, commit))
    run_once(portal.store, client(gh), writer_config(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "branch_not_ours")
    assert not gh.called("PUT", FILE) and not gh.called("POST", R + "/pulls")


def test_a_branch_whose_commit_has_other_content_fails_closed(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    gh.routes[("POST", "api.github.com", R + "/git/refs")] = [(422, {"message": "Reference already exists"})]
    gh.on("GET", branch_ref(req), (200, {"object": {"sha": HEAD}}))
    gh.on("GET", R + f"/commits/{HEAD}", (200, our_commit(req)))
    gh.routes[("GET", "api.github.com", FILE)] = [(404, {}), (200, {"sha": "f" * 40, "encoding": "base64", "content": base64.b64encode(b"owner: evil\n").decode()})]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "branch_not_ours" and not gh.called("PUT", FILE)


@pytest.mark.parametrize("edit", [lambda p: p["head"].update(sha=FOREIGN), lambda p: p["base"].update(ref="release"), lambda p: p.update(state="closed")],
                         ids=["foreign-head", "other-base", "closed"])
def test_an_existing_pr_is_adopted_only_if_it_is_ours(portal, edit):
    req = approved_request(portal.store, portal.people, T0)
    pull = FakeGitHub.fixture("pull_created")
    edit(pull)
    gh = gh_for_write()
    gh.routes[("GET", "api.github.com", R + "/pulls")] = [(200, [pull])]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "pr_not_ours" and not gh.called("POST", R + "/pulls")


def test_an_existing_pr_that_is_ours_is_adopted(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    gh.routes[("GET", "api.github.com", R + "/pulls")] = [(200, [FakeGitHub.fixture("pull_created")])]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "pr_open" and not gh.called("POST", R + "/pulls")


def test_a_pr_the_api_returns_for_another_commit_is_not_taken(portal):
    req = approved_request(portal.store, portal.people, T0)
    pull = FakeGitHub.fixture("pull_created")
    pull["head"]["sha"] = FOREIGN
    gh = gh_for_write()
    gh.routes[("POST", "api.github.com", R + "/pulls")] = [(201, pull)]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "pr_not_ours"


def test_a_pending_or_sent_back_request_is_never_written(portal):
    gh = FakeGitHub()
    run_once(portal.store, client(gh), writer_config(), T0)  # the seeded request is pending
    assert gh.calls == [] and portal.store.get_request(portal.seeds["request"]).state == "pending"
    portal.store.decide(request_id=portal.seeds["request"], actor_id=portal.people["admin"].id, actor_groups=["golden-road-admins"], approve=False,
                        note="Say what it does not do.", final=None, now=T0)
    run_once(portal.store, client(gh), writer_config(), T0)
    assert gh.calls == [] and portal.store.get_request(portal.seeds["request"]).state == "sent_back"


def test_a_pr_that_never_merges_times_out(portal):
    req = open_pr(portal)
    gh = advancing(checks=[(200, {"total_count": 0, "check_runs": []})])
    run_once(portal.store, client(gh), writer_config(), T0 + dt.timedelta(minutes=31))
    assert portal.store.get_request(req.id).failure_reason == "checks_timeout"


def test_the_writer_never_puts_github_text_in_a_failure_reason(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    gh.routes[("GET", "api.github.com", R + "/git/ref/heads/main")] = [(200, {"object": {"sha": "<script>"}})]
    run_once(portal.store, client(gh), writer_config(), T0)
    reason = portal.store.get_request(req.id).failure_reason
    assert "<" not in reason and reason.startswith("github: ")
    req2 = approved_request(portal.store, portal.people, T0, repo="acme/tools")
    gh = gh_for_write()
    gh.routes[("GET", "api.github.com", R + "/git/ref/heads/main")] = [(200, FakeGitHub.fixture("ref_main"))]
    gh.on("GET", R + "/contents/intake/acme/tools.yaml", (404, {}))
    gh.routes[("POST", "api.github.com", R + "/git/refs")] = [(500, {"message": "<script>boom</script>"})]
    run_once(portal.store, client(gh), writer_config(), T0)
    reason = portal.store.get_request(req2.id).failure_reason
    assert "<" not in reason and "boom" not in reason and reason.startswith("github: ")


def test_a_default_branch_that_is_not_a_branch_name_fails_the_request(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = FakeGitHub()
    gh.on("GET", R, (200, {"full_name": "acme/scorecard-factory", "default_branch": "main; <b>x</b>"}))
    run_once(portal.store, client(gh), writer_config(), T0)
    reason = portal.store.get_request(req.id).failure_reason
    assert reason == "github: unexpected default branch" and "<b>" not in reason


def test_a_request_another_process_already_moved_is_skipped_not_fatal(portal, monkeypatch):
    """A Conflict from a mark_* call (the request changed underneath the writer) ends that request's turn, not the pass."""
    first = approved_request(portal.store, portal.people, T0)
    portal.store.replace_repo_admins([], observed_at=T0)
    real = portal.store.mark_failed

    def racing(**kw):
        real(**kw)
        raise Conflict("wrong_state")
    monkeypatch.setattr(portal.store, "mark_failed", racing)
    lines = run_once(portal.store, client(FakeGitHub()), writer_config(role_groups={"admin": frozenset({"x"})}), T0)
    assert any("skipped" in line and first.id in line for line in lines)


# ---- after the merge, the file must really be there: read at the merge commit, and wait for GitHub if it can't say yet ----

def merged_pull():
    return FakeGitHub.fixture("pull_merged")


def test_a_merge_confirmed_at_the_merge_commit_is_recorded(portal):
    req = open_pr(portal)
    gh = advancing(pull=merged_pull(), req=req)
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "merged" and gh.called("GET", FILE)[-1]["query"]["ref"] == "3" * 40


@pytest.mark.parametrize("answer", [(404, {"message": "Not Found"}), (502, {"message": "<html>bad gateway"}), (500, {})], ids=["404", "502", "500"])
def test_a_confirmation_github_cannot_give_yet_leaves_the_request_open_and_is_retried_through_the_merged_path(portal, answer):
    req = open_pr(portal)
    gh = advancing(pulls=[(200, FakeGitHub.fixture("pull_open")), (200, merged_pull())], req=req, on_default=[answer, landed(req)])
    run_once(portal.store, client(gh), writer_config(), T0)  # merges, then can't confirm
    assert gh.called("PUT", R + "/pulls/12/merge")
    pending = portal.store.get_request(req.id)
    assert pending.state == "pr_open" and pending.failure_reason is None, "merge confirmation pending, not failed and not merged"
    run_once(portal.store, client(gh), writer_config(), T0 + dt.timedelta(minutes=1))  # the next pass finds the PR merged and confirms
    assert portal.store.get_request(req.id).state == "merged" and len(gh.called("PUT", R + "/pulls/12/merge")) == 1, "it never merges twice"


def merged_at(minutes):
    """The PR as GitHub shows it once merged `minutes` after the writer opened it."""
    pull = FakeGitHub.fixture("pull_merged")
    pull["merged_at"] = (T0 + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return pull


def test_a_404_at_the_merge_commit_fails_only_after_a_grace_period_measured_from_the_merge(portal):
    req = open_pr(portal)
    gh = advancing(pull=merged_at(0), req=req, on_default=(404, {}))
    run_once(portal.store, client(gh), writer_config(), T0 + dt.timedelta(minutes=29))
    assert portal.store.get_request(req.id).state == "pr_open"
    run_once(portal.store, client(gh), writer_config(), T0 + dt.timedelta(minutes=31))
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "merged_file_missing")


def test_the_grace_period_runs_from_the_merge_not_from_when_the_pr_opened(portal):
    """The PR opened at T0 and merged an hour later: a 404 a minute after the merge is still within the grace period."""
    req = open_pr(portal)
    gh = advancing(pull=merged_at(60), req=req, on_default=(404, {}))
    run_once(portal.store, client(gh), writer_config(), T0 + dt.timedelta(minutes=61))
    assert portal.store.get_request(req.id).state == "pr_open"


@pytest.mark.parametrize("answer", [(500, {}), (502, {"message": "<b>x</b>"}), (503, {}), (403, {"message": "rate limited"})],
                         ids=["500", "502", "503", "403"])
def test_once_merged_a_github_that_does_not_answer_never_fails_the_request_however_long_it_takes(portal, answer):
    req = open_pr(portal)
    gh = advancing(pull=merged_at(0), req=req, on_default=answer)
    for hours in (1, 6, 48):
        run_once(portal.store, client(gh), writer_config(), T0 + dt.timedelta(hours=hours))
        after = portal.store.get_request(req.id)
        assert (after.state, after.failure_reason) == ("pr_open", None)


def test_a_malformed_answer_at_the_merge_commit_is_not_a_failure_either(portal):
    req = open_pr(portal)
    gh = advancing(pull=merged_at(0), req=req, on_default=(200, {"encoding": "base64"}))  # no sha: GitHub said something odd
    run_once(portal.store, client(gh), writer_config(), T0 + dt.timedelta(hours=3))
    assert portal.store.get_request(req.id).state == "pr_open"


def test_checks_that_finish_after_the_merge_timeout_still_merge_and_a_502_on_the_confirmation_only_waits(portal):
    """The PR opened at T0; the check finishes 40 minutes later, past merge_timeout (30 minutes since it opened). The merge succeeds,
    the confirmation read gets a 502: the request stays open, and the next pass, finding the PR merged, confirms it."""
    req = open_pr(portal)
    late = T0 + dt.timedelta(minutes=40)
    gh = advancing(pulls=[(200, FakeGitHub.fixture("pull_open")), (200, merged_at(40))], req=req, on_default=[(502, {"message": "bad gateway"}), landed(req)])
    run_once(portal.store, client(gh), writer_config(), late)
    assert len(gh.called("PUT", R + "/pulls/12/merge")) == 1
    pending = portal.store.get_request(req.id)
    assert (pending.state, pending.failure_reason) == ("pr_open", None)
    run_once(portal.store, client(gh), writer_config(), late + dt.timedelta(minutes=1))
    done = portal.store.get_request(req.id)
    assert (done.state, done.merge_sha) == ("merged", "3" * 40) and len(gh.called("PUT", R + "/pulls/12/merge")) == 1


def test_a_merge_that_left_other_content_is_failed_at_once(portal):
    req = open_pr(portal)
    gh = advancing(pull=merged_pull(), req=req, on_default=landed(req, b"owner: evil\n"))
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "merged_file_differs"


# ---- the check that lets the merge through must be the one for the writer's own PR, traced through its workflow run ----

def stays_open(portal, req, gh):
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "pr_open" and not gh.called("PUT", R + "/pulls/12/merge")


def test_the_check_run_is_traced_to_a_pull_request_workflow_run_for_the_writer_s_branch_and_commit(portal):
    req = open_pr(portal)
    gh = advancing(req=req)
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "merged"
    assert gh.called("GET", R + "/actions/runs")[0]["query"]["check_suite_id"] == "5001"


@pytest.mark.parametrize("changes", [
    {"event": "push"}, {"event": "workflow_dispatch"}, {"event": "pull_request_target"}, {"head_branch": "main"}, {"head_branch": "portal/request-other"},
    {"head_sha": "9" * 40}, {"head_repository": {"full_name": "evil/scorecard-factory"}}, {"head_repository": None}, {"check_suite_id": 5002},
    {"pull_requests": []}, {"pull_requests": "none"},
], ids=lambda c: next(iter(c)) + "=" + str(next(iter(c.values())))[:20])
def test_a_workflow_run_that_is_not_the_pull_request_run_for_this_branch_and_commit_does_not_count(portal, changes):
    req = open_pr(portal)
    stays_open(portal, req, advancing(req=req, workflow=workflow_runs(req, **changes)))


def test_a_pull_request_run_that_lists_our_pr_with_another_base_does_not_count(portal):
    req = open_pr(portal)
    stays_open(portal, req, advancing(req=req, workflow=workflow_runs(req, prs=[pr_ref(12, base="release")])))


def test_another_pr_from_the_same_branch_and_commit_makes_the_run_ambiguous_so_it_does_not_count(portal):
    """GitHub lists every open PR for the head in a run's pull_requests, and the REST API doesn't say which one triggered it.
    A success that could be for a PR into another base is not taken for ours: the writer waits (and times out)."""
    req = open_pr(portal)
    both = [pr_ref(99, base="release"), pr_ref(12)]
    runs = FakeGitHub.fixture("check_runs_success")
    runs["check_runs"][0]["pull_requests"] = both
    stays_open(portal, req, advancing(checks=[(200, runs)], req=req, workflow=workflow_runs(req, prs=both)))


def test_a_failing_run_that_could_be_for_another_pr_does_not_fail_ours(portal):
    req = open_pr(portal)
    both = [pr_ref(99, base="release"), pr_ref(12)]
    runs = FakeGitHub.fixture("check_runs_failure")
    runs["check_runs"][0]["pull_requests"] = both
    stays_open(portal, req, advancing(checks=[(200, runs)], req=req, workflow=workflow_runs(req, prs=both)))


def test_a_failing_run_for_another_pr_does_not_fail_ours_and_our_own_success_still_counts(portal):
    req = open_pr(portal)
    other = FakeGitHub.fixture("check_runs_failure")["check_runs"][0] | {"id": 9002, "check_suite": {"id": 5099}, "pull_requests": [pr_ref(99, base="release")]}
    ours = FakeGitHub.fixture("check_runs_success")["check_runs"][0]
    gh = advancing(checks=[(200, {"total_count": 2, "check_runs": [other, ours]})], req=req)
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "merged"


@pytest.mark.parametrize("edit", [lambda r: r.pop("check_suite"), lambda r: r.update(check_suite={}), lambda r: r.update(check_suite={"id": "5001"}),
                                  lambda r: r.update(pull_requests=[])], ids=["no-suite", "empty-suite", "id-as-text", "no-pr-at-all"])
def test_a_check_run_with_no_usable_suite_does_not_count(portal, edit):
    req = open_pr(portal)
    runs = FakeGitHub.fixture("check_runs_success")
    edit(runs["check_runs"][0])
    gh = advancing(checks=[(200, runs)], req=req)
    stays_open(portal, req, gh)


def test_two_workflow_runs_for_one_check_suite_are_not_trusted(portal):
    req = open_pr(portal)
    doc = workflow_runs(req)
    doc["workflow_runs"].append(dict(doc["workflow_runs"][0], id=8002))
    stays_open(portal, req, advancing(req=req, workflow=doc))


def test_a_github_error_reading_the_workflow_run_fails_with_a_code_written_reason(portal):
    req = open_pr(portal)
    gh = advancing(req=req, workflow=[(500, {"message": "<script>x</script>"})])
    run_once(portal.store, client(gh), writer_config(), T0)
    reason = portal.store.get_request(req.id).failure_reason
    assert reason == f"github: 500 GET {R}/actions/runs" and "<" not in reason and not gh.called("PUT", R + "/pulls/12/merge")


# ---- the PR must come from the target repo itself ----

@pytest.mark.parametrize("repo", ["evil/scorecard-factory", None])
def test_a_pr_from_a_fork_is_never_taken_or_merged(portal, repo):
    req = approved_request(portal.store, portal.people, T0)
    pull = FakeGitHub.fixture("pull_created")
    pull["head"]["repo"] = {"full_name": repo} if repo else None
    gh = gh_for_write()
    gh.routes[("POST", "api.github.com", R + "/pulls")] = [(201, pull)]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "pr_not_ours"


def test_a_pr_that_turns_into_one_from_a_fork_is_never_merged(portal):
    req = open_pr(portal)
    pull = FakeGitHub.fixture("pull_open")
    pull["head"]["repo"] = {"full_name": "evil/scorecard-factory"}
    failed_with(portal, req, advancing(pull=pull, req=req), "pr_not_ours")


def test_an_installation_without_actions_read_fails_the_request_with_the_403_written_by_the_code(portal):
    """GET /actions/runs needs Actions: read on a private repo. Without it the writer can't trace a check, and says so."""
    req = open_pr(portal)
    narrow = {"contents": "write", "pull_requests": "write", "metadata": "read", "checks": "read", "administration": "read"}
    gh = advancing(req=req, permissions=narrow)
    run_once(portal.store, client(gh), writer_config(), T0)
    after = portal.store.get_request(req.id)
    assert after.failure_reason == f"github: 403 GET {R}/actions/runs" and not gh.called("PUT", R + "/pulls/12/merge")


# ---- bindings: apps/<owner>/<repo>/<env>/bindings.yaml and composed.yaml in golden-road-deploy (spec 2026-10-29 GitOps deploy
# repo, correction 3): two contents-API commits on the writer's branch, bindings.yaml first ----

import hashlib  # noqa: E402
from pathlib import Path  # noqa: E402

from portal.compose import compose  # noqa: E402

D = "/repos/acme/golden-road-deploy"
ENV_DIR = "apps/acme/notes/test"
BPATH, CPATH = f"{ENV_DIR}/bindings.yaml", f"{ENV_DIR}/composed.yaml"
BFILE, CFILE = f"{D}/contents/{BPATH}", f"{D}/contents/{CPATH}"
RELFILE, MANFILE = f"{D}/contents/{ENV_DIR}/release.yaml", f"{D}/contents/{ENV_DIR}/base/manifests.yaml"
OLD_BLOB, OLD_COMPOSED_BLOB, MID = "e" * 40, "d" * 40, "4" * 40   # MID: the branch's first commit (bindings.yaml)
IMAGE = "ghcr.io/acme/notes@sha256:" + "b" * 64
MANIFESTS = (Path(__file__).resolve().parent / "fixtures/deploy/manifests.yaml").read_bytes()  # golden-road render's output


def release_doc(**changes):
    doc = {"schema": "deploy-release/v1", "repo": "acme/notes", "environment": "test", "namespace": "notes-test",
           "release": "test-20261029120000-abcdef1", "image": IMAGE, "manifests_sha256": hashlib.sha256(MANIFESTS).hexdigest(),
           "gate_record_sha256": "c" * 64}
    return {**doc, **changes}


def release_yaml(**changes):
    return ("# Written by the bump workflow from the published release test-20261029120000-abcdef1.\n"
            + yaml.safe_dump(release_doc(**changes), sort_keys=False)).encode()


def composed(req, release=None):
    """composed.yaml as golden-road compose writes it from the release and the bindings.yaml the writer writes."""
    rel = release or release_doc()
    return compose(MANIFESTS.decode(), rel["image"], yaml.safe_load(binding_yaml(req.final, req.id)), namespace=rel["namespace"]).encode()


def as_deploy(doc):
    return json.loads(json.dumps(doc).replace("scorecard-factory", "golden-road-deploy").replace("factory-check", "deploy-check"))


def bcfg(**changes):
    return writer_config(**{"directory": fake_directory(), **changes})


def file_answer(raw, sha="f" * 40):
    return (200, {"sha": sha, "encoding": "base64", "content": base64.b64encode(raw).decode()})


NOT_FOUND = (404, {"message": "Not Found"})


def bgh_for_write(existing=None, existing_composed=None, intake=(200, None), release=None, manifests=None):
    """GitHub for the writer's first pass on a binding: the repo's intake record is in the factory (unless `intake` says otherwise),
    the environment has a release pinned (release.yaml and base/manifests.yaml, unless a test changes them), and no bindings.yaml
    or composed.yaml yet, or `existing` / `existing_composed` (bytes) at OLD_BLOB / OLD_COMPOSED_BLOB."""
    gh = FakeGitHub()
    gh.on("GET", R, (200, gh.fixture("repo_factory")))
    gh.on("GET", R + "/git/ref/heads/main", (200, gh.fixture("ref_main")))
    gh.on("GET", FILE, file_answer(b"schema: intake/v2\n") if intake == (200, None) else intake)
    gh.on("GET", D, (200, as_deploy(gh.fixture("repo_factory"))))
    gh.on("GET", D + "/git/ref/heads/main", (200, gh.fixture("ref_main")))
    gh.on("GET", RELFILE, release or file_answer(release_yaml()))
    gh.on("GET", MANFILE, manifests or file_answer(MANIFESTS))
    gh.on("GET", BFILE, NOT_FOUND if existing is None else file_answer(existing, OLD_BLOB))
    gh.on("GET", CFILE, NOT_FOUND if existing_composed is None else file_answer(existing_composed, OLD_COMPOSED_BLOB))
    gh.on("POST", D + "/git/refs", (201, {"ref": "refs/heads/portal/request-x"}))
    gh.on("PUT", BFILE, (201 if existing is None else 200, {"content": {"sha": "f" * 40}, "commit": {"sha": MID}}))
    gh.on("PUT", CFILE, (201 if existing_composed is None else 200, {"content": {"sha": "f" * 40}, "commit": {"sha": HEAD}}))
    gh.on("GET", D + "/pulls", (200, []))
    gh.on("POST", D + "/pulls", (201, as_deploy(gh.fixture("pull_created"))))
    gh.on("PATCH", D + "/pulls/12", (200, {}))  # an adopted PR gets the current body
    return gh


def commit(sha, parent, path, status="added"):
    return (200, {"sha": sha, "parents": [{"sha": parent}], "files": [{"filename": path, "status": status}]})


def badvancing(req, status="added", files=None, landed=None, chain=None, head_composed=None, main=BASE, pull=None, checks="check_runs_success"):
    """GitHub when the writer comes back to a binding's PR: both files `status` (in the request's environment), written as MID
    (bindings.yaml) then HEAD (composed.yaml) on BASE, the check passed, the merge works, and both files land as written."""
    bpath, cpath = (path.replace("/test/", f"/{req.final['environment']}/") for path in (BPATH, CPATH))
    gh = FakeGitHub()
    gh.on("GET", D, (200, as_deploy(gh.fixture("repo_factory"))))
    gh.on("GET", D + "/git/ref/heads/main", (200, {"object": {"sha": main}}))
    gh.on("GET", D + "/pulls/12", (200, pull or as_deploy(gh.fixture("pull_open"))))
    gh.on("GET", D + "/pulls/12/files", (200, files if files is not None else [{"filename": bpath, "status": status},
                                                                               {"filename": cpath, "status": status}]))
    for sha, answer in (chain or {HEAD: commit(HEAD, MID, cpath, status), MID: commit(MID, BASE, bpath, status)}).items():
        gh.on("GET", D + f"/commits/{sha}", answer)
    gh.on("GET", D + f"/commits/{HEAD}/check-runs", (200, as_deploy(gh.fixture(checks))))
    gh.on("PUT", D + "/pulls/12/merge", (200, gh.fixture("merge_ok")))
    gh.on("POST", D + "/issues/12/comments", (201, {"id": 1}))  # a failed request's PR: the reason, then closed
    gh.on("PATCH", D + "/pulls/12", (200, {"state": "closed"}))
    gh.on("GET", D + "/actions/runs", (200, as_deploy(workflow_runs(req))))
    landed = landed or {}
    want_composed = composed(req) if head_composed is None else head_composed
    gh.on("GET", f"{D}/contents/{bpath}", landed.get(bpath, file_answer(binding_yaml(req.final, req.id).encode())))
    gh.on("GET", f"{D}/contents/{cpath}", lambda request: httpx.Response(200, json=file_answer(
        landed.get(cpath, want_composed) if request.url.params.get("ref") == "3" * 40 else want_composed)[1]))
    return gh


def puts(gh):
    return [(c["path"], json.loads(c["body"])) for c in gh.calls if c["method"] == "PUT"]


def test_a_first_binding_writes_bindings_then_composed_in_the_deploy_repo_and_merges_once_deploy_check_passes(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write()
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.pr_number, after.head_sha, after.base_sha) == ("pr_open", 12, HEAD, BASE)
    (first_path, first), (second_path, second) = puts(gh)
    assert (first_path, second_path) == (BFILE, CFILE), "two contents-API commits, bindings.yaml first"
    assert "sha" not in first and "sha" not in second and first["branch"] == second["branch"] == f"portal/request-{req.id}"
    assert base64.b64decode(first["content"]).decode() == binding_yaml(req.final, req.id)
    assert base64.b64decode(second["content"]) == composed(req)
    written = base64.b64decode(second["content"]).decode()
    assert "kind: Namespace" in written.split("---")[0] + written.split("---")[1] and "AZURE_CLIENT_ID" in written and "DATABRICKS_HOST" in written
    pr = json.loads(gh.called("POST", D + "/pulls")[0]["body"])
    assert pr["title"] == f"Connections for acme/notes (test) (portal request {req.id})" and "`sp-notes`" in pr["body"]
    assert {c["path"] for c in gh.calls} <= {R, R + "/git/ref/heads/main", FILE, D, D + "/git/ref/heads/main", RELFILE, MANFILE, BFILE, CFILE,
                                             D + "/git/refs", D + "/pulls"}, "only the two files are written"
    assert not gh.called("PUT", FILE) and gh.called("GET", FILE)[0]["query"]["ref"] == BASE, "the intake record is only read, in the factory"
    for path in (RELFILE, MANFILE, BFILE, CFILE):
        assert gh.called("GET", path)[0]["query"]["ref"] == BASE, f"{path} is read at the base commit"
    gh = badvancing(req)
    run_once(portal.store, client(gh), bcfg(), T0)
    merged = portal.store.get_request(req.id)
    assert (merged.state, merged.merge_sha) == ("merged", "3" * 40)
    assert gh.called("GET", D + f"/commits/{HEAD}/check-runs")[0]["query"]["check_name"] == "deploy-check"
    for path in (BFILE, CFILE):
        assert gh.called("GET", path)[-1]["query"]["ref"] == "3" * 40, f"{path} is confirmed at the merge commit"


PROD = {k: v.replace("/test/", "/prod/") for k, v in {"B": BFILE, "C": CFILE, "REL": RELFILE, "MAN": MANFILE}.items()}
PROD_RELEASE = release_doc(environment="prod", namespace="notes-prod", release="prod-20261029130000-abcdef1")


def pgh_for_write():
    """bgh_for_write for a prod binding: prod has a release pinned, and no bindings.yaml or composed.yaml yet."""
    gh = bgh_for_write()
    gh.on("GET", PROD["REL"], file_answer(yaml.safe_dump(PROD_RELEASE, sort_keys=False).encode()))
    gh.on("GET", PROD["MAN"], file_answer(MANIFESTS))
    gh.on("GET", PROD["B"], NOT_FOUND)
    gh.on("GET", PROD["C"], NOT_FOUND)
    gh.on("PUT", PROD["B"], (201, {"commit": {"sha": MID}}))
    gh.on("PUT", PROD["C"], (201, {"commit": {"sha": HEAD}}))
    return gh


def test_a_prod_binding_writes_prod_s_files_only(portal):
    req = binding_request(portal.store, portal.people, T0, environment="prod")
    p, prod = PROD, PROD_RELEASE
    gh = pgh_for_write()
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "pr_open"
    assert [path for path, _ in puts(gh)] == [p["B"], p["C"]]
    assert base64.b64decode(puts(gh)[1][1]["content"]) == composed(req, prod) and b"name: notes-prod" in composed(req, prod)
    assert not gh.called("GET", BFILE) and not gh.called("GET", RELFILE), "test's files are never read or written"


def test_a_changed_binding_updates_both_files_in_place_with_the_blobs_they_replace(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n", existing_composed=b"# an older composed.yaml\n")
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "pr_open"
    (_, first), (_, second) = puts(gh)
    assert (first["sha"], second["sha"]) == (OLD_BLOB, OLD_COMPOSED_BLOB)
    run_once(portal.store, client(badvancing(req, "modified")), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "merged"


def test_a_composed_yaml_that_would_not_change_is_not_written(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n", existing_composed=composed(req))
    gh.routes[("PUT", "api.github.com", BFILE)] = [(200, {"commit": {"sha": HEAD}})]
    run_once(portal.store, client(gh), bcfg(), T0)
    assert [path for path, _ in puts(gh)] == [BFILE] and portal.store.get_request(req.id).head_sha == HEAD
    gh = badvancing(req, "modified", files=[{"filename": BPATH, "status": "modified"}], chain={HEAD: commit(HEAD, BASE, BPATH, "modified")})
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "merged"


def test_an_unlink_rewrites_the_file_rather_than_deleting_it(portal):
    first = binding_request(portal.store, portal.people, T0)
    portal.store.mark_failed(request_id=first.id, reason="test: make room for the next change", now=T0)
    from portal.binding_record import next_binding
    from portal.ids import new_id
    rid = new_id("r")
    doc = next_binding(current=first.final, repo="acme/notes", org="acme", directory=fake_directory(), person_id=portal.people["owner"].id,
                       request_id=rid, now=T0, environment="test", remove="azure-identity")
    portal.store.create_binding(request_id=rid, person_id=portal.people["owner"].id, final=doc, now=T0, current=first.final)
    req = portal.store.claim_approved(now=T0)
    gh = bgh_for_write(existing=binding_yaml(first.final, first.id).encode(), existing_composed=composed(first))
    run_once(portal.store, client(gh), bcfg(), T0)
    (_, put), (_, cput) = puts(gh)
    assert put["sha"] == OLD_BLOB and not gh.called("DELETE", BFILE)
    written = yaml.safe_load(base64.b64decode(put["content"]))
    assert "azure_identity" not in written and "databricks" not in written and written["request"] == req.id
    assert b"AZURE_CLIENT_ID" not in base64.b64decode(cput["content"]), "composed.yaml loses the identity too"
    assert "neither (unlinked)" in json.loads(gh.called("POST", D + "/pulls")[0]["body"])["body"]
    run_once(portal.store, client(badvancing(req, "modified")), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "merged"


@pytest.mark.parametrize("files", [
    [{"filename": BPATH, "status": "removed"}, {"filename": CPATH, "status": "added"}],
    [{"filename": BPATH, "status": "renamed"}, {"filename": CPATH, "status": "added"}],
    [{"filename": BPATH, "status": "added"}, {"filename": CPATH, "status": "added"}, {"filename": f"{ENV_DIR}/release.yaml", "status": "modified"}],
    [{"filename": BPATH, "status": "added"}, {"filename": f"{ENV_DIR}/kustomization.yaml", "status": "modified"}],
    [{"filename": BPATH, "status": "added"}, {"filename": f"{ENV_DIR}/base/manifests.yaml", "status": "modified"}],
    [{"filename": BPATH, "status": "added"}, {"filename": "clusters/home/flux/acme-notes-test.yaml", "status": "modified"}],
    [{"filename": CPATH, "status": "added"}],
    [{"filename": "apps/acme/notes/prod/bindings.yaml", "status": "added"}, {"filename": "apps/acme/notes/prod/composed.yaml", "status": "added"}],
    [],
], ids=["removed", "renamed", "release", "kustomization", "base", "adapter", "composed-only", "other-environment", "none"])
def test_a_binding_pr_must_change_exactly_its_files(portal, files):
    req = binding_request(portal.store, portal.people, T0)
    run_once(portal.store, client(bgh_for_write()), bcfg(), T0)
    gh = badvancing(req, files=files)
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "pr_files_unexpected") and not gh.called("PUT", D + "/pulls/12/merge")


@pytest.mark.parametrize("chain", [
    {HEAD: commit(HEAD, BASE, CPATH)},                                              # composed.yaml on the base, no bindings commit
    {HEAD: commit(HEAD, MID, CPATH), MID: commit(MID, "8" * 40, BPATH)},             # on another base
    {HEAD: commit(HEAD, MID, BPATH), MID: commit(MID, BASE, CPATH)},                 # the wrong order
    {HEAD: commit(HEAD, MID, CPATH), MID: (200, {"sha": MID, "parents": [{"sha": BASE}],
                                               "files": [{"filename": BPATH, "status": "added"}, {"filename": CPATH, "status": "added"}]})},
    {HEAD: (200, {"sha": HEAD, "parents": [{"sha": MID}, {"sha": BASE}], "files": [{"filename": CPATH, "status": "added"}]})},
], ids=["one-commit", "other-base", "order", "both-in-one", "merge-commit"])
def test_a_binding_pr_whose_commits_are_not_the_writer_s_two_is_never_merged(portal, chain):
    req = binding_request(portal.store, portal.people, T0)
    run_once(portal.store, client(bgh_for_write()), bcfg(), T0)
    gh = badvancing(req, chain=chain)
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "pr_commit_not_ours") and not gh.called("PUT", D + "/pulls/12/merge")


def test_a_stale_blob_sha_fails_the_binding_with_github_s_status_and_opens_no_pr(portal):
    """The file changed on main between the writer's read and its PUT: GitHub answers 409, and the writer neither retries nor opens a PR."""
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n")
    gh.routes[("PUT", "api.github.com", BFILE)] = [(409, {"message": "<b>is at abc but expected</b>"})]
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", f"github: 409 PUT {BFILE}")
    assert len(gh.called("PUT", BFILE)) == 1 and not gh.called("PUT", CFILE) and not gh.called("POST", D + "/pulls")


@pytest.mark.parametrize("missing", ["release", "manifests", "both"])
def test_an_environment_with_no_release_pinned_fails_not_deployed_with_no_branch_put_or_pr(portal, missing):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(release=NOT_FOUND if missing in ("release", "both") else None,
                       manifests=NOT_FOUND if missing in ("manifests", "both") else None)
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "not_deployed")
    assert not gh.called("POST", D + "/git/refs") and not puts(gh) and not gh.called("POST", D + "/pulls")


@pytest.mark.parametrize("release, manifests", [
    (release_yaml(repo="acme/other"), MANIFESTS),                                   # another app's pin
    (release_yaml(repo="acme/Notes"), MANIFESTS),                                   # case differs: deploy-check refuses it too
    (release_yaml(environment="prod", namespace="notes-prod", release="prod-20261029130000-abcdef1"), MANIFESTS),
    (release_yaml(namespace="Notes"), MANIFESTS),                                   # not a valid deploy-release/v1
    (b"schema: deploy-release/v1\nschema: x\n", MANIFESTS),                         # a key twice
    (b"a: &x 1\nb: *x\n", MANIFESTS),                                               # anchors
    (b"\xff\xfe", MANIFESTS),                                                       # not UTF-8
    (release_yaml(), MANIFESTS + b"# edited\n"),                                    # not the release's manifests
    (release_yaml(image="ghcr.io/acme/other@sha256:" + "c" * 64), MANIFESTS),       # compose: the manifests name another image
    (release_yaml(manifests_sha256=hashlib.sha256(b"kind: Pod\n").hexdigest()), b"kind: Pod\n"),   # compose: not the builder's kinds
], ids=["other-repo", "case", "other-environment", "invalid", "duplicate-key", "anchors", "not-utf8", "hash", "image", "kinds"])
def test_what_compose_refuses_fails_compose_refused_with_no_branch_put_or_pr(portal, release, manifests):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(release=file_answer(release), manifests=file_answer(manifests))
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "compose_refused")
    assert not gh.called("POST", D + "/git/refs") and not puts(gh) and not gh.called("POST", D + "/pulls")


def test_a_bindings_yaml_over_deploy_s_cap_is_refused_as_compose_would():
    """golden-road deploy reads bindings.yaml up to MAX_BINDING_BYTES; the writer never writes one it would refuse."""
    from types import SimpleNamespace
    from portal.writer.run import MAX_BINDING_BYTES, Refused, composed_for
    req = SimpleNamespace(repo="acme/notes", draft={"environment": "test"})
    binding = yaml.safe_dump({"schema": "binding/v2", "repo": "acme/notes", "environment": "test", "linked_by": "p_" + "a" * 32,
                              "linked_at": "2026-10-29T15:00:00Z", "request": "r_" + "b" * 32}).encode()
    assert composed_for(req, release_yaml(), MANIFESTS, binding)
    with pytest.raises(Refused, match="compose_refused"):
        composed_for(req, release_yaml(), MANIFESTS, binding + b"#" * (MAX_BINDING_BYTES - len(binding) + 1))
    assert MAX_BINDING_BYTES == 65_536


def test_a_binding_for_a_repo_with_no_intake_record_in_the_factory_fails_not_registered_with_no_branch_put_or_pr(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(intake=NOT_FOUND)
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "not_registered")
    assert gh.called("GET", FILE)[0]["query"]["ref"] == BASE, "read at the factory's default branch commit"
    order = [c["path"] for c in gh.calls]
    assert order.index(D + "/git/ref/heads/main") < order.index(R + "/git/ref/heads/main") < order.index(FILE)
    assert not gh.called("POST", D + "/git/refs") and not puts(gh) and not gh.called("GET", BFILE) and not gh.called("GET", RELFILE)
    assert not gh.called("GET", D + "/pulls") and not gh.called("POST", D + "/pulls")
    assert gh.calls[-1]["path"] == FILE, "no GitHub call after the missing intake record"


def test_an_error_reading_the_intake_record_fails_the_binding_with_a_code_written_reason(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(intake=(502, {"message": "<b>bad gateway</b>"}))
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", f"github: 502 GET {FILE}")
    assert not gh.called("POST", D + "/git/refs") and not puts(gh) and not gh.called("POST", D + "/pulls")


@pytest.mark.parametrize("path", [BPATH, CPATH])
def test_a_merged_binding_either_of_whose_files_reads_otherwise_at_the_merge_commit_is_failed(portal, path):
    req = binding_request(portal.store, portal.people, T0)
    run_once(portal.store, client(bgh_for_write()), bcfg(), T0)
    raw = (binding_yaml(req.final, req.id).encode() if path == BPATH else composed(req)) + b"# extra\n"
    landed = {BPATH: file_answer(raw)} if path == BPATH else {CPATH: raw}
    run_once(portal.store, client(badvancing(req, landed=landed)), bcfg(), T0)
    assert portal.store.get_request(req.id).failure_reason == "merged_file_differs"


def test_the_merge_confirmation_compares_composed_yaml_with_the_commit_the_pr_was_opened_with(portal):
    req = binding_request(portal.store, portal.people, T0)
    run_once(portal.store, client(bgh_for_write()), bcfg(), T0)
    gh = badvancing(req)
    run_once(portal.store, client(gh), bcfg(), T0)
    refs = [c["query"]["ref"] for c in gh.called("GET", CFILE)]
    assert refs == [HEAD, "3" * 40] and portal.store.get_request(req.id).state == "merged"


def test_a_binding_whose_requester_is_no_longer_a_repo_admin_is_failed_with_no_github_call(portal):
    req = binding_request(portal.store, portal.people, T0)
    portal.store.replace_repo_admins([RepoAdmin("acme/tools", "owner-gh", 1001, T0)], observed_at=T0)
    gh = FakeGitHub()
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "recheck: requester_not_admin") and gh.calls == []


def test_a_binding_the_directory_no_longer_backs_is_failed_with_no_github_call(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = FakeGitHub()
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "recheck: no_directory" and gh.calls == []


def crashed(gh, req, tip):
    gh.routes[("POST", "api.github.com", D + "/git/refs")] = [(422, {"message": "Reference already exists"})]
    gh.on("GET", D + f"/git/ref/heads/portal/request-{req.id}", (200, {"object": {"sha": tip}}))


def test_an_interrupted_binding_write_with_both_files_resumes_without_another_write_or_pr(portal):
    """The writer stopped after both PUTs and after opening the PR, before recording it: the next pass adopts both."""
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n", existing_composed=b"# older\n")
    crashed(gh, req, HEAD)
    gh.on("GET", D + f"/commits/{HEAD}", commit(HEAD, MID, CPATH, "modified"))
    gh.on("GET", D + f"/commits/{MID}", commit(MID, BASE, BPATH, "modified"))
    gh.routes[("GET", "api.github.com", BFILE)] = [file_answer(b"# an older binding\n", OLD_BLOB), file_answer(binding_yaml(req.final, req.id).encode())]
    gh.routes[("GET", "api.github.com", CFILE)] = [file_answer(b"# older\n", OLD_COMPOSED_BLOB), file_answer(composed(req))]
    gh.routes[("GET", "api.github.com", D + "/pulls")] = [(200, [as_deploy(FakeGitHub.fixture("pull_open"))])]
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.pr_number, after.head_sha) == ("pr_open", 12, HEAD)
    assert not puts(gh) and not gh.called("POST", D + "/pulls")
    assert gh.called("GET", BFILE)[-1]["query"]["ref"] == gh.called("GET", CFILE)[-1]["query"]["ref"] == HEAD, "both read at the branch tip"


def test_an_interrupted_binding_write_with_only_bindings_yaml_writes_composed_yaml_and_goes_on(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n", existing_composed=b"# older\n")
    crashed(gh, req, MID)
    gh.on("GET", D + f"/commits/{MID}", commit(MID, BASE, BPATH, "modified"))
    gh.routes[("GET", "api.github.com", BFILE)] = [file_answer(b"# an older binding\n", OLD_BLOB), file_answer(binding_yaml(req.final, req.id).encode())]
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.head_sha) == ("pr_open", HEAD)
    [(path, body)] = puts(gh)
    assert path == CFILE and body["sha"] == OLD_COMPOSED_BLOB and base64.b64decode(body["content"]) == composed(req)


@pytest.mark.parametrize("which", ["bindings", "composed"])
def test_an_interrupted_binding_branch_with_other_content_is_not_adopted(portal, which):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n", existing_composed=b"# older\n")
    crashed(gh, req, HEAD)
    gh.on("GET", D + f"/commits/{HEAD}", commit(HEAD, MID, CPATH, "modified"))
    gh.on("GET", D + f"/commits/{MID}", commit(MID, BASE, BPATH, "modified"))
    ours_b, ours_c = binding_yaml(req.final, req.id).encode(), composed(req)
    gh.routes[("GET", "api.github.com", BFILE)] = [file_answer(b"# an older binding\n", OLD_BLOB),
                                                   file_answer(b"schema: binding/v2\n" if which == "bindings" else ours_b)]
    gh.routes[("GET", "api.github.com", CFILE)] = [file_answer(b"# older\n", OLD_COMPOSED_BLOB),
                                                   file_answer(ours_c + b"# foreign\n" if which == "composed" else ours_c)]
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).failure_reason == "branch_not_ours" and not puts(gh)


def test_a_foreign_commit_on_the_binding_branch_is_not_adopted(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write()
    crashed(gh, req, HEAD)
    gh.on("GET", D + f"/commits/{HEAD}", commit(HEAD, MID, f"{ENV_DIR}/release.yaml", "modified"))
    gh.on("GET", D + f"/commits/{MID}", commit(MID, BASE, BPATH))
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).failure_reason == "branch_not_ours" and not puts(gh)


@pytest.mark.parametrize("how", ["main-moved", "behind", "pr-base-sha"])
def test_a_binding_pr_whose_base_moved_fails_base_moved_at_once(portal, how):
    """Both repos' rulesets require the PR to be up to date with main, and the writer never updates its branch: once main has
    moved the PR can't merge, so it fails now with base_moved rather than waiting for merge_timeout."""
    req = binding_request(portal.store, portal.people, T0)
    run_once(portal.store, client(bgh_for_write()), bcfg(), T0)
    gh = badvancing(req, main="8" * 40 if how == "main-moved" else BASE)
    pull = as_deploy(FakeGitHub.fixture("pull_open"))
    if how == "behind":
        pull["mergeable_state"] = "behind"
    if how == "pr-base-sha":
        pull["base"]["sha"] = "8" * 40
    gh.routes[("GET", "api.github.com", D + "/pulls/12")] = [(200, pull)]
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "base_moved")
    assert not gh.called("PUT", D + "/pulls/12/merge") and not gh.called("GET", D + f"/commits/{HEAD}/check-runs")


def test_a_registration_pr_whose_base_moved_fails_base_moved_too(portal):
    req = open_pr(portal)
    failed_with(portal, req, advancing(req=req, main="8" * 40), "base_moved")


def test_a_registration_still_goes_to_the_factory_unchanged(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "pr_open"
    assert [path for path, _ in puts(gh)] == [FILE] and not any(c["path"].startswith(D) for c in gh.calls)


def test_a_registration_s_commit_that_modifies_its_file_is_still_not_ours(portal):
    """Only a binding may modify its file: a registration's interrupted branch whose commit modifies the intake file is foreign."""
    req = approved_request(portal.store, portal.people, T0)
    content = intake_yaml(req.final, req.id).encode()
    gh = gh_for_write()
    gh.routes[("POST", "api.github.com", R + "/git/refs")] = [(422, {"message": "Reference already exists"})]
    gh.on("GET", branch_ref(req), (200, {"object": {"sha": HEAD}}))
    gh.on("GET", R + f"/commits/{HEAD}", (200, our_commit(req, files=[{"filename": "intake/acme/notes.yaml", "status": "modified"}])))
    gh.routes[("GET", "api.github.com", FILE)] = [(404, {}), file_answer(content)]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "branch_not_ours"


# ---- a binding's Delinea secrets (spec 2026-10-29 Delinea secrets): the writer re-checks them as the platform account, and an env
# name the app's own builder-spec sets is compose's to refuse, so it fails compose_refused before anything is written ----

from portal.secretserver_mock import load_mock  # noqa: E402


def test_a_secrets_only_binding_is_written_without_the_directory(portal):
    req = binding_request(portal.store, portal.people, T0, identity=False, secrets=[(1042, "password", "NOTES_DB_PASSWORD")])
    gh = bgh_for_write()
    run_once(portal.store, client(gh), writer_config(secret_server=load_mock()), T0)
    assert portal.store.get_request(req.id).state == "pr_open", portal.store.get_request(req.id).failure_reason
    (_, first), (_, second) = puts(gh)
    assert base64.b64decode(second["content"]) == composed(req)
    written = base64.b64decode(second["content"]).decode()
    assert "kind: ExternalSecret" in written and "secretKeyRef" in written and "AZURE_CLIENT_ID" not in written
    assert "<mock>" not in written and "<mock>" not in base64.b64decode(first["content"]).decode()
    pr = json.loads(gh.called("POST", D + "/pulls")[0]["body"])
    assert "checked against `mock-secret-server` as its platform account `golden-road-eso`" in pr["body"]


def test_a_secret_named_like_an_env_the_app_sets_fails_compose_refused_and_nothing_is_written(portal):
    """NOTES_DB_URL is allowed as a name (env_names), but the release's manifests already set it: compose refuses it."""
    req = binding_request(portal.store, portal.people, T0, secrets=[(1042, "url", "NOTES_DB_URL")])
    gh = bgh_for_write()
    run_once(portal.store, client(gh), bcfg(secret_server=load_mock()), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "compose_refused")
    assert puts(gh) == [] and not gh.called("POST", D + "/git/refs") and not gh.called("POST", D + "/pulls")


def test_a_binding_whose_secret_was_unshared_fails_its_recheck_before_github_is_asked(portal):
    import json as _json
    from portal.secretserver_mock import MOCK, parse
    req = binding_request(portal.store, portal.people, T0, secrets=[(1042, "password", "NOTES_DB_PASSWORD")])
    doc = _json.loads(MOCK.read_text(encoding="utf-8"))
    next(s for s in doc["secrets"] if s["id"] == 1042)["shared_with"] = []
    gh = bgh_for_write()
    run_once(portal.store, client(gh), bcfg(secret_server=parse(doc)), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "recheck: secret_not_shared") and gh.calls == []


# ---- prod bindings (spec 2026-10-30, P1-P6): the writer opens and checks the PR as for test, but never merges it. A person merges
# it through main-review (code-owner review of bindings.yaml), and the writer's next pass records the merge ----

def open_prod_binding(portal):
    req = binding_request(portal.store, portal.people, T0, environment="prod")
    gh = pgh_for_write()
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "pr_open"
    return req, gh


def test_a_prod_binding_s_pr_says_a_code_owner_reviews_it_and_the_writer_never_merges_it(portal):
    req, gh = open_prod_binding(portal)
    body = json.loads(gh.called("POST", D + "/pulls")[0]["body"])["body"]
    assert "The writer never merges a prod binding" in body and "code owner" in body
    assert "It merges once the required checks pass." not in body


def test_a_prod_binding_with_passing_checks_is_never_merged_and_waits_for_review_without_a_timeout(portal):
    req, _ = open_prod_binding(portal)
    gh = badvancing(req)
    for later in (dt.timedelta(0), dt.timedelta(minutes=31), dt.timedelta(days=30)):  # long past merge_timeout: no timeout once checks pass
        assert run_once(portal.store, client(gh), bcfg(), T0 + later) == [f"{req.id}: waiting for review"]
        after = portal.store.get_request(req.id)
        assert (after.state, after.failure_reason, after.merge_sha) == ("pr_open", None, None)
    assert not gh.called("PUT", D + "/pulls/12/merge"), "the writer never calls the merge API for prod"
    assert gh.called("GET", D + f"/commits/{HEAD}/check-runs"), "the checks are still read on every pass"


def test_a_prod_binding_a_person_merged_is_recorded_with_its_merge_commit(portal):
    req, _ = open_prod_binding(portal)
    gh = badvancing(req, pull=as_deploy(merged_pull()))
    run_once(portal.store, client(gh), bcfg(), T0 + dt.timedelta(days=3))
    after = portal.store.get_request(req.id)
    assert (after.state, after.merge_sha) == ("merged", "3" * 40) and not gh.called("PUT", D + "/pulls/12/merge")
    for path in (PROD["B"], PROD["C"]):
        assert gh.called("GET", path)[-1]["query"]["ref"] == "3" * 40, f"{path} is confirmed at the merge commit"


@pytest.mark.parametrize("edit, reason", [
    (lambda p: p["head"].update(sha=FOREIGN), "pr_head_changed"),  # a reviewer updated the branch: the person links again
    (lambda p: p.update(state="closed"), "pr_closed"),
    (lambda p: p.update(mergeable_state="behind"), "base_moved"),
], ids=["head-changed", "closed", "base-moved"])
def test_a_prod_binding_waiting_for_review_still_fails_as_any_pr_does(portal, edit, reason):
    req, _ = open_prod_binding(portal)
    assert run_once(portal.store, client(badvancing(req)), bcfg(), T0) == [f"{req.id}: waiting for review"]
    pull = as_deploy(FakeGitHub.fixture("pull_open"))
    edit(pull)
    gh = badvancing(req, pull=pull)
    run_once(portal.store, client(gh), bcfg(), T0 + dt.timedelta(days=2))
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", reason) and not gh.called("PUT", D + "/pulls/12/merge")


def test_a_prod_binding_whose_check_failed_or_whose_files_changed_fails(portal):
    req, _ = open_prod_binding(portal)
    run_once(portal.store, client(badvancing(req, checks="check_runs_failure")), bcfg(), T0)
    assert portal.store.get_request(req.id).failure_reason == "check_failed: deploy-check"
    req, _ = open_prod_binding(portal)
    extra = [{"filename": PROD["B"].split("/contents/")[1], "status": "added"}, {"filename": PROD["C"].split("/contents/")[1], "status": "added"},
             {"filename": "apps/acme/notes/prod/release.yaml", "status": "modified"}]
    run_once(portal.store, client(badvancing(req, files=extra)), bcfg(), T0)
    assert portal.store.get_request(req.id).failure_reason == "pr_files_unexpected"


def test_a_prod_binding_waits_on_pending_checks_past_merge_timeout_too(portal):
    """A re-run of deploy-check on a prod PR waiting for review leaves no completed run for a while; a person merges a prod PR, so
    it never times out, pending checks included. A test binding keeps merge_timeout for checks that never finish."""
    req, _ = open_prod_binding(portal)
    gh = badvancing(req)
    gh.routes[("GET", "api.github.com", D + f"/commits/{HEAD}/check-runs")] = [(200, {"total_count": 0, "check_runs": []})]
    for later in (dt.timedelta(minutes=31), dt.timedelta(days=30)):
        assert run_once(portal.store, client(gh), bcfg(), T0 + later) == [f"{req.id}: waiting for checks"]
        assert (portal.store.get_request(req.id).state, portal.store.get_request(req.id).failure_reason) == ("pr_open", None)


def test_a_test_binding_whose_checks_never_finish_still_times_out(portal):
    test = binding_request(portal.store, portal.people, T0)
    run_once(portal.store, client(bgh_for_write()), bcfg(), T0)
    gh = badvancing(test)
    gh.routes[("GET", "api.github.com", D + f"/commits/{HEAD}/check-runs")] = [(200, {"total_count": 0, "check_runs": []})]
    run_once(portal.store, client(gh), bcfg(), T0 + dt.timedelta(minutes=31))
    assert portal.store.get_request(test.id).failure_reason == "checks_timeout"


@pytest.mark.parametrize("case", [("prod", "test", False), ("test", "prod", True)], ids=["draft-prod", "draft-test"])
def test_the_environment_that_waits_for_review_is_the_stored_draft_s_not_the_pr_s(portal, case):
    """P6: whether the writer merges comes from target_for (the stored draft, checked against ENVIRONMENTS), never from the record's
    final or the PR. A draft that says prod waits even when the final, the PR title and the PR body say test; one that says test
    merges even when the PR title and body say prod."""
    from portal.writer.allowlist import target_for
    from portal.writer.run import _advance
    for stored_env, shown_env, merges in [case]:
        req = binding_request(portal.store, portal.people, T0, environment=stored_env)
        run_once(portal.store, client(pgh_for_write() if stored_env == "prod" else bgh_for_write()), bcfg(), T0)
        stored = portal.store.get_request(req.id)
        assert stored.state == "pr_open" and target_for("acme", stored).environment == stored_env
        shown = dc.replace(stored, final={**stored.final, "environment": shown_env})
        assert target_for("acme", shown).environment == stored_env
        if merges:  # merged, the files are confirmed against the record as written: only the PR says prod
            shown = stored
        pull = as_deploy(FakeGitHub.fixture("pull_open")) | {
            "title": f"Connections for acme/notes ({shown_env}) (portal request {req.id})",
            "body": f"Portal request `{req.id}`: the connections of `acme/notes` in `{shown_env}`."}
        gh = badvancing(dc.replace(stored, final={**stored.final, "environment": stored_env}), pull=pull)
        line = _advance(portal.store, client(gh), bcfg(), shown, T0)
        assert bool(gh.called("PUT", D + "/pulls/12/merge")) is merges, line
        assert line == (f"{req.id}: merged" if merges else f"{req.id}: waiting for review")
    staging = dc.replace(stored, draft={**stored.draft, "environment": "staging"})
    assert target_for("acme", staging) is None


def test_a_request_with_no_target_fails_bad_target_and_the_pass_goes_on(portal):
    from portal.writer.run import _advance
    req, _ = open_prod_binding(portal)
    stored = portal.store.get_request(req.id)
    gh = badvancing(stored)
    line = _advance(portal.store, client(gh), bcfg(), dc.replace(stored, draft={**stored.draft, "environment": "staging"}), T0)
    after = portal.store.get_request(req.id)
    assert line == f"{req.id}: failed (bad_target)" and (after.state, after.failure_reason) == ("failed", "bad_target") and gh.calls == []


def test_writer_merges_fails_closed():
    from portal.writer.allowlist import Target, writer_merges
    assert writer_merges(Target("acme/scorecard-factory", "intake/acme/notes.yaml", "intake.v2"))
    assert writer_merges(Target("acme/golden-road-deploy", "b", "binding.v2", also="c", environment="test"))
    for environment in ("prod", None, "staging", "TEST"):
        assert not writer_merges(Target("acme/golden-road-deploy", "b", "binding.v2", also="c", environment=environment)), environment
    assert not writer_merges(Target("acme/golden-road-deploy", "b", "composed", environment="test"))
    assert not writer_merges(Target("acme/scorecard-factory", "intake/acme/notes.yaml", "intake.v2", environment="prod"))


# ---- a failed request's PR: the writer never merges it, so it says why on the PR and closes it, best effort ----

def comments(gh, repo=D):
    return [json.loads(c["body"])["body"] for c in gh.calls if c["method"] == "POST" and c["path"] == f"{repo}/issues/12/comments"]


@pytest.mark.parametrize("environment", ["test", "prod"])
def test_a_failed_binding_s_pr_gets_the_reason_and_is_closed(portal, environment):
    req = binding_request(portal.store, portal.people, T0, environment=environment)
    run_once(portal.store, client(pgh_for_write() if environment == "prod" else bgh_for_write()), bcfg(), T0)
    gh = badvancing(req, main="8" * 40)
    assert run_once(portal.store, client(gh), bcfg(), T0) == [f"{req.id}: failed (base_moved)"]
    assert portal.store.get_request(req.id).failure_reason == "base_moved"
    [comment] = comments(gh)
    assert "`base_moved`" in comment and "never merges" in comment and "closes" in comment
    assert [json.loads(c["body"]) for c in gh.called("PATCH", D + "/pulls/12")] == [{"state": "closed"}]
    assert not gh.called("PUT", D + "/pulls/12/merge")


def test_a_failed_registration_s_pr_is_closed_too(portal):
    req = open_pr(portal)
    pull = FakeGitHub.fixture("pull_open")
    pull["head"]["sha"] = FOREIGN
    gh = advancing(pull=pull)
    gh.on("POST", R + "/issues/12/comments", (201, {"id": 1}))
    gh.on("PATCH", R + "/pulls/12", (200, {"state": "closed"}))
    failed_with(portal, req, gh, "pr_head_changed")
    assert "`pr_head_changed`" in comments(gh, R)[0] and json.loads(gh.called("PATCH", R + "/pulls/12")[0]["body"]) == {"state": "closed"}


@pytest.mark.parametrize("answers", [
    {"POST": (500, {}), "PATCH": (500, {})}, {"POST": (403, {"message": "x"}), "PATCH": (200, {})}, {"POST": (201, {}), "PATCH": (422, {})}, {},
], ids=["both-500", "comment-403", "close-422", "no-answer"])
def test_a_comment_or_close_that_fails_never_changes_the_reason_or_stops_the_pass(portal, answers):
    req, _ = open_prod_binding(portal)
    other = binding_request(portal.store, portal.people, T0)  # claimed after: written on the same pass
    gh = badvancing(req, main="8" * 40)
    for method, path in (("POST", D + "/issues/12/comments"), ("PATCH", D + "/pulls/12")):
        gh.routes.pop((method, "api.github.com", path))
        if method in answers:
            gh.on(method, path, answers[method])
    for route, answer in bgh_for_write().routes.items():
        gh.routes.setdefault(route, list(answer))
    lines = run_once(portal.store, client(gh), bcfg(), T0)
    assert f"{req.id}: failed (base_moved)" in lines and portal.store.get_request(req.id).failure_reason == "base_moved"
    assert portal.store.get_request(other.id).state == "pr_open", "the pass went on"


def test_a_merged_pr_whose_confirmation_fails_is_not_closed(portal):
    req = open_pr(portal)
    gh = advancing(pull=merged_pull(), req=req, on_default=landed(req, b"owner: evil\n"))
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "merged_file_differs"
    assert not gh.called("PATCH", R + "/pulls/12") and not gh.called("POST", R + "/issues/12/comments")


# ---- an adopted PR (an interrupted run) gets the current body, so a pre-0.4.2 prod PR stops saying the writer merges it ----

def test_an_adopted_pr_whose_body_differs_is_updated_to_the_current_body(portal):
    req = binding_request(portal.store, portal.people, T0, environment="prod")
    gh = pgh_for_write()
    old = as_deploy(gh.fixture("pull_created")) | {"body": "Written by the golden road portal's writer. It merges once the required checks pass."}
    gh.routes[("GET", "api.github.com", D + "/pulls")] = [(200, [old])]
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "pr_open" and not gh.called("POST", D + "/pulls")
    [patch] = [json.loads(c["body"]) for c in gh.called("PATCH", D + "/pulls/12")]
    assert list(patch) == ["body"] and "The writer never merges a prod binding" in patch["body"]


def test_an_adopted_pr_with_the_current_body_is_left_alone(portal):
    from portal.writer.render import pr_body
    from portal.writer.allowlist import target_for
    req = binding_request(portal.store, portal.people, T0, environment="prod")
    gh = pgh_for_write()
    body = pr_body(req, portal.store.get_person(req.requested_by), None, target_for("acme", req))
    gh.routes[("GET", "api.github.com", D + "/pulls")] = [(200, [as_deploy(gh.fixture("pull_created")) | {"body": body}])]
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "pr_open" and not gh.called("PATCH", D + "/pulls/12")


def test_an_adopted_pr_whose_body_update_fails_is_still_taken(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write()
    gh.routes[("GET", "api.github.com", D + "/pulls")] = [(200, [as_deploy(gh.fixture("pull_created")) | {"body": "old"}])]
    gh.routes[("PATCH", "api.github.com", D + "/pulls/12")] = [(500, {})]
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "pr_open"


def test_test_bindings_and_registrations_still_merge_once_the_checks_pass(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write()
    run_once(portal.store, client(gh), bcfg(), T0)
    body = json.loads(gh.called("POST", D + "/pulls")[0]["body"])["body"]
    assert "It merges once the required checks pass." in body and "never merges" not in body
    gh = badvancing(req)
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "merged" and len(gh.called("PUT", D + "/pulls/12/merge")) == 1
    reg = open_pr(portal)
    gh = advancing(req=reg)
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(reg.id).state == "merged" and gh.called("PUT", R + "/pulls/12/merge")


from portal_fakes import approved_change, record_view  # noqa: E402

TOOLS = R + "/contents/intake/acme/tools.yaml"


def gh_for_change(current_sha="c" * 40, found=True, listed=("tools.yaml",)):
    gh = FakeGitHub()
    intake_trees(gh, "acme/scorecard-factory", BASE, [f"intake/acme/{n}" for n in (listed if found else ())])
    gh.on("GET", R, (200, gh.fixture("repo_factory")))
    gh.on("GET", R + "/git/ref/heads/main", (200, gh.fixture("ref_main")))
    gh.on("GET", TOOLS, (200, {"type": "file", "sha": current_sha, "encoding": "base64", "content": ""}) if found else (404, {"message": "Not Found"}))
    gh.on("POST", R + "/git/refs", (201, {"ref": "refs/heads/portal/request-x"}))
    gh.on("PUT", TOOLS, (200, {"content": {"sha": "f" * 40}, "commit": {"sha": HEAD}}))
    gh.on("GET", R + "/pulls", (200, []))
    gh.on("POST", R + "/pulls", (201, gh.fixture("pull_created")))
    return gh


def test_a_change_replaces_the_record_the_approver_saw_in_place(portal):
    req = approved_change(portal.store, portal.people, T0, record=record_view(portal.people["owner"].id))
    gh = gh_for_change()
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).state == "pr_open"
    [put] = gh.called("PUT", TOOLS)
    body = json.loads(put["body"])
    assert body["sha"] == "c" * 40 and body["message"] == f"Change the registration of acme/tools (portal request {req.id})"
    assert yaml.safe_load(base64.b64decode(body["content"])) == req.final
    pr = json.loads(gh.called("POST", R + "/pulls")[0]["body"])
    assert pr["title"].startswith("Change the registration of acme/tools") and "- Review by: `2026-11-15` → `2027-10-23`" in pr["body"]


@pytest.mark.parametrize("sha, found", [("d" * 40, True), ("c" * 40, False)])
def test_a_record_that_moved_or_went_fails_record_changed_and_writes_nothing(portal, sha, found):
    req = approved_change(portal.store, portal.people, T0, record=record_view(portal.people["owner"].id))
    gh = gh_for_change(sha, found)
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "record_changed"
    assert not gh.called("POST", R + "/git/refs") and not gh.called("PUT", TOOLS)


def test_a_v1_record_is_replaced_by_a_complete_v2(portal):
    current = record_view("platform-team", schema="intake/v1", by_portal=False)
    del current["review_by"]
    req = approved_change(portal.store, portal.people, T0, record=current)
    gh = gh_for_change()
    run_once(portal.store, client(gh), writer_config(), T0)
    written = yaml.safe_load(base64.b64decode(json.loads(gh.called("PUT", TOOLS)[0]["body"])["content"]))
    assert written["schema"] == "intake/v2" and written["owner"] == written["registered_by"] == portal.people["owner"].id
    assert (written["approved_by"], written["request"]) == (portal.people["admin"].id, req.id)
    assert "- Record: `intake/v1` → `intake/v2`" in json.loads(gh.called("POST", R + "/pulls")[0]["body"])["body"]


def test_a_change_by_another_repo_admin_makes_them_the_owner(portal):
    req = approved_change(portal.store, portal.people, T0, record=record_view("p_" + "7" * 32))
    gh = gh_for_change()
    run_once(portal.store, client(gh), writer_config(), T0)
    written = yaml.safe_load(base64.b64decode(json.loads(gh.called("PUT", TOOLS)[0]["body"])["content"]))
    assert written["owner"] == portal.people["owner"].id
    assert f"- Owner: `{'p_' + '7' * 32}` → `{portal.people['owner'].id}`" in json.loads(gh.called("POST", R + "/pulls")[0]["body"])["body"]


@pytest.mark.parametrize("status, outcome", [("modified", ("merged", None)), ("added", ("failed", "pr_commit_not_ours"))])
def test_a_change_s_pr_must_modify_the_record(portal, status, outcome):
    req = approved_change(portal.store, portal.people, T0, record=record_view(portal.people["owner"].id))
    run_once(portal.store, client(gh_for_change()), writer_config(), T0)
    req = portal.store.get_request(req.id)
    commit = {**FakeGitHub.fixture("commit_head"), "files": [{"filename": "intake/acme/tools.yaml", "status": status}]}
    gh = advancing(commit=commit, files=[{"filename": "intake/acme/tools.yaml", "status": status, "additions": 3, "deletions": 3}], req=req)
    gh.on("GET", TOOLS, landed(req))
    run_once(portal.store, client(gh), writer_config(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == outcome


# ---- more cases ----

def test_a_change_s_put_github_refuses_with_a_conflict_is_record_conflict(portal):
    req = approved_change(portal.store, portal.people, T0, record=record_view(portal.people["owner"].id))
    gh = gh_for_change()
    gh.routes[("PUT", "api.github.com", TOOLS)] = [(409, {"message": "sha does not match"})]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "record_conflict"


def test_a_change_whose_record_has_another_spelling_too_writes_nothing(portal):
    req = approved_change(portal.store, portal.people, T0, record=record_view(portal.people["owner"].id))
    gh = gh_for_change(listed=("tools.yaml", "Tools.yaml"))
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "record_case_mismatch" and not gh.called("PUT", TOOLS)


def test_a_first_registration_meeting_a_record_in_another_case_writes_nothing(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    intake_trees(gh, "acme/scorecard-factory", BASE, ["intake/ACME/notes.yaml"])
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "record_case_mismatch" and not gh.called("PUT", FILE)


def test_a_directory_where_the_record_goes_is_refused_by_name(portal):
    req = approved_request(portal.store, portal.people, T0)
    gh = gh_for_write()
    gh.routes[("GET", "api.github.com", FILE)] = [(200, [{"name": "x.yaml", "type": "file"}])]
    run_once(portal.store, client(gh), writer_config(), T0)
    assert portal.store.get_request(req.id).failure_reason == "not_a_file" and not gh.called("PUT", FILE)

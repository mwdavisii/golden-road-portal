import base64
import datetime as dt
import json

import httpx
import pytest
import yaml
from portal_fakes import T0, FakeGitHub, approved_request, binding_request, fake_directory, transport, writer_config

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
    gh.on("POST", R + "/git/refs", (201, {"ref": "refs/heads/portal/request-x"}))
    gh.on("PUT", FILE, (201, {"content": {"sha": "f" * 40}, "commit": {"sha": HEAD}}))
    gh.on("GET", R + "/pulls", (200, []))
    gh.on("POST", R + "/pulls", (201, gh.fixture("pull_created")))
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
              permissions=None):
    """What GitHub says when the writer comes back to a PR it opened. With req, the check's workflow run and the landed file are there too."""
    gh = FakeGitHub(permissions=permissions)
    gh.on("GET", R, (200, gh.fixture("repo_factory")))
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
    assert {c["path"] for c in gh.calls} <= {R, R + "/git/ref/heads/main", FILE, R + "/git/refs", R + "/pulls"}, "only the allowlisted file"


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


# ---- bindings: bindings/<owner>/<repo>.yaml, added the first time and updated in place after that ----

BFILE = R + "/contents/bindings/acme/notes.yaml"
BPATH = "bindings/acme/notes.yaml"
OLD_BLOB = "e" * 40


def bcfg(**changes):
    return writer_config(**{"directory": fake_directory(), **changes})


def file_answer(raw, sha="f" * 40):
    return (200, {"sha": sha, "encoding": "base64", "content": base64.b64encode(raw).decode()})


def bgh_for_write(existing=None, intake=(200, None)):
    """GitHub for the writer's first pass on a binding: the repo's intake record is in the factory (unless `intake` says otherwise);
    no binding file on main yet, or `existing` (bytes) at blob OLD_BLOB."""
    gh = FakeGitHub()
    gh.on("GET", R, (200, gh.fixture("repo_factory")))
    gh.on("GET", R + "/git/ref/heads/main", (200, gh.fixture("ref_main")))
    gh.on("GET", FILE, file_answer(b"schema: intake/v2\n") if intake == (200, None) else intake)
    gh.on("GET", BFILE, (404, {"message": "Not Found"}) if existing is None else file_answer(existing, OLD_BLOB))
    gh.on("POST", R + "/git/refs", (201, {"ref": "refs/heads/portal/request-x"}))
    gh.on("PUT", BFILE, (201 if existing is None else 200, {"content": {"sha": "f" * 40}, "commit": {"sha": HEAD}}))
    gh.on("GET", R + "/pulls", (200, []))
    gh.on("POST", R + "/pulls", (201, gh.fixture("pull_created")))
    return gh


def badvancing(req, status="added", files=None, landed_raw=None):
    """GitHub when the writer comes back to a binding's PR: its one file `status`, the check passed, the merge works."""
    gh = FakeGitHub()
    gh.on("GET", R, (200, gh.fixture("repo_factory")))
    gh.on("GET", R + "/pulls/12", (200, gh.fixture("pull_open")))
    gh.on("GET", R + "/pulls/12/files", (200, files if files is not None else [{"filename": BPATH, "status": status}]))
    gh.on("GET", R + f"/commits/{HEAD}", (200, {"sha": HEAD, "parents": [{"sha": BASE}], "files": [{"filename": BPATH, "status": status}]}))
    gh.on("GET", R + f"/commits/{HEAD}/check-runs", (200, gh.fixture("check_runs_success")))
    gh.on("PUT", R + "/pulls/12/merge", (200, gh.fixture("merge_ok")))
    gh.on("GET", R + "/actions/runs", (200, workflow_runs(req)))
    gh.on("GET", BFILE, file_answer(binding_yaml(req.final, req.id).encode() if landed_raw is None else landed_raw))
    return gh


def test_a_first_binding_adds_its_file_and_merges_once_the_check_passes(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write()
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.pr_number, after.head_sha, after.base_sha) == ("pr_open", 12, HEAD, BASE)
    put = json.loads(gh.called("PUT", BFILE)[0]["body"])
    assert "sha" not in put and put["branch"] == f"portal/request-{req.id}"
    assert base64.b64decode(put["content"]).decode() == binding_yaml(req.final, req.id)
    pr = json.loads(gh.called("POST", R + "/pulls")[0]["body"])
    assert pr["title"] == f"Connections for acme/notes (portal request {req.id})" and "`sp-notes`" in pr["body"] and "`wh-notes`" in pr["body"]
    assert {c["path"] for c in gh.calls} <= {R, R + "/git/ref/heads/main", FILE, BFILE, R + "/git/refs", R + "/pulls"}, "only the binding's file"
    assert not gh.called("PUT", FILE) and gh.called("GET", FILE)[0]["query"]["ref"] == BASE, "the intake record is only read, at the base"
    gh = badvancing(req, "added")
    run_once(portal.store, client(gh), bcfg(), T0)
    merged = portal.store.get_request(req.id)
    assert (merged.state, merged.merge_sha) == ("merged", "3" * 40)
    assert gh.called("GET", BFILE)[-1]["query"]["ref"] == "3" * 40, "confirmed at the merge commit"


def test_a_changed_binding_updates_the_file_in_place_with_the_blob_it_replaces(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\nschema: binding/v1\n")
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "pr_open"
    assert gh.called("GET", BFILE)[0]["query"]["ref"] == BASE
    put = json.loads(gh.called("PUT", BFILE)[0]["body"])
    assert put["sha"] == OLD_BLOB and base64.b64decode(put["content"]).decode() == binding_yaml(req.final, req.id)
    run_once(portal.store, client(badvancing(req, "modified")), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "merged"


def test_an_unlink_rewrites_the_file_rather_than_deleting_it(portal):
    first = binding_request(portal.store, portal.people, T0)
    portal.store.mark_failed(request_id=first.id, reason="test: make room for the next change", now=T0)
    from portal.binding_record import next_binding
    from portal.ids import new_id
    rid = new_id("r")
    doc = next_binding(current=first.final, repo="acme/notes", org="acme", directory=fake_directory(), person_id=portal.people["owner"].id,
                       request_id=rid, now=T0, remove="azure-identity")
    portal.store.create_binding(request_id=rid, person_id=portal.people["owner"].id, final=doc, now=T0, current=first.final)
    req = portal.store.claim_approved(now=T0)
    gh = bgh_for_write(existing=binding_yaml(first.final, first.id).encode())
    run_once(portal.store, client(gh), bcfg(), T0)
    put = json.loads(gh.called("PUT", BFILE)[0]["body"])
    assert put["sha"] == OLD_BLOB and not gh.called("DELETE", BFILE)
    written = yaml.safe_load(base64.b64decode(put["content"]))
    assert "azure_identity" not in written and "databricks" not in written and written["request"] == req.id
    assert "neither (unlinked)" in json.loads(gh.called("POST", R + "/pulls")[0]["body"])["body"]
    run_once(portal.store, client(badvancing(req, "modified")), bcfg(), T0)
    assert portal.store.get_request(req.id).state == "merged"


@pytest.mark.parametrize("files", [
    [{"filename": BPATH, "status": "removed"}], [{"filename": BPATH, "status": "renamed"}],
    [{"filename": BPATH, "status": "modified"}, {"filename": "intake/acme/notes.yaml", "status": "added"}],
    [{"filename": "bindings/acme/other.yaml", "status": "added"}], [],
], ids=["removed", "renamed", "second-file", "other-file", "none"])
def test_a_binding_pr_must_add_or_modify_exactly_its_one_file(portal, files):
    req = binding_request(portal.store, portal.people, T0)
    run_once(portal.store, client(bgh_for_write()), bcfg(), T0)
    gh = badvancing(req, files=files)
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "pr_files_unexpected") and not gh.called("PUT", R + "/pulls/12/merge")


def test_a_stale_blob_sha_fails_the_binding_with_github_s_status_and_opens_no_pr(portal):
    """The file changed on main between the writer's read and its PUT: GitHub answers 409, and the writer neither retries nor opens a PR."""
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n")
    gh.routes[("PUT", "api.github.com", BFILE)] = [(409, {"message": "<b>is at abc but expected</b>"})]
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", f"github: 409 PUT {BFILE}")
    assert len(gh.called("PUT", BFILE)) == 1 and not gh.called("POST", R + "/pulls") and not gh.called("GET", R + "/pulls")


def test_the_existing_file_is_read_at_the_base_commit_the_branch_is_made_from(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n")
    run_once(portal.store, client(gh), bcfg(), T0)
    assert gh.called("GET", BFILE)[0]["query"]["ref"] == BASE, "the blob the PUT replaces is the one at the commit the branch starts from"
    order = [c["path"] for c in gh.calls]
    assert order.index(R + "/git/ref/heads/main") < order.index(BFILE)


def test_a_binding_for_a_repo_with_no_intake_record_in_the_factory_fails_not_registered_with_no_branch_put_or_pr(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(intake=(404, {"message": "Not Found"}))
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", "not_registered")
    assert gh.called("GET", FILE)[0]["query"]["ref"] == BASE
    order = [c["path"] for c in gh.calls]
    assert order.index(R + "/git/ref/heads/main") < order.index(FILE)
    assert not gh.called("POST", R + "/git/refs") and not gh.called("PUT", BFILE) and not gh.called("GET", BFILE)
    assert not gh.called("GET", R + "/pulls") and not gh.called("POST", R + "/pulls")
    assert gh.calls[-1]["path"] == FILE, "no GitHub call after the missing intake record"


def test_an_error_reading_the_intake_record_fails_the_binding_with_a_code_written_reason(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(intake=(502, {"message": "<b>bad gateway</b>"}))
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.failure_reason) == ("failed", f"github: 502 GET {FILE}")
    assert not gh.called("POST", R + "/git/refs") and not gh.called("PUT", BFILE) and not gh.called("POST", R + "/pulls")


def test_a_merged_binding_that_reads_otherwise_at_the_merge_commit_is_failed(portal):
    req = binding_request(portal.store, portal.people, T0)
    run_once(portal.store, client(bgh_for_write()), bcfg(), T0)
    run_once(portal.store, client(badvancing(req, landed_raw=binding_yaml(req.final, req.id).encode() + b"# extra\n")), bcfg(), T0)
    assert portal.store.get_request(req.id).failure_reason == "merged_file_differs"


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


def test_an_interrupted_binding_write_resumes_without_a_second_write_or_pr(portal):
    """The writer stopped after its PUT and after opening the PR, before recording it: the next pass adopts both."""
    req = binding_request(portal.store, portal.people, T0)
    content = binding_yaml(req.final, req.id).encode()
    gh = bgh_for_write(existing=b"# an older binding\n")
    gh.routes[("POST", "api.github.com", R + "/git/refs")] = [(422, {"message": "Reference already exists"})]
    gh.on("GET", branch_ref(req), (200, {"object": {"sha": HEAD}}))
    gh.on("GET", R + f"/commits/{HEAD}", (200, {"sha": HEAD, "parents": [{"sha": BASE}], "files": [{"filename": BPATH, "status": "modified"}]}))
    gh.routes[("GET", "api.github.com", BFILE)] = [file_answer(b"# an older binding\n", OLD_BLOB), file_answer(content)]
    gh.routes[("GET", "api.github.com", R + "/pulls")] = [(200, [FakeGitHub.fixture("pull_open")])]
    run_once(portal.store, client(gh), bcfg(), T0)
    after = portal.store.get_request(req.id)
    assert (after.state, after.pr_number, after.head_sha) == ("pr_open", 12, HEAD)
    assert not gh.called("PUT", BFILE) and not gh.called("POST", R + "/pulls")
    assert gh.called("GET", BFILE)[-1]["query"]["ref"] == HEAD, "the branch's commit is read for its content"


def test_an_interrupted_binding_branch_with_other_content_is_not_adopted(portal):
    req = binding_request(portal.store, portal.people, T0)
    gh = bgh_for_write(existing=b"# an older binding\n")
    gh.routes[("POST", "api.github.com", R + "/git/refs")] = [(422, {"message": "Reference already exists"})]
    gh.on("GET", branch_ref(req), (200, {"object": {"sha": HEAD}}))
    gh.on("GET", R + f"/commits/{HEAD}", (200, {"sha": HEAD, "parents": [{"sha": BASE}], "files": [{"filename": BPATH, "status": "modified"}]}))
    gh.routes[("GET", "api.github.com", BFILE)] = [file_answer(b"# an older binding\n", OLD_BLOB), file_answer(b"schema: binding/v1\n")]
    run_once(portal.store, client(gh), bcfg(), T0)
    assert portal.store.get_request(req.id).failure_reason == "branch_not_ours" and not gh.called("PUT", BFILE)


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

"""The writer's loop (P17, P18). It acts only on requests the store hands it in state writing (claimed from approved), re-checks
each, writes only allowlisted paths, and merges only after the required checks pass. Any mismatch or GitHub error fails the
request with a reason written here, never GitHub's text. Every value taken from a GitHub answer (commit IDs, pull request
number and URL, the default branch) is checked for its shape before it is used or stored.

The merge is pinned. The writer's pull request bypasses the review rule, so what it merges must be exactly what it wrote: it
stores the commit its contents PUT returned (head_sha) and the base commit it branched from (base_sha), and merges only if the
PR's head is still that commit, the PR targets the default branch, the PR adds exactly the one allowlisted file, and the commit
is the single child of the stored base. The merge request names that commit, so a push after the check can't ride along."""

import base64
import datetime as dt
import re

from ..github.client import GitHubError
from ..github.shapes import BRANCH, SHA
from ..store.base import Conflict
from .allowlist import target_for
from .recheck import recheck
from .render import branch_name, intake_yaml, pr_body, title

ACTIONS_APP_ID = 15368  # the GitHub Actions app: the source the factory's ruleset pins its required checks to
PR_URL = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._/-]{1,300}")


def run_once(store, gh, cfg, now) -> list[str]:
    waiting = store.requests_in(("pr_open",))  # taken first: a request this pass opens waits for its checks until the next pass
    done = []

    def turn(request, step):
        try:
            done.append(step(store, gh, cfg, request, now))
        except Conflict as exc:  # another process moved the request: its turn ends here, and the pass goes on
            done.append(f"{request.id}: skipped ({exc.code})")

    for request in store.requests_in(("writing",)):  # left over from an interrupted run
        turn(request, _write)
    while (claimed := store.claim_approved(now=now)) is not None:
        turn(claimed, _write)
    for request in waiting:
        turn(request, _advance)
    return done


def _fail(store, req, reason, now) -> str:
    store.mark_failed(request_id=req.id, reason=reason, now=now)
    return f"{req.id}: failed ({reason})"


def _reason(exc) -> str:
    return f"github: {exc.status} {exc.method} {exc.path}" if isinstance(exc, GitHubError) else "github: unexpected response"


def _sha(value) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError("not a commit ID")
    return value


def _default_branch(gh, repo):
    base = gh.call("GET", f"/repos/{repo}").json()["default_branch"]
    if not isinstance(base, str) or not BRANCH.fullmatch(base):
        raise ValueError("not a branch name")
    return base


def _file(gh, repo, path, ref):
    try:
        doc = gh.call("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref}).json()
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    try:
        raw = base64.b64decode(doc.get("content", "") or "", validate=False) if doc.get("encoding", "base64") == "base64" else b""
    except ValueError:  # undecodable content is simply not our record
        raw = b""
    return doc["sha"], raw


def _only_the_target(files, path) -> bool:
    return isinstance(files, list) and len(files) == 1 and isinstance(files[0], dict) \
        and files[0].get("filename") == path and files[0].get("status") == "added"


def _single_child_of(commit, base_sha) -> bool:
    parents = commit.get("parents")
    return isinstance(parents, list) and len(parents) == 1 and isinstance(parents[0], dict) and parents[0].get("sha") == base_sha


def _is_ours(gh, target, sha, base_sha, content) -> bool:
    """A commit already on our branch is the writer's own only if it is one commit on the base we branched from that adds exactly
    the target file with exactly the content we would write."""
    commit = gh.call("GET", f"/repos/{target.repo}/commits/{sha}").json()
    if not _single_child_of(commit, base_sha) or not _only_the_target(commit.get("files"), target.path):
        return False
    existing = _file(gh, target.repo, target.path, sha)
    return existing is not None and existing[1] == content


def _from_the_target_repo(pr, repo) -> bool:
    """The PR's head branch lives in the target repo itself, not in a fork whose branch someone else controls."""
    return ((pr.get("head") or {}).get("repo") or {}).get("full_name") == repo


def _our_pr(pr, base, head, repo) -> bool:
    return (isinstance(pr, dict) and pr.get("state") == "open" and (pr.get("base") or {}).get("ref") == base
            and (pr.get("head") or {}).get("sha") == head and _from_the_target_repo(pr, repo))


def _only_our_pr(prs, req, default_branch) -> bool:
    """The list names our PR, into the default branch, and no other: GitHub fills pull_requests with every open PR for the head, and
    the REST API doesn't say which of several triggered a run, so a run shared with another PR is not taken for ours."""
    return (isinstance(prs, list) and len(prs) == 1 and isinstance(prs[0], dict) and type(prs[0].get("number")) is int
            and prs[0]["number"] == req.pr_number and (prs[0].get("base") or {}).get("ref") == default_branch)


def _run_is_ours(gh, target, req, default_branch, run) -> bool:
    """A check run counts only if it can be traced to the writer's own PR. The run names its check suite (only its id); the suite's
    one workflow run says the event, the head branch, commit and repo, and the PRs. All must match: a pull_request run, for our
    branch and commit, from the target repo itself, listing only our PR into the default branch."""
    suite = (run.get("check_suite") or {}).get("id")
    if type(suite) is not int or run.get("head_sha") != req.head_sha or not _only_our_pr(run.get("pull_requests"), req, default_branch):
        return False
    found = gh.call("GET", f"/repos/{target.repo}/actions/runs", params={"check_suite_id": suite}).json().get("workflow_runs")
    if not isinstance(found, list) or len(found) != 1 or not isinstance(found[0], dict):
        return False
    wf = found[0]
    return (wf.get("check_suite_id") == suite and wf.get("event") == "pull_request"
            and (wf.get("head_repository") or {}).get("full_name") == target.repo and wf.get("head_branch") == req.branch
            and wf.get("head_sha") == req.head_sha and _only_our_pr(wf.get("pull_requests"), req, default_branch))


def _merged_at(pr):
    """When GitHub says the PR merged, or None (unknown: then nothing is ever failed for want of an answer)."""
    try:
        return dt.datetime.strptime(pr["merged_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except (KeyError, TypeError, ValueError):
        return None


def _finish(store, gh, cfg, target, req, merge_sha, now, merged_at=None) -> str:
    """The PR is merged on GitHub, so the request is never failed because GitHub didn't answer. Before it is recorded, the target
    file must read exactly as written at the merge commit itself (which can't move, unlike a branch tip). An error, a malformed
    answer, or a 404 inside the grace period leaves the request open as "merge confirmation pending", and the next pass tries again
    through the merged path. It fails on other content at once, and on a definite 404 at the merge commit once confirm_grace has
    passed since the merge (merged_at, from the PR; unknown means never)."""
    pending = f"{req.id}: merged on GitHub, confirmation pending"
    try:
        found = _file(gh, target.repo, target.path, merge_sha)
    except (GitHubError, KeyError, TypeError, ValueError):
        return pending
    if found is None:
        if merged_at is not None and now - merged_at > cfg.confirm_grace:
            return _fail(store, req, "merged_file_missing", now)
        return pending
    if found[1] != intake_yaml(req.final, req.id).encode("utf-8"):
        return _fail(store, req, "merged_file_differs", now)
    store.mark_merged(request_id=req.id, merge_sha=merge_sha, now=now)
    return f"{req.id}: merged"


def _write(store, gh, cfg, req, now) -> str:
    problems = recheck(req, cfg, store.evidence_for(req.id, req.submission))
    if problems:
        return _fail(store, req, "recheck: " + ", ".join(problems), now)
    target, branch = target_for(cfg.org, req), branch_name(req.id)
    content = intake_yaml(req.final, req.id).encode("utf-8")
    try:
        base = _default_branch(gh, target.repo)
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        return _fail(store, req, "github: unexpected default branch" if isinstance(exc, ValueError) else _reason(exc), now)
    try:
        if _file(gh, target.repo, target.path, base) is not None:
            return _fail(store, req, f"already_registered: {target.path} exists on {base}", now)
        base_sha = _sha(gh.call("GET", f"/repos/{target.repo}/git/ref/heads/{base}").json()["object"]["sha"])
        try:
            gh.call("POST", f"/repos/{target.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha}, ok=(201,))
            tip = base_sha
        except GitHubError as exc:
            if exc.status != 422:
                raise
            # 422: the branch exists (an interrupted run, or someone got there first). It is ours only at the base commit we
            # branched from (nothing written yet) or at the writer's own commit.
            tip = _sha(gh.call("GET", f"/repos/{target.repo}/git/ref/heads/{branch}").json()["object"]["sha"])
        if tip == base_sha:
            head = _sha(gh.call("PUT", f"/repos/{target.repo}/contents/{target.path}", ok=(200, 201), json={
                "message": title(req), "content": base64.b64encode(content).decode("ascii"), "branch": branch}).json()["commit"]["sha"])
        elif _is_ours(gh, target, tip, base_sha, content):
            head = tip
        else:
            return _fail(store, req, "branch_not_ours", now)
        owner = target.repo.split("/")[0]
        found = gh.call("GET", f"/repos/{target.repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all"}).json()
        pr = found[0] if found else gh.call("POST", f"/repos/{target.repo}/pulls", ok=(201,), json={
            "title": title(req), "head": branch, "base": base, "maintainer_can_modify": False,
            "body": pr_body(req, store.get_person(req.requested_by), store.get_person(req.decided_by))}).json()
        if not _our_pr(pr, base, head, target.repo):
            return _fail(store, req, "pr_not_ours", now)
        number, url = int(pr["number"]), str(pr["html_url"])
        if number < 1 or not PR_URL.fullmatch(url):
            raise ValueError("unexpected pull request")
        store.mark_pr_open(request_id=req.id, branch=branch, pr_number=number, pr_url=url, head_sha=head, base_sha=base_sha, now=now)
        return f"{req.id}: pull request #{number} opened"
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        return _fail(store, req, _reason(exc), now)


def _advance(store, gh, cfg, req, now) -> str:
    target = target_for(cfg.org, req)
    try:
        if not (req.head_sha and req.base_sha):
            return _fail(store, req, "no_pinned_commit", now)
        pr = gh.call("GET", f"/repos/{target.repo}/pulls/{req.pr_number}").json()
        if (pr.get("head") or {}).get("sha") != req.head_sha:
            return _fail(store, req, "pr_head_changed", now)  # a push after the writer opened it: never merged
        if not _from_the_target_repo(pr, target.repo):
            return _fail(store, req, "pr_not_ours", now)
        if pr.get("merged"):
            return _finish(store, gh, cfg, target, req, _sha(pr["merge_commit_sha"]), now, _merged_at(pr))
        if pr.get("state") != "open":
            return _fail(store, req, "pr_closed", now)
        default_branch = _default_branch(gh, target.repo)
        if (pr.get("base") or {}).get("ref") != default_branch:
            return _fail(store, req, "pr_base_changed", now)
        files = list(gh.paginate(f"/repos/{target.repo}/pulls/{req.pr_number}/files"))
        if not _only_the_target(files, target.path):
            return _fail(store, req, "pr_files_unexpected", now)
        if not _single_child_of(gh.call("GET", f"/repos/{target.repo}/commits/{req.head_sha}").json(), req.base_sha):
            return _fail(store, req, "pr_commit_not_ours", now)
        conclusions = {}
        for name in cfg.required_checks:
            runs = gh.call("GET", f"/repos/{target.repo}/commits/{req.head_sha}/check-runs", params={"check_name": name}).json()["check_runs"]
            ours = [r for r in runs if r.get("name") == name and (r.get("app") or {}).get("id") == ACTIONS_APP_ID
                    and _run_is_ours(gh, target, req, default_branch, r)]
            conclusions[name] = next((r.get("conclusion") for r in ours if r.get("status") == "completed"), None)
        failed = [n for n, c in conclusions.items() if c not in (None, "success")]
        if failed:
            return _fail(store, req, f"check_failed: {failed[0]}", now)
        if any(c is None for c in conclusions.values()):
            return _fail(store, req, "checks_timeout", now) if now - req.updated_at > cfg.merge_timeout else f"{req.id}: waiting for checks"
        try:
            merged = gh.call("PUT", f"/repos/{target.repo}/pulls/{req.pr_number}/merge",
                             json={"merge_method": cfg.merge_method, "sha": req.head_sha}).json()
        except GitHubError as exc:
            if exc.status in (405, 409) and now - req.updated_at <= cfg.merge_timeout:
                return f"{req.id}: merge not allowed yet ({exc.status})"
            raise
        return _finish(store, gh, cfg, target, req, _sha(merged["sha"]), now)
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        return _fail(store, req, _reason(exc), now)

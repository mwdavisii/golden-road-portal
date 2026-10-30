"""The writer's loop (P17, P18). It acts only on requests the store hands it in state writing (claimed from approved), re-checks
each, writes only allowlisted paths, and merges only after the required checks pass (never a prod binding: a person merges that
one through the review rule, and the writer records the merge, spec 2026-10-30). Any mismatch or GitHub error fails the
request with a reason written here, never GitHub's text. Every value taken from a GitHub answer (commit IDs, pull request
number and URL, the default branch) is checked for its shape before it is used or stored.

The merge is pinned. The writer's pull request bypasses the review rule, so what it merges must be exactly what it wrote: it
stores the commit its last contents PUT returned (head_sha) and the base commit it branched from (base_sha), and merges only if
the PR's head is still that commit, the PR targets the default branch, the PR changes exactly the allowlisted files, and the
commits from the stored base to head are the writer's own, one per file. A registration adds its one intake record in the
factory. A binding (spec 2026-10-29 GitOps deploy repo, correction 3) writes its environment's bindings.yaml and then its
composed.yaml in golden-road-deploy, both added or modified in place (an unlink rewrites bindings.yaml, never deletes it);
composed.yaml is computed here with the portal's byte-identical copy of golden-road compose, so deploy-check's drift check
passes, and it is left out only when it would not change. The merge request names head, so a push after the check can't ride
along."""

import base64
import datetime as dt
import hashlib
import re

import yaml

from ..compose import ComposeError, UniqueKeyLoader, compose
from ..contracts import validate
from ..github.client import GitHubError
from ..github.shapes import BRANCH, SHA
from ..store.base import Conflict
from .allowlist import factory, intake_path, target_for, writer_merges
from .recheck import recheck
from .render import branch_name, content_for, pr_body, title

# The status each file of a writer PR may show: a registration adds its record; a binding adds or updates its bindings.yaml and
# composed.yaml in place (an unlink rewrites bindings.yaml, never deletes it).
STATUSES = {"registration": ("added",), "binding": ("added", "modified")}

ACTIONS_APP_ID = 15368  # the GitHub Actions app: the source the factory's ruleset pins its required checks to
MAX_FILE_BYTES = 1_000_000  # golden_road.render.MAX_FILE_BYTES: the largest release.yaml or base/manifests.yaml golden-road reads
MAX_BINDING_BYTES = 65_536  # golden_road.deploy.MAX_BINDING_BYTES: the largest bindings.yaml golden-road deploy reads
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


def _fail_and_close(store, gh, target, req, reason, now) -> str:
    """_fail for a request whose PR is open: the writer will never merge it, so it says why on the PR and closes it. Best effort,
    after the failure is recorded: nothing the comment or the close raises may change the reason or stop the pass."""
    done = _fail(store, req, reason, now)
    note = (f"The golden road portal's writer failed this request: `{reason}`. It never merges a failed request, so it closes this "
            "pull request. Make the change again in the portal.")
    for method, path, body in (("POST", f"/repos/{target.repo}/issues/{req.pr_number}/comments", {"body": note}),
                               ("PATCH", f"/repos/{target.repo}/pulls/{req.pr_number}", {"state": "closed"})):
        try:
            gh.call(method, path, ok=(200, 201), json=body)
        except Exception:  # noqa: BLE001 - best effort: the request is already failed with its own reason
            pass
    return done


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


def _only_these(files, expected) -> bool:
    """The PR's (or a commit's) files are exactly the expected paths, each with one of its allowed statuses. expected: {path: statuses}."""
    if not isinstance(files, list) or len(files) != len(expected) or not all(isinstance(f, dict) for f in files):
        return False
    seen = {f.get("filename"): f.get("status") for f in files}
    return len(seen) == len(expected) and all(path in seen and seen[path] in statuses for path, statuses in expected.items())


def _single_child_of(commit, base_sha) -> bool:
    parents = commit.get("parents")
    return isinstance(parents, list) and len(parents) == 1 and isinstance(parents[0], dict) and parents[0].get("sha") == base_sha


def _chain(gh, repo, head, base_sha, limit):
    """The commits from base_sha's child up to head, oldest first, when head is at most `limit` single-parent commits above
    base_sha; None otherwise (a merge commit, another base, a longer history)."""
    commits, sha = [], head
    for _ in range(limit):
        commit = gh.call("GET", f"/repos/{repo}/commits/{sha}").json()
        parents = commit.get("parents") if isinstance(commit, dict) else None
        if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict):
            return None
        commits.insert(0, commit)
        if parents[0].get("sha") == base_sha:
            return commits
        sha = _sha(parents[0].get("sha"))
    return None


def _chain_writes(commits, order, statuses):
    """The chain writes order's files one commit each, in order (a prefix of them: a binding whose composed.yaml is unchanged
    writes only bindings.yaml). Returns how many, or None."""
    if commits is None or not 1 <= len(commits) <= len(order):
        return None
    for commit, path in zip(commits, order):
        if not _only_these(commit.get("files"), {path: statuses}):
            return None
    return len(commits)


def _is_ours(gh, target, tip, base_sha, files, statuses):
    """How many of `files` ([(path, content, existing)], in write order) a branch tip already holds as the writer's own commits:
    a chain of single-parent commits on the base we branched from, each adding (or, for a binding, modifying) exactly the next
    file with exactly the content we would write. None when the branch holds anything else."""
    order = [path for path, _, _ in files]
    done = _chain_writes(_chain(gh, target.repo, tip, base_sha, len(order)), order, statuses)
    if done is None:
        return None
    for path, content, _ in files[:done]:
        found = _file(gh, target.repo, path, tip)
        if found is None or found[1] != content:
            return None
    return done


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


def _expected_at_merge(gh, target, req):
    """{path: the bytes it must read at the merge commit}: the record we wrote, and for a binding the composed.yaml of the commit the
    PR was opened with (immutable, and what deploy-check passed). Raises on a GitHub error; a composed.yaml missing at that commit
    is expected as b"" so it never matches."""
    expected = {target.path: content_for(req).encode("utf-8")}
    if target.also is not None:
        found = _file(gh, target.repo, target.also, req.head_sha)
        expected[target.also] = b"" if found is None else found[1]
    return expected


def _finish(store, gh, cfg, target, req, merge_sha, now, merged_at=None) -> str:
    """The PR is merged on GitHub, so the request is never failed because GitHub didn't answer. Before it is recorded, every file
    it wrote must read exactly as written at the merge commit itself (which can't move, unlike a branch tip). An error, a malformed
    answer, or a 404 inside the grace period leaves the request open as "merge confirmation pending", and the next pass tries again
    through the merged path. It fails on other content at once, and on a definite 404 at the merge commit once confirm_grace has
    passed since the merge (merged_at, from the PR; unknown means never)."""
    pending = f"{req.id}: merged on GitHub, confirmation pending"
    try:
        expected = _expected_at_merge(gh, target, req)
        found = {path: _file(gh, target.repo, path, merge_sha) for path in expected}
    except (GitHubError, KeyError, TypeError, ValueError, AttributeError):
        return pending
    if any(f is not None and f[1] != expected[path] for path, f in found.items()):
        return _fail(store, req, "merged_file_differs", now)
    if any(f is None for f in found.values()):
        if merged_at is not None and now - merged_at > cfg.confirm_grace:
            return _fail(store, req, "merged_file_missing", now)
        return pending
    store.mark_merged(request_id=req.id, merge_sha=merge_sha, now=now)
    return f"{req.id}: merged"


class Refused(Exception):
    """A binding the writer can't write: its environment has no release pinned (not_deployed), or composing refused (compose_refused)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _one_yaml_object(raw: bytes):
    """One YAML object, as golden-road deploy reads release.yaml and bindings.yaml: UTF-8, no anchors or aliases, no key twice."""
    text = raw.decode("utf-8")
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ValueError("anchors")
    docs = [d for d in yaml.load_all(text, Loader=UniqueKeyLoader) if d is not None]
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise ValueError("not one object")
    return docs[0]


def composed_for(request, release_raw: bytes, manifests_raw: bytes, binding_raw: bytes) -> bytes:
    """The environment's composed.yaml as `golden-road compose` writes it from release.yaml, base/manifests.yaml and the bindings.yaml
    the writer writes (binding_raw, read back as deploy-check reads it), byte for byte: portal/compose.py is golden_road/compose.py.
    release.yaml must be a valid deploy-release/v1 for exactly the request's repo and environment, and the manifests must be its
    manifests_sha256. Raises Refused("compose_refused") on anything golden-road compose would refuse."""
    environment = request.draft["environment"]
    try:
        release = _one_yaml_object(release_raw)
        if len(release_raw) > MAX_FILE_BYTES or validate("deploy-release.v1", release) or release["repo"] != request.repo \
                or release["environment"] != environment:
            raise ValueError("not this environment's release")
        if len(manifests_raw) > MAX_FILE_BYTES or hashlib.sha256(manifests_raw).hexdigest() != release["manifests_sha256"]:
            raise ValueError("not the release's manifests")
        if len(binding_raw) > MAX_BINDING_BYTES:
            raise ValueError("bindings.yaml too large")
        binding = _one_yaml_object(binding_raw)
        if validate("binding.v2", binding) or binding["repo"] != release["repo"] or binding["environment"] != environment:
            raise ValueError("not this environment's binding")
        return compose(manifests_raw.decode("utf-8"), release["image"], binding, namespace=release["namespace"]).encode("utf-8")
    except (ComposeError, ValueError, KeyError, TypeError, yaml.YAMLError, RecursionError):  # UnicodeDecodeError is a ValueError
        raise Refused("compose_refused") from None


def _factory_head(gh, org):
    """The factory's default branch commit: where a binding's intake record must be."""
    repo = factory(org)
    return repo, _sha(gh.call("GET", f"/repos/{repo}/git/ref/heads/{_default_branch(gh, repo)}").json()["object"]["sha"])


def _files_to_write(gh, cfg, req, target, base, base_sha, content):
    """[(path, content, existing (blob sha, bytes) or None)] in write order. A registration writes its new intake record. A binding
    writes its bindings.yaml and then its composed.yaml, the latter only when it changes; first its app must be registered (the
    intake record in the factory) and deployed in the environment (release.yaml and base/manifests.yaml at base_sha)."""
    existing = _file(gh, target.repo, target.path, base_sha)  # read at the commit the branch starts from: the blob a PUT replaces
    if req.kind != "binding":
        if existing is not None:
            raise Refused(f"already_registered: {target.path} exists on {base}")
        return [(target.path, content, None)]
    d = target.path.rsplit("/", 1)[0]
    release = _file(gh, target.repo, f"{d}/release.yaml", base_sha)
    manifests = _file(gh, target.repo, f"{d}/base/manifests.yaml", base_sha)
    if release is None or manifests is None:
        raise Refused("not_deployed")
    composed = composed_for(req, release[1], manifests[1], content)
    current = _file(gh, target.repo, target.also, base_sha)
    files = [(target.path, content, existing)]
    if current is None or current[1] != composed:
        files.append((target.also, composed, current))
    return files


def _write(store, gh, cfg, req, now) -> str:
    evidence = store.evidence_for(req.id, req.submission) if req.kind == "registration" else None
    problems = recheck(req, cfg, evidence, store, now)
    if problems:
        return _fail(store, req, "recheck: " + ", ".join(problems), now)
    target, branch = target_for(cfg.org, req), branch_name(req.id)
    content = content_for(req).encode("utf-8")
    try:
        base = _default_branch(gh, target.repo)
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        return _fail(store, req, "github: unexpected default branch" if isinstance(exc, ValueError) else _reason(exc), now)
    try:
        base_sha = _sha(gh.call("GET", f"/repos/{target.repo}/git/ref/heads/{base}").json()["object"]["sha"])
        # a binding is only for a registered app: its intake record (same owner/repo) must be in the factory at the factory's
        # default branch; if it isn't, nothing more is asked of GitHub
        if req.kind == "binding":
            repo, head = _factory_head(gh, cfg.org)
            if _file(gh, repo, intake_path(req.repo), head) is None:
                return _fail(store, req, "not_registered", now)
        try:
            files = _files_to_write(gh, cfg, req, target, base, base_sha, content)
        except Refused as exc:
            return _fail(store, req, exc.reason, now)
        try:
            gh.call("POST", f"/repos/{target.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha}, ok=(201,))
            tip, done = base_sha, 0
        except GitHubError as exc:
            if exc.status != 422:
                raise
            # 422: the branch exists (an interrupted run, or someone got there first). It is ours only at the base commit we
            # branched from (nothing written yet) or at the writer's own commits.
            tip = _sha(gh.call("GET", f"/repos/{target.repo}/git/ref/heads/{branch}").json()["object"]["sha"])
            done = 0 if tip == base_sha else _is_ours(gh, target, tip, base_sha, files, STATUSES[req.kind])
            if done is None:
                return _fail(store, req, "branch_not_ours", now)
        head = tip
        for path, data, existing in files[done:]:  # one contents-API commit per file, in order
            body = {"message": title(req), "content": base64.b64encode(data).decode("ascii"), "branch": branch}
            if existing is not None:
                body["sha"] = _sha(existing[0])  # updated in place: the contents API needs the blob it replaces
            head = _sha(gh.call("PUT", f"/repos/{target.repo}/contents/{path}", ok=(200, 201), json=body).json()["commit"]["sha"])
        owner = target.repo.split("/")[0]
        found = gh.call("GET", f"/repos/{target.repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all"}).json()
        body = pr_body(req, store.get_person(req.requested_by), store.get_person(req.decided_by) if req.decided_by else None, target,
                       platform_user=getattr(cfg.secret_server, "platform_user", None))
        pr = found[0] if found else gh.call("POST", f"/repos/{target.repo}/pulls", ok=(201,), json={
            "title": title(req), "head": branch, "base": base, "maintainer_can_modify": False, "body": body}).json()
        if not _our_pr(pr, base, head, target.repo):
            return _fail(store, req, "pr_not_ours", now)
        number, url = int(pr["number"]), str(pr["html_url"])
        if number < 1 or not PR_URL.fullmatch(url):
            raise ValueError("unexpected pull request")
        # An adopted PR (an interrupted run) may carry an older body, one that says the writer merges a prod binding: bring it up to
        # date, best effort (the body is what the person and the reviewer read, not what the merge rests on).
        if found and pr.get("body") != body:
            try:
                gh.call("PATCH", f"/repos/{target.repo}/pulls/{number}", json={"body": body})
            except GitHubError:
                pass
        store.mark_pr_open(request_id=req.id, branch=branch, pr_number=number, pr_url=url, head_sha=head, base_sha=base_sha, now=now)
        return f"{req.id}: pull request #{number} opened"
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        return _fail(store, req, _reason(exc), now)


def _order(target):
    return [target.path] + ([target.also] if target.also else [])


def _advance(store, gh, cfg, req, now) -> str:
    target = target_for(cfg.org, req)
    if target is None:
        return _fail(store, req, "bad_target", now)

    def fail(reason):
        return _fail_and_close(store, gh, target, req, reason, now) if req.pr_number else _fail(store, req, reason, now)

    try:
        if not (req.head_sha and req.base_sha):
            return fail("no_pinned_commit")
        pr = gh.call("GET", f"/repos/{target.repo}/pulls/{req.pr_number}").json()
        if (pr.get("head") or {}).get("sha") != req.head_sha:
            return fail("pr_head_changed")  # a push after the writer opened it: never merged
        if not _from_the_target_repo(pr, target.repo):
            return fail("pr_not_ours")
        if pr.get("merged"):
            return _finish(store, gh, cfg, target, req, _sha(pr["merge_commit_sha"]), now, _merged_at(pr))
        if pr.get("state") != "open":
            return fail("pr_closed")
        default_branch = _default_branch(gh, target.repo)
        if (pr.get("base") or {}).get("ref") != default_branch:
            return fail("pr_base_changed")
        # Both target repos' rulesets require the PR to be up to date with the default branch, and the writer never updates its
        # branch: once the default branch has moved past the base it branched from, the PR can't merge. Fail now, not at
        # merge_timeout; the person makes the change again, on the new base.
        current = _sha(gh.call("GET", f"/repos/{target.repo}/git/ref/heads/{default_branch}").json()["object"]["sha"])
        if current != req.base_sha or pr.get("mergeable_state") == "behind" or (pr.get("base") or {}).get("sha") not in (None, current):
            return fail("base_moved")
        order, statuses = _order(target), STATUSES[req.kind]
        # the commits from the base to head are ours, one per file (for a binding, composed.yaml is left out when unchanged) ...
        written = _chain_writes(_chain(gh, target.repo, req.head_sha, req.base_sha, len(order)), order, statuses)
        if written is None:
            return fail("pr_commit_not_ours")
        # ... and the PR shows exactly the files they wrote, nothing else
        files = list(gh.paginate(f"/repos/{target.repo}/pulls/{req.pr_number}/files"))
        if not _only_these(files, {p: statuses for p in order[:written]}):
            return fail("pr_files_unexpected")
        conclusions = {}
        for name in cfg.required_checks[target.repo]:
            runs = gh.call("GET", f"/repos/{target.repo}/commits/{req.head_sha}/check-runs", params={"check_name": name}).json()["check_runs"]
            ours = [r for r in runs if r.get("name") == name and (r.get("app") or {}).get("id") == ACTIONS_APP_ID
                    and _run_is_ours(gh, target, req, default_branch, r)]
            conclusions[name] = next((r.get("conclusion") for r in ours if r.get("status") == "completed"), None)
        if not conclusions:
            return fail("no_required_checks")
        failed = [n for n, c in conclusions.items() if c not in (None, "success")]
        if failed:
            return fail(f"check_failed: {failed[0]}")
        # merge_timeout is for the writer's own merges. A PR a person merges (prod) has no timeout at all: a re-run of a check
        # leaves no completed run for a while, and it waits for that as long as for the review.
        if any(c is None for c in conclusions.values()):
            if writer_merges(target) and now - req.updated_at > cfg.merge_timeout:
                return fail("checks_timeout")
            return f"{req.id}: waiting for checks"
        # A prod binding is merged by a person, through main-review; the next pass finds it merged and finishes it. It waits as long
        # as the review takes, and every check above still runs on each pass.
        if not writer_merges(target):
            return f"{req.id}: waiting for review"
        try:
            merged = gh.call("PUT", f"/repos/{target.repo}/pulls/{req.pr_number}/merge",
                             json={"merge_method": cfg.merge_method, "sha": req.head_sha}).json()
        except GitHubError as exc:
            if exc.status in (405, 409) and now - req.updated_at <= cfg.merge_timeout:
                return f"{req.id}: merge not allowed yet ({exc.status})"
            raise
        return _finish(store, gh, cfg, target, req, _sha(merged["sha"]), now)
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        return fail(_reason(exc))

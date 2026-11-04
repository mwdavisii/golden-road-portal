"""The writer's lifecycle requests (spec 2026-11-02): suspend, resume and decommission, applied in git (L12).

A request is planned once: the writer checks it again (rules 1 to 4 as far as the writer can see; decided while planning: item 3) and
records its steps, ACTION_STEPS's order (rule 7; deploy, archive, factory for a decommission since item 21). Each pass carries out an
open command first (retry, abandon or stop: lifecycle_commands), then works on the first step that isn't done:
- a PR step (deploy, then factory after the archive) plans its files at the target repo's current main, commits them one per file with
  the contents API on portal/lifecycle-<request>-<step>-<attempt>, and opens the PR; later passes check it as the run loop checks a
  binding's (the pinned head, our commits exactly, the PR's files exactly, the required checks traced to it). The writer merges a
  suspend or resume itself, after the L5 proof (l5_problems); a decommission's two PRs wait for a person. If main moves first, the PR
  is closed and the step rebuilt on the new main (decided while planning: item 2), with a "rebuilt" note. A merged PR is confirmed
  file by file at its merge commit. Before a step plans, every attempt's branch is asked for a merged PR the step never recorded (a
  pass stopped after opening it, or a person merged it as the writer closed it): it is checked as a recorded one and its merge
  recorded. Every PR is read on its own (GET /pulls/{n}): GitHub's list of PRs doesn't say whether one merged.
- the archive step archives the repository with the lifecycle App (lifecycle_archive).
A decommission's archive and factory steps, and its completion, need the app still decommissioned by this request: an app the platform
team brought back by hand (platform-team procedure 18) is never archived, marked or set decommissioned again by a Retry
(not_decommissioned_by_this_request).
Every step queues its note in the outbox (lifecycle_events) for the collector to send to the ServiceNow task. A failure fails the step,
never the request: an admin retries or abandons it. GitHub's text never becomes a reason."""

import base64
import dataclasses as dc
import re

import yaml

from ..collectors.repo_files import Malformed, case_variants, git_tree
from ..compose import UniqueKeyLoader
from ..contracts import validate
from ..github.client import GitHubError
from ..lifecycle import is_platform_repo
from ..store.base import ACTION_STEPS, STATE_AFTER
from .allowlist import (ADAPTER_DIRS, DEPLOY, ENVIRONMENTS, FACTORY, Target, adapter_name, environment_dir, factory, intake_path,
                        lifecycle_merges, lifecycle_path_allowed)
from .render import lifecycle_body, lifecycle_title, lifecycle_yaml, utc
from .run import (PR_URL, Refused, _chain, _default_branch, _file, _from_the_target_repo, _merged_at, _one_yaml_object, _only_these,
                  _our_pr, _reason, _sha, check_conclusions, compose_bytes)

REGULAR = ("100644", "100755")


@dc.dataclass(frozen=True)
class Planned:
    """One file a PR step changes, in write order: its new bytes (None: deleted) and the blob it replaces or deletes (None: new)."""
    path: str
    content: bytes | None
    existing: str | None

    @property
    def status(self) -> str:
        return "removed" if self.content is None else "modified" if self.existing else "added"


def step_repo(cfg, step) -> str:
    """The repo a step's PR is in: the factory for the factory step, the deploy repo otherwise."""
    return f"{cfg.org}/{FACTORY}" if step.step == "factory" else f"{cfg.org}/{DEPLOY}"


def branch_for(req, step) -> str:
    return f"portal/lifecycle-{req.id}-{step.step}-{step.attempt}"


def _head(gh, repo) -> str:
    return _sha(gh.call("GET", f"/repos/{repo}/git/ref/heads/{_default_branch(gh, repo)}").json()["object"]["sha"])


# ---- planning a step's files ----

def deploy_plan(gh, cfg, req, base_sha) -> list[Planned]:
    """A suspend: lifecycle.yaml added and composed.yaml recomposed in each environment with a release pinned. A resume: lifecycle.yaml
    deleted and composed.yaml recomposed in each environment that has one. A decommission: removal_plan. Idempotent, as factory_plan:
    an environment whose lifecycle.yaml is already this request's (a suspend) is left as it is; an empty plan is a done step. (A PR of
    this request that merged unrecorded is found before any plan, by _unrecorded_merge, and checked as a recorded one.)"""
    repo, action = f"{cfg.org}/{DEPLOY}", req.final["action"]
    if action == "decommission":
        return removal_plan(gh, repo, req.repo, base_sha)
    planned, pinned = [], False
    for env in ENVIRONMENTS:
        d = environment_dir(req.repo, env)
        release = _file(gh, repo, f"{d}/release.yaml", base_sha)
        if release is None:
            continue
        pinned = True
        manifests = _file(gh, repo, f"{d}/base/manifests.yaml", base_sha)
        if manifests is None:
            raise Refused("not_deployed")
        binding = _file(gh, repo, f"{d}/bindings.yaml", base_sha)
        current = _file(gh, repo, f"{d}/lifecycle.yaml", base_sha)
        composed = _file(gh, repo, f"{d}/composed.yaml", base_sha)
        if action == "suspend":
            if current is not None:
                if _is_this_suspension(current[1], req, env):
                    continue  # this request's suspension is in git already
                raise Refused("already_suspended")
            life = lifecycle_yaml(req, env).encode("utf-8")
            planned.append(Planned(f"{d}/lifecycle.yaml", life, None))
        else:
            if current is None:
                continue  # an environment pinned after the suspend runs as it is (decided while planning: item 19)
            life = None
            planned.append(Planned(f"{d}/lifecycle.yaml", None, current[0]))
        new = compose_bytes(req.repo, env, release[1], manifests[1], binding[1] if binding else None, cfg.skills_catalog, life)
        if composed is None or composed[1] != new:
            planned.append(Planned(f"{d}/composed.yaml", new, composed[0] if composed else None))
    if not pinned:
        raise Refused("nothing_to_suspend" if action == "suspend" else "not_suspended_in_git")
    return planned


def _is_this_suspension(raw, req, env) -> bool:
    """lifecycle.yaml is a valid lifecycle/v1 of this request for this repo, environment and task."""
    try:
        doc = _one_yaml_object(raw or b"")
        return (not validate("lifecycle.v1", doc) and (doc["repo"], doc["environment"], doc["request"], doc["task"])
                == (req.repo, env, req.id, req.final["task"]))
    except (ValueError, KeyError, TypeError, yaml.YAMLError, RecursionError):
        return False


def _subtree(gh, repo, sha, parts):
    """The tree at parts (["apps", owner, name]) under commit sha, or None. Each level is listed case-insensitively: only another
    spelling there (Acme/Notes for acme/notes) is case_mismatch, never "nothing in the deploy repo" while the app runs, and so is the
    request's spelling beside another (deploy-verify refuses one repo under two cases). A symlink or submodule where a directory should
    be is not_a_file."""
    for part in parts:
        same = [e for e in git_tree(gh, repo, sha) if isinstance(e.get("path"), str) and e["path"].lower() == part.lower()]
        entry = next((e for e in same if e["path"] == part), None)
        if len(same) > (1 if entry else 0):
            raise Refused("case_mismatch")
        if entry is None:
            return None
        if entry["type"] != "tree":
            raise Refused("not_a_file")
        sha = entry["sha"]
    return sha


def _blobs(gh, repo, tree_sha, prefix) -> list[tuple[str, str]]:
    """(path, blob sha) for every file under the tree, recursively, sorted by path. A symlink or submodule is not_a_file."""
    found = []
    for e in sorted(git_tree(gh, repo, tree_sha), key=lambda e: e["path"]):
        path = f"{prefix}/{e['path']}"
        if e["type"] == "tree":
            found += _blobs(gh, repo, e["sha"], path)
        elif e["type"] == "blob" and e["mode"] in REGULAR:
            found.append((path, e["sha"]))
        else:
            raise Refused("not_a_file")
    return found


def removal_plan(gh, repo, app_repo, base_sha) -> list[Planned]:
    """L6: every file of apps/<owner>/<repo>/ (each environment's release, bindings, composed, kustomization, cloudflare-access, lifecycle
    and base/manifests), then each environment's two adapters (flux, then argo), all deleted, in one PR; an adapter by the app's name
    that reconciles another path is adapter_not_ours. [] when the deploy repo holds
    nothing of the app (decided while planning: item 17)."""
    owner, name = app_repo.split("/")
    try:
        app = _subtree(gh, repo, base_sha, ["apps", owner, name])
        planned = [Planned(p, None, sha) for p, sha in (_blobs(gh, repo, app, f"apps/{owner}/{name}") if app else [])]
    except Malformed:
        raise Refused("tree_unreadable") from None
    for env in ENVIRONMENTS:
        adapter = adapter_name(owner, name, env)
        for d in ADAPTER_DIRS if adapter else ():
            found = _file(gh, repo, f"{d}/{adapter}.yaml", base_sha)
            if found is not None:
                if not _adapter_is_for(found[1], d, f"apps/{owner}/{name}/{env}"):
                    raise Refused("adapter_not_ours")
                planned.append(Planned(f"{d}/{adapter}.yaml", None, found[0]))
    return planned


def _adapter_is_for(raw, adapter_dir, env_dir) -> bool:
    """The adapter reconciles env_dir, as golden_road.deploy.adapters writes it: a Flux Kustomization with spec.path ./<env_dir>, or an
    Argo CD Application with spec.source.path <env_dir>. adapter_name isn't one-to-one (acme-my/app and acme/my-app share a name), so an
    adapter by the app's name may be another app's."""
    try:
        doc = _one_yaml_object(raw)
    except (ValueError, yaml.YAMLError, RecursionError):
        return False
    spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
    if adapter_dir == ADAPTER_DIRS[0]:
        return doc.get("kind") == "Kustomization" and spec.get("path") == f"./{env_dir}"
    source = spec.get("source") if isinstance(spec.get("source"), dict) else {}
    return doc.get("kind") == "Application" and source.get("path") == env_dir


def factory_plan(gh, cfg, req, base_sha, at) -> list[Planned]:
    """The intake record with the decommissioned key appended and not one other byte changed ({at, task, by, request}; at is when the deploy step
    was recorded done: decided while planning: item 16). [] when the record already says this request decommissioned it."""
    repo, path = factory(cfg.org), intake_path(req.repo)
    if case_variants(gh, repo, path, base_sha):
        raise Refused("record_case_mismatch")
    found = _file(gh, repo, path, base_sha)
    if found is None:
        raise Refused("not_registered")
    try:
        record = _one_yaml_object(found[1])
    except (ValueError, yaml.YAMLError, RecursionError):
        raise Refused("record_unreadable") from None
    if record.get("schema") != "intake/v2" or record.get("repo") != req.repo or validate("intake.v2", record):
        raise Refused("record_not_intake_v2")
    if "decommissioned" in record:
        if record["decommissioned"].get("request") == req.id:
            return []
        raise Refused("already_decommissioned")
    key = {"decommissioned": {"at": utc(at), "task": req.final["task"], "by": req.requested_by, "request": req.id}}
    marked = {**record, **key}
    if validate("intake.v2", marked):
        raise Refused("record_not_intake_v2")
    # the record's own bytes (its header, the registration's request id, every key) and the key's YAML after them: nothing else changes
    text = found[1].decode("utf-8")
    new = (text if text.endswith("\n") else text + "\n") + yaml.safe_dump(key, sort_keys=False, allow_unicode=True, width=1000)
    try:
        if _one_yaml_object(new.encode("utf-8")) != marked:
            raise ValueError("not the record and the key")
    except (ValueError, yaml.YAMLError, RecursionError):
        raise Refused("record_unreadable") from None
    return [Planned(path, new.encode("utf-8"), found[0])]


def _plan(gh, cfg, req, step, base_sha, store) -> list[Planned]:
    """The step's files at base_sha, each held to lifecycle_path_allowed (else path_not_allowlisted)."""
    if step.step == "deploy":
        planned = deploy_plan(gh, cfg, req, base_sha)
    elif step.step == "factory":
        deploy = next(s for s in store.lifecycle_steps(req.id) if s.step == "deploy")
        planned = factory_plan(gh, cfg, req, base_sha, deploy.updated_at)
    else:
        raise Refused("path_not_allowlisted")  # the archive writes no file
    repo = step_repo(cfg, step)
    if any(not lifecycle_path_allowed(cfg.org, repo, p.path, req.repo, req.final["action"], step.step) for p in planned):
        raise Refused("path_not_allowlisted")
    return planned


# ---- the L5 proof ----

def _documents(raw):
    if raw is None:
        raise ValueError("missing")
    return [d for d in yaml.load_all(raw.decode("utf-8"), Loader=UniqueKeyLoader) if d is not None]


def _deployment(docs):
    found = [d for d in docs if isinstance(d, dict) and d.get("kind") == "Deployment"]
    if len(found) != 1 or not isinstance(found[0].get("spec"), dict):
        raise ValueError("not one Deployment")
    return found[0]


def _replicas(deployment):
    """(present, value) of a Deployment's spec.replicas: absent is (False, None), never a default."""
    spec = deployment["spec"]
    return ("replicas" in spec, spec.get("replicas"))


def _shown(replicas) -> str:
    return repr(replicas[1]) if replicas[0] else "no replicas"


def _without_replicas(doc):
    if isinstance(doc, dict) and doc.get("kind") == "Deployment" and isinstance(doc.get("spec"), dict):
        return {**doc, "spec": {k: v for k, v in doc["spec"].items() if k != "replicas"}}
    return doc


def l5_problems(action, req, files, read) -> list[str]:
    """L5, before the writer merges a suspend or resume itself (prod included): from the PR as GitHub shows it, never from what the writer
    meant to write. files: the PR's files ({filename, status}); read(path, "base" | "head"): a file's bytes at the PR's base or head commit
    (None: not there). In each environment the PR touches, exactly lifecycle.yaml (added for a suspend, removed for a resume) and
    composed.yaml (modified) change; composed.yaml's only difference is the Deployment's replicas, going to the integer 0 (suspend) or to
    its value in base/manifests.yaml (resume; absent there, absent here); a suspend's lifecycle.yaml is this request's. [] lets the writer merge; anything else goes to a person."""
    want = {"suspend": "added", "resume": "removed"}[action]
    owner, name = req.repo.split("/")
    pattern = re.compile(rf"apps/{re.escape(owner)}/{re.escape(name)}/(test|prod)/(lifecycle|composed)\.yaml")
    problems, envs = [], set()
    for f in files:
        path, status = (f.get("filename"), f.get("status")) if isinstance(f, dict) else (None, None)
        m = pattern.fullmatch(path) if isinstance(path, str) else None
        if m is None or status != (want if m.group(2) == "lifecycle" else "modified"):
            problems.append(f"{path} ({status}) is not a {action}'s file")
        else:
            envs.add(m.group(1))
    if problems:
        return problems
    if not envs:
        return ["the pull request changes no environment"]
    names = {f["filename"] for f in files}
    for env in sorted(envs):
        d = f"apps/{owner}/{name}/{env}"
        if {f"{d}/lifecycle.yaml", f"{d}/composed.yaml"} - names:
            problems.append(f"{d}: a {action} changes both lifecycle.yaml and composed.yaml")
            continue
        try:
            before, after = _documents(read(f"{d}/composed.yaml", "base")), _documents(read(f"{d}/composed.yaml", "head"))
            # a suspend: exactly the integer 0; a resume: what base/manifests.yaml's Deployment says, absent when it says nothing
            # (compose then leaves replicas out)
            target = (True, 0) if action == "suspend" else _replicas(_deployment(_documents(read(f"{d}/base/manifests.yaml", "head"))))
            got = _replicas(_deployment(after))
        except (ValueError, yaml.YAMLError, RecursionError):  # UnicodeDecodeError is a ValueError
            problems.append(f"{d}/composed.yaml can't be compared")
            continue
        if len(before) != len(after) or any(_without_replicas(a) != _without_replicas(b) for a, b in zip(before, after)):
            problems.append(f"{d}/composed.yaml changes more than the Deployment's replicas")
            continue
        if got != target or (got[0] and type(got[1]) is not int):
            problems.append(f"{d}/composed.yaml sets replicas to {_shown(got)}, not {_shown(target)}")
        if action == "suspend":
            if not _is_this_suspension(read(f"{d}/lifecycle.yaml", "head"), req, env):
                problems.append(f"{d}/lifecycle.yaml is not this request's suspension")
    return problems


def _l5(gh, repo, req, step, files) -> list[str]:
    def read(path, which):
        found = _file(gh, repo, path, step.base_sha if which == "base" else step.head_sha)
        return None if found is None else found[1]
    return l5_problems(req.final["action"], req, files, read)


# ---- the notes (spec "The notes") ----

MERGED = {"suspend": "Merged to the deploy repo (merge commit {sha}): every environment scales to zero when the cluster's reconcile applies "
                     "it. Nothing is deleted.",
          "resume": "Merged to the deploy repo (merge commit {sha}): the cluster's reconcile brings the app back as it was reviewed.",
          "decommission": "Merged to the deploy repo (merge commit {sha}): the cluster's reconcile removes the workloads, the ExternalSecret, "
                          "the skills ConfigMap and the namespace's contents. The removed cloudflare-access.yaml tells the Cloudflare "
                          "reconcile job to revoke the app's tokens. This can't be undone."}
NOTHING_IN_DEPLOY = "Nothing of {repo} was in the deploy repo, so no pull request was needed there."
ALREADY_IN_DEPLOY = {"suspend": "The suspension of {repo} is already in the deploy repo (an earlier pull request of this request merged), so no "
                                "new pull request was needed: every environment scales to zero when the cluster's reconcile applies it.",
                     "resume": "{repo} is already resumed in the deploy repo (no lifecycle.yaml is left), so no new pull request was needed."}
SUMMARY = {"suspend": "Applied in git: {repo} is suspended for {task}. Every environment scales to zero when the cluster's reconcile applies "
                      "it; the platform can't see the cluster. Resume it through the portal.",
           "resume": "Applied in git: {repo} is resumed for {task}. The cluster's reconcile brings it back as it was reviewed.",
           "decommission": "Applied in git: {repo} is decommissioned for {task}: removed from the deploy repo, its repository archived, and "
                           "its intake record marked decommissioned. Kept: the images in ghcr, the Delinea secret values, and the Azure "
                           "identity and Databricks principal (their owners' to delete)."}


# A reason the code names, said plainly in the failed note (the reason stays the short code)
PLAIN = {"merged_with_other_commits": " The pull request was merged with commits the writer didn't write, so the platform can't say what "
                                      "the merge applied: a platform engineer checks the merge commit by hand before anything else is done."}


def _event(store, req, step, note, now):
    store.add_lifecycle_event(request_id=req.id, step=step, note=note[:3000], now=now)


def _move(store, req, step, to_state, event, note, now, **fields):
    """The step's move and the note that says so, in one transaction (L11: a note is never lost between two writes)."""
    return store.move_lifecycle_step_with_event(request_id=req.id, step=step.step, to_state=to_state, now=now, event=event, note=note[:3000],
                                                **fields)[0]


def _close_pr(gh, repo, number, note):
    """Best effort: say why on the PR and close it. Nothing it raises changes the step."""
    for method, path, body in (("POST", f"/repos/{repo}/issues/{number}/comments", {"body": note}),
                               ("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})):
        try:
            gh.call(method, path, ok=(200, 201), json=body)
        except Exception:  # noqa: BLE001 - best effort
            pass


def _fail_step(store, gh, cfg, req, step, reason, now) -> str:
    """The step failed (the request goes on, for an admin's Retry or Abandon): an open PR is closed with the reason (gh None: the step has
    no PR, as the archive), and the failed note links the PR."""
    reason = reason[:300]
    if gh is not None and step.state == "pr_open" and step.pr_number:
        _close_pr(gh, step_repo(cfg, step), step.pr_number, f"The golden road portal's writer stopped this step: `{reason}`. It never merges a "
                  "failed step, so it closes this pull request. An admin can retry it from the portal.")
    link = _pr_link(step.pr_url) if step.pr_url else ""
    plain = PLAIN.get(reason, "")
    _move(store, req, step, "failed", "failed", f"{_failed(step.step)}{reason}.{plain}{link} An admin can Retry it from the portal; a "
          "Retry will be noted here.", now, failure_reason=reason)
    return f"{req.id}: {step.step} failed ({reason})"


def _failed(step_name) -> str:
    return f"The {step_name} step failed: "


def _pr_link(url) -> str:
    return f" Its pull request: {url}."


# What a merged PR may fail with: once a failed note of the step says so and links the PR, the writer has judged that merge, and a
# Retry plans at main instead of failing the step again for it
MERGED_REASONS = ("merged_with_other_commits", "merged_file_differs", "merged_file_missing")


def _judged(store, req, step, url) -> bool:
    """A failed note of this step (the writer's own: 0012 lets only the writer add one) failed this merged PR already."""
    starts = tuple(f"{_failed(step.step)}{r}." for r in MERGED_REASONS)
    return any(e.step == "failed" and e.note.startswith(starts) and _pr_link(url) in e.note for e in store.lifecycle_events(req.id))


def _done(store, req, step, merge_sha, now, adopted=None) -> str:
    """The step is done in git: the state it applies (the deploy step: rule 4 holds from its merge), the step, and its note. The state
    first: set again by the same request it is kept as it is, so a pass that stopped between the two writes finishes them. The step
    and its note are one write."""
    action = req.final["action"]
    if step.step == "deploy":
        store.set_lifecycle_state(request_id=req.id, state=STATE_AFTER[action], now=now)  # repo and task are the request's
    if step.step == "deploy":
        event, note = "merged", (MERGED[action].format(sha=merge_sha) if merge_sha
                                 else ALREADY_IN_DEPLOY.get(action, NOTHING_IN_DEPLOY).format(repo=req.repo))
    elif step.step == "factory":
        event, note = "factory_merged", (f"The factory's intake record of {req.repo} now says decommissioned"
                                         + (f" (merge commit {merge_sha})." if merge_sha else "."))
    else:
        event, note = "archived", f"Repository {req.repo} archived: read-only, no push and no release; its code is kept for audit."
    if adopted is not None:  # (number, url) of a PR a person merged before the writer recorded it
        note = (f"Pull request #{adopted[0]} ({adopted[1]}) was merged before the writer recorded it (a pass stopped after opening it, "
                f"or it merged as the writer closed it for a rebuild); the writer checked the merge as its own. {note}")
    _move(store, req, step, "done", event, note, now, **({"merge_sha": merge_sha} if merge_sha else {}))
    return f"{req.id}: {step.step} done"


def _complete(store, req, steps, now) -> str:
    if not _still_decommissioned(store, req):  # every step done, then brought back by hand: nothing is set again, the task stays open
        reason = NOT_DECOMMISSIONED
        _event(store, req, "failed", f"The decommission of {req.repo} was not completed: {reason}. The app was brought back by hand "
               "after its steps were done, so the platform sets nothing again. The task stays open for ServiceNow to decide.", now)
        store.mark_failed(request_id=req.id, reason=reason, now=now)
        return f"{req.id}: failed ({reason})"
    merge = next((s.merge_sha for s in reversed(steps) if s.merge_sha), None)
    # one transaction: the state (kept: the deploy step set it), the completed note that closes the task, and writing -> merged
    store.apply_lifecycle(request_id=req.id, merge_sha=merge, note=SUMMARY[req.final["action"]].format(repo=req.repo, task=req.final["task"]),
                          now=now)
    return f"{req.id}: applied"


NOT_DECOMMISSIONED = "not_decommissioned_by_this_request"


def _still_decommissioned(store, req) -> bool:
    """A decommission's later steps and its completion: the app's state is still decommissioned, set by this request (platform-team
    procedure 18 brings an app back to active with the same request). Any other action: always."""
    if req.final["action"] != "decommission":
        return True
    state = store.lifecycle_state(req.repo)
    return state is not None and state.state == "decommissioned" and state.request == req.id


def _still_admin(store, person_id) -> bool:
    """Rule 1: the person (a request's requester, a command's) is a portal admin, by their roles at their last sign-in."""
    person = store.get_person(person_id)
    return person is not None and "admin" in person.last_roles


def _rule_4(store, req) -> str | None:
    """Rule 4 as the store has it now: a suspend is of an active app, a resume of a suspended one, and nothing after a decommission."""
    state = store.lifecycle_state(req.repo)
    current = state.state if state else "active"
    action = req.final["action"]
    if current == "decommissioned":
        return "decommissioned"
    if action == "suspend" and current != "active":
        return "not_active"
    if action == "resume" and current != "suspended":
        return "not_suspended"
    return None


def _recheck(store, gh, cfg, req) -> str | None:
    """Before any step: the requester is still a portal admin (rule 1, their roles at their last sign-in), and rules 3 and 4 as the writer
    sees them now. A suspend needs an environment in the deploy repo (item 3); a decommission is never of a platform repo (item 5), needs
    the lifecycle App (the writer refuses to start one without it) and an intake/v2 record (item 4)."""
    if not _still_admin(store, req.requested_by):
        return "not_admin"
    action = req.final["action"]
    problem = _rule_4(store, req)
    if problem:
        return problem
    if action == "suspend":
        repo = f"{cfg.org}/{DEPLOY}"
        head = _head(gh, repo)
        if all(_file(gh, repo, f"{environment_dir(req.repo, env)}/release.yaml", head) is None for env in ENVIRONMENTS):
            return "nothing_to_suspend"
    if action == "decommission":
        if is_platform_repo(req.repo, cfg.org):
            return "platform_repo"
        if req.repo.split("/")[0].lower() != cfg.org.lower():
            return "not_in_org"
        if cfg.lifecycle_app is None:
            return "no_lifecycle_app"
        repo = factory(cfg.org)
        found = _file(gh, repo, intake_path(req.repo), _head(gh, repo))
        try:
            record = _one_yaml_object(found[1]) if found else None
        except (ValueError, yaml.YAMLError, RecursionError):
            record = None
        if record is None or record.get("schema") != "intake/v2":
            return "record_not_intake_v2"
    return None


def advance(store, gh, cfg, req, now) -> str:
    """One pass of one lifecycle request (state writing)."""
    steps = store.lifecycle_steps(req.id)
    if not steps:
        try:
            problem = _recheck(store, gh, cfg, req)
        except Refused as exc:  # a path that isn't a file where the recheck reads one
            problem = exc.reason
        except (GitHubError, KeyError, TypeError, ValueError) as exc:
            return f"{req.id}: not planned yet ({_reason(exc)})"
        if problem:
            _event(store, req, "failed", f"The platform refused the request before changing anything: {problem}. Nothing was applied; ask "
                   "again once it is fixed.", now)
            command = store.open_lifecycle_command(req.id)  # asked while the request waited: never left open on a refused request
            if command is not None:
                from .lifecycle_commands import _name
                store.finish_lifecycle_command(command_id=command.id, now=now, note=(
                    f"{command.command.capitalize()} asked by {_name(store, command.requested_by)} was not carried out: the platform "
                    f"refused the request before changing anything ({problem})."))
            store.mark_failed(request_id=req.id, reason=f"recheck: {problem}", now=now)
            return f"{req.id}: failed (recheck: {problem})"
        steps = store.add_lifecycle_steps(request_id=req.id, steps=ACTION_STEPS[req.final["action"]], now=now)
    from .lifecycle_commands import carry_out  # Task 15: retry and abandon; the final review: stop
    finished = carry_out(store, gh, cfg, req, steps, now)
    if finished is not None:
        return finished
    steps = store.lifecycle_steps(req.id)
    current = next((s for s in steps if s.state != "done"), None)
    if current is None:
        return _complete(store, req, steps, now)
    if current.state == "failed":
        return f"{req.id}: {current.step} failed; waiting for Retry or Abandon"
    try:
        if current.step != "deploy" and not _still_decommissioned(store, req):
            raise Refused(NOT_DECOMMISSIONED)
        if current.step == "archive":
            from .lifecycle_archive import archive  # Task 14: the lifecycle App
            return archive(store, cfg, req, current, now)
        if current.state == "planned":
            return _open(store, gh, cfg, req, current, now)
        return _advance_pr(store, gh, cfg, req, current, now)
    except Refused as exc:
        return _fail_step(store, gh, cfg, req, current, exc.reason, now)
    except (GitHubError, KeyError, TypeError, ValueError) as exc:
        return _fail_step(store, gh, cfg, req, current, _reason(exc), now)


# ---- a PR step ----

def _commit(gh, repo, branch, planned, message) -> str:
    if planned.content is None:
        answer = gh.call("DELETE", f"/repos/{repo}/contents/{planned.path}", ok=(200,),
                         json={"message": message, "sha": _sha(planned.existing), "branch": branch}).json()
    else:
        body = {"message": message, "content": base64.b64encode(planned.content).decode("ascii"), "branch": branch}
        if planned.existing is not None:
            body["sha"] = _sha(planned.existing)
        answer = gh.call("PUT", f"/repos/{repo}/contents/{planned.path}", ok=(200, 201), json=body).json()
    return _sha(answer["commit"]["sha"])


def _chain_is(commits, planned) -> int | None:
    """How many of planned's files the commits (oldest first) are, one file each with its status, in order; None for anything else."""
    if commits is None or len(commits) > len(planned):
        return None
    for commit, p in zip(commits, planned):
        if not _only_these(commit.get("files") if isinstance(commit, dict) else None, {p.path: (p.status,)}):
            return None
    return len(commits)


def _done_on_branch(gh, repo, tip, base_sha, planned) -> int | None:
    """How many of planned's files an existing branch already holds as the writer's own commits with exactly the planned content (an
    interrupted pass), or None when it holds anything else."""
    if tip == base_sha:
        return 0
    for n in range(1, len(planned) + 1):
        chain = _chain(gh, repo, tip, base_sha, n)
        if chain is None:
            continue
        if _chain_is(chain, planned) != n:
            return None
        for p in planned[:n]:
            found = _file(gh, repo, p.path, tip)
            if (found is None) != (p.content is None) or (found is not None and found[1] != p.content):
                return None
        return n
    return None


def _attempt_branches(req, step) -> list[str]:
    """Every branch the step has had: portal/lifecycle-<request>-<step>-<n> for each attempt so far."""
    return [f"portal/lifecycle-{req.id}-{step.step}-{n}" for n in range(1, step.attempt + 1)]


def _unrecorded_merge(store, gh, req, step, repo):
    """(branch, PR) of the first PR of any attempt's branch that merged and that the writer hasn't judged (a pass stopped after opening
    it, or a person merged it as the writer closed it for a rebuild), or None. Raises GitHubError or ValueError when GitHub doesn't
    say."""
    for branch in _attempt_branches(req, step):
        for pr in _prs_on(gh, repo, branch):
            if pr["merged"] and not _judged(store, req, step, pr.get("html_url")):
                return branch, pr
    return None


def _adopt_merge(store, gh, cfg, req, step, repo, branch, pr, now) -> str:
    """A merged PR the step never recorded is checked as a recorded one is (_advance_pr): its head is the chain the writer plans at its
    base, it came from the target repo, and its files read as planned at the merge commit; else merged_with_other_commits."""
    url = pr["html_url"]  # _pr checked it
    try:
        base_sha, head_sha = _sha((pr.get("base") or {}).get("sha")), _sha((pr.get("head") or {}).get("sha"))
        merge_sha = _sha(pr.get("merge_commit_sha"))
        seen = dc.replace(step, branch=branch, pr_number=pr["number"], pr_url=url, head_sha=head_sha, base_sha=base_sha)
        planned = _plan(gh, cfg, req, seen, base_sha, store)
        ours = (bool(planned) and _from_the_target_repo(pr, repo)
                and _chain_is(_chain(gh, repo, head_sha, base_sha, len(planned)), planned) == len(planned))
    except GitHubError:
        return f"{req.id}: {step.step} waits: GitHub didn't answer for a merged pull request"
    except (Refused, KeyError, TypeError, ValueError):
        seen, ours = dc.replace(step, pr_url=url), False
    if not ours:
        return _fail_step(store, None, cfg, req, seen, "merged_with_other_commits", now)  # merged: nothing to close
    return _finish(store, gh, cfg, req, seen, merge_sha, now, _merged_at(pr), adopted=(pr["number"], url))


def _open(store, gh, cfg, req, step, now) -> str:
    repo = step_repo(cfg, step)
    if cfg.write_login is None:  # fail closed: no PR could be told for the writer's
        return f"{req.id}: {step.step} waits: the write App's login is unknown"
    try:
        merged = _unrecorded_merge(store, gh, req, step, repo)
    except (GitHubError, ValueError):  # unknown is never unmerged
        return f"{req.id}: {step.step} waits: GitHub didn't say whether a pull request of the step merged"
    if merged is not None:
        return _adopt_merge(store, gh, cfg, req, step, repo, *merged, now)
    base = _default_branch(gh, repo)
    base_sha = _sha(gh.call("GET", f"/repos/{repo}/git/ref/heads/{base}").json()["object"]["sha"])
    planned = _plan(gh, cfg, req, step, base_sha, store)
    if not planned:
        return _done(store, req, step, None, now)
    branch, message = branch_for(req, step), lifecycle_title(req, step.step)
    try:
        gh.call("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha}, ok=(201,))
        tip, done = base_sha, 0
    except GitHubError as exc:
        if exc.status != 422:
            raise
        tip = _sha(gh.call("GET", f"/repos/{repo}/git/ref/heads/{branch}").json()["object"]["sha"])
        done = _done_on_branch(gh, repo, tip, base_sha, planned)
        if done is None:
            if _stranded(gh, cfg, repo, branch, tip, base_sha):
                return _rebuild_stranded(store, gh, cfg, req, step, repo, branch, now)
            raise Refused("branch_not_ours") from None
    head = tip
    for p in planned[done:]:
        head = _commit(gh, repo, branch, p, message)
    owner = repo.split("/")[0]
    found = gh.call("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "all"}).json()
    body = lifecycle_body(req, step.step, store.get_person(req.requested_by), [p.path for p in planned],
                          lifecycle_merges(req.final["action"], step.step))
    pr = found[0] if found else gh.call("POST", f"/repos/{repo}/pulls", ok=(201,), json={
        "title": message, "head": branch, "base": base, "maintainer_can_modify": False, "body": body}).json()
    if not _our_pr(pr, base, head, repo, cfg.write_login):
        raise Refused("pr_not_ours")
    number, url = int(pr["number"]), str(pr["html_url"])
    if number < 1 or not PR_URL.fullmatch(url):
        raise ValueError("unexpected pull request")
    again = f" (attempt {step.attempt})" if step.attempt > 1 else ""
    _move(store, req, step, "pr_open", "pr_opened", f"Pull request #{number} opened in {repo}{again}: {url}", now, branch=branch,
          pr_number=number, pr_url=url, head_sha=head, base_sha=base_sha)
    return f"{req.id}: {step.step} pull request #{number} opened"


FORK_WALK = 50  # how far down a branch the writer looks for the current main before it calls the branch's fork point an old main


def _pr(gh, repo, number) -> dict:
    """One PR as GET /pulls/{number} shows it: the only answer with "merged". Raises ValueError when it doesn't say whether it merged
    (unknown is never taken for unmerged)."""
    pr = gh.call("GET", f"/repos/{repo}/pulls/{number}").json()
    if (not isinstance(pr, dict) or type(pr.get("merged")) is not bool or pr.get("number") != number
            or not isinstance(pr.get("html_url"), str) or not PR_URL.fullmatch(pr["html_url"])):  # the URL names it in the notes
        raise ValueError("unexpected pull request")
    return pr


def _prs_on(gh, repo, branch) -> list:
    """Every PR on the branch, each read on its own: the list answers GitHub's pull-request-simple shape, which has merged_at and
    merge_commit_sha but no "merged"."""
    found = gh.call("GET", f"/repos/{repo}/pulls", params={"head": f"{repo.split('/')[0]}:{branch}", "state": "all"}).json()
    if not isinstance(found, list) or not all(isinstance(p, dict) and type(p.get("number")) is int for p in found):
        raise ValueError("unexpected pull request list")
    return [_pr(gh, repo, p["number"]) for p in found]


def _by_the_writer(pr, cfg) -> bool:
    author = pr.get("user") or {}
    return cfg.write_login is not None and author.get("login") == cfg.write_login and author.get("type") == "Bot"


def _stranded(gh, cfg, repo, branch, tip, base_sha) -> bool:
    """The branch was left on an old main by a pass that stopped before recording its PR: every PR on it is the writer's, none merged,
    and the branch doesn't reach the current main (within FORK_WALK single-parent commits). Anything else is branch_not_ours."""
    prs = _prs_on(gh, repo, branch)
    if any(not _by_the_writer(p, cfg) or p.get("merged") for p in prs):
        return False
    sha = tip
    for _ in range(FORK_WALK):
        if sha == base_sha:
            return False
        commit = gh.call("GET", f"/repos/{repo}/commits/{sha}").json()
        parents = commit.get("parents") if isinstance(commit, dict) else None
        if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict):
            return True  # a root or a merge before the current main: not on it
        sha = _sha(parents[0].get("sha"))
    return sha != base_sha


def _repo_name(repo) -> str:
    return "factory" if repo.endswith(f"/{FACTORY}") else "deploy repo"


def _rebuild_stranded(store, gh, cfg, req, step, repo, branch, now) -> str:
    """Close the writer's open PRs on the stranded branch (never anyone else's: one may have opened since _stranded's read) and start
    a new attempt on a new branch from the current main, with a note that says which PR was closed. A PR that merges meanwhile is
    found by the next pass (_unrecorded_merge)."""
    closed = []
    for pr in _prs_on(gh, repo, branch):
        if pr.get("state") == "open" and not pr["merged"] and _by_the_writer(pr, cfg):
            _close_pr(gh, repo, pr["number"], "The default branch moved before the golden road portal's writer recorded this pull request, so "
                      "it closes it and rebuilds the change on the new main in a new pull request.")
            closed.append(f"#{pr['number']}")
    what = f"Pull request {', '.join(closed)} closed" if closed else f"Branch {branch} left"
    _move(store, req, step, "planned", "rebuilt", f"{what}: the {_repo_name(repo)}'s default branch moved before the writer recorded it "
          f"(branch {branch}), so the writer rebuilds the {step.step} step on the new main (attempt {step.attempt + 1}).", now)
    return f"{req.id}: {step.step} rebuilt on the new main next pass"


def _rebuild(store, gh, cfg, req, step, now) -> str:
    """(decided while planning: item 2) main moved: close the PR, and the next pass opens it again on the new main, on a new branch."""
    repo = step_repo(cfg, step)
    _close_pr(gh, repo, step.pr_number, "The default branch moved, so the golden road portal's writer rebuilds this change "
              "on the new main in a new pull request.")
    _move(store, req, step, "planned", "rebuilt", f"Pull request #{step.pr_number} ({step.pr_url}) closed: the {_repo_name(repo)}'s default "
          f"branch moved, so the writer rebuilds the {step.step} step on the new main (attempt {step.attempt + 1}).", now)
    return f"{req.id}: {step.step} rebuilt on the new main next pass"


def _finish(store, gh, cfg, req, step, merge_sha, now, merged_at=None, adopted=None) -> str:
    """The PR merged: every planned file reads as written (or is gone, for a deletion) at the merge commit, then the step is done. As
    run._finish: an error or a 404 inside confirm_grace leaves it pending; other content fails the step."""
    repo, pending = step_repo(cfg, step), f"{req.id}: {step.step} merged on GitHub, confirmation pending"
    try:
        planned = _plan(gh, cfg, req, step, step.base_sha, store)
        found = {p.path: _file(gh, repo, p.path, merge_sha) for p in planned}
    except Refused:
        return _fail_step(store, gh, cfg, req, step, "merged_file_differs", now)
    except (GitHubError, KeyError, TypeError, ValueError, AttributeError):
        return pending
    for p in planned:
        got = found[p.path]
        if p.content is None and got is not None or p.content is not None and got is not None and got[1] != p.content:
            return _fail_step(store, gh, cfg, req, step, "merged_file_differs", now)
        if p.content is not None and got is None:
            if merged_at is not None and now - merged_at > cfg.confirm_grace:
                return _fail_step(store, gh, cfg, req, step, "merged_file_missing", now)
            return pending
    return _done(store, req, step, merge_sha, now, adopted)


def _advance_pr(store, gh, cfg, req, step, now) -> str:
    repo = step_repo(cfg, step)
    pr = gh.call("GET", f"/repos/{repo}/pulls/{step.pr_number}").json()
    if pr.get("merged"):  # first: a person merged it, whatever it holds now, and that is what the step reports
        if (pr.get("head") or {}).get("sha") != step.head_sha or not _from_the_target_repo(pr, repo):
            return _fail_step(store, None, cfg, req, step, "merged_with_other_commits", now)  # merged: nothing to close
        return _finish(store, gh, cfg, req, step, _sha(pr["merge_commit_sha"]), now, _merged_at(pr))
    if (pr.get("head") or {}).get("sha") != step.head_sha:
        raise Refused("pr_head_changed")
    if not _from_the_target_repo(pr, repo):
        raise Refused("pr_not_ours")
    if pr.get("state") != "open":
        raise Refused("pr_closed")
    default = _default_branch(gh, repo)
    if (pr.get("base") or {}).get("ref") != default:
        raise Refused("pr_base_changed")
    current = _sha(gh.call("GET", f"/repos/{repo}/git/ref/heads/{default}").json()["object"]["sha"])
    if current != step.base_sha or pr.get("mergeable_state") == "behind" or (pr.get("base") or {}).get("sha") not in (None, current):
        return _rebuild(store, gh, cfg, req, step, now)
    planned = _plan(gh, cfg, req, step, step.base_sha, store)
    if _chain_is(_chain(gh, repo, step.head_sha, step.base_sha, len(planned)), planned) != len(planned):
        raise Refused("pr_commit_not_ours")
    files = list(gh.paginate(f"/repos/{repo}/pulls/{step.pr_number}/files"))
    if not _only_these(files, {p.path: (p.status,) for p in planned}):
        extra = sorted({f.get("filename") for f in files if isinstance(f, dict)} - {p.path for p in planned}, key=str)
        raise Refused("pr_files_unexpected" + (f": {', '.join(str(e) for e in extra)}" if extra else ""))
    target = Target(repo, "", "lifecycle")
    conclusions = check_conclusions(gh, cfg, target, step, default)
    if not conclusions:
        raise Refused("no_required_checks")
    failed = [n for n, c in conclusions.items() if c not in (None, "success")]
    if failed:
        raise Refused(f"check_failed: {failed[0]}")
    merges = lifecycle_merges(req.final["action"], step.step)
    if any(c is None for c in conclusions.values()):
        if merges and now - step.updated_at > cfg.merge_timeout:
            raise Refused("checks_timeout")
        return f"{req.id}: {step.step} waiting for checks"
    if not merges:
        return f"{req.id}: {step.step} waiting for review"
    problems = _l5(gh, repo, req, step, files)
    if problems:
        raise Refused("l5_refused: " + problems[0])
    if not _still_admin(store, req.requested_by):  # rule 1 again before each merge the writer makes
        raise Refused("not_admin")
    problem = _rule_4(store, req)  # and rule 4: another request may have changed the app's state meanwhile
    if problem:
        raise Refused(problem)
    try:
        merged = gh.call("PUT", f"/repos/{repo}/pulls/{step.pr_number}/merge",
                         json={"merge_method": cfg.merge_method, "sha": step.head_sha}).json()
    except GitHubError as exc:
        if exc.status in (405, 409) and now - step.updated_at <= cfg.merge_timeout:
            return f"{req.id}: {step.step} merge not allowed yet ({exc.status})"
        raise
    return _finish(store, gh, cfg, req, step, _sha(merged["sha"]), now)

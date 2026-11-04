"""Where the writer may write (P17). Intake records: a registration's tier and purpose, in the factory. Bindings: one environment's
connections (its Azure identity and Databricks connection) in golden-road-deploy, with the composed.yaml that golden-road compose
makes from them (spec 2026-10-29 GitOps deploy repo, G9 and correction 3). Only those files, for the request's own repo and
environment; never release.yaml, kustomization.yaml, base/ or the adapters under clusters/, which come from bump.yml. A binding
with MCP servers also has its cloudflare-access.yaml, the desired state compose makes from its mcp (spec 2026-10-31 M6).

A lifecycle request (spec 2026-11-02) writes a suspend's lifecycle.yaml and composed.yaml (resume deletes lifecycle.yaml), deletes a
decommissioned app's directory and adapters, and adds the decommissioned key to its intake record: lifecycle_path_allowed."""

import dataclasses as dc
import hashlib
import re

INTAKE = re.compile(r"intake/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.yaml")
DEPLOY_BINDING = re.compile(r"apps/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(test|prod)/bindings\.yaml")
COMPOSED = re.compile(r"apps/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(test|prod)/composed\.yaml")
CLOUDFLARE_ACCESS = re.compile(r"apps/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(test|prod)/cloudflare-access\.yaml")
LIFECYCLE = re.compile(r"apps/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(test|prod)/lifecycle\.yaml")
# A decommission deletes every file of its app's directory (golden_road.deploy.ALLOWED) and each environment's two adapters.
APP_FILE = re.compile(r"apps/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(test|prod)/(release\.yaml|bindings\.yaml|lifecycle\.yaml|composed\.yaml"
                      r"|kustomization\.yaml|cloudflare-access\.yaml|base/manifests\.yaml)")
ADAPTER_DIRS = ("clusters/home/flux", "clusters/office/argo")
NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
LIFECYCLE_WRITER_MERGES = ("suspend", "resume")  # L5: the one prod change the writer merges itself, inside writer.lifecycle.l5_problems
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ENVIRONMENTS = ("test", "prod")
# A binding for one of these is opened and checked by the writer but merged by a person, through main-review (code-owner review of
# bindings.yaml): GitHub can't limit the write App's bypass to a path, so the writer limits itself (spec 2026-10-30, P1).
REVIEWED = ("prod",)
FACTORY, DEPLOY = "scorecard-factory", "golden-road-deploy"
ALLOWED_PATHS = ("intake/<owner>/<repo>.yaml in <org>/scorecard-factory",
                 "apps/<owner>/<repo>/<test|prod>/bindings.yaml in <org>/golden-road-deploy",
                 "apps/<owner>/<repo>/<test|prod>/composed.yaml in <org>/golden-road-deploy",
                 "apps/<owner>/<repo>/<test|prod>/cloudflare-access.yaml in <org>/golden-road-deploy",
                 "apps/<owner>/<repo>/<test|prod>/lifecycle.yaml in <org>/golden-road-deploy (a lifecycle suspend; resume deletes it)",
                 "apps/<owner>/<repo>/ and its environments' adapters under clusters/ in <org>/golden-road-deploy, deleted (a decommission only)",
                 "intake/<owner>/<repo>.yaml's decommissioned key in <org>/scorecard-factory (a decommission only)")
KINDS = {"registration": (FACTORY, "intake.v2"), "binding": (DEPLOY, "binding.v2")}
COMPOSED_SCHEMA = "composed"  # not a JSON schema: the file is golden-road compose's output, checked byte for byte by deploy-check
ACCESS_SCHEMA = "cloudflare-access"  # compose's cloudflare_access output (cloudflare-access/v1), checked byte for byte by deploy-check
LIFECYCLE_SCHEMA = "lifecycle.v1"


@dc.dataclass(frozen=True)
class Target:
    repo: str
    path: str
    schema: str
    also: str | None = None  # a binding's second file, its environment's composed.yaml
    environment: str | None = None  # a binding's environment, from its stored draft
    access: str | None = None  # a binding's third file, its environment's cloudflare-access.yaml (written only for a binding with mcp)


def writer_merges(target: Target) -> bool:
    """True only for a registration (its intake record) and a test binding. Anything else, prod included, waits for a person."""
    if target.schema == "intake.v2":
        return target.environment is None
    return target.schema == "binding.v2" and target.environment == "test" and target.environment not in REVIEWED


def _clean(match) -> bool:
    return not {match.group(1), match.group(2)} & {".", ".."}


def allowed(org: str, repo: str, path: str) -> str | None:
    """The schema a file at repo/path must validate against (or "composed", "cloudflare-access"), or None when the writer may not write there."""
    if repo == f"{org}/{FACTORY}":
        rules = ((INTAKE, "intake.v2"),)
    elif repo == f"{org}/{DEPLOY}":
        rules = ((DEPLOY_BINDING, "binding.v2"), (COMPOSED, COMPOSED_SCHEMA), (CLOUDFLARE_ACCESS, ACCESS_SCHEMA), (LIFECYCLE, LIFECYCLE_SCHEMA))
    else:
        return None
    for pattern, schema in rules:
        match = pattern.fullmatch(path)
        if match:
            return schema if _clean(match) else None
    return None


def factory(org: str) -> str:
    return f"{org}/{FACTORY}"


def intake_path(repo: str) -> str:
    """The intake record's path for owner/repo (the repo already checked by target_for)."""
    owner, name = repo.split("/")
    return f"intake/{owner}/{name}.yaml"


def environment_dir(repo: str, environment: str) -> str:
    """apps/<owner>/<repo>/<env> in golden-road-deploy (the repo and environment already checked by target_for)."""
    owner, name = repo.split("/")
    return f"apps/{owner}/{name}/{environment}"


def target_for(org: str, request) -> Target | None:
    """The files a request may write, built from the request's own kind, stored repo and (for a binding) environment, never from
    the record's text. A binding's environment is its draft's: the request has no column for it (migration 0007)."""
    if request.kind not in KINDS or not REPO.fullmatch(request.repo) or {".", ".."} & set(request.repo.split("/")):
        return None
    if request.kind == "registration":
        return Target(factory(org), intake_path(request.repo), "intake.v2")
    environment = request.draft.get("environment") if isinstance(request.draft, dict) else None
    if not isinstance(environment, str) or environment not in ENVIRONMENTS:
        return None
    d = environment_dir(request.repo, environment)
    return Target(f"{org}/{DEPLOY}", f"{d}/bindings.yaml", "binding.v2", also=f"{d}/composed.yaml", environment=environment,
                  access=f"{d}/cloudflare-access.yaml")


def adapter_name(owner: str, repo: str, environment: str) -> str | None:
    """golden_road.deploy.adapter_name, which the portal can't import (tests/pipeline/test_portal_lifecycle_parity.py holds them equal):
    <owner>-<repo>-<env> lowercased with . and _ as -, cut to 56 and a 6-hex digest past 63. None for a name that isn't a DNS label."""
    full = re.sub(r"[._]", "-", f"{owner}-{repo}-{environment}".lower())
    if len(full) > 63:
        digest = hashlib.sha256(f"{owner}/{repo}/{environment}".encode("utf-8")).hexdigest()[:6]
        full = f"{full[:56].rstrip('-')}-{digest}"
    return full if NAMESPACE.fullmatch(full) else None


def lifecycle_merges(action: str, step: str) -> bool:
    """Only a suspend's or resume's deploy PR is merged by the writer (after L5); a decommission's PRs wait for a person."""
    return action in LIFECYCLE_WRITER_MERGES and step == "deploy"


def _ours(match, owner, name) -> bool:
    return bool(match) and _clean(match) and (match.group(1), match.group(2)) == (owner, name)


def lifecycle_path_allowed(org: str, target_repo: str, path: str, app_repo: str, action: str, step: str) -> bool:
    """Whether a lifecycle step may change `path` in `target_repo` for `app_repo` (the request's stored repo). A suspend or resume: its
    app's lifecycle.yaml and composed.yaml in the deploy repo. A decommission's deploy step: any file of its app's directory, and the
    two adapters of each of its environments. Its factory step: the app's intake record. The archive step writes no file."""
    if not REPO.fullmatch(app_repo) or {".", ".."} & set(app_repo.split("/")):
        return False
    owner, name = app_repo.split("/")
    if step == "factory":
        return action == "decommission" and target_repo == factory(org) and path == intake_path(app_repo)
    if step != "deploy" or target_repo != f"{org}/{DEPLOY}":
        return False
    if action in ("suspend", "resume"):
        return _ours(LIFECYCLE.fullmatch(path) or COMPOSED.fullmatch(path), owner, name)
    if action != "decommission":
        return False
    if path.startswith("apps/"):
        return _ours(APP_FILE.fullmatch(path), owner, name)
    names = {adapter_name(owner, name, env) for env in ENVIRONMENTS} - {None}
    return any(path == f"{d}/{n}.yaml" for d in ADAPTER_DIRS for n in names)

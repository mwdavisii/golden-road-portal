"""Where the writer may write (P17). Intake records: a registration's tier and purpose, in the factory. Bindings: one environment's
connections (its Azure identity and Databricks connection) in golden-road-deploy, with the composed.yaml that golden-road compose
makes from them (spec 2026-10-29 GitOps deploy repo, G9 and correction 3). Only those files, for the request's own repo and
environment; never release.yaml, kustomization.yaml, base/ or the adapters under clusters/, which come from bump.yml."""

import dataclasses as dc
import re

INTAKE = re.compile(r"intake/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.yaml")
DEPLOY_BINDING = re.compile(r"apps/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(test|prod)/bindings\.yaml")
COMPOSED = re.compile(r"apps/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(test|prod)/composed\.yaml")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ENVIRONMENTS = ("test", "prod")
FACTORY, DEPLOY = "scorecard-factory", "golden-road-deploy"
ALLOWED_PATHS = ("intake/<owner>/<repo>.yaml in <org>/scorecard-factory",
                 "apps/<owner>/<repo>/<test|prod>/bindings.yaml in <org>/golden-road-deploy",
                 "apps/<owner>/<repo>/<test|prod>/composed.yaml in <org>/golden-road-deploy")
KINDS = {"registration": (FACTORY, "intake.v2"), "binding": (DEPLOY, "binding.v2")}
COMPOSED_SCHEMA = "composed"  # not a JSON schema: the file is golden-road compose's output, checked byte for byte by deploy-check


@dc.dataclass(frozen=True)
class Target:
    repo: str
    path: str
    schema: str
    also: str | None = None  # a binding's second file, its environment's composed.yaml


def _clean(match) -> bool:
    return not {match.group(1), match.group(2)} & {".", ".."}


def allowed(org: str, repo: str, path: str) -> str | None:
    """The schema a file at repo/path must validate against (or "composed"), or None when the writer may not write there."""
    if repo == f"{org}/{FACTORY}":
        rules = ((INTAKE, "intake.v2"),)
    elif repo == f"{org}/{DEPLOY}":
        rules = ((DEPLOY_BINDING, "binding.v2"), (COMPOSED, COMPOSED_SCHEMA))
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
    return Target(f"{org}/{DEPLOY}", f"{d}/bindings.yaml", "binding.v2", also=f"{d}/composed.yaml")

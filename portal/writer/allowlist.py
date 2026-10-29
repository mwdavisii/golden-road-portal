"""Where the writer may write (P17). Intake records: a registration's tier and purpose. Bindings: the app's connections (its Azure
identity and Databricks connection). One file per request, in the factory, for the request's own repo; nothing else."""

import dataclasses as dc
import re

INTAKE = re.compile(r"intake/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.yaml")
BINDING = re.compile(r"bindings/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.yaml")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ALLOWED_PATHS = ("intake/<owner>/<repo>.yaml in <org>/scorecard-factory", "bindings/<owner>/<repo>.yaml in <org>/scorecard-factory")
KINDS = {"registration": ("intake", "intake.v2"), "binding": ("bindings", "binding.v1")}


@dc.dataclass(frozen=True)
class Target:
    repo: str
    path: str
    schema: str


def allowed(org: str, repo: str, path: str) -> str | None:
    """The schema a file at repo/path must validate against, or None when the writer may not write there."""
    if repo != f"{org}/scorecard-factory":
        return None
    for pattern, schema in ((INTAKE, "intake.v2"), (BINDING, "binding.v1")):
        match = pattern.fullmatch(path)
        if match:
            return None if {match.group(1), match.group(2)} & {".", ".."} else schema
    return None


def intake_path(repo: str) -> str:
    """The intake record's path for owner/repo (the repo already checked by target_for)."""
    owner, name = repo.split("/")
    return f"intake/{owner}/{name}.yaml"


def target_for(org: str, request) -> Target | None:
    """The one file a request may write, built from the request's own kind and repo (never from the record's text)."""
    if request.kind not in KINDS or not REPO.fullmatch(request.repo) or {".", ".."} & set(request.repo.split("/")):
        return None
    folder, schema = KINDS[request.kind]
    owner, name = request.repo.split("/")
    return Target(f"{org}/scorecard-factory", f"{folder}/{owner}/{name}.yaml", schema)

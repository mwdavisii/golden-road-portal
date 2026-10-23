"""Where the writer may write (P17). Slice 1: intake records in the factory, nothing else. Later slices add their kinds here."""

import dataclasses as dc
import re

INTAKE = re.compile(r"intake/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\.yaml")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ALLOWED_PATHS = ("intake/<owner>/<repo>.yaml in <org>/scorecard-factory",)


@dc.dataclass(frozen=True)
class Target:
    repo: str
    path: str
    schema: str


def allowed(org: str, repo: str, path: str) -> str | None:
    """The schema a file at repo/path must validate against, or None when the writer may not write there."""
    if repo != f"{org}/scorecard-factory":
        return None
    match = INTAKE.fullmatch(path)
    if not match or {match.group(1), match.group(2)} & {".", ".."}:
        return None
    return "intake.v2"


def target_for(org: str, request) -> Target | None:
    if request.kind != "registration" or not REPO.fullmatch(request.repo) or {".", ".."} & set(request.repo.split("/")):
        return None
    owner, name = request.repo.split("/")
    return Target(f"{org}/scorecard-factory", f"intake/{owner}/{name}.yaml", "intake.v2")

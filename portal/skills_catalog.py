"""The portal's skills catalog (spec 2026-10-31 Part 2): what the skills picker offers and what the writer puts in an app's skills
ConfigMap. The real catalog is the platform's golden-road-skills repo (platform.json's skills_repo): security approves each skill
by pull request, and a binding pins a commit on its main branch. There is only the mock for now: mock_skills_catalog.json, the
same bytes as the fixture deploy-verify reads (src/golden_road/data/mock_skills_catalog.json, a repo test keeps them equal), so
the writer's ConfigMap and deploy-check's are built from the same content.

A skill's skill.yaml (skill/v1) is the catalog's metadata: which environments may mount it and who may pick it. It is never
mounted, so Skill.files holds every other file. Offline, and checked whole on load."""

import dataclasses as dc
import json
import re
from pathlib import Path

import yaml

from .compose import UniqueKeyLoader, skill_files_problem
from .contracts import validate

MOCK = Path(__file__).resolve().parent / "mock_skills_catalog.json"
SCHEMA = "mock-skills-catalog/v1"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
COMMIT = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")  # builder.config's SKILLS_REPO
META = "skill.yaml"
EVERYONE = "everyone"


class SkillsCatalogError(ValueError):
    pass


@dc.dataclass(frozen=True)
class Skill:
    name: str
    description: str
    environments: tuple
    audience: str | tuple  # "everyone", or the identity provider groups that may pick it
    files: dict  # {path: text}, without skill.yaml

    def allows(self, environment: str, groups) -> bool:
        """The one rule: the environment may mount the skill, and the person is in its audience (everyone, or a shared group,
        matched exactly as roles are)."""
        if environment not in self.environments:
            return False
        if self.audience == EVERYONE:
            return True
        if isinstance(groups, str):  # one group name is not a list of them: set("finance") would be its letters
            return False
        return isinstance(self.audience, tuple) and bool(set(self.audience) & set(groups))


def _meta(name, text, where) -> dict:
    """skill.yaml's text as a valid skill/v1 naming exactly this skill."""
    try:
        doc = yaml.load(text, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise SkillsCatalogError(f"{where}: {META} is not YAML ({type(exc).__name__})") from None
    errors = validate("skill.v1", doc)
    if errors:
        raise SkillsCatalogError(f"{where}: {META} is not a valid skill/v1: {errors[0]}"[:300])
    if doc["name"] != name:
        raise SkillsCatalogError(f"{where}: {META} names {doc['name']}, not {name}")
    return doc


def _skill(name, files, where) -> Skill:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SkillsCatalogError(f"{where}: not a catalog name")
    if not isinstance(files, dict) or not all(isinstance(p, str) and isinstance(t, str) for p, t in files.items()):
        raise SkillsCatalogError(f"{where}: its files are not a mapping of path to text")
    if META not in files:
        raise SkillsCatalogError(f"{where}: no {META}")
    meta = _meta(name, files[META], where)
    mounted = {path: text for path, text in files.items() if path != META}
    problem = skill_files_problem(name, mounted)
    if problem:
        raise SkillsCatalogError(f"{where}: {problem}")
    audience = meta["audience"] if meta["audience"] == EVERYONE else tuple(meta["audience"])
    return Skill(name, meta["description"], tuple(meta["environments"]), audience, mounted)


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise SkillsCatalogError(f"the mock skills catalog names {str(key)[:80]!r} twice")
        out[key] = value
    return out


def _copy(skill: Skill) -> Skill:
    return dc.replace(skill, files=dict(skill.files))


class MockSkillsCatalog:
    """The fixture: {"schema", "repo", "main", "commits": {commit: {"skills": {name: {path: text}}}}}."""

    name = "mock-skills-catalog"  # what binding/v2 records as a skill's verified_by

    def __init__(self, repo: str, main: str, commits: dict):
        self.repo, self.main, self._commits = repo, main, commits  # {commit: {name: Skill}}

    def skill(self, name, commit) -> Skill | None:
        """The skill at that commit, or None when the catalog has no such skill there."""
        if not (isinstance(name, str) and isinstance(commit, str) and COMMIT.fullmatch(commit)):
            return None
        found = self._commits.get(commit, {}).get(name)
        return None if found is None else _copy(found)

    def offered(self, environment: str, groups) -> list[Skill]:
        """The skills at main that this environment may mount and a person in these groups may pick, by name."""
        groups = tuple(groups)
        return [_copy(s) for _, s in sorted(self._commits[self.main].items()) if s.allows(environment, groups)]


def load_mock(path=None) -> MockSkillsCatalog:
    """The mock catalog from mock_skills_catalog.json (or `path`), checked whole: every skill at every commit."""
    try:
        doc = json.loads(Path(path or MOCK).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except SkillsCatalogError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise SkillsCatalogError(f"the mock skills catalog can't be read ({type(exc).__name__})") from None
    if not isinstance(doc, dict) or set(doc) != {"schema", "repo", "main", "commits"} or doc["schema"] != SCHEMA:
        raise SkillsCatalogError(f"the mock skills catalog is not {SCHEMA} (schema, repo, main and commits)")
    if not isinstance(doc["repo"], str) or not REPO.fullmatch(doc["repo"]):
        raise SkillsCatalogError("the mock skills catalog's repo is not OWNER/NAME")
    commits = doc["commits"]
    if not isinstance(commits, dict) or not all(COMMIT.fullmatch(c) for c in commits):
        raise SkillsCatalogError("the mock skills catalog's commits are not keyed by 40-hex commit")
    if not isinstance(doc["main"], str) or doc["main"] not in commits:
        raise SkillsCatalogError("the mock skills catalog's main is not one of its commits")
    parsed = {}
    for commit, entry in commits.items():
        if not isinstance(entry, dict) or set(entry) != {"skills"} or not isinstance(entry["skills"], dict):
            raise SkillsCatalogError(f"the mock skills catalog's commit {commit[:12]} has no skills mapping")
        parsed[commit] = {name: _skill(name, files, f"the mock skills catalog's {str(name)[:80]} at {commit[:12]}")
                          for name, files in entry["skills"].items()}
    return MockSkillsCatalog(doc["repo"], doc["main"], parsed)

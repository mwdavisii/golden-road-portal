"""The writer's startup check (P18, Security): refuse to run if the write App can bypass more than it needs. GitHub can't
limit a bypass by path, so the check enforces the strongest shape GitHub can express (plan: Spec vs code, item 1). The App
bypasses only a review-only ruleset on the default branch, and only by pull request. The required checks still bind it. The
installation is limited to the target repos and the permissions below. The path limit is the writer's allowlist.

check_bypass returns a list of problems, and an empty list means the shape is right. Whatever it can't read counts as a
problem (fail closed). Problems are written here from ids and repo names that matched a strict pattern: GitHub's own text never
appears in one."""

import re

from ..github.client import GitHubError

ACTIONS_APP_ID = 15368
LEVELS = {"read": 1, "write": 2, "admin": 3}
# (a) and (d): everything the write installation may hold. administration:read is only there to see the bypass actors, and
# actions:read to trace a check run to its workflow run (GET /actions/runs needs it on a private or internal repo).
ALLOWED_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read", "checks": "read", "administration": "read", "actions": "read"}
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _default_branch_only(ruleset) -> bool:
    """A branch ruleset whose conditions name the default branch and nothing else (a missing exclude list means none)."""
    ref = (ruleset.get("conditions") or {}).get("ref_name")
    return ruleset.get("target") == "branch" and isinstance(ref, dict) and ref.get("include") == ["~DEFAULT_BRANCH"] and (ref.get("exclude") or []) == []


def _repo_label(name) -> str:
    return name if isinstance(name, str) and REPO.fullmatch(name) else "an unexpected repository"


def _installation_problems(app_gh, gh, installation_id, cfg) -> list[str]:
    problems = []
    try:
        doc = app_gh.call("GET", f"/app/installations/{installation_id}").json()
        if doc.get("repository_selection") != "selected":
            problems.append("the write installation must be limited to selected repositories, not all of them")
        permissions = doc.get("permissions")
        if not isinstance(permissions, dict):
            problems.append("can't read the write installation's permissions")
        else:
            for name, level in sorted(permissions.items(), key=lambda kv: str(kv[0])):
                allowed = ALLOWED_PERMISSIONS.get(name)
                if allowed is None or LEVELS.get(level, 99) > LEVELS[allowed]:
                    label = name if isinstance(name, str) and re.fullmatch(r"[a-z_]{1,40}", name) else "an unexpected permission"
                    problems.append(f"the write installation's permissions go beyond the allowed set: {label}")
        repos = {r.get("full_name") if isinstance(r, dict) else None for r in gh.paginate("/installation/repositories", key="repositories")}
    except (GitHubError, AttributeError, KeyError, TypeError, ValueError):
        return problems + ["can't read the write installation, so its permissions and repositories are unchecked"]
    for extra in sorted(repos - set(cfg.targets), key=str):
        problems.append(f"the write installation also covers {_repo_label(extra)}, which is not a target")
    for missing in sorted(set(cfg.targets) - repos):
        problems.append(f"the write installation does not cover {missing}")
    return problems


def _is_ours(actor, cfg) -> bool:
    return isinstance(actor, dict) and actor.get("actor_type") == "Integration" and actor.get("actor_id") == cfg.app_id


def _enforces(ruleset, check) -> bool:
    for rule in ruleset.get("rules") or []:
        if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
            for entry in (rule.get("parameters") or {}).get("required_status_checks") or []:
                if isinstance(entry, dict) and entry.get("context") == check and entry.get("integration_id") == ACTIONS_APP_ID:
                    return True
    return False


def _ruleset_problems(gh, repo, cfg) -> list[str]:
    problems, enforced = [], {name: False for name in cfg.required_checks}
    try:
        listing = gh.call("GET", f"/repos/{repo}/rulesets", params={"includes_parents": "true"}).json()
        ids = [r["id"] for r in listing if isinstance(r, dict) and type(r.get("id")) is int]
    except (GitHubError, KeyError, TypeError, ValueError):
        return [f"can't read the rulesets of {repo}, so the write App's bypass is unchecked"]
    for ruleset_id in ids:
        try:
            ruleset = gh.call("GET", f"/repos/{repo}/rulesets/{ruleset_id}", params={"includes_parents": "true"}).json()
            actors = ruleset["bypass_actors"] if isinstance(ruleset, dict) else None
        except (GitHubError, KeyError, TypeError, ValueError):
            actors = None
        if not isinstance(actors, list):
            problems.append(f"can't read the bypass actors of ruleset {ruleset_id} in {repo}")
            continue
        # Every ruleset counts, whatever its enforcement: one that is off or only evaluating today can be switched on tomorrow.
        where = f"ruleset {ruleset_id} in {repo}"
        types = [r.get("type") for r in ruleset.get("rules") or [] if isinstance(r, dict)]
        ours = [a for a in actors if _is_ours(a, cfg)]
        can_bypass = ruleset.get("current_user_can_bypass")  # what GitHub says about the caller (the installation), when it says
        if "required_status_checks" in types and (ours or can_bypass not in (None, "never")):
            problems.append(f"the write App can bypass status checks in {where}")
        if ours:
            if not _default_branch_only(ruleset):
                problems.append(f"{where}, which the write App can bypass, must target only the default branch")
            if types != ["pull_request"]:
                problems.append(f"{where}, which the write App can bypass, may hold only a pull_request rule")
            if any(a.get("bypass_mode") != "pull_request" for a in ours):
                problems.append(f"the write App's bypass_mode in {where} must be pull_request")
        if can_bypass not in (None, "never", "pull_requests_only") and "required_status_checks" not in types:
            problems.append(f"GitHub reports current_user_can_bypass {can_bypass!r} for the write App in {where}, more than a pull request bypass"
                            if isinstance(can_bypass, str) and re.fullmatch(r"[a-z_]{1,40}", can_bypass) else
                            f"GitHub reports an unexpected current_user_can_bypass in {where}")
        if not ours and can_bypass in (None, "never") and ruleset.get("enforcement") == "active" and _default_branch_only(ruleset):
            for check in enforced:
                if _enforces(ruleset, check):
                    enforced[check] = True
    problems += [f"{repo} has no active default-branch ruleset that the write App can't bypass and that requires {check}"
                 for check, found in enforced.items() if not found]
    return problems


def check_bypass(app_gh, gh, installation_id, cfg) -> list[str]:
    """app_gh calls GitHub as the App (a JWT), gh as the write installation (its token)."""
    problems = _installation_problems(app_gh, gh, installation_id, cfg)
    for repo in cfg.targets:
        problems += _ruleset_problems(gh, repo, cfg)
    return problems

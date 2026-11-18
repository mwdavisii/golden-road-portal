"""The agents part (spec 2026-11-17; decided while planning: item 29): which repos the read App sees are agent repos (builder-spec.json
says app_type agent at the default branch's head), and what each one's agent.yaml says. One pass replaces every agent_observed row; a pass
GitHub can't answer (Transient) changes nothing. One repo's odd answer (a refusal, a shape GitHub doesn't document) leaves that repo out
and never stops the others. The web's agent pages read these rows: the web never calls GitHub. Also the evidence's check that an agent
app's commit holds a valid agent.yaml (decided while planning: item 30). Every page of the installation's repositories is read (no cap)."""

import dataclasses as dc
import json

from ..agent_definition import model_ids
from ..agent_file import MAX_BYTES, AgentFileError, load
from ..github.client import GitHubError
from ..store.base import AgentObserved, check_agent_observed
from .repo_admins import REPO
from .repo_files import UNREADABLE, Malformed, Transient, head_commit, is_transient, read_stored, tree_modes

SPEC_CAP = 65536  # evidence.CAPS["builder-spec.json"]: the same file, the same bound
ODD = (Malformed, ValueError, KeyError, TypeError, AttributeError, IndexError)  # an answer that isn't the documented shape


def _problem(gh, repo, commit, modes, models):
    """(the agent/v1 document, None) or (None, the problem) for the commit's agent.yaml. A bad file is only ever AgentFileError."""
    raw = read_stored(gh, repo, "agent.yaml", commit, MAX_BYTES, modes)
    if raw is None:
        return None, "agent_yaml_missing"
    if raw is UNREADABLE:  # a symlink, a submodule, a directory, too large, or not base64
        return None, "agent_yaml_invalid"
    try:
        return load(raw, models=models), None
    except AgentFileError:
        return None, "agent_yaml_invalid"


def agent_yaml_problem(gh, repo, commit) -> str | None:
    """None when `commit` holds a valid agent.yaml (agent/v1, one of the platform's models); else why a registration waits."""
    return _problem(gh, repo, commit, tree_modes(gh, repo, commit), model_ids())[1]


def _is_agent_spec(raw) -> bool:
    if raw is None or raw is UNREADABLE:
        return False
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return False
    return isinstance(spec, dict) and spec.get("app_type") == "agent"


def _observed(gh, repo, models, now):
    """The repo's row, or None when it isn't an agent repo."""
    commit = head_commit(gh, repo)
    modes = tree_modes(gh, repo, commit)
    if not _is_agent_spec(read_stored(gh, repo, "builder-spec.json", commit, SPEC_CAP, modes)):
        return None
    agent, problem = _problem(gh, repo, commit, modes, models)
    row = AgentObserved(repo, commit, agent, problem, now)
    try:
        check_agent_observed([row])  # the store's own rules, per row, so one row it would refuse can't fail the pass
    except ValueError:
        if agent is None:
            return None
        row = dc.replace(row, agent=None, problem="agent_yaml_invalid")  # loads, but doesn't write back as itself
        check_agent_observed([row])
    return row


def collect_agents(gh, store, *, now) -> dict:
    models = model_ids()  # an unusable models file fails the pass (nothing changes), not each repo
    rows, seen = [], set()
    for item in gh.paginate("/installation/repositories", key="repositories", max_pages=None):
        full = item.get("full_name") if isinstance(item, dict) else None
        if not isinstance(full, str) or not REPO.fullmatch(full) or {".", ".."} & set(full.split("/")) or full.lower() in seen:
            continue
        seen.add(full.lower())
        try:
            row = _observed(gh, full, models, now)
        except Transient:
            raise
        except GitHubError as exc:
            if is_transient(exc):
                raise Transient(str(exc)) from exc
            continue  # a repo the read App can't read (403, 404, an empty repo's 409): not an agent repo it can vouch for
        except ODD:
            continue
        if row is not None:
            rows.append(row)
    store.replace_agent_observed(rows, observed_at=now)
    return {"agents": len(rows), "problems": sum(1 for r in rows if r.problem)}

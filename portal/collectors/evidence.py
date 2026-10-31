"""Registration evidence (spec 2026-10-23 E1): what a registered repo's default branch says about itself, read at one commit through
the read App. No model, and nothing from the registration draft. Every string in the result is repo data that the schema checks
against a pattern, or a line written here from a fixed template and filled only with checked values."""

import json
import re

from ..classification import LEVELS, RETIRED, retired_message, retired_values
from ..contracts import validate
from ..floor import compute_floor
from ..github.client import GitHubError
from ..store.base import Conflict
from ..writer.allowlist import factory
from .intake import RecordCaseMismatch, RecordUnreadable, current_record
from .repo_files import UNREADABLE, Transient, head_commit, read_stored, tree_modes
from .rules import RulesProblem, load_rules

# R2: file caps in bytes. package-lock.json may be over the contents API's 1 MiB inline limit: it is then read through its blob, up to 10 MiB.
CAPS = {"builder-spec.json": 65536, ".builder.lock": 65536, "requirements.txt": 262144, "package-lock.json": 10485760}
MAX_ITEMS = 100
MAX_NAME = 200  # registration-evidence/v1's bound on a copied name
LOCKFILES = {"python": ("requirements.txt",), "node": ("package-lock.json",), "static": ()}
PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==\S+")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHOWN = 10
MAX_LINE = 300


def _list(values) -> str:
    values = list(values)
    shown = ", ".join(values[:SHOWN])
    return shown + (f" and {len(values) - SHOWN} more" if len(values) > SHOWN else "")


def _signal(rule, line) -> dict:
    return {"rule": rule, "line": line[:MAX_LINE]}


def _unreadable(name) -> dict:
    return _signal(f"unreadable:{name}", f"{name} could not be read, or is larger than {CAPS[name]} bytes.")


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return UNREADABLE


def _spec(gh, repo, commit, modes, signals):
    """(the spec or None, the level a retired data_classification maps to or None)."""
    raw = read_stored(gh, repo, "builder-spec.json", commit, CAPS["builder-spec.json"], modes)
    if raw is None:
        signals.append(_signal("no_spec", "No builder-spec.json in the repository at this commit."))
        return None, None
    if raw is UNREADABLE:
        signals.append(_unreadable("builder-spec.json"))
        return None, None
    doc = _json(raw)
    retired = retired_values(doc)
    if retired:  # builder 0.5.0 (spec 2026-10-30 C4): name the replacement, and keep the level it maps to for the floor
        signals.append(_signal("no_spec", f"builder-spec.json: {retired_message(retired[0])}."))
        return None, max((RETIRED[v] for v in retired), key=LEVELS.index)
    if doc is UNREADABLE or validate("builder-spec.v1", doc):
        signals.append(_signal("no_spec", "builder-spec.json is not valid builder-spec/v1."))
        return None, None
    lists = [doc.get("egress", []), doc.get("env", []), (doc.get("authz") or {}).get("roles", []), (doc.get("authz") or {}).get("rules", [])]
    if any(len(items) > MAX_ITEMS for items in lists):
        signals.append(_unreadable("builder-spec.json"))
        return None, None
    authz = doc.get("authz") or {}
    copied = [e["name"] for e in doc["env"] if e.get("secret") is True] + [r["name"] for r in authz.get("roles", []) if r["access"] == "write"]
    if any(len(name) > MAX_NAME for name in copied):  # the evidence schema bounds the names it copies: a longer one is not a spec we accept
        signals.append(_signal("no_spec", "builder-spec.json is not valid builder-spec/v1."))
        return None, None
    return doc, None


def _count(name, doc_or_raw) -> int | None:
    if name == "requirements.txt":
        return sum(1 for line in doc_or_raw.decode("utf-8", "replace").splitlines() if PINNED.match(line))
    doc = _json(doc_or_raw)
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("packages"), dict):
        return sum(1 for key in doc["packages"] if key != "")
    if isinstance(doc.get("dependencies"), dict):
        return len(doc["dependencies"])
    return None


def _dependencies(gh, repo, commit, modes, spec, signals):
    names = LOCKFILES[spec["app_type"]] if spec else ("requirements.txt", "package-lock.json")
    for name in names:
        raw = read_stored(gh, repo, name, commit, CAPS[name], modes)
        if raw is None:
            continue
        count = None if raw is UNREADABLE else _count(name, raw)
        if count is None:
            signals.append(_unreadable(name))
            return {"count": 0, "lockfile_present": True, "lockfile": name}
        return {"count": count, "lockfile_present": True, "lockfile": name}
    return {"count": 0, "lockfile_present": False, "lockfile": None}


def _builder_version(gh, repo, commit, modes, signals):
    raw = read_stored(gh, repo, ".builder.lock", commit, CAPS[".builder.lock"], modes)
    if raw is None:
        return None
    doc = _json(raw) if raw is not UNREADABLE else UNREADABLE
    if not isinstance(doc, dict):
        signals.append(_unreadable(".builder.lock"))
        return None
    version = doc.get("builder_version")
    return version if isinstance(version, str) and VERSION.fullmatch(version) else None


def compute_evidence(gh, repo, commit, *, gateway_hosts, many_dependencies_over) -> dict:
    signals: list[dict] = []
    modes = tree_modes(gh, repo, commit)
    spec, retired = _spec(gh, repo, commit, modes, signals)
    version = _builder_version(gh, repo, commit, modes, signals)
    dependencies = _dependencies(gh, repo, commit, modes, spec, signals)
    doc = {"schema": "registration-evidence/v1", "repo": repo, "commit": commit, "spec_present": spec is not None,
           "data_classification": retired, "app_type": None, "resources_tier": None, "egress": [], "secret_env": [], "write_roles": [],
           "write_routes": 0, "builder_version": version, "dependencies": dependencies, "signals": signals}
    if spec is not None:
        authz = spec.get("authz") or {}
        doc.update(data_classification=spec["data_classification"], app_type=spec["app_type"], resources_tier=spec["resources_tier"],
                   egress=[{"host": e["host"], "port": e["port"], "model_gateway": e["host"] in gateway_hosts} for e in spec["egress"]],
                   secret_env=[e["name"] for e in spec["env"] if e.get("secret") is True],
                   write_roles=[r["name"] for r in authz.get("roles", []) if r["access"] == "write"],
                   write_routes=sum(1 for rule in authz.get("rules", []) if any(m != "GET" for m in rule["methods"])))
        fired = []
        if doc["write_roles"]:
            fired.append(_signal("write_roles", f"Write roles: {_list(doc['write_roles'])}."))
        if doc["secret_env"]:
            fired.append(_signal("secret_env", f"Secret settings: {_list(doc['secret_env'])}."))
        gateway = [f"{e['host']}:{e['port']}" for e in doc["egress"] if e["model_gateway"]]
        other = [f"{e['host']}:{e['port']}" for e in doc["egress"] if not e["model_gateway"]]
        if gateway:
            fired.append(_signal("model_gateway_egress", f"Calls the model gateway: {_list(gateway)}."))
        if other:
            fired.append(_signal("other_egress", f"Other egress: {_list(other)}."))
        signals.extend(fired)
    if dependencies["count"] > many_dependencies_over:
        signals.append(_signal("many_dependencies", f"{dependencies['count']} pinned dependencies (more than {many_dependencies_over})."))
    problems = validate("registration-evidence.v1", doc)
    if problems:
        raise ValueError(f"the computed evidence is not valid: {problems[0]}")
    return doc


def collect_evidence(gh, store, *, org, gateway_hosts, now) -> dict:
    """One pass: for each pending registration without evidence, read the factory's rules and the repo's default branch at one commit,
    compute the evidence and the floor, and store both. The store hands out only (request, repo, submission): never the draft.
    For a repo that already has an intake record (a change), the record's blob SHA and view, read at the rules commit, are stored with
    the evidence. A problem the portal can name is stored as a reason the request waits; a transient GitHub error stores nothing (next
    pass retries)."""
    waiting = store.awaiting_evidence()
    if not waiting:
        return {"computed": 0, "waiting": 0}

    def note(request, problem):
        try:
            store.add_evidence_problem(request_id=request.request_id, submission=request.submission, problem=problem, now=now)
        except Conflict:
            pass  # the request moved meanwhile

    try:
        rules, rules_commit = load_rules(gh, org)
    except RulesProblem as exc:
        for request in waiting:
            note(request, exc.code)
        return {"computed": 0, "waiting": len(waiting), "problem": exc.code}
    except Transient:
        return {"computed": 0, "waiting": len(waiting), "transient": True}
    computed, problem, transient, malformed = 0, None, False, 0
    for request in waiting:
        try:
            commit = head_commit(gh, request.repo)
            doc = compute_evidence(gh, request.repo, commit, gateway_hosts=gateway_hosts, many_dependencies_over=rules["many_dependencies_over"])
            record_sha, record = current_record(gh, factory(org), request.repo, rules_commit)  # a change, or (None, None)
        except (RecordUnreadable, RecordCaseMismatch) as exc:
            problem = "record_case_mismatch" if isinstance(exc, RecordCaseMismatch) else "record_unreadable"
            note(request, problem)
            continue
        except Transient:
            transient = True
            continue
        except GitHubError as exc:
            if exc.status in (403, 404):
                note(request, "repo_unreadable")
                problem = "repo_unreadable"
            else:
                transient = True
            continue
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            # GitHub's answer wasn't the documented shape (or a value wasn't a branch name or commit ID): skip this request, never crash
            malformed += 1
            continue
        floor_tier, floor_classification = compute_floor(doc, rules)
        try:
            store.add_evidence(request_id=request.request_id, submission=request.submission, commit=commit, evidence=doc,
                               floor_tier=floor_tier, floor_classification=floor_classification, rules_commit=rules_commit, now=now,
                               record_sha=record_sha, record=record)
            computed += 1
        except Conflict:
            continue  # evidence_exists or not_awaiting: another pass or a decision got there first
    result = {"computed": computed, "waiting": len(waiting) - computed}
    if problem:
        result["problem"] = problem
    if transient:
        result["transient"] = True
    if malformed:
        result["malformed"] = malformed
    return result

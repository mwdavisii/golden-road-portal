"""The writer trusts nothing it reads from the database (Security, writer): every claimed request is checked again. A registration
against its approval and the platform's evidence; a binding (which nobody approves) against the directory, its secrets against
Secret Server as the platform's account, and the requester's admin rights on the repo as the collector last saw them."""

from .. import env_names
from ..binding_record import MOCK_PASSWORD, MOCK_SERVER
from ..contracts import validate
from ..secretserver import SecretServerError
from ..floor import below_floor
from ..roles import roles_for
from .allowlist import COMPOSED_SCHEMA, ENVIRONMENTS, allowed, target_for


def _floor_problems(request, final, evidence) -> list[str]:
    """The record must carry the stored evidence's commit and floor, and sit at or above that floor, or carry the approver's override
    (an admin who isn't the requester, at least 30 characters, the approval's time, the very override the request holds)."""
    if evidence is None:
        return ["no_evidence"]
    if (final.get("evidence_commit"), final.get("floor_tier"), final.get("floor_classification")) != (
            evidence.commit, evidence.floor_tier, evidence.floor_classification):
        return ["evidence_mismatch"]
    below = below_floor(final["tier"], final["data_classification"], evidence.floor_tier, evidence.floor_classification)
    override = final.get("floor_override")
    problems = []
    if below and override is None:
        problems.append("below_floor")
    if override is None:
        if request.floor_override is not None:
            problems.append("override_invalid")
    elif (not below or override["by"] != request.decided_by or override["by"] == request.requested_by or len(override["reason"]) < 30
          or override["at"] != final["approved_at"] or override != request.floor_override):
        problems.append("override_invalid")
    return problems


def recheck(request, cfg, evidence, store=None, now=None) -> list[str]:
    """The problems with a claimed request, or [] when the writer may write it. store and now are needed for a binding (the
    requester's admin rights); without them a binding fails closed."""
    problems = [] if request.state == "writing" else ["not_approved"]
    if request.kind == "registration":
        return problems + _registration(request, cfg, evidence)
    if request.kind == "binding":
        return problems + _binding(request, cfg, store, now)
    return problems + ["unknown_kind"]


def _path_problems(request, cfg) -> list[str]:
    """Every file the request would write is on the allowlist, with the schema it is written as: a registration's intake record,
    or a binding's bindings.yaml and its environment's composed.yaml, and nothing else."""
    target = target_for(cfg.org, request)
    if target is None or allowed(cfg.org, target.repo, target.path) != target.schema:
        return ["path_not_allowlisted"]
    if (request.kind == "binding") != (target.also is not None) or (
            target.also is not None and allowed(cfg.org, target.repo, target.also) != COMPOSED_SCHEMA):
        return ["path_not_allowlisted"]
    return []


def _registration(request, cfg, evidence) -> list[str]:
    problems = []
    if not request.decided_by:
        problems.append("no_approver")
    elif request.decided_by == request.requested_by:
        problems.append("self_approval")
    if "admin" not in roles_for(request.decision_groups, cfg.role_groups):
        problems.append("approver_not_admin")
    problems += _path_problems(request, cfg)
    final = request.final
    if not isinstance(final, dict) or validate("intake.v2", final):
        problems.append("schema_invalid")
    elif (final["repo"], final["approved_by"], final["registered_by"], final["owner"], final["request"]) != (
            request.repo, request.decided_by, request.requested_by, request.requested_by, request.id):
        problems.append("record_mismatch")
    else:
        problems += _floor_problems(request, final, evidence)
    return problems


def _still_admin(request, cfg, store, now) -> bool:
    """The requester administers the request's repo as the collector saw it within the staleness limit (the rule the app page uses)."""
    if store is None or now is None:
        return False
    return any(r.repo.lower() == request.repo.lower() and now - r.observed_at <= cfg.repo_admins_stale_after
               for r in store.admin_repos_of_person(request.requested_by))


def _directory_problems(request, cfg, final) -> list[str]:
    """Every directory entry the binding names is still there, with the values recorded: the principal tagged for this repo, the
    warehouse granted to that principal."""
    directory = cfg.directory
    identity, data = final.get("azure_identity"), final.get("databricks")
    if not identity and not data:
        return []  # nothing the directory holds: secrets (or nothing) only
    if directory is None:
        return ["no_directory"]
    if identity:
        found = next((p for p in directory.principals_for(cfg.org, request.repo) if p.id == identity["directory_id"]), None)
        if found is None or (found.client_id, directory.tenant_id, directory.name) != (
                identity["client_id"], identity["tenant_id"], identity["verified_by"]):
            return ["not_in_directory"]
    if data:
        granted = directory.warehouses_for(identity["directory_id"]) if identity else []
        found = next((w for w in granted if w.id == data["directory_id"]), None)
        if found is None or (found.host, found.http_path, found.catalog, found.schema, directory.name) != (
                data["host"], data["http_path"], data.get("catalog"), data.get("schema"), data["verified_by"]):
            return ["not_in_directory"]
    return []


def _secret_problems(cfg, final) -> list[str]:
    """Every secret the binding names, as Secret Server's platform account (golden-road-eso) sees it now, through the client: still
    shared with that account (its summary answers), the field still a non-file field of the secret's template as it is now, and the
    env name still allowed. The person's own visibility can't be re-checked here: the writer has no person token, so the web checked
    it on submit (a recorded gap). An env name the app's own builder-spec sets is compose's to refuse (compose_refused).

    Only the mock is wired (MOCK ONLY): a real Secret Server needs the platform account's credential, which isn't, so it fails
    closed as no_secret_server. A reference checked by another Secret Server (its verified_by isn't this one's name) is
    secret_verified_elsewhere. A Secret Server error is secret_server_unavailable, never its text."""
    secrets = final.get("secrets") or []
    if not secrets:
        return []
    problems = ["secret_env_reserved"] if any(env_names.refused(s["env"]) for s in secrets) else []
    server = cfg.secret_server
    if server is None or getattr(server, "name", None) != MOCK_SERVER:
        return problems + ["no_secret_server"]
    if any(s["verified_by"] != server.name for s in secrets):
        return problems + ["secret_verified_elsewhere"]
    found = set()
    failed = False
    try:
        with server.client() as client:
            token = client.token(server.platform_user, MOCK_PASSWORD)
            for s in secrets:
                summary = client.secret_summary(token, s["secret_id"])  # None: the platform's account can't see it
                if summary is None:
                    found.add("secret_not_shared")
                elif s["field_slug"] not in {f.slug for f in client.template_fields(token, summary.template_id)}:
                    found.add("secret_field_gone")
    except SecretServerError:
        failed = True  # never the error's text: it lands in the request's failure reason
    if failed:
        return problems + ["secret_server_unavailable"]
    return problems + [p for p in ("secret_not_shared", "secret_field_gone") if p in found]


def _binding(request, cfg, store, now) -> list[str]:
    problems = []
    if request.decided_by or request.decided_at or request.decision_groups:
        problems.append("unexpected_decision")  # a binding is created approved; a decision on it was never made by the portal
    problems += _path_problems(request, cfg)
    final = request.final
    if not isinstance(final, dict) or final.get("schema") != "binding/v2" or final.get("environment") not in ENVIRONMENTS \
            or validate("binding.v2", final):
        return problems + ["schema_invalid"]
    if (final["repo"], final["request"], final["linked_by"]) != (request.repo, request.id, request.requested_by) or final != request.draft:
        problems.append("record_mismatch")
    problems += _directory_problems(request, cfg, final)
    problems += _secret_problems(cfg, final)
    if not _still_admin(request, cfg, store, now):
        problems.append("requester_not_admin")
    return problems

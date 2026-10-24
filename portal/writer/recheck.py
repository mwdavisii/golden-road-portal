"""The writer trusts nothing it reads from the database (Security, writer): every claimed request is checked again."""

from ..contracts import validate
from ..floor import below_floor
from ..roles import roles_for
from .allowlist import allowed, target_for


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


def recheck(request, cfg, evidence) -> list[str]:
    problems = []
    if request.state != "writing":
        problems.append("not_approved")
    if request.kind != "registration":
        problems.append("unknown_kind")
    if not request.decided_by:
        problems.append("no_approver")
    elif request.decided_by == request.requested_by:
        problems.append("self_approval")
    if "admin" not in roles_for(request.decision_groups, cfg.role_groups):
        problems.append("approver_not_admin")
    target = target_for(cfg.org, request)
    if target is None or allowed(cfg.org, target.repo, target.path) != target.schema:
        problems.append("path_not_allowlisted")
    final = request.final
    if not isinstance(final, dict) or validate("intake.v2", final):
        problems.append("schema_invalid")
    elif (final["repo"], final["approved_by"], final["registered_by"], final["owner"], final["request"]) != (
            request.repo, request.decided_by, request.requested_by, request.requested_by, request.id):
        problems.append("record_mismatch")
    else:
        problems += _floor_problems(request, final, evidence)
    return problems

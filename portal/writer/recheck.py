"""The writer trusts nothing it reads from the database (Security, writer): every claimed request is checked again."""

from ..contracts import validate
from ..roles import roles_for
from .allowlist import allowed, target_for


def recheck(request, cfg) -> list[str]:
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
    return problems

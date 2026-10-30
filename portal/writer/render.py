"""The file and the pull request the writer creates. The approver, the time and the justification are in both (P18). Text that
came from people (names, justifications) sits in code spans or fences, so it can't mention anyone or add markup."""

import re

import yaml

from .allowlist import writer_merges

HEADER = ("# Written by the golden road portal (request {id}). The only trusted source of this app's tier and purpose.\n"
          "# Change it through the portal.\n")


def intake_yaml(final: dict, request_id: str) -> str:
    return HEADER.format(id=request_id) + yaml.safe_dump(final, sort_keys=False, allow_unicode=True, width=1000)


BINDING_HEADER = ("# Written by the golden road portal (request {id}). The {env} environment's Azure identity and Databricks connection.\n"
                  "# Change it on the app's page in the portal.\n")


def binding_yaml(final: dict, request_id: str) -> str:
    return BINDING_HEADER.format(id=request_id, env=final.get("environment")) + yaml.safe_dump(final, sort_keys=False, allow_unicode=True, width=1000)


def content_for(request) -> str:
    """The file a request writes: its binding, or its intake record."""
    return binding_yaml(request.final, request.id) if request.kind == "binding" else intake_yaml(request.final, request.id)


def branch_name(request_id: str) -> str:
    return f"portal/request-{request_id}"


def title(request) -> str:
    if request.kind == "binding":
        return f"Connections for {request.repo} ({request.final.get('environment')}) (portal request {request.id})"
    return f"Register {request.repo} (portal request {request.id})"


def fenced(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _name(person) -> str:
    return "`" + (person.display_name if person else "unknown").replace("`", "'").replace("\n", " ") + "`"


def _evidence_lines(f) -> list[str]:
    if "evidence_commit" not in f:
        return []
    return [f"- Evidence: commit `{f['evidence_commit']}`, floor tier `{f['floor_tier']}`, classification `{f['floor_classification']}`"]


def _override_lines(f, approver) -> list[str]:
    override = f.get("floor_override")
    if not override:
        return []
    return [f"- Approved below the floor by {_name(approver)} (`{override['by']}`):", fenced(override["reason"]), ""]


WRITER_MERGES = "Written by the golden road portal's writer. It merges once the required checks pass."
PERSON_MERGES = ("Written by the golden road portal's writer. The writer never merges a prod binding: it needs a code owner's review "
                 "of `bindings.yaml`, and a person merges it. Don't update the branch: the writer takes only the commits it wrote. "
                 "If `main` moves before the merge, the request fails, the writer closes this PR, and the person must link it again "
                 "on the app's page.")


def _binding_body(request, requester, platform_user, writer_merges) -> str:
    """Nobody approves a binding: the body names who changed it, what is linked now, that only the directory (and, for secrets,
    Secret Server as its platform account) was consulted, and who merges it."""
    f = request.final
    linked = [f"Azure identity `{f['azure_identity']['directory_id']}`"] if f.get("azure_identity") else []
    linked += [f"Databricks connection `{f['databricks']['directory_id']}`"] if f.get("databricks") else []
    verified_by = (f.get("azure_identity") or {}).get("verified_by", "mock-directory")
    secrets = f.get("secrets") or []
    if secrets:  # references only (env, id, field: pattern-checked by binding/v2), never a value
        now_linked = ", ".join(linked) if linked else "no Azure identity or Databricks connection"
        secret_lines = ["- Secrets from Delinea Secret Server: " + ", ".join(
            f"`{s['env']}` (secret `{int(s['secret_id'])}`, field `{s['field_slug']}`)" for s in secrets),
            f"- Each secret checked against `{secrets[0]['verified_by']}` as its platform account"
            + (f" `{platform_user}`" if platform_user else "") + ": still shared with it, the field still its template's; the value "
            "never passes through the portal."]
    else:
        now_linked, secret_lines = ", ".join(linked) if linked else "neither (unlinked)", []
    return "\n".join([
        f"Portal request `{request.id}`: the connections of `{request.repo}` in `{f['environment']}`.", "",
        f"- Changed by {_name(requester)} (`{request.requested_by}`), an admin of the repository, at {f['linked_at']}",
        f"- Now linked: {now_linked}",
        *secret_lines,
        *([f"- Checked against the portal's directory (`{verified_by}`); nothing was verified with Azure or Databricks."]
          if linked or not secrets else []), "",
        f"- `apps/{request.repo}/{f['environment']}/composed.yaml` is what golden-road compose makes from the environment's pinned "
        "release and this binding; deploy-check composes it again and fails on any difference.", "",
        f"{WRITER_MERGES if writer_merges else PERSON_MERGES} Change it on the app's page in the portal.",
    ])


def pr_body(request, requester, approver, target, platform_user=None) -> str:
    """target: the request's allowlist.target_for, which says who merges (allowlist.writer_merges). platform_user: Secret Server's
    platform account (the writer's secret_server.platform_user), named for a binding's secrets."""
    if request.kind == "binding":
        return _binding_body(request, requester, platform_user, writer_merges(target))
    f = request.final
    return "\n".join([
        f"Portal request `{request.id}`: register `{request.repo}`.", "",
        f"- Requested by {_name(requester)} (`{request.requested_by}`)",
        f"- Approved by {_name(approver)} (`{request.decided_by}`) at {f['approved_at']}",
        f"- Tier `{f['tier']}`, data classification `{f['data_classification']}`, review by {f['review_by']}",
        *_evidence_lines(f), "",
        "Justification:", fenced(f["approval_justification"]), "",
        *_override_lines(f, approver),
        f"{WRITER_MERGES} Change this record through the portal.",
    ])

"""The file and the pull request the writer creates. The approver, the time and the justification are in both (P18). Text that
came from people (names, justifications) sits in code spans or fences, so it can't mention anyone or add markup."""

import re

import yaml

HEADER = ("# Written by the golden road portal (request {id}). The only trusted source of this app's tier and purpose.\n"
          "# Change it through the portal.\n")


def intake_yaml(final: dict, request_id: str) -> str:
    return HEADER.format(id=request_id) + yaml.safe_dump(final, sort_keys=False, allow_unicode=True, width=1000)


BINDING_HEADER = ("# Written by the golden road portal (request {id}). This app's Azure identity and Databricks connection.\n"
                  "# Change it on the app's page in the portal.\n")


def binding_yaml(final: dict, request_id: str) -> str:
    return BINDING_HEADER.format(id=request_id) + yaml.safe_dump(final, sort_keys=False, allow_unicode=True, width=1000)


def content_for(request) -> str:
    """The file a request writes: its binding, or its intake record."""
    return binding_yaml(request.final, request.id) if request.kind == "binding" else intake_yaml(request.final, request.id)


def branch_name(request_id: str) -> str:
    return f"portal/request-{request_id}"


def title(request) -> str:
    if request.kind == "binding":
        return f"Connections for {request.repo} (portal request {request.id})"
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


def _binding_body(request, requester) -> str:
    """Nobody approves a binding: the body names who changed it, what is linked now, and that only the directory was consulted."""
    f = request.final
    linked = [f"Azure identity `{f['azure_identity']['directory_id']}`"] if f.get("azure_identity") else []
    linked += [f"Databricks connection `{f['databricks']['directory_id']}`"] if f.get("databricks") else []
    verified_by = (f.get("azure_identity") or {}).get("verified_by", "mock-directory")
    return "\n".join([
        f"Portal request `{request.id}`: the connections of `{request.repo}`.", "",
        f"- Changed by {_name(requester)} (`{request.requested_by}`), an admin of the repository, at {f['linked_at']}",
        f"- Now linked: {', '.join(linked) if linked else 'neither (unlinked)'}",
        f"- Checked against the portal's directory (`{verified_by}`); nothing was verified with Azure or Databricks.", "",
        "Written by the golden road portal's writer. It merges once the required checks pass. Change it on the app's page in the portal.",
    ])


def pr_body(request, requester, approver) -> str:
    if request.kind == "binding":
        return _binding_body(request, requester)
    f = request.final
    return "\n".join([
        f"Portal request `{request.id}`: register `{request.repo}`.", "",
        f"- Requested by {_name(requester)} (`{request.requested_by}`)",
        f"- Approved by {_name(approver)} (`{request.decided_by}`) at {f['approved_at']}",
        f"- Tier `{f['tier']}`, data classification `{f['data_classification']}`, review by {f['review_by']}",
        *_evidence_lines(f), "",
        "Justification:", fenced(f["approval_justification"]), "",
        *_override_lines(f, approver),
        "Written by the golden road portal's writer. It merges once the required checks pass. Change this record through the portal.",
    ])

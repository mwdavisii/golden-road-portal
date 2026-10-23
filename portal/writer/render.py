"""The file and the pull request the writer creates. The approver, the time and the justification are in both (P18). Text that
came from people (names, justifications) sits in code spans or fences, so it can't mention anyone or add markup."""

import re

import yaml

HEADER = ("# Written by the golden road portal (request {id}). The only trusted source of this app's tier and purpose.\n"
          "# Change it through the portal.\n")


def intake_yaml(final: dict, request_id: str) -> str:
    return HEADER.format(id=request_id) + yaml.safe_dump(final, sort_keys=False, allow_unicode=True, width=1000)


def branch_name(request_id: str) -> str:
    return f"portal/request-{request_id}"


def title(request) -> str:
    return f"Register {request.repo} (portal request {request.id})"


def fenced(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _name(person) -> str:
    return "`" + (person.display_name if person else "unknown").replace("`", "'").replace("\n", " ") + "`"


def pr_body(request, requester, approver) -> str:
    f = request.final
    return "\n".join([
        f"Portal request `{request.id}`: register `{request.repo}`.", "",
        f"- Requested by {_name(requester)} (`{request.requested_by}`)",
        f"- Approved by {_name(approver)} (`{request.decided_by}`) at {f['approved_at']}",
        f"- Tier `{f['tier']}`, data classification `{f['data_classification']}`, review by {f['review_by']}", "",
        "Justification:", fenced(f["approval_justification"]), "",
        "Written by the golden road portal's writer. It merges once the required checks pass. Change this record through the portal.",
    ])

"""The file and the pull request the writer creates. The approver, the time and the justification are in both (P18). Text that
came from people (names, justifications) sits in code spans or fences, so it can't mention anyone or add markup."""

import datetime as dt
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


def title(request, change=False) -> str:
    if request.kind == "binding":
        return f"Connections for {request.repo} ({request.final.get('environment')}) (portal request {request.id})"
    return f"{'Change the registration of' if change else 'Register'} {request.repo} (portal request {request.id})"


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


MAX_CODE = 200  # a field in the PR body: GitHub refuses a body over 65536 characters


def _code(value) -> str:
    """A value from a record, in a code span: never markup, never a mention, and cut to MAX_CODE characters."""
    text = ("none" if value is None else str(value)).replace("`", "'").replace("\r", " ").replace("\n", " ")
    return "`" + (text[:MAX_CODE] + "…" if len(text) > MAX_CODE else text) + "`"


CHANGED_FIELDS = (("schema", "Record"), ("tier", "Tier"), ("data_classification", "Data classification"), ("owner", "Owner"),
                  ("review_by", "Review by"))


def _change_lines(f, current) -> list[str]:
    """What a change does to the record it replaces (spec 2026-10-31 C8): each field that moves, current → new; the purpose fenced."""
    if current is None:
        return []
    if current.get("schema") == "invalid":
        return ["Changes to the current record: it was not a valid intake record, and this replaces it whole.", ""]
    lines = [f"- {label}: {_code(current.get(key))} → {_code(f.get(key))}" for key, label in CHANGED_FIELDS if current.get(key) != f.get(key)]
    if current.get("purpose") != f["purpose"]:
        lines += ["- Purpose, from:", fenced(current.get("purpose") or ""), "  to:", fenced(f["purpose"])]
    return ["Changes to the current record:", *(lines or ["- none of its fields"]), ""]


WRITER_MERGES = "Written by the golden road portal's writer. It merges once the required checks pass."
PERSON_MERGES = ("Written by the golden road portal's writer. The writer never merges a prod binding: it needs a code owner's review "
                 "of `bindings.yaml`, and a person merges it. Don't update the branch: the writer takes only the commits it wrote. "
                 "If `main` moves before the merge, the request fails, the writer closes this PR, and the person must link it again "
                 "on the app's page.")


def _mcp_skills_lines(f, environment_dir, writes_access) -> list[str]:
    """The binding's MCP servers and skills (spec 2026-10-31): ids, names and commits only (pattern-checked by binding/v2), what each
    was checked against, and, when the PR writes it, what cloudflare-access.yaml is."""
    lines = []
    mcp = f.get("mcp")
    if mcp is not None:
        servers = mcp.get("servers") or []
        names = ", ".join(f"`{s['server_id']}`" for s in servers) if servers else "none (the app's token may reach no server)"
        lines.append(f"- MCP servers on `{mcp['gateway']}`: {names}")
        if servers:
            lines.append(f"- Each server checked against `{servers[0]['verified_by']}`: still on the gateway, ready, and usable with the "
                         "app's Cloudflare service token (not one that needs each person's own sign-in). The token comes from Secret "
                         "Server at its fixed path; its value never passes through the portal.")
    skills = f.get("skills") or []
    if skills:
        lines.append("- Skills: " + ", ".join(f"`{s['name']}` at `{s['commit'][:12]}`" for s in skills))
        lines.append(f"- Each skill read from `{skills[0]['verified_by']}` at its commit, approved there for `{f['environment']}`; "
                     "composed.yaml mounts it read-only at `/skills`.")
    if writes_access:
        lines.append(f"- `{environment_dir}/cloudflare-access.yaml` is the desired state a platform job reconciles into Cloudflare (the "
                     "app's service token and each server's Service Auth policy); nothing applies it from git.")
    return lines


def _binding_body(request, requester, platform_user, writer_merges, writes_access=False) -> str:
    """Nobody approves a binding: the body names who changed it, what is linked now, that only the directory (and, for secrets,
    Secret Server as its platform account; for MCP servers, Cloudflare; for skills, the catalog) was consulted, and who merges it."""
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
    extra = _mcp_skills_lines(f, f"apps/{request.repo}/{f['environment']}", writes_access)
    if not linked and extra:
        now_linked = "no Azure identity or Databricks connection"
    return "\n".join([
        f"Portal request `{request.id}`: the connections of `{request.repo}` in `{f['environment']}`.", "",
        f"- Changed by {_name(requester)} (`{request.requested_by}`), an admin of the repository, at {f['linked_at']}",
        f"- Now linked: {now_linked}",
        *secret_lines,
        *extra,
        *([f"- Checked against the portal's directory (`{verified_by}`); nothing was verified with Azure or Databricks."]
          if linked or not (secrets or extra) else []), "",
        f"- `apps/{request.repo}/{f['environment']}/composed.yaml` is what golden-road compose makes from the environment's pinned "
        "release and this binding; deploy-check composes it again and fails on any difference.", "",
        f"{WRITER_MERGES if writer_merges else PERSON_MERGES} Change it on the app's page in the portal.",
    ])


def pr_body(request, requester, approver, target, platform_user=None, current=None, writes_access=False) -> str:
    """target: the request's allowlist.target_for, which says who merges (allowlist.writer_merges). platform_user: Secret Server's
    platform account (the writer's secret_server.platform_user), named for a binding's secrets. current: for a change, the stored
    view of the record it replaces (its evidence's record). writes_access: the binding's PR writes its cloudflare-access.yaml."""
    if request.kind == "binding":
        return _binding_body(request, requester, platform_user, writer_merges(target), writes_access)
    f = request.final
    what = "change the registration of" if current is not None else "register"
    return "\n".join([
        f"Portal request `{request.id}`: {what} `{request.repo}`.", "",
        *_change_lines(f, current),
        f"- Requested by {_name(requester)} (`{request.requested_by}`)",
        f"- Approved by {_name(approver)} (`{request.decided_by}`) at {f['approved_at']}",
        f"- Tier `{f['tier']}`, data classification `{f['data_classification']}`, review by {f['review_by']}",
        *_evidence_lines(f), "",
        "Justification:", fenced(f["approval_justification"]), "",
        *_override_lines(f, approver),
        f"{WRITER_MERGES} Change this record through the portal.",
    ])


# ---- lifecycle (spec 2026-11-02) ----

Z = "%Y-%m-%dT%H:%M:%SZ"
LIFECYCLE_HEADER = ("# Written by the golden road portal (request {id}). The platform suspended this environment for ServiceNow task {task}.\n"
                    "# Resume it through the portal: golden-road compose keeps the Deployment at zero replicas while this file is here.\n")


def utc(when) -> str:
    """A stored time as lifecycle/v1 and intake/v2 write it: UTC, to the second."""
    return when.astimezone(dt.UTC).strftime(Z)


def lifecycle_yaml(request, environment: str) -> str:
    """A suspend's lifecycle.yaml (lifecycle/v1) for one environment. requested_at is the request's creation time, so a rebuilt or resumed
    step writes the same bytes (decided while planning: item 16)."""
    doc = {"schema": "lifecycle/v1", "repo": request.repo, "environment": environment, "state": "suspended", "task": request.final["task"],
           "reason": request.final["reason"], "requested_by": request.requested_by, "requested_at": utc(request.created_at),
           "request": request.id}
    return LIFECYCLE_HEADER.format(id=request.id, task=request.final["task"]) + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=1000)


LIFECYCLE_TITLES = {("suspend", "deploy"): "Suspend {repo}", ("resume", "deploy"): "Resume {repo}",
                    ("decommission", "deploy"): "Decommission {repo}: remove it from the deploy repo",
                    ("decommission", "factory"): "Decommission {repo}: mark its intake record decommissioned"}


def lifecycle_title(request, step: str) -> str:
    words = LIFECYCLE_TITLES[(request.final["action"], step)].format(repo=request.repo)
    return f"{words} (ServiceNow {request.final['task']}, portal request {request.id})"


def lifecycle_body(request, step: str, requester, paths, writer_merges: bool) -> str:
    """What the reviewer reads: who asked, for which task and why, every file, and who merges. Text from people is fenced or in code spans."""
    action = request.final["action"]
    verb = {"suspend": "suspends", "resume": "resumes"}.get(action, "decommissions")
    lines = [f"The platform {verb} `{request.repo}` for ServiceNow task `{request.final['task']}`, asked in the portal by {_name(requester)} "
             f"(`{request.requested_by}`), portal request `{request.id}`. Reason:",
             fenced(request.final["reason"]), "", "Files:", *(f"- `{p}`" for p in paths), ""]
    if writer_merges:
        lines.append("Written by the golden road portal's writer. The writer merges it once deploy-check passes, and only if it changes "
                     "nothing but lifecycle.yaml and the Deployment's replicas (spec L5). Don't push to the branch: the writer then never merges it.")
    elif step == "deploy":
        lines.append("Written by the golden road portal's writer. A platform reviewer reviews and merges it: it must delete "
                     f"`apps/{request.repo}/` and each environment's two adapters under `clusters/`, and nothing else. Once it merges, the "
                     "decommission can't be undone; the writer then archives the repository and opens the factory PR.")
    else:
        lines.append("Written by the golden road portal's writer. A security code owner of `intake/` reviews and merges it: it adds only the "
                     "`decommissioned` key. The app was already removed from the deploy repo and its repository archived.")
    return "\n".join(lines)

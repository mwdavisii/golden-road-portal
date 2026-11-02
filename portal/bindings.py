"""What an environment's binding (binding/v2, in golden-road-deploy's apps/<owner>/<repo>/<env>/bindings.yaml) adds to the
container: its Azure workload identity's client id, plain environment variables, the hosts it must reach on 443, and its Delinea
Secret Server references (an id and a field, never a value), which compose turns into one ExternalSecret.
golden-road compose applies it to the release's signed manifests to make that environment's composed.yaml; the portal shows the
same thing on the app's page and its writer composes with the same code. The two packages hold this file byte for byte (a repo
test keeps them equal), so they can't disagree. The input is a document that already validated (azure_identity and databricks
are the same in binding/v1 and binding/v2).

binding/v2 also names MCP servers on the environment's shared Cloudflare gateway and skills pinned to a catalog commit (spec
2026-10-31). MCP servers become plain env (MCP_URL, MCP_SERVERS), the gateway's egress, and the app's Cloudflare service token, read
from Secret Server at a path fixed by the repo and environment; skills become SKILLS_DIR and a read-only mount. Neither needs an
Azure identity."""

import re

AZURE_LOGIN = "login.microsoftonline.com"  # where the workload identity's token is exchanged
SKILLS_DIR = "/skills"  # where an environment's skills are mounted, read-only
MCP_SECRET_FOLDER = "/golden-road/mcp-tokens"  # Secret Server: the app's Cloudflare service token per environment (spec M5); no space
# A GitHub owner: letters, digits and single hyphens, never at either end, at most 39. So an owner never holds "--", which makes
# "--" an unambiguous separator in the token's name.
MCP_OWNER = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,38}")


def mcp_token_name(repo: str, environment: str) -> str:
    """cf-mcp-<owner>--<repo>-<env>, lower case (GitHub names are case-insensitive), the repo's "." and "_" kept (spec M5). One-to-one:
    the owner holds no "--" and doesn't end in "-", so the first "--" ends it; the environment is the fixed suffix. Raises ValueError
    for an owner GitHub wouldn't allow, since the name would no longer be one app's."""
    owner, _, name = repo.lower().partition("/")
    if not MCP_OWNER.fullmatch(owner) or not name:
        raise ValueError(f"{repo[:200]!r} has an owner that is not a GitHub owner, so it has no MCP token name")
    return f"cf-mcp-{owner}--{name}-{environment}"


def mcp_secret_path(repo: str, environment: str) -> str:
    """The token's Secret Server path. Fixed by the repo and environment, so no binding can point an app at another app's token."""
    return f"{MCP_SECRET_FOLDER}/{mcp_token_name(repo, environment)}"


def _mcp(binding: dict):
    """The MCP servers' effect, or None when the binding names none (an mcp with servers [] included)."""
    mcp = binding.get("mcp") or {}
    servers = sorted(s["server_id"] for s in mcp.get("servers") or [])
    if not servers:
        return None
    gateway = mcp["gateway"]
    return {"gateway": gateway, "url": f"https://{gateway}/mcp", "servers": servers,
            "token": mcp_token_name(binding["repo"], binding["environment"]),
            "secret_path": mcp_secret_path(binding["repo"], binding["environment"])}


def effects(binding) -> dict:
    """{"client_id", "env": [(name, value)], "egress": [host], "secrets": [(env, secret_id, field_slug)], "mcp": {...} or None,
    "skills": [(name, commit)]} for a binding. The identity's env and egress need an Azure identity, because the Databricks
    connection authenticates as it; secrets, MCP servers (their env, the gateway's egress, the token) and skills (SKILLS_DIR) don't.
    env is in order: the identity's, Databricks', MCP_URL, MCP_SERVERS, SKILLS_DIR. Secrets are sorted by env name, skills by name.
    A fresh dict every call, so no caller can change another's."""
    b = binding or {}
    secrets = sorted((s["env"], s["secret_id"], s["field_slug"]) for s in b.get("secrets") or [])
    skills = sorted((s["name"], s["commit"]) for s in b.get("skills") or [])
    mcp = _mcp(b)
    env, egress, client_id = [], set(), None
    identity = b.get("azure_identity")
    if identity:
        client_id = identity["client_id"]
        env += [("AZURE_CLIENT_ID", identity["client_id"]), ("AZURE_TENANT_ID", identity["tenant_id"])]
        egress.add(AZURE_LOGIN)
        data = b.get("databricks")
        if data:
            env += [("DATABRICKS_HOST", data["host"]), ("DATABRICKS_HTTP_PATH", data["http_path"])]
            env += [(name, data[key]) for name, key in (("DATABRICKS_CATALOG", "catalog"), ("DATABRICKS_SCHEMA", "schema")) if data.get(key)]
            egress.add(data["host"])
    if mcp:
        env += [("MCP_URL", mcp["url"]), ("MCP_SERVERS", ",".join(mcp["servers"]))]
        egress.add(mcp["gateway"])
    if skills:
        env.append(("SKILLS_DIR", SKILLS_DIR))
    return {"client_id": client_id, "env": env, "egress": sorted(egress), "secrets": secrets, "mcp": mcp, "skills": skills}

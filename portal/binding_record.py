"""The next binding/v2 document, one environment's, from what the app's admin picked on the app's page (spec 2026-10-29 app
bindings; GitOps deploy repo G3: bindings are per environment). Every choice is checked against the directory here, on the server:
the page only offers valid ones, and a hand-made POST gets the same answer. `repo` is the canonical owner/repo the portal stores;
it is recorded as given (deploy-check matches the file's path exactly)."""

import contextlib
import datetime as dt
import re

from . import env_names
from .cloudflare import CloudflareError
from .compose import ComposeError, skills_config_map
from .contracts import validate
from .secretserver import SecretServerError

REMOVABLE = ("azure-identity", "databricks")
ENVIRONMENTS = ("test", "prod")
OPEN_STATES = ("pending", "sent_back", "approved", "writing", "pr_open")  # store.base.OPEN_STATES (a test keeps them equal)
MAX_SECRETS = 20  # binding/v2's secrets maxItems
SECRET_ID_MAX = 2147483647
FIELD_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # binding/v2's secret_ref.field_slug
SECRET_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,126}[A-Za-z0-9._-])?")  # binding/v2's secret_ref.secret_name
MAX_MCP_SERVERS = 20  # binding/v2's mcp.servers maxItems
MAX_SKILLS = 20  # binding/v2's skills maxItems
SERVER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")  # binding/v2's mcp_server.server_id
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # binding/v2's skill_ref.name
DISPLAY_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,200}")  # binding/v2's display_name
ON_BEHALF = "This server needs each person's own sign-in, so an app can't use it."  # spec M4
CLOUDFLARE_DOWN = "Cloudflare could not answer: try again."
MOVED = ("The platform moved this environment's MCP gateway: remove its MCP servers first (one Remove takes them all off the old "
         "gateway), then make this change.")
# The mock's token endpoint does not check a password (it is a mock); the real version uses the person's own token (spec D7)
MOCK_PASSWORD = "<mock>"


def environment_of(request):
    """The environment a binding request is for: its draft's, or test for a request recorded before bindings were per environment
    (builder 0.3.16's one binding was what the test release rendered). None for anything else, which matches no environment."""
    env = (request.draft or {}).get("environment") if isinstance(request.draft, dict) else None
    if env is None and isinstance(request.draft, dict) and request.draft.get("schema") == "binding/v1":
        return "test"
    return env if env in ENVIRONMENTS else None


def environment_state(requests, environment):
    """(current binding or None, the open binding request or None, the latest change if it failed or None) for one environment,
    from a repo's requests newest first. The current binding is the newest merged one for that environment."""
    if environment not in ENVIRONMENTS:
        raise ValueError("not an environment")
    changes = [r for r in requests if r.kind == "binding" and environment_of(r) == environment]
    current = next((r.final for r in changes if r.state == "merged"), None)
    open_one = next((r for r in changes if r.state in OPEN_STATES), None)
    failed = changes[0] if changes and changes[0].state == "failed" else None
    return current, open_one, failed


class BindingRefused(Exception):
    pass


class SecretServerUnavailable(BindingRefused):
    """Secret Server didn't answer: nothing about the choice was refused, so a page says so with a 503."""


class CloudflareUnavailable(BindingRefused):
    """Cloudflare didn't answer: nothing about the choice was refused, so a page says so with a 503."""


def _tagged(directory, org, repo, principal_id):
    return next((p for p in directory.principals_for(org, repo) if p.id == principal_id), None)


def _databricks(directory, warehouse) -> dict:
    """The Databricks block as the directory has the warehouse now, never as an old binding recorded it."""
    data = {"directory_id": warehouse.id, "host": warehouse.host, "http_path": warehouse.http_path, "verified_by": directory.name}
    data.update({k: v for k, v in (("catalog", warehouse.catalog), ("schema", warehouse.schema)) if v})
    return data


MOCK_SERVER = "mock-secret-server"  # portal.secretserver_mock.MockSecretServer.name


@contextlib.contextmanager
def secret_tokens(secret_server, secret_user):
    """(client, the person's token, the platform account's token), the client closed afterwards. Through the client exactly as a
    real instance is used; in the mock the person is the Secret Server user their verified GitHub link maps to.

    Mock only (spec D7): minting the person's token with the password grant is what the mock allows, because it checks no
    password. A real Secret Server must use the person's own token from their sign-in (Secret Server federated with the same
    identity provider), never one the portal mints, so anything but the mock is refused here."""
    if getattr(secret_server, "name", None) != MOCK_SERVER:
        raise RuntimeError("only the mock Secret Server mints a person's token; a real one must use the person's own (spec D7)")
    with secret_server.client() as client:
        yield client, client.token(secret_user, MOCK_PASSWORD), client.token(secret_server.platform_user, MOCK_PASSWORD)


def _secret(secret_server, secret_user, secrets, choice) -> dict:
    """A new secret reference, checked as the person (they can see it) and as the platform (it is shared with the platform's account
    and the field is one of its template's non-file fields). Only the id, the field and the env name come from the form; the
    name and the folder are Secret Server's."""
    if secret_server is None:
        raise BindingRefused("Secret Server is not set up here.")
    if not isinstance(secret_user, str) or not secret_user:
        raise BindingRefused("Link your GitHub account (verified) first: the portal finds your Secret Server user through it.")
    secret_id, slug, env = (choice.get(k) for k in ("secret_id", "field_slug", "env")) if isinstance(choice, dict) else (None,) * 3
    if type(secret_id) is not int or not 1 <= secret_id <= SECRET_ID_MAX or not isinstance(slug, str) or not FIELD_SLUG.fullmatch(slug):
        raise BindingRefused("Choose a secret and one of its fields.")
    why = env_names.refused(env)
    if why:
        raise BindingRefused(why)
    if any(s["env"] == env for s in secrets):
        raise BindingRefused(f"{env} already holds a secret in this environment: remove it first, or choose another name.")
    if len(secrets) >= MAX_SECRETS:
        raise BindingRefused(f"An environment has at most {MAX_SECRETS} secrets.")
    failure = None
    try:
        with secret_tokens(secret_server, secret_user) as (client, person_token, platform_token):
            summary = client.secret_summary(person_token, secret_id)  # the person's own view: what they can see
            if summary is None:
                raise BindingRefused("You can't see this secret in Secret Server.")
            if not client.shared_with_platform(platform_token, secret_id):
                raise BindingRefused(f"This secret isn't shared with {secret_server.platform_user}: ask security to share it.")
            if slug not in {f.slug for f in client.template_fields(platform_token, summary.template_id)}:
                raise BindingRefused("That is not a field this secret can deliver: choose one of its template's fields (never a file).")
    except SecretServerError:
        failure = "Secret Server could not answer: try again."  # never the error's text: the person sees this
    if failure:
        raise SecretServerUnavailable(failure)
    if not SECRET_NAME.fullmatch(summary.name):
        raise BindingRefused("This secret's name can't be recorded in a binding: rename it in Secret Server (letters, digits, "
                             "spaces, . _ -).")
    return {"env": env, "secret_id": summary.id, "secret_name": summary.name, "folder_id": summary.folder_id, "field_slug": slug,
            "verified_by": secret_server.name}


def _mcp_server(cloudflare, gateway_host, person_email, mcp, environment, server_id) -> dict:
    """A new MCP server for this environment, checked in Cloudflare (spec 2026-10-31 M2 to M4): on the environment's gateway, ready,
    not one that acts as each signed-in person, and one the person may use (the mock's per-user view, M8). Only the id comes from the
    form; the name is Cloudflare's."""
    if cloudflare is None or not gateway_host:
        raise BindingRefused("Cloudflare is not set up here.")
    if not isinstance(server_id, str) or not SERVER_ID.fullmatch(server_id):
        raise BindingRefused("Choose a server from the list.")
    if not isinstance(person_email, str) or not person_email:
        raise BindingRefused("Your sign-in has no email address, so Cloudflare can't tell which servers you may use.")
    servers = (mcp or {}).get("servers") or []
    if any(s["server_id"] == server_id for s in servers):
        raise BindingRefused(f"{server_id} is already on this app in {environment}.")
    if len(servers) >= MAX_MCP_SERVERS:
        raise BindingRefused(f"An environment has at most {MAX_MCP_SERVERS} MCP servers.")
    if servers and mcp.get("gateway") != gateway_host:  # with none left, the new gateway simply replaces the old
        raise BindingRefused("This environment's MCP gateway has changed: remove its servers and add them again.")
    failure = None
    try:
        with cloudflare.client() as client:
            gateway = client.gateway(gateway_host)
    except CloudflareError:
        failure = CLOUDFLARE_DOWN  # never the error's text: the person sees this
    if failure:
        raise CloudflareUnavailable(failure)
    if gateway is None:
        raise BindingRefused(f"The {environment} MCP gateway ({gateway_host}) is not in Cloudflare.")
    found = next((s for s in gateway.servers if s.server_id == server_id), None)
    if found is None:
        raise BindingRefused(f"That server is not on the {environment} gateway.")
    if found.status != "ready":
        raise BindingRefused("That server is not ready in Cloudflare.")
    if found.on_behalf:
        raise BindingRefused(ON_BEHALF)
    if server_id not in cloudflare.servers_for(person_email):
        raise BindingRefused("You can't use that server in Cloudflare.")
    if not isinstance(found.name, str) or not DISPLAY_NAME.fullmatch(found.name):
        raise BindingRefused("This server's name can't be recorded: ask the gateway's admins to rename it.")
    return {"server_id": found.server_id, "name": found.name, "verified_by": cloudflare.name}


def _skill(catalog, skills, environment, groups, name, update) -> list:
    """The skills with `name` added (or, for update, re-pinned) at the catalog's main commit (spec 2026-10-31 S2, S3): the catalog
    has it, it is approved for the environment and the person is in its audience, and every bound skill still fits one ConfigMap."""
    if catalog is None:
        raise BindingRefused("The skills catalog is not set up here.")
    if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
        raise BindingRefused("Choose a skill from the list.")
    found = catalog.skill(name, catalog.main)
    if found is None:
        raise BindingRefused("That skill is not in the catalog.")
    bound = next((s for s in skills if s["name"] == name), None)
    if update:
        if bound is None:
            raise BindingRefused(f"{name} is not on this app in {environment}: there is nothing to update.")
        if bound["commit"] == catalog.main:
            raise BindingRefused(f"{name} is already at the catalog's current version.")
    else:
        if bound is not None:
            raise BindingRefused(f"{name} is already on this app in {environment}: use Update to move it to the catalog's current version.")
        if len(skills) >= MAX_SKILLS:
            raise BindingRefused(f"An environment has at most {MAX_SKILLS} skills.")
    if environment not in found.environments:
        raise BindingRefused(f"That skill is not approved for {environment}.")
    if not found.allows(environment, groups):
        raise BindingRefused("That skill is for some groups only, and you are not in one.")
    after = [s for s in skills if s["name"] != name] + [{"name": name, "commit": catalog.main, "verified_by": catalog.name}]
    contents = {}
    for s in after:
        pinned = found if s["name"] == name else catalog.skill(s["name"], s["commit"])
        if pinned is None:
            raise BindingRefused(f"{s['name']} at {s['commit'][:7]} is no longer in the catalog: update it or remove it first.")
        contents[s["name"]] = pinned.files
    try:
        skills_config_map("app", contents)
    except ComposeError:
        raise BindingRefused("Together these skills are over 512 KiB, the most one environment can mount.") from None
    return after


def next_binding(*, current, repo, org, directory, person_id, request_id, now, environment, principal_id=None, warehouse_id=None,
                 remove=None, add_secret=None, remove_secret=None, secret_server=None, secret_user=None, add_mcp=None, remove_mcp=None,
                 add_skill=None, update_skill=None, remove_skill=None, cloudflare=None, mcp_gateway=None, person_email=None,
                 skills_catalog=None, person_groups=()) -> dict:
    """Exactly one of principal_id, warehouse_id, remove, add_secret ({"secret_id", "field_slug", "env"}), remove_secret (an env
    name), add_mcp or remove_mcp (a server id), add_skill, update_skill or remove_skill (a skill name). `current` is the binding the
    environment has now (or None); only its identity, connection, secrets, MCP servers and skills carry over. secret_server and
    secret_user (the person's Secret Server user) are needed only for add_secret; cloudflare, mcp_gateway (the environment's
    gateway host, from settings) and person_email for add_mcp (mcp_gateway also for every other choice, so a binding still naming
    a gateway the platform moved from is cleared, never carried on); skills_catalog and person_groups (the sign-in's groups) only
    for add_skill and update_skill."""
    if not isinstance(environment, str) or environment not in ENVIRONMENTS:
        raise BindingRefused("The environment must be test or prod.")
    if sum(x is not None for x in (principal_id, warehouse_id, remove, add_secret, remove_secret, add_mcp, remove_mcp, add_skill,
                                   update_skill, remove_skill)) != 1:
        raise BindingRefused("Choose one thing to change.")
    identity = dict((current or {}).get("azure_identity") or {}) or None
    data = dict((current or {}).get("databricks") or {}) or None
    secrets = [dict(s) for s in ((current or {}).get("secrets") or [])]
    had_mcp, had_skills = "mcp" in (current or {}), "skills" in (current or {})
    mcp = {"gateway": current["mcp"]["gateway"], "servers": [dict(s) for s in current["mcp"].get("servers") or []]} if had_mcp else None
    skills = [dict(s) for s in ((current or {}).get("skills") or [])]
    # The platform moved this environment to another gateway (settings, platform.json): deploy-check refuses a binding naming the old
    # one, so no change may keep it. A removal clears every server on it at once; any other change waits until that is done.
    moved = mcp is not None and isinstance(mcp_gateway, str) and bool(mcp_gateway) and mcp["gateway"] != mcp_gateway
    if moved and mcp["servers"] and remove_mcp is None and add_mcp is None:
        raise BindingRefused(MOVED)
    if add_mcp is not None:
        added = _mcp_server(cloudflare, mcp_gateway, person_email, mcp, environment, add_mcp)
        mcp = {"gateway": mcp_gateway, "servers": (mcp or {}).get("servers", []) + [added]}
    elif remove_mcp is not None:
        if not isinstance(remove_mcp, str) or not SERVER_ID.fullmatch(remove_mcp) \
                or not any(s["server_id"] == remove_mcp for s in (mcp or {}).get("servers") or []):
            raise BindingRefused("This environment has no MCP server by that id: there is nothing to remove.")  # never the form's text
        # the key stays: cloudflare-access.yaml says so. Off a moved gateway, every server goes in one request.
        mcp["servers"] = [] if moved else [s for s in mcp["servers"] if s["server_id"] != remove_mcp]
    elif add_skill is not None or update_skill is not None:
        skills = _skill(skills_catalog, skills, environment, person_groups, add_skill if add_skill is not None else update_skill,
                        update=update_skill is not None)
    elif remove_skill is not None:
        if not isinstance(remove_skill, str) or not SKILL_NAME.fullmatch(remove_skill) or not any(s["name"] == remove_skill for s in skills):
            raise BindingRefused("This environment has no skill by that name: there is nothing to remove.")  # never the form's text
        skills = [s for s in skills if s["name"] != remove_skill]
    elif add_secret is not None:
        secrets.append(_secret(secret_server, secret_user, secrets, add_secret))
    elif remove_secret is not None:
        if not isinstance(remove_secret, str) or not env_names.ENV_NAME.fullmatch(remove_secret) \
                or not any(s["env"] == remove_secret for s in secrets):
            raise BindingRefused("This environment has no secret by that name: there is nothing to remove.")  # never the form's text
        secrets = [s for s in secrets if s["env"] != remove_secret]
    elif principal_id is not None:
        found = _tagged(directory, org, repo, principal_id)
        if found is None:
            raise BindingRefused(f"This principal isn't tagged for {repo}.")
        if identity is None or identity["directory_id"] != found.id or data is None \
                or data["directory_id"] not in {w.id for w in directory.warehouses_for(found.id)}:
            data = None  # a new identity has none of the old one's grants
        else:
            data = _databricks(directory, directory.warehouse(data["directory_id"]))  # kept, so rebuilt like the identity
        identity = {"directory_id": found.id, "tenant_id": directory.tenant_id, "client_id": found.client_id, "verified_by": directory.name}
    elif warehouse_id is not None:
        if identity is None:
            raise BindingRefused("Link an Azure identity first: the Databricks connection signs in as that identity.")
        principal = _tagged(directory, org, repo, identity["directory_id"])
        if principal is None:
            raise BindingRefused(f"The linked principal isn't tagged for {repo} any more: link an Azure identity again.")
        # the identity as the directory has it now, never as the old binding recorded it
        identity = {"directory_id": principal.id, "tenant_id": directory.tenant_id, "client_id": principal.client_id, "verified_by": directory.name}
        found = next((w for w in directory.warehouses_for(identity["directory_id"]) if w.id == warehouse_id), None)
        if found is None:
            raise BindingRefused(f"This warehouse isn't granted to {identity['directory_id']}.")
        data = _databricks(directory, found)
    else:
        if remove not in REMOVABLE:
            raise BindingRefused("Choose what to remove.")
        if (remove == "azure-identity" and identity is None) or (remove == "databricks" and data is None):
            raise BindingRefused("That is not linked: there is nothing to remove.")
        if remove == "databricks" and _tagged(directory, org, repo, identity["directory_id"]) is None:
            raise BindingRefused(f"The linked principal isn't tagged for {repo} any more. Remove the Azure identity instead: "
                                 "that removes the Databricks connection too.")
        data = None
        identity = None if remove == "azure-identity" else identity
    doc = {"schema": "binding/v2", "repo": repo, "environment": environment}
    if identity:
        doc["azure_identity"] = identity
    if data:
        doc["databricks"] = data
    doc["secrets"] = sorted(secrets, key=lambda s: s["env"])
    if mcp is not None and not mcp["servers"] and isinstance(mcp_gateway, str) and mcp_gateway:
        mcp["gateway"] = mcp_gateway  # with no server left, the environment's gateway now is the one recorded
    if mcp is not None:
        doc["mcp"] = {"gateway": mcp["gateway"], "servers": sorted(mcp["servers"], key=lambda s: s["server_id"])}
    if skills or had_skills:
        doc["skills"] = sorted(skills, key=lambda s: s["name"])
    doc.update(linked_by=person_id, linked_at=now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), request=request_id)
    problems = validate("binding.v2", doc)
    if problems:
        raise BindingRefused(f"The binding is not valid: {problems[0]}")
    return doc

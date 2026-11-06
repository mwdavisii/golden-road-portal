"""The app page (spec 2026-10-29 app bindings): an app's two connections, picked from the directory, recorded by the writer. The
server checks everything again: the repo comes from APP_ADMIN (the name the portal stores), the current binding from the store,
and the posted directory id goes through next_binding. Nothing in a form is trusted but the one choice and the environment.

Each environment (test, prod) has its own connections (spec 2026-10-29 GitOps deploy repo, G3 and G9): every route takes
`environment` as a query (GET) or form (POST) value, test by default, and any other value is a 400.

Delinea Secret Server secrets (spec 2026-10-29 Delinea secrets, "The portal"; spec 2026-10-30 R7): the steps in routes_secrets.py
search as the person (their Secret Server user, through a GitHub link they verified with OAuth) and ask the platform's account
whether each secret is shared with it. The final POST here is checked again by next_binding, whatever the form posts, and the secret
must be in the folder the steps chose. No page, form or request ever carries a value."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request

from ..binding_record import (ENVIRONMENTS, BindingRefused, CloudflareUnavailable, SecretServerUnavailable, environment_state, next_binding,
                              secret_tokens)
from ..bindings import effects
from ..ids import new_id
from ..lifecycle import decommissioned_words
from ..secretserver import SecretServerError
from ..store.base import DATE_REASONS, Conflict
from .access import ADMIN, APP_ADMIN
from .flash import redirect
from .render import page
from .routes_lifecycle import banner, panel
from .secret_steps import DIGITS, SECRET_SERVER_DOWN

router = APIRouter()
IN_PROGRESS = "A change to this app's connections is in progress. Wait for it to finish."
NOT_REPO_ADMIN = "Only an admin of the repository, as the portal last saw it, can change its connections."
NO_LINK = ("Link your GitHub account (verified) first: the portal finds your Secret Server user through it. An admin-made link "
           "is not enough; link it yourself from your page.")
NO_SECRET_SERVER = "Secret Server is not set up here."
NO_CLOUDFLARE = "Cloudflare is not set up here."
NO_SKILLS_CATALOG = "The skills catalog is not set up here."
# Each choice that isn't the directory's, by the setting it needs and what a POST without that setting is told
NEEDS = {"add_secret": ("secret_server", NO_SECRET_SERVER), "remove_secret": ("secret_server", NO_SECRET_SERVER),
         "add_mcp": ("cloudflare", NO_CLOUDFLARE), "remove_mcp": ("cloudflare", NO_CLOUDFLARE),
         "add_skill": ("skills_catalog", NO_SKILLS_CATALOG), "update_skill": ("skills_catalog", NO_SKILLS_CATALOG),
         "remove_skill": ("skills_catalog", NO_SKILLS_CATALOG)}
CONFIRM_FIRST = "Confirm the removal first: press Remove, then confirm it."
NOTHING_TO_REMOVE = "There is nothing to remove."
WRONG_FOLDER = "Choose the secret from its folder: start again from Add a secret."


def binding_state(store, repo, environment):
    """(current binding or None, the open binding request or None, the latest change if it failed or None, the portal's merged
    registration or None) for one environment. requests_for_repo is newest first."""
    found = store.requests_for_repo(repo)
    current, open_one, failed = environment_state(found, environment)
    registration = next((r for r in found if r.kind == "registration" and r.state == "merged"), None)
    return current, open_one, failed, registration


def app_url(repo, environment):
    owner_name, name = repo.split("/")
    return f"/apps/{quote(owner_name)}/{quote(name)}?environment={environment}"


async def _posted_environment(request: Request) -> str:
    """The form's environment exactly as posted, test when the form has none. FastAPI's Form() treats an empty field as missing,
    and an empty environment is not test: it is refused like any other value. Starlette caches the parsed form."""
    values = (await request.form()).getlist("environment")
    return "test" if not values else values[0] if len(values) == 1 and isinstance(values[0], str) else ""


FORM_ENVIRONMENT = Depends(_posted_environment)


def _bad_environment(request):
    return page(request, "error.html", 400, message="The environment must be test or prod.")


def secret_user(request):
    """The signed-in person's Secret Server user, or None: in the mock, through a GitHub link they verified with OAuth."""
    state = request.app.state
    return state.settings.secret_server.user_for(state.store.links_for(request.state.principal.person.id))


def confirmation(current, repo, what=None, secret=None, mcp_server=None, skill=None, gateway=None) -> dict | None:
    """The confirmation card for a removal the person asked for, or None when there is nothing to remove (spec 2026-10-30 R6)."""
    current, base = current or {}, app_url(repo, "test").split("?", 1)[0]
    if what == "azure-identity" and current.get("azure_identity"):
        also = " It also removes the Databricks connection." if current.get("databricks") else ""
        return {"title": "Remove the Azure identity?", "action": f"{base}/remove", "fields": [("what", "azure-identity")],
                "text": "The app loses its Azure workload identity in this environment." + also}
    if what == "databricks" and current.get("databricks"):
        return {"title": "Remove the Databricks connection?", "action": f"{base}/remove", "fields": [("what", "databricks")],
                "text": "The app loses its Databricks connection in this environment. The Azure identity stays."}
    if secret is not None and any(s["env"] == secret for s in current.get("secrets") or []):
        return {"title": f"Remove the secret {secret}?", "action": f"{base}/secrets/remove", "fields": [("env", secret)],
                "text": f"The app no longer gets {secret} in this environment. The secret itself stays in Secret Server."}
    found = next((s for s in (current.get("mcp") or {}).get("servers") or [] if s["server_id"] == mcp_server), None)
    mcp = current.get("mcp") or {}
    if mcp_server is not None and found is not None and gateway and mcp.get("gateway") != gateway:
        n = len(mcp["servers"])
        return {"title": "Remove every MCP server on the old gateway?", "action": f"{base}/mcp/remove", "fields": [("server_id", found["server_id"])],
                "text": f"The platform moved this environment to a new gateway ({gateway}); this removes all {n} server{'s' if n != 1 else ''} "
                        f"on {mcp['gateway']}. Add them again from the new gateway afterwards."}
    if mcp_server is not None and found is not None:
        return {"title": f"Remove the MCP server {found['name']}?", "action": f"{base}/mcp/remove", "fields": [("server_id", found["server_id"])],
                "text": f"The app loses {found['name']} in this environment: its token is taken off the server's policy when the platform "
                        "reconciles cloudflare-access.yaml."}
    if skill is not None and any(s["name"] == skill for s in current.get("skills") or []):
        return {"title": f"Remove the skill {skill}?", "action": f"{base}/skills/remove", "fields": [("name", skill)],
                "text": f"The app no longer mounts /skills/{skill} in this environment. The skill stays in the catalog."}
    return None


def cmdb_card(store, settings, repo, record, viewer_id) -> dict | None:
    """What the app page shows of the collector's CMDB record (spec 2026-10-31 "What owning the app means"), or None when ServiceNow is
    not set up here or nothing is recorded: the CI's number once a sync succeeded (the ownership notice), the record's review date, whose
    app it is (an intake/v1 record has no owner of record; an intake record that is missing or can't be read names nobody), and whether
    ServiceNow knows no user with the owner's email."""
    found = store.cmdb_record(repo) if settings.servicenow else None
    if found is None:
        return None
    readable = record is not None and record.problem is None  # the collector skips a record it can't use: so does the card
    owner = record.owner if readable and record.schema == "intake/v2" and isinstance(record.owner, str) else None
    person = store.get_person(owner) if owner and owner.startswith("p_") else None
    return {"number": found.number, "review_by": record.review_by.isoformat() if readable and record.review_by else None,
            "mine": owner is not None and owner == viewer_id, "owner": person.display_name if person else owner,
            "unreadable": not readable, "owner_unknown": found.last_error == "cmdb_owner_unknown"}


def app_page(request, environment, status=200, errors=(), confirm=None):
    state, repo = request.app.state, request.state.app_repo
    directory, org = state.settings.directory, state.settings.org
    current, open_one, failed, registration = binding_state(state.store, repo, environment)
    identity = (current or {}).get("azure_identity")
    owner_name, repo_name = repo.split("/")
    shown = open_one.final if open_one else current  # what composed.yaml gets: the pending binding once recorded, else the current
    records = state.store.intake_observed(repo)  # the factory's record (spec 2026-10-31 R7): its open date reasons are the owner's banner
    reviews = [i for i in state.store.rereview_items() if i.repo.lower() == repo.lower() and i.reason in DATE_REASONS]
    record = records[0] if records else None
    lifecycle_state = state.store.lifecycle_state(repo)
    return page(request, "app.html", status, repo=repo, owner_name=owner_name, repo_name=repo_name, registration=registration, directory=directory,
                record=record, reviews=reviews, cmdb=cmdb_card(state.store, state.settings, repo, record, request.state.principal.person.id),
                environment=environment, environments=ENVIRONMENTS, current=current, open_request=open_one, failed=failed,
                can_change=request.state.app_repo_admin,
                principals=directory.principals_for(org, repo) if directory else [],
                warehouses=directory.warehouses_for(identity["directory_id"]) if directory and identity else [],
                gateway=state.settings.mcp_gateways[environment],
                current_servers=sorted(((current or {}).get("mcp") or {}).get("servers") or [], key=lambda s: s["server_id"]),
                current_skills=sorted((current or {}).get("skills") or [], key=lambda s: s["name"]),
                catalog_main=state.settings.skills_catalog.main if state.settings.skills_catalog else None,
                in_catalog={s["name"] for s in (current or {}).get("skills") or []
                            if state.settings.skills_catalog and state.settings.skills_catalog.skill(s["name"], state.settings.skills_catalog.main)},
                gateway_moved=bool(((current or {}).get("mcp") or {}).get("servers"))
                              and current["mcp"]["gateway"] != state.settings.mcp_gateways[environment],
                effects=effects(shown), shown_secrets=sorted((shown or {}).get("secrets") or [], key=lambda s: s["env"]),
                current_secrets=sorted((current or {}).get("secrets") or [], key=lambda s: s["env"]),
                errors=list(errors), confirm=confirm,
                decommissioned=lifecycle_state is not None and lifecycle_state.state == "decommissioned",
                lifecycle_banner_info=banner(state.store, lifecycle_state),
                lifecycle_panel_info=panel(state.store, state.settings, repo, request.state.principal))


@router.get("/apps/{owner}/{repo}")
def show(request: Request, owner: str, repo: str, environment: str = Query("test"), confirm: str | None = Query(None),
         confirm_secret: str | None = Query(None), confirm_mcp: str | None = Query(None), confirm_skill: str | None = Query(None),
         principal=APP_ADMIN):
    if environment not in ENVIRONMENTS:
        return _bad_environment(request)
    if any(x is not None for x in (confirm, confirm_secret, confirm_mcp, confirm_skill)):  # a removal asks first; only its confirming POST removes
        if not request.state.app_repo_admin:
            return app_page(request, environment, 403, [NOT_REPO_ADMIN])
        current, open_one, _, _ = binding_state(request.app.state.store, request.state.app_repo, environment)
        if open_one is not None:
            return app_page(request, environment, 409, [IN_PROGRESS])
        card = confirmation(current, request.state.app_repo, what=confirm, secret=confirm_secret, mcp_server=confirm_mcp,
                            skill=confirm_skill, gateway=request.app.state.settings.mcp_gateways[environment])
        if card is None:
            return app_page(request, environment, 400, [NOTHING_TO_REMOVE])
        return app_page(request, environment, confirm=card)
    return app_page(request, environment)


def person_folders(secret_server, user) -> dict:
    """The folders the person can see in Secret Server now (the steps' Source). Raises SecretServerError."""
    with secret_tokens(secret_server, user) as (client, person_token, _):
        return client.folders(person_token)


def _change(request, principal, environment, flash_key, confirmed=None, folder=None, **choice):
    # the path parameters are not used: request.state.app_repo is the checked, stored owner/name
    state, repo = request.app.state, request.state.app_repo
    kind = next((key for key in choice if key in NEEDS), None)
    needed, missing = (getattr(state.settings, NEEDS[kind][0]), NEEDS[kind][1]) if kind else (state.settings.directory, None)
    # Azure and Databricks need the directory; secrets need only Secret Server, MCP servers only Cloudflare, skills only the catalog.
    # With neither the directory nor what the choice needs, there is nothing to change here.
    if state.settings.directory is None and needed is None:
        return page(request, "error.html", 404, message="Not found.")
    if environment not in ENVIRONMENTS:
        return _bad_environment(request)
    if not request.state.app_repo_admin:  # the writer records a change only for the repo's admin (its recheck); say so now
        return app_page(request, environment, 403, [NOT_REPO_ADMIN])
    lifecycle_state = state.store.lifecycle_state(repo)
    if lifecycle_state is not None and lifecycle_state.state == "decommissioned":  # rule 4: nothing more for this repo (item 15)
        return app_page(request, environment, 409, [decommissioned_words(lifecycle_state)])
    current, open_one, _, _ = binding_state(state.store, repo, environment)
    if open_one is not None:
        return app_page(request, environment, 409, [IN_PROGRESS])
    request_id, now = new_id("r"), state.clock()
    if needed is None:  # the directory is here, but not what this choice needs
        return app_page(request, environment, 400, [missing])
    if "add_secret" in choice:
        choice.update(secret_server=state.settings.secret_server, secret_user=secret_user(request))
    choice["mcp_gateway"] = state.settings.mcp_gateways[environment]  # every choice: a moved gateway is never carried on
    if "add_mcp" in choice:
        choice.update(cloudflare=state.settings.cloudflare, person_email=principal.person.email)
    if "add_skill" in choice or "update_skill" in choice:
        choice.update(skills_catalog=state.settings.skills_catalog, person_groups=tuple(principal.session.groups))
    if confirmed is not None and confirmed != "yes":  # a removal needs its confirming POST (spec 2026-10-30 R6)
        return app_page(request, environment, 400, [CONFIRM_FIRST])
    try:
        final = next_binding(current=current, repo=repo, org=state.settings.org, directory=state.settings.directory,
                             person_id=principal.person.id, request_id=request_id, now=now, environment=environment, **choice)
    except (SecretServerUnavailable, CloudflareUnavailable) as exc:
        return app_page(request, environment, 503, [str(exc)])
    except BindingRefused as exc:
        return app_page(request, environment, 400, [str(exc)])
    if folder is not None:  # the secret must be the one picked in its folder (spec 2026-10-30 R7): every step re-checks the earlier ones
        added = next(s for s in final["secrets"] if s["env"] == choice["add_secret"]["env"])
        try:
            visible = person_folders(state.settings.secret_server, choice["secret_user"])
        except SecretServerError:
            return app_page(request, environment, 503, [SECRET_SERVER_DOWN])
        if added["folder_id"] != folder or folder not in visible:
            return app_page(request, environment, 400, [WRONG_FOLDER])
    try:
        state.store.create_binding(request_id=request_id, person_id=principal.person.id, final=final, now=now, current=current)
    except Conflict as exc:
        if exc.code == "decommissioned":  # decommissioned since the check above
            return app_page(request, environment, 409, [decommissioned_words(state.store.lifecycle_state(repo))])
        if exc.code != "open_request_exists":  # request_exists, unknown_person: a bug, not a race the person can retry
            raise
        return app_page(request, environment, 409, [IN_PROGRESS])
    return redirect(app_url(repo, environment), flash_key)


@router.post("/apps/{owner}/{repo}/azure-identity")
def link_identity(request: Request, owner: str, repo: str, principal_id: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, "connection", principal_id=principal_id)


@router.post("/apps/{owner}/{repo}/databricks")
def link_databricks(request: Request, owner: str, repo: str, warehouse_id: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, "connection", warehouse_id=warehouse_id)


@router.post("/apps/{owner}/{repo}/remove")
def remove(request: Request, owner: str, repo: str, what: str = Form(""), confirmed: str = Form(""), principal=APP_ADMIN,
           environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, "removal", confirmed=confirmed, remove=what)


@router.post("/apps/{owner}/{repo}/secrets")
def add_secret(request: Request, owner: str, repo: str, folder: str = Form(""), secret_id: str = Form(""), field_slug: str = Form(""),
               env: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    # only the folder, the id, the field and the env name are taken from the form; next_binding asks Secret Server for the rest
    sid = int(secret_id) if DIGITS.fullmatch(secret_id) else None
    fid = int(folder) if DIGITS.fullmatch(folder) else 0   # 0 is no folder: never a Secret Server id
    return _change(request, principal, environment, "secret", folder=fid, add_secret={"secret_id": sid, "field_slug": field_slug, "env": env})


@router.post("/apps/{owner}/{repo}/secrets/remove")
def remove_secret(request: Request, owner: str, repo: str, env: str = Form(""), confirmed: str = Form(""), principal=APP_ADMIN,
                  environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, "removal", confirmed=confirmed, remove_secret=env)


@router.get("/admin/directory")
def directory_page(request: Request, principal=ADMIN):
    settings = request.app.state.settings
    return page(request, "admin_directory.html", directory=settings.directory, org=settings.org)

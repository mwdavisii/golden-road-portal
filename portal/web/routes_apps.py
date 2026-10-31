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

from ..binding_record import ENVIRONMENTS, BindingRefused, SecretServerUnavailable, environment_state, next_binding, secret_tokens
from ..bindings import effects
from ..ids import new_id
from ..secretserver import SecretServerError
from ..store.base import DATE_REASONS, Conflict
from .access import ADMIN, APP_ADMIN
from .flash import redirect
from .render import page
from .secret_steps import DIGITS, SECRET_SERVER_DOWN

router = APIRouter()
IN_PROGRESS = "A change to this app's connections is in progress. Wait for it to finish."
NOT_REPO_ADMIN = "Only an admin of the repository, as the portal last saw it, can change its connections."
NO_LINK = ("Link your GitHub account (verified) first: the portal finds your Secret Server user through it. An admin-made link "
           "is not enough; link it yourself from your page.")
NO_SECRET_SERVER = "Secret Server is not set up here."
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


def confirmation(current, repo, what=None, secret=None) -> dict | None:
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
    return None


def app_page(request, environment, status=200, errors=(), confirm=None):
    state, repo = request.app.state, request.state.app_repo
    directory, org = state.settings.directory, state.settings.org
    current, open_one, failed, registration = binding_state(state.store, repo, environment)
    identity = (current or {}).get("azure_identity")
    owner_name, repo_name = repo.split("/")
    shown = open_one.final if open_one else current  # what composed.yaml gets: the pending binding once recorded, else the current
    records = state.store.intake_observed(repo)  # the factory's record (spec 2026-10-31 R7): its open date reasons are the owner's banner
    reviews = [i for i in state.store.rereview_items() if i.repo.lower() == repo.lower() and i.reason in DATE_REASONS]
    return page(request, "app.html", status, repo=repo, owner_name=owner_name, repo_name=repo_name, registration=registration, directory=directory,
                record=records[0] if records else None, reviews=reviews,
                environment=environment, environments=ENVIRONMENTS, current=current, open_request=open_one, failed=failed,
                can_change=request.state.app_repo_admin,
                principals=directory.principals_for(org, repo) if directory else [],
                warehouses=directory.warehouses_for(identity["directory_id"]) if directory and identity else [],
                effects=effects(shown), shown_secrets=sorted((shown or {}).get("secrets") or [], key=lambda s: s["env"]),
                current_secrets=sorted((current or {}).get("secrets") or [], key=lambda s: s["env"]),
                errors=list(errors), confirm=confirm)


@router.get("/apps/{owner}/{repo}")
def show(request: Request, owner: str, repo: str, environment: str = Query("test"), confirm: str | None = Query(None),
         confirm_secret: str | None = Query(None), principal=APP_ADMIN):
    if environment not in ENVIRONMENTS:
        return _bad_environment(request)
    if confirm is not None or confirm_secret is not None:  # a removal asks first; only its confirming POST removes
        if not request.state.app_repo_admin:
            return app_page(request, environment, 403, [NOT_REPO_ADMIN])
        current, open_one, _, _ = binding_state(request.app.state.store, request.state.app_repo, environment)
        if open_one is not None:
            return app_page(request, environment, 409, [IN_PROGRESS])
        card = confirmation(current, request.state.app_repo, what=confirm, secret=confirm_secret)
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
    secret_choice = "add_secret" in choice or "remove_secret" in choice
    # Azure and Databricks need the directory; secrets need only Secret Server. With neither, there is nothing to change here.
    if state.settings.directory is None and not (secret_choice and state.settings.secret_server is not None):
        return page(request, "error.html", 404, message="Not found.")
    if environment not in ENVIRONMENTS:
        return _bad_environment(request)
    if not request.state.app_repo_admin:  # the writer records a change only for the repo's admin (its recheck); say so now
        return app_page(request, environment, 403, [NOT_REPO_ADMIN])
    current, open_one, _, _ = binding_state(state.store, repo, environment)
    if open_one is not None:
        return app_page(request, environment, 409, [IN_PROGRESS])
    request_id, now = new_id("r"), state.clock()
    if secret_choice and state.settings.secret_server is None:
        return app_page(request, environment, 400, [NO_SECRET_SERVER])
    if "add_secret" in choice:
        choice.update(secret_server=state.settings.secret_server, secret_user=secret_user(request))
    if confirmed is not None and confirmed != "yes":  # a removal needs its confirming POST (spec 2026-10-30 R6)
        return app_page(request, environment, 400, [CONFIRM_FIRST])
    try:
        final = next_binding(current=current, repo=repo, org=state.settings.org, directory=state.settings.directory,
                             person_id=principal.person.id, request_id=request_id, now=now, environment=environment, **choice)
    except SecretServerUnavailable as exc:
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

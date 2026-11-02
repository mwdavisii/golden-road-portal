"""The MCP server picker's routes (spec 2026-10-31 Part 1, "The portal"; the steps are in mcp_steps.py): the steps' one GET, the final
POST that creates the binding request, and the confirmed removal. Both POSTs go through routes_apps._change, so next_binding checks the
server in Cloudflare again whatever the form posts. No page, form or request ever carries a token."""

from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request

from ..binding_record import ENVIRONMENTS
from ..ids import new_id
from .access import APP_ADMIN
from .mcp_steps import STEPS, Choices, StepRefused, parse, view
from .render import page
from .routes_apps import FORM_ENVIRONMENT, IN_PROGRESS, NOT_REPO_ADMIN, _bad_environment, _change, app_page, binding_state

router = APIRouter()
NOTHING = {"step": 1, "environments": []}


def steps_page(request, environment, tab, shown, choices, errors=(), status=200, note=None):
    owner_name, repo_name = request.state.app_repo.split("/")
    base = f"/apps/{quote(owner_name)}/{quote(repo_name)}"
    return page(request, "mcp_new.html", status, repo=request.state.app_repo, base=base,
                app_href=f"{base}?environment={environment or tab}", environment=environment, tab=environment or tab, shown=shown,
                choices=choices, errors=list(errors), note=note, STEPS=STEPS)


@router.get("/apps/{owner}/{repo}/mcp/new")
def new_server(request: Request, owner: str, repo: str, tab: str = Query("test"), principal=APP_ADMIN):
    # the path parameters are not used: request.state.app_repo is the checked, stored owner/name
    state = request.app.state
    if state.settings.cloudflare is None:  # as the POST routes do: the feature that isn't there is a 404 before anything else
        return page(request, "error.html", 404, message="Not found.")
    environment = request.query_params.get("environment")  # step 1's choice; none yet is step 1, with the tab's pre-chosen
    if (environment is not None and environment not in ENVIRONMENTS) or tab not in ENVIRONMENTS:
        return _bad_environment(request)
    if not request.state.app_repo_admin:
        return app_page(request, environment or tab, 403, [NOT_REPO_ADMIN])
    binding = None
    if environment is not None:
        current, open_one, _, _ = binding_state(state.store, request.state.app_repo, environment)
        if open_one is not None:
            return app_page(request, environment, 409, [IN_PROGRESS])
        binding = {"current": current, "repo": request.state.app_repo, "org": state.settings.org, "directory": state.settings.directory,
                   "person_id": principal.person.id, "request_id": new_id("r"), "now": state.clock(), "environment": environment}
    cloudflare, gateways, email = state.settings.cloudflare, state.settings.mcp_gateways, principal.person.email
    errors, status = [], 200
    try:
        choices = parse(request.query_params)
    except StepRefused as exc:
        choices, errors, status = Choices(environment).up_to(exc.step), [exc.message], exc.status
    try:
        shown = view(cloudflare, gateways, email, choices, binding=binding)
    except StepRefused as exc:
        if exc.status == 503:  # Cloudflare didn't answer: say so, and offer nothing to pick
            return steps_page(request, environment, tab, NOTHING, Choices(), status=503, note=exc.message)
        errors, status, step = [exc.message], exc.status, exc.step
        while True:  # show the refused step again; if that one can't be shown either, the one before it
            try:
                shown = view(cloudflare, gateways, email, choices.up_to(step), binding=binding)
                break
            except StepRefused as again:
                if again.status == 503 or step <= 1:
                    return steps_page(request, environment, tab, NOTHING, Choices(), status=again.status, note=again.message)
                step -= 1
    return steps_page(request, shown_environment(shown, choices), environment or tab, shown, choices, errors, status)


def shown_environment(shown, choices):
    """The environment the shown step is in: none at step 1, where it is still to be chosen."""
    return choices.environment if shown["step"] > 1 else None


@router.post("/apps/{owner}/{repo}/mcp")
def add_server(request: Request, owner: str, repo: str, server_id: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    # only the server's id and the environment are taken from the form; next_binding asks Cloudflare for the rest
    return _change(request, principal, environment, "mcp_server", add_mcp=server_id)


@router.post("/apps/{owner}/{repo}/mcp/remove")
def remove_server(request: Request, owner: str, repo: str, server_id: str = Form(""), confirmed: str = Form(""), principal=APP_ADMIN,
                  environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, "removal", confirmed=confirmed, remove_mcp=server_id)

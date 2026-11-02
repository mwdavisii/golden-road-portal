"""The skills picker's routes (spec 2026-10-31 Part 2, "The portal"; the steps are in skill_steps.py): the steps' one GET, the final POST
that creates the binding request, Update (a new request that re-pins a bound skill to the catalog's current main commit) and the
confirmed removal. Every POST goes through routes_apps._change, so next_binding checks the skill in the catalog again, against the
environment and the person's sign-in groups, whatever the form posts."""

from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request

from ..binding_record import ENVIRONMENTS
from ..ids import new_id
from .access import APP_ADMIN
from .render import page
from .routes_apps import FORM_ENVIRONMENT, IN_PROGRESS, NOT_REPO_ADMIN, _bad_environment, _change, app_page, binding_state
from .skill_steps import STEPS, Choices, StepRefused, parse, view

router = APIRouter()


def steps_page(request, environment, tab, shown, choices, errors=(), status=200):
    owner_name, repo_name = request.state.app_repo.split("/")
    base = f"/apps/{quote(owner_name)}/{quote(repo_name)}"
    return page(request, "skill_new.html", status, repo=request.state.app_repo, base=base,
                app_href=f"{base}?environment={environment or tab}", environment=environment, tab=environment or tab, shown=shown,
                choices=choices, errors=list(errors), STEPS=STEPS)


@router.get("/apps/{owner}/{repo}/skills/new")
def new_skill(request: Request, owner: str, repo: str, tab: str = Query("test"), principal=APP_ADMIN):
    # the path parameters are not used: request.state.app_repo is the checked, stored owner/name
    state = request.app.state
    catalog = state.settings.skills_catalog
    if catalog is None:  # as the POST routes do: the feature that isn't there is a 404 before anything else
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
                   "person_id": principal.person.id, "request_id": new_id("r"), "now": state.clock(), "environment": environment,
                   "mcp_gateway": state.settings.mcp_gateways[environment]}
    groups = tuple(principal.session.groups)
    errors, status = [], 200
    try:
        choices = parse(request.query_params)
    except StepRefused as exc:
        choices, errors, status = Choices(environment).up_to(exc.step), [exc.message], exc.status
    try:
        shown = view(catalog, groups, choices, binding=binding)
    except StepRefused as exc:  # the catalog is local: a refusal is always the choice's, and the step before it can be shown
        errors, status = [exc.message], exc.status
        shown = view(catalog, groups, choices.up_to(exc.step), binding=binding)
    return steps_page(request, choices.environment if shown["step"] > 1 else None, environment or tab, shown, choices, errors, status)


@router.post("/apps/{owner}/{repo}/skills")
def add_skill(request: Request, owner: str, repo: str, name: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    # only the skill's name and the environment are taken from the form; next_binding pins the catalog's main commit
    return _change(request, principal, environment, "skill", add_skill=name)


@router.post("/apps/{owner}/{repo}/skills/update")
def update_skill(request: Request, owner: str, repo: str, name: str = Form(""), principal=APP_ADMIN, environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, "skill", update_skill=name)


@router.post("/apps/{owner}/{repo}/skills/remove")
def remove_skill(request: Request, owner: str, repo: str, name: str = Form(""), confirmed: str = Form(""), principal=APP_ADMIN,
                 environment: str = FORM_ENVIRONMENT):
    return _change(request, principal, environment, "removal", confirmed=confirmed, remove_skill=name)

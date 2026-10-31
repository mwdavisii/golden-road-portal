"""The secrets picker's one GET route (spec 2026-10-30 R7; the steps are in secret_steps.py). The final POST is routes_apps.add_secret."""

from urllib.parse import quote

from fastapi import APIRouter, Query, Request

from ..binding_record import ENVIRONMENTS
from ..ids import new_id
from .access import APP_ADMIN
from .render import page
from .routes_apps import IN_PROGRESS, NO_LINK, NOT_REPO_ADMIN, _bad_environment, app_page, binding_state, secret_user
from .secret_steps import SHOWN_MAX, STEPS, Choices, StepRefused, parse, view

router = APIRouter()
NOTHING = {"step": 1, "folders": {}}


def steps_page(request, environment, shown, choices, typed, errors=(), status=200, note=None):
    owner_name, repo_name = request.state.app_repo.split("/")
    base = f"/apps/{quote(owner_name)}/{quote(repo_name)}"
    return page(request, "secret_new.html", status, repo=request.state.app_repo, base=base, app_href=f"{base}?environment={environment}",
                environment=environment, shown=shown, choices=choices, typed=typed, errors=list(errors), note=note, STEPS=STEPS,
                shown_max=SHOWN_MAX)


@router.get("/apps/{owner}/{repo}/secrets/new")
def new_secret(request: Request, owner: str, repo: str, environment: str = Query("test"), principal=APP_ADMIN):
    # the path parameters are not used: request.state.app_repo is the checked, stored owner/name
    state = request.app.state
    if state.settings.secret_server is None:  # as the POST routes do: the feature that isn't there is a 404 before anything else
        return page(request, "error.html", 404, message="Not found.")
    if environment not in ENVIRONMENTS:
        return _bad_environment(request)
    if not request.state.app_repo_admin:
        return app_page(request, environment, 403, [NOT_REPO_ADMIN])
    current, open_one, _, _ = binding_state(state.store, request.state.app_repo, environment)
    if open_one is not None:
        return app_page(request, environment, 409, [IN_PROGRESS])
    typed = {key: request.query_params.get(key, "") for key in ("q", "field_slug", "env")}
    user = secret_user(request)
    if user is None:
        return steps_page(request, environment, NOTHING, Choices(), typed, note=NO_LINK)
    binding = {"current": current, "repo": request.state.app_repo, "org": state.settings.org, "directory": state.settings.directory,
               "person_id": principal.person.id, "request_id": new_id("r"), "now": state.clock(), "environment": environment}
    errors, status = [], 200
    try:
        choices = parse(request.query_params)
    except StepRefused as exc:
        choices, errors, status = Choices(), [exc.message], exc.status
    try:
        shown = view(state.settings.secret_server, user, choices, binding=binding)
    except StepRefused as exc:
        if exc.status == 503:  # Secret Server didn't answer: say so, and offer nothing to pick
            return steps_page(request, environment, NOTHING, Choices(), typed, status=503, note=exc.message)
        errors, status, step = [exc.message], exc.status, exc.step
        while True:  # show the refused step again; if that one can't be shown either, the one before it
            try:
                shown = view(state.settings.secret_server, user, choices.up_to(step), binding=binding)
                break
            except StepRefused as again:
                if again.status == 503 or step <= 1:
                    return steps_page(request, environment, NOTHING, Choices(), typed, status=again.status, note=again.message)
                step -= 1
    return steps_page(request, environment, shown, choices, typed, errors, status)

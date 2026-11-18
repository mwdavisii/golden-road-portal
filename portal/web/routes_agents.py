"""Build an agent (spec 2026-11-17 "What the person sees"; A12; decided while planning: item 32). /agents/new lists the person's agent
repos the collector has seen and links the template; /apps/{owner}/{repo}/agent is the Agent form (APP_ADMIN). Saving records an
agent_definition request (approved: the PR's review is the gate) that the writer turns into a PR. From the form only the name, the
model, the instructions and the starters are read. The template's link is built from the settings, never from the request."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request

from .. import agent_definition as ad
from ..ids import new_id
from ..lifecycle import decommissioned_words
from ..store.base import OPEN_STATES, SUPERSEDED, Conflict
from .access import APP_ADMIN, SIGNED_IN
from .flash import redirect
from .ownership import fresh_admin_repos, stale_message
from .render import page

router = APIRouter()
NOT_AGENT = "This repository isn't an agent repo: the collector found no builder-spec.json with app_type agent on its default branch."
NOT_REPO_ADMIN = "Only an admin of the repository, as the portal last saw it, can change its agent."


async def _posted_starters(request: Request) -> list:
    """Every starter field as posted, in order (Form() would keep one). Starlette caches the parsed form."""
    return [v for v in (await request.form()).getlist("starter") if isinstance(v, str)]


FORM_STARTERS = Depends(_posted_starters)


def observed(store, repo):
    """The repo as the collector last saw it as an agent repo (its agent.yaml or its problem), or None for a repo that isn't one."""
    found = store.agent_observed(repo)
    return found[0] if found else None


def requests_of(store, repo) -> tuple:
    """(the newest open agent definition of the repo, the newest open one with a pull request (an older one's while the newest waits
    for the writer: its next pass updates that pull request), its newest one when that failed for a reason of its own, not superseded)."""
    mine = [r for r in store.requests_for_repo(repo) if r.kind == ad.KIND]  # newest first
    open_one = next((r for r in mine if r.state in OPEN_STATES), None)
    with_pr = next((r for r in mine if r.state in OPEN_STATES and r.pr_url), None)
    last = mine[0] if mine else None
    failed = last if last is not None and last.state == "failed" and last.failure_reason != SUPERSEDED else None
    return open_one, with_pr, failed


def card(store, repo, shown) -> dict | None:
    """The app page's Agent card (spec 2026-11-17 "What the person sees"; decided while planning: item 33), or None for an app that
    isn't an agent: the model, the instructions' first lines, and what the runtime gets from the binding the page shows for the tab's
    environment (the open change's record once recorded, else the current one)."""
    found = observed(store, repo)
    if found is None:
        return None
    agent = found.agent or {}
    return {"display_name": agent.get("display_name"), "model": agent.get("model"),
            "preview": ad.preview(agent["instructions"]) if agent else None, "problem": ad.PROBLEM_WORDS.get(found.problem),
            "view": ad.runtime_view(shown)}


@router.get("/agents/new")
def build(request: Request, principal=SIGNED_IN):
    state = request.app.state
    fresh, stale = fresh_admin_repos(state.store, principal.person.id, state.clock(), state.settings.repo_admins_stale_after)
    seen = {o.repo.lower(): o for o in state.store.agent_observed()}
    repos, listed = [], set()
    for r in sorted(fresh, key=lambda r: r.repo.lower()):
        found = seen.get(r.repo.lower())
        if found is not None and r.repo.lower() not in listed:  # a repo is listed once, however many of the person's logins administer it
            listed.add(r.repo.lower())
            owner, name = r.repo.split("/")
            repos.append({"repo": r.repo, "owner": quote(owner), "name": quote(name), "problem": found.problem,
                          "display_name": (found.agent or {}).get("display_name")})
    stale_note = stale_message(stale) if stale and not fresh else None  # as registration says it: nothing is listed on a stale observation
    return page(request, "agents_new.html", repos=repos, stale_note=stale_note, template_url=ad.template_link(state.settings), problems=ad.PROBLEM_WORDS)


def agent_page(request, status=200, errors=(), form=None):
    state, repo = request.app.state, request.state.app_repo
    owner_name, repo_name = repo.split("/")
    found = observed(state.store, repo)
    open_one, with_pr, failed = requests_of(state.store, repo)
    if form is None:  # the open definition's draft, else agent.yaml as the collector read it, else an empty form
        start = (open_one.draft if open_one else None) or (found.agent if found else None) or {}
        form = {"display_name": start.get("display_name", ""), "model": start.get("model", ad.model_ids()[0]),
                "instructions": start.get("instructions", ""), "starters": list(start.get("starters") or [])}
    life = state.store.lifecycle_state(repo)
    return page(request, "agent.html", status, repo=repo, owner_name=quote(owner_name), repo_name=quote(repo_name), observed=found,
                open_request=open_one, open_pr=with_pr, failed=failed, form=form, models=ad.models(), errors=list(errors), problems=ad.PROBLEM_WORDS,
                can_change=request.state.app_repo_admin, decommissioned=life is not None and life.state == "decommissioned",
                template_url=ad.template_link(state.settings), max_name=ad.MAX_NAME, max_instructions=ad.MAX_INSTRUCTIONS,
                max_starters=ad.MAX_STARTERS, max_starter=ad.MAX_STARTER, not_agent=NOT_AGENT, not_repo_admin=NOT_REPO_ADMIN)


@router.get("/apps/{owner}/{repo}/agent")
def show(request: Request, owner: str, repo: str, principal=APP_ADMIN):
    return agent_page(request)


@router.post("/apps/{owner}/{repo}/agent")
def save(request: Request, owner: str, repo: str, display_name: str = Form(""), model: str = Form(""), instructions: str = Form(""),
         principal=APP_ADMIN, starters: list = FORM_STARTERS):
    state, repo = request.app.state, request.state.app_repo  # the stored name, never the URL's case
    form = {"display_name": display_name, "model": model, "instructions": instructions, "starters": starters}
    if not request.state.app_repo_admin:  # the writer rechecks the requester: say so now
        return agent_page(request, 403, [NOT_REPO_ADMIN], form)
    life = state.store.lifecycle_state(repo)
    if life is not None and life.state == "decommissioned":  # rule 4 (spec 2026-11-02): nothing more for this repo
        return agent_page(request, 409, [decommissioned_words(life)], form)
    if observed(state.store, repo) is None:
        return agent_page(request, 409, [NOT_AGENT], form)
    draft, errors = ad.form_draft(display_name, model, instructions, starters)
    if errors:
        return agent_page(request, 400, errors, form)
    try:
        state.store.create_agent_definition(request_id=new_id("r"), person_id=principal.person.id, repo=repo, draft=draft, now=state.clock())
    except Conflict as exc:
        if exc.code == "decommissioned":  # decommissioned since the check above
            return agent_page(request, 409, [decommissioned_words(state.store.lifecycle_state(repo))], form)
        raise
    owner_name, repo_name = repo.split("/")
    return redirect(f"/apps/{quote(owner_name)}/{quote(repo_name)}/agent", "agent_saved")

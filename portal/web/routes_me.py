import datetime as dt
import hmac
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..auth import github_link
from ..store.base import Conflict, LoginState
from .access import SIGNED_IN
from .cookies import LINK_COOKIE, clear_cookie, set_cookie
from .render import page

router = APIRouter()
LINK_TTL = dt.timedelta(minutes=10)


@router.get("/me")
def me(request: Request, principal=SIGNED_IN):
    return page(request, "me.html", links=request.app.state.store.links_for(principal.person.id))


@router.get("/link/github")
def link_start(request: Request, principal=SIGNED_IN):
    state = secrets.token_urlsafe(32)
    request.app.state.store.put_login_state(state, LoginState("github", "", "", "/me", principal.person.id),
                                            expires_at=request.app.state.clock() + LINK_TTL)
    response = RedirectResponse(github_link.authorize_url(request.app.state.settings, state), status_code=302)
    set_cookie(response, LINK_COOKIE, state, LINK_TTL.total_seconds())
    return response


@router.get("/link/github/callback")
def link_callback(request: Request, code: str = "", state: str = "", principal=SIGNED_IN):
    store, now = request.app.state.store, request.app.state.clock()

    def bad():
        return page(request, "error.html", 400, message="Linking didn't work. Start again from your page.")

    cookie = request.cookies.get(LINK_COOKIE, "")
    if not code or not state or not cookie or not hmac.compare_digest(cookie, state):
        return bad()
    found = store.take_login_state(state, kind="github", now=now)
    if found is None or found.person_id != principal.person.id:
        return bad()
    try:
        login, uid = github_link.identify(request.app.state.http, request.app.state.settings, code)
    except github_link.LinkError:
        return bad()
    try:
        store.add_link(person_id=principal.person.id, github_login=login, github_user_id=uid, source="github-oauth",
                       actor_id=principal.person.id, reason="the person authorized the GitHub App as this login", now=now)
    except Conflict:
        return page(request, "error.html", 409, message="That GitHub login is linked to another person; ask an admin.")
    response = RedirectResponse("/me", status_code=303)
    clear_cookie(response, LINK_COOKIE)
    return response

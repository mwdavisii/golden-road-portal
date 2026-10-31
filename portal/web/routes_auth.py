import datetime as dt
import hmac
import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..auth import oidc
from ..roles import roles_for
from ..store.base import LoginState
from .access import PUBLIC, SIGNED_IN
from .cookies import LOGIN_COOKIE, SESSION_COOKIE, clear_cookie, set_cookie
from .render import page

router = APIRouter()
LOGIN_TTL = dt.timedelta(minutes=10)
LOCAL_PATH = re.compile(r"/(?![/\\])[A-Za-z0-9._~!$&'()*+,;=:@/%?-]{0,200}")  # a query too: /register?repo=owner/name


def safe_next(value) -> str:
    """Where to go after sign-in: a local path or nothing. `//host`, `/\\host` and absolute URLs all become `/`."""
    return value if isinstance(value, str) and LOCAL_PATH.fullmatch(value) else "/"


def _provider(request):
    return oidc.discover(request.app.state.http, request.app.state.settings.oidc_issuer)


@router.post("/logout")
def logout(request: Request, principal=SIGNED_IN):
    request.app.state.store.end_session(request.cookies.get(SESSION_COOKIE, ""))
    response = RedirectResponse("/", status_code=303)
    clear_cookie(response, SESSION_COOKIE)
    return response


@router.get("/login")
def login(request: Request, next: str = "/", principal=PUBLIC):
    s, store, now = request.app.state.settings, request.app.state.store, request.app.state.clock()
    try:
        provider = _provider(request)
    except oidc.OIDCError:
        return page(request, "error.html", 502, message="The sign-in provider can't be reached. Try again later.")
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = oidc.pkce_pair()
    store.put_login_state(state, LoginState("oidc", nonce, verifier, safe_next(next), None), expires_at=now + LOGIN_TTL)
    response = RedirectResponse(oidc.authorize_url(provider, s.oidc_client_id, s.base_url + "/auth/callback", s.oidc_scopes, state, nonce, challenge),
                                status_code=302)
    set_cookie(response, LOGIN_COOKIE, state, LOGIN_TTL.total_seconds())
    return response


@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", principal=PUBLIC):
    s, store, now = request.app.state.settings, request.app.state.store, request.app.state.clock()

    def bad():
        return page(request, "error.html", 400, message="This sign-in didn't work. Start again from Sign in.")

    cookie = request.cookies.get(LOGIN_COOKIE, "")
    if not code or not state or not cookie or not hmac.compare_digest(cookie, state):
        return bad()
    login_state = store.take_login_state(state, kind="oidc", now=now)
    if login_state is None:
        return bad()
    try:
        provider = _provider(request)
        token = oidc.exchange(request.app.state.http, provider, s.oidc_client_id, s.oidc_client_secret, code, s.base_url + "/auth/callback",
                              login_state.code_verifier)
        claims = oidc.verify_id_token(request.app.state.http, provider, token, client_id=s.oidc_client_id, nonce=login_state.nonce,
                                      algorithms=s.oidc_algorithms)
    except oidc.OIDCError:
        return bad()
    groups = oidc.groups_from(claims, s.oidc_groups_claim)
    old = request.cookies.get(SESSION_COOKIE)
    if old:
        store.end_session(old)
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    _, session_token, _ = store.sign_in(issuer=s.oidc_issuer, subject=claims["sub"], display_name=oidc.display_name(claims), email=email,
                                        groups=groups, roles=roles_for(groups, s.role_groups), now=now, max_age=s.session_max)
    response = RedirectResponse(login_state.next_path, status_code=303)
    set_cookie(response, SESSION_COOKIE, session_token, s.session_max.total_seconds())
    clear_cookie(response, LOGIN_COOKIE)
    return response

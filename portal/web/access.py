"""Authorization in portal code (P14). Every route declares exactly one Access dependency, and app.check_routes refuses to
serve any route without one. Access reads only the portal's own session cookie. The edge's X-Platform-Roles header (or any
identity header) is never read."""

import enum
import hmac
from dataclasses import dataclass

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from ..store.base import Person, Session
from .cookies import SESSION_COOKIE

SAFE_METHODS = frozenset({"GET", "HEAD"})


class Need(enum.Enum):
    PUBLIC = "anyone"
    SIGNED_IN = "a signed-in person"
    ADMIN = "an admin"
    REQUESTER = "the requester"
    REQUESTER_OR_ADMIN = "the requester or an admin"


class Denied(Exception):
    """303: sign in first (GET); 401: sign in first (other methods); 403: not allowed or a bad CSRF token; 404: not yours."""

    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


@dataclass(frozen=True)
class Principal:
    person: Person | None = None
    session: Session | None = None

    @property
    def signed_in(self) -> bool:
        return self.person is not None

    @property
    def roles(self) -> frozenset:
        return frozenset(self.session.roles) if self.session else frozenset()

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def csrf(self) -> str:
        return self.session.csrf if self.session else ""


def principal_from(request: Request) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        return Principal()
    state = request.app.state
    session = state.store.get_session(token, now=state.clock(), idle=state.settings.session_idle)
    if session is None:
        return Principal()
    person = state.store.get_person(session.person_id)
    return Principal(person, session) if person else Principal()


async def csrf_ok(request: Request, principal: Principal) -> bool:
    origin = request.headers.get("origin")
    if origin is not None and origin != request.app.state.settings.origin:
        return False
    sent = request.headers.get("x-csrf-token")
    if sent is None:
        sent = (await request.form()).get("csrf_token")
    return isinstance(sent, str) and bool(principal.csrf) and hmac.compare_digest(sent, principal.csrf)


class Access:
    def __init__(self, need: Need):
        self.need = need

    async def __call__(self, request: Request) -> Principal:
        principal = await run_in_threadpool(principal_from, request)
        request.state.principal = principal
        if self.need is Need.PUBLIC:
            return principal
        if not principal.signed_in:
            raise Denied(303 if request.method in SAFE_METHODS else 401)
        if self.need is Need.ADMIN and not principal.is_admin:
            raise Denied(403)
        if self.need in (Need.REQUESTER, Need.REQUESTER_OR_ADMIN):
            found = await run_in_threadpool(request.app.state.store.get_request, request.path_params.get("request_id", ""))
            mine = found is not None and found.requested_by == principal.person.id
            if not (mine or (self.need is Need.REQUESTER_OR_ADMIN and found is not None and principal.is_admin)):
                raise Denied(404)
            request.state.found_request = found
        if request.method not in SAFE_METHODS and not await csrf_ok(request, principal):
            raise Denied(403)
        return principal


PUBLIC = Depends(Access(Need.PUBLIC))
SIGNED_IN = Depends(Access(Need.SIGNED_IN))
ADMIN = Depends(Access(Need.ADMIN))
REQUESTER = Depends(Access(Need.REQUESTER))
REQUESTER_OR_ADMIN = Depends(Access(Need.REQUESTER_OR_ADMIN))

"""The web process. create_app takes its collaborators so tests can pass a MemoryStore, a fake transport and a fixed clock.
Without them (the image's _platform.entry imports portal.main:app), startup reads the environment."""

import contextlib
import datetime as dt
import logging
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

from . import routes_admin, routes_apps, routes_auth, routes_me, routes_public, routes_register
from .access import SAFE_METHODS, Access, Denied, Need, Principal
from .headers import SecurityHeaders
from .render import page

STATIC = Path(__file__).resolve().parents[1] / "static"
ROUTERS = [routes_public.router, routes_auth.router, routes_me.router, routes_admin.router, routes_register.router, routes_apps.router]  # later tasks append theirs
DENIED = {401: "Sign in first.", 403: "You don't have access to this.", 404: "Not found."}


class RouteWithoutAccess(RuntimeError):
    pass


def api_routes(routes):
    """Every APIRoute under `routes`, looking inside included routers. FastAPI 0.161 includes a router as one lazy
    `_IncludedRouter` entry in app.routes (holding `original_router`), so the check has to walk into it. Nothing is skipped."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from api_routes(inner.routes)
        else:
            yield route


def _is_portal_static(route) -> bool:
    """The one mount allowed: /static serving this package's static directory, and nothing else."""
    inner = getattr(route, "app", None)
    return (isinstance(route, Mount) and route.path == "/static" and isinstance(inner, StaticFiles)
            and Path(inner.directory).resolve() == STATIC.resolve())


def check_routes(app: FastAPI) -> None:
    """Fail closed: every route declares exactly one Access requirement, a PUBLIC route answers only safe methods (PUBLIC returns
    before the CSRF check), and the only other thing served is the portal's own /static."""
    problems = []
    # The unconfigured app has no sessions, store or forms, so its routes use NoSession for every method; no other app may.
    unconfigured = getattr(app.state, "unconfigured", False) is True
    for route in api_routes(app.routes):
        if isinstance(route, APIRoute):
            found = [d.call for d in route.dependant.dependencies if isinstance(d.call, Access)]
            if len(found) != 1:
                problems.append(f"{','.join(sorted(route.methods))} {route.path}")
            elif isinstance(found[0], NoSession) != unconfigured:
                problems.append(f"{','.join(sorted(route.methods))} {route.path} (NoSession is only for the unconfigured app, and it uses nothing else)")
            elif found[0].need is Need.PUBLIC and route.methods - SAFE_METHODS and not unconfigured:
                problems.append(f"{','.join(sorted(route.methods - SAFE_METHODS))} {route.path} (PUBLIC allows only safe methods)")
        elif not _is_portal_static(route):
            problems.append(f"{type(route).__name__} {getattr(route, 'path', '?')}")
    if problems:
        raise RouteWithoutAccess("routes without exactly one Access requirement: " + "; ".join(problems))


def _utcnow():
    return dt.datetime.now(dt.UTC)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    check_routes(app)
    yield


async def _denied(request, exc: Denied):
    from fastapi.responses import RedirectResponse
    if exc.status == 303:
        return RedirectResponse("/login?next=" + quote(request.url.path, safe="/"), status_code=303)
    return page(request, "error.html", exc.status, message=DENIED[exc.status])


def create_app(settings=None, store=None, http=None, clock=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.state.settings, app.state.store, app.state.http, app.state.clock = settings, store, http, clock or _utcnow
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    for router in ROUTERS:
        app.include_router(router)
    app.add_exception_handler(Denied, _denied)
    app.add_middleware(SecurityHeaders)
    check_routes(app)
    return app


class NoSession(Access):
    """The one requirement of the unconfigured app: anyone, and it never reads a cookie or touches a store, because there is none."""

    def __init__(self):
        super().__init__(Need.PUBLIC)

    async def __call__(self, request: Request) -> Principal:
        return Principal()


UNCONFIGURED_BODY = "Service unavailable: the portal is not configured.\n"
log = logging.getLogger("portal")


def create_unconfigured_app(names) -> FastAPI:
    """What runs when a required setting is missing or invalid (the platform's preview starts the image with no environment).
    The health path answers 200 naming the settings (their names, never values); everything else is a fixed 503. No sign-in, no
    store, no database, no forms, no state change. `names` are the portal's own setting names from settings.py."""
    listed = ", ".join(sorted(names))
    body = f"portal is not configured: missing or invalid settings: {listed}\n"
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = app.state.store = app.state.http = None
    app.state.unconfigured = True
    app.state.clock = _utcnow
    gate = Depends(NoSession())

    @app.get("/healthz")
    def healthz(principal=gate):
        return PlainTextResponse(body)

    @app.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    def everything_else(rest: str, principal=gate):
        return PlainTextResponse(UNCONFIGURED_BODY, status_code=503)

    async def refuse(request, exc):  # a method the routes above don't list (PROPFIND) or another method on /healthz
        return PlainTextResponse(UNCONFIGURED_BODY, status_code=503)

    app.add_exception_handler(404, refuse)
    app.add_exception_handler(405, refuse)
    app.add_middleware(SecurityHeaders)
    check_routes(app)
    log.warning("portal not configured: serving only %s; missing or invalid settings: %s", "/healthz", listed)
    return app


def create_app_from_env(env) -> FastAPI:
    """The image's entry: the configured app when every setting is present and valid, else the unconfigured one. A partial
    configuration is unconfigured. The database is only touched in the configured case."""
    from ..settings import Settings
    settings, problems = Settings.check(env)
    if problems:
        return create_unconfigured_app(problems)
    from ..store.postgres import PostgresStore
    return create_app(settings=settings, store=PostgresStore(settings.database_url), http=httpx.Client(timeout=10))

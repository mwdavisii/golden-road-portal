"""The web process. create_app takes its collaborators so tests can pass a MemoryStore, a fake transport and a fixed clock.
Without them (the image's _platform.entry imports portal.main:app), startup reads the environment."""

import contextlib
import datetime as dt
import os
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

from . import routes_admin, routes_auth, routes_me, routes_public, routes_register
from .access import SAFE_METHODS, Access, Denied, Need
from .headers import SecurityHeaders
from .render import page

STATIC = Path(__file__).resolve().parents[1] / "static"
ROUTERS = [routes_public.router, routes_auth.router, routes_me.router, routes_admin.router, routes_register.router]  # later tasks append theirs
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
    for route in api_routes(app.routes):
        if isinstance(route, APIRoute):
            found = [d.call for d in route.dependant.dependencies if isinstance(d.call, Access)]
            if len(found) != 1:
                problems.append(f"{','.join(sorted(route.methods))} {route.path}")
            elif found[0].need is Need.PUBLIC and route.methods - SAFE_METHODS:
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
    if app.state.settings is None:
        from ..settings import Settings
        from ..store.postgres import PostgresStore
        app.state.settings = Settings.from_env(os.environ)
        app.state.store = PostgresStore(app.state.settings.database_url)
        app.state.http = httpx.Client(timeout=10)
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

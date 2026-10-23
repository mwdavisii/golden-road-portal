import pytest
from fastapi import APIRouter, Depends, FastAPI

from portal.web.access import PUBLIC, SIGNED_IN
from portal.web.app import RouteWithoutAccess, check_routes


def test_healthz_answers_without_a_session(portal):
    response = portal.client().get("/healthz")
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_a_route_without_an_access_requirement_stops_the_app(portal):
    @portal.app.get("/oops")
    def oops():
        return {}
    with pytest.raises(RouteWithoutAccess, match="GET /oops"):
        check_routes(portal.app)


def test_a_route_with_two_requirements_is_refused_too():
    app = FastAPI()

    @app.get("/twice")
    def twice(a=PUBLIC, b=SIGNED_IN):
        return {}
    with pytest.raises(RouteWithoutAccess):
        check_routes(app)


def test_the_startup_check_also_runs_when_the_server_starts(portal):
    from fastapi.testclient import TestClient

    @portal.app.post("/late")
    def late():
        return {}
    with pytest.raises(RouteWithoutAccess):
        with TestClient(portal.app):
            pass


def test_no_docs_or_schema_routes_are_served(portal):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert portal.client("admin").get(path).status_code == 404


import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

PORTAL_DIR = Path(__file__).resolve().parents[1] / "portal"


def test_every_response_carries_the_security_headers(portal):
    for path in ("/", "/healthz", "/nope"):
        headers = portal.client().get(path).headers
        csp = headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert headers["x-content-type-options"] == "nosniff" and headers["referrer-policy"] == "no-referrer"
        assert headers["cache-control"] == "no-store"
    static = portal.client().get("/static/portal.css").headers
    assert "content-security-policy" in static and static.get("cache-control") != "no-store"  # static files may be cached


def test_start_here_is_public_and_has_the_paste_block_and_the_releases_link(portal):
    response = portal.client().get("/")
    text = response.text
    assert response.status_code == 200
    assert "repos/acme/golden-road-agents/contents/INSTALL_FOR_AGENTS.md" in text and "gh api --hostname github.com" in text
    assert 'href="https://github.com/acme/golden-road-agents/releases"' in text and "Install <code>uv</code>" in text
    assert "<script>" not in text and 'onclick' not in text  # no inline script: the CSP forbids it


def test_the_vendored_htmx_is_the_recorded_one():
    record = json.loads((PORTAL_DIR / "static/VENDORED.json").read_text())["htmx.min.js"]
    assert hashlib.sha256((PORTAL_DIR / "static/htmx.min.js").read_bytes()).hexdigest() == record["sha256"]
    assert re.fullmatch(r"2\.\d+\.\d+", record["version"])


def test_the_vendored_htmx_differs_from_upstream_only_by_the_recorded_patches():
    """The scanner refuses dynamic code evaluation anywhere in the tree, so two call sites (unreachable with allowEval false) are
    patched out. Undoing the recorded patches must give back the upstream file."""
    record = json.loads((PORTAL_DIR / "static/VENDORED.json").read_text())["htmx.min.js"]
    text = (PORTAL_DIR / "static/htmx.min.js").read_text(encoding="utf-8")
    for patch in record["patches"]:
        assert text.count(patch["to"]) == 1
        text = text.replace(patch["to"], patch["from"])
    assert hashlib.sha256(text.encode()).hexdigest() == record["upstream_sha256"]


def test_templates_never_turn_off_escaping():
    for path in (PORTAL_DIR / "templates").rglob("*"):
        text = path.read_text()
        for marker in ("|safe", "| safe", "autoescape false", "Markup(", "{% raw"):
            assert marker not in text, (path.name, marker)


def test_htmx_is_configured_for_the_csp(portal):
    text = portal.client().get("/").text
    assert '"allowEval": false' in text and '"includeIndicatorStyles": false' in text and '"selfRequestsOnly": true' in text


def test_a_public_route_may_only_answer_safe_methods():
    """PUBLIC returns before the CSRF check, so a PUBLIC POST would be a state change with no token."""
    app = FastAPI()

    @app.post("/open")
    def open_post(principal=PUBLIC):
        return {}
    with pytest.raises(RouteWithoutAccess, match="POST /open"):
        check_routes(app)

    @app.get("/fine")
    def fine(principal=PUBLIC):
        return {}


def test_create_app_refuses_a_public_post_route(portal, monkeypatch):
    from portal.web import app as app_module
    router = APIRouter()

    @router.post("/open")
    def open_post(principal=PUBLIC):
        return {}
    monkeypatch.setattr(app_module, "ROUTERS", [*app_module.ROUTERS, router])
    with pytest.raises(RouteWithoutAccess, match="POST /open"):
        app_module.create_app(settings=portal.settings, store=portal.store, http=None, clock=portal.clock)


def test_the_static_mount_must_really_be_the_portal_s_static_files(tmp_path):
    from fastapi.staticfiles import StaticFiles

    async def sneaky(scope, receive, send):  # something else mounted at /static
        pass
    elsewhere = FastAPI()
    elsewhere.mount("/static", sneaky)
    with pytest.raises(RouteWithoutAccess, match="Mount"):
        check_routes(elsewhere)
    other_dir = FastAPI()
    other_dir.mount("/static", StaticFiles(directory=tmp_path))
    with pytest.raises(RouteWithoutAccess, match="Mount"):
        check_routes(other_dir)


def test_the_vendored_htmx_patch_reason_names_what_remains():
    reason = json.loads((PORTAL_DIR / "static/VENDORED.json").read_text())["htmx.min.js"]["patch_reason"]
    assert "remain" in reason and "allowEval" in reason and "unsafe-eval" in reason
    text = (PORTAL_DIR / "static/htmx.min.js").read_text()
    assert text.count("Function(") == 2, "the reason says exactly two Function calls remain"
    for template in (PORTAL_DIR / "templates").rglob("*"):
        assert not any(a in template.read_text() for a in ("hx-vals", "hx-vars", "hx-on", "hx-trigger")), template.name

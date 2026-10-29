"""Unconfigured mode: the platform's preview runs the image with no environment and the app must still answer its health path
(builder-spec.json) and the authz probe, while serving nothing real. The pod reports healthy but serves only 503; that trade-off
is in the README."""

import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from test_portal_settings import ENV

from portal.web.access import Access
from portal.web.app import api_routes, check_routes, create_app_from_env

SECRET = "hunter2-do-not-print"
NOT_CONFIGURED = b"portal is not configured"


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """Nothing in the unconfigured tests may open a database connection."""
    import portal.store.postgres as pg

    def connect(*args, **kwargs):
        raise AssertionError("unconfigured mode must not connect to a database")
    monkeypatch.setattr(pg, "PostgresStore", connect)


def client(env):
    return TestClient(create_app_from_env(env), follow_redirects=False)


def test_with_no_environment_the_health_path_answers_200_and_names_every_missing_setting():
    response = client({}).get("/healthz")
    assert response.status_code == 200 and NOT_CONFIGURED in response.content
    for name in ENV:
        assert name in response.text


def test_partial_configuration_is_unconfigured_and_lists_only_what_is_wrong():
    env = {**ENV, "PORTAL_ORG": "bad org", "PORTAL_DATABASE_URL": ""}
    del env["PORTAL_OIDC_CLIENT_ID"]
    body = client(env).get("/healthz").text
    assert "PORTAL_ORG" in body and "PORTAL_DATABASE_URL" in body and "PORTAL_OIDC_CLIENT_ID" in body
    assert "PORTAL_BASE_URL" not in body and "bad org" not in body


def test_values_are_never_echoed():
    env = {**ENV, "PORTAL_OIDC_CLIENT_SECRET": SECRET, "PORTAL_ROLE_GROUPS": SECRET, "PORTAL_BASE_URL": f"http://{SECRET}.test"}
    body = client(env).get("/healthz").text
    assert SECRET not in body and "PORTAL_ROLE_GROUPS" in body and "PORTAL_BASE_URL" in body


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "PROPFIND"]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("path", ["/", "/login", "/me", "/register", "/requests/1", "/admin/people", "/auth/callback",
                                  "/static/htmx.min.js", "/docs", "/openapi.json", "/nope/deeper/still", "/healthz/x"])
def test_everything_else_is_503_with_one_fixed_body(method, path):
    response = client({}).request(method, path, headers={"Origin": "https://evil.test"}, cookies={"portal_session": "abc"})
    assert response.status_code == 503
    if method != "HEAD":
        assert response.text == "Service unavailable: the portal is not configured.\n"
    assert "set-cookie" not in response.headers and "location" not in response.headers


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_the_health_path_with_other_methods_is_503_too(method):
    assert client({}).request(method, "/healthz").status_code == 503


def test_a_form_post_is_not_parsed_or_acted_on():
    assert client({}).post("/register", data={"csrf_token": "x", "repo": "acme/notes"}).status_code == 503


def test_security_headers_still_apply():
    headers = client({}).get("/anything").headers
    assert "frame-ancestors 'none'" in headers["content-security-policy"] and headers["x-content-type-options"] == "nosniff"


def test_no_store_settings_or_http_client_exist_even_after_startup():
    app = create_app_from_env({})
    assert app.state.store is None and app.state.settings is None and app.state.http is None
    with TestClient(app) as c:  # the lifespan runs: it must not build a store or read settings either
        assert c.get("/healthz").status_code == 200
    assert app.state.store is None and app.state.settings is None


def test_routes_pass_the_route_check_and_each_declares_one_requirement():
    app = create_app_from_env({})
    check_routes(app)
    routes = [r for r in api_routes(app.routes) if isinstance(r, APIRoute)]
    assert routes
    for route in routes:
        assert len([d for d in route.dependant.dependencies if isinstance(d.call, Access)]) == 1


def test_the_route_check_still_refuses_an_undeclared_route_on_the_unconfigured_app():
    from portal.web.app import RouteWithoutAccess
    app = create_app_from_env({})

    @app.get("/oops")
    def oops():
        return {}
    with pytest.raises(RouteWithoutAccess):
        check_routes(app)


def test_there_is_no_sign_in_static_mount_or_docs_route():
    assert {getattr(r, "path", None) for r in api_routes(create_app_from_env({}).routes)} == {"/healthz", "/{rest:path}"}


def test_startup_logs_one_line_with_names_only(caplog):
    env = {**ENV, "PORTAL_OIDC_CLIENT_SECRET": SECRET}
    del env["PORTAL_ORG"]
    with caplog.at_level(logging.INFO):
        create_app_from_env(env)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "PORTAL_ORG" in lines[0] and "not configured" in lines[0]
    assert SECRET not in lines[0] and "acme" not in lines[0] and "\n" not in lines[0]


def test_a_complete_environment_builds_the_configured_app(monkeypatch):
    built = []

    class FakeStore:
        def __init__(self, dsn):
            built.append(dsn)

    import portal.store.postgres as pg
    monkeypatch.setattr(pg, "PostgresStore", FakeStore)
    app = create_app_from_env(ENV)
    assert built == [ENV["PORTAL_DATABASE_URL"]] and app.state.settings.org == "acme"
    response = TestClient(app, base_url="https://portal.example.test").get("/healthz")
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_settings_check_names_every_problem_without_values():
    from portal.settings import Settings
    settings, problems = Settings.check({**ENV, "PORTAL_ORG": "bad org", "PORTAL_REVIEW_MONTHS": "99"})
    assert settings is None and set(problems) == {"PORTAL_ORG", "PORTAL_REVIEW_MONTHS"}
    assert Settings.check(ENV) == (Settings.from_env(ENV), {})


def test_no_session_is_refused_on_a_configured_app():
    """NoSession skips the safe-methods rule only because the unconfigured app has no sessions to forge: a configured app that
    used it would have an unprotected state-changing route."""
    from fastapi import Depends, FastAPI

    from portal.web.app import NoSession, RouteWithoutAccess
    app = FastAPI()

    @app.post("/x")
    def x(principal=Depends(NoSession())):
        return {}
    with pytest.raises(RouteWithoutAccess, match="NoSession"):
        check_routes(app)


def test_the_unconfigured_app_uses_only_no_session():
    from portal.web.access import PUBLIC
    from portal.web.app import RouteWithoutAccess
    app = create_app_from_env({})

    @app.get("/public-in-unconfigured")
    def other(principal=PUBLIC):
        return {}
    with pytest.raises(RouteWithoutAccess):
        check_routes(app)


def test_a_bad_mock_directory_leaves_the_portal_unconfigured(tmp_path, monkeypatch):
    import portal.directory
    (tmp_path / "mock.json").write_text("{}")
    monkeypatch.setattr(portal.directory, "MOCK", tmp_path / "mock.json")
    response = client({**ENV, "PORTAL_DIRECTORY": "mock"}).get("/healthz")
    assert response.status_code == 200 and NOT_CONFIGURED in response.content and "PORTAL_DIRECTORY" in response.text

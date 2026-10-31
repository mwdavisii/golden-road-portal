"""Every route x every kind of caller (spec Testing, P14). It stands in for the builder's authz probe, which can't see the
portal's roles: the generated layer allows one role everywhere. A route missing from ROUTES fails the first test."""

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from portal_fakes import PRINCIPALS

from portal.web.app import api_routes

A, O, X, P, D = PRINCIPALS  # anonymous, owner (contributor who owns the request), other contributor, ops, admin


def same(status):
    return dict.fromkeys(PRINCIPALS, status)


def signed_in(get=200, anonymous=303):
    return {A: anonymous, O: get, X: get, P: get, D: get}


def admin_only(ok=200, anonymous=303):
    return {A: anonymous, O: 403, X: 403, P: 403, D: ok}


ROUTES = {
    ("GET", "/healthz"): same(200),
    ("GET", "/"): same(200),
    ("POST", "/logout"): signed_in(303, anonymous=401),
    ("GET", "/login"): same(302),
    ("GET", "/auth/callback"): same(400),
    ("GET", "/me"): signed_in(200),
    ("GET", "/link/github"): signed_in(302),
    ("GET", "/link/github/callback"): signed_in(400),
    ("GET", "/register"): same(200),
    ("POST", "/register"): signed_in(400, anonymous=401),
    ("GET", "/requests"): signed_in(200),
    ("GET", "/requests/{request_id}"): {A: 303, O: 200, X: 404, P: 404, D: 200},
    ("POST", "/requests/{request_id}"): {A: 401, O: 409, X: 404, P: 404, D: 404},
    ("GET", "/admin/people"): admin_only(),
    ("POST", "/admin/people/{person_id}/links"): admin_only(400, anonymous=401),
    ("POST", "/admin/links/{link_id}/remove"): admin_only(400, anonymous=401),
    ("GET", "/admin/approvals"): admin_only(),
    ("GET", "/admin/rereview"): admin_only(),
    ("POST", "/admin/rereview/{item_id}/close"): admin_only(400, anonymous=401),   # no note: 400 before the item is looked up
    ("POST", "/admin/requests/{request_id}/approve"): admin_only(400, anonymous=401),
    ("POST", "/admin/requests/{request_id}/send-back"): admin_only(400, anonymous=401),
    ("GET", "/admin/audit"): admin_only(),
    ("GET", "/admin/identity-links.json"): admin_only(),
    ("GET", "/admin/directory"): admin_only(),
    # the app page: the repo's admin (owner) and portal admins see it; only the repo's admin changes it (an empty choice is a 400)
    ("GET", "/apps/{owner}/{repo}"): {A: 303, O: 200, X: 404, P: 404, D: 200},
    ("GET", "/apps/{owner}/{repo}/secrets/new"): {A: 303, O: 404, X: 404, P: 404, D: 404},   # 404 for everyone signed in: no Secret Server here
    ("POST", "/apps/{owner}/{repo}/azure-identity"): {A: 401, O: 400, X: 404, P: 404, D: 403},
    ("POST", "/apps/{owner}/{repo}/databricks"): {A: 401, O: 400, X: 404, P: 404, D: 403},
    ("POST", "/apps/{owner}/{repo}/remove"): {A: 401, O: 400, X: 404, P: 404, D: 403},
    ("POST", "/apps/{owner}/{repo}/secrets"): {A: 401, O: 400, X: 404, P: 404, D: 403},
    ("POST", "/apps/{owner}/{repo}/secrets/remove"): {A: 401, O: 400, X: 404, P: 404, D: 403},
}


def fill(path, seeds):
    return path.format(request_id=seeds["request"], person_id=seeds["owner"], link_id=seeds["link"], owner="acme", repo="notes",
                       item_id="i_" + "0" * 32)


def call(portal, who, method, path, headers=None):
    client = portal.client(who)
    data = {"csrf_token": portal.csrfs[who]} if who in portal.csrfs else {}
    url = fill(path, portal.seeds)
    return client.get(url, headers=headers) if method == "GET" else client.request(method, url, data=data, headers=headers)


def test_every_route_is_in_the_matrix(portal):
    declared = {(m, r.path) for r in api_routes(portal.app.routes) if isinstance(r, APIRoute) for m in r.methods - {"HEAD"}}
    assert declared == set(ROUTES)


@pytest.mark.parametrize("who", PRINCIPALS)
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_each_caller_gets_what_the_matrix_says(portal, route, who):
    assert call(portal, who, *route).status_code == ROUTES[route][who]


POLICY = Path(__file__).resolve().parents[1] / "_platform/authz-policy.json"
IDENTITY_HEADER = json.loads(POLICY.read_text())["identity_header"] if POLICY.exists() else "X-Platform-Roles"


@pytest.mark.parametrize("who", PRINCIPALS)
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_forged_roles_header_changes_nothing(portal, route, who):
    forged = {"X-Platform-Roles": "admin,ops,portal", IDENTITY_HEADER: "admin,ops,portal"}
    assert call(portal, who, *route, headers=forged).status_code == ROUTES[route][who]



# the app page's environment selector: ?environment=prod (GET) or environment=prod (POST) gets what the plain route gets, and a
# bad environment is a 400 for whoever passes the route's Access requirement (that requirement is checked first)
APP_ROUTES = sorted(r for r in ROUTES if r[1].startswith("/apps/"))


@pytest.mark.parametrize("who", PRINCIPALS)
@pytest.mark.parametrize("route", APP_ROUTES)
def test_each_caller_gets_the_same_for_prod(portal, route, who):
    method, path = route
    client, url = portal.client(who), fill(path, portal.seeds)
    data = {"csrf_token": portal.csrfs[who]} if who in portal.csrfs else {}
    response = client.get(url, params={"environment": "prod"}) if method == "GET" else client.post(url, data={**data, "environment": "prod"})
    assert response.status_code == ROUTES[route][who]


@pytest.mark.parametrize("who", PRINCIPALS)
@pytest.mark.parametrize("route", APP_ROUTES)
def test_a_bad_environment_is_a_400_once_access_is_granted(portal, route, who):
    method, path = route
    client, url = portal.client(who), fill(path, portal.seeds)
    data = {"csrf_token": portal.csrfs[who]} if who in portal.csrfs else {}
    response = client.get(url, params={"environment": "staging"}) if method == "GET" else client.post(url, data={**data, "environment": "staging"})
    want = ROUTES[route][who]
    assert response.status_code == (400 if want in (200, 400, 403) else want)

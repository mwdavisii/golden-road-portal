"""Need.APP_ADMIN (spec 2026-10-29 app bindings, "App page"): the repo's admin as the collector last saw it, or a portal admin.
Anyone else gets 404, like other pages. The checked repo is the name the portal stores, not the URL's case."""

from fastapi import APIRouter, Request

from portal.store.base import RepoAdmin
from portal.web.access import APP_ADMIN


def include_probe(portal):
    router = APIRouter()

    @router.get("/probe/apps/{owner}/{repo}")
    def probe(request: Request, owner: str, repo: str, principal=APP_ADMIN):
        return {"repo": request.state.app_repo, "repo_admin": request.state.app_repo_admin}
    portal.app.include_router(router)


def test_the_app_page_is_for_the_repo_s_admin_and_portal_admins(portal):
    include_probe(portal)
    assert portal.client("owner").get("/probe/apps/acme/notes").json() == {"repo": "acme/notes", "repo_admin": True}  # seeded
    assert portal.client("admin").get("/probe/apps/acme/notes").json() == {"repo": "acme/notes", "repo_admin": False}
    assert portal.client("other").get("/probe/apps/acme/notes").status_code == 404
    assert portal.client("ops").get("/probe/apps/acme/notes").status_code == 404
    assert portal.client("owner").get("/probe/apps/acme/other").status_code == 404
    assert portal.client().get("/probe/apps/acme/notes").status_code == 303


def test_the_repo_is_the_stored_name_whatever_the_url_s_case(portal):
    include_probe(portal)
    portal.store.replace_repo_admins([RepoAdmin("acme/Notes", "owner-gh", 1001, portal.clock())], observed_at=portal.clock())
    assert portal.client("owner").get("/probe/apps/ACME/notes").json()["repo"] == "acme/Notes"
    assert portal.client("admin").get("/probe/apps/acme/NOTES").json()["repo"] == "acme/Notes"


def test_an_admin_sees_only_repos_the_portal_knows(portal):
    include_probe(portal)
    assert portal.client("admin").get("/probe/apps/acme/tools").json()["repo"] == "acme/tools"   # the collector saw it
    assert portal.client("admin").get("/probe/apps/acme/unknown").status_code == 404


def test_a_stale_observation_is_not_admin(portal):
    include_probe(portal)
    portal.clock.advance(minutes=11)  # past repo_admins_stale_after (10 minutes); the session stays valid (idle 1 hour)
    assert portal.client("owner").get("/probe/apps/acme/notes").status_code == 404
    assert portal.client("admin").get("/probe/apps/acme/notes").status_code == 200


def test_bad_repo_names_are_404(portal):
    include_probe(portal)
    for path in ("/probe/apps/acme/..", "/probe/apps/acme/no%2Fpe", "/probe/apps/.../notes", "/probe/apps/acme/.", "/probe/apps/acme/" + "n" * 101):
        assert portal.client("admin").get(path).status_code == 404, path

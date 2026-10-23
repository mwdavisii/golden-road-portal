import httpx
import pytest
from portal_fakes import T0, FakeGitHub, transport

from portal.collectors.repo_admins import collect
from portal.github.client import GitHub, GitHubError
from portal.store.base import RepoAdmin
from portal.store.memory import MemoryStore

API = "https://api.github.com"


def github():
    gh = FakeGitHub()
    gh.on("GET", "/installation/repositories", (200, {"repositories": [{"full_name": "acme/notes"}, {"full_name": "acme/tools"}]}))
    gh.on("GET", "/repos/acme/notes/collaborators",
          (200, [{"login": "pat", "id": 1001}], {"link": f'<{API}/repos/acme/notes/collaborators?page=2>; rel="next"'}),
          (200, [{"login": "sam", "id": 1002}]))
    gh.on("GET", "/repos/acme/tools/collaborators", (200, [{"login": "pat", "id": 1001}]))
    return gh


def test_admins_of_every_installation_repo_are_recorded_in_one_go():
    gh, store = github(), MemoryStore()
    count = collect(GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x"), store, now=T0)
    assert count == 3 and [r.repo for r in store.admin_repos([1001])] == ["acme/notes", "acme/tools"]
    asked = gh.called("GET", "/repos/acme/notes/collaborators")[0]["query"]
    assert asked["permission"] == "admin" and asked["affiliation"] == "all"


def test_a_failed_run_leaves_the_last_observation():
    store = MemoryStore()
    store.replace_repo_admins([RepoAdmin("acme/notes", "pat", 1001, T0)], observed_at=T0)
    gh = github()
    gh.routes[("GET", "api.github.com", "/repos/acme/tools/collaborators")] = [(502, {"message": "bad gateway"})]
    with pytest.raises(GitHubError):
        collect(GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x"), store, now=T0)
    assert [r.repo for r in store.admin_repos([1001])] == ["acme/notes"]


def test_malformed_entries_are_skipped():
    gh = FakeGitHub()
    gh.on("GET", "/installation/repositories", (200, {"repositories": [{"full_name": "acme/notes"}, {"full_name": "../x"}]}))
    gh.on("GET", "/repos/acme/notes/collaborators", (200, [{"login": "pat", "id": "1001"}, {"login": 5, "id": 2}, {"login": "ok", "id": 3}]))
    store = MemoryStore()
    assert collect(GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x"), store, now=T0) == 1


def test_a_missing_setting_stops_the_collector_with_its_name():
    import io
    from contextlib import redirect_stderr

    from portal.collectors.__main__ import main
    err = io.StringIO()
    with redirect_stderr(err):
        assert main(["--once"], env={}) == 2
    assert "PORTAL_GITHUB_HOST" in err.getvalue()

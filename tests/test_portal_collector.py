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


FULL_ENV = {"PORTAL_GITHUB_HOST": "github.com", "PORTAL_READ_APP_ID": "7", "PORTAL_READ_INSTALLATION_ID": "9", "PORTAL_READ_APP_KEY_FILE": "__KEY__",
            "PORTAL_DATABASE_URL": "postgresql://unused", "PORTAL_ORG": "acme", "PORTAL_MODEL_GATEWAY_HOSTS": "litellm.internal"}


def env_with(tmp_path, **changes):
    key = tmp_path / "key.pem"
    key.write_bytes(b"not used: the token source is replaced")
    env = {**FULL_ENV, "PORTAL_READ_APP_KEY_FILE": str(key), **changes}
    return {k: v for k, v in env.items() if v is not None}


def run_main(argv, env):
    import io
    from contextlib import redirect_stderr

    from portal.collectors.__main__ import main
    err = io.StringIO()
    with redirect_stderr(err):
        code = main(argv, env=env)
    return code, err.getvalue()


def test_the_collector_needs_the_org_and_the_gateway_hosts(tmp_path):
    code, err = run_main(["--once"], env_with(tmp_path, PORTAL_ORG=None))
    assert code == 2 and "PORTAL_ORG" in err
    code, err = run_main(["--once"], env_with(tmp_path, PORTAL_MODEL_GATEWAY_HOSTS=None))
    assert code == 2 and "PORTAL_MODEL_GATEWAY_HOSTS" in err
    code, err = run_main(["--once"], env_with(tmp_path, PORTAL_MODEL_GATEWAY_HOSTS="litellm.internal,Bad Host"))
    assert code == 2 and "Bad Host" in err
    code, err = run_main(["--once"], env_with(tmp_path, PORTAL_ORG="bad org"))
    assert code == 2 and "PORTAL_ORG" in err


def test_one_pass_runs_repo_admins_then_evidence_and_a_failure_in_one_does_not_stop_the_other(tmp_path, monkeypatch):
    import portal.collectors.__main__ as entry
    import portal.store.postgres as postgres

    class Store:
        def close(self):
            pass

    seen = []

    def boom(gh, store, *, now):
        seen.append("repo_admins")
        raise GitHubError(502, "GET", "/installation/repositories")

    def evidence(gh, store, *, org, gateway_hosts, now):
        seen.append(("evidence", org, gateway_hosts))
        return {"computed": 2, "waiting": 1, "problem": "rules_missing"}

    monkeypatch.setattr(postgres, "PostgresStore", lambda url: Store())
    monkeypatch.setattr(entry, "collect", boom)
    monkeypatch.setattr(entry, "collect_evidence", evidence)
    monkeypatch.setattr(entry, "InstallationToken", lambda *a, **k: (lambda: "ghs_x"))
    code, err = run_main(["--once"], env_with(tmp_path))
    assert code == 1 and seen == ["repo_admins", ("evidence", "acme", ("litellm.internal",))]
    assert "repo_admins failed: 502 GET /installation/repositories" in err
    assert "collectors: evidence: 2 computed, 1 waiting (rules_missing)" in err

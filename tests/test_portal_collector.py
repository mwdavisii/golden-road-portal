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


def test_one_pass_runs_repo_admins_evidence_intake_and_events_and_a_failure_in_one_does_not_stop_the_others(tmp_path, monkeypatch):
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

    def intake(gh, store, *, org, today, due_soon_days, now):
        seen.append(("intake", org, due_soon_days))
        return {"records": 2, "unreadable": 0, "opened": 1, "closed": 0}

    def events(gh, store, *, org, signals, now, actions_readable, artifact_hosts):
        seen.append(("events", actions_readable))
        return {"stored": 0, "skipped": 0, "problem": "actions_unreadable"}

    monkeypatch.setattr(postgres, "PostgresStore", lambda url: Store())
    monkeypatch.setattr(entry, "collect", boom)
    monkeypatch.setattr(entry, "collect_evidence", evidence)
    monkeypatch.setattr(entry, "collect_intake", intake)
    monkeypatch.setattr(entry, "collect_events", events)
    monkeypatch.setattr(entry, "InstallationToken", lambda *a, **k: (lambda: "ghs_x"))
    code, err = run_main(["--once"], env_with(tmp_path))
    assert code == 1 and seen == ["repo_admins", ("evidence", "acme", ("litellm.internal",)), ("intake", "acme", 30), ("events", None)]
    assert "collectors: intake: 2 records (0 unreadable), 1 opened, 0 closed" in err
    assert "collectors: re-review events: 0 stored, 0 skipped (actions_unreadable: give the read App actions: read)" in err
    assert "repo_admins failed: 502 GET /installation/repositories" in err
    assert "collectors: evidence: 2 computed, 1 waiting (rules_missing)" in err


@pytest.mark.parametrize("name, value", [("PORTAL_REVIEW_DUE_SOON_DAYS", "soon"), ("PORTAL_REREVIEW_SIGNALS", "T-17,elevated")])
def test_a_bad_re_review_setting_stops_the_collector_with_its_name(tmp_path, name, value):
    code, err = run_main(["--once"], {**env_with(tmp_path), name: value})
    assert code == 2 and name in err and value not in err


def test_the_actions_check_reads_the_token_s_permissions(tmp_path, monkeypatch):
    import portal.collectors.__main__ as entry
    import portal.store.postgres as postgres

    class Token:
        permissions = {"metadata": "read", "contents": "read", "actions": "read"}

        def __call__(self):
            return "ghs_x"

    class Store:
        def close(self):
            pass

    seen = []
    monkeypatch.setattr(postgres, "PostgresStore", lambda url: Store())
    monkeypatch.setattr(entry, "InstallationToken", lambda *a, **k: Token())
    monkeypatch.setattr(entry, "collect", lambda gh, store, *, now: 0)
    monkeypatch.setattr(entry, "collect_evidence", lambda gh, store, **k: {"computed": 0, "waiting": 0})
    monkeypatch.setattr(entry, "collect_intake", lambda gh, store, **k: {"records": 0, "unreadable": 0, "opened": 0, "closed": 0})
    monkeypatch.setattr(entry, "collect_events", lambda gh, store, **k: seen.append(k["actions_readable"]) or {"stored": 0, "skipped": 0})
    code, _ = run_main(["--once"], {**env_with(tmp_path), "PORTAL_REREVIEW_SIGNALS": "T-17"})
    assert code == 0 and seen == [True]


# ---- more cases ----

def test_any_failure_in_one_part_is_logged_by_its_type_and_the_other_parts_still_run(tmp_path, monkeypatch):
    import portal.collectors.__main__ as entry
    import portal.store.postgres as postgres

    class Store:
        def close(self):
            pass

    seen = []

    def boom(*a, **k):
        raise RuntimeError("secret detail that must not be printed")

    monkeypatch.setattr(postgres, "PostgresStore", lambda url: Store())
    monkeypatch.setattr(entry, "InstallationToken", lambda *a, **k: (lambda: "ghs_x"))
    monkeypatch.setattr(entry, "collect", boom)
    monkeypatch.setattr(entry, "collect_evidence", lambda gh, store, **k: seen.append("evidence") or {"computed": 0, "waiting": 0})
    monkeypatch.setattr(entry, "collect_intake", boom)
    monkeypatch.setattr(entry, "collect_events", lambda gh, store, **k: seen.append(("events", k["artifact_hosts"])) or {"stored": 0, "skipped": 0})
    code, err = run_main(["--once"], env_with(tmp_path))
    assert code == 1 and seen == ["evidence", ("events", ("*.blob.core.windows.net",))]
    assert "collectors: repo_admins failed: RuntimeError" in err and "collectors: intake failed: RuntimeError" in err and "secret detail" not in err


def test_the_artifact_hosts_are_a_setting(tmp_path, monkeypatch):
    code, err = run_main(["--once"], {**env_with(tmp_path), "PORTAL_ARTIFACT_HOSTS": "https://evil.example.test/"})
    assert code == 2 and "PORTAL_ARTIFACT_HOSTS" in err and "evil" not in err
    from portal.collectors.__main__ import artifact_hosts
    assert artifact_hosts("") == ("*.blob.core.windows.net",) and artifact_hosts(" *.storage.example.test, files.example.test ") == (
        "*.storage.example.test", "files.example.test")


def test_the_http_client_logs_no_request_urls():
    """httpx logs each request's URL at INFO; a storage URL carries a signature."""
    import logging
    import portal.collectors.__main__  # noqa: F401
    assert logging.getLogger("httpx").level == logging.WARNING


def test_each_part_records_its_status_and_the_interval_so_the_page_can_tell_a_stopped_collector(tmp_path, monkeypatch):
    """a part that raised is "failed" with the time it last worked; the parts that report nothing of their own (repo
    admins, evidence) record that they ran, with the collector's interval."""
    import portal.collectors.__main__ as entry
    import portal.store.postgres as postgres
    from portal.store.memory import MemoryStore
    store = MemoryStore()
    store.close = lambda: None
    monkeypatch.setattr(postgres, "PostgresStore", lambda url: store)
    monkeypatch.setattr(entry, "InstallationToken", lambda *a, **k: (lambda: "ghs_x"))
    monkeypatch.setattr(entry, "collect", lambda gh, store, **k: 2)
    monkeypatch.setattr(entry, "collect_evidence", lambda gh, store, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(entry, "collect_intake", lambda gh, store, **k: {"records": 0, "unreadable": 0, "opened": 0, "closed": 0})
    monkeypatch.setattr(entry, "collect_events", lambda gh, store, **k: {"stored": 0, "skipped": 0})
    code, _ = run_main(["--once"], {**env_with(tmp_path), "PORTAL_COLLECT_INTERVAL": "120"})
    status = store.rereview_status()
    assert code == 1 and status["repo_admins"].problem is None and status["repo_admins"].counts == {"interval_seconds": 120}
    assert status["evidence"].problem == "failed" and status["evidence"].last_ok_at is None


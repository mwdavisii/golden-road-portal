import io
from contextlib import redirect_stderr

import httpx
import pytest
from portal_fakes import T0, FakeGitHub, approved_request, new_rsa_key, pem, transport
from test_portal_writer_bypass import recorded
from test_portal_writer_run import gh_for_write

from portal.writer.__main__ import main

R = "/repos/acme/scorecard-factory"
ENV = {"PORTAL_GITHUB_HOST": "github.com", "PORTAL_ORG": "acme", "PORTAL_ROLE_GROUPS": '{"admin": ["golden-road-admins"]}',
       "PORTAL_WRITE_APP_ID": "424242", "PORTAL_WRITE_INSTALLATION_ID": "777", "PORTAL_DATABASE_URL": "postgresql://unused"}


class StopLoop(Exception):
    pass


def run(tmp_path, gh, store, env=None, argv=("--once",), key=None, **kw):
    path = tmp_path / "write-app.pem"
    path.write_bytes(key if key is not None else pem(new_rsa_key()))
    err = io.StringIO()
    with redirect_stderr(err):
        code = main(list(argv), env={**ENV, "PORTAL_WRITE_APP_KEY_FILE": str(path), **(env or {})}, http=httpx.Client(transport=transport(gh)),
                    store=store, clock=lambda: T0, **kw)
    return code, err.getvalue()


def good_github():
    gh = recorded()
    gh.routes.update(gh_for_write().routes)
    gh.on("POST", "/app/installations/777/access_tokens", (201, {"token": "ghs_" + "w" * 36, "expires_at": "2099-01-01T00:00:00Z"}))
    return gh


def too_wide(edit=lambda d: d["ruleset_review"]["bypass_actors"][0].update(bypass_mode="always")):
    docs = recorded(edit)
    return {k: v for k, v in docs.routes.items() if "/rulesets/" in k[2]}


def test_a_missing_setting_stops_the_writer_with_its_name():
    err = io.StringIO()
    with redirect_stderr(err):
        assert main(["--once"], env={}) == 2
    assert "PORTAL_GITHUB_HOST" in err.getvalue()


@pytest.mark.parametrize("env, name", [({"PORTAL_ORG": "not an org!"}, "PORTAL_ORG"), ({"PORTAL_ORG": "-acme"}, "PORTAL_ORG"),
                                       ({"PORTAL_WRITE_APP_ID": "x"}, "PORTAL_WRITE_APP_ID"), ({"PORTAL_WRITER_INTERVAL": "1"}, "PORTAL_WRITER_INTERVAL"),
                                       ({"PORTAL_WRITER_INTERVAL": "99999"}, "PORTAL_WRITER_INTERVAL"),
                                       ({"PORTAL_WRITER_REQUIRED_CHECKS": "other-check"}, "PORTAL_WRITER_REQUIRED_CHECKS"),
                                       ({"PORTAL_WRITER_REQUIRED_CHECKS": "factory-check,<b>x</b>"}, "PORTAL_WRITER_REQUIRED_CHECKS")])
def test_a_bad_setting_stops_the_writer(portal, tmp_path, env, name):
    code, err = run(tmp_path, FakeGitHub(), portal.store, env=env)
    assert code == 2 and name in err


def test_more_required_checks_may_be_added_but_factory_check_stays(portal, tmp_path):
    gh = good_github()
    code, err = run(tmp_path, gh, portal.store, env={"PORTAL_WRITER_REQUIRED_CHECKS": "factory-check, another-check"})
    assert code == 3 and "another-check" in err  # the startup check wants the extra check enforced too


def test_a_wider_bypass_stops_the_writer_before_it_acts(portal, tmp_path):
    req = approved_request(portal.store, portal.people, T0)
    gh = good_github()
    gh.routes.update(too_wide())
    code, err = run(tmp_path, gh, portal.store)
    assert code == 3 and "refusing to start" in err and "bypass_mode" in err
    assert portal.store.get_request(req.id).state == "writing", "nothing was claimed or written"
    assert not gh.called("PUT", R + "/contents/intake/acme/notes.yaml")


def test_a_good_shape_runs_one_pass_and_prints_no_credential(portal, tmp_path):
    req = approved_request(portal.store, portal.people, T0)
    code, err = run(tmp_path, good_github(), portal.store)
    assert code == 0 and portal.store.get_request(req.id).state == "pr_open"
    assert "ghs_" not in err and "BEGIN" not in err and "PRIVATE KEY" not in err


def test_the_bypass_is_checked_again_at_the_start_of_every_pass(portal, tmp_path):
    first = approved_request(portal.store, portal.people, T0)
    gh = good_github()
    good_doc = gh.fixture("ruleset_review")
    wide = gh.fixture("ruleset_review")
    wide["bypass_actors"][0]["bypass_mode"] = "always"
    gh.routes[("GET", "api.github.com", R + "/rulesets/102")] = [(200, good_doc), (200, wide)]  # startup is fine; the next pass is not
    second, sleeps = {}, []

    def sleep(_):
        sleeps.append(1)
        if len(sleeps) == 1:  # between the passes a second request arrives
            second["req"] = approved_request(portal.store, portal.people, T0, repo="acme/tools")
        else:
            raise StopLoop

    err = io.StringIO()
    key = tmp_path / "k.pem"
    key.write_bytes(pem(new_rsa_key()))
    with redirect_stderr(err), pytest.raises(StopLoop):
        main([], env={**ENV, "PORTAL_WRITE_APP_KEY_FILE": str(key)}, http=httpx.Client(transport=transport(gh)), store=portal.store,
             clock=lambda: T0, sleep=sleep)
    text = err.getvalue()
    assert portal.store.get_request(first.id).state == "pr_open", "the first pass wrote"
    assert portal.store.get_request(second["req"].id).state == "writing", "the second pass wrote nothing"
    assert not gh.called("PUT", R + "/contents/intake/acme/tools.yaml")
    assert "skipping this pass" in text and "bypass_mode" in text


def test_an_unreadable_key_file_stops_the_writer(portal, tmp_path):
    code, err = run(tmp_path, FakeGitHub(), portal.store, env={"PORTAL_WRITE_APP_KEY_FILE": str(tmp_path / "missing.pem")})
    assert code == 2 and "PORTAL_WRITE_APP_KEY_FILE" in err


@pytest.mark.parametrize("key", [b"", b"not a key", b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"])
def test_a_key_that_is_not_a_private_key_stops_the_writer_without_a_traceback_or_the_key(portal, tmp_path, key):
    code, err = run(tmp_path, FakeGitHub(), portal.store, key=key)
    assert code == 2 and "PORTAL_WRITE_APP_KEY_FILE" in err and "Traceback" not in err
    assert "AAAA" not in err and "not a key" not in err


def load_with(tmp_path, **env):
    from portal.writer.__main__ import _load
    path = tmp_path / "write-app.pem"
    path.write_bytes(pem(new_rsa_key()))
    return _load({**ENV, "PORTAL_WRITE_APP_KEY_FILE": str(path), **env})[0]


def test_the_writer_loads_the_mock_directory_when_asked(tmp_path):
    from portal.settings import SettingsError
    cfg = load_with(tmp_path, PORTAL_DIRECTORY="mock")
    assert cfg.directory is not None and cfg.directory.name == "mock-directory"
    assert load_with(tmp_path).directory is None and load_with(tmp_path, PORTAL_DIRECTORY="").directory is None
    with pytest.raises(SettingsError, match="PORTAL_DIRECTORY"):
        load_with(tmp_path, PORTAL_DIRECTORY="azure")


def test_a_mock_directory_that_does_not_load_stops_the_writer(tmp_path, monkeypatch):
    from portal import directory
    from portal.settings import SettingsError
    monkeypatch.setattr(directory, "MOCK", tmp_path / "missing.json")
    with pytest.raises(SettingsError, match="PORTAL_DIRECTORY is mock, but the mock directory is not valid"):
        load_with(tmp_path, PORTAL_DIRECTORY="mock")


def test_the_writer_takes_the_portal_s_staleness_limit(tmp_path):
    import datetime as dt
    from portal.settings import SettingsError
    assert load_with(tmp_path).repo_admins_stale_after == dt.timedelta(seconds=600)
    assert load_with(tmp_path, PORTAL_REPO_ADMINS_STALE_AFTER="").repo_admins_stale_after == dt.timedelta(seconds=600)
    assert load_with(tmp_path, PORTAL_REPO_ADMINS_STALE_AFTER="900").repo_admins_stale_after == dt.timedelta(seconds=900)
    for bad in ("59", "x", str(7 * 86400 + 1)):
        with pytest.raises(SettingsError, match="PORTAL_REPO_ADMINS_STALE_AFTER"):
            load_with(tmp_path, PORTAL_REPO_ADMINS_STALE_AFTER=bad)


def test_a_bad_directory_setting_stops_the_writer_with_its_name(portal, tmp_path):
    code, err = run(tmp_path, FakeGitHub(), portal.store, env={"PORTAL_DIRECTORY": "azure"})
    assert code == 2 and "PORTAL_DIRECTORY" in err

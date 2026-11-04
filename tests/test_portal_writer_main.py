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
    write = gh_for_write()
    gh.routes.update(write.routes)
    gh.fallbacks += write.fallbacks
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
                                       ({"PORTAL_WRITER_REQUIRED_CHECKS": "factory-check,<b>x</b>"}, "PORTAL_WRITER_REQUIRED_CHECKS"),
                                       ({"PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS": "factory-check"}, "PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS"),
                                       ({"PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS": "deploy-check,<b>x</b>"}, "PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS")])
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



def test_more_deploy_required_checks_may_be_added_but_deploy_check_stays(portal, tmp_path):
    from portal.writer.__main__ import _load
    key = tmp_path / "k.pem"
    key.write_bytes(pem(new_rsa_key()))
    base = {**ENV, "PORTAL_WRITE_APP_KEY_FILE": str(key)}
    assert _load(base)[0].required_checks["acme/golden-road-deploy"] == ("deploy-check",)
    cfg = _load({**base, "PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS": "deploy-check, kustomize-build"})[0]
    assert cfg.required_checks == {"acme/scorecard-factory": ("factory-check",), "acme/golden-road-deploy": ("deploy-check", "kustomize-build")}
    code, err = run(tmp_path, good_github(), portal.store, env={"PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS": "deploy-check, kustomize-build"})
    assert code == 3 and "golden-road-deploy" in err and "kustomize-build" in err  # the startup check wants it enforced too


# ---- Delinea Secret Server: the writer reads PORTAL_SECRET_SERVER as the web does ----

def test_the_writer_loads_the_mock_secret_server_when_asked(tmp_path):
    from portal.secretserver_mock import MockSecretServer
    from portal.settings import SettingsError
    assert isinstance(load_with(tmp_path, PORTAL_SECRET_SERVER="mock").secret_server, MockSecretServer)
    assert load_with(tmp_path).secret_server is None and load_with(tmp_path, PORTAL_SECRET_SERVER="").secret_server is None
    for bad in ("delinea", "Mock", "real"):
        with pytest.raises(SettingsError, match="PORTAL_SECRET_SERVER"):
            load_with(tmp_path, PORTAL_SECRET_SERVER=bad)
    with pytest.raises(SettingsError, match="PORTAL_SECRET_SERVER_URL"):
        load_with(tmp_path, PORTAL_SECRET_SERVER="mock", PORTAL_SECRET_SERVER_URL="https://ss.example.test/SecretServer")


def test_a_mock_secret_server_that_does_not_load_stops_the_writer(tmp_path, monkeypatch):
    from portal import secretserver_mock
    from portal.settings import SettingsError
    monkeypatch.setattr(secretserver_mock, "MOCK", tmp_path / "missing.json")
    with pytest.raises(SettingsError, match="PORTAL_SECRET_SERVER is mock, but the mock Secret Server is not valid"):
        load_with(tmp_path, PORTAL_SECRET_SERVER="mock")


def test_a_bad_secret_server_setting_stops_the_writer_with_its_name(portal, tmp_path):
    code, err = run(tmp_path, FakeGitHub(), portal.store, env={"PORTAL_SECRET_SERVER": "delinea"})
    assert code == 2 and "PORTAL_SECRET_SERVER" in err and "delinea" not in err


# ---- MCP servers and skills: the writer reads PORTAL_CLOUDFLARE, the two gateways and PORTAL_SKILLS_CATALOG as the web does ----

def test_the_writer_loads_the_mock_cloudflare_and_the_gateways_when_asked(tmp_path):
    from portal.cloudflare_mock import MockCloudflare
    from portal.settings import SettingsError
    cfg = load_with(tmp_path)
    assert cfg.cloudflare is None and cfg.skills_catalog is None
    assert cfg.mcp_gateways == {"test": "mcp-test.example.com", "prod": "mcp.example.com"}
    cfg = load_with(tmp_path, PORTAL_CLOUDFLARE="mock", PORTAL_MCP_GATEWAY_PROD="mcp.acme.example")
    assert isinstance(cfg.cloudflare, MockCloudflare)
    assert cfg.mcp_gateways == {"test": "mcp-test.example.com", "prod": "mcp.acme.example"} == cfg.cloudflare.gateways
    for env, name in (({"PORTAL_CLOUDFLARE": "real"}, "PORTAL_CLOUDFLARE"), ({"PORTAL_MCP_GATEWAY_TEST": "not a host"}, "PORTAL_MCP_GATEWAY_TEST"),
                      ({"PORTAL_MCP_GATEWAY_TEST": "mcp.example.com"}, "PORTAL_MCP_GATEWAY_PROD"),
                      ({"PORTAL_CLOUDFLARE": "mock", "PORTAL_CLOUDFLARE_ACCOUNT_ID": "f" * 32}, "PORTAL_CLOUDFLARE_ACCOUNT_ID"),
                      ({"PORTAL_SKILLS_CATALOG": "git"}, "PORTAL_SKILLS_CATALOG")):
        with pytest.raises(SettingsError, match=name):
            load_with(tmp_path, **env)


def test_the_writer_loads_the_mock_skills_catalog_when_asked(tmp_path, monkeypatch):
    from portal import skills_catalog
    from portal.settings import SettingsError
    assert isinstance(load_with(tmp_path, PORTAL_SKILLS_CATALOG="mock").skills_catalog, skills_catalog.MockSkillsCatalog)
    monkeypatch.setattr(skills_catalog, "MOCK", tmp_path / "missing.json")
    with pytest.raises(SettingsError, match="PORTAL_SKILLS_CATALOG is mock, but the mock skills catalog is not valid"):
        load_with(tmp_path, PORTAL_SKILLS_CATALOG="mock")


@pytest.mark.parametrize("env, name", [({"PORTAL_CLOUDFLARE": "cloudflare"}, "PORTAL_CLOUDFLARE"),
                                       ({"PORTAL_MCP_GATEWAY_PROD": "bad host"}, "PORTAL_MCP_GATEWAY_PROD"),
                                       ({"PORTAL_CLOUDFLARE_ACCOUNT_ID": "f" * 32}, "PORTAL_CLOUDFLARE_ACCOUNT_ID"),
                                       ({"PORTAL_SKILLS_CATALOG": "git"}, "PORTAL_SKILLS_CATALOG")])
def test_a_bad_mcp_or_skills_setting_stops_the_writer_with_its_name(portal, tmp_path, env, name):
    code, err = run(tmp_path, FakeGitHub(), portal.store, env=env)
    assert code == 2 and name in err and next(iter(env.values())) not in err


# ---- the lifecycle App (spec 2026-11-02 L9): its key in the writer's Secret only, optional ----

def lifecycle_env(tmp_path, key=None, app_id="424243"):
    path = tmp_path / "lifecycle-app.pem"
    path.write_bytes(key if key is not None else pem(new_rsa_key()))
    return {"PORTAL_LIFECYCLE_APP_ID": app_id, "PORTAL_LIFECYCLE_APP_KEY_FILE": str(path)}


def test_the_writer_loads_the_lifecycle_app_when_both_settings_are_set(tmp_path):
    assert load_with(tmp_path).lifecycle_app is None, "no lifecycle App: a decommission is refused, suspend and resume go on"
    assert load_with(tmp_path, PORTAL_LIFECYCLE_APP_ID="", PORTAL_LIFECYCLE_APP_KEY_FILE="").lifecycle_app is None
    env = lifecycle_env(tmp_path)
    app = load_with(tmp_path, **env).lifecycle_app
    assert (app.app_id, app.api) == (424243, "https://api.github.com")
    assert app.key == (tmp_path / "lifecycle-app.pem").read_bytes()
    assert load_with(tmp_path, PORTAL_GITHUB_HOST="ghe.example.test", **env).lifecycle_app.api == "https://ghe.example.test/api/v3"


def test_the_writer_starts_with_the_lifecycle_app_and_gives_it_its_client(portal, tmp_path, monkeypatch):
    from portal.writer import __main__ as writer_main
    seen = []
    monkeypatch.setattr(writer_main, "run_once", lambda store, gh, cfg, now: seen.append(cfg) or [])
    code, err = run(tmp_path, good_github(), portal.store, env=lifecycle_env(tmp_path))
    assert code == 0 and seen[0].lifecycle_app.app_id == 424243 and seen[0].lifecycle_app.http is not None
    assert "BEGIN" not in err and "PRIVATE KEY" not in err


@pytest.mark.parametrize("drop", ["PORTAL_LIFECYCLE_APP_ID", "PORTAL_LIFECYCLE_APP_KEY_FILE"])
def test_one_lifecycle_setting_without_the_other_stops_the_writer(portal, tmp_path, drop):
    code, err = run(tmp_path, FakeGitHub(), portal.store, env={**lifecycle_env(tmp_path), drop: ""})
    assert code == 2 and "PORTAL_LIFECYCLE_APP_ID and PORTAL_LIFECYCLE_APP_KEY_FILE are set together, or neither" in err


def test_a_bad_lifecycle_app_id_stops_the_writer(portal, tmp_path):
    code, err = run(tmp_path, FakeGitHub(), portal.store, env=lifecycle_env(tmp_path, app_id="x"))
    assert code == 2 and "PORTAL_LIFECYCLE_APP_ID" in err


def ec_key():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


@pytest.mark.parametrize("key", [b"not a key", b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", "ec"])
def test_a_lifecycle_key_that_is_not_an_rsa_key_stops_the_writer_without_the_key(portal, tmp_path, key):
    raw = ec_key() if key == "ec" else key
    code, err = run(tmp_path, FakeGitHub(), portal.store, env=lifecycle_env(tmp_path, key=raw))
    assert code == 2 and "PORTAL_LIFECYCLE_APP_KEY_FILE" in err and "PORTAL_WRITE_APP_KEY_FILE" not in err and "Traceback" not in err
    assert "AAAA" not in err and "not a key" not in err and "BEGIN" not in err


def test_an_unreadable_lifecycle_key_file_stops_the_writer(portal, tmp_path):
    env = {**lifecycle_env(tmp_path), "PORTAL_LIFECYCLE_APP_KEY_FILE": str(tmp_path / "missing.pem")}
    code, err = run(tmp_path, FakeGitHub(), portal.store, env=env)
    assert code == 2 and "PORTAL_LIFECYCLE_APP_KEY_FILE can't be read" in err


def test_the_writer_learns_its_bot_login_from_the_installation(portal, tmp_path, monkeypatch):
    from portal.writer import __main__ as writer_main
    seen = []
    monkeypatch.setattr(writer_main, "run_once", lambda store, gh, cfg, now: seen.append(cfg) or [])
    code, _ = run(tmp_path, good_github(), portal.store)
    assert code == 0 and seen[0].write_login == "golden-road-portal-writer[bot]"


@pytest.mark.parametrize("edit", [lambda d: d.pop("app_slug"), lambda d: d.update(app_id=1), lambda d: d.update(app_slug="Not A Slug")])
def test_an_installation_that_does_not_name_the_write_app_stops_the_writer(portal, tmp_path, edit):
    gh = good_github()
    doc = gh.fixture("installation")
    edit(doc)
    gh.routes[("GET", "api.github.com", "/app/installations/777")] = [(200, doc)]
    req = approved_request(portal.store, portal.people, T0)
    code, err = run(tmp_path, gh, portal.store)
    assert code == 3 and "refusing to start" in err and portal.store.get_request(req.id).state == "writing"


def test_a_lifecycle_app_that_is_the_write_app_stops_the_writer(portal, tmp_path):
    code, err = run(tmp_path, FakeGitHub(), portal.store, env=lifecycle_env(tmp_path, app_id="424242"))
    assert code == 2 and "PORTAL_LIFECYCLE_APP_ID must be another App than PORTAL_WRITE_APP_ID" in err

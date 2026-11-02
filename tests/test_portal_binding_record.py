"""The next binding/v2 document (one environment's) from what the app's admin picked (spec 2026-10-29 app bindings, B2/B5). Every choice is checked
against the directory on the server, whatever a form posts."""

import pytest
from portal_fakes import T0, fake_directory

from portal.binding_record import BindingRefused, next_binding
from portal.contracts import validate

P, R = "p_" + "a" * 32, "r_" + "b" * 32


def make(current=None, repo="acme/notes", environment="test", **choice):
    return next_binding(current=current, repo=repo, org="acme", directory=fake_directory(), person_id=P, request_id=R, now=T0,
                        environment=environment, **choice)


def test_linking_the_identity():
    doc = make(principal_id="sp-notes")
    assert doc["azure_identity"] == {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                                     "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"}
    assert "databricks" not in doc and validate("binding.v2", doc) == []
    assert (doc["schema"], doc["repo"], doc["environment"], doc["linked_by"], doc["request"], doc["linked_at"], doc["secrets"]) == (
        "binding/v2", "acme/notes", "test", P, R, "2026-10-23T12:00:00Z", [])


def test_the_repo_is_recorded_as_given_never_lowercased():
    doc = make(repo="acme/Notes", principal_id="sp-notes")   # tagged_repo_name matches case-insensitively, like GitHub
    assert doc["repo"] == "acme/Notes"


def test_then_databricks_keeps_the_identity():
    first = make(principal_id="sp-notes")
    doc = make(current=first, warehouse_id="wh-notes")
    assert doc["azure_identity"] == first["azure_identity"] and doc["databricks"] == {
        "directory_id": "wh-notes", "host": "adb-1111111111111111.1.azuredatabricks.net", "http_path": "/sql/1.0/warehouses/0000notes0000",
        "catalog": "notes", "schema": "public", "verified_by": "mock-directory"}
    assert validate("binding.v2", doc) == []


def test_relinking_the_same_identity_keeps_its_databricks_connection():
    full = make(current=make(principal_id="sp-notes"), warehouse_id="wh-notes")
    assert make(current=full, principal_id="sp-notes")["databricks"] == full["databricks"]


def test_relinking_the_identity_rebuilds_the_kept_databricks_connection_from_the_directory_as_it_is_now():
    import copy
    from portal.directory import parse
    from portal_fakes import FAKE_DIRECTORY
    full = make(current=make(principal_id="sp-notes"), warehouse_id="wh-notes")
    changed = copy.deepcopy(FAKE_DIRECTORY)
    changed["databricks"][0].update(host="adb-3333333333333333.3.azuredatabricks.net", http_path="/sql/1.0/warehouses/0000moved0000",
                                    catalog="notes2")
    del changed["databricks"][0]["schema"]
    doc = next_binding(current=full, repo="acme/notes", org="acme", directory=parse(changed), person_id=P, request_id=R, now=T0,
                       environment="test", principal_id="sp-notes")
    assert doc["databricks"] == {"directory_id": "wh-notes", "host": "adb-3333333333333333.3.azuredatabricks.net",
                                 "http_path": "/sql/1.0/warehouses/0000moved0000", "catalog": "notes2", "verified_by": "mock-directory"}
    assert validate("binding.v2", doc) == []


@pytest.mark.parametrize("choice, message", [
    ({"principal_id": "sp-other"}, "isn't tagged for acme/notes"),          # in the directory, tagged for another repo
    ({"principal_id": "sp-missing"}, "isn't tagged for acme/notes"),        # not in the directory at all
    ({"principal_id": "wh-notes"}, "isn't tagged for acme/notes"),          # a warehouse id posted as a principal
    ({"warehouse_id": "wh-notes"}, "Link an Azure identity first"),
    ({}, "Choose one thing"),
    ({"principal_id": "sp-notes", "remove": "databricks"}, "Choose one thing"),
    ({"remove": "everything"}, "Choose what to remove"),
])
def test_refused_choices(choice, message):
    with pytest.raises(BindingRefused, match=message):
        make(**choice)


def test_a_principal_is_offered_only_under_the_portal_s_org():
    with pytest.raises(BindingRefused, match="isn't tagged"):
        next_binding(current=None, repo="other-org/notes", org="acme", directory=fake_directory(), person_id=P, request_id=R, now=T0,
                     environment="test", principal_id="sp-notes")


def test_a_warehouse_not_granted_to_the_identity_is_refused():
    with pytest.raises(BindingRefused, match="isn't granted to sp-notes"):
        make(current=make(principal_id="sp-notes"), warehouse_id="wh-other")
    with pytest.raises(BindingRefused, match="isn't granted to sp-notes"):
        make(current=make(principal_id="sp-notes"), warehouse_id="wh-missing")


def test_databricks_needs_the_current_identity_to_still_be_tagged_for_the_repo():
    """The identity in the current binding is the directory's too: one no longer tagged for this repo gets no warehouse."""
    stale = {**make(principal_id="sp-notes"), "azure_identity": {"directory_id": "sp-other", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                                                                 "client_id": "33333333-3333-4333-8333-333333333333", "verified_by": "mock-directory"}}
    with pytest.raises(BindingRefused, match="isn't tagged for acme/notes"):
        make(current=stale, warehouse_id="wh-other")


def test_removing_the_identity_removes_databricks_too():
    full = make(current=make(principal_id="sp-notes"), warehouse_id="wh-notes")
    doc = make(current=full, remove="azure-identity")
    assert "azure_identity" not in doc and "databricks" not in doc and validate("binding.v2", doc) == []
    assert (doc["linked_by"], doc["request"]) == (P, R)   # who unlinked and when stay on record


def test_removing_databricks_keeps_the_identity():
    full = make(current=make(principal_id="sp-notes"), warehouse_id="wh-notes")
    doc = make(current=full, remove="databricks")
    assert doc["azure_identity"] == full["azure_identity"] and "databricks" not in doc


def test_removing_what_is_not_linked_is_refused():
    with pytest.raises(BindingRefused, match="nothing to remove"):
        make(remove="databricks")
    with pytest.raises(BindingRefused, match="nothing to remove"):
        make(remove="azure-identity")


def test_linking_databricks_takes_the_identity_from_the_directory_not_the_old_binding():
    first = make(principal_id="sp-notes")
    stale = {**first, "azure_identity": {**first["azure_identity"], "client_id": "99999999-9999-4999-8999-999999999999",
                                         "tenant_id": "99999999-0000-4000-8000-000000000000"}}
    doc = make(current=stale, warehouse_id="wh-notes")
    assert doc["azure_identity"] == first["azure_identity"]


def test_removing_databricks_from_an_identity_no_longer_tagged_says_to_remove_the_identity():
    full = make(current=make(principal_id="sp-notes"), warehouse_id="wh-notes")
    stale = {**full, "azure_identity": {**full["azure_identity"], "directory_id": "sp-other"}}
    with pytest.raises(BindingRefused, match="Remove the Azure identity instead"):
        make(current=stale, remove="databricks")
    doc = make(current=stale, remove="azure-identity")   # removing the identity is always allowed
    assert "azure_identity" not in doc and "databricks" not in doc


# ---- per environment (spec 2026-10-29 GitOps deploy repo, G3) ----

def test_each_environment_gets_its_own_binding_v2():
    prod = make(environment="prod", principal_id="sp-notes")
    assert (prod["schema"], prod["environment"]) == ("binding/v2", "prod") and validate("binding.v2", prod) == []


@pytest.mark.parametrize("environment", ["staging", "", None, "Test", "prod\n"])
def test_an_environment_other_than_test_or_prod_is_refused(environment):
    with pytest.raises(BindingRefused, match="test or prod"):
        make(environment=environment, principal_id="sp-notes")


def test_a_current_binding_from_another_environment_or_a_v1_record_is_carried_over_as_this_one():
    """The current binding's identity and connection carry over; its environment and schema never do."""
    v1 = {"schema": "binding/v1", "repo": "acme/notes", "linked_by": P, "linked_at": "2026-10-23T12:00:00Z", "request": R,
          "azure_identity": make(principal_id="sp-notes")["azure_identity"]}
    doc = make(current=v1, environment="prod", warehouse_id="wh-notes")
    assert (doc["schema"], doc["environment"]) == ("binding/v2", "prod") and validate("binding.v2", doc) == []


def _req(state, environment=None, kind="binding", schema="binding/v2", n=0):
    import types
    draft = {"schema": schema, "n": n, **({"environment": environment} if environment else {})}
    return types.SimpleNamespace(kind=kind, state=state, draft=draft, final=draft)


def test_environment_of_reads_the_draft_and_an_older_binding_without_one_is_test():
    from portal.binding_record import environment_of
    assert environment_of(_req("merged", "prod")) == "prod" and environment_of(_req("merged", "test")) == "test"
    assert environment_of(_req("merged", schema="binding/v1")) == "test"     # 0.3.16's bindings were the test environment's
    assert environment_of(_req("merged", "staging")) is None                 # not an environment: matches neither


def test_the_state_of_one_environment_ignores_the_other():
    from portal.binding_record import environment_state
    newest_first = [_req("failed", "prod", n=4), _req("pr_open", "prod", n=3), _req("merged", "test", n=2), _req("merged", "prod", n=1),
                    _req("merged", schema="binding/v1", n=0), _req("merged", kind="registration")]
    current, open_one, failed = environment_state(newest_first, "test")
    assert (current["n"], open_one, failed) == (2, None, None)
    current, open_one, failed = environment_state(newest_first, "prod")
    assert (current["n"], open_one.draft["n"], failed.draft["n"]) == (1, 3, 4)
    assert environment_state(newest_first[3:], "test")[0]["n"] == 0   # a v1 record counts as test's
    with pytest.raises(ValueError):
        environment_state(newest_first, "staging")


def test_the_open_states_are_the_store_s():
    from portal import binding_record
    from portal.store import base
    assert binding_record.OPEN_STATES == base.OPEN_STATES


# Delinea Secret Server secrets (spec 2026-10-29 Delinea secrets, "The portal"): checked as the person (can see it) and as the
# platform (shared with golden-road-eso, a non-file field). The mock user mwdavisii sees folders 17 and 18: 1042 notes-db (shared),
# 1043 notes-api-key (not shared), 1044 notes-tls (shared, a certificate file field and a passphrase), 1050 portal-smtp; 2001
# finance-db is other's.

from portal import env_names  # noqa: E402
from portal.secretserver import SecretServerError  # noqa: E402
from portal.secretserver_mock import load_mock, parse as parse_mock  # noqa: E402


@pytest.fixture
def secret_server():
    return load_mock()


def add(secret_server, current=None, secret_user="mwdavisii", secret_id=1042, field_slug="password", env="NOTES_DB_PASSWORD", **kw):
    return make(current=current, add_secret={"secret_id": secret_id, "field_slug": field_slug, "env": env},
                secret_server=secret_server, secret_user=secret_user, **kw)


def test_adding_a_secret_records_secret_server_s_reference(secret_server):
    doc = add(secret_server)
    assert doc["secrets"] == [{"env": "NOTES_DB_PASSWORD", "secret_id": 1042, "secret_name": "notes-db", "folder_id": 17,
                               "field_slug": "password", "verified_by": "mock-secret-server"}]
    assert validate("binding.v2", doc) == [] and "azure_identity" not in doc


def test_a_secret_needs_no_identity_and_an_identity_keeps_it(secret_server):
    doc = add(secret_server)
    linked = make(current=doc, principal_id="sp-notes")
    assert linked["secrets"] == doc["secrets"] and linked["azure_identity"]["directory_id"] == "sp-notes"
    with_data = make(current=linked, warehouse_id="wh-notes")
    assert with_data["secrets"] == doc["secrets"]
    assert make(current=with_data, remove="azure-identity")["secrets"] == doc["secrets"]
    assert make(current=with_data, remove="databricks")["secrets"] == doc["secrets"]
    kept = add(secret_server, current=with_data, secret_id=1050, field_slug="username", env="SMTP_USER")
    assert kept["azure_identity"] == with_data["azure_identity"] and kept["databricks"] == with_data["databricks"]


def test_secrets_are_kept_sorted_by_env(secret_server):
    doc = add(secret_server, env="ZED")
    doc = add(secret_server, current=doc, secret_id=1050, field_slug="username", env="ALPHA")
    doc = add(secret_server, current=doc, secret_id=1044, field_slug="passphrase", env="MIDDLE")
    assert [s["env"] for s in doc["secrets"]] == ["ALPHA", "MIDDLE", "ZED"]
    assert validate("binding.v2", doc) == []


def test_removing_a_secret(secret_server):
    doc = add(secret_server, current=add(secret_server, env="A"), secret_id=1050, field_slug="url", env="B")
    after = make(current=doc, remove_secret="A")
    assert [s["env"] for s in after["secrets"]] == ["B"] and validate("binding.v2", after) == []
    for bad in ("C", "", 3, ["A"], "a", "<b>x</b>", "A\n", "A" * 129):
        with pytest.raises(BindingRefused, match="^This environment has no secret by that name: there is nothing to remove.$"):
            make(current=doc, remove_secret=bad)


def test_a_file_field_is_refused_and_its_other_field_accepted(secret_server):
    with pytest.raises(BindingRefused, match="not a field this secret can deliver"):
        add(secret_server, secret_id=1044, field_slug="certificate")
    assert add(secret_server, secret_id=1044, field_slug="passphrase")["secrets"][0]["field_slug"] == "passphrase"
    with pytest.raises(BindingRefused, match="not a field this secret can deliver"):
        add(secret_server, secret_id=1042, field_slug="api-key")   # another template's field


def test_a_secret_not_shared_with_the_platform_is_refused(secret_server):
    with pytest.raises(BindingRefused, match="isn't shared with golden-road-eso"):
        add(secret_server, secret_id=1043, field_slug="api-key")


@pytest.mark.parametrize("secret_id", [2001, 9999])
def test_a_secret_the_person_can_t_see_is_refused(secret_server, secret_id):
    with pytest.raises(BindingRefused, match="You can't see this secret in Secret Server"):
        add(secret_server, secret_id=secret_id)


@pytest.mark.parametrize("server, user, message", [
    (None, "mwdavisii", "Secret Server is not set up here"),
    ("mock", None, "Link your GitHub account \\(verified\\) first"),
    ("mock", "", "Link your GitHub account \\(verified\\) first"),
])
def test_secret_server_and_a_verified_link_are_needed(secret_server, server, user, message):
    with pytest.raises(BindingRefused, match=message):
        add(secret_server if server else None, secret_user=user)


@pytest.mark.parametrize("secret_id, slug", [(0, "password"), (-1, "password"), ("1042", "password"), (True, "password"),
                                             (1042.0, "password"), (None, "password"), (2 ** 31, "password"),
                                             (1042, "Password"), (1042, ""), (1042, "pass word"), (1042, None), (1042, "-x")])
def test_a_malformed_choice_is_refused(secret_server, secret_id, slug):
    with pytest.raises(BindingRefused, match="Choose a secret and one of its fields"):
        add(secret_server, secret_id=secret_id, field_slug=slug)


@pytest.mark.parametrize("env", ["PORT", "HOME", "LD_PRELOAD", "PYTHONPATH", "AZURE_CLIENT_ID", "DATABRICKS_HOST", "SECRETSERVER_X",
                                 "lower", "1ABC", "A-B", "", "A" * 129, "A\n", None, 5])
def test_a_refused_env_name(secret_server, env):
    with pytest.raises(BindingRefused, match=r"Name the variable|set by the platform"):
        add(secret_server, env=env)


def test_a_duplicate_env_is_refused(secret_server):
    doc = add(secret_server)
    with pytest.raises(BindingRefused, match="NOTES_DB_PASSWORD already holds a secret"):
        add(secret_server, current=doc, secret_id=1050, field_slug="username")


def test_at_most_twenty_secrets(secret_server):
    doc = None
    for i in range(20):
        doc = add(secret_server, current=doc, env=f"S{i:02d}")
    assert len(doc["secrets"]) == 20 and validate("binding.v2", doc) == []
    with pytest.raises(BindingRefused, match="at most 20 secrets"):
        add(secret_server, current=doc, env="S20")


def test_the_checks_run_in_order(secret_server):
    full = None
    for i in range(20):
        full = add(secret_server, current=full, env=f"S{i:02d}")
    # a reserved env is refused before the count, a duplicate before the count, and the count before Secret Server is asked
    with pytest.raises(BindingRefused, match="set by the platform"):
        add(secret_server, current=full, env="PORT")
    with pytest.raises(BindingRefused, match="already holds"):
        add(secret_server, current=full, env="S00")
    with pytest.raises(BindingRefused, match="at most 20"):
        add(secret_server, current=full, secret_id=2001, env="S20")
    with pytest.raises(BindingRefused, match="Choose a secret"):
        add(secret_server, secret_id=0, env="PORT")


def test_secret_server_failing_is_a_plain_refusal(secret_server, monkeypatch):
    from portal.secretserver import Client

    def fail(self, *a, **kw):
        raise SecretServerError("Secret Server failed (500) answering for /api/v1/secrets")
    for method in ("token", "secret_summary", "shared_with_platform", "template_fields"):
        with monkeypatch.context() as m:
            m.setattr(Client, method, fail)
            with pytest.raises(BindingRefused, match="^Secret Server could not answer: try again"):
                add(secret_server)


def test_a_secret_name_the_binding_can_t_hold_is_refused(secret_server):
    import json
    from portal.secretserver_mock import MOCK
    doc = json.loads(MOCK.read_text())
    doc["secrets"][0]["name"] = "notes db!"
    with pytest.raises(BindingRefused, match="This secret's name can't be recorded in a binding: rename it in Secret Server"):
        add(parse_mock(doc))


def test_the_secret_s_name_and_folder_come_from_secret_server_never_the_form(secret_server):
    doc = make(add_secret={"secret_id": 1042, "field_slug": "password", "env": "X", "secret_name": "evil", "folder_id": 99},
               secret_server=secret_server, secret_user="mwdavisii")
    assert doc["secrets"][0]["secret_name"] == "notes-db" and doc["secrets"][0]["folder_id"] == 17
    with pytest.raises(BindingRefused, match="Choose a secret"):
        make(add_secret="1042", secret_server=secret_server, secret_user="mwdavisii")


def test_a_secret_change_counts_as_the_one_choice(secret_server):
    with pytest.raises(BindingRefused, match="Choose one thing"):
        add(secret_server, principal_id="sp-notes")
    with pytest.raises(BindingRefused, match="Choose one thing"):
        make(remove_secret="A", remove="databricks")


def test_the_person_is_asked_as_the_person_and_sharing_as_the_platform(secret_server, monkeypatch):
    from portal.secretserver import Client
    seen = []
    for method in ("secret_summary", "search_secrets", "shared_with_platform", "template_fields"):
        original = getattr(Client, method)

        def spy(self, token, *a, _name=method, _original=original, **kw):
            seen.append((_name, token))
            return _original(self, token, *a, **kw)
        monkeypatch.setattr(Client, method, spy)
    add(secret_server)
    assert seen == [("secret_summary", "mock-mwdavisii"), ("shared_with_platform", "mock-golden-road-eso"),
                    ("template_fields", "mock-golden-road-eso")]


def test_env_names_refused_says_why():
    assert env_names.refused("NOTES_DB_PASSWORD") is None and env_names.refused("_A") is None
    assert "set by the platform" in env_names.refused("AZURE_TENANT_ID")
    assert "capitals" in env_names.refused("a")


def test_visibility_is_the_person_s_summary_by_id_never_a_full_search(secret_server, monkeypatch):
    """A search through every page would fail past the client's page limit; the person's /summary of the one id doesn't."""
    from portal.secretserver import Client

    def no_search(self, *a, **kw):
        raise AssertionError("next_binding must not search")
    monkeypatch.setattr(Client, "search_secrets", no_search)
    monkeypatch.setattr(Client, "search_first", no_search)
    assert add(secret_server)["secrets"][0]["secret_id"] == 1042


def test_the_client_is_closed_after_the_checks(secret_server, monkeypatch):
    from portal.secretserver import Client
    closed = []
    monkeypatch.setattr(Client, "close", lambda self: closed.append(self))
    add(secret_server)
    with pytest.raises(BindingRefused):
        add(secret_server, secret_id=2001)
    assert len(closed) == 2


def test_only_the_mock_mints_a_person_s_token(secret_server):
    from portal.binding_record import secret_tokens

    class Real:
        name, platform_user = "secret-server", "golden-road-eso"

        def client(self):
            raise AssertionError("never reached")
    with pytest.raises(RuntimeError, match="person's own"):
        with secret_tokens(Real(), "mwdavisii"):
            pass


# ---- MCP servers and skills (spec 2026-10-31 Parts 1 and 2) ----
# The mock Cloudflare's policy gives mwdavisii@example.com's servers to owner@example.test (as the secrets tests swap logins):
# notes (both gateways, ready), tickets (on_behalf: an app can't use it), docs-search (test, not ready); finance-ledger (prod) is
# other@example.com's. The mock catalog's main has incident-triage (test, prod; everyone), release-notes (test only) and
# finance-close (prod; finance-analysts only); its older commit OLD has incident-triage only.

import copy  # noqa: E402
import json  # noqa: E402

import httpx  # noqa: E402
from portal_fakes import mock_cloudflare  # noqa: E402

from portal import skills_catalog as catalog_module  # noqa: E402
from portal.binding_record import CloudflareUnavailable  # noqa: E402
from portal.cloudflare_mock import MOCK as CLOUDFLARE_MOCK, MockCloudflare  # noqa: E402

COMMON = dict(current=None, repo="acme/notes", org="acme", directory=fake_directory(), person_id=P, request_id=R, now=T0,
              environment="test")
EMAIL = "owner@example.test"
OLD = "2c7d1e9f0a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d"
MAIN = "6b1f0c2d9e8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"


@pytest.fixture
def cloudflare():
    return mock_cloudflare()


@pytest.fixture
def catalog():
    return catalog_module.load_mock()


def add_mcp(cloudflare, server_id="notes", current=None, environment="test", email=EMAIL, gateway=None, **kw):
    return next_binding(**{**COMMON, "current": current, "environment": environment}, add_mcp=server_id, cloudflare=cloudflare,
                        mcp_gateway=gateway or cloudflare.gateways[environment], person_email=email, **kw)


def skill(catalog, current=None, environment="test", groups=(), **choice):
    return next_binding(**{**COMMON, "current": current, "environment": environment}, skills_catalog=catalog, person_groups=groups, **choice)


def test_add_notes_then_remove_it_keeps_the_mcp_key(cloudflare):
    doc = add_mcp(cloudflare)
    assert doc["mcp"] == {"gateway": "mcp-test.example.com", "servers": [{"server_id": "notes", "name": "Notes", "verified_by": "mock-cloudflare"}]}
    assert validate("binding.v2", doc) == [] and "azure_identity" not in doc and "skills" not in doc
    doc = next_binding(**{**COMMON, "current": doc}, remove_mcp="notes")
    assert doc["mcp"] == {"gateway": "mcp-test.example.com", "servers": []} and validate("binding.v2", doc) == []


def test_no_mcp_key_until_a_server_is_added(cloudflare):
    assert "mcp" not in make(principal_id="sp-notes") and "skills" not in make(principal_id="sp-notes")


@pytest.mark.parametrize("server_id, environment, needle", [
    ("tickets", "test", "own sign-in"), ("docs-search", "test", "not ready"), ("finance-ledger", "prod", "can't use"),
    ("finance-ledger", "test", "not on the test gateway"), ("Notes", "test", "from the list"), ("", "test", "from the list"),
    (7, "test", "from the list"), ("nope", "test", "not on the test gateway")])
def test_servers_an_app_may_not_have_are_refused(cloudflare, server_id, environment, needle):
    gateway = cloudflare.gateways[environment]
    with pytest.raises(BindingRefused, match=needle):
        next_binding(**{**COMMON, "environment": environment}, add_mcp=server_id, cloudflare=cloudflare, mcp_gateway=gateway,
                     person_email="owner@example.test")


def test_the_on_behalf_refusal_is_the_spec_s_sentence(cloudflare):
    with pytest.raises(BindingRefused, match="^This server needs each person's own sign-in, so an app can't use it.$"):
        add_mcp(cloudflare, "tickets")


def test_other_may_use_finance_ledger_in_prod(cloudflare):
    doc = add_mcp(cloudflare, "finance-ledger", environment="prod", email="other@example.com")
    assert doc["mcp"]["gateway"] == "mcp.example.com" and [s["server_id"] for s in doc["mcp"]["servers"]] == ["finance-ledger"]


def test_cloudflare_and_an_email_are_needed(cloudflare):
    with pytest.raises(BindingRefused, match="^Cloudflare is not set up here.$"):
        next_binding(**COMMON, add_mcp="notes", cloudflare=None, mcp_gateway="mcp-test.example.com", person_email=EMAIL)
    for email in (None, ""):
        with pytest.raises(BindingRefused, match="has no email address"):
            add_mcp(cloudflare, email=email)


def test_a_duplicate_server_is_refused_and_servers_are_sorted(cloudflare):
    doc = add_mcp(cloudflare)
    with pytest.raises(BindingRefused, match="^notes is already on this app in test.$"):
        add_mcp(cloudflare, current=doc)
    doc2 = json.loads(CLOUDFLARE_MOCK.read_text())
    doc2["portals"][0]["servers"].append({**doc2["portals"][0]["servers"][0], "server_id": "aardvark", "name": "Aardvark"})
    doc2["users"][0]["servers"].append("aardvark")
    wider = mock_cloudflare(doc=doc2)
    after = add_mcp(wider, "aardvark", current=doc)
    assert [s["server_id"] for s in after["mcp"]["servers"]] == ["aardvark", "notes"]


def test_at_most_twenty_servers():
    doc = json.loads(CLOUDFLARE_MOCK.read_text())
    template = doc["portals"][0]["servers"][0]
    doc["portals"][0]["servers"] += [{**template, "server_id": f"s{i:02d}", "name": f"Server {i}"} for i in range(21)]
    doc["users"][0]["servers"] += [f"s{i:02d}" for i in range(21)]
    many = mock_cloudflare(doc=doc)
    current = None
    for i in range(20):
        current = add_mcp(many, f"s{i:02d}", current=current)
    assert len(current["mcp"]["servers"]) == 20 and validate("binding.v2", current) == []
    with pytest.raises(BindingRefused, match="^An environment has at most 20 MCP servers.$"):
        add_mcp(many, "s20", current=current)


def test_a_changed_gateway_is_refused(cloudflare):
    doc = add_mcp(cloudflare)
    old = {"server_id": "old-one", "name": "Old", "verified_by": "mock-cloudflare"}
    moved = {**doc, "mcp": {"gateway": "mcp-old.example.com", "servers": [old]}}
    with pytest.raises(BindingRefused, match="gateway has changed: remove its servers and add them again"):
        add_mcp(cloudflare, "notes", current=moved)
    emptied = {**doc, "mcp": {"gateway": "mcp-old.example.com", "servers": []}}   # nothing left on the old one: the new one replaces it
    assert add_mcp(cloudflare, "notes", current=emptied)["mcp"]["gateway"] == "mcp-test.example.com"


def test_a_gateway_cloudflare_doesn_t_have_is_refused(cloudflare):
    with pytest.raises(BindingRefused, match=r"^The test MCP gateway \(mcp-nowhere.example.com\) is not in Cloudflare.$"):
        add_mcp(cloudflare, gateway="mcp-nowhere.example.com")


def test_cloudflare_down_is_unavailable_not_a_refusal(cloudflare):
    class Down(MockCloudflare):
        def _handle(self, request):
            return httpx.Response(500, json={"success": False, "errors": [{"code": 1, "message": "secret detail"}]})
    down = Down(cloudflare.account_id, cloudflare.portals, cloudflare.users)
    with pytest.raises(CloudflareUnavailable, match="^Cloudflare could not answer: try again.$"):
        add_mcp(down)


def test_a_server_name_the_binding_can_t_hold_is_refused(cloudflare, monkeypatch):
    import dataclasses
    from portal.cloudflare import Client
    original = Client.gateway

    def renamed(self, hostname):
        found = original(self, hostname)
        return dataclasses.replace(found, servers=tuple(dataclasses.replace(s, name="line\nbreak") for s in found.servers))
    monkeypatch.setattr(Client, "gateway", renamed)
    with pytest.raises(BindingRefused, match="^This server's name can't be recorded: ask the gateway's admins to rename it.$"):
        add_mcp(cloudflare)


def test_removing_a_server_that_isn_t_bound_is_refused(cloudflare):
    doc = add_mcp(cloudflare)
    for bad in ("tickets", "", 3, ["notes"], "NOTES"):
        with pytest.raises(BindingRefused, match="^This environment has no MCP server by that id: there is nothing to remove.$"):
            next_binding(**{**COMMON, "current": doc}, remove_mcp=bad)
    with pytest.raises(BindingRefused, match="nothing to remove"):
        next_binding(**COMMON, remove_mcp="notes")


def test_mcp_and_skills_count_as_the_one_choice(cloudflare, catalog):
    with pytest.raises(BindingRefused, match="Choose one thing"):
        next_binding(**COMMON, add_mcp="notes", add_skill="incident-triage", cloudflare=cloudflare, mcp_gateway="mcp-test.example.com",
                     person_email=EMAIL, skills_catalog=catalog)
    with pytest.raises(BindingRefused, match="Choose one thing"):
        next_binding(**COMMON, principal_id="sp-notes", remove_skill="incident-triage")


def test_add_a_skill_pins_main(catalog):
    doc = skill(catalog, add_skill="incident-triage")
    assert doc["skills"] == [{"name": "incident-triage", "commit": MAIN, "verified_by": "mock-skills-catalog"}]
    assert validate("binding.v2", doc) == [] and "mcp" not in doc


def test_skills_are_sorted_and_refused_outside_their_environment_or_audience(catalog):
    doc = skill(catalog, add_skill="release-notes")
    doc = skill(catalog, current=doc, add_skill="incident-triage")
    assert [s["name"] for s in doc["skills"]] == ["incident-triage", "release-notes"]
    with pytest.raises(BindingRefused, match="^That skill is not approved for prod.$"):
        skill(catalog, environment="prod", add_skill="release-notes")
    with pytest.raises(BindingRefused, match="^That skill is for some groups only, and you are not in one.$"):
        skill(catalog, environment="prod", add_skill="finance-close")
    assert skill(catalog, environment="prod", groups=("finance-analysts",), add_skill="finance-close")["skills"][0]["name"] == "finance-close"
    with pytest.raises(BindingRefused, match="not approved for test"):
        skill(catalog, groups=("finance-analysts",), add_skill="finance-close")


@pytest.mark.parametrize("name, message", [("Incident-Triage", "^Choose a skill from the list.$"), ("", "from the list"), (3, "from the list"),
                                           ("nope", "^That skill is not in the catalog.$")])
def test_unknown_or_malformed_skills_are_refused(catalog, name, message):
    with pytest.raises(BindingRefused, match=message):
        skill(catalog, add_skill=name)


def test_the_catalog_is_needed(catalog):
    with pytest.raises(BindingRefused, match="^The skills catalog is not set up here.$"):
        skill(None, add_skill="incident-triage")


def test_a_duplicate_skill_points_at_update(catalog):
    doc = skill(catalog, add_skill="incident-triage")
    with pytest.raises(BindingRefused, match="^incident-triage is already on this app in test: use Update to move it to the catalog's current version.$"):
        skill(catalog, current=doc, add_skill="incident-triage")


def old_pin(catalog):
    doc = skill(catalog, add_skill="incident-triage")
    doc["skills"][0]["commit"] = OLD
    return doc


def test_update_moves_an_old_pin_to_main_and_refuses_one_at_main(catalog):
    updated = skill(catalog, current=old_pin(catalog), update_skill="incident-triage")
    assert updated["skills"] == [{"name": "incident-triage", "commit": MAIN, "verified_by": "mock-skills-catalog"}]
    with pytest.raises(BindingRefused, match="^incident-triage is already at the catalog's current version.$"):
        skill(catalog, current=updated, update_skill="incident-triage")
    with pytest.raises(BindingRefused, match="is not on this app in test"):
        skill(catalog, update_skill="release-notes")


def test_remove_a_skill(catalog):
    doc = skill(catalog, current=skill(catalog, add_skill="release-notes"), add_skill="incident-triage")
    after = next_binding(**{**COMMON, "current": doc}, remove_skill="release-notes")
    assert [s["name"] for s in after["skills"]] == ["incident-triage"]
    after = next_binding(**{**COMMON, "current": after}, remove_skill="incident-triage")
    assert after["skills"] == [] and validate("binding.v2", after) == []
    with pytest.raises(BindingRefused, match="^This environment has no skill by that name: there is nothing to remove.$"):
        next_binding(**{**COMMON, "current": after}, remove_skill="incident-triage")


def test_at_most_twenty_skills(tmp_path):
    doc = json.loads(catalog_module.MOCK.read_text())
    main = doc["commits"][doc["main"]]["skills"]
    for i in range(21):
        main[f"s{i:02d}"] = {"skill.yaml": f"schema: skill/v1\nname: s{i:02d}\ndescription: A skill.\nenvironments: [test]\naudience: everyone\n",
                             "SKILL.md": "# S\n"}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(doc))
    many = catalog_module.load_mock(path)
    current = None
    for i in range(20):
        current = skill(many, current=current, add_skill=f"s{i:02d}")
    with pytest.raises(BindingRefused, match="^An environment has at most 20 skills.$"):
        skill(many, current=current, add_skill="s20")


def test_skills_together_stay_under_512_kib(tmp_path):
    doc = json.loads(catalog_module.MOCK.read_text())
    main = doc["commits"][doc["main"]]["skills"]
    for name in ("big-a", "big-b", "big-c"):
        main[name] = {"skill.yaml": f"schema: skill/v1\nname: {name}\ndescription: Big.\nenvironments: [test]\naudience: everyone\n",
                      "SKILL.md": "x" * (200 * 1024)}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(doc))
    big = catalog_module.load_mock(path)
    current = skill(big, current=skill(big, add_skill="big-a"), add_skill="big-b")
    with pytest.raises(BindingRefused, match="^Together these skills are over 512 KiB, the most one environment can mount.$"):
        skill(big, current=current, add_skill="big-c")


def test_an_identity_keeps_mcp_and_skills_and_removing_it_keeps_them(cloudflare, catalog):
    doc = skill(catalog, current=add_mcp(cloudflare), add_skill="incident-triage")
    linked = make(current=doc, principal_id="sp-notes")
    assert linked["mcp"] == doc["mcp"] and linked["skills"] == doc["skills"]
    data = make(current=linked, warehouse_id="wh-notes")
    for after in (make(current=data, remove="azure-identity"), make(current=data, remove="databricks")):
        assert after["mcp"] == doc["mcp"] and after["skills"] == doc["skills"] and validate("binding.v2", after) == []
    emptied = next_binding(**{**COMMON, "current": copy.deepcopy(doc)}, remove_skill="incident-triage")
    assert emptied["mcp"] == doc["mcp"] and emptied["skills"] == []
    kept = make(current=emptied, principal_id="sp-notes")
    assert kept["skills"] == [] and kept["mcp"] == doc["mcp"]   # the key stays once the binding had it


# ---- a gateway the platform moved (review fixes, batch 4) ----

MOVED_FROM = {"gateway": "mcp-old.example.com", "servers": [{"server_id": "notes", "name": "Notes", "verified_by": "mock-cloudflare"},
                                                            {"server_id": "wiki", "name": "Wiki", "verified_by": "mock-cloudflare"}]}


def moved(cloudflare):
    return {**add_mcp(cloudflare), "mcp": copy.deepcopy(MOVED_FROM)}


def test_one_removal_clears_every_server_on_a_moved_gateway(cloudflare):
    doc = next_binding(**{**COMMON, "current": moved(cloudflare)}, remove_mcp="wiki", mcp_gateway="mcp-test.example.com")
    assert doc["mcp"] == {"gateway": "mcp-test.example.com", "servers": []} and validate("binding.v2", doc) == []
    with pytest.raises(BindingRefused, match="nothing to remove"):
        next_binding(**{**COMMON, "current": moved(cloudflare)}, remove_mcp="tickets", mcp_gateway="mcp-test.example.com")


def test_the_last_removal_records_the_environment_s_gateway_now(cloudflare):
    one = {**add_mcp(cloudflare), "mcp": {"gateway": "mcp-test.example.com", "servers": [MOVED_FROM["servers"][0]]}}
    doc = next_binding(**{**COMMON, "current": one}, remove_mcp="notes", mcp_gateway="mcp-test.example.com")
    assert doc["mcp"] == {"gateway": "mcp-test.example.com", "servers": []}
    emptied = {**doc, "mcp": {"gateway": "mcp-old.example.com", "servers": []}}
    assert make(current=emptied, principal_id="sp-notes", mcp_gateway="mcp-test.example.com")["mcp"]["gateway"] == "mcp-test.example.com"


@pytest.mark.parametrize("choice", [{"principal_id": "sp-notes"}, {"remove_skill": "x"}, {"add_skill": "incident-triage"},
                                    {"remove_secret": "X"}])
def test_other_changes_wait_until_the_moved_gateway_s_servers_are_removed(cloudflare, catalog, choice):
    with pytest.raises(BindingRefused, match="^The platform moved this environment's MCP gateway: remove its MCP servers first"):
        next_binding(**{**COMMON, "current": moved(cloudflare)}, mcp_gateway="mcp-test.example.com", skills_catalog=catalog, **choice)


def test_a_secret_waits_too(cloudflare):
    server = load_mock()
    with pytest.raises(BindingRefused, match="remove its MCP servers first"):
        add(server, current=moved(cloudflare), mcp_gateway="mcp-test.example.com")

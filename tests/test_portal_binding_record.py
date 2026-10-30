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

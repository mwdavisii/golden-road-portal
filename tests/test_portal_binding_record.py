"""The next binding/v1 document from what the app's admin picked (spec 2026-10-29 app bindings, B2/B5). Every choice is checked
against the directory on the server, whatever a form posts."""

import pytest
from portal_fakes import T0, fake_directory

from portal.binding_record import BindingRefused, next_binding
from portal.contracts import validate

P, R = "p_" + "a" * 32, "r_" + "b" * 32


def make(current=None, repo="acme/notes", **choice):
    return next_binding(current=current, repo=repo, org="acme", directory=fake_directory(), person_id=P, request_id=R, now=T0, **choice)


def test_linking_the_identity():
    doc = make(principal_id="sp-notes")
    assert doc["azure_identity"] == {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                                     "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"}
    assert "databricks" not in doc and validate("binding.v1", doc) == []
    assert (doc["schema"], doc["repo"], doc["linked_by"], doc["request"], doc["linked_at"]) == (
        "binding/v1", "acme/notes", P, R, "2026-10-23T12:00:00Z")


def test_the_repo_is_recorded_as_given_never_lowercased():
    doc = make(repo="acme/Notes", principal_id="sp-notes")   # tagged_repo_name matches case-insensitively, like GitHub
    assert doc["repo"] == "acme/Notes"


def test_then_databricks_keeps_the_identity():
    first = make(principal_id="sp-notes")
    doc = make(current=first, warehouse_id="wh-notes")
    assert doc["azure_identity"] == first["azure_identity"] and doc["databricks"] == {
        "directory_id": "wh-notes", "host": "adb-1111111111111111.1.azuredatabricks.net", "http_path": "/sql/1.0/warehouses/0000notes0000",
        "catalog": "notes", "schema": "public", "verified_by": "mock-directory"}
    assert validate("binding.v1", doc) == []


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
                       principal_id="sp-notes")
    assert doc["databricks"] == {"directory_id": "wh-notes", "host": "adb-3333333333333333.3.azuredatabricks.net",
                                 "http_path": "/sql/1.0/warehouses/0000moved0000", "catalog": "notes2", "verified_by": "mock-directory"}
    assert validate("binding.v1", doc) == []


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
                     principal_id="sp-notes")


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
    assert "azure_identity" not in doc and "databricks" not in doc and validate("binding.v1", doc) == []
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

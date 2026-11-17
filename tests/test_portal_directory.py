"""The mock directory (spec 2026-10-29 app bindings, B2/B3): what it offers, and that a bad file is refused."""

import copy
import json

import pytest
from portal_fakes import FAKE_DIRECTORY as DOC

from portal.directory import MOCK, DirectoryError, load_mock, parse


def test_only_principals_tagged_for_the_repo_are_offered():
    d = parse(DOC)
    assert [p.id for p in d.principals_for("acme", "acme/notes")] == ["sp-notes"]
    assert d.principals_for("acme", "acme/unknown") == []
    assert d.principals_for("other-org", "acme/notes") == []   # the org is the portal's, never the directory's


def test_only_warehouses_granted_to_the_principal_are_offered():
    d = parse(DOC)
    assert [w.id for w in d.warehouses_for("sp-notes")] == ["wh-notes"]
    assert d.warehouses_for("sp-nobody") == []


def test_lookups():
    d = parse(DOC)
    assert d.principal("sp-other").client_id.startswith("3333") and d.principal("nope") is None
    assert d.warehouse("wh-other").catalog is None and d.warehouse("nope") is None


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema="x"),
    lambda d: d.update(extra=1),
    lambda d: d.pop("principals"),
    lambda d: d.update(principals={}),
    lambda d: d["principals"][0].update(client_id="nope"),
    lambda d: d["principals"][0].update(client_id="11111111-1111-4111-8111-111111111111\n"),
    lambda d: d["principals"].append(dict(d["principals"][0])),                    # duplicate id
    lambda d: d["databricks"][0].update(id="sp-notes"),                            # an id twice across both lists
    lambda d: d["databricks"][0].update(host="evil.example.com"),
    lambda d: d["databricks"][0].update(http_path="/sql/1.0/warehouses/../x"),
    lambda d: d["databricks"][0].update(catalog="no spaces"),
    lambda d: d["databricks"][0].update(granted_to=["sp-missing"]),               # granted to no known principal
    lambda d: d["databricks"][0].update(granted_to=[]),
    lambda d: d["principals"][0].update(tagged_repo_name="acme/notes"),           # a name, never owner/name
    lambda d: d["principals"][0].update(display_name="x" * 201),
    lambda d: d["principals"][0].update(display_name="two\nlines"),
    lambda d: d["principals"][0].update(display_name="bell\x07"),                 # Cc
    lambda d: d["principals"][0].update(display_name="sp-notes\u202eppa"),        # Cf: a right-to-left override
    lambda d: d["databricks"][0].update(display_name="zero\u200bwidth"),          # Cf
    lambda d: d["principals"][1].update(client_id=d["principals"][0]["client_id"]),  # two principals, one client id
])
def test_a_bad_directory_is_refused(mutate):
    doc = copy.deepcopy(DOC)
    mutate(doc)
    with pytest.raises(DirectoryError):
        parse(doc)


def test_the_shipped_mock_loads_and_names_the_poc_apps():
    d = load_mock()
    assert {p.tagged_repo_name for p in d.principals} == {"hello-app", "golden-road-portal", "finance-reports"}
    assert json.loads(MOCK.read_text())["schema"] == "mock-directory/v1"
    assert all(p.client_id.count("-") == 4 for p in d.principals)
    assert [w.id for w in d.warehouses_for("sp-hello-app")] == ["wh-notes-readonly", "wh-platform-metrics"]


def test_an_unreadable_mock_is_a_directory_error(tmp_path, monkeypatch):
    import portal.directory
    monkeypatch.setattr(portal.directory, "MOCK", tmp_path / "missing.json")
    with pytest.raises(DirectoryError):
        load_mock()
    (tmp_path / "bad.json").write_text("{not json")
    monkeypatch.setattr(portal.directory, "MOCK", tmp_path / "bad.json")
    with pytest.raises(DirectoryError):
        load_mock()


def test_a_mock_with_a_duplicate_key_is_refused(tmp_path, monkeypatch):
    import portal.directory
    text = json.dumps(DOC).replace('"tenant_id": ', '"tenant_id": "00000000-0000-4000-8000-00000000a2e0", "tenant_id": ', 1)
    (tmp_path / "dup.json").write_text(text)
    monkeypatch.setattr(portal.directory, "MOCK", tmp_path / "dup.json")
    with pytest.raises(DirectoryError, match="twice"):
        load_mock()


def test_the_overlay_adds_the_store_s_principals_of_the_org_s_repos():
    import datetime as dt
    from portal.directory import overlay
    from portal.store.memory import MemoryStore
    from portal_fakes import CREATED_CLIENT_ID, CREATED_ID, T0, created_principal
    store = MemoryStore()
    owner = store.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=T0, max_age=dt.timedelta(hours=8))[0]
    created_principal(store, owner.id, T0)
    base = load_mock()
    before = (base.principals, base.warehouses)
    seen = overlay(base, store, "acme")
    [made] = [p for p in seen.principals_for("acme", "acme/notes") if p.id == CREATED_ID]
    assert (made.display_name, made.client_id, made.tagged_repo_name) == ("sp-notes-calendar", CREATED_CLIENT_ID, "notes")
    assert seen.principal(CREATED_ID) == made and seen.tenant_id == base.tenant_id and seen.name == base.name
    assert seen.warehouses == base.warehouses and seen.warehouses_for(CREATED_ID) == []   # a created principal has no warehouses
    assert all(made not in seen.principals_for("acme", f"acme/{p.tagged_repo_name}") for p in base.principals if p.tagged_repo_name != "notes")
    assert (base.principals, base.warehouses) == before and base.principal(CREATED_ID) is None   # the static directory is never changed
    assert overlay(base, store, "other-org").principals == base.principals   # another org's repo is never tagged here
    assert overlay(None, store, "acme") is None and overlay(base, None, "acme") is base


def test_the_overlay_never_lets_a_created_principal_shadow_a_listed_one():
    import dataclasses as dc
    from portal.directory import overlay
    from portal.store.base import ServicePrincipal
    from portal_fakes import T0
    base = load_mock()
    listed = base.principals[0]

    class Store:
        def service_principals(self, repo=None):
            return [ServicePrincipal(listed.id, "acme/notes", "sp-notes-x", "0e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f", ("Calendars.Read",), "r_" + "a" * 32, T0),
                    ServicePrincipal("sp-" + "d" * 24, "acme/notes", "sp-notes-y", listed.client_id, ("Calendars.Read",), "r_" + "b" * 32, T0)]
    assert overlay(base, Store(), "acme").principals == base.principals
    assert dc.is_dataclass(overlay(base, Store(), "acme"))


def test_the_overlay_logs_each_created_principal_it_skips_by_its_id_only(caplog):
    from portal.directory import overlay
    from portal.store.base import ServicePrincipal
    from portal_fakes import T0
    base = load_mock()
    listed = base.principals[0]
    clash = ServicePrincipal("sp-" + "d" * 24, "acme/notes", "sp-notes-y", listed.client_id, ("Calendars.Read",), "r_" + "b" * 32, T0)

    class Store:
        def service_principals(self, repo=None):
            return [clash]
    with caplog.at_level("WARNING", logger="portal.directory"):
        overlay(base, Store(), "acme")
    [record] = [r for r in caplog.records if r.name == "portal.directory"]
    assert record.levelname == "WARNING" and clash.id in record.getMessage()
    assert listed.client_id not in record.getMessage() and "sp-notes-y" not in record.getMessage()   # the skipped id only

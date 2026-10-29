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

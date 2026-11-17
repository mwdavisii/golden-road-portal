"""The scope catalog (spec 2026-11-16 S1): each Graph application permission an app may ask for, with its tier; a requestable one says
its risk. A bad file is refused whole."""

import copy
import json

import pytest

from portal.graph_scopes import MOCK, CatalogError, load_mock, parse

DOC = {"schema": "mock-graph-scopes/v1", "scopes": [
    {"id": "Calendars.Read", "label": "Read calendars", "description": "Read events in every calendar.", "tier": "approved"},
    {"id": "Calendars.ReadWrite", "label": "Read and write calendars", "description": "Change events in every calendar.",
     "tier": "requestable", "risk": "The app can change or delete anyone's meetings."}]}


def test_a_catalog_parses_into_its_two_tiers():
    catalog = parse(DOC)
    assert [s.id for s in catalog.approved] == ["Calendars.Read"] and [s.id for s in catalog.requestable] == ["Calendars.ReadWrite"]
    assert catalog.scope("Calendars.ReadWrite").risk == "The app can change or delete anyone's meetings."
    assert catalog.scope("Calendars.Read").risk is None and catalog.scope("Mail.Send") is None


@pytest.mark.parametrize("change", [
    lambda d: d.update(extra=1),
    lambda d: d.pop("scopes"),
    lambda d: d.update(schema="mock-graph-scopes/v2"),
    lambda d: d.update(scopes=[]),
    lambda d: d["scopes"][0].update(extra=1),
    lambda d: d["scopes"][0].pop("label"),
    lambda d: d["scopes"].append(dict(d["scopes"][0])),                       # a scope twice
    lambda d: d["scopes"].append({**d["scopes"][0], "id": "calendars.read"}),  # twice, in another case
    lambda d: d["scopes"][0].update(tier="pre-approved"),
    lambda d: d["scopes"][1].pop("risk"),                                     # requestable without its risk
    lambda d: d["scopes"][0].update(risk="Nothing."),                          # risk on an approved scope
    lambda d: d["scopes"][0].update(id="Calendars"),
    lambda d: d["scopes"][0].update(id="Calendars.Read^ORx"),
    lambda d: d["scopes"][0].update(label="two\nlines"),
    lambda d: d["scopes"][0].update(label=""),
    lambda d: d["scopes"][0].update(description="x" * 301),
    lambda d: d["scopes"][1].update(risk="‮override"),
])
def test_a_bad_catalog_is_refused(change):
    doc = copy.deepcopy(DOC)
    change(doc)
    with pytest.raises(CatalogError):
        parse(doc)


def test_the_mock_catalog_is_valid_and_has_both_tiers():
    catalog = load_mock()
    assert catalog.approved and catalog.requestable
    assert {"Calendars.Read", "Calendars.ReadWrite"} <= {s.id for s in catalog.scopes}
    assert all(s.risk for s in catalog.requestable)


def test_a_key_named_twice_is_refused(tmp_path, monkeypatch):
    import portal.graph_scopes
    path = tmp_path / "dup.json"
    path.write_text('{"schema": "mock-graph-scopes/v1", "schema": "x", "scopes": []}')
    monkeypatch.setattr(portal.graph_scopes, "MOCK", path)
    with pytest.raises(CatalogError, match="twice"):
        load_mock()


def test_an_unreadable_catalog_is_a_catalog_error(tmp_path, monkeypatch):
    import portal.graph_scopes
    monkeypatch.setattr(portal.graph_scopes, "MOCK", tmp_path / "missing.json")
    with pytest.raises(CatalogError):
        load_mock()


def test_the_mock_file_is_json():
    assert json.loads(MOCK.read_text())["schema"] == "mock-graph-scopes/v1"


def test_a_permission_may_start_with_a_lowercase_letter_as_graph_s_do():
    """eDiscovery.Read.All is a real Graph application permission: the first letter may be lowercase; each later part starts capital."""
    from portal.store.base import SCOPE_ID
    doc = copy.deepcopy(DOC)
    doc["scopes"].append({"id": "eDiscovery.Read.All", "label": "Read eDiscovery cases", "description": "Read every eDiscovery case.",
                          "tier": "approved"})
    assert parse(doc).scope("eDiscovery.Read.All").tier == "approved"
    assert SCOPE_ID.fullmatch("eDiscovery.Read.All") and not SCOPE_ID.fullmatch("eDiscovery.read.All")

import re

from portal.contracts import validate
from portal.ids import new_id


def test_ids_have_a_kind_and_32_hex_digits():
    for kind in ("p", "r", "l"):
        assert re.fullmatch(rf"{kind}_[0-9a-f]{{32}}", new_id(kind))
    assert new_id("p") != new_id("p")


def test_the_vendored_schemas_validate():
    assert validate("registration-draft.v1", {"schema": "registration-draft/v1", "repo": "acme/notes"}) == []
    assert validate("registration-draft.v1", {"schema": "registration-draft/v1", "repo": "acme"})


def test_a_date_time_that_is_not_a_real_instant_is_rejected():
    p = "p_" + "a" * 32
    intake = {"schema": "intake/v2", "repo": "acme/notes", "tier": "low", "purpose": "Stores short team notes.", "owner": p,
              "data_classification": "public", "registered_by": p, "approved_by": "p_" + "b" * 32, "approved_at": "2026-10-23T12:00:00Z",
              "approval_justification": "Purpose and classification checked.", "review_by": "2027-10-23", "request": "r_" + "c" * 32}
    assert validate("intake.v2", intake) == []
    for stamp in ("2026-99-99T99:99:99Z", "2026-02-30T12:00:00Z", "2026-10-23T24:00:00Z"):
        assert validate("intake.v2", {**intake, "approved_at": stamp}), stamp


def test_a_floor_override_time_that_is_not_a_real_instant_is_rejected():
    p = "p_" + "a" * 32
    intake = {"schema": "intake/v2", "repo": "acme/notes", "tier": "low", "purpose": "Stores short team notes.", "owner": p,
              "data_classification": "public", "registered_by": p, "approved_by": "p_" + "b" * 32, "approved_at": "2026-10-23T12:00:00Z",
              "approval_justification": "Purpose and classification checked.", "review_by": "2027-10-23", "request": "r_" + "c" * 32,
              "evidence_commit": "e" * 40, "floor_tier": "moderate", "floor_classification": "public",
              "floor_override": {"by": "p_" + "b" * 32, "at": "2026-10-23T12:00:00Z", "reason": "The spec over-declares egress; checked it."}}
    assert validate("intake.v2", intake) == []
    assert validate("intake.v2", {**intake, "floor_override": {**intake["floor_override"], "at": "2026-02-30T12:00:00Z"}})


def test_every_vendored_schema_has_its_file_name_as_its_id_and_its_references_resolve():
    import json
    from portal.contracts import SCHEMAS, validate
    for path in SCHEMAS.glob("*.json"):
        assert json.loads(path.read_text())["$id"].endswith("/" + path.name), path.name
    assert validate("intake.v1", {"schema": "intake/v1", "repo": "acme/notes", "tier": "low", "purpose": "Stores notes.",
                                  "owner": "platform-team", "data_classification": "public"}) == []


def test_a_reference_to_a_schema_that_is_not_vendored_fails_closed_without_a_lookup(monkeypatch):
    """scorecard/v1 and scorecard/v2 name schemas the portal doesn't vendor (assessment/v1, requirement-result/v1, ...). Only their
    $defs that re-review-event/v1 uses are reached; anything that reaches the others raises, and nothing is fetched."""
    import socket
    import pytest
    from jsonschema import Draft202012Validator
    from referencing.exceptions import Unresolvable
    from portal.contracts import _registry

    def no_network(*args, **kwargs):
        raise AssertionError("a schema reference reached for the network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    reached = "https://platform.internal/schemas/scorecard.v1.json#/properties/assessment/properties/model"  # it names assessment.v1.json
    assert _registry().resolver().lookup(reached).contents == {"anyOf": [{"$ref": "assessment.v1.json#/properties/model"}, {"type": "null"}]}
    with pytest.raises(Unresolvable):
        Draft202012Validator({"$ref": reached}, registry=_registry()).is_valid("gpt-x")

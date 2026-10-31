import copy

import pytest
from portal_fakes import RULES

from portal.floor import below_floor, compute_floor, effective, rules_problems


def ev(**kw):
    base = {"spec_present": True, "data_classification": "public", "egress": [], "secret_env": [], "write_roles": [],
            "dependencies": {"count": 3, "lockfile_present": True, "lockfile": "requirements.txt"}, "signals": []}
    return {**base, **kw}


def sig(*rules):
    return [{"rule": r, "line": "x"} for r in rules]


def test_the_rules_example_has_no_problems():
    assert rules_problems(RULES) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.pop("raise_to"), "registration-rules.v1"), (lambda d: d.update(extra=1), "registration-rules.v1"),
    (lambda d: d["classification_tier"].update(protected="low"), "must not decrease"),
    (lambda d: d["classification_tier"].update(public="moderate", confidential="low"), "must not decrease"),
])
def test_bad_rules_are_refused(mutate, needle):
    doc = copy.deepcopy(RULES)
    mutate(doc)
    assert any(needle in p for p in rules_problems(doc))


@pytest.mark.parametrize("evidence, floor", [
    (ev(), ("low", "public")),
    (ev(data_classification="confidential", signals=sig("write_roles")), ("high", "confidential")),
    (ev(data_classification="confidential"), ("high", "confidential")),
    (ev(data_classification="protected", signals=sig("write_roles")), ("high", "protected")),
    (ev(signals=sig("write_roles")), ("moderate", "public")),
    (ev(signals=sig("secret_env", "other_egress")), ("moderate", "public")),
    (ev(signals=sig("model_gateway_egress")), ("moderate", "public")),
    (ev(signals=sig("many_dependencies")), ("moderate", "public")),
    (ev(spec_present=False, data_classification=None, signals=sig("no_spec")), ("high", "confidential")),
    (ev(signals=sig("unreadable:requirements.txt")), ("high", "confidential")),
    (ev(data_classification="protected", signals=sig("unreadable:.builder.lock")), ("high", "protected")),
])
def test_the_floor_is_the_highest_of_the_classification_tier_every_raise_and_the_no_spec_level(evidence, floor):
    assert compute_floor(evidence, RULES) == floor


def test_a_raise_rule_set_to_low_does_not_raise():
    rules = copy.deepcopy(RULES)
    rules["raise_to"]["write_roles"] = "low"
    assert compute_floor(ev(signals=sig("write_roles")), rules) == ("low", "public")


def test_effective_is_the_higher_of_claim_and_floor():
    assert effective("low", "public", "moderate", "confidential") == ("moderate", "confidential")
    assert effective("high", "protected", "moderate", "confidential") == ("high", "protected")


def test_below_floor_names_what_is_below():
    assert below_floor("low", "public", "moderate", "public") == ["tier"]
    assert below_floor("high", "public", "moderate", "confidential") == ["data_classification"]
    assert below_floor("moderate", "public", "moderate", "public") == []
    assert below_floor("bogus", None, "low", "public") == ["tier", "data_classification"]  # unknown is below everything


def test_rules_that_name_a_retired_level_say_its_replacement():
    doc = copy.deepcopy(RULES)
    doc["no_spec"]["data_classification"] = "restricted"
    assert rules_problems(doc) == ["`restricted` is retired: use `confidential`"]

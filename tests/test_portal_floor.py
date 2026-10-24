import copy

import pytest
from portal_fakes import RULES

from portal.floor import below_floor, compute_floor, effective, rules_problems


def ev(**kw):
    base = {"spec_present": True, "data_classification": "internal", "egress": [], "secret_env": [], "write_roles": [],
            "dependencies": {"count": 3, "lockfile_present": True, "lockfile": "requirements.txt"}, "signals": []}
    return {**base, **kw}


def sig(*rules):
    return [{"rule": r, "line": "x"} for r in rules]


def test_the_rules_example_has_no_problems():
    assert rules_problems(RULES) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.pop("raise_to"), "registration-rules.v1"), (lambda d: d.update(extra=1), "registration-rules.v1"),
    (lambda d: d["classification_tier"].update(phi="low"), "must not decrease"),
    (lambda d: d["classification_tier"].update(internal="moderate", restricted="low"), "must not decrease"),
])
def test_bad_rules_are_refused(mutate, needle):
    doc = copy.deepcopy(RULES)
    mutate(doc)
    assert any(needle in p for p in rules_problems(doc))


@pytest.mark.parametrize("evidence, floor", [
    (ev(), ("low", "internal")),
    (ev(data_classification="public"), ("low", "public")),
    (ev(data_classification="restricted"), ("high", "restricted")),
    (ev(data_classification="phi", signals=sig("write_roles")), ("high", "phi")),
    (ev(signals=sig("write_roles")), ("moderate", "internal")),
    (ev(signals=sig("secret_env", "other_egress")), ("moderate", "internal")),
    (ev(signals=sig("model_gateway_egress")), ("moderate", "internal")),
    (ev(signals=sig("many_dependencies")), ("moderate", "internal")),
    (ev(spec_present=False, data_classification=None, signals=sig("no_spec")), ("high", "restricted")),
    (ev(signals=sig("unreadable:requirements.txt")), ("high", "restricted")),
    (ev(data_classification="phi", signals=sig("unreadable:.builder.lock")), ("high", "phi")),
])
def test_the_floor_is_the_highest_of_the_classification_tier_every_raise_and_the_no_spec_level(evidence, floor):
    assert compute_floor(evidence, RULES) == floor


def test_a_raise_rule_set_to_low_does_not_raise():
    rules = copy.deepcopy(RULES)
    rules["raise_to"]["write_roles"] = "low"
    assert compute_floor(ev(signals=sig("write_roles")), rules) == ("low", "internal")


def test_effective_is_the_higher_of_claim_and_floor():
    assert effective("low", "public", "moderate", "internal") == ("moderate", "internal")
    assert effective("high", "phi", "moderate", "internal") == ("high", "phi")


def test_below_floor_names_what_is_below():
    assert below_floor("low", "internal", "moderate", "internal") == ["tier"]
    assert below_floor("high", "public", "moderate", "internal") == ["data_classification"]
    assert below_floor("moderate", "internal", "moderate", "internal") == []
    assert below_floor("bogus", None, "low", "public") == ["tier", "data_classification"]  # unknown is below everything

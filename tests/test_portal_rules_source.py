import httpx
import pytest
import yaml
from portal_fakes import RULES, READ_PERMISSIONS, FakeGitHub, repo_at, transport

from portal.collectors.repo_files import Transient
from portal.collectors.rules import RulesProblem, load_rules, parse_rules
from portal.github.client import GitHub

API, FACTORY = "https://api.github.com", "acme/scorecard-factory"


def client(gh):
    return GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x")


def factory_with(entry):
    return repo_at(FakeGitHub(permissions=READ_PERMISSIONS), FACTORY, {} if entry is None else {"config/registration.yaml": entry})


def test_the_rules_are_read_from_the_factory_s_default_branch_at_one_commit():
    gh = factory_with(yaml.safe_dump(RULES).encode())
    assert load_rules(client(gh), "acme") == (RULES, "a" * 40)
    assert gh.called("GET", f"/repos/{FACTORY}/contents/config/registration.yaml")[0]["query"]["ref"] == "a" * 40


BAD = yaml.safe_dump(RULES).replace("phi: high", "phi: low").encode()
DUP = yaml.safe_dump(RULES).encode() + b"no_spec: {tier: low, data_classification: public}\n"


@pytest.mark.parametrize("entry, code", [
    (None, "rules_missing"), (BAD, "rules_invalid"), (DUP, "rules_invalid"), (b"!!python/object:builtins.object {}", "rules_invalid"),
    (b"#" * (70 * 1024), "rules_unreadable"), (403, "rules_unreadable"),
])
def test_missing_invalid_or_unreadable_rules_are_a_problem_with_a_code(entry, code):
    with pytest.raises(RulesProblem) as caught:
        load_rules(client(factory_with(entry)), "acme")
    assert caught.value.code == code


def test_a_transient_error_reading_the_rules_raises_transient():
    with pytest.raises(Transient):
        load_rules(client(factory_with(502)), "acme")


def test_parse_rules_takes_bytes_and_refuses_what_the_scorecard_refuses():
    assert parse_rules(yaml.safe_dump(RULES).encode()) == RULES
    with pytest.raises(RulesProblem):
        parse_rules(b"")

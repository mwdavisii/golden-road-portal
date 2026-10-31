"""The floor rules (spec E3): security's config/registration.yaml in the factory, read from the factory's default branch at one
commit. The loader accepts exactly what the scorecard's loader accepts (a test keeps the two in step): safe YAML, no duplicate
keys, no implicit timestamps, no anchors or aliases, then the registration-rules/v1 schema and the ordering of classification_tier."""

import yaml

from ..github.client import GitHubError
from ..floor import rules_problems
from .repo_files import UNREADABLE, head_commit, read_capped

PATH = "config/registration.yaml"
CAP = 65536


class RulesProblem(Exception):
    """The rules can't be used. `code` is one of rules_missing, rules_invalid, rules_unreadable (written by the portal, never GitHub's text)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class StrictLoader(yaml.SafeLoader):
    """SafeLoader without implicit timestamps, without duplicate keys, and without anchors or aliases (the scorecard's loader refuses
    them too): a few hundred bytes of aliases expand to a document no walk over it finishes, so they are refused before it is built."""

    def compose_node(self, parent, index):
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise yaml.YAMLError("anchors or aliases")
        return super().compose_node(parent, index)


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            if key in seen:
                raise yaml.YAMLError("duplicate key")
            seen.add(key)
        except TypeError:
            continue
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


StrictLoader.construct_mapping = _construct_mapping
StrictLoader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def parse_rules(raw: bytes) -> dict:
    if len(raw) > CAP:  # the scorecard's loader refuses the same size
        raise RulesProblem("rules_unreadable")
    try:
        doc = yaml.load(raw.decode("utf-8"), Loader=StrictLoader)  # noqa: S506 - StrictLoader is a SafeLoader
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        raise RulesProblem("rules_invalid") from None
    if rules_problems(doc):
        raise RulesProblem("rules_invalid")
    return doc


def load_rules(gh, org) -> tuple[dict, str]:
    """(rules, the factory commit they were read at). Raises RulesProblem, or Transient when GitHub can't answer for now."""
    factory = f"{org}/scorecard-factory"
    try:
        commit = head_commit(gh, factory)
    except GitHubError as exc:
        raise RulesProblem("rules_missing" if exc.status == 404 else "rules_unreadable") from None
    except ValueError:
        raise RulesProblem("rules_unreadable") from None
    raw = read_capped(gh, factory, PATH, commit, CAP)
    if raw is None:
        raise RulesProblem("rules_missing")
    if raw is UNREADABLE:
        raise RulesProblem("rules_unreadable")
    return parse_rules(raw), commit

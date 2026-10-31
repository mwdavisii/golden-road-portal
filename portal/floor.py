"""The floor (spec 2026-10-23 registration evidence): fixed rules from the factory turn the collector's evidence into a minimum tier
and classification. Only the collector computes it; web, store, database and writer compare against the stored floor."""

from .classification import LEVELS as CLASSIFICATIONS, retired_problems
from .contracts import validate

TIERS = ("low", "moderate", "high")
RAISES = ("write_roles", "secret_env", "model_gateway_egress", "other_egress", "many_dependencies")


def _rank(order, value) -> int:
    return order.index(value) if value in order else -1


def rules_problems(doc) -> list[str]:
    retired = retired_problems(doc)  # before the schema, so an old-level file names the replacement (spec 2026-10-30 C4)
    if retired:
        return retired
    problems = [f"not a valid registration-rules.v1: {p}" for p in validate("registration-rules.v1", doc)]
    if problems:
        return problems
    tiers = [_rank(TIERS, doc["classification_tier"][c]) for c in CLASSIFICATIONS]
    if tiers != sorted(tiers):
        problems.append("classification_tier must not decrease from public to protected")
    return problems


def compute_floor(evidence, rules) -> tuple[str, str]:
    fired = {s["rule"] for s in evidence["signals"]}
    spec = evidence["spec_present"]
    no_spec = not spec or any(r.startswith("unreadable:") or r == "no_spec" for r in fired)
    if spec:
        cls = evidence["data_classification"] if evidence["data_classification"] in CLASSIFICATIONS else CLASSIFICATIONS[-1]
        tiers = [rules["classification_tier"][cls]]
    else:  # a retired spec's evidence carries the level it maps to: the floor is never below it (spec 2026-10-30 C4)
        cls, tiers = rules["no_spec"]["data_classification"], []
        if evidence["data_classification"] in CLASSIFICATIONS:
            cls = max(cls, evidence["data_classification"], key=lambda c: _rank(CLASSIFICATIONS, c))
    tiers += [rules["raise_to"][r] for r in RAISES if r in fired]
    if no_spec:
        cls = max(cls, rules["no_spec"]["data_classification"], key=lambda c: _rank(CLASSIFICATIONS, c))
        tiers += [rules["no_spec"]["tier"], rules["classification_tier"][cls]]
    return max(tiers, key=lambda t: _rank(TIERS, t)), cls


def effective(claim_tier, claim_cls, floor_tier, floor_cls) -> tuple[str, str]:
    return (max((claim_tier, floor_tier), key=lambda t: _rank(TIERS, t)), max((claim_cls, floor_cls), key=lambda c: _rank(CLASSIFICATIONS, c)))


def below_floor(tier, cls, floor_tier, floor_cls) -> list[str]:
    return (["tier"] if _rank(TIERS, tier) < _rank(TIERS, floor_tier) else []) + (
        ["data_classification"] if _rank(CLASSIFICATIONS, cls) < _rank(CLASSIFICATIONS, floor_cls) else [])

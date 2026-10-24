import datetime as dt
import json

import httpx
import pytest
import yaml
from portal_fakes import RULES, READ_PERMISSIONS, T0, FakeGitHub, repo_at, transport

from portal.collectors.evidence import collect_evidence
from portal.github.client import GitHub
from portal.store.base import Conflict
from portal.store.memory import MemoryStore

API, GW = "https://api.github.com", ("litellm.internal",)
SPEC = {"app_name": "notes", "app_type": "python", "entrypoint": "app.main:app", "port": 8000, "data_classification": "internal",
        "egress": [], "env": [], "resources_tier": "default"}
RULES_BYTES = yaml.safe_dump(RULES).encode()
DRAFT = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "SENTINEL-PURPOSE stores short notes.",
         "data_classification": "internal", "proposed_tier": "low"}


def with_spec(**changes):
    return json.dumps({**SPEC, **changes}).encode()


def world(*, rules=RULES_BYTES, spec=None, factory=True):
    gh = FakeGitHub(permissions=READ_PERMISSIONS)
    repo_at(gh, "acme/notes", {"builder-spec.json": spec or with_spec(), "requirements.txt": b"fastapi==0.1\n"})
    if factory:
        repo_at(gh, "acme/scorecard-factory", {} if rules is None else {"config/registration.yaml": rules})
    store = MemoryStore()
    owner, _, _ = store.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=T0, max_age=dt.timedelta(hours=8))
    admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=["g"], roles=["admin"], now=T0,
                                max_age=dt.timedelta(hours=8))
    req = store.create_registration(person_id=owner.id, draft=DRAFT, now=T0)
    return gh, store, req, owner, admin


def run(gh, store):
    return collect_evidence(GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x"), store, org="acme", gateway_hosts=GW, now=T0)


def test_evidence_is_computed_once_per_request_at_one_commit():
    gh, store, req, *_ = world()
    assert run(gh, store) == {"computed": 1, "waiting": 0}
    calls = len(gh.calls)
    assert run(gh, store) == {"computed": 0, "waiting": 0} and len(gh.calls) == calls  # nothing waits: no GitHub call at all
    found = store.evidence_for(req.id, 1)
    assert found.commit == "a" * 40 and found.rules_commit == "a" * 40 and found.evidence["repo"] == "acme/notes"
    assert (found.floor_tier, found.floor_classification) == ("low", "internal")


@pytest.mark.parametrize("rules, code", [(None, "rules_missing"), (yaml.safe_dump(RULES).replace("phi: high", "phi: low").encode(), "rules_invalid")])
def test_without_valid_rules_the_request_waits_with_a_reason(rules, code):
    gh, store, req, *_ = world(rules=rules)
    assert run(gh, store) == {"computed": 0, "waiting": 1, "problem": code}
    assert run(gh, store)["problem"] == code
    assert store.evidence_problem_for(req.id, 1) == code and store.evidence_for(req.id, 1) is None
    assert len(store._problems) == 1  # written once, not once per pass


def test_a_resubmitted_request_gets_new_evidence():
    gh, store, req, owner, admin = world()
    gh.on("GET", "/repos/acme/notes/git/ref/heads/main", (200, {"object": {"sha": "b" * 40}}))
    tree = "/repos/acme/notes/git/trees/"
    gh.routes[("GET", "api.github.com", tree + "b" * 40)] = gh.routes[("GET", "api.github.com", tree + "a" * 40)]
    assert run(gh, store)["computed"] == 1
    store.decide(request_id=req.id, actor_id=admin.id, actor_groups=["g"], approve=False, note="Say what it stores.", final=None, now=T0)
    store.resubmit(request_id=req.id, person_id=owner.id, draft=DRAFT, now=T0)
    assert run(gh, store)["computed"] == 1
    assert store.evidence_for(req.id, 1).commit == "a" * 40 and store.evidence_for(req.id, 2).commit == "b" * 40


@pytest.mark.parametrize("status", [404, 403])
def test_a_repo_the_read_app_cannot_see_waits_with_repo_unreadable(status):
    gh, store, req, *_ = world()
    gh.routes[("GET", "api.github.com", "/repos/acme/notes")] = [(status, {"message": "no"})]
    assert run(gh, store) == {"computed": 0, "waiting": 1, "problem": "repo_unreadable"}
    assert store.evidence_problem_for(req.id, 1) == "repo_unreadable"


@pytest.mark.parametrize("broken", ["/repos/acme/scorecard-factory/contents/config/registration.yaml", "/repos/acme/notes/contents/builder-spec.json"])
def test_a_transient_error_writes_nothing_and_is_retried(broken):
    gh, store, req, *_ = world()
    good = gh.routes[("GET", "api.github.com", broken)]
    gh.routes[("GET", "api.github.com", broken)] = [(502, {"message": "bad gateway"})]
    first = run(gh, store)
    assert first["computed"] == 0 and first["transient"] is True
    assert store.evidence_for(req.id, 1) is None and store.evidence_problem_for(req.id, 1) is None
    gh.routes[("GET", "api.github.com", broken)] = good
    assert run(gh, store)["computed"] == 1


class NoDraft:
    """A store that fails the test when the pass reads a request (the collector has no grant on the draft)."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        if name in ("get_request", "requests_in", "requests_by"):
            raise AssertionError(f"the collector read requests through {name}")
        return getattr(self.inner, name)


def test_the_collector_never_reads_the_draft():
    gh, store, req, *_ = world()
    assert run(gh, NoDraft(store))["computed"] == 1
    assert "SENTINEL-PURPOSE" not in json.dumps(store.evidence_for(req.id, 1).evidence)


def test_the_collector_needs_only_metadata_and_contents_read():
    gh, store, *_ = world()
    assert gh.permissions == READ_PERMISSIONS and run(gh, store)["computed"] == 1  # any other call would be a 403 from the fake


def test_the_floor_is_stored_with_the_evidence():
    gh, store, req, *_ = world(spec=with_spec(data_classification="restricted"))
    run(gh, store)
    found = store.evidence_for(req.id, 1)
    assert (found.floor_tier, found.floor_classification) == ("high", "restricted")


def test_a_request_moved_meanwhile_is_skipped(monkeypatch):
    gh, store, req, *_ = world()

    def moved(**kw):
        raise Conflict("not_awaiting")
    monkeypatch.setattr(store, "add_evidence", moved)
    assert run(gh, store) == {"computed": 0, "waiting": 1}


RATE_LIMITED = (403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0"})


@pytest.mark.parametrize("broken", ["/repos/acme/scorecard-factory/contents/config/registration.yaml", "/repos/acme/notes/contents/builder-spec.json",
                                    "/repos/acme/notes", "/repos/acme/notes/git/ref/heads/main"])
def test_a_rate_limit_403_writes_nothing_and_is_retried_next_pass(broken):
    gh, store, req, *_ = world()
    good = gh.routes[("GET", "api.github.com", broken)]
    gh.routes[("GET", "api.github.com", broken)] = [RATE_LIMITED]
    first = run(gh, store)
    assert first["computed"] == 0 and first["transient"] is True and "problem" not in first
    assert store.evidence_for(req.id, 1) is None and store.evidence_problem_for(req.id, 1) is None
    gh.routes[("GET", "api.github.com", broken)] = good
    assert run(gh, store)["computed"] == 1


def test_a_plain_403_on_a_file_is_unreadable_evidence_not_a_retry():
    gh, store, req, *_ = world()
    gh.routes[("GET", "api.github.com", "/repos/acme/notes/contents/builder-spec.json")] = [(403, {"message": "no"}, {"x-ratelimit-remaining": "40"})]
    assert run(gh, store)["computed"] == 1
    found = store.evidence_for(req.id, 1)
    assert [s["rule"] for s in found.evidence["signals"]] == ["unreadable:builder-spec.json"] and found.floor_tier == "high"


def test_a_malformed_answer_skips_that_request_and_the_pass_goes_on():
    gh, store, req, owner, _ = world()
    repo_at(gh, "acme/tools", {"builder-spec.json": with_spec()})
    gh.routes[("GET", "api.github.com", "/repos/acme/notes/git/ref/heads/main")] = [(200, {"object": None})]
    store.replace_repo_admins([], observed_at=T0)
    other = store.create_registration(person_id=owner.id, now=T0, draft={**DRAFT, "repo": "acme/tools"})
    result = run(gh, store)
    assert result == {"computed": 1, "waiting": 1, "malformed": 1}
    assert store.evidence_for(req.id, 1) is None and store.evidence_for(other.id, 1) is not None

"""The agents part (spec 2026-11-17; decided while planning: item 29)."""

import json

import httpx
import pytest
from portal_fakes import READ_PERMISSIONS, T0, FakeGitHub, repo_at, transport

from portal.collectors.agents import agent_yaml_problem, collect_agents
from portal.collectors.repo_files import Transient
from portal.github.client import GitHub
from portal.store.base import AgentObserved
from portal.store.memory import MemoryStore

API = "https://api.github.com"
SPEC = {"app_name": "golden-road-agent-template", "app_type": "agent", "entrypoint": "agent.yaml", "port": 8080, "health_path": "/healthz",
        "data_classification": "confidential", "egress": [], "env": [], "resources_tier": "default"}
AGENT = b"schema: agent/v1\ndisplay_name: Calendar helper\nmodel: chat-default\ninstructions: Help.\n"


def world(repos):
    gh = FakeGitHub(permissions=READ_PERMISSIONS)
    gh.on("GET", "/installation/repositories", (200, {"total_count": len(repos), "repositories": [{"full_name": r} for r in repos]}))
    for repo, files in repos.items():
        repo_at(gh, repo, files)
    return gh


def client(gh):
    return GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x")


def run(gh, store):
    return collect_agents(client(gh), store, now=T0)


def test_only_agent_repos_are_kept_each_with_its_agent_or_its_problem():
    store = MemoryStore()
    gh = world({"acme/agent-calendar": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT},
                "acme/agent-broken": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": b"schema: agent/v2\n"},
                "acme/agent-empty": {"builder-spec.json": json.dumps(SPEC).encode()},
                "acme/notes": {"builder-spec.json": json.dumps({**SPEC, "app_type": "python"}).encode()},
                "acme/no-spec": {}})
    assert run(gh, store) == {"agents": 3, "problems": 2}
    found = {o.repo: (o.agent, o.problem) for o in store.agent_observed()}
    assert found == {"acme/agent-broken": (None, "agent_yaml_invalid"), "acme/agent-calendar": ({"schema": "agent/v1", "display_name": "Calendar helper",
                     "model": "chat-default", "instructions": "Help."}, None), "acme/agent-empty": (None, "agent_yaml_missing")}
    assert {o.commit for o in store.agent_observed()} == {"a" * 40}


def test_a_pass_replaces_every_row():
    store = MemoryStore()
    store.replace_agent_observed([AgentObserved("acme/agent-gone", "a" * 40, None, "agent_yaml_missing", T0)], observed_at=T0)
    gh = world({"acme/agent-calendar": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT}})
    run(gh, store)
    assert [o.repo for o in store.agent_observed()] == ["acme/agent-calendar"]


def test_a_pass_github_can_t_answer_changes_nothing():
    store = MemoryStore()
    store.replace_agent_observed([AgentObserved("acme/agent-calendar", "a" * 40, None, "agent_yaml_missing", T0)], observed_at=T0)
    gh = world({"acme/agent-calendar": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT}})
    gh.routes[("GET", "api.github.com", "/repos/acme/agent-calendar")] = [(502, {"message": "busy"})]
    with pytest.raises(Transient):
        run(gh, store)
    assert store.agent_observed()[0].problem == "agent_yaml_missing"


def test_a_busy_agent_yaml_read_changes_nothing_either():
    store = MemoryStore()
    store.replace_agent_observed([AgentObserved("acme/agent-calendar", "a" * 40, None, "agent_yaml_missing", T0)], observed_at=T0)
    gh = world({"acme/agent-calendar": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT}})
    gh.routes[("GET", "api.github.com", "/repos/acme/agent-calendar/contents/agent.yaml")] = [(503, {"message": "busy"})]
    with pytest.raises(Transient):
        run(gh, store)
    assert store.agent_observed()[0].problem == "agent_yaml_missing"


def test_a_repo_the_read_app_can_t_read_is_left_out():
    store = MemoryStore()
    gh = world({"acme/agent-calendar": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT}, "acme/private": {}})
    gh.routes[("GET", "api.github.com", "/repos/acme/private")] = [(404, {"message": "Not Found"})]
    assert run(gh, store) == {"agents": 1, "problems": 0}


def test_one_repo_s_odd_answers_never_stop_the_others():
    store = MemoryStore()
    gh = world({"acme/agent-odd": {"builder-spec.json": json.dumps(SPEC).encode()},
                "acme/agent-calendar": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT},
                "acme/agent-tree": {"builder-spec.json": json.dumps(SPEC).encode()}})
    gh.routes[("GET", "api.github.com", "/repos/acme/agent-odd/git/ref/heads/main")] = [(200, {"object": "not a dict"})]
    gh.routes[("GET", "api.github.com", "/repos/acme/agent-tree/git/trees/" + "a" * 40)] = [(200, {"tree": "not a list"})]
    assert run(gh, store) == {"agents": 1, "problems": 0}


def test_a_spec_that_isn_t_json_or_a_mapping_is_not_an_agent_repo():
    store = MemoryStore()
    gh = world({"acme/a": {"builder-spec.json": b"{not json"}, "acme/b": {"builder-spec.json": b"[1]"},
                "acme/c": {"builder-spec.json": b"\xff\xfe"}})
    assert run(gh, store) == {"agents": 0, "problems": 0}


def test_every_page_of_the_installation_s_repositories_is_read_and_odd_names_are_skipped():
    store = MemoryStore()
    gh = world({"acme/agent-calendar": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT},
                "acme/agent-two": {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT}})
    gh.routes[("GET", "api.github.com", "/installation/repositories")] = [
        (200, {"total_count": 4, "repositories": [{"full_name": "acme/agent-calendar"}, {"full_name": "../x"}, "odd"]},
         {"link": f'<{API}/installation/repositories?page=2>; rel="next"'}),
        (200, {"total_count": 4, "repositories": [{"full_name": "acme/agent-two"}, {"full_name": "ACME/agent-two"}]})]
    assert run(gh, store) == {"agents": 2, "problems": 0}
    assert [o.repo for o in store.agent_observed()] == ["acme/agent-calendar", "acme/agent-two"]


def test_an_agent_yaml_that_is_a_symlink_is_invalid():
    store = MemoryStore()
    gh = FakeGitHub(permissions=READ_PERMISSIONS)
    gh.on("GET", "/installation/repositories", (200, {"repositories": [{"full_name": "acme/agent-calendar"}]}))
    repo_at(gh, "acme/agent-calendar", {"builder-spec.json": json.dumps(SPEC).encode(), "agent.yaml": AGENT}, modes={"agent.yaml": "120000"})
    assert run(gh, store) == {"agents": 1, "problems": 1}
    assert store.agent_observed()[0].problem == "agent_yaml_invalid"


def test_an_agent_yaml_naming_a_model_the_platform_lacks_is_invalid():
    gh = world({"acme/agent-calendar": {"agent.yaml": AGENT.replace(b"chat-default", b"other")}})
    assert agent_yaml_problem(client(gh), "acme/agent-calendar", "a" * 40) == "agent_yaml_invalid"


def test_a_valid_agent_yaml_has_no_problem():
    gh = world({"acme/agent-calendar": {"agent.yaml": AGENT}})
    assert agent_yaml_problem(client(gh), "acme/agent-calendar", "a" * 40) is None

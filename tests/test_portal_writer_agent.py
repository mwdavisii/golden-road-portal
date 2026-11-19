"""The agent PR (spec 2026-11-17 "Portal changes"; decided while planning: items 26 to 28)."""

import json
import uuid

import httpx
import pytest
from portal_fakes import RepoWorld, agent_app, transport, writer_config

from portal.agent_definition import BRANCH, agent_yaml
from portal.github.client import GitHub
from portal.store.base import Conflict
from portal.writer.run import run_once

SPEC = json.dumps({"app_name": "golden-road-agent-template", "app_type": "agent", "entrypoint": "agent.yaml", "port": 8080,
                   "health_path": "/healthz", "data_classification": "confidential", "egress": [], "env": [], "resources_tier": "default"}).encode()
OLD = b"schema: agent/v1\ndisplay_name: My agent\nmodel: chat-default\ninstructions: Help.\n"
DRAFT = {"schema": "agent/v1", "display_name": "Calendar helper", "model": "chat-default", "instructions": "Find free time.", "starters": []}
PERMISSIONS = {"contents": "write", "pull_requests": "write"}


@pytest.fixture
def agents(portal):
    world = RepoWorld()
    world.add_repo("acme/notes", {"builder-spec.json": SPEC, "agent.yaml": OLD})
    world.agent_installed["acme/notes"] = 77
    cfg = writer_config(agent_app=agent_app(world))
    return portal, world, cfg


def save(portal, draft=DRAFT, minutes=1):
    portal.clock.advance(minutes=minutes)
    return portal.store.create_agent_definition(request_id="r_" + uuid.uuid4().hex, person_id=portal.people["owner"].id, repo="acme/notes",
                                                draft=draft, now=portal.clock())


def pass_(portal, cfg, world):
    return run_once(portal.store, GitHub("https://api.github.com", httpx.Client(transport=transport(world)), lambda: "ghs_" + "w" * 36), cfg,
                    portal.clock())


def test_a_save_opens_one_pr_with_agent_yaml_alone_from_the_agent_app(agents):
    portal, world, cfg = agents
    req = save(portal)
    assert pass_(portal, cfg, world) == [f"{req.id}: pull request #1 opened"]
    found = portal.store.get_request(req.id)
    assert (found.state, found.branch, found.pr_number) == ("pr_open", BRANCH, 1)
    assert found.head_sha == world.head("acme/notes", BRANCH) and found.base_sha == world.head("acme/notes")
    assert world.files("acme/notes", BRANCH)["agent.yaml"] == agent_yaml(DRAFT, req.id).encode()
    assert world._diff("acme/notes", world.pulls[("acme/notes", 1)]) == [{"filename": "agent.yaml", "status": "modified"}]
    pull = world.pull("acme/notes", 1)
    assert pull["user"]["login"] == "golden-road-portal-agent[bot]" and pull["title"] == f"Define the agent for acme/notes (portal request {req.id})"
    assert "Find free time." in pull["body"]
    assert world.agent_token_requests == [(77, {"repositories": ["notes"], "permissions": PERMISSIONS})]
    assert world.agent_token in world.revoked
    assert not world.token_requests, "never the lifecycle App"


def test_without_an_agent_yaml_on_main_the_pr_adds_it(agents):
    portal, world, cfg = agents
    world.add_repo("acme/notes", {"builder-spec.json": SPEC})
    req = save(portal)
    assert pass_(portal, cfg, world) == [f"{req.id}: pull request #1 opened"]
    assert world._diff("acme/notes", world.pulls[("acme/notes", 1)]) == [{"filename": "agent.yaml", "status": "added"}]


def test_a_second_save_updates_the_same_pr_and_supersedes_the_first(agents):
    portal, world, cfg = agents
    first = save(portal)
    pass_(portal, cfg, world)
    second = save(portal, {**DRAFT, "instructions": "Find free time, briefly."})
    out = pass_(portal, cfg, world)
    assert f"{second.id}: pull request #1 updated" in out
    assert portal.store.get_request(first.id).failure_reason == "superseded" and portal.store.get_request(second.id).state == "pr_open"
    assert world.files("acme/notes", BRANCH)["agent.yaml"] == agent_yaml({**DRAFT, "instructions": "Find free time, briefly."}, second.id).encode()
    assert len(world.pulls) == 1 and world.pull("acme/notes", 1)["title"].endswith(f"(portal request {second.id})")
    assert world.commits[world.head("acme/notes", BRANCH)]["parent"] == portal.store.get_request(first.id).head_sha  # in place


def test_an_older_save_still_waiting_is_superseded_without_writing(agents):
    portal, world, cfg = agents
    first, second = save(portal), save(portal, {**DRAFT, "display_name": "Second"})
    assert pass_(portal, cfg, world) == [f"{first.id}: failed (superseded)", f"{second.id}: pull request #1 opened"]  # never claimed
    assert portal.store.get_request(first.id).failure_reason == "superseded"
    assert len(world.agent_token_requests) == 1


def test_opening_a_pr_never_touches_a_newer_definition(agents, monkeypatch):
    """Only older open definitions are superseded (by created_at, then id): a newer one saved meanwhile keeps its turn."""
    portal, world, cfg = agents
    first = save(portal)
    newer = []
    original = portal.store.mark_pr_open

    def and_a_newer_save(**kw):
        found = original(**kw)
        if not newer:
            newer.append(save(portal, {**DRAFT, "display_name": "Newer"}))
        return found

    monkeypatch.setattr(portal.store, "mark_pr_open", and_a_newer_save)
    out = pass_(portal, cfg, world)
    assert out[0] == f"{first.id}: pull request #1 opened"  # the newer one, saved after, is never failed by the older's move
    later = portal.store.get_request(newer[0].id)
    assert later.state == "pr_open" and later.failure_reason is None
    assert portal.store.get_request(first.id).failure_reason == "superseded"  # the newer one's own turn supersedes the older


def test_a_store_that_refuses_the_move_for_a_newer_definition_supersedes_it(agents, monkeypatch):
    portal, world, cfg = agents
    req = save(portal)
    original, newer = portal.store.mark_pr_open, []

    def a_newer_save_first(**kw):
        if not newer:
            newer.append(save(portal, {**DRAFT, "display_name": "Newer"}))
            with pytest.raises(Conflict, match="newer_definition"):
                original(**kw)
        return original(**kw)

    monkeypatch.setattr(portal.store, "mark_pr_open", a_newer_save_first)
    out = pass_(portal, cfg, world)
    assert out[0] == f"{req.id}: failed (superseded)" and out[1] == f"{newer[0].id}: pull request #1 updated"
    assert world.agent_token in world.revoked


def test_the_person_merges_and_the_writer_records_it(agents):
    portal, world, cfg = agents
    req = save(portal)
    pass_(portal, cfg, world)
    assert pass_(portal, cfg, world) == [f"{req.id}: waiting for review"]
    sha = world.merge("acme/notes", 1)
    assert pass_(portal, cfg, world) == [f"{req.id}: merged"]
    assert portal.store.get_request(req.id).merge_sha == sha
    assert not [c for c in world.calls if c["path"].endswith("/merge")], "the writer never merges an agent PR"


def test_a_pr_closed_unmerged_fails_and_the_next_save_starts_a_fresh_branch(agents):
    portal, world, cfg = agents
    first = save(portal)
    pass_(portal, cfg, world)
    world.pull("acme/notes", 1)["state"] = "closed"
    assert pass_(portal, cfg, world) == [f"{first.id}: failed (pr_closed)"]
    second = save(portal)
    assert pass_(portal, cfg, world) == [f"{second.id}: pull request #2 opened"]
    assert world.commits[world.head("acme/notes", BRANCH)]["parent"] == world.head("acme/notes")  # one commit on main's head


def test_a_merged_pr_s_newer_save_opens_a_new_pr_and_the_merge_is_still_recorded(agents):
    portal, world, cfg = agents
    first = save(portal)
    pass_(portal, cfg, world)
    world.merge("acme/notes", 1)
    second = save(portal, {**DRAFT, "display_name": "Second"})
    out = pass_(portal, cfg, world)
    assert f"{second.id}: pull request #2 opened" in out and f"{first.id}: merged" in out


def test_without_the_agent_app_it_waits_every_pass_with_no_cap(agents):
    portal, world, _ = agents
    cfg = writer_config()
    req = save(portal)
    for _ in range(5):
        pass_(portal, cfg, world)
    found = portal.store.get_request(req.id)
    assert (found.state, found.attempts, found.last_error) == ("writing", 5, "no_agent_app: PORTAL_AGENT_APP_ID is not set")


def test_a_github_failure_waits_too(agents):
    portal, world, cfg = agents
    del world.agent_installed["acme/notes"]  # the App isn't installed on the repo yet: a 404, retried
    req = save(portal)
    pass_(portal, cfg, world)
    found = portal.store.get_request(req.id)
    assert (found.state, found.attempts, found.last_error) == ("writing", 1, "github: 404 GET /repos/acme/notes/installation")
    world.agent_installed["acme/notes"] = 77
    assert pass_(portal, cfg, world) == [f"{req.id}: pull request #1 opened"]


def test_a_token_wider_than_asked_is_revoked_unused_and_the_request_waits(agents):
    portal, world, cfg = agents
    world.agent_token_permissions = {**PERMISSIONS, "administration": "write"}
    req = save(portal)
    pass_(portal, cfg, world)
    assert portal.store.get_request(req.id).last_error == "github: unexpected response" and world.agent_token in world.revoked
    assert not [c for c in world.calls if c["auth"] == f"Bearer {world.agent_token}" and c["method"] != "DELETE"]


def test_a_token_whose_installation_lists_other_repositories_is_not_used(agents):
    portal, world, cfg = agents
    req = save(portal)
    world.agent_installed["acme/other"] = 77
    world.add_repo("acme/other", {})
    original = world.handle

    def wide(request):
        if request.url.path == "/installation/repositories":
            world.agent_token_repos = ["acme/notes", "acme/other"]
        return original(request)

    world.handle = wide
    pass_(portal, cfg, world)
    assert portal.store.get_request(req.id).last_error == "github: unexpected response" and not world.pulls


@pytest.mark.parametrize("change, reason", [
    (lambda world: world.add_repo("acme/notes", {"builder-spec.json": SPEC.replace(b'"agent"', b'"python"'), "agent.yaml": OLD}), "not_an_agent_repo"),
    (lambda world: world.add_repo("acme/notes", {"agent.yaml": OLD}), "not_an_agent_repo"),
    (lambda world: world.add_repo("acme/notes", {"builder-spec.json": SPEC, "agent.yaml": agent_yaml(DRAFT, "r_x").encode()}), None),
])
def test_a_refusal_fails_with_its_reason(agents, change, reason):
    portal, world, cfg = agents
    change(world)
    req = save(portal)
    if reason is None:  # main already holds exactly this file
        world.add_repo("acme/notes", {"builder-spec.json": SPEC, "agent.yaml": agent_yaml(DRAFT, req.id).encode()})
        reason = "already_on_main"
    assert pass_(portal, cfg, world) == [f"{req.id}: failed ({reason})"]
    assert not world.pulls


def test_an_open_pr_from_the_branch_that_isn_t_the_agent_app_s_is_refused(agents):
    portal, world, cfg = agents
    world.push("acme/notes", "main", "README.md", b"# notes\n")
    theirs = world.head("acme/notes")
    world.repos["acme/notes"]["main"] = world.commits[theirs]["parent"]  # main stays behind: the branch is at a commit of its own
    world.repos["acme/notes"][BRANCH] = theirs
    world.pulls[("acme/notes", 1)] = {"number": 1, "html_url": "https://github.com/acme/notes/pull/1", "state": "open", "merged": False,
                                      "merged_at": None, "merge_commit_sha": None, "title": "x", "body": "", "head": {"ref": BRANCH},
                                      "base_sha": world.head("acme/notes"), "user": {"login": "someone", "type": "User"}}
    req = save(portal)
    assert pass_(portal, cfg, world) == [f"{req.id}: failed (pr_not_ours)"]
    assert world.repos["acme/notes"][BRANCH] == theirs != world.head("acme/notes"), "someone's branch is left as it is"
    assert not [c for c in world.calls if c["method"] == "DELETE" and "/git/refs" in c["path"]]


def test_a_platform_repo_is_refused_before_github_is_asked(agents):
    portal, world, cfg = agents
    from portal.store.base import RepoAdmin
    portal.store.replace_repo_admins([RepoAdmin("acme/golden-road-agent-template", "owner-gh", 1001, portal.clock())], observed_at=portal.clock())
    portal.clock.advance(minutes=1)
    req = portal.store.create_agent_definition(request_id="r_" + uuid.uuid4().hex, person_id=portal.people["owner"].id,
                                               repo="acme/golden-road-agent-template", draft=DRAFT, now=portal.clock())
    assert pass_(portal, cfg, world) == [f"{req.id}: failed (platform_repo)"] and not world.calls


def test_a_requester_who_is_no_longer_the_repo_s_admin_is_refused(agents):
    portal, world, cfg = agents
    req = save(portal)
    portal.clock.advance(minutes=30)  # the collector's last sight of the admins is too old (PORTAL_REPO_ADMINS_STALE_AFTER)
    assert pass_(portal, cfg, world) == [f"{req.id}: failed (requester_not_admin)"]


def test_a_model_the_platform_lacks_is_refused(agents):
    portal, world, cfg = agents
    req = save(portal, {**DRAFT, "model": "other-model"})
    assert pass_(portal, cfg, world) == [f"{req.id}: failed (agent_yaml_invalid)"]


def test_a_merge_of_other_content_is_not_recorded_as_this_request_s(agents):
    portal, world, cfg = agents
    req = save(portal)
    pass_(portal, cfg, world)
    world.push("acme/notes", BRANCH, "agent.yaml", OLD)
    world.merge("acme/notes", 1)
    assert pass_(portal, cfg, world) == [f"{req.id}: failed (merged_file_differs)"]


def test_a_github_failure_while_waiting_for_review_waits_with_no_cap(agents):
    portal, world, cfg = agents
    req = save(portal)
    pass_(portal, cfg, world)
    del world.agent_installed["acme/notes"]
    for _ in range(3):
        pass_(portal, cfg, world)
    found = portal.store.get_request(req.id)
    assert (found.state, found.attempts) == ("pr_open", 3)
    world.agent_installed["acme/notes"] = 77
    world.merge("acme/notes", 1)
    assert pass_(portal, cfg, world) == [f"{req.id}: merged"]


def test_an_older_save_is_superseded_even_while_the_newer_one_waits(agents):
    portal, world, _ = agents
    first, second = save(portal), save(portal, {**DRAFT, "display_name": "Second"})
    out = pass_(portal, writer_config(), world)  # no agent App: the newer one waits in writing
    assert out[0] == f"{first.id}: failed (superseded)" and portal.store.get_request(second.id).state == "writing"


def test_a_newer_save_during_the_claim_ends_the_claims_for_the_pass(agents, monkeypatch):
    portal, world, cfg = agents
    req = save(portal)
    monkeypatch.setattr(portal.store, "claim_approved", lambda **kw: (_ for _ in ()).throw(Conflict("newer_definition")))
    assert pass_(portal, cfg, world) == [] and portal.store.get_request(req.id).state == "approved"


def test_a_pr_url_of_another_repo_is_not_recorded_and_the_request_waits(agents):
    portal, world, cfg = agents
    req = save(portal)
    original = world.handle

    def elsewhere(request):
        answer = original(request)
        if request.method == "POST" and request.url.path == "/repos/acme/notes/pulls":
            doc = json.loads(answer.content)
            return httpx.Response(201, json={**doc, "html_url": "https://github.com/acme/other/pull/1"})
        return answer

    world.handle = elsewhere
    pass_(portal, cfg, world)
    found = portal.store.get_request(req.id)
    assert (found.state, found.last_error) == ("writing", "github: unexpected response")


# ---- the Round 6 review's fixes ----

def _someone_s_pr(world, number, base_ref="main"):
    world.pulls[("acme/notes", number)] = {"number": number, "html_url": f"https://github.com/acme/notes/pull/{number}", "state": "open",
                                           "merged": False, "merged_at": None, "merge_commit_sha": None, "title": "x", "body": "",
                                           "head": {"ref": BRANCH}, "base_sha": world.head("acme/notes"), "base_ref": base_ref,
                                           "user": {"login": "someone", "type": "User"}}


def test_every_page_of_the_branch_s_open_prs_is_read_before_the_branch_is_touched(agents):
    portal, world, cfg = agents
    first = save(portal)
    pass_(portal, cfg, world)  # PR #1, the agent App's
    _someone_s_pr(world, 2, base_ref="release")  # someone else's, from the same branch, on the list's second page
    world.max_per_page = 1
    second = save(portal, {**DRAFT, "display_name": "Second"})
    assert f"{second.id}: failed (pr_not_ours)" in pass_(portal, cfg, world)
    listed = [c for c in world.calls if c["method"] == "GET" and c["path"] == "/repos/acme/notes/pulls"]
    assert listed[-1]["query"]["page"] == "2" and all(c["query"]["state"] == "open" for c in listed)
    assert portal.store.get_request(first.id).state == "pr_open"


def test_the_list_asks_for_open_prs_so_old_closed_ones_never_hide_an_open_one(agents):
    portal, world, cfg = agents
    for n in range(1, 41):
        world.pulls[("acme/notes", n)] = {**{"number": n, "html_url": f"https://github.com/acme/notes/pull/{n}", "merged": False,
                                             "merged_at": None, "merge_commit_sha": None, "title": "x", "body": "", "head": {"ref": BRANCH},
                                             "base_sha": world.head("acme/notes"), "user": {"login": "someone", "type": "User"}}, "state": "closed"}
    world.max_per_page = 30
    world.repos["acme/notes"][BRANCH] = world.head("acme/notes")
    _someone_s_pr(world, 41)
    req = save(portal)
    assert pass_(portal, cfg, world) == [f"{req.id}: failed (pr_not_ours)"]
    assert not [c for c in world.calls if c["method"] == "DELETE" and "/git/refs" in c["path"]]


def test_a_failed_update_of_the_pr_s_text_waits_and_the_next_pass_only_retries_it(agents):
    portal, world, cfg = agents
    save(portal)
    pass_(portal, cfg, world)
    second = save(portal, {**DRAFT, "instructions": "Find free time, briefly."})
    original, failing = world.handle, [True]

    def patch_fails_once(request):
        if request.method == "PATCH" and request.url.path == "/repos/acme/notes/pulls/1" and failing:
            failing.clear()
            return httpx.Response(502, json={"message": "busy"})
        return original(request)

    world.handle = patch_fails_once
    pass_(portal, cfg, world)
    found = portal.store.get_request(second.id)
    assert (found.state, found.attempts, found.last_error) == ("writing", 1, "github: 502 PATCH /repos/acme/notes/pulls/1")
    puts = len([c for c in world.calls if c["method"] == "PUT"])
    assert f"{second.id}: pull request #1 updated" in pass_(portal, cfg, world)
    assert len([c for c in world.calls if c["method"] == "PUT"]) == puts, "the file is already there: no second commit"
    assert "Find free time, briefly." in world.pull("acme/notes", 1)["body"]


@pytest.mark.parametrize("change, error", [
    (lambda world: world.agent_installed.update({"acme/scorecard-factory": 5}) or world.add_repo("acme/scorecard-factory", {}),
     "agent_app_on_platform_repo: acme/scorecard-factory"),
    (lambda world: world.agent_selection.update({"acme/notes": "all"}), "agent_app_on_all_repositories"),
])
def test_an_agent_app_set_up_too_wide_waits_every_pass_and_asks_for_no_token(agents, change, error):
    portal, world, cfg = agents
    change(world)
    req = save(portal)
    for _ in range(3):
        pass_(portal, cfg, world)
    found = portal.store.get_request(req.id)
    assert (found.state, found.attempts, found.last_error) == ("writing", 3, error) and not world.agent_token_requests


def test_a_pr_merged_into_another_branch_is_not_recorded(agents):
    portal, world, cfg = agents
    req = save(portal)
    pass_(portal, cfg, world)
    world.pulls[("acme/notes", 1)]["base_ref"] = "release"  # retargeted by hand
    world.merge("acme/notes", 1)
    assert pass_(portal, cfg, world) == [f"{req.id}: failed (merged_file_differs)"]


def test_a_crash_after_the_put_and_a_person_s_merge_is_recorded_as_merged(agents, monkeypatch):
    portal, world, cfg = agents
    req = save(portal)
    original = portal.store.mark_pr_open
    monkeypatch.setattr(portal.store, "mark_pr_open", lambda **kw: (_ for _ in ()).throw(RuntimeError("the writer stopped")))
    with pytest.raises(RuntimeError):
        pass_(portal, cfg, world)
    monkeypatch.setattr(portal.store, "mark_pr_open", original)
    sha = world.merge("acme/notes", 1)
    assert pass_(portal, cfg, world) == [f"{req.id}: merged"]
    found = portal.store.get_request(req.id)
    assert (found.state, found.pr_number, found.merge_sha) == ("merged", 1, sha)


def test_main_holding_the_file_with_no_merged_pr_of_the_app_s_is_already_on_main(agents):
    portal, world, cfg = agents
    req = save(portal)
    world.add_repo("acme/notes", {"builder-spec.json": SPEC, "agent.yaml": agent_yaml(DRAFT, req.id).encode()})
    assert pass_(portal, cfg, world) == [f"{req.id}: failed (already_on_main)"]


def test_the_pr_body_s_name_is_a_code_span_cut_to_its_length():
    from types import SimpleNamespace

    from portal.writer.agent import pr_body
    req = SimpleNamespace(id="r_" + "a" * 32, final=DRAFT)
    body = pr_body(req, SimpleNamespace(display_name="A`b\n" + "x" * 300))
    assert "`A'b " + "x" * 196 + "…`" in body


def test_the_open_pr_check_reads_the_repo_s_name_in_any_case():
    from portal.writer.agent import _open_pr

    class Token:
        def paginate(self, path, **kw):
            return iter([{"state": "open", "head": {"ref": BRANCH, "repo": {"full_name": "ACME/Notes"}}, "base": {"ref": "main"},
                          "user": {"login": "golden-road-portal-agent[bot]", "type": "Bot"}, "number": 1}])

    assert _open_pr(Token(), "acme/notes", "main", "golden-road-portal-agent[bot]")["number"] == 1


def test_a_scoped_token_names_the_repository_in_any_case():
    from portal_fakes import FakeGitHub
    from portal.writer.config import AGENT_PERMISSIONS, scoped_client
    world = RepoWorld()
    app = agent_app(world)
    fake = FakeGitHub().on("POST", "/app/installations/77/access_tokens", (201, {
        "token": "ghs_" + "t" * 36, "permissions": AGENT_PERMISSIONS, "repository_selection": "selected", "repositories": [{"name": "Notes"}]}))
    import dataclasses as dc
    client = scoped_client(dc.replace(app, http=httpx.Client(transport=transport(fake))), 77, "notes", AGENT_PERMISSIONS)
    assert client is not None

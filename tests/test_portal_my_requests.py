"""My requests (spec 2026-10-30 R4): filter chips All, Needs attention (pending or sent back) and Merged, as links; state badges in words."""

import re

import pytest
from portal_fakes import approved_request

DRAFT = {"schema": "registration-draft/v1", "purpose": "Builds internal tools; no email, no files.", "data_classification": "public"}


def three_states(portal):
    """acme/notes merged (the seeded one, through the writer), acme/tools pending, acme/other sent back: all by owner."""
    store, now, owner = portal.store, portal.clock(), portal.people["owner"]
    merged = approved_request(store, portal.people, now)
    store.mark_pr_open(request_id=merged.id, branch=f"portal/{merged.id}", pr_number=7, pr_url="https://github.com/acme/scorecard-factory/pull/7",
                       head_sha="2" * 40, base_sha="1" * 40, now=now)
    store.mark_merged(request_id=merged.id, merge_sha="3" * 40, now=now)
    store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": "acme/tools"}, now=now)
    back = store.create_registration(person_id=owner.id, draft={**DRAFT, "repo": "acme/other"}, now=now)
    store.decide(request_id=back.id, actor_id=portal.people["admin"].id, actor_groups=["golden-road-admins"], approve=False,
                 note="Say what it does not do.", final=None, now=now)


def rows(text):
    return set(re.findall(r'href="/requests/r_[0-9a-f]{32}">(acme/[a-z]+)</a>', text))


@pytest.mark.parametrize("value, shown", [("all", {"acme/notes", "acme/tools", "acme/other"}), ("attention", {"acme/tools", "acme/other"}),
                                          ("merged", {"acme/notes"})])
def test_the_chips_filter(portal, value, shown):
    three_states(portal)
    text = portal.client("owner").get("/requests", params={"filter": value}).text
    assert rows(text) == shown and f'<a href="/requests?filter={value}" aria-current="page">' in text


def test_all_is_the_default_and_every_chip_is_a_link(portal):
    three_states(portal)
    text = portal.client("owner").get("/requests").text
    assert rows(text) == {"acme/notes", "acme/tools", "acme/other"} and '<a href="/requests?filter=all" aria-current="page">All</a>' in text
    assert '<a href="/requests?filter=attention">Needs attention</a>' in text and '<a href="/requests?filter=merged">Merged</a>' in text


def test_badges_say_the_state_in_words(portal):
    three_states(portal)
    text = portal.client("owner").get("/requests").text
    for state, word in (("merged", "Merged"), ("pending", "Pending"), ("sent_back", "Sent back")):
        assert f'<span class="badge state-{state}">{word}</span>' in text


def test_an_empty_filter_says_so_and_a_bad_one_is_a_400(portal):
    text = portal.client("owner").get("/requests", params={"filter": "merged"}).text   # only the pending seed exists
    assert rows(text) == set() and "No request matches this filter." in text
    assert portal.client("owner").get("/requests", params={"filter": "everything"}).status_code == 400


def test_my_apps_are_cards(portal):
    text = portal.client("owner").get("/requests").text
    assert '<ul class="apps">' in text and '<a href="/apps/acme/notes"><span class="mono">acme/notes</span>' in text


def every_state(portal):
    """Owner's requests in approved, writing, pr_open and failed, beside three_states' merged, pending and sent back."""
    from portal.store.base import RepoAdmin
    store, now = portal.store, portal.clock()
    repos = ("acme/writ", "acme/prop", "acme/fail", "acme/appr")  # left approved last: the writer claims the oldest approved
    store.replace_repo_admins([RepoAdmin(r, "owner-gh", 1001, now) for r in ("acme/notes", "acme/tools", "acme/other", *repos)], observed_at=now)
    three_states(portal)
    made = {}
    for repo in repos:
        made[repo] = approved_request(store, portal.people, now, repo=repo, claim=repo != "acme/appr")
    store.mark_pr_open(request_id=made["acme/prop"].id, branch="b", pr_number=8, pr_url="https://github.com/acme/scorecard-factory/pull/8",
                       head_sha="2" * 40, base_sha="1" * 40, now=now)
    store.mark_failed(request_id=made["acme/fail"].id, reason="base_moved", now=now)
    return {repo: store.get_request(r.id).state for repo, r in made.items()}


@pytest.mark.parametrize("value, shown", [
    ("all", {"acme/notes", "acme/tools", "acme/other", "acme/appr", "acme/writ", "acme/prop", "acme/fail"}),
    ("attention", {"acme/tools", "acme/other"}),   # failed, approved and on its way are not the requester's to act on
    ("merged", {"acme/notes"})])
def test_the_chips_filter_every_state(portal, value, shown):
    assert every_state(portal) == {"acme/appr": "approved", "acme/writ": "writing", "acme/prop": "pr_open", "acme/fail": "failed"}
    assert rows(portal.client("owner").get("/requests", params={"filter": value}).text) == shown


def test_the_requests_table_centres_its_cells(portal):
    assert '<table class="requests">' in portal.client("owner").get("/requests").text

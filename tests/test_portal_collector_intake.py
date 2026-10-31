"""Reading an intake record (spec 2026-10-31 C4, R2): the scorecard's rules, and a small view of what it says."""

import httpx
import pytest
import yaml
from portal_fakes import READ_PERMISSIONS, FakeGitHub, git_sha, intake_trees, repo_at, transport

from portal.collectors.intake import INVALID, RecordUnreadable, current_record, parse_record
from portal.collectors.repo_files import Transient
from portal.github.client import GitHub
from portal.writer.render import intake_yaml

P1, P2, REQ = "p_" + "1" * 32, "p_" + "2" * 32, "r_" + "3" * 32
V2 = {"schema": "intake/v2", "repo": "acme/notes", "tier": "moderate", "purpose": "Stores short team notes.", "owner": P1,
      "data_classification": "public", "registered_by": P1, "approved_by": P2, "approved_at": "2026-10-01T12:00:00Z",
      "approval_justification": "Purpose and classification checked.", "review_by": "2026-11-15", "request": REQ}
V1 = {"schema": "intake/v1", "repo": "acme/notes", "tier": "high", "purpose": "A notes API.", "owner": "platform-team",
      "data_classification": "confidential"}


def raw(doc, header=""):
    return (header + yaml.safe_dump(doc, sort_keys=False)).encode()


def test_a_portal_written_v2_record_is_viewed_with_its_review_date():
    view = parse_record(intake_yaml(V2, REQ).encode(), "acme/notes")
    assert view == {"schema": "intake/v2", "by_portal": True, "tier": "moderate", "data_classification": "public",
                    "purpose": "Stores short team notes.", "owner": P1, "review_by": "2026-11-15"}


def test_a_record_below_its_floor_with_the_approver_s_override_is_viewed():
    doc = {**V2, "evidence_commit": "e" * 40, "floor_tier": "high", "floor_classification": "public",
           "floor_override": {"by": P2, "at": "2026-10-01T12:00:00Z", "reason": "Reviewed by security on the change request."}}
    assert parse_record(raw(doc), "acme/notes")["tier"] == "moderate"
    assert parse_record(raw({**doc, "floor_override": {**doc["floor_override"], "by": P1}}), "acme/notes") == INVALID


def test_a_hand_written_v2_and_a_v1_record_say_so():
    assert parse_record(raw(V2), "acme/notes")["by_portal"] is False
    view = parse_record(raw(V1, "# registered outside the portal\n"), "acme/notes")
    assert view["schema"] == "intake/v1" and "review_by" not in view and view["owner"] == "platform-team"


def test_the_central_mark_is_kept_only_spelled_as_the_poller_reads_it():
    assert parse_record(raw({**V2, "scorecard": "central"}), "acme/notes")["scorecard"] == "central"
    quoted = raw(V2) + b'scorecard: "central"\n'
    assert parse_record(quoted, "acme/notes") == INVALID


@pytest.mark.parametrize("bad", [
    b"schema: intake/v2\nschema: intake/v2\n", b": not yaml : [", "ÿ".encode("latin-1"), b"- a list\n",
    raw({**V2, "repo": "acme/other"}), raw({**V2, "approved_by": P1}), raw({**V2, "review_by": "2026-02-30"}),
    raw({**V1, "data_classification": "internal"}), raw({**V2, "schema": "intake/v3"}), raw({**V2, "tier": "huge"}),
    raw({**V2, "floor_tier": "low"}),  # the floor's three fields, or none (the scorecard's _check_floor, as every rule here)
    raw({**V2, "evidence_commit": "e" * 40, "floor_tier": "high", "floor_classification": "public"}),  # below the floor, no override
    raw({**V2, "floor_override": {"by": P2, "at": "2026-10-01T12:00:00Z", "reason": "x" * 30}})])  # an override with no floor
def test_anything_else_is_invalid_and_nothing_of_it_is_kept(bad):
    assert parse_record(bad, "acme/notes") == INVALID


def gh_with(answer, commit="f" * 40):
    gh = FakeGitHub(permissions=READ_PERMISSIONS)
    gh.on("GET", "/repos/acme/scorecard-factory/contents/intake/acme/notes.yaml", answer)
    intake_trees(gh, "acme/scorecard-factory", commit, ["intake/acme/notes.yaml"])  # no other spelling
    return gh, GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_x")


def test_the_current_record_is_read_at_one_commit_with_its_blob_sha():
    body = intake_yaml(V2, REQ).encode()
    gh = FakeGitHub(permissions=READ_PERMISSIONS)
    repo_at(gh, "acme/scorecard-factory", {"intake/acme/notes.yaml": body}, commit="f" * 40)
    sha, view = current_record(GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_x"),
                               "acme/scorecard-factory", "acme/notes", "f" * 40)
    assert sha == git_sha(body) and view["schema"] == "intake/v2"
    [call] = gh.called("GET", "/repos/acme/scorecard-factory/contents/intake/acme/notes.yaml")
    assert call["query"]["ref"] == "f" * 40


def test_no_record_is_a_first_registration():
    gh, client = gh_with((404, {"message": "Not Found"}))
    assert current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40) == (None, None)


@pytest.mark.parametrize("answer", [(403, {"message": "no"}), (200, [{"type": "file", "name": "x"}]), (200, {"type": "file", "sha": "zz"})])
def test_a_record_with_no_sha_to_compare_can_t_be_used(answer):
    _, client = gh_with(answer)
    with pytest.raises(RecordUnreadable):
        current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40)


def test_a_record_too_large_or_undecodable_is_invalid_but_still_has_its_sha():
    _, client = gh_with((200, {"type": "file", "sha": "c" * 40, "size": 70000, "encoding": "base64", "content": ""}))
    assert current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40) == ("c" * 40, INVALID)


def test_github_busy_is_transient():
    _, client = gh_with((502, {"message": "busy"}))
    with pytest.raises(Transient):
        current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40)


def alias_bomb(head=""):
    """~500 bytes of YAML whose aliases expand to 9**9 leaves: built as shared references it is small, walked it never ends."""
    lines = [head + 'l0: &l0 ["x", "x", "x", "x", "x", "x", "x", "x", "x"]']
    lines += [f"l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 9) + "]" for i in range(1, 9)]
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize("raw_doc", [alias_bomb("schema: intake/v2\nrepo: acme/notes\n"),
                                     raw(V1).replace(b"tier: high", b"tier: &t high")])
def test_anchors_and_aliases_are_refused_before_the_document_is_built(raw_doc):
    """An alias bomb is a few hundred bytes that the walk over the document (retired_values) never finishes: refused as text."""
    assert parse_record(raw_doc, "acme/notes") == INVALID


# ---- more cases ----

def test_an_invalid_record_keeps_only_the_central_mark_spelled_as_the_poller_reads_it():
    """The approval carries it, so replacing a broken record of a central app doesn't take the app out of central review."""
    broken = raw({**V2, "tier": "huge"})
    assert parse_record(broken + b"scorecard: central\n", "acme/notes") == {**INVALID, "scorecard": "central"}
    assert parse_record(broken + b'scorecard: "central"\n', "acme/notes") == INVALID
    assert parse_record(b"scorecard: central\n- not a map\n", "acme/notes") == {**INVALID, "scorecard": "central"}


def test_an_owner_too_long_to_show_is_invalid():
    assert parse_record(raw({**V1, "owner": "t" * 201}), "acme/notes") == INVALID
    assert parse_record(raw({**V1, "owner": "t" * 200}), "acme/notes")["owner"] == "t" * 200


def test_a_record_spelled_in_another_case_can_t_be_compared():
    from portal.collectors.intake import RecordCaseMismatch
    gh = FakeGitHub(permissions=READ_PERMISSIONS)
    repo_at(gh, "acme/scorecard-factory", {"intake/Acme/Notes.yaml": intake_yaml(V2, REQ).encode()}, commit="f" * 40)
    client = GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_x")
    with pytest.raises(RecordCaseMismatch):
        current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40)
    repo_at(gh, "acme/scorecard-factory", {"intake/acme/notes.yaml": intake_yaml(V2, REQ).encode(),
                                           "intake/acme/NOTES.yaml": intake_yaml(V2, REQ).encode()}, commit="f" * 40)
    with pytest.raises(RecordCaseMismatch):  # the exact spelling and another one: which is the record?
        current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40)


import datetime as dt  # noqa: E402

from portal_fakes import T0, factory_with_records  # noqa: E402

from portal.collectors.intake import collect_intake  # noqa: E402
from portal.store.memory import MemoryStore  # noqa: E402

TODAY = dt.date(2026, 10, 31)


def records(**review_by):
    """acme/notes (v2, portal-written), acme/old (v1), acme/broken (not YAML): review dates per repo name."""
    notes = intake_yaml({**V2, "review_by": review_by.get("notes", "2026-11-15")}, REQ).encode()
    return {"acme/notes": notes, "acme/old": raw({**V1, "repo": "acme/old"}), "acme/broken": b": [not yaml"}


def collect(store, files, *, today=TODAY, days=30, modes=None):
    gh = factory_with_records(FakeGitHub(permissions=READ_PERMISSIONS), files, modes=modes)
    client = GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_x")
    return collect_intake(client, store, org="acme", today=today, due_soon_days=days, now=T0), gh


def test_every_record_is_observed_v1_v2_and_malformed():
    store = MemoryStore()
    result, _ = collect(store, records())
    assert result == {"records": 3, "unreadable": 1, "opened": 1, "closed": 0}
    rows = {r.repo: r for r in store.intake_observed()}
    assert (rows["acme/notes"].schema, rows["acme/notes"].review_by, rows["acme/notes"].by_portal) == ("intake/v2", dt.date(2026, 11, 15), True)
    assert (rows["acme/old"].schema, rows["acme/old"].review_by, rows["acme/old"].owner) == ("intake/v1", None, "platform-team")
    assert (rows["acme/broken"].problem, rows["acme/broken"].schema) == ("invalid", None)
    assert store.rereview_status()["intake"].counts == {"records": 3, "unreadable": 1}


def test_a_symlinked_record_is_not_a_file_and_is_never_read():
    store = MemoryStore()
    _, gh = collect(store, {"acme/notes": raw(V2)}, modes={"acme/notes": "120000"})
    assert store.intake_observed()[0].problem == "not_a_file"
    assert not [c for c in gh.calls if "/git/blobs/" in c["path"]]


def test_an_unchanged_record_is_not_fetched_again():
    store = MemoryStore()
    collect(store, records())
    _, gh = collect(store, records())
    assert not [c for c in gh.calls if "/git/blobs/" in c["path"] and c["path"].endswith(git_sha(records()["acme/notes"]))]


def test_due_soon_then_overdue_then_renewed():
    store = MemoryStore()
    collect(store, records())   # 2026-11-15 is 15 days away
    [soon] = store.rereview_items()
    assert (soon.repo, soon.reason, soon.key) == ("acme/notes", "review_due_soon", "2026-11-15")
    collect(store, records(), today=dt.date(2026, 11, 16))   # the date has passed
    [overdue] = store.rereview_items()
    assert (overdue.reason, overdue.key) == ("review_overdue", "2026-11-15")
    assert [i.close_kind for i in store.rereview_items(open_only=False) if i.id == soon.id] == ["auto"]
    result, _ = collect(store, records(notes="2027-11-15"), today=dt.date(2026, 11, 16))   # renewed
    assert result["closed"] == 1 and store.rereview_items() == []
    last = store.audit_for("rereview_item", overdue.id)[-1]
    assert (last.actor_kind, last.reason) == ("system", "Closed by the platform: the record's review date is now 2027-11-15.")


def test_0_turns_due_soon_off_and_closes_what_it_opened():
    store = MemoryStore()
    collect(store, records())
    result, _ = collect(store, records(), days=0)
    assert result["closed"] == 1 and store.rereview_items() == []


def test_an_item_an_admin_closed_is_not_reopened_for_the_same_date():
    store = MemoryStore()
    admin, _, _ = store.sign_in(issuer="i", subject="a", display_name="A", email=None, groups=[], roles=["admin"], now=T0,
                                max_age=dt.timedelta(hours=8))
    collect(store, records(notes="2026-10-01"))
    [item] = store.rereview_items()
    store.close_rereview_item(item_id=item.id, actor_id=admin.id, note="Owner is renewing it this week.", now=T0, events_seen=0)
    result, _ = collect(store, records(notes="2026-10-01"))
    assert result["opened"] == 0 and store.rereview_items() == []


def test_a_record_that_breaks_keeps_its_open_items():
    store = MemoryStore()
    collect(store, records(notes="2026-10-01"))
    collect(store, {**records(), "acme/notes": b": [broken"})
    assert [i.reason for i in store.rereview_items()] == ["review_overdue"]


def test_a_factory_that_can_t_be_read_changes_nothing_and_says_so():
    store = MemoryStore()
    collect(store, records(notes="2026-10-01"))
    gh = FakeGitHub(permissions=READ_PERMISSIONS).on("GET", "/repos/acme/scorecard-factory", (404, {"message": "Not Found"}))
    client = GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_x")
    assert collect_intake(client, store, org="acme", today=TODAY, due_soon_days=30, now=T0) == {"problem": "factory_unreadable"}
    assert len(store.intake_observed()) == 3 and len(store.rereview_items()) == 1
    assert store.rereview_status()["intake"].problem == "factory_unreadable"


def test_github_busy_changes_nothing():
    store = MemoryStore()
    gh = FakeGitHub(permissions=READ_PERMISSIONS).on("GET", "/repos/acme/scorecard-factory", (502, {"message": "busy"}))
    client = GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_x")
    assert collect_intake(client, store, org="acme", today=TODAY, due_soon_days=30, now=T0) == {"transient": True}
    assert store.rereview_status() == {}


def test_an_invalid_record_keeps_the_central_mark_only_spelled_as_the_poller_reads_it():
    store = MemoryStore()
    broken = raw({**V2, "tier": "huge"})
    collect(store, {"acme/notes": broken + b"scorecard: central\n", "acme/tools": raw({**V2, "repo": "acme/tools", "tier": "huge"})
                    + b'scorecard: "central"\n'})
    rows = {r.repo: r for r in store.intake_observed()}
    assert (rows["acme/notes"].problem, rows["acme/notes"].scorecard) == ("invalid", "central")
    assert (rows["acme/tools"].problem, rows["acme/tools"].scorecard) == ("invalid", None)


def test_a_record_in_two_spellings_is_ambiguous_and_keeps_its_open_items():
    store = MemoryStore()
    collect(store, records(notes="2026-10-01"))
    collect(store, {**records(notes="2026-10-01"), "acme/Notes": raw(V2)})
    [row] = [r for r in store.intake_observed() if r.repo.lower() == "acme/notes"]
    assert (row.repo, row.problem, row.schema) == ("acme/Notes", "case_mismatch", None)  # sorted: Notes before notes
    assert [i.reason for i in store.rereview_items()] == ["review_overdue"]


# ---- the spellings are read from git trees, which don't stop at 1000 entries ----

def spelled_client(**trees):
    gh = FakeGitHub(permissions=READ_PERMISSIONS)
    repo_at(gh, "acme/scorecard-factory", {"intake/acme/notes.yaml": intake_yaml(V2, REQ).encode()}, commit="f" * 40)
    intake_trees(gh, "acme/scorecard-factory", "f" * 40, ["intake/acme/notes.yaml"], **trees)
    return gh, GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_x")


def test_another_spelling_is_found_in_an_owner_directory_of_any_size():
    from portal.collectors.intake import RecordCaseMismatch
    many = [{"path": f"app{i:04d}.yaml", "mode": "100644", "type": "blob", "sha": "b" * 40} for i in range(1500)]
    _, client = spelled_client(owner_entries=many + [{"path": "NOTES.yaml", "mode": "100644", "type": "blob", "sha": "b" * 40}])
    with pytest.raises(RecordCaseMismatch):
        current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40)
    _, client = spelled_client(owner_entries=many)
    assert current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40)[1]["schema"] == "intake/v2"


def test_a_truncated_tree_can_t_say_there_is_no_other_spelling():
    _, client = spelled_client(truncated=True)
    with pytest.raises(RecordUnreadable):
        current_record(client, "acme/scorecard-factory", "acme/notes", "f" * 40)


@pytest.mark.parametrize("text, mark", [
    ("scorecard: central\n", "central"),                   # the line grep -qxF finds
    ("scorecard: central\r\n", None),                      # GNU grep reads "scorecard: central\r": no match
    ("tier: moderate\u2028scorecard: central\n", None)])   # U+2028 ends a line for YAML and str.splitlines, not for grep
def test_the_central_mark_is_the_line_grep_finds_split_on_newline_only(text, mark):
    """scorecard-assess runs `grep -qxF 'scorecard: central'`: a mark the YAML has but grep doesn't see is
    refused; an invalid record keeps the mark only when grep would see it."""
    base = raw({k: v for k, v in V2.items() if k != "tier"})
    view = parse_record(base + text.encode(), "acme/notes")
    if mark is None:
        assert view.get("scorecard") is None
    else:
        assert view["scorecard"] == mark
    broken = raw({**V2, "tier": "huge"}) + text.encode()
    assert parse_record(broken, "acme/notes").get("scorecard") == mark


# ---- more cases ----

def test_the_whole_intake_tree_gone_holds_the_date_items_it_had():
    store = MemoryStore()
    collect(store, records(notes="2026-10-01"))
    for _ in range(2):   # the factory's intake/ is empty: more likely a mistake than every app gone, on this pass and the next
        result, _ = collect(store, {})
        assert result["held"] == 3 and [i.reason for i in store.rereview_items()] == ["review_overdue"]
    assert len(store.intake_observed()) == 3   # the cache keeps what it last read


def test_a_record_rewritten_as_v1_keeps_its_overdue_item():
    store = MemoryStore()
    collect(store, records(notes="2026-10-01"))
    collect(store, {**records(), "acme/notes": raw(V1)})
    assert [i.reason for i in store.rereview_items()] == ["review_overdue"]


def test_the_platform_s_notes_say_why_it_closed():
    store = MemoryStore()
    collect(store, records())                                  # due soon (2026-11-15)
    [soon] = store.rereview_items()
    collect(store, records(), today=dt.date(2026, 11, 16))    # passed
    assert store.audit_for("rereview_item", soon.id)[-1].reason == (
        "Closed by the platform: the review date 2026-11-15 has passed, so a Review overdue item replaces this one.")
    store = MemoryStore()
    collect(store, records())
    [soon] = store.rereview_items()
    collect(store, records(), days=0)
    assert store.audit_for("rereview_item", soon.id)[-1].reason == (
        "Closed by the platform: review-due-soon items are off (PORTAL_REVIEW_DUE_SOON_DAYS is 0).")


def test_owner_directories_named_dot_or_dot_dot_are_not_owners():
    store = MemoryStore()
    collect(store, {**records(), "../notes": raw({**V1, "repo": "../notes"}), "./notes": raw({**V1, "repo": "./notes"})})
    assert sorted(r.repo for r in store.intake_observed()) == ["acme/broken", "acme/notes", "acme/old"]


def test_a_record_that_had_a_problem_is_read_again_even_when_its_blob_is_the_same():
    store = MemoryStore()
    broken = {"acme/broken": b": [not yaml"}
    collect(store, broken)
    _, gh = collect(store, broken)
    assert [c for c in gh.calls if c["path"].endswith(git_sha(broken["acme/broken"]))]

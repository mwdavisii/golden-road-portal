import httpx
import pytest
from portal_fakes import READ_PERMISSIONS, FakeGitHub, repo_at, transport

from portal.collectors.repo_files import UNREADABLE, Transient, head_commit, read_capped
from portal.github.client import GitHub

API = "https://api.github.com"


def client(gh):
    return GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x")


def test_the_head_commit_is_the_default_branch_s_and_reads_are_at_that_commit():
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {"builder-spec.json": b"{}"})
    sha = head_commit(client(gh), "acme/notes")
    assert sha == "a" * 40 and read_capped(client(gh), "acme/notes", "builder-spec.json", sha, 100) == b"{}"
    assert gh.called("GET", "/repos/acme/notes/contents/builder-spec.json")[0]["query"]["ref"] == sha


@pytest.mark.parametrize("answer", [b"x" * 101, 403, 451, {"type": "dir"}, {"type": "file", "encoding": "none", "size": 5, "content": ""},
                                    {"type": "file", "encoding": "base64", "size": 3, "content": "!!!"}, {"type": "symlink", "target": "/etc"}])
def test_oversized_forbidden_or_odd_files_are_unreadable(answer):
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {"requirements.txt": answer})
    assert read_capped(client(gh), "acme/notes", "requirements.txt", "a" * 40, 100) is UNREADABLE


def test_a_missing_file_is_none():
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {})
    assert read_capped(client(gh), "acme/notes", "package-lock.json", "a" * 40, 100) is None


@pytest.mark.parametrize("status", [500, 502, 429])
def test_a_transient_github_error_is_transient_not_unreadable(status):
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {"builder-spec.json": status})
    with pytest.raises(Transient):
        read_capped(client(gh), "acme/notes", "builder-spec.json", "a" * 40, 100)


def test_a_head_that_is_not_a_commit_id_or_branch_is_refused():
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {}, commit="not-a-sha")
    with pytest.raises(ValueError):
        head_commit(client(gh), "acme/notes")


LIMITED = [{"x-ratelimit-remaining": "0"}, {"retry-after": "30"}]


@pytest.mark.parametrize("headers", LIMITED)
def test_a_rate_limit_403_is_transient_and_a_plain_403_is_not(headers):
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {})
    gh.routes[("GET", "api.github.com", "/repos/acme/notes/contents/requirements.txt")] = [(403, {"message": "rate limit"}, headers)]
    with pytest.raises(Transient):
        read_capped(client(gh), "acme/notes", "requirements.txt", "a" * 40, 100)
    gh.routes[("GET", "api.github.com", "/repos/acme/notes/contents/requirements.txt")] = [(403, {"message": "no"}, {"x-ratelimit-remaining": "42"})]
    assert read_capped(client(gh), "acme/notes", "requirements.txt", "a" * 40, 100) is UNREADABLE


@pytest.mark.parametrize("headers", LIMITED)
def test_a_rate_limit_403_on_the_repo_or_its_head_is_transient(headers):
    for path in ("/repos/acme/notes", "/repos/acme/notes/git/ref/heads/main"):
        gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {})
        gh.routes[("GET", "api.github.com", path)] = [(403, {"message": "rate limit"}, headers)]
        with pytest.raises(Transient):
            head_commit(client(gh), "acme/notes")


def test_a_github_error_keeps_only_the_two_rate_limit_headers_and_never_a_body():
    from portal.github.client import GitHubError
    gh = FakeGitHub(permissions=READ_PERMISSIONS)
    gh.on("GET", "/repos/acme/notes", (403, {"message": "SECRET BODY"}, {"x-ratelimit-remaining": "0", "retry-after": "9", "set-cookie": "s=1"}))
    with pytest.raises(GitHubError) as caught:
        client(gh).call("GET", "/repos/acme/notes")
    assert caught.value.headers == {"x-ratelimit-remaining": "0", "retry-after": "9"} and "SECRET" not in str(caught.value)


@pytest.mark.parametrize("path, answer", [
    ("/repos/acme/notes", (200, {})), ("/repos/acme/notes", (200, [])), ("/repos/acme/notes", (200, "main")),
    ("/repos/acme/notes/git/ref/heads/main", (200, {})), ("/repos/acme/notes/git/ref/heads/main", (200, {"object": None})),
    ("/repos/acme/notes/git/ref/heads/main", (200, {"object": {"sha": 5}})),
])
def test_a_malformed_answer_is_a_malformed_error_not_a_crash(path, answer):
    from portal.collectors.repo_files import Malformed
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {})
    gh.routes[("GET", "api.github.com", path)] = [answer]
    with pytest.raises(Malformed):
        head_commit(client(gh), "acme/notes")


# ---- files over the contents API's inline limit come through the blobs API ----

import base64  # noqa: E402

BLOB = "b" * 40


def big_file(gh, size, blob_answer=None, *, sha=BLOB, repo="acme/notes", path="package-lock.json"):
    """The contents API's answer for a file too large to inline (no content, encoding none), and the blob that holds it."""
    gh.routes[("GET", "api.github.com", f"/repos/{repo}/contents/{path}")] = [(200, {"type": "file", "encoding": "none", "size": size, "content": "", "sha": sha})]
    if blob_answer is not None:
        gh.routes[("GET", "api.github.com", f"/repos/{repo}/git/blobs/{sha}")] = [blob_answer]
    return gh


def blob_of(raw, **changes):
    return (200, {"encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode(), "sha": BLOB, **changes})


def test_a_file_over_the_inline_limit_is_read_through_its_blob_at_the_same_commit():
    raw = b"x" * 1_500_000
    gh = big_file(repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {}), len(raw), blob_of(raw))
    assert read_capped(client(gh), "acme/notes", "package-lock.json", "a" * 40, 10 * 1024 * 1024) == raw
    assert gh.called("GET", f"/repos/acme/notes/git/blobs/{BLOB}")


def test_a_file_over_the_cap_is_unreadable_without_fetching_its_blob():
    gh = big_file(repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {}), 11 * 1024 * 1024, blob_of(b"x"))
    assert read_capped(client(gh), "acme/notes", "package-lock.json", "a" * 40, 10 * 1024 * 1024) is UNREADABLE
    assert not gh.called("GET", f"/repos/acme/notes/git/blobs/{BLOB}")


def test_a_large_file_that_names_no_blob_or_has_an_odd_blob_is_unreadable():
    raw = b"x" * 1_500_000
    for sha, blob in ((None, None), ("not-a-sha", None), (BLOB, blob_of(raw, encoding="utf-8")), (BLOB, blob_of(raw, content="!!!")),
                      (BLOB, blob_of(raw, size="big")), (BLOB, (200, ["not", "a", "dict"])), (BLOB, (403, {"message": "no"})), (BLOB, (404, {"message": "gone"}))):
        gh = big_file(repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {}), len(raw), blob, sha=sha)
        assert read_capped(client(gh), "acme/notes", "package-lock.json", "a" * 40, 10 * 1024 * 1024) is UNREADABLE, (sha, blob)
    gh = big_file(repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {}), 1_500_000, blob_of(b"y" * 2_000_000))  # bigger than it said
    assert read_capped(client(gh), "acme/notes", "package-lock.json", "a" * 40, 1_800_000) is UNREADABLE


@pytest.mark.parametrize("answer", [(502, {"message": "x"}), (429, {"message": "x"}), (403, {"message": "x"}, {"retry-after": "5"})])
def test_a_transient_or_rate_limited_blob_fetch_is_transient(answer):
    gh = big_file(repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {}), 1_500_000, answer)
    with pytest.raises(Transient):
        read_capped(client(gh), "acme/notes", "package-lock.json", "a" * 40, 10 * 1024 * 1024)

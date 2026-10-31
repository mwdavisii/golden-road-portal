"""Read one file of a repo at one commit with a size cap, through the read App (Contents: read). The answer is one of: the bytes, None
(the file isn't there), UNREADABLE (it is there but can't be used: forbidden, oversized, not a plain file, undecodable) or a
Transient error (GitHub is down or limiting us: nothing may be concluded from it, so the next pass retries)."""

import base64
import binascii

from ..github.client import GitHubError
from ..github.shapes import BRANCH, SHA

UNREADABLE = object()


class Transient(Exception):
    pass


class Malformed(ValueError):
    """GitHub answered, but not with the shape the API documents (or with a value that isn't a branch name or a commit ID)."""


def is_transient(exc: GitHubError) -> bool:
    return exc.status == 0 or exc.status >= 500 or exc.rate_limited


def head_commit(gh, repo) -> str:
    """The head commit of the repo's default branch. A GitHubError for a repo or ref the App can't see is the caller's to handle."""
    try:
        branch = gh.call("GET", f"/repos/{repo}").json()["default_branch"]
        if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
            raise Malformed("not a branch name")
        sha = gh.call("GET", f"/repos/{repo}/git/ref/heads/{branch}").json()["object"]["sha"]
    except GitHubError as exc:
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        raise
    except Malformed:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        raise Malformed("unexpected answer") from exc
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise Malformed("not a commit ID")
    return sha


def _decode(content, cap):
    if not isinstance(content, str):
        return UNREADABLE
    try:
        raw = base64.b64decode("".join(content.split()), validate=True)
    except (binascii.Error, ValueError):
        return UNREADABLE
    return UNREADABLE if len(raw) > cap else raw


def read_blob(gh, repo, sha, cap):
    """A file too large for the contents API to inline: its blob, by the sha the contents answer named. Same cap, same rules."""
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        return UNREADABLE
    try:
        doc = gh.call("GET", f"/repos/{repo}/git/blobs/{sha}").json()
    except GitHubError as exc:
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        return UNREADABLE
    except ValueError:
        return UNREADABLE
    if not isinstance(doc, dict) or doc.get("encoding") != "base64" or type(doc.get("size")) is not int or doc["size"] > cap:
        return UNREADABLE
    return _decode(doc.get("content"), cap)



def read_with_sha(gh, repo, path, commit, cap):
    """(blob SHA, bytes or UNREADABLE) of one file at one commit, or None when it isn't there. The SHA is the contents API's, the
    value the writer compares before it replaces the file (spec 2026-10-31 C5). Raises Transient when GitHub can't answer for now,
    Malformed when the answer isn't a file with a SHA, and GitHubError for any other refusal (the caller decides what it means)."""
    try:
        doc = gh.call("GET", f"/repos/{repo}/contents/{path}", params={"ref": commit}).json()
    except GitHubError as exc:
        if exc.status == 404:
            return None
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        raise
    except ValueError as exc:
        raise Malformed("unexpected answer") from exc
    if not isinstance(doc, dict) or doc.get("type") != "file" or not isinstance(doc.get("sha"), str) or not SHA.fullmatch(doc["sha"]):
        raise Malformed("not a file with a blob SHA")
    size = doc.get("size")
    if type(size) is not int or size > cap or doc.get("encoding") != "base64":
        return doc["sha"], UNREADABLE
    return doc["sha"], _decode(doc.get("content"), cap)


def read_capped(gh, repo, path, commit, cap):
    try:
        doc = gh.call("GET", f"/repos/{repo}/contents/{path}", params={"ref": commit}).json()
    except GitHubError as exc:
        if exc.status == 404:
            return None
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        return UNREADABLE
    except ValueError:
        return UNREADABLE
    if not isinstance(doc, dict) or doc.get("type") != "file":
        return UNREADABLE
    size = doc.get("size")
    if type(size) is not int or size > cap:
        return UNREADABLE
    if doc.get("encoding") == "base64":
        return _decode(doc.get("content"), cap)
    if doc.get("encoding") == "none" and size > 0 and not doc.get("content"):  # over the inline limit: the contents API sends no content
        return read_blob(gh, repo, doc.get("sha"), cap)
    return UNREADABLE


REGULAR = ("100644", "100755")


def tree_modes(gh, repo, commit) -> dict:
    """The stored mode of every entry in the commit's root tree (not recursive: the evidence reads four root files). The contents API
    follows a symlink to an in-repo file and answers it as a file; the stored mode is the truth, and the scorecard reads the same."""
    try:
        doc = gh.call("GET", f"/repos/{repo}/git/trees/{commit}").json()
    except GitHubError as exc:
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        raise
    except ValueError as exc:
        raise Malformed("unexpected answer") from exc
    entries = doc.get("tree") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or any(not isinstance(e, dict) or not isinstance(e.get("path"), str) or not isinstance(e.get("mode"), str)
                                            for e in entries):
        raise Malformed("unexpected answer")
    return {e["path"]: e["mode"] for e in entries}


def read_stored(gh, repo, path, commit, cap, modes):
    """Like read_capped, for a root file of the commit whose tree gave `modes`: not in the tree is None; a symlink, a submodule or a
    directory is UNREADABLE (never followed)."""
    mode = modes.get(path)
    if mode is None:
        return None
    if mode not in REGULAR:
        return UNREADABLE
    return read_capped(gh, repo, path, commit, cap)


def git_tree(gh, repo, sha) -> list[dict]:
    """One git tree's entries (not recursive), each checked for its shape: unlike the contents API's listing, it doesn't stop at 1000
    entries. A truncated tree is Malformed (it can't say what isn't there). Raises GitHubError for any refusal: the caller decides."""
    try:
        doc = gh.call("GET", f"/repos/{repo}/git/trees/{sha}").json()
    except ValueError as exc:
        raise Malformed("unexpected answer") from exc
    entries = doc.get("tree") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or doc.get("truncated") is not False or any(
            not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("path", "mode", "type", "sha")) or not SHA.fullmatch(e["sha"])
            for e in entries):
        raise Malformed("unexpected tree")
    return entries


def case_variants(gh, repo, path, commit) -> list[str]:
    """The other spellings of an intake/<owner>/<name>.yaml at one commit: the same path in another case (intake/ACME/notes.yaml for
    intake/acme/notes.yaml). GitHub's paths are case-sensitive and the scorecard reads exactly one, so a second spelling makes the
    record ambiguous. Walks the commit's trees: the root, intake/, each owner in any case. Raises as git_tree does."""
    top, owner, name = path.split("/")
    intake = next((e for e in git_tree(gh, repo, commit) if e["path"] == top and e["type"] == "tree"), None)
    if intake is None:
        return []
    found = []
    for entry in git_tree(gh, repo, intake["sha"]):
        if entry["type"] == "tree" and entry["path"].lower() == owner.lower():
            found += [f"{top}/{entry['path']}/{e['path']}" for e in git_tree(gh, repo, entry["sha"]) if e["path"].lower() == name.lower()]
    return [p for p in found if p != path]

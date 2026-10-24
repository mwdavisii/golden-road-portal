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


def _transient(exc: GitHubError) -> bool:
    return exc.status == 0 or exc.status >= 500 or exc.rate_limited


def head_commit(gh, repo) -> str:
    """The head commit of the repo's default branch. A GitHubError for a repo or ref the App can't see is the caller's to handle."""
    try:
        branch = gh.call("GET", f"/repos/{repo}").json()["default_branch"]
        if not isinstance(branch, str) or not BRANCH.fullmatch(branch):
            raise Malformed("not a branch name")
        sha = gh.call("GET", f"/repos/{repo}/git/ref/heads/{branch}").json()["object"]["sha"]
    except GitHubError as exc:
        if _transient(exc):
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


def _blob(gh, repo, sha, cap):
    """A file too large for the contents API to inline: its blob, by the sha the contents answer named. Same cap, same rules."""
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        return UNREADABLE
    try:
        doc = gh.call("GET", f"/repos/{repo}/git/blobs/{sha}").json()
    except GitHubError as exc:
        if _transient(exc):
            raise Transient(str(exc)) from exc
        return UNREADABLE
    except ValueError:
        return UNREADABLE
    if not isinstance(doc, dict) or doc.get("encoding") != "base64" or type(doc.get("size")) is not int or doc["size"] > cap:
        return UNREADABLE
    return _decode(doc.get("content"), cap)


def read_capped(gh, repo, path, commit, cap):
    try:
        doc = gh.call("GET", f"/repos/{repo}/contents/{path}", params={"ref": commit}).json()
    except GitHubError as exc:
        if exc.status == 404:
            return None
        if _transient(exc):
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
        return _blob(gh, repo, doc.get("sha"), cap)
    return UNREADABLE


REGULAR = ("100644", "100755")


def tree_modes(gh, repo, commit) -> dict:
    """The stored mode of every entry in the commit's root tree (not recursive: the evidence reads four root files). The contents API
    follows a symlink to an in-repo file and answers it as a file; the stored mode is the truth, and the scorecard reads the same."""
    try:
        doc = gh.call("GET", f"/repos/{repo}/git/trees/{commit}").json()
    except GitHubError as exc:
        if _transient(exc):
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

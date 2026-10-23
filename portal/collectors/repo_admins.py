"""The repo_admins collector (decision D9): with the read installation, record every installation repo's admins. One run
replaces them all; a failed run changes nothing (spec "Sources and collectors"). Slice 4 grows this into the github collector."""

import re

from ..store.base import RepoAdmin

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


def collect(gh, store, *, now) -> int:
    rows = []
    for repo in gh.paginate("/installation/repositories", key="repositories"):
        full = repo.get("full_name") if isinstance(repo, dict) else None
        if not isinstance(full, str) or not REPO.fullmatch(full) or {".", ".."} & set(full.split("/")):
            continue
        for user in gh.paginate(f"/repos/{full}/collaborators", params={"affiliation": "all", "permission": "admin"}):
            if isinstance(user, dict) and isinstance(user.get("login"), str) and LOGIN.fullmatch(user["login"]) and type(user.get("id")) is int:
                rows.append(RepoAdmin(full, user["login"], user["id"], now))
    store.replace_repo_admins(rows, observed_at=now)
    return len(rows)

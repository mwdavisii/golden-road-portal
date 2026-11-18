"""The decommission's second step (spec 2026-11-02 L7, L9; before the factory PR since item 21): archive the app's repository with the
lifecycle App. Never a platform repository, and only while the App is installed on none of them (decided while planning: item 5). The
calls (item 6): as the App, each platform repository's installation (a 404 each) and the app repository's, then a token for that one
repository with only administration: write (a token granted more or less is revoked unused). The token's calls: GET /repos/{owner}/{repo}
(decided item 22: already archived, the step is done with no PATCH), PATCH /repos/{owner}/{repo} {"archived": true}, then
DELETE /installation/token, on success or failure. Never a repository outside PORTAL_ORG (not_in_org).

A failure fails the step with GitHub's status, method and path (never its text), and Retry repeats it; GitHub not answering (status 0,
5xx, a rate limit) waits for the next pass; the deploy step stays done and
the factory PR waits for it."""

from ..github.client import GitHubError
from ..lifecycle import PLATFORM_REPOS, is_platform_repo
from .config import installation_of, platform_installation, revoke
from .run import _reason


def _waits(exc) -> bool:
    """GitHub didn't answer (status 0), failed itself (5xx) or rate-limited the call: the next pass tries again, nothing failed."""
    return exc.status == 0 or exc.status >= 500 or exc.rate_limited


def archive(store, cfg, req, step, now) -> str:
    from .lifecycle import _done, _fail_step
    app = cfg.lifecycle_app
    if app is None:
        return _fail_step(store, None, cfg, req, step, "no_lifecycle_app", now)
    if is_platform_repo(req.repo, cfg.org):
        return _fail_step(store, None, cfg, req, step, "platform_repo", now)
    owner, name = req.repo.split("/")
    if owner.lower() != cfg.org.lower():
        return _fail_step(store, None, cfg, req, step, "not_in_org", now)
    token = None
    try:
        as_app = app.app_client()
        platform = platform_installation(as_app, cfg.org, PLATFORM_REPOS)
        if platform is not None:
            return _fail_step(store, None, cfg, req, step, f"lifecycle_app_on_platform_repo: {platform}", now)
        token = app.repo_client(installation_of(as_app, f"{owner}/{name}"), name)
        # decided item 22: read first, so a retry after a crash between the PATCH and the record finds it archived and PATCHes nothing
        current = token.call("GET", f"/repos/{owner}/{name}").json()
        if not isinstance(current, dict) or not isinstance(current.get("archived"), bool):
            raise ValueError("no archived flag")
        if not current["archived"]:
            answer = token.call("PATCH", f"/repos/{owner}/{name}", json={"archived": True}).json()
            if not isinstance(answer, dict) or answer.get("archived") is not True:
                raise ValueError("not archived")
    except GitHubError as exc:
        if _waits(exc):
            return f"{req.id}: archive waits: GitHub didn't answer"
        return _fail_step(store, None, cfg, req, step, "archive: " + _reason(exc), now)
    except (KeyError, TypeError, ValueError, AttributeError):  # a JSON decode error is a ValueError
        return _fail_step(store, None, cfg, req, step, "archive: github: unexpected response", now)
    finally:
        if token is not None:
            revoke(token)
    return _done(store, req, step, None, now)

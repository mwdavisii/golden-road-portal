"""An agent definition's pull request (spec 2026-11-17 "Portal changes"; decided while planning: items 26 to 28). agent.yaml on branch
portal/agent-definition in the agent repo, opened or updated in place with the agent App (the write App's installation covers only the
factory and the deploy repo), merged by a person; the writer records the merge once agent.yaml at the merge commit is what it wrote.

The agent App's calls: as the App (its JWT), each platform repository's installation (a 404 each: never while the App is installed on
one), the app repository's installation, GET /app (its bot login), and one token per call for that one repository with exactly contents
and pull requests write (a token granted more or less is revoked unused); the token's own repository list must be that repository alone.
The token reads the repo, its default branch, builder-spec.json and agent.yaml, lists the PRs from the branch, deletes and recreates the
branch from the default branch when no PR of the App's is open, PUTs agent.yaml, and opens or retitles the PR; it is revoked after use
(DELETE /installation/token), on success or failure.

A GitHub failure, no agent App, or the App installed on a platform repo or on all repositories (the platform's to fix) waits for the
next pass with one more attempt and the error (no cap); a refusal fails the request with its reason. Only an older open definition of the same repo is ever superseded (by created_at, then id), never a newer one: an older one's
turn fails it as superseded before anything is written, and the store refusing a move because a newer one exists (newer_definition) is
that too. A PR that merged or closed is the advance's to record, even when a newer definition has opened the next PR."""

import base64
import json

from ..agent_definition import BRANCH, KIND, PATH, agent_yaml, model_ids
from ..agent_file import AgentFileError, load
from ..github.client import GitHubError
from ..lifecycle import PLATFORM_REPOS, is_platform_repo
from ..store.base import OPEN_STATES, Conflict, agent_pr_url_ok
from .bypass import SLUG
from .config import AppSetupProblem, installation_of, platform_installation, revoke
from .recheck import _still_admin
from .render import _code, fenced
from .run import PR_URL, Refused, _default_branch, _fail, _file, _reason, _sha

NO_APP = "no_agent_app: PORTAL_AGENT_APP_ID is not set"
OPEN = ("writing", "pr_open")  # the states the writer moves a definition from (approved ones are claimed into writing first)
NEWER = "newer_definition"  # the store's refusal of a move while a newer open definition exists (0014's guard)


def title(req) -> str:
    return f"Define the agent for {req.repo} (portal request {req.id})"


def pr_body(req, person) -> str:
    name = _code(person.display_name if person else "unknown")
    return "\n".join([
        f"The golden road portal writes this agent's `agent.yaml` (request `{req.id}`), as {name} saved it on the app's Agent page.", "",
        "Review it as you review code: the instructions are what the agent does, and the scorecard reviews them too. Merge it when it is "
        "right; the portal records the merge. Saving again in the portal updates this pull request.", "",
        f"- Display name: {_code(req.final['display_name'])}", f"- Model: {_code(req.final['model'])}", "", "Instructions:", "",
        fenced(req.final["instructions"]), ""])


def _content(req) -> bytes:
    return agent_yaml(req.final, req.id).encode("utf-8")


def _wait(store, req, error, now) -> str:
    store.record_request_error(request_id=req.id, error=error[:300], now=now)
    return f"{req.id}: waits ({error[:120]})"


def _older(a, b) -> bool:
    """a was saved before b."""
    return (a.created_at, a.id) < (b.created_at, b.id)


def _newer(store, req) -> bool:
    """A newer open definition of the same repo exists: it wins, this one is superseded."""
    return any(r.kind == KIND and r.id != req.id and r.state in OPEN_STATES and _older(req, r) for r in store.requests_for_repo(req.repo))


def _supersede(store, req, now) -> str:
    try:
        return _fail(store, req, "superseded", now)
    except Conflict as exc:
        return f"{req.id}: skipped ({exc.code})"


def _refusal(store, cfg, req, now) -> str | None:
    state = store.lifecycle_state(req.repo)
    if state is not None and state.state == "decommissioned":
        return "decommissioned"
    if is_platform_repo(req.repo, cfg.org):
        return "platform_repo"
    if not _still_admin(req, cfg, store, now):
        return "requester_not_admin"
    try:
        load(_content(req), models=model_ids())
    except AgentFileError:
        return "agent_yaml_invalid"
    return None


def _app_type(raw: bytes):
    try:
        spec = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return spec.get("app_type") if isinstance(spec, dict) else None


def _token(cfg, req):
    """(the App's client, a token client for the request's repository alone). Raises AppSetupProblem while the App is installed on a
    platform repo or on all of the account's repositories (the request waits: the platform's to fix); ValueError for a token that
    reaches anything else (revoked first)."""
    as_app = cfg.agent_app.app_client()
    platform = platform_installation(as_app, cfg.org, PLATFORM_REPOS)
    if platform is not None:
        raise AppSetupProblem(f"agent_app_on_platform_repo: {platform}")
    name = req.repo.split("/")[1]
    installation = installation_of(as_app, req.repo, refuse_all="agent_app_on_all_repositories")
    token = cfg.agent_app.repo_client(installation, name)
    try:
        listed = [r.get("full_name") if isinstance(r, dict) else None
                  for r in token.paginate("/installation/repositories", key="repositories", max_pages=None)]
        if [str(r).lower() for r in listed] != [req.repo.lower()]:
            raise ValueError("the token reaches other repositories")
    except BaseException:
        revoke(token)
        raise
    return as_app, token


def _login(as_app, cfg) -> str:
    doc = as_app.call("GET", "/app").json()
    slug = doc.get("slug") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or doc.get("id") != cfg.agent_app.app_id or not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ValueError("not the agent App")
    return f"{slug}[bot]"


def _prs(token, repo, state):
    """Every PR from BRANCH in `state` (every page: no cap), GitHub's pull-request-simple shape (state, head, base, user; merged_at,
    never merged)."""
    owner = repo.split("/")[0]
    prs = list(token.paginate(f"/repos/{repo}/pulls", params={"head": f"{owner}:{BRANCH}", "state": state}, max_pages=None))
    if not all(isinstance(pr, dict) for pr in prs):
        raise ValueError("not a pull request")
    return prs


def _the_app_s(pr, repo, base, login) -> bool:
    author = pr.get("user") or {}
    return ((pr.get("head") or {}).get("ref") == BRANCH and (pr.get("base") or {}).get("ref") == base
            and str(((pr.get("head") or {}).get("repo") or {}).get("full_name")).lower() == repo.lower()
            and author.get("login") == login and author.get("type") == "Bot")


def _open_pr(token, repo, base, login):
    """The agent App's open PR from BRANCH into base, or None. Any other open PR from the branch (someone else's, or into another base)
    is refused (pr_not_ours), before the branch is touched: every page of the open ones is read."""
    found = None
    for pr in token.paginate(f"/repos/{repo}/pulls", params={"head": f"{repo.split('/')[0]}:{BRANCH}", "state": "open"}, max_pages=None):
        if not isinstance(pr, dict):
            raise ValueError("not a pull request")
        if pr.get("state") != "open":
            continue
        if not _the_app_s(pr, repo, base, login) or found is not None:
            raise Refused("pr_not_ours")
        found = pr
    return found


def _merged_already(store, token, req, base, login, content, now) -> str | None:
    """main already holds exactly this request's agent.yaml because a person merged the agent App's PR of it (the writer stopped after
    its PUT, before it recorded the PR): the request is recorded as that PR, merged. None when no such merged PR is found."""
    for pr in _prs(token, req.repo, "closed"):
        merge_sha = pr.get("merge_commit_sha")
        if not pr.get("merged_at") or not _the_app_s(pr, req.repo, base, login) or not isinstance(merge_sha, str):
            continue
        found = _file(token, req.repo, PATH, _sha(merge_sha))
        if found is None or found[1] != content:
            continue
        number, url = pr.get("number"), pr.get("html_url")
        if type(number) is not int or not isinstance(url, str) or not PR_URL.fullmatch(url) or not agent_pr_url_ok(req.repo, number, url):
            raise ValueError("unexpected pull request")
        try:
            store.mark_pr_open(request_id=req.id, branch=BRANCH, pr_number=number, pr_url=url, head_sha=_sha((pr.get("head") or {}).get("sha")),
                               base_sha=_sha((pr.get("base") or {}).get("sha")), now=now)
        except Conflict as exc:
            if exc.code == NEWER:
                return _supersede(store, req, now)
            raise
        store.mark_merged(request_id=req.id, merge_sha=merge_sha, now=now)
        return f"{req.id}: merged"
    return None


def _fresh_branch(token, repo, base_sha) -> None:
    """BRANCH from base_sha: deleted first when it is there (no open PR of the agent App's uses it)."""
    try:
        token.call("GET", f"/repos/{repo}/git/ref/heads/{BRANCH}")
    except GitHubError as exc:
        if exc.status != 404:
            raise
    else:
        token.call("DELETE", f"/repos/{repo}/git/refs/heads/{BRANCH}", ok=(204,))
    token.call("POST", f"/repos/{repo}/git/refs", ok=(201,), json={"ref": f"refs/heads/{BRANCH}", "sha": base_sha})


def _supersede_older(store, req, number, now) -> None:
    """Every older definition of the repo still approved (the store never hands it out once a newer one is open) or writing, or open on
    the PR this one just took over: superseded. Never a newer one, and never one whose PR is another (merged or closed: the advance
    records it)."""
    for older in store.requests_for_repo(req.repo):
        if (older.kind == KIND and older.id != req.id and older.state in ("approved", *OPEN) and _older(older, req)
                and (older.state != "pr_open" or older.pr_number == number)):
            try:
                store.mark_failed(request_id=older.id, reason="superseded", now=now)
            except Conflict:
                pass  # another pass moved it


def sweep(store, now) -> list[str]:
    """Each pass, before the claims: every approved agent definition with a newer open one for its repo fails as superseded (the
    store never hands it out). Never a newer one: the store refuses superseded without a newer open definition."""
    done = []
    for req in store.requests_in(("approved",)):
        if req.kind == KIND and _newer(store, req):
            done.append(_supersede(store, req, now))
    return done


def write(store, gh, cfg, req, now) -> str:
    if _newer(store, req):
        return _supersede(store, req, now)
    refused = _refusal(store, cfg, req, now)
    if refused:
        return _fail(store, req, refused, now)
    if cfg.agent_app is None:
        return _wait(store, req, NO_APP, now)
    token = None
    try:
        as_app, token = _token(cfg, req)
        login = _login(as_app, cfg)
        base = _default_branch(token, req.repo)
        base_sha = _sha(token.call("GET", f"/repos/{req.repo}/git/ref/heads/{base}").json()["object"]["sha"])
        spec = _file(token, req.repo, "builder-spec.json", base_sha)
        if spec is None or _app_type(spec[1]) != "agent":
            return _fail(store, req, "not_an_agent_repo", now)
        content = _content(req)
        on_main = _file(token, req.repo, PATH, base_sha)
        if on_main is not None and on_main[1] == content:
            return _merged_already(store, token, req, base, login, content, now) or _fail(store, req, "already_on_main", now)
        pr = _open_pr(token, req.repo, base, login)
        if pr is None:
            _fresh_branch(token, req.repo, base_sha)
        current = _file(token, req.repo, PATH, BRANCH)
        if current is not None and current[1] == content:  # a retry after a crash between the PUT and the record
            head = _sha(token.call("GET", f"/repos/{req.repo}/git/ref/heads/{BRANCH}").json()["object"]["sha"])
        else:
            put = {"message": title(req), "content": base64.b64encode(content).decode("ascii"), "branch": BRANCH}
            if current is not None:
                put["sha"] = _sha(current[0])
            head = _sha(token.call("PUT", f"/repos/{req.repo}/contents/{PATH}", ok=(200, 201), json=put).json()["commit"]["sha"])
        body = pr_body(req, store.get_person(req.requested_by))
        verb = "updated" if pr is not None else "opened"
        if pr is None:
            pr = token.call("POST", f"/repos/{req.repo}/pulls", ok=(201,), json={"title": title(req), "head": BRANCH, "base": base,
                                                                                "maintainer_can_modify": False, "body": body}).json()
        else:  # a failure waits: the next pass finds the file already there and only sends the title and body again
            token.call("PATCH", f"/repos/{req.repo}/pulls/{pr['number']}", json={"title": title(req), "body": body})
        number, url = pr["number"], pr["html_url"]
        if (type(number) is not int or number < 1 or not isinstance(url, str) or not PR_URL.fullmatch(url)
                or not agent_pr_url_ok(req.repo, number, url)):  # the store keeps only a PR of the request's own repo
            raise ValueError("unexpected pull request")
        try:
            store.mark_pr_open(request_id=req.id, branch=BRANCH, pr_number=number, pr_url=url, head_sha=head, base_sha=base_sha, now=now)
        except Conflict as exc:
            if exc.code == NEWER:  # a newer definition was saved meanwhile: its turn writes the PR again
                return _supersede(store, req, now)
            raise
        _supersede_older(store, req, number, now)
        return f"{req.id}: pull request #{number} {verb}"
    except Refused as exc:
        return _fail(store, req, exc.reason, now)
    except AppSetupProblem as exc:
        return _wait(store, req, exc.reason, now)
    except GitHubError as exc:
        return _wait(store, req, _reason(exc), now)
    except (KeyError, TypeError, ValueError, AttributeError):
        return _wait(store, req, "github: unexpected response", now)
    finally:
        if token is not None:
            revoke(token)


def advance(store, gh, cfg, req, now) -> str:
    """A definition's open PR: merged with what was written (merged), merged with anything else (merged_file_differs), closed unmerged
    (pr_closed), or still open (waiting for review). GET /pulls/{number} is the one PR shape that says merged. A PR merged into another
    branch than the repo's default branch is merged_file_differs: agent.yaml on main isn't what was written."""
    current = store.get_request(req.id)
    if current is None or current.state != "pr_open":
        return f"{req.id}: skipped (no longer open)"
    if cfg.agent_app is None:
        return _wait(store, req, NO_APP, now)
    token = None
    try:
        _, token = _token(cfg, req)
        pr = token.call("GET", f"/repos/{req.repo}/pulls/{req.pr_number}").json()
        if not isinstance(pr, dict) or not isinstance(pr.get("merged"), bool):
            raise ValueError("not a pull request")
        if pr["merged"]:
            if (pr.get("base") or {}).get("ref") != _default_branch(token, req.repo):
                return _fail(store, req, "merged_file_differs", now)
            merge_sha = _sha(pr.get("merge_commit_sha"))
            found = _file(token, req.repo, PATH, merge_sha)
            if found is None or found[1] != _content(req):
                return _fail(store, req, "merged_file_differs", now)
            store.mark_merged(request_id=req.id, merge_sha=merge_sha, now=now)
            return f"{req.id}: merged"
        if pr.get("state") != "open":
            return _fail(store, req, "pr_closed", now)
        return f"{req.id}: waiting for review"
    except Refused as exc:
        return _fail(store, req, exc.reason, now)
    except AppSetupProblem as exc:
        return _wait(store, req, exc.reason, now)
    except GitHubError as exc:
        return _wait(store, req, _reason(exc), now)
    except (KeyError, TypeError, ValueError, AttributeError):
        return _wait(store, req, "github: unexpected response", now)
    finally:
        if token is not None:
            revoke(token)

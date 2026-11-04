"""The commands an admin asks the writer for (spec 2026-11-02 rules 7 and 8), carried out at the start of the request's pass.
- Rule 1 holds for a command too: 0012 lets the web insert one, so a command whose requester is not a portal admin now (their roles at
  their last sign-in) is refused, with a note, and changes nothing.
- retry: the failed step starts again (a new attempt: a PR step on a new branch from the current main); the move, its "retried" note
  and the command finished are one transaction (retry_lifecycle). The steps before it stay done. With no failed step it changes
  nothing, and says so.
- abandon: only before the deploy repo PR merged, by any attempt (a Retry or a rebuild starts a new attempt and forgets the old PR
  number, so the writer asks GitHub for the PRs of every attempt's branch, reading each PR on its own: GitHub's list doesn't say
  whether a PR merged). It first closes every open PR of the request it opened and deletes every attempt's branch, then reads every
  attempt's PR again. It waits while any PR on them is open (anyone's: the writer closes only its own), while a branch is still there
  (a 422 counts as gone only when the branch then reads 404), and while the write App's login is unknown. Only if none merged (a
  reviewer may merge between the first read and the close) are the steps closed and the request failed "abandoned", with its note
  and the command finished, all in one transaction (abandon_lifecycle; the task stays open, for ServiceNow to decide). If a deploy
  PR merged, the command is finished as too late, with a note, and the pass records the merge as usual (a decommission can't be
  undone after the deploy merge).
- stop: only a decommission whose deploy step is done (where an abandon is too late), for an archive or factory step that can't finish
  (the repository gone, the factory PR refused, the app brought back). As an abandon, it closes the writer's open PRs of the steps not
  done and deletes their branches, then reads them again; it waits as an abandon waits. A factory PR that merged unjudged makes it too
  late (the pass records the merge), unless the app is no longer decommissioned by this request. Then the steps not done are closed and
  the request failed "stopped", with its note and the command finished, in one transaction (stop_lifecycle). Nothing is undone: the
  state stays decommissioned, and the task stays open.
A command GitHub can't answer for waits for the next pass. A command the writer doesn't carry out is finished with a "command" note."""

from ..github.client import GitHubError

PR_STEPS = ("deploy", "factory")


def _name(store, person_id) -> str:
    person = store.get_person(person_id)
    return person.display_name if person else "an admin"


def _deploy_merged(gh, cfg, req, steps) -> bool:
    """A deploy PR of any attempt merged on GitHub (the deploy step done counts too). Raises GitHubError or ValueError when GitHub
    doesn't answer as expected."""
    from .lifecycle import _attempt_branches, _prs_on, step_repo
    deploy = next(s for s in steps if s.step == "deploy")
    if deploy.state == "done":
        return True
    repo = step_repo(cfg, deploy)
    return any(pr.get("merged") is True for branch in _attempt_branches(req, deploy) for pr in _prs_on(gh, repo, branch))


def _branch_gone(gh, repo, branch) -> bool:
    """Delete the branch; True once it is gone. A 422 is "already gone" only when the branch then reads as 404 (never made, or deleted
    by a pass that stopped): GitHub answers 422 for a branch a rule protects too."""
    try:
        gh.call("DELETE", f"/repos/{repo}/git/refs/heads/{branch}", ok=(204,))
        return True
    except GitHubError as exc:
        if exc.status == 404:
            return True
        if exc.status != 422:
            raise
    try:
        gh.call("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    except GitHubError as exc:
        if exc.status == 404:
            return True
        raise
    return False


def _close_everything(gh, cfg, req, steps, who, done="Abandoned") -> str | None:
    """Close every open PR the writer opened on any attempt's branch of the given PR steps, and delete those branches. None when every
    branch is gone and no PR on them is open afterwards, the writer's or anyone's (read again); else why the command waits."""
    from .lifecycle import _attempt_branches, _by_the_writer, _close_pr, _prs_on, step_repo
    if cfg.write_login is None:  # fail closed: no PR would read as the writer's, so none would be closed
        return "the write App's login is unknown"
    kept = False
    for step in (s for s in steps if s.step in PR_STEPS):
        repo = step_repo(cfg, step)
        for branch in _attempt_branches(req, step):
            for pr in _prs_on(gh, repo, branch):
                if pr.get("state") == "open" and not pr.get("merged") and _by_the_writer(pr, cfg):
                    _close_pr(gh, repo, pr["number"], f"{done} in the golden road portal by {who}: the writer closes this pull request.")
            kept = not _branch_gone(gh, repo, branch) or kept
    if any(pr.get("state") == "open" for step in steps if step.step in PR_STEPS for branch in _attempt_branches(req, step)
           for pr in _prs_on(gh, step_repo(cfg, step), branch)):
        return "a pull request is still open"
    return "a branch could not be deleted" if kept else None


def carry_out(store, gh, cfg, req, steps, now) -> str | None:
    """None when the pass goes on (no command, a retry, a refused or late command); else what the pass did."""
    from .lifecycle import _still_admin
    command = store.open_lifecycle_command(req.id)
    if command is None:
        return None
    who = _name(store, command.requested_by)
    if not _still_admin(store, command.requested_by):
        store.finish_lifecycle_command(command_id=command.id, now=now, note=f"Refused: {who} asked to {command.command} this request but is "
                                       "not a portal admin (rule 1), so nothing changed.")
        return None
    if command.command == "retry":
        failed = next((s for s in steps if s.state == "failed"), None)
        if failed is None:
            store.finish_lifecycle_command(command_id=command.id, now=now, note=f"Retry asked by {who}, but no step had failed: nothing changed.")
            return None
        store.retry_lifecycle(request_id=req.id, command_id=command.id, step=failed.step, now=now,
                              note=f"Retried by {who}: the {failed.step} step starts again (attempt {failed.attempt + 1}).")
        return None
    if command.command == "stop":
        return _stop(store, gh, cfg, req, steps, command, who, now)
    too_late = (f"Abandon asked by {who} came too late: the deploy repo PR merged, so nothing was abandoned and the "
                f"{req.final['action']} goes on (it can't be undone after the deploy merge).")
    try:
        if _deploy_merged(gh, cfg, req, steps):
            store.finish_lifecycle_command(command_id=command.id, now=now, note=too_late)
            return None
        waits = _close_everything(gh, cfg, req, steps, who)
        if _deploy_merged(gh, cfg, req, steps):  # merged between the first read and the close: recorded as a merge, not abandoned
            store.finish_lifecycle_command(command_id=command.id, now=now, note=too_late)
            return None
    except (GitHubError, ValueError):  # a JSON decode error is a ValueError
        return f"{req.id}: abandon waits: GitHub didn't answer"
    if waits:
        return f"{req.id}: abandon waits: {waits}"
    store.abandon_lifecycle(request_id=req.id, command_id=command.id, now=now,
                            note=f"Abandoned by {who} before the deploy repo PR merged: the writer closed its pull requests and nothing was "
                                 "applied. The task stays open for ServiceNow to decide.")
    return f"{req.id}: abandoned"


def _factory_merged(store, gh, cfg, req, steps) -> bool:
    """A factory PR of any attempt merged on GitHub and the writer hasn't judged it (a failed note of the step names it): the pass
    records it. Raises GitHubError or ValueError when GitHub doesn't answer as expected."""
    from .lifecycle import _attempt_branches, _judged, _prs_on, step_repo
    factory = next(s for s in steps if s.step == "factory")
    repo = step_repo(cfg, factory)
    return any(pr.get("merged") is True and not _judged(store, req, factory, pr.get("html_url"))
               for branch in _attempt_branches(req, factory) for pr in _prs_on(gh, repo, branch))


def _stop(store, gh, cfg, req, steps, command, who, now) -> str | None:
    from .lifecycle import _still_decommissioned
    if req.final["action"] != "decommission" or steps[0].state != "done":
        store.finish_lifecycle_command(command_id=command.id, now=now, note=f"Stop asked by {who}, but only a decommission whose deploy "
                                       "repo PR merged can be stopped: nothing changed. Abandon it instead.")
        return None
    left = [s for s in steps if s.state != "done"]
    too_late = (f"Stop asked by {who} came too late: the factory PR merged, so nothing was stopped and the decommission is recorded as "
                "applied.")
    if not left:
        store.finish_lifecycle_command(command_id=command.id, now=now, note=too_late)
        return None
    try:
        # brought back by hand, the request can't complete: whatever merged, the stop goes on
        late = _still_decommissioned(store, req)
        if late and _factory_merged(store, gh, cfg, req, steps):
            store.finish_lifecycle_command(command_id=command.id, now=now, note=too_late)
            return None
        waits = _close_everything(gh, cfg, req, left, who, "Stopped")
        if late and _factory_merged(store, gh, cfg, req, steps):  # merged between the first read and the close
            store.finish_lifecycle_command(command_id=command.id, now=now, note=too_late)
            return None
    except (GitHubError, ValueError):
        return f"{req.id}: stop waits: GitHub didn't answer"
    if waits:
        return f"{req.id}: stop waits: {waits}"
    archived = any(s.step == "archive" and s.state == "done" for s in steps)
    store.stop_lifecycle(request_id=req.id, command_id=command.id, now=now, note=(
        f"Stopped by {who} after the deploy repo PR merged: the writer does no more steps ({', '.join(s.step for s in left)} closed). "
        f"{req.repo} stays removed from the deploy repo and {'decommissioned' if late else 'brought back'} in the portal. The repository is "
        f"{'archived' if archived else 'not archived'}. The writer did not mark the factory's intake record decommissioned. The task "
        "stays open for ServiceNow to decide."))
    return f"{req.id}: stopped"

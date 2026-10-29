"""Which repos a person administers, as the collector last saw it: registering and the app page use the same rule."""


def admin_repos_of(store, person_id):
    """The repos this person is an admin of, as the collector last saw it, through their linked GitHub logins (by user ID where the
    link has one, by login where an admin made the link without it). One row per repo, sorted."""
    links = store.links_for(person_id)
    rows = store.admin_repos([link.github_user_id for link in links if link.github_user_id is not None])
    rows += store.admin_repos_by_login([link.github_login.lower() for link in links if link.github_user_id is None])
    unique = {r.repo: r for r in rows}
    return [unique[k] for k in sorted(unique, key=str.lower)]


def fresh_admin_repos(store, person_id, now, stale_after):
    """(fresh, stale): the person's admin repos split by whether the collector saw them within stale_after. A repo seen both ways
    is fresh. Nothing is decided on a stale observation: the collector may be down, and a person's rights may have changed."""
    rows = admin_repos_of(store, person_id)
    fresh = [r for r in rows if now - r.observed_at <= stale_after]
    stale = [r for r in rows if now - r.observed_at > stale_after]
    return fresh, stale


def stale_message(stale) -> str:
    last = max(r.observed_at for r in stale).strftime("%Y-%m-%d %H:%M")
    return (f"The portal last saw your repositories at {last} UTC, which is too old to rely on: the collector that reads them from GitHub "
            "may be down. Try again later, or ask an admin.")


def app_repo(store, person_id, is_admin, wanted, now, stale_after):
    """(the repo's name as the portal stores it, whether the person administers it now) for `wanted` (owner/repo in any case), or
    (None, False) when it isn't theirs to see. The repo's admin, as the collector saw it recently, gets the collector's name. A portal
    admin gets the name the collector or a request recorded; a repo the portal has never seen is not found."""
    fresh, _ = fresh_admin_repos(store, person_id, now, stale_after)
    mine = next((r.repo for r in fresh if r.repo.lower() == wanted.lower()), None)
    if mine is not None:
        return mine, True
    if not is_admin:
        return None, False
    return store.observed_repo(wanted) or next((r.repo for r in store.requests_for_repo(wanted)), None), False

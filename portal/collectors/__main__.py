"""`python -m portal.collectors [--once]`: the minimal collector. Reads the read App's credentials and the database URL from the
environment, records the repo admins every PORTAL_COLLECT_INTERVAL seconds, and never prints a credential."""

import datetime as dt
import os
import sys
import time
from pathlib import Path

import httpx

from ..github.appauth import InstallationToken
from ..github.client import GitHub, GitHubError
from ..settings import SettingsError, github_bases
from .repo_admins import collect


def _fail(message: str) -> int:
    print(f"collectors: {message}", file=sys.stderr)
    return 2


def _int(env, name, low=1, high=2**53):
    raw = env.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number") from None
    if not low <= value <= high:
        raise SettingsError(f"{name} must be from {low} to {high}")
    return value


def main(argv=None, env=os.environ) -> int:
    argv = sys.argv[1:] if argv is None else argv
    once = "--once" in argv
    try:
        host = env.get("PORTAL_GITHUB_HOST", "").lower()
        if not host:
            raise SettingsError("PORTAL_GITHUB_HOST is not set")
        app_id = _int(env, "PORTAL_READ_APP_ID") if env.get("PORTAL_READ_APP_ID") else _missing("PORTAL_READ_APP_ID")
        key_file = env.get("PORTAL_READ_APP_KEY_FILE") or _missing("PORTAL_READ_APP_KEY_FILE")
        installation = _int(env, "PORTAL_READ_INSTALLATION_ID") if env.get("PORTAL_READ_INSTALLATION_ID") else _missing("PORTAL_READ_INSTALLATION_ID")
        url = env.get("PORTAL_DATABASE_URL") or _missing("PORTAL_DATABASE_URL")
        interval = _int(env, "PORTAL_COLLECT_INTERVAL", 60, 86400) if env.get("PORTAL_COLLECT_INTERVAL") else 300
        try:
            key = Path(key_file).read_bytes()
        except OSError:
            raise SettingsError("PORTAL_READ_APP_KEY_FILE can't be read") from None
    except SettingsError as exc:
        return _fail(str(exc))
    from ..store.postgres import PostgresStore

    api = github_bases(host)[1]
    http = httpx.Client(timeout=30)
    gh = GitHub(api, http, InstallationToken(http, api, app_id, key, installation))
    store = PostgresStore(url)
    try:
        while True:
            try:
                count = collect(gh, store, now=dt.datetime.now(dt.UTC))
                print(f"collectors: repo_admins: {count} rows", file=sys.stderr)
            except GitHubError as exc:
                print(f"collectors: repo_admins failed: {exc.status} {exc.method} {exc.path}", file=sys.stderr)
                if once:
                    return 1
            if once:
                return 0
            time.sleep(interval)
    finally:
        store.close()


def _missing(name):
    raise SettingsError(f"{name} is not set")


if __name__ == "__main__":
    sys.exit(main())

"""`python -m portal.collectors [--once]`: the minimal collector. Reads the read App's credentials and the database URL from the
environment, and every PORTAL_COLLECT_INTERVAL seconds records the repo admins and computes the evidence of each waiting registration
(PORTAL_ORG names the factory's owner, PORTAL_MODEL_GATEWAY_HOSTS the platform's model gateway hosts). Each pass also observes the
factory's intake records and opens or closes the review-date items (PORTAL_REVIEW_DUE_SOON_DAYS), and reads the scorecard's re-review
events from run artifacts onto the re-review list (PORTAL_REREVIEW_SIGNALS). Without actions: read, the events part is skipped and the
Re-review page says so. It never prints a credential."""

import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from ..github.appauth import InstallationToken
from ..github.client import GitHub, GitHubError
from ..rereview import BadSetting, parse_days, parse_signals
from ..settings import HOST, ORG, SettingsError, github_bases
from .events import ARTIFACT_HOSTS, collect_events
from .evidence import collect_evidence
from .intake import collect_intake
from .repo_admins import collect

logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs each request's URL at INFO, and a storage URL carries a signature


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
        org = env.get("PORTAL_ORG") or _missing("PORTAL_ORG")
        if not ORG.fullmatch(org):
            raise SettingsError("PORTAL_ORG must be a GitHub organization or user name")
        gateway_hosts = _hosts(env.get("PORTAL_MODEL_GATEWAY_HOSTS") or _missing("PORTAL_MODEL_GATEWAY_HOSTS"))
        interval = _int(env, "PORTAL_COLLECT_INTERVAL", 60, 86400) if env.get("PORTAL_COLLECT_INTERVAL") else 300
        due_soon = parse_days("PORTAL_REVIEW_DUE_SOON_DAYS", env.get("PORTAL_REVIEW_DUE_SOON_DAYS", ""))
        signals = parse_signals(env.get("PORTAL_REREVIEW_SIGNALS", ""))
        hosts = artifact_hosts(env.get("PORTAL_ARTIFACT_HOSTS", ""))
        try:
            key = Path(key_file).read_bytes()
        except OSError:
            raise SettingsError("PORTAL_READ_APP_KEY_FILE can't be read") from None
    except (SettingsError, BadSetting) as exc:
        return _fail(str(exc))
    from ..store.postgres import PostgresStore

    api = github_bases(host)[1]
    http = httpx.Client(timeout=30)
    token = InstallationToken(http, api, app_id, key, installation)
    gh = GitHub(api, http, token)
    store = PostgresStore(url)
    try:
        while True:
            failed = False
            for name, part, run in (
                    ("repo_admins", "repo_admins", lambda: f"{collect(gh, store, now=dt.datetime.now(dt.UTC))} rows"),
                    ("evidence", "evidence", lambda: _evidence_note(collect_evidence(gh, store, org=org, gateway_hosts=gateway_hosts,
                                                                         now=dt.datetime.now(dt.UTC)))),
                    ("intake", "intake", lambda: _intake_note(collect_intake(gh, store, org=org, today=dt.datetime.now(dt.UTC).date(),
                                                                   due_soon_days=due_soon, now=dt.datetime.now(dt.UTC)))),
                    ("re-review events", "events", lambda: _events_note(collect_events(gh, store, org=org, signals=signals, now=dt.datetime.now(dt.UTC),
                                                                             actions_readable=actions_readable(token), artifact_hosts=hosts)))):
                ok = _part(name, run)
                _status(store, part, ok, interval)
                failed = not ok or failed
            if once:
                return 1 if failed else 0
            time.sleep(interval)
    finally:
        store.close()


def _part(name, run) -> bool:
    """One part of a pass: its line, or why it failed. Any failure is logged (GitHub's by status, method and path; anything else by its
    type only: a message may carry what it read) and the next part still runs, so one bad input never stops the others."""
    try:
        print(f"collectors: {name}: {run()}", file=sys.stderr)
        return True
    except GitHubError as exc:
        print(f"collectors: {name} failed: {exc.status} {exc.method} {exc.path}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - logged by type and the pass goes on
        print(f"collectors: {name} failed: {type(exc).__name__}", file=sys.stderr)
    return False


def _status(store, part, ok, interval) -> None:
    """The part's line on the Re-review page: the parts with no status of their own (repo admins, evidence) record that
    they ran and the collector's interval, so the page can tell a stopped collector; a part that raised is "failed". Best effort: the
    store may be what failed."""
    try:
        if not ok:
            store.set_rereview_status(source=part, problem="failed", counts={}, now=dt.datetime.now(dt.UTC))
        elif part in ("repo_admins", "evidence"):
            store.set_rereview_status(source=part, problem=None, counts={"interval_seconds": interval}, now=dt.datetime.now(dt.UTC))
    except Exception as exc:  # noqa: BLE001 - logged by type; the next part still runs
        print(f"collectors: {part} status not recorded: {type(exc).__name__}", file=sys.stderr)


def _evidence_note(result) -> str:
    note = f" ({result['problem']})" if result.get("problem") else " (GitHub is busy: will retry)" if result.get("transient") else ""
    if result.get("malformed"):
        note += f" ({result['malformed']} skipped: GitHub's answer was not the documented shape)"
    return f"{result['computed']} computed, {result['waiting']} waiting{note}"


def actions_readable(token):
    """Whether the read installation may read Actions (spec R10): GitHub returns each token's permissions, and InstallationToken keeps them.
    Checked on every pass, so granting the permission takes effect without a restart. None until a token has been fetched: then the
    calls themselves say (a 403 is actions_unreadable)."""
    permissions = getattr(token, "permissions", None)
    if not permissions:
        return None
    return permissions.get("actions") in ("read", "write")


def artifact_hosts(raw) -> tuple:
    """PORTAL_ARTIFACT_HOSTS: the storage hosts an artifact download may be redirected to, comma-separated, each a name or *.suffix.
    Empty is github.com's (*.blob.core.windows.net)."""
    hosts = tuple(h.strip().lower() for h in (raw or "").split(",") if h.strip())
    if not hosts:
        return ARTIFACT_HOSTS
    if len(hosts) > 20 or not all(HOST.fullmatch(h[2:] if h.startswith("*.") else h) for h in hosts):
        raise SettingsError("PORTAL_ARTIFACT_HOSTS must list 1 to 20 host names, each a name or *.suffix")
    return hosts


def _intake_note(result) -> str:
    if result.get("problem"):
        return f"{result['problem']} (the read App can't read the factory)"
    if result.get("transient"):
        return "GitHub is busy: will retry"
    if result.get("held"):
        return f"no intake records in the factory: kept the {result['held']} it had, and their items"
    return f"{result['records']} records ({result['unreadable']} unreadable), {result['opened']} opened, {result['closed']} closed"


def _events_note(result) -> str:
    note = f"{result['stored']} stored, {result['skipped']} skipped"
    if result.get("problem") == "actions_unreadable":
        note += " (actions_unreadable: give the read App actions: read)"
    if result.get("transient"):
        note += " (GitHub is busy: will retry)"
    return note


def _hosts(raw):
    hosts = [h.strip() for h in raw.split(",")]
    if not 1 <= len(hosts) <= 20:
        raise SettingsError("PORTAL_MODEL_GATEWAY_HOSTS must list 1 to 20 host names")
    for host in hosts:
        if not HOST.fullmatch(host):
            raise SettingsError(f"PORTAL_MODEL_GATEWAY_HOSTS has a bad host name: {host!r}")
    return tuple(hosts)


def _missing(name):
    raise SettingsError(f"{name} is not set")


if __name__ == "__main__":
    sys.exit(main())

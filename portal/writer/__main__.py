"""`python -m portal.writer [--once]`: the only process that writes to git (P17). It checks the write App's bypass at startup and
refuses to run if it covers more than the plan allows, and checks it again at the start of every pass, writing nothing in a pass
whose check finds a problem. Then it claims approved requests, writes, opens PRs and merges after checks. It holds the write
App's key and prints neither it nor any token.

Exit codes: 0 after `--once`, 2 for a missing or bad setting or key, 3 for a refusal to start (the bypass check found a problem)."""

import argparse
import datetime as dt
import os
import re
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from ..github.appauth import InstallationToken, app_jwt
from ..github.client import GitHub
from ..settings import ORG, SettingsError, github_bases, no_secret_server_url, parse_role_groups
from .bypass import check_bypass
from .config import WriterConfig
from .run import run_once

CHECK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,99}")
REFUSED, BAD_SETTING = 3, 2


def _say(message: str) -> None:
    print(f"writer: {message}", file=sys.stderr)


def _need(env, name):
    value = env.get(name, "")
    if not value:
        raise SettingsError(f"{name} is not set")
    return value


def _int(env, name, low=1, high=2**53, default=None):
    raw = env.get(name, "")
    if not raw and default is not None:
        return default
    try:
        value = int(_need(env, name))
    except ValueError:
        raise SettingsError(f"{name} must be a whole number") from None
    if not low <= value <= high:
        raise SettingsError(f"{name} must be from {low} to {high}")
    return value


def _key(path: str) -> bytes:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise SettingsError("PORTAL_WRITE_APP_KEY_FILE can't be read") from None
    try:
        loaded = load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):  # never the file's text, whatever the library says
        raise SettingsError("PORTAL_WRITE_APP_KEY_FILE does not hold a usable PEM private key") from None
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise SettingsError("PORTAL_WRITE_APP_KEY_FILE must hold an RSA private key (a GitHub App's)") from None
    return raw


def _check_list(env, name, required) -> tuple[str, ...]:
    """A target's checks that must pass before a merge: `required` always, and more may be added."""
    names = tuple(c.strip() for c in env.get(name, required).split(",") if c.strip()) or (required,)
    if required not in names or not all(CHECK_NAME.fullmatch(n) for n in names):
        raise SettingsError(f"{name} must be a comma-separated list of check names that includes {required}")
    return names


def _checks(env) -> tuple[str, ...]:
    """The factory's checks (PORTAL_WRITER_REQUIRED_CHECKS): factory-check stays."""
    return _check_list(env, "PORTAL_WRITER_REQUIRED_CHECKS", "factory-check")


def _deploy_checks(env) -> tuple[str, ...]:
    """golden-road-deploy's checks (PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS): deploy-check stays."""
    return _check_list(env, "PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS", "deploy-check")


def _directory(env):
    """PORTAL_DIRECTORY, as the web reads it: unset (bindings are refused), or mock (portal/mock_directory.json)."""
    value = env.get("PORTAL_DIRECTORY", "")
    if value == "":
        return None
    if value != "mock":
        raise SettingsError("PORTAL_DIRECTORY must be mock or unset (the only directory there is is the mock)")
    from ..directory import DirectoryError, load_mock
    try:
        return load_mock()
    except DirectoryError:
        raise SettingsError("PORTAL_DIRECTORY is mock, but the mock directory is not valid") from None


def _secret_server(env):
    """PORTAL_SECRET_SERVER, as the web reads it: unset, or mock (portal/mock_secret_server.json). PORTAL_SECRET_SERVER_URL must
    stay unset: there is no real Secret Server."""
    no_secret_server_url(env.get("PORTAL_SECRET_SERVER_URL", ""))
    value = env.get("PORTAL_SECRET_SERVER", "")
    if value == "":
        return None
    if value != "mock":
        raise SettingsError("PORTAL_SECRET_SERVER must be mock or unset (the only Secret Server there is is the mock)")
    from ..secretserver_mock import MockSecretServerError, load_mock
    try:
        return load_mock()
    except MockSecretServerError:
        raise SettingsError("PORTAL_SECRET_SERVER is mock, but the mock Secret Server is not valid") from None


def _load(env):
    host = _need(env, "PORTAL_GITHUB_HOST").lower()
    org = _need(env, "PORTAL_ORG")
    if not ORG.fullmatch(org) or len(org) > 39:
        raise SettingsError("PORTAL_ORG is not a GitHub organization name")
    role_groups = parse_role_groups(_need(env, "PORTAL_ROLE_GROUPS"))
    app_id, installation = _int(env, "PORTAL_WRITE_APP_ID"), _int(env, "PORTAL_WRITE_INSTALLATION_ID")
    interval = _int(env, "PORTAL_WRITER_INTERVAL", 5, 3600, default=30)
    url = _need(env, "PORTAL_DATABASE_URL")
    key = _key(_need(env, "PORTAL_WRITE_APP_KEY_FILE"))
    stale = dt.timedelta(seconds=_int(env, "PORTAL_REPO_ADMINS_STALE_AFTER", 60, 7 * 86400, default=600))  # the web's limit and default
    cfg = WriterConfig(org=org, role_groups=role_groups, app_id=app_id, factory_checks=_checks(env), deploy_checks=_deploy_checks(env),
                       directory=_directory(env), secret_server=_secret_server(env),
                       repo_admins_stale_after=stale)
    return cfg, key, installation, github_bases(host)[1], url, interval


def main(argv=None, env=os.environ, *, http=None, store=None, sleep=time.sleep, clock=lambda: dt.datetime.now(dt.UTC)) -> int:
    parser = argparse.ArgumentParser(prog="python -m portal.writer")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        cfg, key, installation, api, url, interval = _load(env)
    except SettingsError as exc:
        _say(str(exc))
        return BAD_SETTING
    http = http or httpx.Client(timeout=30)
    as_app = GitHub(api, http, lambda: app_jwt(cfg.app_id, key, int(time.time())))
    as_installation = GitHub(api, http, InstallationToken(http, api, cfg.app_id, key, installation))
    if store is None:
        from ..store.postgres import PostgresStore
        store = PostgresStore(url)
    first = True
    try:
        while True:
            problems = check_bypass(as_app, as_installation, installation, cfg)
            if problems and first:
                _say("refusing to start: the write App can do more than the plan allows, or its limits can't be read:")
                for problem in problems:
                    _say(f"  - {problem}")
                return REFUSED
            if problems:  # the rulesets changed since the last pass: write nothing until they are right again
                _say("skipping this pass, the write App's limits are wrong now:")
                for problem in problems:
                    _say(f"  - {problem}")
            else:
                for line in run_once(store, as_installation, cfg, clock()):
                    _say(line)
            first = False
            if args.once:
                return 0 if not problems else REFUSED
            sleep(interval)
    finally:
        close = getattr(store, "close", None)
        if close:
            close()


if __name__ == "__main__":
    sys.exit(main())

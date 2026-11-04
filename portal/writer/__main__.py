"""`python -m portal.writer [--once]`: the only process that writes to git (P17). It checks the write App's bypass at startup and
refuses to run if it covers more than the plan allows, and checks it again at the start of every pass, writing nothing in a pass
whose check finds a problem. Then it claims approved requests, writes, opens PRs and merges after checks. It holds the write
App's key. It also holds the lifecycle App's key (spec 2026-11-02 L9; optional: without it, a decommission is refused), and prints
neither key nor any token.

Exit codes: 0 after `--once`, 2 for a missing or bad setting or key, 3 for a refusal to start (the bypass check found a problem)."""

import argparse
import dataclasses as dc
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
from ..settings import (ORG, SettingsError, cloudflare_from, github_bases, mcp_gateways_from, no_cloudflare_account, no_secret_server_url,
                        parse_role_groups, skills_catalog_from)
from .bypass import check_bypass, write_login
from .config import LifecycleApp, WriterConfig
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


def _key(path: str, name: str = "PORTAL_WRITE_APP_KEY_FILE") -> bytes:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise SettingsError(f"{name} can't be read") from None
    try:
        loaded = load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):  # never the file's text, whatever the library says
        raise SettingsError(f"{name} does not hold a usable PEM private key") from None
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise SettingsError(f"{name} must hold an RSA private key (a GitHub App's)") from None
    return raw


def _lifecycle_app(env, api):
    """PORTAL_LIFECYCLE_APP_ID and PORTAL_LIFECYCLE_APP_KEY_FILE (spec 2026-11-02 L9): both set, or neither (no lifecycle App: a
    decommission is refused, suspend and resume go on). main gives it the writer's HTTP client."""
    app_id, path = env.get("PORTAL_LIFECYCLE_APP_ID", ""), env.get("PORTAL_LIFECYCLE_APP_KEY_FILE", "")
    if not app_id and not path:
        return None
    if not app_id or not path:
        raise SettingsError("PORTAL_LIFECYCLE_APP_ID and PORTAL_LIFECYCLE_APP_KEY_FILE are set together, or neither")
    return LifecycleApp(_int(env, "PORTAL_LIFECYCLE_APP_ID"), _key(path, "PORTAL_LIFECYCLE_APP_KEY_FILE"), api)


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


def _cloudflare(env):
    """PORTAL_CLOUDFLARE and the two gateways, as the web reads them: (unset or the mock serving those hosts, {env: host}).
    PORTAL_CLOUDFLARE_ACCOUNT_ID must stay unset: there is no real Cloudflare account."""
    no_cloudflare_account(env.get("PORTAL_CLOUDFLARE_ACCOUNT_ID", ""))
    gateways = mcp_gateways_from(env)
    return cloudflare_from(env.get("PORTAL_CLOUDFLARE", ""), gateways), gateways


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
    cloudflare, gateways = _cloudflare(env)
    api = github_bases(host)[1]
    cfg = WriterConfig(org=org, role_groups=role_groups, app_id=app_id, factory_checks=_checks(env), deploy_checks=_deploy_checks(env),
                       directory=_directory(env), secret_server=_secret_server(env),
                       repo_admins_stale_after=stale, cloudflare=cloudflare, mcp_gateways=gateways,
                       skills_catalog=skills_catalog_from(env.get("PORTAL_SKILLS_CATALOG", "")),
                       lifecycle_app=_lifecycle_app(env, api))
    if cfg.lifecycle_app is not None and cfg.lifecycle_app.app_id == app_id:
        raise SettingsError("PORTAL_LIFECYCLE_APP_ID must be another App than PORTAL_WRITE_APP_ID: the write App never archives")
    return cfg, key, installation, api, url, interval


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
    if cfg.lifecycle_app is not None:
        cfg = dc.replace(cfg, lifecycle_app=dc.replace(cfg.lifecycle_app, http=http))
    as_app = GitHub(api, http, lambda: app_jwt(cfg.app_id, key, int(time.time())))
    as_installation = GitHub(api, http, InstallationToken(http, api, cfg.app_id, key, installation))
    if store is None:
        from ..store.postgres import PostgresStore
        store = PostgresStore(url)
    first = True
    try:
        login = write_login(as_app, installation, cfg)
        if login is None:
            _say("refusing to start: the write App's installation doesn't name the App (its app_id and app_slug can't be read)")
            return REFUSED
        cfg = dc.replace(cfg, write_login=login)
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

"""Shared fixtures for the portal's tests. The portal root goes on sys.path so `import portal` works from this repo's suite
and from the portal's own repo."""

import dataclasses
import sys
from pathlib import Path

import pytest

PORTAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PORTAL_ROOT))

try:
    import fastapi, httpx, jinja2, jwt, psycopg, yaml  # noqa: E401,F401
except ImportError as exc:  # a missing dependency must fail loudly, never skip the portal's tests
    raise pytest.UsageError(
        f"the portal tests need the portal's pinned dependencies ({exc.name} is missing). Install them with: "
        "uv pip install --python .venv/bin/python --require-hashes -r bootstrap/golden-road-portal/requirements-test.txt") from exc

from fastapi.testclient import TestClient  # noqa: E402
from portal_fakes import BASE, GROUPS, ISSUER, PRINCIPALS, T0, make_settings, seed  # noqa: E402,F401



@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.docker)])
def store(request):
    if request.param == "memory":
        from portal.store.memory import MemoryStore
        yield MemoryStore()
        return
    dsn = request.getfixturevalue("pg_db")
    from portal.store.postgres import PostgresStore
    s = PostgresStore(dsn("portal_owner"))
    yield s
    s.close()


@dataclasses.dataclass
class Portal:
    app: object
    store: object
    clock: object
    settings: object
    fakes: dict
    people: dict
    tokens: dict
    csrfs: dict
    seeds: dict

    def client(self, who="anonymous"):
        client = TestClient(self.app, base_url=BASE, follow_redirects=False)
        if self.tokens.get(who):
            from portal.web.cookies import SESSION_COOKIE
            client.cookies.set(SESSION_COOKIE, self.tokens[who], domain="portal.example.test")
        return client


@pytest.fixture
def portal():
    import httpx
    from portal_fakes import Clock, FakeGitHub, FakeIdP, fake_directory, transport
    from portal.roles import roles_for
    from portal.store.memory import MemoryStore
    from portal.web.app import create_app

    store, clock, settings = MemoryStore(), Clock(T0), make_settings(directory=fake_directory())
    fakes = {"idp": FakeIdP(ISSUER), "github": FakeGitHub()}
    http = httpx.Client(transport=transport(*fakes.values()))
    app = create_app(settings=settings, store=store, http=http, clock=clock)
    people, tokens, csrfs = {}, {}, {}
    for who, groups in GROUPS.items():
        if groups is None:
            continue
        person, token, csrf = store.sign_in(issuer=ISSUER, subject=f"sub-{who}", display_name=who.title(), email=f"{who}@example.test",
                                            groups=groups, roles=roles_for(groups, settings.role_groups), now=clock(), max_age=settings.session_max)
        people[who], tokens[who], csrfs[who] = person, token, csrf
    return Portal(app, store, clock, settings, fakes, people, tokens, csrfs, seed(store, people, clock()))


from portal_pg import pg_db, pg_server  # noqa: F401,E402

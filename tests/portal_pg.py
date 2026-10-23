"""A throwaway Postgres for the docker-marked portal tests: one container per session, one database per test, the portal's
roles created the way a DBA would (README), and the migrations applied as the schema owner."""

import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql

PG_IMAGE = "docker.io/library/postgres@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb"
ROLES = ("portal_owner", "portal_web", "portal_writer", "portal_collector")


@dataclass(frozen=True)
class Server:
    host: str
    port: int

    def dsn(self, user="postgres", db="postgres"):
        return f"postgresql://{user}:test@{self.host}:{self.port}/{db}"


@pytest.fixture(scope="session")
def pg_server():
    assert re.fullmatch(r"docker\.io/library/postgres@sha256:[0-9a-f]{64}", PG_IMAGE), "pin PG_IMAGE by digest"
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    name = f"portal-pg-{uuid.uuid4().hex[:10]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test", "-p", "127.0.0.1::5432", PG_IMAGE],
                   check=True, capture_output=True)
    try:
        mapped = subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True).stdout.split()[0]
        host, port = mapped.rsplit(":", 1)
        server, deadline = Server(host, int(port)), time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(server.dsn(), connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        with psycopg.connect(server.dsn(), autocommit=True) as conn:
            for role in ROLES:
                conn.execute(sql.Composed([sql.SQL("CREATE ROLE "), sql.Identifier(role), sql.SQL(" LOGIN PASSWORD 'test'")]))
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def pg_db(pg_server):
    from portal.migrate import apply
    name = f"portal_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
        conn.execute(sql.Composed([sql.SQL("CREATE DATABASE "), sql.Identifier(name), sql.SQL(" OWNER portal_owner")]))
    apply(pg_server.dsn("portal_owner", name))
    yield lambda role="portal_owner": pg_server.dsn(role, name)
    with psycopg.connect(pg_server.dsn(), autocommit=True) as conn:
        conn.execute(sql.Composed([sql.SQL("DROP DATABASE "), sql.Identifier(name), sql.SQL(" WITH (FORCE)")]))

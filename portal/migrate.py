"""Apply portal/migrations/NNNN_*.sql in order as the schema owner (python -m portal.migrate, PORTAL_MIGRATE_DATABASE_URL).
Each applied file's SHA-256 is recorded, and a file that changed after it was applied stops the run: write a new migration."""

import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class MigrationChanged(Exception):
    pass


def migrations() -> list[Path]:
    return sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def apply(conninfo: str) -> list[str]:
    done_now = []
    with psycopg.connect(conninfo) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migration (version text PRIMARY KEY, sha256 text NOT NULL, "
                     "applied_at timestamptz NOT NULL DEFAULT now())")
        conn.execute("LOCK TABLE schema_migration IN EXCLUSIVE MODE")
        applied = dict(conn.execute("SELECT version, sha256 FROM schema_migration").fetchall())
        for path in migrations():
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if path.stem in applied:
                if applied[path.stem] != digest:
                    raise MigrationChanged(f"migration {path.stem} changed after it was applied")
                continue
            conn.execute(text)
            conn.execute("INSERT INTO schema_migration (version, sha256) VALUES (%s, %s)", (path.stem, digest))
            done_now.append(path.stem)
    return done_now


if __name__ == "__main__":
    url = os.environ.get("PORTAL_MIGRATE_DATABASE_URL", "")
    if not url:
        sys.exit("PORTAL_MIGRATE_DATABASE_URL is not set")
    print("\n".join(apply(url)) or "nothing to apply")

#!/usr/bin/env python
"""
KLASSER AI - migration runner.

Applies every .sql file in migrations/ in filename order, once each, inside a
transaction. Applied migrations are recorded in schema_migrations, so re-running
is safe and only new files execute.

    python migrate.py            # apply pending migrations
    python migrate.py --status   # list applied / pending, change nothing
    python migrate.py --dry-run  # show what would run, change nothing
"""

import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

import keys
from models.database import normalise_dsn, ssl_setting

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def migration_files() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"No migrations directory at {MIGRATIONS_DIR}")
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def main(status_only: bool = False, dry_run: bool = False) -> int:
    if keys.missing(keys.REQUIRED_FOR_DB):
        print("DATABASE_URL is not set.")
        print("Copy .env.example to .env and fill in your Supabase connection string.")
        return 1

    files = migration_files()
    if not files:
        print("No migration files found.")
        return 0

    # Must honour DATABASE_SSL like every other connection. Connecting with TLS
    # regardless meant migrations failed with a connection reset on a network
    # that blocks TLS on 5432, while the rest of the app worked fine.
    conn = await asyncpg.connect(
        normalise_dsn(keys.DATABASE_URL),
        statement_cache_size=0,
        ssl=ssl_setting())
    try:
        await conn.execute(TRACKING_TABLE)

        applied = {
            r["filename"]: r["checksum"]
            for r in await conn.fetch("SELECT filename, checksum FROM schema_migrations")
        }

        pending = [f for f in files if f.name not in applied]

        # A migration that changed after being applied is almost always a mistake -
        # the database no longer matches the file. Report it rather than silently
        # ignoring it.
        drifted = [
            f for f in files
            if f.name in applied and applied[f.name] != checksum(f)
        ]

        if status_only or dry_run:
            for f in files:
                mark = "pending" if f.name in {p.name for p in pending} else "applied"
                if f in drifted:
                    mark = "APPLIED, FILE CHANGED SINCE"
                print(f"  [{mark}] {f.name}")
            if drifted:
                print("\nWarning: files above marked FILE CHANGED no longer match what")
                print("was applied. Write a new migration rather than editing an old one.")
            return 0

        if drifted:
            print("Refusing to run - these migrations changed after being applied:")
            for f in drifted:
                print(f"  {f.name}")
            print("\nWrite a new migration instead of editing an applied one.")
            print("If this is a throwaway dev database, drop it and migrate from scratch.")
            return 1

        if not pending:
            print(f"Up to date - {len(applied)} migration(s) already applied.")
            return 0

        for path in pending:
            sql = path.read_text(encoding="utf-8")
            print(f"Applying {path.name} ...", end=" ", flush=True)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                    path.name,
                    checksum(path),
                )
            print("ok")

        print(f"\nApplied {len(pending)} migration(s).")
        return 0

    finally:
        await conn.close()


if __name__ == "__main__":
    args = set(sys.argv[1:])
    try:
        code = asyncio.run(
            main(status_only="--status" in args, dry_run="--dry-run" in args)
        )
    except asyncpg.PostgresError as exc:
        print(f"\nMigration failed: {exc}")
        code = 1
    sys.exit(code)

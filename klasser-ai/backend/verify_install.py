"""
KLASSER AI - post-migration verification.

Checks the things a column listing cannot show: constraints, the generated
column, RLS policies, seed data, and the sandbox school. Run after migrate.py.

    python verify_install.py

Exit code 0 if everything expected is present, 1 otherwise.
"""

import asyncio
import sys

import asyncpg

import keys
from models.database import normalise_dsn

EXPECTED_TABLES = 66
# 003_seed_settings.sql: 5 model + 13 task + 4 validator + 3 quorum + 3 retry
#                        + 16 pricing + 1 duties + 6 billing + 2 dev/sandbox
EXPECTED_SETTINGS = 53
EXPECTED_PACKAGES = 5

# Tables that must carry RLS, from DATABASE.md plus the parent-scoped and
# identity tables added in 002_rls.sql.
RLS_SAMPLE = [
    "campuses", "teachers", "students", "timetables", "timetable_entries",
    "transport_schedule", "duty_assignments", "generation_attempts",
    "schools", "users", "invites", "settings", "api_keys",
]

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def main() -> int:
    if keys.missing(keys.REQUIRED_FOR_DB):
        print("DATABASE_URL is not set - fill it in .env first.")
        return 1

    conn = await asyncpg.connect(normalise_dsn(keys.DATABASE_URL), statement_cache_size=0)
    try:
        # --- Tables ----------------------------------------------------------
        n = await conn.fetchval("""
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
              AND table_name <> 'schema_migrations'
        """)
        check(n == EXPECTED_TABLES, "tables", f"{n} found, expected {EXPECTED_TABLES}")

        gone = await conn.fetchval("SELECT to_regclass('public.blocks') IS NULL")
        check(gone, "old 'blocks' table removed")

        # --- Constraints -----------------------------------------------------
        pks = await conn.fetchval("""
            SELECT count(*) FROM information_schema.table_constraints
            WHERE table_schema = 'public' AND constraint_type = 'PRIMARY KEY'
        """)
        check(pks >= EXPECTED_TABLES - 1, "primary keys", f"{pks} found")

        fks = await conn.fetchval("""
            SELECT count(*) FROM information_schema.table_constraints
            WHERE table_schema = 'public' AND constraint_type = 'FOREIGN KEY'
        """)
        check(fks >= 140, "foreign keys", f"{fks} found, expected ~149")

        # The generated column is the whole point of the teacher name split.
        gen = await conn.fetchval("""
            SELECT is_generated FROM information_schema.columns
            WHERE table_name = 'teachers' AND column_name = 'full_name'
        """)
        check(gen == "ALWAYS", "teachers.full_name is generated", f"is_generated={gen}")

        # --- RLS -------------------------------------------------------------
        unprotected = await conn.fetch("""
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND c.relname = ANY($1::text[]) AND NOT c.relrowsecurity
        """, RLS_SAMPLE)
        check(not unprotected, "RLS enabled on sampled tables",
              f"missing on: {[r['relname'] for r in unprotected]}" if unprotected else "")

        policies = await conn.fetchval(
            "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        check(policies > 0, "RLS policies exist", f"{policies} policies")

        helper = await conn.fetchval("SELECT to_regprocedure('public.current_school_id()') IS NOT NULL")
        check(helper, "current_school_id() helper present")

        # --- Seed data -------------------------------------------------------
        s = await conn.fetchval("SELECT count(*) FROM settings")
        check(s >= EXPECTED_SETTINGS, "settings seeded", f"{s} rows, expected {EXPECTED_SETTINGS}")

        dupe_models = await conn.fetchval(
            "SELECT count(*) FROM settings WHERE key LIKE 'model_%'")
        check(dupe_models == 5, "model_* settings", f"{dupe_models} rows, expected 5")

        invoiced = await conn.fetchval(
            "SELECT value FROM settings WHERE key = 'credit_invoiced_rate_cents'")
        check(invoiced == "230", "credit_invoiced_rate_cents", f"= {invoiced}")

        p = await conn.fetchval("SELECT count(*) FROM credit_packages")
        check(p == EXPECTED_PACKAGES, "credit packages", f"{p} rows")

        # --- Sandbox school --------------------------------------------------
        school = await conn.fetchrow("SELECT id, name FROM schools WHERE name = 'Westfield College'")
        check(school is not None, "sandbox school seeded")

        if school:
            sid = school["id"]
            for table, expected, label in [
                ("campuses", 2, "campuses"),
                ("rooms", 12, "rooms"),
                ("teachers", 14, "teachers"),
                ("students", 48, "students"),
                ("subject_settings", 8, "subjects"),
                ("buses", 3, "buses"),
                ("routes", 2, "routes"),
            ]:
                got = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE school_id = $1", sid)
                check(got == expected, f"sandbox {label}", f"{got}, expected {expected}")

            periods = await conn.fetchval(
                "SELECT count(*) FROM layout_periods WHERE school_id = $1", sid)
            check(periods == 40, "sandbox layout periods", f"{periods}, expected 40 (5 days x 8)")

            slots = await conn.fetchval(
                "SELECT count(*) FROM layout_duty_slots WHERE school_id = $1", sid)
            check(slots == 30, "sandbox duty slots", f"{slots}, expected 30")

            exempt = await conn.fetchval(
                "SELECT count(*) FROM teacher_duty_exemptions WHERE school_id = $1", sid)
            check(exempt == 1, "sandbox duty exemption", f"{exempt}, expected 1")

            enrol = await conn.fetchval(
                "SELECT count(*) FROM student_subjects WHERE school_id = $1", sid)
            check(enrol >= 48 * 3, "sandbox subject enrolments", f"{enrol}")

            pointer = await conn.fetchval(
                "SELECT value FROM settings WHERE key = 'sandbox_school_id'")
            check(pointer == str(sid), "sandbox_school_id setting points at it")

        return report()

    finally:
        await conn.close()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = 0
    for ok, message in results:
        if not ok:
            failed += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    if failed:
        print(f"{failed} of {len(results)} checks failed.")
        return 1
    print(f"All {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except asyncpg.PostgresError as exc:
        print(f"Verification failed: {exc}")
        sys.exit(1)

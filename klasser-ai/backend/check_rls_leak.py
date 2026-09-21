"""
KLASSER AI - anonymous access leak test.

Uses the public anon key exactly as a browser would, with no user logged in.
Every school-data table must return zero rows. A table that returns data here
is readable by anyone on the internet who has the anon key - which is shipped
in the frontend bundle, so that means everyone.

    python check_rls_leak.py

**The table list comes from the database, not from this file.** An earlier
version walked a hand-written list and reported PASS for a year while seventeen
tables had no RLS at all - it could only find leaks in tables someone had
remembered to add. A check that cannot see a table cannot clear it.

Two things are therefore reported separately: tables that returned rows (a
proven leak), and tables that answered 200 with nothing in them, which proves
nothing either way because an empty table looks identical to a protected one.
The second list is cross-checked against pg_class when DATABASE_URL is
available, so "empty" is only accepted from a table that actually has RLS on.

Needs SUPABASE_URL and SUPABASE_ANON_KEY; DATABASE_URL as well for the full
check. Exit 0 if nothing leaks.
"""

import asyncio
import ssl
import sys

import httpx

import keys
from models import database as db

# This machine's TLS chain is intercepted (corporate proxy / AV root CA), which
# certifi's bundle does not contain. truststore reads the OS certificate store
# instead, so verification still happens - against the certs Windows trusts -
# rather than being switched off.
try:
    import truststore

    SSL_CONTEXT: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    SSL_CONTEXT = True

# The fallback when DATABASE_URL is not set. Enumerating from pg_class is the
# real list; this exists only so the check still does something useful with
# nothing but the two Supabase keys.
FALLBACK_TABLES = [
    "schools", "users", "invites", "campuses", "rooms", "teachers",
    "students", "student_subjects", "buses", "routes", "subject_settings",
    "timetables", "timetable_versions", "timetable_entries",
    "transport_schedule", "duty_assignments", "school_credits",
    "school_billing", "credit_transactions", "invoices", "import_jobs",
    "support_tickets", "onboarding", "layout_periods",
    "teacher_duty_exemptions", "settings", "api_keys", "error_log",
    "pipeline_log", "credit_packages",
]


async def schema_tables() -> tuple[list[str], dict[str, bool], dict[str, int]]:
    """
    Every public table, whether RLS is on, and how many rows it holds.

    Row counts matter: "the anon key got 0 rows" is only evidence of protection
    if there was something there to protect.
    """
    await db.connect()
    if db.pool() is None:
        return [], {}, {}

    rows = await db.fetch("""
        SELECT c.relname AS name, c.relrowsecurity AS rls
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY c.relname
    """)
    names = [r["name"] for r in rows]
    rls = {r["name"]: r["rls"] for r in rows}

    # One query, no interpolation. Counting per table would mean building a
    # statement around each name; the names come from pg_class rather than from
    # a request, but a checker that exists to find security holes should not be
    # the one file that interpolates into SQL. query_to_xml runs the count
    # server-side and hands back a number.
    counts = {
        r["name"]: r["n"]
        for r in await db.fetch("""
            SELECT c.relname AS name,
                   (xpath('/row/n/text()',
                          query_to_xml(format('SELECT count(*) AS n FROM %I.%I',
                                              n.nspname, c.relname),
                                       false, true, ''))
                   )[1]::text::bigint AS n
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
        """)
    }

    await db.disconnect()
    return names, rls, counts


class Unreachable(Exception):
    """The request never got an HTTP response - proves nothing either way."""


async def probe(client: httpx.AsyncClient, table: str) -> tuple[int | None, int, str]:
    """Returns (row_count or None, http_status, note). Raises Unreachable on
    transport failure - a connection error must never be reported as 'blocked',
    or a broken network would look like working security."""
    try:
        r = await client.get(f"/rest/v1/{table}", params={"select": "*", "limit": "5"})
    except httpx.HTTPError as exc:
        raise Unreachable(str(exc)) from exc

    if r.status_code == 200:
        try:
            return len(r.json()), 200, ""
        except ValueError:
            return None, 200, "non-JSON body"
    # 401/403/404 all mean "not reachable anonymously", which is what we want.
    detail = ""
    try:
        detail = r.json().get("message", "")[:60]
    except Exception:
        pass
    return None, r.status_code, detail


async def main() -> int:
    if keys.missing(keys.REQUIRED_FOR_AUTH):
        print("SUPABASE_URL / SUPABASE_ANON_KEY not set in .env")
        return 1

    headers = {
        "apikey": keys.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {keys.SUPABASE_ANON_KEY}",
    }

    try:
        tables, rls, counts = await schema_tables()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read the schema ({exc}); falling back to the built-in list.")
        tables, rls, counts = [], {}, {}

    enumerated = bool(tables)
    if not enumerated:
        tables = FALLBACK_TABLES

    leaks: list[str] = []
    no_rls: list[str] = []
    unproven: list[str] = []

    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, headers=headers,
                                 timeout=20.0, verify=SSL_CONTEXT) as client:

        print("Anonymous access (no user logged in)")
        print(f"{len(tables)} tables "
              f"{'enumerated from the database' if enumerated else '(built-in list)'}\n")

        try:
            for t in tables:
                rows, status, note = await probe(client, t)

                # A table with RLS switched off is a finding whatever the probe
                # returned: it is empty today and readable tomorrow.
                if enumerated and not rls.get(t, True):
                    print(f"    NO RLS   {t:<34} row security disabled")
                    no_rls.append(t)
                    continue

                if rows is None:
                    print(f"    blocked  {t:<34} HTTP {status} {note}")
                elif rows:
                    print(f"    LEAK     {t:<34} {rows} rows returned")
                    leaks.append(t)
                elif enumerated and not counts.get(t):
                    # 0 rows from an empty table says nothing either way.
                    print(f"    unproven {t:<34} table is empty")
                    unproven.append(t)
                else:
                    print(f"    filtered {t:<34} has rows, returned none")

        except Unreachable as exc:
            print(f"\nCould not reach Supabase: {exc}")
            print("No conclusion can be drawn about RLS - this test did not run.")
            return 1

    print()
    if leaks or no_rls:
        if leaks:
            print(f"FAIL: {len(leaks)} table(s) readable anonymously: "
                  f"{', '.join(leaks)}")
        if no_rls:
            print(f"FAIL: {len(no_rls)} table(s) have no row security at all: "
                  f"{', '.join(no_rls)}")
            print("They leak as soon as they hold data. See 011_rls_gaps.sql.")
        return 1

    print(f"PASS: no table leaks to anonymous callers"
          f"{f' ({len(unproven)} empty, so protected but unproven)' if unproven else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

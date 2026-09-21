"""
KLASSER AI - seed data check via the REST API using the service role key.

The service role bypasses RLS, so row counts here are the true contents of the
database. Run alongside check_rls_leak.py: together they separate "RLS is
blocking you" from "the table is empty".

    python check_seed_data.py
"""

import asyncio
import ssl
import sys

import httpx

import keys

try:
    import truststore

    SSL_CONTEXT: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    SSL_CONTEXT = True

# Platform-wide, not tied to any school.
# Settings: 53 from 003, + 2 from 007 (default_min/max_periods_per_cycle),
# + 3 from 008 (model_gemini_free, model_claude, model_claude_fast),
# + 4 from 009 (the queue category), + 2 from 014 (the SSE stream's lifetime
# and poll interval).
GLOBAL_EXPECTED = [
    ("settings", 64, "settings (003 + 007 + 008 + 009 + 014)"),
    ("credit_packages", 5, "credit packages (003)"),
    ("api_keys", None, "AI provider keys"),
]

# Scoped to the sandbox school, so schools created through signup do not look
# like duplicated seed data.
SANDBOX_EXPECTED = [
    ("campuses", 2, "campuses"),
    ("rooms", 12, "rooms"),
    ("teachers", 14, "teachers"),
    ("students", 48, "students"),
    ("student_subjects", None, "subject enrolments"),
    ("subject_settings", 8, "subjects"),
    ("layout_periods", 40, "layout periods"),
    ("layout_duty_slots", 30, "duty slots"),
    ("teacher_duty_exemptions", 1, "duty exemptions"),
    ("buses", 3, "buses"),
    ("routes", 2, "routes"),
    ("timetable_layouts", 1, "layouts"),
    ("onboarding", 1, "onboarding rows"),
    ("school_credits", 1, "credit rows"),
    ("school_billing", 1, "billing rows"),
]


async def count(client: httpx.AsyncClient, table: str,
                school_id: str | None = None) -> int | None:
    """
    Exact row count via PostgREST's Content-Range header.

    Pass school_id to scope the count. Sandbox checks must be scoped: real
    schools created through signup live in the same tables, so a global count
    reports them as if the seed had duplicated.
    """
    params = {"select": "*"}
    if school_id:
        params["school_id"] = f"eq.{school_id}"
    r = await client.get(
        f"/rest/v1/{table}",
        params=params,
        headers={"Prefer": "count=exact", "Range": "0-0"},
    )
    if r.status_code not in (200, 206):
        return None
    rng = r.headers.get("content-range", "")
    return int(rng.split("/")[-1]) if "/" in rng else None


async def main() -> int:
    if keys.missing(keys.REQUIRED_FOR_ADMIN):
        print("SUPABASE_SERVICE_KEY not set in .env")
        return 1

    headers = {
        "apikey": keys.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {keys.SUPABASE_SERVICE_KEY}",
    }

    failures = 0
    print("Actual row counts (service role, RLS bypassed)\n")

    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, headers=headers,
                                 timeout=20.0, verify=SSL_CONTEXT) as client:

        async def report(table, expected, label, school_id=None) -> int:
            try:
                n = await count(client, table, school_id)
            except httpx.HTTPError as exc:
                print(f"  ERROR  {label:<28} {exc}")
                return 1
            if n is None:
                print(f"  ERROR  {label:<28} could not read count")
                return 1
            if expected is None:
                print(f"  info   {label:<28} {n}")
                return 0
            if n == expected:
                print(f"  ok     {label:<28} {n}")
                return 0
            print(f"  WRONG  {label:<28} {n}, expected {expected}")
            return 1

        for table, expected, label in GLOBAL_EXPECTED:
            failures += await report(table, expected, label)

        # Locate the sandbox school; everything below is scoped to it.
        r = await client.get("/rest/v1/settings",
                             params={"select": "value", "key": "eq.sandbox_school_id"})
        rows = r.json() if r.status_code == 200 else []
        pointer = rows[0]["value"] if rows else ""

        s = await client.get("/rest/v1/schools", params={"select": "id,name"})
        schools = s.json() if s.status_code == 200 else []
        match = [x for x in schools if str(x["id"]) == pointer]

        if match:
            print(f"  ok     {'sandbox school':<28} {match[0]['name']}")
        else:
            print(f"  WRONG  {'sandbox school':<28} "
                  f"sandbox_school_id={pointer!r} matches no school")
            failures += 1
            print(f"\n{failures} problem(s) found.")
            return 1

        print()
        for table, expected, label in SANDBOX_EXPECTED:
            failures += await report(table, expected, f"sandbox {label}", pointer)

        others = [x for x in schools if str(x["id"]) != pointer]
        print(f"\n  info   {'other schools':<28} {len(others)}"
              + (f" ({', '.join(x['name'] for x in others[:5])})" if others else ""))

    print()
    print("All expected seed data present." if not failures
          else f"{failures} problem(s) found.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

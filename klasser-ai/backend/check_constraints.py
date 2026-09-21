"""
KLASSER AI - functional constraint test via the REST API.

A column listing shows neither constraints nor generated-column expressions, so
this proves them by behaviour: writes that must succeed, and writes that must
be rejected. Uses the service role (RLS bypassed) against the sandbox school.

Writes temporary rows and deletes them again. Safe on a dev database; do not
point it at production.

    python check_constraints.py
"""

import asyncio
import ssl
import sys
import uuid

import httpx

import keys

try:
    import truststore

    SSL_CONTEXT: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    SSL_CONTEXT = True

MARKER = "ZZTest"  # surname used for every row this script creates

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def main() -> int:
    if keys.missing(keys.REQUIRED_FOR_ADMIN):
        print("SUPABASE_SERVICE_KEY not set in .env")
        return 1

    headers = {
        "apikey": keys.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {keys.SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, headers=headers,
                                 timeout=20.0, verify=SSL_CONTEXT) as client:

        school = (await client.get("/rest/v1/schools",
                                   params={"select": "id", "limit": "1"})).json()
        if not school:
            print("No school found - run 004_seed_sandbox.sql first.")
            return 1
        school_id = school[0]["id"]

        teacher_id = None
        try:
            # 1. Generated column: full_name must be derived, not supplied.
            r = await client.post("/rest/v1/teachers", json={
                "school_id": school_id,
                "first_name": "Generated",
                "surname": MARKER,
            })
            if r.status_code in (200, 201):
                row = r.json()[0]
                teacher_id = row["id"]
                check(row.get("full_name") == f"Generated {MARKER}",
                      "teachers.full_name is generated",
                      f"got {row.get('full_name')!r}")
                check(row.get("max_duties_per_cycle") == 10,
                      "teachers.max_duties_per_cycle default",
                      f"got {row.get('max_duties_per_cycle')}")
            else:
                check(False, "insert teacher", f"HTTP {r.status_code} {r.text[:120]}")

            # 2. Generated column must reject a supplied value.
            r = await client.post("/rest/v1/teachers", json={
                "school_id": school_id, "first_name": "Bad",
                "surname": MARKER, "full_name": "Should Not Work",
            })
            check(r.status_code >= 400,
                  "full_name cannot be written directly",
                  f"HTTP {r.status_code}")
            if r.status_code in (200, 201):
                await client.delete("/rest/v1/teachers",
                                    params={"id": f"eq.{r.json()[0]['id']}"})

            # 3. Foreign key must reject an unknown school.
            r = await client.post("/rest/v1/teachers", json={
                "school_id": str(uuid.uuid4()),
                "first_name": "Orphan", "surname": MARKER,
            })
            check(r.status_code >= 400, "FK teachers.school_id enforced",
                  f"HTTP {r.status_code}")
            if r.status_code in (200, 201):
                await client.delete("/rest/v1/teachers",
                                    params={"id": f"eq.{r.json()[0]['id']}"})

            # 4. NOT NULL must reject a missing surname.
            r = await client.post("/rest/v1/teachers", json={
                "school_id": school_id, "first_name": "NoSurname",
            })
            check(r.status_code >= 400, "NOT NULL teachers.surname enforced",
                  f"HTTP {r.status_code}")

            # 5. UNIQUE (teacher_id) on teacher_duty_exemptions.
            if teacher_id:
                a = await client.post("/rest/v1/teacher_duty_exemptions", json={
                    "teacher_id": teacher_id, "school_id": school_id,
                    "reason": "constraint test",
                })
                b = await client.post("/rest/v1/teacher_duty_exemptions", json={
                    "teacher_id": teacher_id, "school_id": school_id,
                    "reason": "duplicate",
                })
                check(a.status_code in (200, 201) and b.status_code >= 400,
                      "UNIQUE teacher_duty_exemptions.teacher_id",
                      f"first HTTP {a.status_code}, second HTTP {b.status_code}")
                await client.delete("/rest/v1/teacher_duty_exemptions",
                                    params={"teacher_id": f"eq.{teacher_id}"})

            # 6. UNIQUE (layout_id, day_number, period_number) on layout_periods.
            lp = (await client.get("/rest/v1/layout_periods",
                                   params={"select": "layout_id,school_id,day_number,"
                                                     "period_number,period_type,"
                                                     "start_time,end_time",
                                           "limit": "1"})).json()[0]
            r = await client.post("/rest/v1/layout_periods", json=lp)
            check(r.status_code >= 400,
                  "UNIQUE layout_periods (layout, day, period)",
                  f"HTTP {r.status_code}")
            if r.status_code in (200, 201):
                await client.delete("/rest/v1/layout_periods",
                                    params={"id": f"eq.{r.json()[0]['id']}"})

            # 7. settings primary key - the duplicate model_* insert bug.
            r = await client.post("/rest/v1/settings", json={
                "key": "model_magistral", "value": "dupe",
                "category": "model", "description": "should fail",
            })
            check(r.status_code >= 400, "PRIMARY KEY settings.key enforced",
                  f"HTTP {r.status_code}")

        finally:
            # Clean up everything this script created.
            await client.delete("/rest/v1/teacher_duty_exemptions",
                                params={"reason": "eq.constraint test"})
            d = await client.delete("/rest/v1/teachers",
                                    params={"surname": f"eq.{MARKER}"})
            left = (await client.get("/rest/v1/teachers",
                                     params={"select": "id",
                                             "surname": f"eq.{MARKER}"})).json()
            check(not left, "test rows cleaned up",
                  f"{len(left)} left behind" if left else "")

    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("Constraint behaviour\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

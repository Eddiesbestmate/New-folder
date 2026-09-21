"""
Phase 3 onboarding test against the live Supabase project.

Creates a school, walks the wizard steps, checks that progress is derived from
real data rather than trusted from the client, then cleans up.

    python test_onboarding.py
"""

import asyncio
import logging
import ssl
import sys
import uuid

import httpx

import keys
from models import database as db

try:
    import truststore

    truststore.inject_into_ssl()
    SSL_CONTEXT: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    SSL_CONTEXT = True

import main  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

SUFFIX = uuid.uuid4().hex[:8]
EMAIL = f"phase3.{SUFFIX}@example.com"
PASSWORD = "TestPass123"
SCHOOL = f"Phase 3 Test School {SUFFIX}"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(email: str, password: str) -> str | None:
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": password})
    return r.json().get("access_token") if r.status_code == 200 else None


async def cleanup(user_id: str | None, school_id: str | None) -> None:
    if school_id:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM teacher_duty_exemptions WHERE school_id = $1",
                "DELETE FROM teachers WHERE school_id = $1",
                "DELETE FROM students WHERE school_id = $1",
                "DELETE FROM rooms WHERE school_id = $1",
                "DELETE FROM subject_settings WHERE school_id = $1",
                "DELETE FROM buses WHERE school_id = $1",
                "DELETE FROM notification_preferences WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM onboarding WHERE school_id = $1",
                "DELETE FROM school_complexity WHERE school_id = $1",
                "DELETE FROM school_billing WHERE school_id = $1",
                "DELETE FROM school_credits WHERE school_id = $1",
                "DELETE FROM campuses WHERE school_id = $1",
                "DELETE FROM users WHERE school_id = $1",
                "DELETE FROM schools WHERE id = $1",
            ):
                await conn.execute(sql, school_id)
    if user_id:
        await sb.delete_auth_user(user_id)


def step(progress: dict, key: str) -> dict:
    return next(s for s in progress["steps"] if s["key"] == key)


async def main_test() -> None:
    await db.connect()
    transport = httpx.ASGITransport(app=main.app)
    user_id = school_id = None

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            r = await client.post("/auth/signup", json={
                "school_name": SCHOOL, "timezone": "Australia/Sydney",
                "first_name": "Test", "surname": "Owner",
                "email": EMAIL, "password": PASSWORD,
                "campuses": [{"name": "Main Campus", "address": None}],
            })
            if r.status_code != 201:
                check(False, "signup", f"HTTP {r.status_code} {r.text[:120]}")
                return
            school_id, user_id = r.json()["school_id"], r.json()["user_id"]

            token = await sign_in(EMAIL, PASSWORD)
            auth = {"Authorization": f"Bearer {token}"}

            # --- Progress requires auth ---------------------------------------
            r = await client.get("/onboarding/progress")
            check(r.status_code == 401, "progress requires auth", f"got {r.status_code}")

            # --- Initial state --------------------------------------------------
            r = await client.get("/onboarding/progress", headers=auth)
            check(r.status_code == 200, "GET progress", f"HTTP {r.status_code}")
            p = r.json()
            check(len(p["steps"]) == 9, "nine steps reported", f"{len(p['steps'])}")
            check(step(p, "step_school_details")["complete"], "school details done at signup")
            check(step(p, "step_campuses")["complete"], "campuses done at signup")
            check(step(p, "step_campuses")["count"] == 1, "campus count is 1")
            check(not step(p, "step_billing")["complete"], "billing not yet done")
            check(not p["ready_to_generate"], "not ready to generate yet")
            check("Billing" in p["missing"], "billing listed as missing")

            # --- Step 1: school details ------------------------------------------
            r = await client.put("/onboarding/school-details", headers=auth, json={
                "school_name": SCHOOL + " Renamed",
                "timezone": "Australia/Perth",
                "campuses": [
                    {"name": "Main Campus", "address": "1 Test Street"},
                    {"name": "North Campus", "address": "2 Test Street"},
                ],
            })
            check(r.status_code == 200, "PUT school-details", f"HTTP {r.status_code}")

            row = await db.fetchrow(
                "SELECT name, timezone FROM schools WHERE id = $1", school_id)
            check(row["timezone"] == "Australia/Perth", "timezone updated")
            n = await db.fetchval(
                "SELECT count(*) FROM campuses WHERE school_id = $1", school_id)
            check(n == 2, "second campus added, first reused not duplicated", f"{n}")

            # Existing campus was updated in place, not recreated.
            addr = await db.fetchval("""
                SELECT address FROM campuses
                WHERE school_id = $1 AND name = 'Main Campus'
            """, school_id)
            check(addr == "1 Test Street", "existing campus updated in place")

            # --- Validation --------------------------------------------------------
            r = await client.put("/onboarding/school-details", headers=auth, json={
                "school_name": SCHOOL, "timezone": "Australia/Sydney",
                "campuses": [{"name": "A"}, {"name": "a"}],
            })
            check(r.status_code == 422, "duplicate campus names rejected",
                  f"got {r.status_code}")

            r = await client.put("/onboarding/school-details", headers=auth, json={
                "school_name": SCHOOL, "timezone": "Australia/Sydney", "campuses": [],
            })
            check(r.status_code == 422, "empty campus list rejected", f"got {r.status_code}")

            # --- Step 2: billing -----------------------------------------------------
            r = await client.get("/onboarding/billing-options", headers=auth)
            check(r.status_code == 200, "GET billing-options", f"HTTP {r.status_code}")
            b = r.json()
            check(len(b["packages"]) == 5, "five packages offered", f"{len(b['packages'])}")
            check(b["payg_rate_dollars"] == 2.30, "PAYG rate is $2.30",
                  f"${b['payg_rate_dollars']}")
            trial = next(p for p in b["packages"] if p["name"] == "trial")
            check(trial["available"], "trial available to a new school")
            check(trial["price_dollars"] == 480.0, "trial priced at $480",
                  f"${trial['price_dollars']}")

            r = await client.put("/onboarding/billing-mode", headers=auth,
                                 json={"billing_mode": "nonsense"})
            check(r.status_code == 422, "invalid billing mode rejected",
                  f"got {r.status_code}")

            r = await client.put("/onboarding/billing-mode", headers=auth,
                                 json={"billing_mode": "credits"})
            check(r.status_code == 200, "PUT billing-mode", f"HTTP {r.status_code}")

            r = await client.get("/onboarding/progress", headers=auth)
            check(step(r.json(), "step_billing")["complete"], "billing step now complete")

            # --- Derived steps: flags follow the data, not the client ----------------
            campus_id = await db.fetchval(
                "SELECT id FROM campuses WHERE school_id = $1 LIMIT 1", school_id)

            await db.execute("""
                INSERT INTO teachers (school_id, first_name, surname, campus_id)
                VALUES ($1, 'Ada', 'Lovelace', $2)
            """, school_id, campus_id)
            r = await client.get("/onboarding/progress", headers=auth)
            check(step(r.json(), "step_teachers")["complete"],
                  "teachers step derived from inserted row")
            check(step(r.json(), "step_teachers")["count"] == 1, "teacher count reported")

            await db.execute("""
                INSERT INTO students (school_id, full_name, year_group, campus_id)
                VALUES ($1, 'Alan Turing', 'Year 11', $2)
            """, school_id, campus_id)
            await db.execute("""
                INSERT INTO rooms (school_id, campus_id, name, capacity)
                VALUES ($1, $2, 'R1', 30)
            """, school_id, campus_id)

            r = await client.get("/onboarding/progress", headers=auth)
            p = r.json()
            check(p["ready_to_generate"], "ready to generate once all required steps done")
            check(p["completed"], "onboarding marked complete")
            check(p["missing"] == [], "nothing missing", f"{p['missing']}")

            done = await db.fetchval(
                "SELECT completed FROM onboarding WHERE school_id = $1", school_id)
            check(done is True, "completed persisted to the database")

            # Deleting the data un-completes the step - the flag is not sticky.
            await db.execute("DELETE FROM rooms WHERE school_id = $1", school_id)
            r = await client.get("/onboarding/progress", headers=auth)
            check(not step(r.json(), "step_rooms")["complete"],
                  "step reverts when the data is removed")
            check(not r.json()["ready_to_generate"], "not ready once rooms are gone")

            # --- Staff cannot change school settings ---------------------------------
            await db.execute("UPDATE users SET role = 'staff' WHERE id = $1", user_id)
            sb.forget_token(token)
            r = await client.put("/onboarding/billing-mode", headers=auth,
                                 json={"billing_mode": "payg"})
            check(r.status_code == 403, "staff blocked from billing changes",
                  f"got {r.status_code}")
            r = await client.get("/onboarding/progress", headers=auth)
            check(r.status_code == 200, "staff can still read progress",
                  f"got {r.status_code}")

        finally:
            await cleanup(user_id, school_id)
            left = await db.fetchval(
                "SELECT count(*) FROM schools WHERE id = $1", school_id) if school_id else 0
            check(left == 0, "test data cleaned up", f"{left} left")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 3 - onboarding\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

"""
Phase 4 school data management test against the live Supabase project.

Covers CRUD for teachers, students, rooms and subjects, the layout builder, the
delete guards, and tenant isolation between two schools.

    python test_schools.py
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

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []  # (school_id, user_id)


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(email: str) -> str | None:
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def make_school(client: httpx.AsyncClient, tag: str) -> tuple[dict, str]:
    email = f"phase4.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Phase 4 {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": "Owner", "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main Campus"}],
    })
    if r.status_code != 201:
        raise RuntimeError(f"signup failed: {r.status_code} {r.text[:150]}")
    schools.append((r.json()["school_id"], r.json()["user_id"]))
    token = await sign_in(email)
    return {"Authorization": f"Bearer {token}"}, r.json()["school_id"]


async def cleanup() -> None:
    for school_id, user_id in schools:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM layout_periods WHERE school_id = $1",
                "DELETE FROM timetable_layouts WHERE school_id = $1",
                "DELETE FROM teacher_duty_exemptions WHERE school_id = $1",
                "DELETE FROM teacher_availability WHERE school_id = $1",
                "DELETE FROM teachers WHERE school_id = $1",
                "DELETE FROM student_subjects WHERE school_id = $1",
                "DELETE FROM students WHERE school_id = $1",
                "DELETE FROM rooms WHERE school_id = $1",
                "DELETE FROM subject_settings WHERE school_id = $1",
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
        await sb.delete_auth_user(user_id)


async def main_test() -> None:
    await db.connect()
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            auth, school_id = await make_school(client, "A")
            other_auth, other_id = await make_school(client, "B")

            campuses = (await client.get("/schools/campuses", headers=auth)).json()
            campus_id = campuses[0]["id"]
            other_campus = (await client.get("/schools/campuses",
                                             headers=other_auth)).json()[0]["id"]

            # --- Teachers -------------------------------------------------------
            r = await client.post("/schools/teachers", headers=auth, json={
                "first_name": "Ada", "surname": "Lovelace",
                "subjects": ["Mathematics", "Computing"], "campus_id": campus_id,
                "max_blocks": 5,
            })
            check(r.status_code == 201, "create teacher", f"HTTP {r.status_code} {r.text[:100]}")
            teacher_id = r.json().get("id")
            check(r.json().get("full_name") == "Ada Lovelace",
                  "generated full_name returned", r.json().get("full_name"))

            r = await client.post("/schools/teachers", headers=auth,
                                  json={"full_name": "Grace Brewster Hopper"})
            check(r.status_code == 201, "teacher from a single full name")
            gid = r.json().get("id")
            row = await db.fetchrow("SELECT first_name, surname FROM teachers WHERE id = $1", gid)
            check(row["first_name"] == "Grace Brewster" and row["surname"] == "Hopper",
                  "full name split on the last space",
                  f"{row['first_name']!r} / {row['surname']!r}")

            r = await client.post("/schools/teachers", headers=auth,
                                  json={"full_name": "Prince"})
            check(r.status_code == 422, "single-word name rejected", f"got {r.status_code}")

            r = await client.post("/schools/teachers", headers=auth, json={
                "first_name": "ada", "surname": "LOVELACE"})
            check(r.status_code == 409, "duplicate teacher rejected case-insensitively",
                  f"got {r.status_code}")

            r = await client.post("/schools/teachers", headers=auth, json={
                "first_name": "Cross", "surname": "Tenant", "campus_id": other_campus})
            check(r.status_code == 400, "campus from another school rejected",
                  f"got {r.status_code}")

            r = await client.put(f"/schools/teachers/{teacher_id}/duty-exempt",
                                 headers=auth, params={"exempt": "true",
                                                       "reason": "Part time"})
            check(r.status_code == 200, "set duty exemption")
            r = await client.get("/schools/teachers", headers=auth)
            ada = next(t for t in r.json() if t["id"] == teacher_id)
            check(ada["duty_exempt"] is True, "exemption reflected in list")

            # --- Students -------------------------------------------------------
            r = await client.post("/schools/students", headers=auth, json={
                "full_name": "Alan Turing", "year_group": "Year 11",
                "campus_id": campus_id, "subjects": ["Mathematics", "Computing"],
            })
            check(r.status_code == 201, "create student", f"HTTP {r.status_code}")
            student_id = r.json().get("id")

            n = await db.fetchval(
                "SELECT count(*) FROM student_subjects WHERE student_id = $1", student_id)
            check(n == 2, "student subject enrolments written", f"{n}")

            r = await client.patch(f"/schools/students/{student_id}", headers=auth, json={
                "full_name": "Alan Turing", "year_group": "Year 12",
                "campus_id": campus_id, "subjects": ["Mathematics"],
            })
            check(r.status_code == 200, "update student")
            n = await db.fetchval(
                "SELECT count(*) FROM student_subjects WHERE student_id = $1", student_id)
            check(n == 1, "subjects replaced on update", f"{n}")

            r = await client.get("/schools/students", headers=auth,
                                 params={"year_group": "Year 12"})
            check(r.json()["total"] == 1, "filter by year group")
            r = await client.get("/schools/students", headers=auth,
                                 params={"search": "turing"})
            check(r.json()["total"] == 1, "search is case-insensitive")

            # --- Rooms ------------------------------------------------------------
            r = await client.post("/schools/rooms", headers=auth, json={
                "name": "N-101", "campus_id": campus_id, "capacity": 30,
                "room_type": "classroom",
            })
            check(r.status_code == 201, "create room", f"HTTP {r.status_code}")
            room_id = r.json().get("id")

            r = await client.post("/schools/rooms", headers=auth, json={
                "name": "n-101", "campus_id": campus_id, "capacity": 25})
            check(r.status_code == 409, "duplicate room on same campus rejected",
                  f"got {r.status_code}")

            r = await client.post("/schools/rooms", headers=auth, json={
                "name": "N-102", "campus_id": campus_id, "capacity": 20,
                "preferred_min_capacity": 30})
            check(r.status_code == 422, "min capacity above capacity rejected",
                  f"got {r.status_code}")

            r = await client.post("/schools/rooms", headers=auth, json={
                "name": "N-103", "campus_id": campus_id, "capacity": 0})
            check(r.status_code == 422, "zero capacity rejected", f"got {r.status_code}")

            # --- Subjects ----------------------------------------------------------
            r = await client.post("/schools/subjects", headers=auth, json={
                "subject": "Computing", "year_level": "Year 11",
                "hard_max_size": 26, "soft_max_size": 22,
                "default_room_type": "computer_lab", "code_prefix": "COM",
            })
            check(r.status_code == 201, "create subject", f"HTTP {r.status_code}")

            r = await client.post("/schools/subjects", headers=auth, json={
                "subject": "Computing", "year_level": "Year 11"})
            check(r.status_code == 409, "duplicate subject/year rejected",
                  f"got {r.status_code}")

            r = await client.post("/schools/subjects", headers=auth, json={
                "subject": "Physics", "hard_max_size": 20, "soft_max_size": 25})
            check(r.status_code == 422, "soft max above hard max rejected",
                  f"got {r.status_code}")

            # --- Layout -------------------------------------------------------------
            periods = []
            for day in range(1, 6):
                for num, (ptype, start, end) in enumerate([
                    ("teaching", "09:00", "10:00"),
                    ("teaching", "10:00", "11:00"),
                    ("break", "11:00", "11:20"),
                    ("teaching", "11:20", "12:20"),
                ], start=1):
                    periods.append({
                        "day_number": day, "period_number": num,
                        "period_type": ptype, "start_time": start,
                        "end_time": end, "label": f"P{num}",
                    })

            r = await client.put("/schools/layout", headers=auth, json={
                "name": "Standard", "days_in_cycle": 5, "periods": periods})
            check(r.status_code == 200, "save layout", f"HTTP {r.status_code} {r.text[:100]}")

            n = await db.fetchval(
                "SELECT count(*) FROM layout_periods WHERE school_id = $1", school_id)
            check(n == 20, "20 periods written", f"{n}")

            r = await client.get("/schools/layout", headers=auth)
            check(r.json()["days_in_cycle"] == 5, "layout read back")
            check(r.json()["periods"][0]["start_time"] == "09:00",
                  "times formatted HH:MM", r.json()["periods"][0]["start_time"])

            # Saving again replaces rather than duplicating.
            r = await client.put("/schools/layout", headers=auth, json={
                "name": "Standard v2", "days_in_cycle": 5, "periods": periods})
            n = await db.fetchval(
                "SELECT count(*) FROM layout_periods WHERE school_id = $1", school_id)
            check(n == 20, "re-saving replaces periods, no duplicates", f"{n}")

            r = await client.put("/schools/layout", headers=auth, json={
                "name": "Bad", "days_in_cycle": 2, "periods": [{
                    "day_number": 5, "period_number": 1, "period_type": "teaching",
                    "start_time": "09:00", "end_time": "10:00"}]})
            check(r.status_code == 422, "period beyond cycle length rejected",
                  f"got {r.status_code}")

            r = await client.put("/schools/layout", headers=auth, json={
                "name": "Bad", "days_in_cycle": 1, "periods": [{
                    "day_number": 1, "period_number": 1, "period_type": "teaching",
                    "start_time": "12:00", "end_time": "09:00"}]})
            check(r.status_code == 422, "end before start rejected", f"got {r.status_code}")

            r = await client.put("/schools/layout", headers=auth, json={
                "name": "Bad", "days_in_cycle": 1, "periods": [{
                    "day_number": 1, "period_number": 1, "period_type": "lecture",
                    "start_time": "09:00", "end_time": "10:00"}]})
            check(r.status_code == 422, "unknown period type rejected", f"got {r.status_code}")

            # --- Tenant isolation ------------------------------------------------------
            r = await client.get("/schools/teachers", headers=other_auth)
            check(r.json() == [], "other school sees no teachers", f"{len(r.json())} rows")

            r = await client.patch(f"/schools/teachers/{teacher_id}", headers=other_auth,
                                   json={"first_name": "Stolen", "surname": "Row"})
            check(r.status_code == 404, "cannot edit another school's teacher",
                  f"got {r.status_code}")

            r = await client.delete(f"/schools/rooms/{room_id}", headers=other_auth)
            check(r.status_code == 404, "cannot delete another school's room",
                  f"got {r.status_code}")

            r = await client.get("/schools/layout", headers=other_auth)
            check(r.json() is None, "other school has no layout")

            # --- Delete guard -----------------------------------------------------------
            # Put the teacher and room into a timetable entry, then try to delete.
            layout_id = await db.fetchval("""
                SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
            """, school_id)
            period_id = await db.fetchval("""
                SELECT id FROM layout_periods WHERE layout_id = $1 LIMIT 1
            """, layout_id)
            tt_id = await db.fetchval("""
                INSERT INTO timetables (school_id, name, layout_id, status)
                VALUES ($1, 'Guard test', $2, 'complete') RETURNING id
            """, school_id, layout_id)
            ver_id = await db.fetchval("""
                INSERT INTO timetable_versions (timetable_id, school_id, version_number)
                VALUES ($1, $2, 1) RETURNING id
            """, tt_id, school_id)
            await db.execute("""
                INSERT INTO timetable_entries
                    (timetable_id, version_id, layout_period_id, day_number, room_id,
                     teacher_id, campus_id, subject, class_code)
                VALUES ($1,$2,$3,1,$4,$5,$6,'Computing','11COM1')
            """, tt_id, ver_id, period_id, room_id, teacher_id, campus_id)

            r = await client.delete(f"/schools/teachers/{teacher_id}", headers=auth)
            check(r.status_code == 409, "cannot delete a timetabled teacher",
                  f"got {r.status_code}")
            check("timetable" in r.text.lower(), "delete guard explains why")

            r = await client.delete(f"/schools/rooms/{room_id}", headers=auth)
            check(r.status_code == 409, "cannot delete a timetabled room",
                  f"got {r.status_code}")

            r = await client.delete(f"/schools/students/{student_id}", headers=auth)
            check(r.status_code == 204, "unreferenced student deletes",
                  f"got {r.status_code}")

            await db.execute("DELETE FROM timetable_entries WHERE timetable_id = $1", tt_id)
            r = await client.delete(f"/schools/teachers/{teacher_id}", headers=auth)
            check(r.status_code == 204, "teacher deletes once no longer timetabled",
                  f"got {r.status_code}")

            n = await db.fetchval(
                "SELECT count(*) FROM teacher_duty_exemptions WHERE teacher_id = $1",
                teacher_id)
            check(n == 0, "exemption removed with the teacher", f"{n}")

            await db.execute("DELETE FROM timetable_versions WHERE timetable_id = $1", tt_id)
            await db.execute("DELETE FROM timetables WHERE id = $1", tt_id)

            # --- Role guard ----------------------------------------------------------------
            _, staff_user = schools[0]
            await db.execute("UPDATE users SET role = 'staff' WHERE id = $1", staff_user)
            token = auth["Authorization"].split()[1]
            sb.forget_token(token)

            r = await client.post("/schools/rooms", headers=auth, json={
                "name": "Staff room", "campus_id": campus_id, "capacity": 10})
            check(r.status_code == 403, "staff cannot create rooms", f"got {r.status_code}")
            r = await client.get("/schools/rooms", headers=auth)
            check(r.status_code == 200, "staff can still read rooms", f"got {r.status_code}")

        finally:
            await cleanup()
            left = await db.fetchval(
                "SELECT count(*) FROM schools WHERE name LIKE $1", f"Phase 4%{RUN}")
            check(left == 0, "test data cleaned up", f"{left} left")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 4 - school data management\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

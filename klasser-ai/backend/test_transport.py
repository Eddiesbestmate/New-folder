"""
Phase 9 transport tests - fleet, routes, readiness and the solved schedule.

    python test_transport.py
"""

import asyncio
import json
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
from services import ai_cluster, pipeline  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.pipeline").setLevel(logging.CRITICAL)
logging.getLogger("klasser.deterministic").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []
attempts: list[str] = []
timetables: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


class StubAI:
    @staticmethod
    async def call(task_key, prompt, **kwargs):
        if task_key.startswith("validator_"):
            return json.dumps({"result": "pass", "reason": None,
                               "confidence": "high"})
        if "subject_map" in prompt:
            return json.dumps({"subject_map": []})
        if '"groups"' in prompt:
            return json.dumps({"groups": []})
        return json.dumps({"assignments": {}})


async def sign_in(email: str):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def make_school(client, tag: str, campuses: list[str]) -> tuple[dict, str]:
    email = f"transport.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Transport {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": "Owner", "email": email,
        "password": PASSWORD,
        "campuses": [{"name": c} for c in campuses]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))
    return {"Authorization": f"Bearer {await sign_in(email)}"}, school_id


async def cleanup() -> None:
    for attempt_id in attempts:
        for sql in (
            "DELETE FROM timetable_entry_students WHERE timetable_entry_id IN "
            "  (SELECT id FROM timetable_entries WHERE attempt_id = $1)",
            "DELETE FROM timetable_entries WHERE attempt_id = $1",
            "DELETE FROM duty_assignments WHERE attempt_id = $1",
            "DELETE FROM class_group_slots WHERE attempt_id = $1",
            "DELETE FROM allocation_registry_students WHERE attempt_id = $1",
            "DELETE FROM allocation_registry_teachers WHERE attempt_id = $1",
            "DELETE FROM allocation_registry_rooms WHERE attempt_id = $1",
            "DELETE FROM allocation_registry_duties WHERE attempt_id = $1",
            "DELETE FROM allocation_registry_duty_counts WHERE attempt_id = $1",
            "DELETE FROM class_consistency_registry WHERE attempt_id = $1",
            "DELETE FROM class_group_students WHERE class_group_id IN "
            "  (SELECT id FROM class_groups WHERE attempt_id = $1)",
            "DELETE FROM class_groups WHERE attempt_id = $1",
            "DELETE FROM generation_events WHERE attempt_id = $1",
            "DELETE FROM pipeline_state WHERE attempt_id = $1",
            "DELETE FROM generation_jobs WHERE attempt_id = $1",
            "DELETE FROM pipeline_log WHERE attempt_id = $1",
            "DELETE FROM error_log WHERE attempt_id = $1",
            "DELETE FROM validation_results WHERE attempt_id = $1",
            "DELETE FROM partial_solutions WHERE attempt_id = $1",
        ):
            await db.execute(sql, attempt_id)

    for tid in timetables:
        await db.execute(
            "DELETE FROM transport_schedule WHERE timetable_id = $1", tid)
        await db.execute(
            "DELETE FROM timetable_versions WHERE timetable_id = $1", tid)
        await db.execute(
            "DELETE FROM generation_attempts WHERE timetable_id = $1", tid)
        await db.execute("DELETE FROM timetables WHERE id = $1", tid)

    for school_id, user_id in schools:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM job_queue WHERE school_id = $1",
                "DELETE FROM student_subjects WHERE school_id = $1",
                "DELETE FROM students WHERE school_id = $1",
                "DELETE FROM teachers WHERE school_id = $1",
                "DELETE FROM rooms WHERE school_id = $1",
                "DELETE FROM subject_settings WHERE school_id = $1",
                "DELETE FROM routes WHERE school_id = $1",
                "DELETE FROM buses WHERE school_id = $1",
                "DELETE FROM notification_preferences WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM onboarding WHERE school_id = $1",
                "DELETE FROM school_complexity WHERE school_id = $1",
                "DELETE FROM school_billing WHERE school_id = $1",
                "DELETE FROM school_credits WHERE school_id = $1",
                "DELETE FROM layout_periods WHERE school_id = $1",
                "DELETE FROM timetable_layouts WHERE school_id = $1",
                "DELETE FROM campuses WHERE school_id = $1",
                "DELETE FROM users WHERE school_id = $1",
                "DELETE FROM schools WHERE id = $1",
            ):
                await conn.execute(sql, school_id)
        await sb.delete_auth_user(user_id)


async def main_test() -> None:
    await db.connect()
    original = ai_cluster.call
    ai_cluster.call = StubAI.call  # type: ignore[assignment]
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            # --- Single campus: transport does not apply -----------------------
            solo_auth, solo_id = await make_school(client, "solo", ["Only"])
            r = await client.get("/transport/overview", headers=solo_auth)
            check(r.status_code == 200, "GET overview", f"HTTP {r.status_code}")
            check(r.json()["applies"] is False,
                  "transport does not apply to a one-campus school")
            check(r.json()["ready"] is True,
                  "a one-campus school is ready without buses")

            # --- Two campuses: incomplete until set up ---------------------------
            auth, school_id = await make_school(client, "two", ["North", "South"])
            campuses = (await client.get("/schools/campuses", headers=auth)).json()
            north = next(c for c in campuses if c["name"] == "North")["id"]
            south = next(c for c in campuses if c["name"] == "South")["id"]

            r = await client.get("/transport/overview", headers=auth)
            o = r.json()
            check(o["applies"] is True, "transport applies to two campuses")
            check(o["ready"] is False, "not ready with no buses or routes")
            check(len(o["missing_routes"]) == 2,
                  "both directions reported missing", str(len(o["missing_routes"])))

            # --- Buses ---------------------------------------------------------------
            r = await client.post("/transport/buses", headers=auth, json={
                "name": "Bus 1", "capacity": 45, "home_campus_id": north})
            check(r.status_code == 201, "create bus", f"HTTP {r.status_code}")
            bus_id = r.json()["id"]

            r = await client.post("/transport/buses", headers=auth, json={
                "name": "bus 1", "capacity": 30, "home_campus_id": south})
            check(r.status_code == 409, "duplicate bus name rejected",
                  f"got {r.status_code}")

            r = await client.post("/transport/buses", headers=auth, json={
                "name": "Bad", "capacity": 0, "home_campus_id": north})
            check(r.status_code == 422, "zero capacity rejected",
                  f"got {r.status_code}")

            other_campus = (await client.get("/schools/campuses",
                                             headers=solo_auth)).json()[0]["id"]
            r = await client.post("/transport/buses", headers=auth, json={
                "name": "Foreign", "capacity": 20,
                "home_campus_id": other_campus})
            check(r.status_code == 400, "campus from another school rejected",
                  f"got {r.status_code}")

            # --- Routes ------------------------------------------------------------------
            r = await client.post("/transport/routes", headers=auth,
                                  json={"from_campus_id": north,
                                        "to_campus_id": south,
                                        "travel_minutes": 12},
                                  params={"both_ways": "true"})
            check(r.status_code == 201, "create route", f"HTTP {r.status_code}")
            check(len(r.json()["created"]) == 2,
                  "both directions created", str(len(r.json()["created"])))

            r = await client.post("/transport/routes", headers=auth,
                                  json={"from_campus_id": north,
                                        "to_campus_id": south,
                                        "travel_minutes": 12})
            check(r.status_code == 409, "duplicate route rejected",
                  f"got {r.status_code}")

            r = await client.post("/transport/routes", headers=auth,
                                  json={"from_campus_id": north,
                                        "to_campus_id": north,
                                        "travel_minutes": 5})
            check(r.status_code == 422, "route to the same campus rejected",
                  f"got {r.status_code}")

            r = await client.get("/transport/overview", headers=auth)
            check(r.json()["ready"] is True, "ready once buses and routes exist")
            check(r.json()["seats"] == 45, "seat count reported",
                  str(r.json()["seats"]))

            # --- Complexity kept in step, because it drives pricing --------------------------
            complexity = await db.fetchrow("""
                SELECT bus_route_count, transport_enabled
                FROM school_complexity WHERE school_id = $1
            """, school_id)
            check(complexity["bus_route_count"] == 2,
                  "route count synced to the cost inputs",
                  str(complexity["bus_route_count"]))
            check(complexity["transport_enabled"] is True,
                  "transport flagged in the cost inputs")

            # --- Generate, then read the schedule ------------------------------------------------
            await seed_for_generation(school_id, north, south)

            layout_id = await db.fetchval("""
                SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
            """, school_id)
            tid = str(await db.fetchval("""
                INSERT INTO timetables (school_id, name, layout_id, status)
                VALUES ($1, $2, $3, 'processing') RETURNING id
            """, school_id, f"Transport test {RUN}", layout_id))
            timetables.append(tid)

            summary = await pipeline.run_with_retries(tid, "t@example.com")
            for row in await db.fetch(
                    "SELECT id FROM generation_attempts WHERE timetable_id = $1", tid):
                attempts.append(str(row["id"]))
            version_id = summary["version_id"]

            r = await client.get(f"/transport/schedule/{version_id}", headers=auth)
            check(r.status_code == 200, "GET schedule", f"HTTP {r.status_code}")
            data = r.json()
            check(data["movements"] > 0, "bus movements produced",
                  str(data["movements"]))
            check(data["chains"], "movements grouped into per-bus day chains",
                  f"{len(data['chains'])} chains")

            # Every chain belongs to one bus on one day, and its movements are in
            # departure order - the day chain in TRANSPORT.md.
            out_of_order = [
                c["bus"] for c in data["chains"]
                if [m["departure"] for m in c["movements"]]
                != sorted(m["departure"] for m in c["movements"])
            ]
            check(not out_of_order, "each chain is in departure order",
                  str(out_of_order[:3]))

            check(all(m["from_campus"] != m["to_campus"]
                      for c in data["chains"] for m in c["movements"]),
                  "no movement starts and ends at the same campus")

            carrying = [m for c in data["chains"] for m in c["movements"]
                        if not m["is_empty_leg"]]
            check(all(m["passenger_count"] > 0 for m in carrying),
                  "every passenger leg carries someone",
                  f"{sum(1 for m in carrying if m['passenger_count'] == 0)} empty")
            check(all(m["passenger_count"] <= c["capacity"]
                      for c in data["chains"] for m in c["movements"]),
                  "no bus carries more than its capacity")

            # Times come from the period the students are travelling to, less
            # the route's travel time - not a fixed placeholder. Layer 8 places
            # bus duties off these, so identical times everywhere would be wrong.
            times = {m["departure"] for c in data["chains"]
                     for m in c["movements"]}
            check(len(times) > 1, "departure times vary by period",
                  f"{sorted(times)}")
            check(all(m["departure"] < m["arrival"]
                      for c in data["chains"] for m in c["movements"]),
                  "every movement arrives after it departs")
            # Route is 12 minutes, so a leg landing for period 1 (09:00) leaves
            # at 08:48.
            check(any(m["arrival"] == "09:00" and m["departure"] == "08:48"
                      for c in data["chains"] for m in c["movements"]),
                  "arrival is the bell, departure is travel_minutes earlier",
                  f"{sorted({(m['departure'], m['arrival']) for c in data['chains'] for m in c['movements']})[:3]}")

            days = {c["day"] for c in data["chains"]}
            if days:
                one = sorted(days)[0]
                r = await client.get(f"/transport/schedule/{version_id}",
                                     headers=auth, params={"day": one})
                check(all(c["day"] == one for c in r.json()["chains"]),
                      "filtering by day returns only that day")

            # --- Delete guards ----------------------------------------------------------------------
            r = await client.delete(f"/transport/buses/{bus_id}", headers=auth)
            if data["movements"]:
                check(r.status_code == 409,
                      "a bus in a schedule cannot be deleted",
                      f"got {r.status_code}")
            else:
                check(r.status_code == 204, "unused bus deletes")

            # --- Tenant isolation --------------------------------------------------------------------
            r = await client.get("/transport/buses", headers=solo_auth)
            check(r.json() == [], "another school sees no buses",
                  f"{len(r.json())} rows")
            r = await client.get(f"/transport/schedule/{version_id}",
                                 headers=solo_auth)
            check(r.status_code == 404,
                  "another school cannot read the schedule", f"got {r.status_code}")
            r = await client.delete(f"/transport/buses/{bus_id}", headers=solo_auth)
            check(r.status_code == 404, "another school cannot delete a bus",
                  f"got {r.status_code}")

            # --- Staff cannot change the fleet ----------------------------------------------------------
            _, user_id = schools[1]
            await db.execute("UPDATE users SET role = 'staff' WHERE id = $1", user_id)
            sb.forget_token(auth["Authorization"].split()[1])
            r = await client.post("/transport/buses", headers=auth, json={
                "name": "Staff bus", "capacity": 10, "home_campus_id": north})
            check(r.status_code == 403, "staff cannot add a bus",
                  f"got {r.status_code}")
            r = await client.get("/transport/buses", headers=auth)
            check(r.status_code == 200, "staff can still read the fleet")

        finally:
            ai_cluster.call = original  # type: ignore[assignment]
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM schools WHERE name LIKE $1",
                    f"Transport%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


async def seed_for_generation(school_id: str, north: str, south: str) -> None:
    """
    Enough data for a generation that actually needs buses.

    A class sits at the campus most of its students are already at, so the
    minority travel. Eight students are based at North and four at South, all
    taking the same two subjects - that is the cross-campus class in
    TRANSPORT.md, and the four have to be bussed.
    """
    layout_id = await db.fetchval("""
        INSERT INTO timetable_layouts (school_id, name, days_in_cycle, is_active)
        VALUES ($1, 'Transport layout', 5, true) RETURNING id
    """, school_id)
    await db.execute("""
        INSERT INTO layout_periods
            (layout_id, school_id, day_number, period_number, period_type,
             start_time, end_time, label)
        SELECT $1, $2, d, p, 'teaching',
               make_time(8 + p, 0, 0), make_time(8 + p, 50, 0), 'P' || p
        FROM generate_series(1, 5) AS d, generate_series(1, 4) AS p
    """, layout_id, school_id)

    await db.execute("""
        INSERT INTO teachers (school_id, first_name, surname, subjects,
                              campus_id, max_blocks)
        VALUES ($1, 'North', 'Teacher', ARRAY['English'], $2, 5),
               ($1, 'Roving', 'Teacher', ARRAY['Science'], NULL, 5)
    """, school_id, north)

    await db.execute("""
        INSERT INTO rooms (school_id, campus_id, name, capacity, room_type)
        VALUES ($1, $2, 'N1', 30, 'classroom'), ($1, $2, 'N2', 30, 'classroom'),
               ($1, $3, 'S1', 30, 'classroom')
    """, school_id, north, south)

    await db.execute("""
        INSERT INTO subject_settings
            (school_id, subject, hard_max_size, soft_max_size,
             min_periods_per_cycle, max_periods_per_cycle)
        VALUES ($1, 'English', 30, 25, 2, 3), ($1, 'Science', 30, 25, 2, 3)
    """, school_id)

    for i in range(12):
        home = north if i < 8 else south
        student_id = await db.fetchval("""
            INSERT INTO students (school_id, full_name, year_group, campus_id)
            VALUES ($1, $2, 'Year 11', $3) RETURNING id
        """, school_id, f"Student {i}", home)
        await db.executemany("""
            INSERT INTO student_subjects (student_id, school_id, subject)
            VALUES ($1, $2, $3)
        """, [(student_id, school_id, "English"),
              (student_id, school_id, "Science")])


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 9 - transport\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

"""
Phase 8 test - cost estimate, listing, output views and publishing.

Generates a real timetable against the sandbox school with a stubbed AI, then
exercises every endpoint the output pages call.

    python test_output.py
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
timetables: list[str] = []
schools: list[tuple[str, str]] = []


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


async def cleanup() -> None:
    for tid in timetables:
        for a in await db.fetch(
                "SELECT id FROM generation_attempts WHERE timetable_id = $1", tid):
            aid = a["id"]
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
                await db.execute(sql, aid)
        await db.execute(
            "DELETE FROM transport_schedule WHERE timetable_id = $1", tid)
        await db.execute(
            "DELETE FROM timetable_versions WHERE timetable_id = $1", tid)
        await db.execute(
            "DELETE FROM generation_attempts WHERE timetable_id = $1", tid)
        await db.execute("DELETE FROM timetables WHERE id = $1", tid)

    for school_id, user_id in schools:
        # By id, not by school. This suite moves its user onto the sandbox
        # school, so deleting "users of the throwaway school" misses them - and
        # because users.id references auth.users(id), the leftover row then
        # makes the Supabase auth delete fail with a 500 and the account is
        # stranded too.
        await db.execute(
            "DELETE FROM notification_preferences WHERE user_id = $1", user_id)
        await db.execute("DELETE FROM users WHERE id = $1", user_id)

        async with db.transaction() as conn:
            for sql in (
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
    original = ai_cluster.call
    ai_cluster.call = StubAI.call  # type: ignore[assignment]
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            # An owner on the sandbox school, so the generated timetable has
            # real data behind it.
            school_id = await db.fetchval(
                "SELECT value FROM settings WHERE key = 'sandbox_school_id'")
            email = f"output.{RUN}@example.com"
            r = await client.post("/auth/signup", json={
                "school_name": f"Output Test {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Test", "surname": "Owner", "email": email,
                "password": PASSWORD, "campuses": [{"name": "Main"}]})
            throwaway_school, user_id = r.json()["school_id"], r.json()["user_id"]
            schools.append((throwaway_school, user_id))

            # Move the user onto the sandbox school so they can read its data.
            await db.execute("UPDATE users SET school_id = $2 WHERE id = $1",
                             user_id, school_id)
            auth = {"Authorization": f"Bearer {await sign_in(email)}"}

            # --- Cost estimate ------------------------------------------------
            r = await client.get("/timetables/estimate", headers=auth)
            check(r.status_code == 200, "GET estimate", f"HTTP {r.status_code}")
            est = r.json()
            check(len(est["lines"]) == 9, "nine cost lines", str(len(est["lines"])))
            check(est["total_credits"] > 0, "a total is calculated",
                  str(est["total_credits"]))
            base = next(l for l in est["lines"] if l["label"] == "Base fee")
            check(base["credits"] == 60, "base fee from settings",
                  str(base["credits"]))
            students = next(l for l in est["lines"] if l["label"] == "Students")
            check(abs(students["credits"] - 48 * 0.20) < 0.01,
                  "student cost matches roll", str(students["credits"]))
            transport_line = next(l for l in est["lines"] if l["label"] == "Transport")
            check(transport_line["credits"] == 40,
                  "transport charged for the two-campus school",
                  str(transport_line["credits"]))
            check(est["billing_mode"] == "credits", "billing mode reported")
            check(est["can_afford"] is False,
                  "cannot afford with a zero balance", str(est["available"]))

            # --- Generate -----------------------------------------------------
            layout_id = await db.fetchval("""
                SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
                ORDER BY created_at DESC LIMIT 1
            """, school_id)
            tid = str(await db.fetchval("""
                INSERT INTO timetables (school_id, name, layout_id, status)
                VALUES ($1, $2, $3, 'processing') RETURNING id
            """, school_id, f"Output Test {RUN}", layout_id))
            timetables.append(tid)

            summary = await pipeline.run_with_retries(tid, "test@example.com")
            await db.execute(
                "UPDATE timetables SET status = 'complete' WHERE id = $1", tid)
            version_id = summary["version_id"]
            check(summary["entries"] > 0, "timetable generated",
                  f"{summary['entries']} entries")

            # --- Listing -------------------------------------------------------
            r = await client.get("/timetables", headers=auth)
            check(r.status_code == 200, "GET timetables", f"HTTP {r.status_code}")
            mine = next((t for t in r.json() if t["id"] == tid), None)
            check(mine is not None, "generated timetable is listed")
            if mine:
                check(mine["versions"] == 1, "version count reported",
                      str(mine["versions"]))
                check(mine["latest_version"] == version_id,
                      "latest version id returned")

            r = await client.get(f"/timetables/{tid}/versions", headers=auth)
            check(r.status_code == 200 and len(r.json()) == 1,
                  "GET versions", f"{len(r.json())} versions")
            check(r.json()[0]["entries"] == summary["entries"],
                  "version entry count matches")

            # --- Summary --------------------------------------------------------
            r = await client.get(f"/timetables/version/{version_id}/summary",
                                 headers=auth)
            check(r.status_code == 200, "GET summary", f"HTTP {r.status_code}")
            s = r.json()
            check(s["status"] == "draft", "new version is a draft")
            check(s["counts"]["entries"] == summary["entries"],
                  "summary entry count matches")
            check(s["counts"]["students"] > 0, "student count reported",
                  str(s["counts"]["students"]))
            check(len(s["days"]) == 5, "five days in the cycle", str(len(s["days"])))
            check(len(s["periods"]) > 0, "teaching periods listed",
                  str(len(s["periods"])))

            # --- Options and grids per view -------------------------------------
            for view, minimum in (("class", 1), ("teacher", 1),
                                  ("room", 1), ("student", 1)):
                r = await client.get(
                    f"/timetables/version/{version_id}/options?view={view}",
                    headers=auth)
                check(r.status_code == 200 and len(r.json()) >= minimum,
                      f"options for {view}", f"{len(r.json())} items")
                if not r.json():
                    continue

                key = r.json()[0]["id"]
                g = await client.get(
                    f"/timetables/version/{version_id}/grid",
                    headers=auth, params={"view": view, "key": key})
                check(g.status_code == 200, f"grid for {view}",
                      f"HTTP {g.status_code}")
                check(g.json()["count"] > 0, f"{view} grid has entries",
                      str(g.json()["count"]))

                # Cells must be keyed day-period and agree with the entry data.
                cells = g.json()["cells"]
                bad = [k for k in cells if "-" not in k]
                check(not bad, f"{view} cells keyed day-period", str(bad[:3]))

            # A teacher's grid must never show two classes in one cell - that is
            # the double-booking the checker forbids, seen from the UI's side.
            # Counting the collisions rather than breaking on the first keeps
            # this a single assertion with a real condition.
            r = await client.get(
                f"/timetables/version/{version_id}/options?view=teacher",
                headers=auth)
            teachers = r.json()
            collisions = []
            for teacher in teachers:
                g = await client.get(
                    f"/timetables/version/{version_id}/grid", headers=auth,
                    params={"view": "teacher", "key": teacher["id"]})
                entries, cells = g.json()["count"], len(g.json()["cells"])
                if entries != cells:
                    collisions.append(f"{teacher['label']}: {entries} into {cells}")

            check(bool(teachers), "teachers found to check",
                  f"{len(teachers)}")
            check(not collisions, "every teacher grid has one class per slot",
                  "; ".join(collisions[:3]))

            r = await client.get(f"/timetables/version/{version_id}/grid",
                                 headers=auth, params={"view": "wombat", "key": "x"})
            check(r.status_code == 422, "unknown view rejected", f"got {r.status_code}")

            r = await client.get(f"/timetables/version/{version_id}/grid",
                                 headers=auth, params={"view": "class"})
            check(r.status_code == 422, "missing key rejected", f"got {r.status_code}")

            # --- Transport and duties --------------------------------------------
            r = await client.get(f"/timetables/version/{version_id}/transport",
                                 headers=auth)
            check(r.status_code == 200, "GET transport", f"HTTP {r.status_code}")
            r = await client.get(f"/timetables/version/{version_id}/duties",
                                 headers=auth)
            check(r.status_code == 200 and len(r.json()) > 0,
                  "GET duties", f"{len(r.json())} assignments")

            # --- Publishing --------------------------------------------------------
            r = await client.post(f"/timetables/version/{version_id}/publish",
                                  headers=auth)
            check(r.status_code == 200, "publish version", f"HTTP {r.status_code}")

            state = await db.fetchval(
                "SELECT status FROM timetable_versions WHERE id = $1", version_id)
            check(state == "published", "version is published", str(state))

            published_by = await db.fetchval(
                "SELECT published_by FROM timetable_versions WHERE id = $1",
                version_id)
            check(str(published_by) == user_id, "publisher recorded")

            # A second version must archive the first. It needs entries of its
            # own: publishing an empty version is refused, because it would
            # leave the school with a live timetable containing nothing.
            v2 = await db.fetchval("""
                INSERT INTO timetable_versions
                    (timetable_id, school_id, version_number, status)
                VALUES ($1, $2, 2, 'draft') RETURNING id
            """, tid, school_id)
            await db.execute("""
                INSERT INTO timetable_entries
                    (timetable_id, version_id, attempt_id, layout_period_id,
                     day_number, room_id, teacher_id, campus_id,
                     class_group_id, subject, class_code)
                SELECT timetable_id, $2, attempt_id, layout_period_id,
                       day_number, room_id, teacher_id, campus_id,
                       class_group_id, subject, class_code
                FROM timetable_entries WHERE version_id = $1
            """, version_id, v2)

            r = await client.post(f"/timetables/version/{v2}/publish", headers=auth)
            check(r.status_code == 200, "publish a second version",
                  f"HTTP {r.status_code} {r.text[:120]}")

            r = await client.post(f"/timetables/version/{v2}/publish", headers=auth)
            check(r.json().get("unchanged") is True,
                  "republishing the live version changes nothing")
            first = await db.fetchval(
                "SELECT status FROM timetable_versions WHERE id = $1", version_id)
            check(first == "archived", "previous version archived", str(first))
            live = await db.fetchval("""
                SELECT count(*) FROM timetable_versions
                WHERE timetable_id = $1 AND status = 'published'
            """, tid)
            check(live == 1, "exactly one published version", str(live))

            # An empty version cannot be made live.
            empty = await db.fetchval("""
                INSERT INTO timetable_versions
                    (timetable_id, school_id, version_number, status)
                VALUES ($1, $2, 3, 'draft') RETURNING id
            """, tid, school_id)
            r = await client.post(f"/timetables/version/{empty}/publish",
                                  headers=auth)
            check(r.status_code == 409, "an empty version cannot be published",
                  f"got {r.status_code}")
            check(await db.fetchval(
                "SELECT status FROM timetable_versions WHERE id = $1", v2)
                == "published", "and the live version is untouched")

            # --- Staff cannot publish ------------------------------------------------
            await db.execute("UPDATE users SET role = 'staff' WHERE id = $1", user_id)
            sb.forget_token(auth["Authorization"].split()[1])
            r = await client.post(f"/timetables/version/{version_id}/publish",
                                  headers=auth)
            check(r.status_code == 403, "staff cannot publish", f"got {r.status_code}")
            r = await client.get(f"/timetables/version/{version_id}/summary",
                                 headers=auth)
            check(r.status_code == 200, "staff can still read output")
            await db.execute("UPDATE users SET role = 'owner' WHERE id = $1", user_id)

            # --- Tenant isolation ------------------------------------------------------
            other_email = f"output.other.{RUN}@example.com"
            r2 = await client.post("/auth/signup", json={
                "school_name": f"Other Output {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Other", "surname": "Owner", "email": other_email,
                "password": PASSWORD, "campuses": [{"name": "Main"}]})
            schools.append((r2.json()["school_id"], r2.json()["user_id"]))
            other = {"Authorization": f"Bearer {await sign_in(other_email)}"}

            for path in (f"/timetables/version/{version_id}/summary",
                         f"/timetables/{tid}/versions"):
                r = await client.get(path, headers=other)
                check(r.status_code == 404,
                      f"another school gets 404 on {path.split('/')[-1]}",
                      f"got {r.status_code}")

            r = await client.post(f"/timetables/version/{version_id}/publish",
                                  headers=other)
            check(r.status_code == 404, "another school cannot publish",
                  f"got {r.status_code}")

        finally:
            ai_cluster.call = original  # type: ignore[assignment]
            await cleanup()
            left = await db.fetchval(
                "SELECT count(*) FROM timetables WHERE name LIKE 'Output Test%'")
            check(left == 0, "test data cleaned up", f"{left} left")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 8 - progress and output\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

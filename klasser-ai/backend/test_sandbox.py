"""
The free sample run (Phase 3, promised on the marketing site).

Runs a real generation on the demonstration school for a brand-new school with
no data of its own, then checks the grant is exactly what it claims: read-only,
one timetable, once.

The cases that matter are the boundary ones. A sample must be viewable and
nothing else - not publishable, not editable, not exportable, and invisible to
every school that was not granted it.

    python test_sandbox.py
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
from services import ai_cluster, pipeline, queue, sandbox  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.pipeline").setLevel(logging.CRITICAL)
logging.getLogger("klasser.deterministic").setLevel(logging.CRITICAL)
logging.getLogger("klasser.sandbox").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []
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


async def make_school(client, tag: str) -> tuple[dict, str, str]:
    email = f"sbx.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Sandbox {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": tag.title(), "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))
    return ({"Authorization": f"Bearer {await sign_in(email)}"},
            school_id, user_id)


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
        for sql in (
            "UPDATE onboarding SET sandbox_timetable_id = NULL "
            "  WHERE sandbox_timetable_id = $1",
            "DELETE FROM transport_schedule WHERE timetable_id = $1",
            "DELETE FROM job_queue WHERE timetable_id = $1",
            "DELETE FROM timetable_versions WHERE timetable_id = $1",
            "DELETE FROM generation_attempts WHERE timetable_id = $1",
            "DELETE FROM timetables WHERE id = $1",
        ):
            await db.execute(sql, tid)

    for school_id, user_id in schools:
        await db.execute(
            "DELETE FROM notification_preferences WHERE user_id = $1", user_id)
        async with db.transaction() as conn:
            for sql in (
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
            sandbox_id = await db.fetchval(
                "SELECT value FROM settings WHERE key = 'sandbox_school_id'")
            check(bool(sandbox_id), "the demonstration school is configured")

            auth, school_id, user_id = await make_school(client, "a")
            other_auth, other_school, _ = await make_school(client, "b")

            # --- Offered to a school with no data of its own -------------------
            r = await client.get("/onboarding/sandbox", headers=auth)
            check(r.status_code == 200, "GET sandbox state",
                  f"HTTP {r.status_code}")
            check(r.json()["available"] is True,
                  "a new school is offered the free run")
            check(r.json()["timetable_id"] is None, "and has no sample yet")

            balance = await db.fetchval(
                "SELECT balance FROM school_credits WHERE school_id = $1",
                school_id)
            check(balance == 0, "the school has no credits", str(balance))

            # A normal generation is impossible for them - which is the point.
            r = await client.post("/allocation/start", headers=auth,
                                  json={"name": f"Real {RUN}"})
            check(r.status_code == 422,
                  "they cannot generate normally - no layout, no data",
                  f"got {r.status_code}")

            # --- Start it --------------------------------------------------------
            r = await client.post("/onboarding/sandbox", headers=auth)
            check(r.status_code == 202, "start the sample run",
                  f"HTTP {r.status_code} {r.text[:140]}")
            started = r.json()
            timetable_id = started["timetable_id"]
            timetables.append(timetable_id)
            check(started["credits_charged"] == 0, "nothing is charged",
                  str(started["credits_charged"]))

            owner_of = await db.fetchval(
                "SELECT school_id FROM timetables WHERE id = $1", timetable_id)
            check(str(owner_of) == str(sandbox_id),
                  "the timetable belongs to the demonstration school, "
                  "not theirs")

            balance_after = await db.fetchval(
                "SELECT balance FROM school_credits WHERE school_id = $1",
                school_id)
            check(balance_after == balance, "their balance is untouched",
                  f"{balance} -> {balance_after}")
            held = await db.fetchval("""
                SELECT count(*) FROM generation_cost_breakdown
                WHERE timetable_id = $1
            """, timetable_id)
            check(held == 0, "and no credits were even reserved", str(held))

            # --- Once, and only once ------------------------------------------------
            r = await client.post("/onboarding/sandbox", headers=auth)
            check(r.status_code == 409, "a second run is refused",
                  f"got {r.status_code}")

            r = await client.get("/onboarding/sandbox", headers=auth)
            check(r.json()["available"] is False, "the offer is gone")
            check(r.json()["timetable_id"] == timetable_id,
                  "and points at their sample")

            runs = await db.fetchval("""
                SELECT count(*) FROM timetables
                WHERE school_id = $1 AND name = $2
            """, sandbox_id, f"Sample for Sandbox a {RUN}")
            check(runs == 1, "exactly one sample timetable exists", str(runs))

            # --- They can watch it -----------------------------------------------------
            r = await client.get(
                f"/allocation/timetable/{timetable_id}/attempts", headers=auth)
            check(r.status_code == 200,
                  "they can read the attempts of a timetable they do not own",
                  f"got {r.status_code}")

            r = await client.get(
                f"/allocation/timetable/{timetable_id}/attempts",
                headers=other_auth)
            check(r.status_code == 404,
                  "another school cannot", f"got {r.status_code}")

            # --- Run it for real ----------------------------------------------------------
            # Read before claiming: the Job dataclass does not carry priority,
            # and the stored value is what the queue actually orders by.
            priority = await db.fetchval("""
                SELECT priority FROM job_queue WHERE timetable_id = $1
            """, timetable_id)
            check(priority == queue.PRIORITY_URGENT,
                  "queued at urgent priority - someone is watching it",
                  f"{priority} vs {queue.PRIORITY_URGENT}")

            job = await queue.claim("test-worker")
            check(job is not None and str(job.timetable_id) == timetable_id,
                  "the run was queued")

            summary = await pipeline.run_with_retries(timetable_id,
                                                      "sbx@example.com")
            await queue.complete(job.id)
            version_id = summary["version_id"]
            await db.execute("""
                UPDATE timetables SET status = 'complete', completed_at = now()
                WHERE id = $1
            """, timetable_id)

            check(summary["entries"] > 0, "a real timetable was produced",
                  f"{summary['entries']} entries")

            # --- Reading the result ----------------------------------------------------------
            r = await client.get(f"/timetables/version/{version_id}/summary",
                                 headers=auth)
            check(r.status_code == 200, "they can open the sample",
                  f"got {r.status_code}")
            check(r.json()["is_sample"] is True,
                  "and it is marked as a sample - the watermark")
            check(r.json()["counts"]["entries"] > 0, "with real content",
                  str(r.json()["counts"]["entries"]))

            # Ask for a class that really is in this version. A hardcoded code
            # that happens not to exist would return an empty grid and a 200,
            # proving nothing about whether they can read the sample.
            classes = (await client.get(
                f"/timetables/version/{version_id}/options",
                headers=auth, params={"view": "class"})).json()
            check(len(classes) > 0, "the sample has classes to look at",
                  str(len(classes)))

            r = await client.get(f"/timetables/version/{version_id}/grid",
                                 headers=auth,
                                 params={"view": "class", "key": classes[0]["id"]})
            check(r.status_code == 200, "and browse the grid",
                  f"got {r.status_code}")
            check(r.json()["count"] > 0,
                  "with actual lessons in it",
                  f"{r.json()['count']} cells for {classes[0]['id']}")

            r = await client.get(f"/timetables/version/{version_id}/options",
                                 headers=auth, params={"view": "teacher"})
            check(r.status_code == 200 and len(r.json()) > 0,
                  "and see the sandbox school's teachers",
                  f"{len(r.json()) if r.status_code == 200 else r.status_code}")

            # --- The grant is read-only ----------------------------------------------------------
            r = await client.post(f"/timetables/version/{version_id}/publish",
                                  headers=auth)
            check(r.status_code == 404, "a sample cannot be published",
                  f"got {r.status_code}")

            r = await client.delete(f"/timetables/version/{version_id}",
                                    headers=auth)
            check(r.status_code == 404, "nor discarded", f"got {r.status_code}")

            r = await client.post("/editing/request", headers=auth, json={
                "version_id": version_id, "request_text": "move something"})
            check(r.status_code == 404, "nor edited", f"got {r.status_code}")

            template = await client.post("/export/templates", headers=auth, json={
                "name": f"T {RUN}", "output_type": "timetable_entries",
                "columns": [{"label": "Class", "source": "class_code",
                             "format": None}],
                "delimiter": ",", "include_header": True})
            r = await client.get(
                f"/export/download/{version_id}/{template.json()['id']}",
                headers=auth)
            check(r.status_code == 404, "nor exported as their own",
                  f"got {r.status_code}")
            await db.execute("DELETE FROM csv_output_templates WHERE id = $1",
                             template.json()["id"])

            published = await db.fetchval(
                "SELECT status FROM timetable_versions WHERE id = $1", version_id)
            check(published == "draft",
                  "and none of that changed the version", str(published))

            # --- Nobody else can see it ---------------------------------------------------------------
            r = await client.get(f"/timetables/version/{version_id}/summary",
                                 headers=other_auth)
            check(r.status_code == 404,
                  "a school that was not granted it sees nothing",
                  f"got {r.status_code}")
            r = await client.get(f"/timetables/version/{version_id}/grid",
                                 headers=other_auth,
                                 params={"view": "class", "key": "10ENG1"})
            check(r.status_code == 404, "on any output endpoint",
                  f"got {r.status_code}")

            # Their own timetable list must not show the sandbox school's work.
            r = await client.get("/timetables", headers=auth)
            check(all(t["id"] != timetable_id for t in r.json()),
                  "the sample never appears in their own timetable list",
                  f"{len(r.json())} rows")

            # --- The demonstration school itself ---------------------------------------------------------
            r = await client.get("/onboarding/sandbox", headers=auth)
            check(r.json()["version_id"] == version_id,
                  "the dashboard can find the finished sample")
            check(r.json()["status"] == "complete", "and knows it is done",
                  str(r.json()["status"]))

        finally:
            ai_cluster.call = original  # type: ignore[assignment]
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM timetables WHERE name LIKE $1",
                    f"Sample for Sandbox%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nThe free sample run\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

"""
Job queue tests.

The queue replaced FastAPI background_tasks, so its two promises need proving
rather than assuming: concurrent workers never take the same job, and a worker
that dies mid-job does not lose the work. Both are the kind of property that
looks fine until it silently is not.

    python test_queue.py
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
from services import queue  # noqa: E402
from services import settings as settings_service  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.queue").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []
layouts: dict[str, str] = {}
timetables: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(email: str):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def make_school(client, tag: str) -> tuple[dict, str, str]:
    email = f"queue.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Queue {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": "Owner", "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))

    layouts[school_id] = str(await db.fetchval("""
        INSERT INTO timetable_layouts (school_id, name, days_in_cycle, is_active)
        VALUES ($1, 'Queue test layout', 5, true) RETURNING id
    """, school_id))

    return {"Authorization": f"Bearer {await sign_in(email)}"}, school_id, user_id


async def make_timetable(school_id: str, name: str) -> str:
    tid = str(await db.fetchval("""
        INSERT INTO timetables (school_id, name, layout_id, status)
        VALUES ($1, $2, $3, 'processing') RETURNING id
    """, school_id, name, layouts[school_id]))
    timetables.append(tid)
    return tid


async def cleanup() -> None:
    for school_id, _ in schools:
        await db.execute("DELETE FROM job_queue WHERE school_id = $1", school_id)
    for tid in timetables:
        await db.execute("DELETE FROM timetables WHERE id = $1", tid)
    for school_id, user_id in schools:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM notification_preferences WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM onboarding WHERE school_id = $1",
                "DELETE FROM school_complexity WHERE school_id = $1",
                "DELETE FROM school_billing WHERE school_id = $1",
                "DELETE FROM school_credits WHERE school_id = $1",
                "DELETE FROM timetable_layouts WHERE school_id = $1",
                "DELETE FROM campuses WHERE school_id = $1",
                "DELETE FROM users WHERE school_id = $1",
                "DELETE FROM schools WHERE id = $1",
            ):
                await conn.execute(sql, school_id)
        await sb.delete_auth_user(user_id)


async def status_of(job_id: str) -> str:
    return await db.fetchval("SELECT status FROM job_queue WHERE id = $1", job_id)


async def main_test() -> None:
    await db.connect()
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            auth, school_id, user_id = await make_school(client, "a")

            # --- Enqueue ------------------------------------------------------
            t1 = await make_timetable(school_id, f"Queue one {RUN}")
            job1 = await queue.enqueue("generate", school_id, timetable_id=t1,
                                       payload={"notify_email": "a@example.com"})
            check(job1 is not None, "enqueue returns a job id")
            check(await status_of(job1) == "queued", "new job is queued")

            # --- One live job per timetable -------------------------------------
            duplicate = await queue.enqueue("generate", school_id, timetable_id=t1)
            check(duplicate is None,
                  "a second live job for the same timetable is refused")
            n = await db.fetchval("""
                SELECT count(*) FROM job_queue WHERE timetable_id = $1
            """, t1)
            check(n == 1, "only one job row exists for that timetable", str(n))

            # --- Priority ordering ------------------------------------------------
            t2 = await make_timetable(school_id, f"Queue two {RUN}")
            urgent = await queue.enqueue("generate", school_id, timetable_id=t2,
                                         priority=queue.PRIORITY_URGENT)
            claimed = await queue.claim("test-worker")
            check(claimed is not None and claimed.id == urgent,
                  "urgent job is claimed before the standard one",
                  claimed.id if claimed else "nothing claimed")
            check(claimed.attempts == 1, "claiming counts an attempt",
                  str(claimed.attempts) if claimed else "")
            check(await status_of(urgent) == "running", "claimed job is running")

            # --- Concurrent claims take different jobs -------------------------------
            # SKIP LOCKED is the whole reason the queue is safe to run in more
            # than one worker. Two claims at once must never return the same row.
            t3 = await make_timetable(school_id, f"Queue three {RUN}")
            t4 = await make_timetable(school_id, f"Queue four {RUN}")
            await queue.enqueue("generate", school_id, timetable_id=t3)
            await queue.enqueue("generate", school_id, timetable_id=t4)

            both = await asyncio.gather(queue.claim("worker-1"),
                                        queue.claim("worker-2"))
            got = [j for j in both if j is not None]
            check(len(got) == 2, "two workers each claimed a job", str(len(got)))
            check(len({j.id for j in got}) == len(got),
                  "concurrent claims never return the same job",
                  f"{[j.id[:8] for j in got]}")

            # Oldest first among equal priorities. job1 was queued before t3 and
            # t4, so it must be one of the two taken - an earlier version of this
            # test wrongly expected it to be left behind.
            check(job1 in {j.id for j in got},
                  "equal priorities are claimed oldest first",
                  f"job1 {'was' if job1 in {j.id for j in got} else 'was not'} claimed")

            # --- Nothing left to claim ------------------------------------------------
            for job in got:
                await queue.complete(job.id)
            await queue.complete(urgent)

            leftover = await queue.claim("worker-3")
            check(leftover is not None, "the last queued job is claimed")
            if leftover:
                await queue.complete(leftover.id)

            empty = await queue.claim("worker-4")
            check(empty is None, "claiming an empty queue returns nothing")

            # --- Retry with backoff -------------------------------------------------------
            t5 = await make_timetable(school_id, f"Queue five {RUN}")
            job5 = await queue.enqueue("generate", school_id, timetable_id=t5,
                                       max_attempts=2)
            await queue.claim("worker-5")
            outcome = await queue.fail(job5, "provider timed out", retry=True)
            check(outcome == "queued", "a failed job with attempts left requeues",
                  outcome)

            row = await db.fetchrow("""
                SELECT status, available_at > now() AS delayed, last_error
                FROM job_queue WHERE id = $1
            """, job5)
            check(row["delayed"], "requeued job is delayed by backoff")
            check("timed out" in (row["last_error"] or ""),
                  "the failure reason is kept", row["last_error"])

            not_yet = await queue.claim("worker-6")
            check(not_yet is None,
                  "a job still inside its backoff is not claimed",
                  not_yet.id if not_yet else "")

            # --- Dead letter ------------------------------------------------------------------
            await db.execute(
                "UPDATE job_queue SET available_at = now() WHERE id = $1", job5)
            await queue.claim("worker-7")
            outcome = await queue.fail(job5, "failed again", retry=True)
            check(outcome == "failed",
                  "a job out of attempts is not requeued", outcome)
            check(await status_of(job5) == "failed",
                  "exhausted job becomes a dead letter")

            # --- Stale worker recovery ----------------------------------------------------------
            # The durability promise: a worker killed mid-job must not take the
            # work with it.
            t6 = await make_timetable(school_id, f"Queue six {RUN}")
            job6 = await queue.enqueue("generate", school_id, timetable_id=t6,
                                       max_attempts=3)
            taken = await queue.claim("worker-that-dies")
            check(taken is not None and taken.id == job6, "job claimed")

            await settings_service.set_value("queue_stale_seconds", "1")
            settings_service.invalidate("queue_stale_seconds")
            await db.execute("""
                UPDATE job_queue SET heartbeat_at = now() - interval '10 seconds'
                WHERE id = $1
            """, job6)

            reclaimed = await queue.reap_stale()
            check(reclaimed >= 1, "stale job reclaimed", str(reclaimed))
            check(await status_of(job6) == "queued",
                  "a dead worker's job returns to the queue")

            again = await queue.claim("worker-8")
            check(again is not None and again.id == job6,
                  "the recovered job can be claimed again")

            # --- Heartbeat keeps a job alive ------------------------------------------------------
            await queue.heartbeat(job6)
            still = await queue.reap_stale()
            check(await status_of(job6) == "running",
                  "a job with a fresh heartbeat is not reclaimed",
                  f"{still} reclaimed")

            # --- Reaping gives up after max attempts -------------------------------------------------
            await db.execute("""
                UPDATE job_queue
                SET attempts = max_attempts,
                    heartbeat_at = now() - interval '10 seconds'
                WHERE id = $1
            """, job6)
            await queue.reap_stale()
            check(await status_of(job6) == "failed",
                  "a repeatedly stale job is failed, not requeued forever")
            tt_status = await db.fetchval(
                "SELECT status FROM timetables WHERE id = $1", t6)
            check(tt_status == "failed",
                  "its timetable is marked failed rather than left processing",
                  str(tt_status))

            await settings_service.set_value("queue_stale_seconds", "180")
            settings_service.invalidate("queue_stale_seconds")

            # --- Cancel --------------------------------------------------------------------------------
            t7 = await make_timetable(school_id, f"Queue seven {RUN}")
            job7 = await queue.enqueue("generate", school_id, timetable_id=t7)
            check(await queue.cancel(job7, school_id), "a queued job can be cancelled")
            check(await status_of(job7) == "cancelled", "cancelled status recorded")

            t8 = await make_timetable(school_id, f"Queue eight {RUN}")
            job8 = await queue.enqueue("generate", school_id, timetable_id=t8)
            await queue.claim("worker-9")
            check(not await queue.cancel(job8, school_id),
                  "a running job cannot be cancelled")

            other_auth, other_school, _ = await make_school(client, "b")
            t9 = await make_timetable(other_school, f"Queue nine {RUN}")
            job9 = await queue.enqueue("generate", other_school, timetable_id=t9)
            check(not await queue.cancel(job9, school_id),
                  "a job cannot be cancelled by another school")

            # --- Stats and listing ----------------------------------------------------------------------
            stats = await queue.stats()
            check(stats["failed"] >= 2, "failed jobs counted", str(stats["failed"]))
            check(stats["cancelled"] >= 1, "cancelled jobs counted",
                  str(stats["cancelled"]))

            mine = await queue.recent(school_id, limit=50)
            check(all(str(j["id"]) for j in mine), "recent jobs listed",
                  f"{len(mine)} rows")
            other_ids = {str(j["id"]) for j in await queue.recent(other_school)}
            check(job9 in other_ids, "other school sees its own job")
            check(job9 not in {str(j["id"]) for j in mine},
                  "a school does not see another school's jobs")

            # --- The API queues rather than running inline -----------------------------------------------
            r = await client.get("/allocation/queue", headers=auth)
            check(r.status_code == 200, "GET /allocation/queue", f"HTTP {r.status_code}")
            check("queue" in r.json() and "jobs" in r.json(),
                  "queue endpoint reports jobs and totals")

        finally:
            try:
                await settings_service.set_value("queue_stale_seconds", "180")
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM schools WHERE name LIKE $1", f"Queue%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nJob queue\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

"""
The live progress stream.

This was the one part of the generation pipeline with no automated coverage at
all - filed for months as "needs a human", on the grounds that EventSource needs
a browser. The browser half does. The server half is where every decision lives:
who may watch, what is sent, in what order, and when it stops. That is testable,
and it is the half that can break a demonstration with no warning.

    python test_sse.py
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
from services import settings as settings_service  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.allocation").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []
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


async def make_school(client, tag: str) -> tuple[str, str, str]:
    """Returns (token, school_id, layout_id)."""
    email = f"sse.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"SSE {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": tag.title(), "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))

    layout_id = str(await db.fetchval("""
        INSERT INTO timetable_layouts (school_id, name, days_in_cycle, is_active)
        VALUES ($1, 'SSE layout', 5, true) RETURNING id
    """, school_id))

    return await sign_in(email), school_id, layout_id


async def make_attempt(school_id: str, layout_id: str, name: str) -> tuple[str, str]:
    """A timetable and an attempt with a job row, as a real run would leave."""
    tid = str(await db.fetchval("""
        INSERT INTO timetables (school_id, name, layout_id, status)
        VALUES ($1, $2, $3, 'processing') RETURNING id
    """, school_id, name, layout_id))
    timetables.append(tid)

    attempt_id = str(await db.fetchval("""
        INSERT INTO generation_attempts (timetable_id, attempt_number, status)
        VALUES ($1, 1, 'in_progress') RETURNING id
    """, tid))

    await db.execute("""
        INSERT INTO generation_jobs (attempt_id, status, current_stage,
                                     progress_pct, notify_email)
        VALUES ($1, 'running', 'layer1_subject_map', 5, 'sse@example.com')
    """, attempt_id)

    return tid, attempt_id


async def emit(attempt_id: str, event_type: str, message: str,
               detail: dict | None = None) -> None:
    await db.execute("""
        INSERT INTO generation_events (attempt_id, event_type, message, detail)
        VALUES ($1, $2, $3, $4)
    """, attempt_id, event_type, message,
        json.dumps(detail) if detail else None)


async def read_stream(client, attempt_id: str, token: str,
                      timeout: float = 20.0) -> list[dict]:
    """
    Consume a whole SSE response and parse it.

    The stream ends by itself once the job is terminal, which is what makes this
    testable at all - so a run that never ends would hang here rather than fail,
    and the timeout is the guard against that.
    """
    events: list[dict] = []
    async with client.stream(
            "GET", f"/allocation/{attempt_id}/progress",
            params={"token": token}, timeout=timeout) as response:
        if response.status_code != 200:
            await response.aread()
            raise AssertionError(f"HTTP {response.status_code}")
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def cleanup() -> None:
    for tid in timetables:
        for a in await db.fetch(
                "SELECT id FROM generation_attempts WHERE timetable_id = $1", tid):
            await db.execute(
                "DELETE FROM generation_events WHERE attempt_id = $1", a["id"])
            await db.execute(
                "DELETE FROM generation_jobs WHERE attempt_id = $1", a["id"])
        await db.execute(
            "DELETE FROM generation_attempts WHERE timetable_id = $1", tid)
        await db.execute("DELETE FROM timetables WHERE id = $1", tid)

    for school_id, user_id in schools:
        await db.execute(
            "DELETE FROM notification_preferences WHERE user_id = $1", user_id)
        async with db.transaction() as conn:
            for sql in (
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
    original_poll = await settings_service.get("sse_poll_seconds")
    original_max = await settings_service.get("sse_max_seconds")

    # A one-second poll would make every assertion below wait a second.
    await settings_service.set_value("sse_poll_seconds", "0.05")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            token, school_id, layout_id = await make_school(client, "a")
            other_token, other_school, other_layout = await make_school(client, "b")

            # --- Who may watch -------------------------------------------------
            tid, attempt_id = await make_attempt(school_id, layout_id,
                                                 f"SSE auth {RUN}")

            r = await client.get(f"/allocation/{attempt_id}/progress")
            check(r.status_code == 401, "no token is refused",
                  f"got {r.status_code}")

            r = await client.get(f"/allocation/{attempt_id}/progress",
                                 params={"token": "not-a-real-token"})
            check(r.status_code == 401, "a bad token is refused",
                  f"got {r.status_code}")

            r = await client.get(f"/allocation/{attempt_id}/progress",
                                 params={"token": other_token})
            check(r.status_code == 404,
                  "another school cannot watch this generation",
                  f"got {r.status_code}")

            unknown = str(uuid.uuid4())
            r = await client.get(f"/allocation/{unknown}/progress",
                                 params={"token": token})
            check(r.status_code == 404, "an unknown attempt is refused",
                  f"got {r.status_code}")

            # Nothing may be streamed before the check - a 401 must not arrive
            # with events already in the body.
            r = await client.get(f"/allocation/{attempt_id}/progress",
                                 params={"token": other_token})
            check("data:" not in r.text,
                  "a refused request streams nothing at all", r.text[:60])

            # --- What it sends ---------------------------------------------------
            await emit(attempt_id, "stage_start", "Mapping subjects to campuses")
            await emit(attempt_id, "chunk_complete", "Day 1 of 5",
                       {"day": 1, "of": 5})
            await emit(attempt_id, "stage_complete", "Subjects mapped")

            # Terminal, so the stream closes rather than polling forever.
            await db.execute("""
                UPDATE generation_jobs
                SET status = 'complete', current_stage = 'layer10_validation',
                    progress_pct = 100
                WHERE attempt_id = $1
            """, attempt_id)

            events = await read_stream(client, attempt_id, token)
            check(len(events) >= 4, "the stream delivers the events",
                  f"{len(events)} frames")

            messages = [e.get("message") for e in events]
            check(messages[:3] == ["Mapping subjects to campuses", "Day 1 of 5",
                                   "Subjects mapped"],
                  "in the order they happened", str(messages[:3]))

            types = [e.get("type") for e in events]
            check(types[:3] == ["stage_start", "chunk_complete",
                                "stage_complete"],
                  "with their event types", str(types[:3]))

            chunk = next(e for e in events if e.get("type") == "chunk_complete")
            check(chunk["detail"] == {"day": 1, "of": 5},
                  "and their detail, decoded from JSON", str(chunk["detail"]))

            progress = [e for e in events if e.get("type") == "progress"]
            check(progress, "a progress frame is sent")
            check(progress[-1]["progress_pct"] == 100,
                  "carrying the percentage", str(progress[-1]["progress_pct"]))
            check(progress[-1]["current_stage"] == "layer10_validation",
                  "and the current stage", str(progress[-1]["current_stage"]))
            check(progress[-1]["status"] == "complete",
                  "and the job status the page needs to stop watching")

            # --- It closes on its own -----------------------------------------------
            check(events[-1].get("type") == "progress",
                  "the last frame is the terminal progress frame",
                  str(events[-1].get("type")))
            check(all(e.get("type") != "stream_timeout" for e in events),
                  "and it finished properly rather than timing out")

            # --- A failed run also ends ----------------------------------------------
            tid2, attempt2 = await make_attempt(school_id, layout_id,
                                                f"SSE failed {RUN}")
            await emit(attempt2, "error", "Day 3: teacher double-booked")
            await db.execute("""
                UPDATE generation_jobs SET status = 'failed', progress_pct = 40
                WHERE attempt_id = $1
            """, attempt2)

            events = await read_stream(client, attempt2, token)
            check(any(e.get("message", "").startswith("Day 3") for e in events),
                  "a failure is streamed before the stream ends",
                  str([e.get("message") for e in events][:2]))
            check(events[-1].get("status") == "failed",
                  "and the last frame says it failed",
                  str(events[-1].get("status")))

            # --- The timeout, which had no cap at all until now -------------------------
            tid3, attempt3 = await make_attempt(school_id, layout_id,
                                                f"SSE stuck {RUN}")
            await emit(attempt3, "stage_start", "Working")
            # Job left 'running' forever: a worker killed mid-run looks exactly
            # like this, and before the cap the stream polled until the tab shut.
            await settings_service.set_value("sse_max_seconds", "1")

            events = await read_stream(client, attempt3, token, timeout=20.0)
            check(events[-1].get("type") == "stream_timeout",
                  "a stream whose job never finishes gives up",
                  str(events[-1].get("type")))
            check("reconnect" in events[-1].get("message", "").lower(),
                  "and says so, rather than going silent",
                  events[-1].get("message", ""))
            still_running = await db.fetchval(
                "SELECT status FROM generation_jobs WHERE attempt_id = $1",
                attempt3)
            check(still_running == "running",
                  "without touching the job it was watching", str(still_running))

            # --- Resuming ---------------------------------------------------------------
            await settings_service.set_value("sse_max_seconds", "1800")
            await db.execute("""
                UPDATE generation_jobs SET status = 'complete', progress_pct = 100
                WHERE attempt_id = $1
            """, attempt3)
            events = await read_stream(client, attempt3, token)
            check(any(e.get("message") == "Working" for e in events),
                  "reconnecting replays the events from the beginning",
                  f"{len(events)} frames")
            check(events[-1].get("status") == "complete",
                  "and now sees the finished job")

            # --- The polling endpoint agrees ----------------------------------------------
            r = await client.get(f"/allocation/{attempt_id}/events",
                                 headers={"Authorization": f"Bearer {token}"})
            check(r.status_code == 200, "GET events", f"HTTP {r.status_code}")
            polled = [e["message"] for e in r.json()]
            check(polled == ["Mapping subjects to campuses", "Day 1 of 5",
                             "Subjects mapped"],
                  "the polling fallback returns the same events",
                  str(polled))

            r = await client.get(f"/allocation/{attempt_id}/events",
                                 headers={"Authorization": f"Bearer {token}"},
                                 params={"after": 2})
            check([e["message"] for e in r.json()] == ["Subjects mapped"],
                  "and can resume from an offset",
                  str([e["message"] for e in r.json()]))

            r = await client.get(f"/allocation/{attempt_id}/events",
                                 headers={"Authorization": f"Bearer {other_token}"})
            check(r.status_code == 404,
                  "another school cannot poll it either",
                  f"got {r.status_code}")

        finally:
            if original_poll is not None:
                await settings_service.set_value("sse_poll_seconds", original_poll)
            if original_max is not None:
                await settings_service.set_value("sse_max_seconds", original_max)
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM timetables WHERE name LIKE $1",
                    f"SSE %{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nLive progress stream (SSE)\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

"""
Durable job queue, backed by Postgres.

Replaces FastAPI background_tasks, which loses work whenever the process
restarts: a generation in flight simply vanishes and its timetable sits in
`processing` forever with nothing coming to finish it.

Claiming uses `FOR UPDATE SKIP LOCKED`, the standard Postgres queue pattern -
two workers asking for work at the same moment take different rows rather than
blocking or double-running one. Pickup is driven by LISTEN/NOTIFY so a job
starts within milliseconds, with a poll as a fallback in case a notification is
missed.

A worker that dies mid-job leaves its row `running` with a stale heartbeat;
`reap_stale()` returns those to the queue.
"""

import asyncio
import json
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from models import database as db
from services import settings as settings_service

log = logging.getLogger("klasser.queue")

CHANNEL = "klasser_jobs"

PRIORITY_URGENT = 10
PRIORITY_STANDARD = 100


@dataclass
class Job:
    id: str
    job_type: str
    school_id: str
    timetable_id: Optional[str]
    payload: dict
    attempts: int
    max_attempts: int

    @property
    def last_chance(self) -> bool:
        return self.attempts >= self.max_attempts


def worker_name() -> str:
    """Identifies which process holds a claim, for debugging a stuck queue."""
    return f"{socket.gethostname()}:{os.getpid()}"


# --- Enqueue -----------------------------------------------------------------

async def enqueue(job_type: str, school_id: str, *,
                  timetable_id: Optional[str] = None,
                  payload: Optional[dict] = None,
                  priority: int = PRIORITY_STANDARD,
                  max_attempts: int = 3) -> Optional[str]:
    """
    Add a job and wake a worker.

    Returns None if an identical live job already exists - a unique partial
    index allows only one queued or running job per timetable, so a
    double-submitted generation cannot run twice and charge twice.
    """
    try:
        job_id = await db.fetchval("""
            INSERT INTO job_queue
                (job_type, school_id, timetable_id, payload, priority,
                 max_attempts)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """, job_type, school_id, timetable_id,
            json.dumps(payload or {}), priority, max_attempts)
    except Exception as exc:  # noqa: BLE001
        if "idx_job_queue_one_live_per_timetable" in str(exc):
            log.warning("Job for timetable %s already queued - ignoring",
                        timetable_id)
            return None
        raise

    await notify()
    log.info("Queued %s for school %s (priority %s)", job_type, school_id, priority)
    return str(job_id)


async def notify() -> None:
    """
    Wake any listening worker immediately.

    pg_notify() takes the channel as a parameter, where the NOTIFY statement
    needs it interpolated into the SQL text. Same effect, no string building.
    """
    try:
        await db.execute("SELECT pg_notify($1, '')", CHANNEL)
    except Exception:
        # A failed notification only costs latency - the poll will find it.
        log.debug("NOTIFY failed; workers will pick this up on the next poll")


# --- Claim -------------------------------------------------------------------

async def claim(worker: str) -> Optional[Job]:
    """
    Take the next runnable job, or None.

    SKIP LOCKED is what makes this safe to run in several workers at once:
    a row already locked by another claimer is passed over rather than waited
    on, so workers never block each other and never take the same job.
    """
    row = await db.fetchrow("""
        WITH next AS (
            SELECT id FROM job_queue
            WHERE status = 'queued' AND available_at <= now()
            ORDER BY priority, available_at, created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE job_queue j
        SET status = 'running',
            attempts = j.attempts + 1,
            claimed_at = now(),
            claimed_by = $1,
            heartbeat_at = now()
        FROM next
        WHERE j.id = next.id
        RETURNING j.id, j.job_type, j.school_id, j.timetable_id, j.payload,
                  j.attempts, j.max_attempts
    """, worker)

    if row is None:
        return None

    return Job(
        id=str(row["id"]),
        job_type=row["job_type"],
        school_id=str(row["school_id"]),
        timetable_id=str(row["timetable_id"]) if row["timetable_id"] else None,
        payload=json.loads(row["payload"]) if isinstance(row["payload"], str)
        else (row["payload"] or {}),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
    )


async def heartbeat(job_id: str) -> None:
    await db.execute("""
        UPDATE job_queue SET heartbeat_at = now()
        WHERE id = $1 AND status = 'running'
    """, job_id)


# --- Finish ------------------------------------------------------------------

async def complete(job_id: str) -> None:
    await db.execute("""
        UPDATE job_queue
        SET status = 'complete', completed_at = now(), last_error = NULL
        WHERE id = $1
    """, job_id)


async def fail(job_id: str, error: str, *, retry: bool = True) -> str:
    """
    Record a failure, requeueing with backoff if attempts remain.

    Returns 'queued' if it will run again, 'failed' if it will not. A job that
    exhausts its attempts stays in the table as a dead letter rather than being
    deleted, so the dev portal can show what went wrong.
    """
    row = await db.fetchrow(
        "SELECT attempts, max_attempts FROM job_queue WHERE id = $1", job_id)
    if row is None:
        return "failed"

    if not retry or row["attempts"] >= row["max_attempts"]:
        await db.execute("""
            UPDATE job_queue
            SET status = 'failed', completed_at = now(), last_error = $2
            WHERE id = $1
        """, job_id, error[:2000])
        log.error("Job %s failed permanently after %s attempts: %s",
                  job_id, row["attempts"], error[:200])
        return "failed"

    # Exponential backoff, so a provider outage is not hammered.
    delay = min(300, 15 * (2 ** (row["attempts"] - 1)))
    await db.execute("""
        UPDATE job_queue
        SET status = 'queued', available_at = now() + ($2 || ' seconds')::interval,
            last_error = $3, claimed_by = NULL, claimed_at = NULL,
            heartbeat_at = NULL
        WHERE id = $1
    """, job_id, str(delay), error[:2000])

    log.warning("Job %s failed (attempt %s), retrying in %ss: %s",
                job_id, row["attempts"], delay, error[:200])
    return "queued"


async def cancel(job_id: str, school_id: str) -> bool:
    """Cancel a job that has not started. A running job is left alone."""
    cancelled = await db.fetchval("""
        UPDATE job_queue SET status = 'cancelled', completed_at = now()
        WHERE id = $1 AND school_id = $2 AND status = 'queued'
        RETURNING id
    """, job_id, school_id)
    return cancelled is not None


# --- Recovery ----------------------------------------------------------------

async def reap_stale() -> int:
    """
    Requeue jobs whose worker stopped reporting.

    This is what makes the queue durable: a worker killed mid-generation leaves
    its row `running` forever, and nothing else would ever pick it up.
    """
    stale_after = await settings_service.get_int("queue_stale_seconds", 180)

    rows = await db.fetch("""
        UPDATE job_queue
        SET status = CASE WHEN attempts >= max_attempts THEN 'failed'
                          ELSE 'queued' END,
            claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL,
            last_error = 'worker stopped responding',
            completed_at = CASE WHEN attempts >= max_attempts THEN now() END
        WHERE status = 'running'
          AND heartbeat_at < now() - ($1 || ' seconds')::interval
        RETURNING id, timetable_id, attempts, max_attempts
    """, str(stale_after))

    for row in rows:
        log.warning("Reclaimed stale job %s (attempt %s of %s)",
                    row["id"], row["attempts"], row["max_attempts"])
        # The timetable was left mid-run; mark it failed if we are giving up.
        if row["attempts"] >= row["max_attempts"] and row["timetable_id"]:
            await db.execute("""
                UPDATE timetables SET status = 'failed' WHERE id = $1
            """, row["timetable_id"])

    if rows:
        await notify()
    return len(rows)


# --- Inspection ---------------------------------------------------------------

async def stats() -> dict:
    rows = await db.fetch("""
        SELECT status, count(*) AS n FROM job_queue GROUP BY status
    """)
    counts = {r["status"]: r["n"] for r in rows}

    oldest = await db.fetchval("""
        SELECT extract(epoch FROM now() - min(created_at))::int
        FROM job_queue WHERE status = 'queued'
    """)

    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "complete": counts.get("complete", 0),
        "failed": counts.get("failed", 0),
        "cancelled": counts.get("cancelled", 0),
        "oldest_wait_seconds": oldest or 0,
    }


async def recent(school_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    rows = await db.fetch("""
        SELECT j.id, j.job_type, j.status, j.priority, j.attempts,
               j.max_attempts, j.last_error, j.claimed_by, j.created_at,
               j.completed_at, t.name AS timetable_name, s.name AS school_name
        FROM job_queue j
        LEFT JOIN timetables t ON t.id = j.timetable_id
        JOIN schools s ON s.id = j.school_id
        WHERE $1::uuid IS NULL OR j.school_id = $1
        ORDER BY j.created_at DESC
        LIMIT $2
    """, school_id, limit)
    return [dict(r) | {"id": str(r["id"])} for r in rows]


# --- Waiting for work ----------------------------------------------------------

async def wait_for_work(timeout: float) -> None:
    """
    Block until a job is queued or the timeout passes.

    LISTEN/NOTIFY means a job starts as soon as it is enqueued instead of
    waiting out a poll interval. The timeout is the fallback: if a notification
    is missed - a dropped connection, a NOTIFY that failed - the worker still
    wakes and checks.
    """
    pool = db.pool()
    if pool is None:
        await asyncio.sleep(timeout)
        return

    woken = asyncio.Event()

    async with pool.acquire() as conn:
        def on_notify(*_args: Any) -> None:
            woken.set()

        await conn.add_listener(CHANNEL, on_notify)
        try:
            await asyncio.wait_for(woken.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                await conn.remove_listener(CHANNEL, on_notify)
            except Exception:
                log.debug("Could not remove queue listener")

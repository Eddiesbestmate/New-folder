"""
Generation - start a run, watch it, read the result.

Progress is streamed with Server-Sent Events (ARCHITECTURE.md). The generation
itself is queued and run by `worker.py`, not in this process: a generation takes
minutes, and an API restart used to lose the work entirely, leaving the
timetable in 'processing' with nothing coming to finish it.
"""

import asyncio
import json
import logging
import time
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from models import database as db
from routers import intake
from routers.auth import CurrentUser, require_owner
from services import billing, deterministic, pipeline, queue, sandbox
from services import settings as settings_service

log = logging.getLogger("klasser.allocation")

router = APIRouter(tags=["allocation"])


async def readable_timetable(timetable_id: str, school_id: str) -> bool:
    """
    Whether this school may watch this generation.

    Their own, or the one sample run they were granted on the demonstration
    school. Read-only either way - nothing in this router changes a timetable.
    """
    owned = await db.fetchval(
        "SELECT 1 FROM timetables WHERE id = $1 AND school_id = $2",
        timetable_id, school_id)
    if owned:
        return True
    return await sandbox.may_read(str(timetable_id), school_id)


async def readable_attempt(attempt_id: str, school_id: str) -> bool:
    timetable_id = await db.fetchval(
        "SELECT timetable_id FROM generation_attempts WHERE id = $1", attempt_id)
    if timetable_id is None:
        return False
    return await readable_timetable(str(timetable_id), school_id)


class StartIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    notify_email: Optional[str] = None


@router.post("/start", status_code=status.HTTP_202_ACCEPTED)
async def start(payload: StartIn,
                user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Check the school's data, then queue a generation.

    The pre-validator runs first and synchronously: contradictions in the
    school's own data are caught before any AI runs and before any credits are
    committed (PIPELINE.md, "fail loudly, fail cheaply").
    """
    school_id = user["school_id"]

    layout_id = await db.fetchval("""
        SELECT id FROM timetable_layouts
        WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, school_id)
    if layout_id is None:
        raise HTTPException(
            422, "No timetable layout. Build one before generating.")

    check = await deterministic.pre_validate(school_id, str(layout_id))
    if not check.passed:
        raise HTTPException(422, {
            "message": "The school data cannot produce a timetable yet.",
            "problems": check.failures,
        })

    # Credits are checked and held here, on the server. The confirm screen shows
    # the same figure, but a frontend check is a courtesy - this is the one that
    # decides.
    estimate = await billing.estimate_cost(school_id)

    timetable_id = await db.fetchval("""
        INSERT INTO timetables (school_id, name, layout_id, status)
        VALUES ($1, $2, $3, 'processing') RETURNING id
    """, school_id, payload.name, layout_id)

    # The school's confirmed requirements become this run's class group
    # definitions. Copied per run rather than referenced, so the record of what
    # a generation was told survives the requirements being changed later.
    definitions = await intake.materialise(str(timetable_id), school_id)

    try:
        reservation = await billing.check_and_reserve(
            school_id, str(timetable_id), estimate)
    except billing.BillingError as exc:
        # The timetable row exists but nothing was spent; remove it so a
        # refused generation leaves no trace in the school's history.
        await db.execute("DELETE FROM timetables WHERE id = $1", timetable_id)
        raise HTTPException(402, {
            "message": exc.message,
            "code": exc.code,
            **exc.detail,
        }) from exc

    # Attempts are created by the retry loop, one per try, so the history of
    # what was attempted stays visible.
    # Queued rather than run in this process. A generation takes minutes; if the
    # API restarted mid-run the work simply vanished and the timetable sat in
    # 'processing' with nothing coming to finish it.
    job_id = await queue.enqueue(
        "generate", school_id,
        timetable_id=str(timetable_id),
        payload={"notify_email": payload.notify_email or user["email"]},
        priority=queue.PRIORITY_STANDARD)

    if job_id is None:
        # The unique index refused a second live job for this timetable.
        await billing.release(school_id, str(timetable_id), "duplicate request")
        await db.execute("DELETE FROM timetables WHERE id = $1", timetable_id)
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "A generation for this timetable is already queued.")

    waiting = await queue.stats()

    return {
        "timetable_id": str(timetable_id),
        "job_id": job_id,
        "estimated_credits": estimate.total,
        "reserved": reservation["reserved"],
        "billing_mode": reservation["billing_mode"],
        "queued_ahead": max(0, waiting["queued"] - 1),
        "warnings": check.warnings,
        "class_definitions": definitions,
    }


@router.get("/queue")
async def queue_status(user: CurrentUser) -> dict:
    """What this school has queued, and how busy the queue is overall."""
    return {
        "jobs": await queue.recent(user["school_id"], limit=20),
        "queue": await queue.stats(),
    }


@router.post("/stop")
async def stop(user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Stop every generation this school has queued or running.

    The two cases are cleaned up in different places, because only one of them
    has a worker to speak for it. A job that never started is finished here:
    its hold is released and its timetable row removed, leaving no trace of a
    run that produced nothing - the same treatment a refused generation gets.
    A job already running is only marked; its worker notices within seconds,
    abandons the work and releases its own hold, because until it actually
    stops we do not know how far it got.
    """
    school_id = user["school_id"]
    cancelled = await queue.cancel_school(school_id)

    never_started = [j for j in cancelled if j["was"] == "queued"]
    interrupted = [j for j in cancelled if j["was"] == "running"]

    for job in never_started:
        if not job["timetable_id"]:
            continue
        await billing.release(school_id, job["timetable_id"], "stopped before starting")
        await db.execute(
            "DELETE FROM timetables WHERE id = $1 AND school_id = $2",
            job["timetable_id"], school_id)

    log.info("School %s stopped %d queued and %d running job(s)",
             school_id, len(never_started), len(interrupted))

    return {
        "stopped": len(cancelled),
        "queued_cancelled": len(never_started),
        "running_stopped": len(interrupted),
        # The worker needs a moment to notice, so the caller should not expect
        # the run to read as stopped the instant this returns.
        "message": _stop_message(len(never_started), len(interrupted)),
    }


def _stop_message(queued: int, running: int) -> str:
    if not queued and not running:
        return "Nothing was queued or running."
    parts = []
    if running:
        parts.append(f"{running} running generation"
                     f"{'s' if running != 1 else ''} stopping now")
    if queued:
        parts.append(f"{queued} queued generation"
                     f"{'s' if queued != 1 else ''} cancelled")
    return " and ".join(parts) + ". Any credits held have been released."


@router.get("/timetable/{timetable_id}/attempts")
async def attempts(timetable_id: str, user: CurrentUser) -> list[dict]:
    """Every attempt made for a timetable, so retries are visible."""
    if not await readable_timetable(timetable_id, user["school_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Timetable not found")

    rows = await db.fetch("""
        SELECT ga.id, ga.attempt_number, ga.status, ga.failure_reason,
               ga.locked_model, ga.started_at, ga.completed_at,
               j.progress_pct
        FROM generation_attempts ga
        LEFT JOIN generation_jobs j ON j.attempt_id = ga.id
        WHERE ga.timetable_id = $1
        ORDER BY ga.attempt_number
    """, timetable_id)
    return [dict(r) | {"id": str(r["id"])} for r in rows]


@router.get("/{attempt_id}/status")
async def attempt_status(attempt_id: str, user: CurrentUser) -> dict:
    row = await db.fetchrow("""
        SELECT ga.id, ga.status, ga.failure_reason, ga.locked_model,
               ga.started_at, ga.completed_at,
               t.name, t.school_id,
               j.current_stage, j.current_chunk, j.progress_pct, j.status AS job_status
        FROM generation_attempts ga
        JOIN timetables t ON t.id = ga.timetable_id
        LEFT JOIN generation_jobs j ON j.attempt_id = ga.id
        WHERE ga.id = $1
    """, attempt_id)
    if row is None or not await readable_attempt(attempt_id, user["school_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Generation not found")

    stages = await db.fetch("""
        SELECT stage, status, failure_reason, started_at, completed_at
        FROM pipeline_state WHERE attempt_id = $1 ORDER BY started_at
    """, attempt_id)

    version_id = await db.fetchval("""
        SELECT id FROM timetable_versions WHERE generation_attempt_id = $1
    """, attempt_id)

    return {
        "attempt_id": str(row["id"]),
        "name": row["name"],
        "status": row["status"],
        "job_status": row["job_status"],
        "failure_reason": row["failure_reason"],
        "locked_model": row["locked_model"],
        "current_stage": row["current_stage"],
        "current_chunk": row["current_chunk"],
        "progress_pct": row["progress_pct"] or 0,
        "version_id": str(version_id) if version_id else None,
        "stages": [dict(s) for s in stages],
        "all_stages": [{"key": k, "label": l} for k, l, _ in pipeline.STAGES],
    }


@router.get("/{attempt_id}/events")
async def events(attempt_id: str, user: CurrentUser, after: int = 0) -> list[dict]:
    """Events since a point, for clients that prefer polling to SSE."""
    if not await readable_attempt(attempt_id, user["school_id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Generation not found")

    rows = await db.fetch("""
        SELECT event_type, message, detail, created_at
        FROM generation_events
        WHERE attempt_id = $1
        ORDER BY created_at
        OFFSET $2
    """, attempt_id, after)
    return [dict(r) | {"detail": json.loads(r["detail"]) if r["detail"] else None}
            for r in rows]


@router.get("/{attempt_id}/progress")
async def progress_stream(attempt_id: str, token: str = "") -> StreamingResponse:
    """
    Server-Sent Events for the progress screen.

    EventSource cannot set an Authorization header, so the access token comes
    as a query parameter and is verified here before anything is streamed.
    """
    from services import supabase_client as sb

    auth_user = await sb.get_auth_user(token) if token else None
    if auth_user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")

    school_id = await db.fetchval(
        "SELECT school_id FROM users WHERE id = $1", auth_user["id"])
    if not await readable_attempt(attempt_id, str(school_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Generation not found")

    # A stream ends when its job reaches a terminal state. If that never happens
    # - the worker was killed mid-run, or the job row was removed - the loop
    # would poll the database once a second for as long as the tab stays open.
    # Closing the tab cancels the generator, so this is not a leak in normal
    # use, but "not a leak in normal use" is not a reason to run forever.
    max_seconds = await settings_service.get_int("sse_max_seconds", 1800)
    poll_seconds = await settings_service.get_float("sse_poll_seconds", 1.0)

    async def stream():
        sent = 0
        started = time.monotonic()

        while True:
            rows = await db.fetch("""
                SELECT event_type, message, detail, created_at
                FROM generation_events WHERE attempt_id = $1
                ORDER BY created_at OFFSET $2
            """, attempt_id, sent)

            for row in rows:
                sent += 1
                payload = {
                    "type": row["event_type"],
                    "message": row["message"],
                    "detail": json.loads(row["detail"]) if row["detail"] else None,
                }
                yield f"data: {json.dumps(payload)}\n\n"

            job = await db.fetchrow("""
                SELECT status, current_stage, current_chunk, progress_pct
                FROM generation_jobs WHERE attempt_id = $1
            """, attempt_id)
            if job:
                yield f"data: {json.dumps({'type': 'progress', **dict(job)})}\n\n"
                if job["status"] in ("complete", "failed"):
                    break

            if time.monotonic() - started > max_seconds:
                # Say so rather than just going quiet: the page can decide to
                # reconnect, and a silent stop looks identical to a hung run.
                log.warning("SSE stream for attempt %s hit the %ss cap",
                            attempt_id, max_seconds)
                yield ("data: " + json.dumps({
                    "type": "stream_timeout",
                    "message": "This live view timed out. Refresh to reconnect.",
                }) + "\n\n")
                break

            await asyncio.sleep(poll_seconds)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

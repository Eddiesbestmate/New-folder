"""
Klasser job worker.

Runs generations off the queue instead of inside the web process. Start at
least one alongside the API:

    python worker.py                 # run until interrupted
    python worker.py --once          # take one job and exit, for testing
    python worker.py --concurrency 1 # override queue_max_concurrent

Several workers can run at once; FOR UPDATE SKIP LOCKED means they take
different jobs. Killing one mid-generation is safe - its job is requeued when
the heartbeat goes stale.
"""

import argparse
import asyncio
import logging
import signal
import sys
from typing import Optional

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import config
from models import database as db
from services import ai_cluster, billing, email, pipeline, queue, scheduler
from services import settings as settings_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("klasser.worker")

stopping = asyncio.Event()


async def run_generation(job: queue.Job) -> None:
    """
    One generation, with billing settled either way.

    This mirrors what the API used to do inline. The difference is that a crash
    here no longer loses the work: the job returns to the queue and another
    worker picks it up.
    """
    timetable_id = job.timetable_id
    if not timetable_id:
        raise ValueError("generate job has no timetable_id")

    notify_email = job.payload.get("notify_email") or ""

    try:
        summary = await pipeline.run_with_retries(timetable_id, notify_email)
    except Exception as exc:
        await db.execute(
            "UPDATE timetables SET status = 'failed' WHERE id = $1", timetable_id)
        # The school gets no timetable, so they are not charged.
        try:
            await billing.release(job.school_id, timetable_id,
                                  reason="generation failed")
        except Exception:
            log.exception("Could not release the credit hold for %s", timetable_id)

        try:
            name = await db.fetchval(
                "SELECT name FROM timetables WHERE id = $1", timetable_id)
            school = await db.fetchval(
                "SELECT name FROM schools WHERE id = $1", job.school_id)
            await email.notify_school(
                job.school_id, "notify_generation_failed", "generation_failed",
                {
                    "school_name": school, "timetable_name": name,
                    "failure_reason": str(exc)[:300],
                    "is_school_fault": isinstance(exc, pipeline.PipelineError)
                                       and getattr(exc, "school_fault", False),
                    "credits_charged": 0, "credits_refunded": 0,
                    "retry_url": f"{config.APP_URL}/generate.html",
                    "support_url": f"{config.APP_URL}/support.html",
                })
        except Exception:  # noqa: BLE001
            log.exception("Could not send the failure email for %s", timetable_id)
        raise

    await db.execute("""
        UPDATE timetables SET status = 'complete', completed_at = now()
        WHERE id = $1
    """, timetable_id)

    try:
        estimate = await billing.estimate_cost(job.school_id)
        outcome = await billing.settle(
            job.school_id, timetable_id, estimate.total,
            school_retries=summary.get("school_retries", 0),
            dev_retries=summary.get("dev_retries", 0))

        # PAYG schools pay after the run rather than up front, so the card is
        # charged here. A decline blocks the account but never touches the
        # timetable - the school keeps the work and owes for it.
        if not outcome.get("already_settled"):
            await billing.charge_payg(
                job.school_id, timetable_id,
                outcome.get("charged") or estimate.total)
    except Exception:
        # Never lose a finished timetable over a billing error.
        log.exception("Could not settle billing for %s", timetable_id)

    await announce(job, summary)

    log.info("Generated %s: %s entries in %s attempt(s)",
             timetable_id, summary.get("entries"), summary.get("attempts"))


async def announce(job: queue.Job, summary: dict) -> None:
    """
    Tell the school their timetable is ready.

    Outside the billing block and wrapped again here: a generation that
    succeeded and was charged correctly must not be reported as failed because
    an email provider was down.
    """
    try:
        row = await db.fetchrow("""
            SELECT t.name, s.name AS school,
                   (SELECT v.id FROM timetable_versions v
                     WHERE v.timetable_id = t.id
                     ORDER BY v.version_number DESC LIMIT 1) AS version_id
            FROM timetables t JOIN schools s ON s.id = t.school_id
            WHERE t.id = $1
        """, job.timetable_id)
        if row is None:
            return

        await email.notify_school(
            job.school_id, "notify_generation_complete", "generation_complete",
            {
                "school_name": row["school"],
                "timetable_name": row["name"],
                "classes_formed": summary.get("classes", 0),
                "teachers_assigned": summary.get("teachers", 0),
                "time_taken": summary.get("duration", "a few minutes"),
                "attempt_count": summary.get("attempts", 1),
                "credits_estimated": summary.get("estimated", 0),
                "credits_actual": summary.get("charged", 0),
                "credits_refunded": 0,
                "view_url": f"{config.APP_URL}/output.html"
                            f"?version={row['version_id']}",
            })
    except Exception:  # noqa: BLE001
        log.exception("Could not send the completion email for %s",
                      job.timetable_id)


HANDLERS = {
    "generate": run_generation,
}


async def with_heartbeat(job: queue.Job) -> None:
    """Run a job while keeping its heartbeat fresh, so it is not reaped."""
    interval = await settings_service.get_int("queue_heartbeat_seconds", 30)

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            await queue.heartbeat(job.id)

    beating = asyncio.create_task(beat())
    try:
        handler = HANDLERS.get(job.job_type)
        if handler is None:
            raise ValueError(f"No handler for job type {job.job_type!r}")
        await handler(job)
    finally:
        beating.cancel()


async def process(job: queue.Job) -> None:
    log.info("Picked up %s job %s (attempt %s of %s)",
             job.job_type, job.id, job.attempts, job.max_attempts)
    try:
        await with_heartbeat(job)
        await queue.complete(job.id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Job %s failed", job.id)
        # A generation that failed on its own terms has already exhausted the
        # pipeline's own retries, so requeueing would repeat all of it. Only
        # infrastructure failures are worth another go.
        retry = not isinstance(exc, pipeline.PipelineError)
        await queue.fail(job.id, str(exc), retry=retry)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Klasser job worker")
    parser.add_argument("--once", action="store_true",
                        help="Process one job and exit")
    parser.add_argument("--concurrency", type=int,
                        help="Override queue_max_concurrent")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="Do not run the billing jobs from this worker")
    args = parser.parse_args()

    await db.connect()
    if db.pool() is None:
        log.error("No database connection - set DATABASE_URL")
        return 1

    await ai_cluster.load_key_pools()

    concurrency = args.concurrency or await settings_service.get_int(
        "queue_max_concurrent", 2)
    poll = await settings_service.get_int("queue_poll_seconds", 5)
    worker = queue.worker_name()

    log.info("Worker %s started (concurrency %s)", worker, concurrency)

    # The billing jobs live here rather than in the API because this is the one
    # long-lived process. Running several workers is safe: each job claims a
    # row per day, so only one of them does the work.
    if not args.once and not args.no_scheduler:
        await scheduler.start()

    running: set[asyncio.Task] = set()
    reaped = 0

    try:
        while not stopping.is_set():
            # Recover anything a dead worker left behind.
            reclaimed = await queue.reap_stale()
            reaped += reclaimed

            while len(running) < concurrency and not stopping.is_set():
                job = await queue.claim(worker)
                if job is None:
                    break
                task = asyncio.create_task(process(job))
                running.add(task)
                task.add_done_callback(running.discard)

            if args.once:
                if running:
                    await asyncio.gather(*running, return_exceptions=True)
                    return 0
                log.info("No jobs waiting")
                return 0

            if not running:
                await queue.wait_for_work(timeout=poll)
            else:
                # Work in flight: check back sooner to top up the slots.
                done, _ = await asyncio.wait(running, timeout=poll,
                                             return_when=asyncio.FIRST_COMPLETED)

    finally:
        scheduler.stop()
        if running:
            log.info("Finishing %s job(s) before shutdown", len(running))
            await asyncio.gather(*running, return_exceptions=True)
        await db.disconnect()
        log.info("Worker stopped (reclaimed %s stale job(s) while running)", reaped)

    return 0


def request_stop(*_args) -> None:
    log.info("Shutdown requested - finishing current work")
    stopping.set()


if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, AttributeError):
            pass
    sys.exit(asyncio.run(main()))

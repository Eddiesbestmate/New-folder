"""
The clock that fires the billing jobs (BILLING.md).

APScheduler only decides *when* to call something. Whether the work actually
happens is settled by `services/jobs.py`, which claims a row per job per day -
so running several workers, or restarting one just after midnight, cannot
charge a school twice. That separation is deliberate: a scheduler is not a
safe place to put correctness.

Started by the worker, not the API. Web processes are scaled horizontally and
restarted on every deploy; the worker is the one long-lived process, and the
day-claim makes extra workers harmless.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from services import jobs
from services import settings as settings_service

log = logging.getLogger("klasser.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _run(name: str) -> None:
    try:
        await jobs.JOBS[name]()
    except Exception:  # noqa: BLE001
        # A failing job must not kill the scheduler - tomorrow's run is the
        # retry, and the failure is already recorded against today's row.
        log.exception("Scheduled job %s failed", name)


async def start() -> AsyncIOScheduler:
    """Start the daily and monthly billing jobs. Idempotent."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    invoice_day = await settings_service.get_int("invoice_sent_day", 1)

    # The same timezone the jobs reckon a day in, so "just after midnight"
    # means the same thing to the clock and to the work.
    scheduler = AsyncIOScheduler(timezone=config.BILLING_TIMEZONE)

    # Just after midnight, so a day's interest lands on the day it belongs to.
    scheduler.add_job(_run, CronTrigger(hour=0, minute=10),
                      args=["accrue_interest"], id="accrue_interest")

    # Invoices for last month, on the configured day of this one.
    scheduler.add_job(_run, CronTrigger(day=min(max(1, invoice_day), 28),
                                        hour=1, minute=0),
                      args=["generate_invoices"], id="generate_invoices")

    # Enforcement after invoices exist, so a same-day invoice is never
    # immediately judged overdue.
    scheduler.add_job(_run, CronTrigger(hour=2, minute=0),
                      args=["enforce_invoices"], id="enforce_invoices")

    scheduler.start()
    _scheduler = scheduler
    log.info("Scheduler started: %s",
             ", ".join(j.id for j in scheduler.get_jobs()))
    return scheduler


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("Scheduler stopped")


def scheduled() -> list[dict]:
    """What is scheduled and when it next fires - for the dev portal."""
    if _scheduler is None:
        return []
    return [
        {"id": job.id,
         "next_run": job.next_run_time.isoformat() if job.next_run_time else None}
        for job in _scheduler.get_jobs()
    ]

"""
Scheduled billing jobs (BILLING.md).

Three things happen on a clock rather than on a request:

    accrue_interest      daily   - debts grow while they go unpaid
    generate_invoices    monthly - invoiced schools are billed for last month
    enforce_invoices     daily   - overdue invoices warn, then block, then revoke

Each one is destructive if it runs twice: interest applied twice in a day
overcharges, a second invoice for the same month double-bills. They are
therefore claimed through `scheduled_job_runs` - one caller inserts the row for
today and does the work, every other caller stops. That makes them safe to run
from several workers, to trigger by hand from the dev portal, and to call again
after a crash.

Emails are Phase 12. Where BILLING.md says "send email", these jobs record what
would be sent in the returned summary and in the log, so wiring up Resend later
is a matter of filling in `_notify` rather than finding the places.
"""

import json
import logging
from datetime import date, timedelta
from typing import Any, Callable, Optional

import config
from models import database as db
from services import email
from services import settings as settings_service

log = logging.getLogger("klasser.jobs")


# --- Running once ------------------------------------------------------------

async def claim(job_name: str, run_date: date, triggered_by: str) -> bool:
    """
    Take today's slot for a job. True if this caller should do the work.

    The unique primary key does the arbitrating, so two workers racing at
    midnight cannot both proceed.
    """
    claimed = await db.fetchval("""
        INSERT INTO scheduled_job_runs (job_name, run_date, triggered_by)
        VALUES ($1, $2, $3)
        ON CONFLICT (job_name, run_date) DO NOTHING
        RETURNING 1
    """, job_name, run_date, triggered_by)
    return bool(claimed)


async def finish(job_name: str, run_date: date, summary: dict,
                 error: Optional[str] = None) -> None:
    await db.execute("""
        UPDATE scheduled_job_runs
        SET finished_at = now(), status = $3, summary = $4, error = $5
        WHERE job_name = $1 AND run_date = $2
    """, job_name, run_date, "failed" if error else "complete",
        json.dumps(summary), error)


async def run_once(job_name: str, work: Callable, *, on_date: Optional[date] = None,
                   triggered_by: str = "scheduler", force: bool = False) -> dict:
    """
    Run `work` at most once for `on_date`, recording what it did.

    `force` re-runs a job that already ran today. It exists for the dev portal
    and for tests; it is not what the scheduler uses, because forcing an
    interest accrual charges a school twice for the same day.
    """
    on_date = on_date or config.billing_date()

    if force:
        await db.execute("""
            DELETE FROM scheduled_job_runs WHERE job_name = $1 AND run_date = $2
        """, job_name, on_date)

    if not await claim(job_name, on_date, triggered_by):
        previous = await db.fetchrow("""
            SELECT status, summary FROM scheduled_job_runs
            WHERE job_name = $1 AND run_date = $2
        """, job_name, on_date)
        log.info("%s already ran on %s - skipping", job_name, on_date)
        return {"skipped": True, "reason": "already ran today",
                "previous_status": previous["status"] if previous else None}

    try:
        summary = await work()
    except Exception as exc:  # noqa: BLE001
        await finish(job_name, on_date, {}, error=f"{type(exc).__name__}: {exc}")
        log.exception("%s failed", job_name)
        raise

    await finish(job_name, on_date, summary)
    log.info("%s complete: %s", job_name, summary)
    return summary | {"skipped": False}


async def _notify(school_id: str, template: str, detail: dict) -> dict:
    """
    Tell a school something a scheduled job decided.

    Still returns the record as well as sending it, so a job summary says who
    was told what - which is how the tests assert it, and how a dev answers
    "did they know?" without reading a mailbox.
    """
    preference = NOTIFY_PREFERENCE.get(template, "notify_invoice_overdue")
    outcome = await email.notify_school(school_id, preference, template, detail)
    log.info("Emailed %s about %s: %s", school_id, template, outcome)
    return {"school_id": school_id, "template": template,
            "recipients": outcome["recipients"], "sent": outcome["sent"],
            **detail}


# Which preference governs each of these. A school that has switched off invoice
# mail still gets `account_blocked` - that one is in email.ALWAYS_SEND.
NOTIFY_PREFERENCE = {
    "debt_reminder": "notify_invoice_overdue",
    "invoice_sent": "notify_invoice_sent",
    "invoice_reminder": "notify_invoice_overdue",
    "account_blocked": "notify_account_blocked",
    "invoiced_billing_revoked": "notify_invoice_overdue",
}


# --- Daily interest ----------------------------------------------------------

async def _accrue_interest(on_date: Optional[date] = None) -> dict:
    charges = await db.fetch("""
        SELECT id, school_id, total_owed_cents, interest_rate_daily,
               days_outstanding, last_interest_date, charge_type
        FROM outstanding_charges
        WHERE status = 'outstanding'
        ORDER BY created_at
    """)

    today = on_date or config.billing_date()
    touched = 0
    interest_total = 0
    emails: list[dict] = []
    schools: set[str] = set()

    for charge in charges:
        # Charge for the days that actually passed. If the job did not run for
        # three days, three days of interest are due - not one.
        last = charge["last_interest_date"] or today
        days = (today - last).days
        if days <= 0:
            continue

        owed = charge["total_owed_cents"]
        rate = float(charge["interest_rate_daily"])
        interest = 0
        for _ in range(days):
            # Compounded daily, the way the debt is described to the school.
            step = int(round(owed * rate))
            interest += step
            owed += step

        await db.execute("""
            UPDATE outstanding_charges
            SET interest_accrued_cents = interest_accrued_cents + $2,
                total_owed_cents = $3,
                days_outstanding = days_outstanding + $4,
                last_interest_date = $5
            WHERE id = $1
        """, charge["id"], interest, owed, days, today)

        touched += 1
        interest_total += interest
        schools.add(str(charge["school_id"]))
        emails.append(await _notify(str(charge["school_id"]), "debt_reminder", {
            "school_name": await _school_name(str(charge["school_id"])),
            "what": ("a declined card payment"
                     if charge["charge_type"] == "payg_failed"
                     else "an unpaid invoice"),
            "days_outstanding": charge["days_outstanding"] + days,
            "interest_rate_daily_pct": f"{rate * 100:g}",
            "interest_today_dollars": f"{interest / 100:,.2f}",
            "total_owed_dollars": f"{owed / 100:,.2f}",
            "resolve_url": f"{config.APP_URL}/billing.html",
        }))

    for school_id in schools:
        await _sync_outstanding(school_id)

    return {"charges": touched, "interest_cents": interest_total,
            "schools": len(schools), "emails": emails}


async def _school_name(school_id: str) -> str:
    return await db.fetchval(
        "SELECT name FROM schools WHERE id = $1", school_id) or "your school"


async def _owed(school_id: str) -> int:
    """Everything this school currently owes, in cents."""
    return await db.fetchval("""
        SELECT coalesce(sum(total_owed_cents), 0) FROM outstanding_charges
        WHERE school_id = $1 AND status = 'outstanding'
    """, school_id) or 0


async def _sync_outstanding(school_id: str) -> int:
    """Keep the school's headline debt equal to the sum of its open charges."""
    total = await db.fetchval("""
        SELECT coalesce(sum(total_owed_cents), 0) FROM outstanding_charges
        WHERE school_id = $1 AND status = 'outstanding'
    """, school_id)
    await db.execute("""
        UPDATE school_billing
        SET outstanding_balance_cents = $2, updated_at = now()
        WHERE school_id = $1
    """, school_id, total)
    return total


async def accrue_interest(**kwargs) -> dict:
    on_date = kwargs.get("on_date")
    return await run_once("accrue_interest",
                          lambda: _accrue_interest(on_date), **kwargs)


# --- Monthly invoices --------------------------------------------------------

def _last_month(today: date) -> tuple[date, date]:
    """The first and last day of the month before `today`."""
    end = today.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


async def _invoice_number(school_id: str, period_start: date) -> str:
    """
    A readable, unique invoice number: KL-YYYYMM-####.

    The sequence is per period rather than global, so an invoice number says
    when it was raised without a lookup.
    """
    n = await db.fetchval("""
        SELECT count(*) + 1 FROM invoices WHERE period_start = $1
    """, period_start)
    return f"KL-{period_start:%Y%m}-{n:04d}"


async def _generate_invoices(on_date: Optional[date] = None) -> dict:
    today = on_date or config.billing_date()
    period_start, period_end = _last_month(today)

    due_day = await settings_service.get_int("invoice_due_day", 14)
    rate_cents = await settings_service.get_int("credit_invoiced_rate_cents", 230)

    schools = await db.fetch("""
        SELECT school_id, invoiced_payment_days FROM school_billing
        WHERE billing_mode = 'invoiced' AND invoiced_approved = true
          AND coalesce(invoiced_revoked, false) = false
    """)

    created: list[dict] = []
    emails: list[dict] = []
    skipped = 0

    for school in schools:
        school_id = str(school["school_id"])

        lines = await db.fetch("""
            SELECT gcb.timetable_id, gcb.actual_credits, gcb.settled_at,
                   t.name AS timetable_name
            FROM generation_cost_breakdown gcb
            JOIN timetables t ON t.id = gcb.timetable_id
            WHERE gcb.school_id = $1 AND gcb.status = 'charged'
              AND gcb.settled_at >= $2 AND gcb.settled_at < $3
            ORDER BY gcb.settled_at
        """, school_id, period_start, period_start + timedelta(days=32))

        # Only generations settled inside the period, which the date window
        # above over-reaches by design - filter precisely here so a 31-day
        # month is not cut short.
        lines = [row for row in lines
                 if period_start <= row["settled_at"].date() <= period_end]

        if not lines:
            skipped += 1
            continue

        total_credits = sum(row["actual_credits"] or 0 for row in lines)
        total_cents = total_credits * rate_cents
        due = _due_date(period_end, due_day, school["invoiced_payment_days"])

        async with db.transaction() as conn:
            number = await _invoice_number(school_id, period_start)
            invoice_id = await conn.fetchval("""
                INSERT INTO invoices
                    (school_id, invoice_number, period_start, period_end,
                     subtotal_credits, subtotal_cents, total_cents, status,
                     due_date, sent_at)
                VALUES ($1,$2,$3,$4,$5,$6,$6,'sent',$7, now())
                ON CONFLICT (school_id, period_start) DO NOTHING
                RETURNING id
            """, school_id, number,
                period_start, period_end, total_credits, total_cents, due)

            if invoice_id is None:
                # Already invoiced for this period by a concurrent caller.
                skipped += 1
                continue

            for row in lines:
                await conn.execute("""
                    INSERT INTO invoice_line_items
                        (invoice_id, timetable_id, description, credits_used,
                         amount_cents, generated_at)
                    VALUES ($1,$2,$3,$4,$5,$6)
                """, invoice_id, row["timetable_id"],
                    f"Timetable generation - {row['timetable_name']}",
                    row["actual_credits"] or 0,
                    (row["actual_credits"] or 0) * rate_cents,
                    row["settled_at"])

        created.append({"school_id": school_id, "invoice_id": str(invoice_id),
                        "credits": total_credits, "total_cents": total_cents,
                        "due_date": due.isoformat(), "lines": len(lines)})
        emails.append(await _notify(school_id, "invoice_sent", {
            "school_name": await _school_name(school_id),
            "invoice_number": number,
            "period": f"{period_start:%B %Y}",
            "subtotal_credits": total_credits,
            "total_dollars": f"{total_cents / 100:,.2f}",
            "due_date": config.long_date(due) if hasattr(due, "strftime")
                        else str(due),
            "line_items": [
                {"description": f"Timetable generation - {row['timetable_name']}",
                 "credits_used": row["actual_credits"] or 0,
                 "amount_dollars":
                     f"{(row['actual_credits'] or 0) * rate_cents / 100:,.2f}"}
                for row in lines
            ],
            "pay_url": f"{config.APP_URL}/billing.html",
            "invoice_pdf_url": f"{config.APP_URL}/billing.html",
        }))

    return {"invoices": len(created), "schools_skipped": skipped,
            "period": [period_start.isoformat(), period_end.isoformat()],
            "created": created, "emails": emails}


def _due_date(period_end: date, due_day: int, payment_days: Optional[int]) -> date:
    """
    When an invoice must be paid.

    BILLING.md bills on the 1st and takes payment by the 14th. A school with
    negotiated terms (`invoiced_payment_days`) gets those instead, counted from
    the end of the billed period.
    """
    if payment_days:
        return period_end + timedelta(days=payment_days)
    issue = period_end + timedelta(days=1)
    day = min(max(1, due_day), 28)
    return issue.replace(day=day)


async def generate_invoices(**kwargs) -> dict:
    on_date = kwargs.get("on_date")
    return await run_once("generate_invoices",
                          lambda: _generate_invoices(on_date), **kwargs)


# --- Invoice enforcement -----------------------------------------------------

async def _enforce_invoices(on_date: Optional[date] = None) -> dict:
    today = on_date or config.billing_date()

    block_days = await settings_service.get_int("invoice_block_days", 1)
    revoke_after = await settings_service.get_int("invoice_revoke_days", 10)
    rate = await settings_service.get_float("invoice_interest_rate", 0.10)

    overdue = await db.fetch("""
        SELECT id, school_id, invoice_number, total_cents, due_date, status,
               blocked_at, invoiced_revoked_at
        FROM invoices
        WHERE status IN ('sent', 'overdue') AND due_date <= $1
        ORDER BY due_date
    """, today)

    warned = blocked = revoked = 0
    emails: list[dict] = []

    for invoice in overdue:
        school_id = str(invoice["school_id"])
        days_overdue = (today - invoice["due_date"]).days

        await db.execute("""
            UPDATE invoices
            SET status = 'overdue', days_overdue = $2,
                overdue_at = coalesce(overdue_at, now())
            WHERE id = $1
        """, invoice["id"], days_overdue)

        # Day 0 is the due date itself: a warning, not yet a penalty.
        if days_overdue == 0:
            warned += 1
            emails.append(await _notify(school_id, "invoice_reminder", {
                "school_name": await _school_name(school_id),
                "invoice_number": invoice["invoice_number"],
                "total_dollars": f"{invoice['total_cents'] / 100:,.2f}",
                "days_overdue": days_overdue,
                "interest_accrued_dollars": "0.00",
                "total_owed_dollars": f"{invoice['total_cents'] / 100:,.2f}",
                "block_date": config.long_date(
                    today + timedelta(days=block_days)),
                "pay_url": f"{config.APP_URL}/billing.html",
            }))
            continue

        # Past due: the debt is real, so it starts accruing. One outstanding
        # charge per invoice, created the first day it is late; the interest
        # job grows it from there.
        charge_id = await db.fetchval("""
            SELECT id FROM outstanding_charges
            WHERE invoice_id = $1 AND status = 'outstanding'
        """, invoice["id"])
        if charge_id is None:
            charge_id = await db.fetchval("""
                INSERT INTO outstanding_charges
                    (school_id, charge_type, original_amount_cents,
                     interest_rate_daily, total_owed_cents, invoice_id,
                     last_interest_date)
                VALUES ($1,'invoice_overdue',$2,$3,$2,$4,$5)
                RETURNING id
            """, school_id, invoice["total_cents"], rate, invoice["id"], today)
            await _sync_outstanding(school_id)

        if days_overdue >= block_days and invoice["blocked_at"] is None:
            await db.execute("""
                UPDATE school_billing
                SET account_blocked = true, blocked_at = now(),
                    blocked_reason = $2,
                    interest_started_at = coalesce(interest_started_at, now()),
                    updated_at = now()
                WHERE school_id = $1
            """, school_id, f"Invoice {invoice['invoice_number']} overdue")
            await db.execute(
                "UPDATE invoices SET blocked_at = now() WHERE id = $1",
                invoice["id"])
            blocked += 1
            emails.append(await _notify(school_id, "account_blocked", {
                "school_name": await _school_name(school_id),
                "blocked_reason":
                    f"Invoice {invoice['invoice_number']} is {days_overdue} "
                    "days overdue",
                "outstanding_dollars":
                    f"{await _owed(school_id) / 100:,.2f}",
                "resolve_url": f"{config.APP_URL}/billing.html",
                "support_url": f"{config.APP_URL}/support.html",
            }))

        # Ten days after the block, invoiced billing is withdrawn and the school
        # goes back to prepaid credits. The debt survives the change.
        if (days_overdue >= block_days + revoke_after
                and invoice["invoiced_revoked_at"] is None):
            await db.execute("""
                UPDATE school_billing
                SET billing_mode = 'credits', invoiced_approved = false,
                    invoiced_revoked = true, invoiced_revoked_at = now(),
                    invoiced_revoked_reason = $2, updated_at = now()
                WHERE school_id = $1
            """, school_id,
                f"Invoice {invoice['invoice_number']} unpaid for "
                f"{days_overdue} days")
            await db.execute(
                "UPDATE invoices SET invoiced_revoked_at = now() WHERE id = $1",
                invoice["id"])
            revoked += 1
            emails.append(await _notify(school_id, "invoiced_billing_revoked", {
                "school_name": await _school_name(school_id),
                "days_overdue": days_overdue,
                "outstanding_dollars": f"{await _owed(school_id) / 100:,.2f}",
                "new_billing_mode": "credits",
                "top_up_url": f"{config.APP_URL}/billing.html",
                "support_url": f"{config.APP_URL}/support.html",
            }))

    return {"overdue": len(overdue), "warned": warned, "blocked": blocked,
            "revoked": revoked, "emails": emails}


async def enforce_invoices(**kwargs) -> dict:
    on_date = kwargs.get("on_date")
    return await run_once("enforce_invoices",
                          lambda: _enforce_invoices(on_date), **kwargs)


JOBS: dict[str, Any] = {
    "accrue_interest": accrue_interest,
    "generate_invoices": generate_invoices,
    "enforce_invoices": enforce_invoices,
}


async def history(limit: int = 30) -> list[dict]:
    rows = await db.fetch("""
        SELECT job_name, run_date, started_at, finished_at, status,
               triggered_by, summary, error
        FROM scheduled_job_runs
        ORDER BY run_date DESC, started_at DESC
        LIMIT $1
    """, limit)
    return [
        dict(r) | {"run_date": r["run_date"].isoformat(),
                   "summary": json.loads(r["summary"]) if r["summary"] else None}
        for r in rows
    ]

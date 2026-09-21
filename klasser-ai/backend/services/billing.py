"""
Credits - estimating, reserving, settling and refunding.

The flow for one generation (BILLING.md):

    estimate  -> what it will cost, from the school's current data
    reserve   -> soft hold, so two generations cannot spend the same credits
    settle    -> deduct what it actually cost, release the hold
    refund    -> give back credits for a dev-caused retry

Every balance change goes through here and writes a `credit_transactions` row.
Nothing else in the codebase updates `school_credits` directly, so the
transaction log is always a complete account of how the balance got where it is.

Concurrency matters: the balance row is locked for update while reserving, so
two generations started at the same moment cannot both pass a check that only
one of them can afford.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

import config
from models import database as db
from services import fake_terminal
from services import settings as settings_service

log = logging.getLogger("klasser.billing")


class BillingError(Exception):
    """A billing rule stopped the operation. The message is shown to the user."""

    def __init__(self, message: str, *, code: str = "billing_error",
                 detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}


class InsufficientCredits(BillingError):
    def __init__(self, needed: int, available: int) -> None:
        super().__init__(
            f"This generation needs {needed} credits but only {available} are "
            "available. Top up before generating.",
            code="insufficient_credits",
            detail={"needed": needed, "available": available,
                    "shortfall": needed - available})


class AccountBlocked(BillingError):
    def __init__(self, reason: Optional[str]) -> None:
        super().__init__(
            f"This account is blocked{f': {reason}' if reason else ''}. "
            "Resolve the outstanding balance to generate again.",
            code="account_blocked")


class NoPaymentMethod(BillingError):
    def __init__(self) -> None:
        super().__init__(
            "Pay as you go is selected but no card is saved. Add one in Billing.",
            code="no_payment_method")


@dataclass
class CostBreakdown:
    lines: list[dict]
    total: int

    def as_dict(self) -> dict:
        return {"lines": self.lines, "total_credits": self.total}


# --- Estimating --------------------------------------------------------------

async def estimate_cost(school_id: str) -> CostBreakdown:
    """
    What a generation would cost, itemised.

    Computed from live counts rather than the cached school_complexity row, so
    the number a school sees on the confirm screen matches their data as it
    stands at that moment.
    """
    counts = await db.fetchrow("""
        SELECT
            (SELECT count(*) FROM students WHERE school_id = $1) AS students,
            (SELECT count(*) FROM teachers WHERE school_id = $1) AS teachers,
            (SELECT count(*) FROM campuses WHERE school_id = $1) AS campuses,
            (SELECT count(*) FROM routes   WHERE school_id = $1) AS routes,
            (SELECT count(*) FROM buses    WHERE school_id = $1) AS buses,
            (SELECT count(*) FROM duty_types WHERE school_id = $1) AS duty_types,
            (SELECT count(*) FROM subject_settings
              WHERE school_id = $1 AND is_double_period) AS doubles,
            (SELECT count(*) FROM class_group_definitions
              WHERE school_id = $1 AND is_accelerated) AS accelerated
    """, school_id)

    cycle_days = await db.fetchval("""
        SELECT days_in_cycle FROM timetable_layouts
        WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, school_id) or 5

    async def value(key: str, default: float) -> float:
        return await settings_service.get_float(key, default)

    base = await value("credit_base_fee", 60)
    per_student = await value("credit_per_student", 0.20)
    teacher_free = int(await value("credit_teacher_threshold", 20))
    per_teacher = await value("credit_per_teacher_over", 0.40)
    per_campus = await value("credit_per_extra_campus", 30)
    transport_flat = await value("credit_transport_flat", 40)
    per_route = await value("credit_per_bus_route", 10)
    duties_flat = await value("credit_duties_flat", 20)
    day_free = int(await value("credit_day_threshold", 5))
    per_day = await value("credit_per_extra_day", 4)
    accel_flat = await value("credit_accelerated_flat", 20)
    double_flat = await value("credit_double_period_flat", 10)

    transport_on = counts["buses"] > 0 and counts["routes"] > 0
    duties_on = counts["duty_types"] > 0
    teachers_over = max(0, counts["teachers"] - teacher_free)
    extra_campuses = max(0, counts["campuses"] - 1)
    extra_days = max(0, cycle_days - day_free)

    lines = [
        {"key": "base", "label": "Base fee", "detail": "per generation",
         "credits": base},
        {"key": "students", "label": "Students",
         "detail": f"{counts['students']} x {per_student}",
         "credits": counts["students"] * per_student},
        {"key": "teachers", "label": "Teachers over threshold",
         "detail": f"{teachers_over} over {teacher_free}",
         "credits": teachers_over * per_teacher},
        {"key": "campuses", "label": "Extra campuses",
         "detail": f"{extra_campuses} beyond the first",
         "credits": extra_campuses * per_campus},
        {"key": "transport", "label": "Transport",
         "detail": "enabled" if transport_on else "not used",
         "credits": transport_flat if transport_on else 0},
        {"key": "routes", "label": "Bus routes",
         "detail": f"{counts['routes']} routes" if transport_on else "not used",
         "credits": counts["routes"] * per_route if transport_on else 0},
        {"key": "duties", "label": "Duties",
         "detail": "enabled" if duties_on else "not used",
         "credits": duties_flat if duties_on else 0},
        {"key": "days", "label": "Extra cycle days",
         "detail": f"{extra_days} beyond {day_free}",
         "credits": extra_days * per_day},
        {"key": "accelerated", "label": "Accelerated subjects",
         "detail": "in use" if counts["accelerated"] else "not used",
         "credits": accel_flat if counts["accelerated"] else 0},
        {"key": "doubles", "label": "Double periods",
         "detail": "in use" if counts["doubles"] else "not used",
         "credits": double_flat if counts["doubles"] else 0},
    ]

    for line in lines:
        line["credits"] = round(line["credits"], 2)

    return CostBreakdown(lines=lines,
                         total=round(sum(line["credits"] for line in lines)))


# --- State -------------------------------------------------------------------

async def account_state(school_id: str) -> dict:
    credits = await db.fetchrow("""
        SELECT balance, reserved, lifetime_purchased, lifetime_spent
        FROM school_credits WHERE school_id = $1
    """, school_id)
    billing = await db.fetchrow("""
        SELECT billing_mode, account_blocked, blocked_reason, payg_enabled,
               payg_card_last_four, payg_card_brand, payg_card_expires,
               trial_used, low_balance_threshold, outstanding_balance_cents
        FROM school_billing WHERE school_id = $1
    """, school_id)

    balance = credits["balance"] if credits else 0
    reserved = credits["reserved"] if credits else 0

    return {
        "balance": balance,
        "reserved": reserved,
        "available": balance - reserved,
        "lifetime_purchased": credits["lifetime_purchased"] if credits else 0,
        "lifetime_spent": credits["lifetime_spent"] if credits else 0,
        "billing_mode": billing["billing_mode"] if billing else "credits",
        "account_blocked": bool(billing["account_blocked"]) if billing else False,
        "blocked_reason": billing["blocked_reason"] if billing else None,
        "payg_enabled": bool(billing["payg_enabled"]) if billing else False,
        "card": {
            "last_four": billing["payg_card_last_four"],
            "brand": billing["payg_card_brand"],
            "expires": billing["payg_card_expires"],
        } if billing and billing["payg_card_last_four"] else None,
        "trial_used": bool(billing["trial_used"]) if billing else False,
        "low_balance_threshold": billing["low_balance_threshold"] if billing else 300,
        "outstanding_cents": billing["outstanding_balance_cents"] if billing else 0,
    }


# --- Reserving ---------------------------------------------------------------

async def check_and_reserve(school_id: str, timetable_id: str,
                            estimate: CostBreakdown) -> dict:
    """
    Hold credits for a generation that is about to start.

    The balance row is locked for the duration, so two generations starting at
    once cannot both pass a check only one of them can afford. Returns the
    reservation; raises BillingError if the school may not proceed.
    """
    async with db.transaction() as conn:
        billing = await conn.fetchrow("""
            SELECT billing_mode, account_blocked, blocked_reason, payg_enabled,
                   payg_card_last_four
            FROM school_billing WHERE school_id = $1
            FOR UPDATE
        """, school_id)
        if billing is None:
            raise BillingError("This school has no billing record.",
                               code="no_billing_record")
        if billing["account_blocked"]:
            raise AccountBlocked(billing["blocked_reason"])

        mode = billing["billing_mode"]

        if mode == "payg" and not billing["payg_card_last_four"]:
            raise NoPaymentMethod()

        credits = await conn.fetchrow("""
            SELECT balance, reserved FROM school_credits
            WHERE school_id = $1 FOR UPDATE
        """, school_id)
        if credits is None:
            raise BillingError("This school has no credit record.",
                               code="no_credit_record")

        available = credits["balance"] - credits["reserved"]

        # Only prepaid credits must be funded up front. PAYG is charged after
        # the run, and invoiced schools are billed monthly (BILLING.md).
        if mode == "credits" and available < estimate.total:
            raise InsufficientCredits(estimate.total, available)

        if mode == "credits":
            await conn.execute("""
                UPDATE school_credits
                SET reserved = reserved + $2, updated_at = now()
                WHERE school_id = $1
            """, school_id, estimate.total)

        by_key = {line["key"]: line["credits"] for line in estimate.lines}
        await conn.execute("""
            INSERT INTO generation_cost_breakdown
                (timetable_id, school_id, estimated_credits, base_fee,
                 student_cost, teacher_cost, campus_cost, transport_cost,
                 bus_route_cost, duties_cost, day_cost, accelerated_cost,
                 double_period_cost, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'reserved')
        """, timetable_id, school_id, estimate.total,
            int(by_key.get("base", 0)), by_key.get("students", 0),
            by_key.get("teachers", 0), int(by_key.get("campuses", 0)),
            int(by_key.get("transport", 0)), int(by_key.get("routes", 0)),
            int(by_key.get("duties", 0)), int(by_key.get("days", 0)),
            int(by_key.get("accelerated", 0)), int(by_key.get("doubles", 0)))

    log.info("Reserved %s credits for timetable %s (%s mode)",
             estimate.total, timetable_id, mode)
    return {"reserved": estimate.total if mode == "credits" else 0,
            "billing_mode": mode, "estimated": estimate.total}


# --- Settling ----------------------------------------------------------------

async def settle(school_id: str, timetable_id: str, actual: int,
                 *, school_retries: int = 0, dev_retries: int = 0,
                 attempt_id: Optional[str] = None) -> dict:
    """
    Charge for a finished generation and release the hold.

    School-caused retries are charged; dev-caused ones are not
    (ARCHITECTURE.md). The retry surcharge is added to the actual cost rather
    than billed separately, so one generation is one transaction.
    """
    retry_rate = await settings_service.get_int("credit_school_retry", 16)
    surcharge = school_retries * retry_rate
    charged = actual + surcharge

    async with db.transaction() as conn:
        breakdown = await conn.fetchrow("""
            SELECT id, estimated_credits, status FROM generation_cost_breakdown
            WHERE timetable_id = $1 FOR UPDATE
        """, timetable_id)
        if breakdown is None:
            raise BillingError("No cost record for this generation.",
                               code="no_cost_record")
        if breakdown["status"] == "charged":
            log.warning("Generation %s already settled - ignoring", timetable_id)
            return {"already_settled": True}

        mode = await conn.fetchval(
            "SELECT billing_mode FROM school_billing WHERE school_id = $1",
            school_id)
        reserved = breakdown["estimated_credits"] if mode == "credits" else 0

        credits = await conn.fetchrow("""
            SELECT balance, reserved FROM school_credits
            WHERE school_id = $1 FOR UPDATE
        """, school_id)

        new_balance = credits["balance"]
        if mode == "credits":
            new_balance = credits["balance"] - charged
            await conn.execute("""
                UPDATE school_credits
                SET balance = balance - $2,
                    reserved = GREATEST(0, reserved - $3),
                    lifetime_spent = lifetime_spent + $2,
                    updated_at = now()
                WHERE school_id = $1
            """, school_id, charged, reserved)

        await conn.execute("""
            UPDATE generation_cost_breakdown
            SET actual_credits = $2, school_retry_cost = $3, dev_retry_cost = 0,
                status = 'charged', settled_at = now()
            WHERE id = $1
        """, breakdown["id"], charged, surcharge)

        note = f"Generation ({actual} credits"
        if surcharge:
            note += f" + {surcharge} for {school_retries} retry"
            note += "ies" if school_retries > 1 else "y"
        if dev_retries:
            note += f", {dev_retries} system retr"
            note += "ies" if dev_retries > 1 else "y"
            note += " not charged"
        note += ")"

        await conn.execute("""
            INSERT INTO credit_transactions
                (school_id, type, amount, balance_after, timetable_id,
                 attempt_id, payment_method, note, created_by)
            VALUES ($1, 'generation', $2, $3, $4, $5, 'system', $6, 'system')
        """, school_id, -charged if mode == "credits" else 0, new_balance,
            timetable_id, attempt_id, note)

    log.info("Settled timetable %s: %s credits (%s mode)",
             timetable_id, charged, mode)
    return {"charged": charged, "surcharge": surcharge,
            "balance_after": new_balance, "billing_mode": mode,
            "dev_retries_free": dev_retries}


# --- Pay as you go -----------------------------------------------------------

async def charge_payg(school_id: str, timetable_id: str, credits: int) -> dict:
    """
    Charge the saved card after a successful generation (BILLING.md).

    Runs after `settle`, and only for PAYG schools - prepaid credits are already
    deducted and invoiced schools are billed monthly. The timetable is already
    written and belongs to the school whatever happens here: a declined card
    creates a debt, it does not take the work away.

    A decline blocks the account and opens an outstanding charge accruing daily
    interest. That is deliberate and matches BILLING.md, but it is the single
    most damaging thing this module can do to a school, so it happens only on an
    explicit simulated decline - never on an error in this function.
    """
    mode = await db.fetchval(
        "SELECT billing_mode FROM school_billing WHERE school_id = $1", school_id)
    if mode != "payg":
        return {"charged": False, "reason": f"{mode} mode - nothing to charge"}

    # One attempt per generation. A second call finding a success returns it
    # rather than charging the card again: settle() is retried in the worker,
    # and this sits directly behind it.
    existing = await db.fetchrow("""
        SELECT id, status, amount_charged_cents FROM payg_charge_log
        WHERE timetable_id = $1
    """, timetable_id)
    if existing and existing["status"] == "success":
        log.warning("PAYG charge for %s already succeeded - ignoring", timetable_id)
        return {"charged": True, "already_charged": True,
                "amount_cents": existing["amount_charged_cents"]}

    billing = await db.fetchrow("""
        SELECT payg_card_last_four, payg_fake_method_id
        FROM school_billing WHERE school_id = $1
    """, school_id)
    if not billing or not billing["payg_card_last_four"]:
        # check_and_reserve refuses to start a PAYG run without a card, so this
        # means the card was removed mid-generation.
        raise BillingError("No card is saved to charge.", code="no_payment_method")

    rate_cents = await settings_service.get_int("credit_payg_rate_cents", 230)
    amount_cents = credits * rate_cents
    success_rate = await settings_service.get_float("fake_terminal_success_rate", 1.0)

    result = fake_terminal.charge(
        billing["payg_fake_method_id"] or school_id, amount_cents, success_rate)

    if result["success"]:
        async with db.transaction() as conn:
            await conn.execute("""
                INSERT INTO payg_charge_log
                    (school_id, timetable_id, actual_credits,
                     amount_charged_cents, fake_payment_id, status, charged_at)
                VALUES ($1,$2,$3,$4,$5,'success', now())
                ON CONFLICT (timetable_id) DO UPDATE SET
                    status = 'success', fake_payment_id = EXCLUDED.fake_payment_id,
                    amount_charged_cents = EXCLUDED.amount_charged_cents,
                    actual_credits = EXCLUDED.actual_credits,
                    failure_reason = NULL, charged_at = now(),
                    retry_count = payg_charge_log.retry_count + 1,
                    last_retry_at = now()
            """, school_id, timetable_id, credits, amount_cents,
                result["charge_id"])

            await conn.execute("""
                UPDATE school_billing
                SET payg_last_charged_at = now(), updated_at = now()
                WHERE school_id = $1
            """, school_id)

            balance = await conn.fetchval(
                "SELECT balance FROM school_credits WHERE school_id = $1", school_id)
            await conn.execute("""
                INSERT INTO credit_transactions
                    (school_id, type, amount, balance_after, timetable_id,
                     price_paid_cents, payment_method, fake_payment_id, note,
                     created_by)
                VALUES ($1,'payg_charge',0,$2,$3,$4,'fake_card',$5,$6,'system')
            """, school_id, balance or 0, timetable_id, amount_cents,
                result["charge_id"],
                f"Pay as you go: {credits} credits at {rate_cents}c")

        log.info("PAYG charged %s cents for timetable %s", amount_cents, timetable_id)

        await _receipt(school_id, timetable_id, credits, amount_cents,
                       billing, result["charge_id"])

        return {"charged": True, "amount_cents": amount_cents,
                "payment_id": result["charge_id"], "simulated": True}

    # --- Declined -------------------------------------------------------------
    reason = result.get("failure_reason", "card_declined")
    interest = await settings_service.get_float("payg_interest_rate", 0.20)

    async with db.transaction() as conn:
        charge_id = await conn.fetchval("""
            INSERT INTO outstanding_charges
                (school_id, charge_type, original_amount_cents,
                 interest_rate_daily, total_owed_cents, timetable_id,
                 last_interest_date)
            VALUES ($1, 'payg_failed', $2, $3, $2, $4, $5)
            RETURNING id
        """, school_id, amount_cents, interest, timetable_id,
            config.billing_date())

        await conn.execute("""
            INSERT INTO payg_charge_log
                (school_id, timetable_id, actual_credits, amount_charged_cents,
                 status, failure_reason, school_blocked, outstanding_charge_id)
            VALUES ($1,$2,$3,$4,'simulated_fail',$5,true,$6)
            ON CONFLICT (timetable_id) DO UPDATE SET
                status = 'simulated_fail', failure_reason = EXCLUDED.failure_reason,
                school_blocked = true,
                outstanding_charge_id = EXCLUDED.outstanding_charge_id,
                retry_count = payg_charge_log.retry_count + 1,
                last_retry_at = now()
        """, school_id, timetable_id, credits, amount_cents, reason, charge_id)

        await conn.execute("""
            UPDATE school_billing
            SET account_blocked = true, blocked_at = now(),
                blocked_reason = 'Pay as you go charge declined',
                outstanding_balance_cents = outstanding_balance_cents + $2,
                interest_started_at = coalesce(interest_started_at, now()),
                updated_at = now()
            WHERE school_id = $1
        """, school_id, amount_cents)

    log.warning("PAYG charge declined for %s (%s) - school blocked, %s cents owed",
                school_id, reason, amount_cents)

    # A school whose card just failed and whose account is now blocked must be
    # told. Wrapped, because failing to email must not undo the block or throw
    # back into the worker that had just finished their timetable.
    try:
        from services import email

        await email.notify_school(
            school_id, "notify_payg_failed", "payg_failed", {
                "school_name": await db.fetchval(
                    "SELECT name FROM schools WHERE id = $1", school_id),
                "amount_dollars": f"{amount_cents / 100:,.2f}",
                "card_brand": billing["payg_card_brand"] or "Card",
                "card_last_four": billing["payg_card_last_four"],
                "interest_rate_daily_pct": f"{interest * 100:g}",
                "update_payment_url": f"{config.APP_URL}/billing.html",
                "outstanding_url": f"{config.APP_URL}/billing.html",
            })
    except Exception:  # noqa: BLE001
        log.exception("Could not send the declined-payment email to %s", school_id)

    return {"charged": False, "declined": True, "reason": reason,
            "amount_cents": amount_cents, "blocked": True,
            "outstanding_charge_id": str(charge_id)}


async def _receipt(school_id: str, timetable_id: str, credits: int,
                   amount_cents: int, billing_row, charge_id: str) -> None:
    """The receipt for a successful pay-as-you-go charge. Never raises."""
    try:
        from services import email

        row = await db.fetchrow("""
            SELECT t.name AS timetable, s.name AS school
            FROM timetables t JOIN schools s ON s.id = t.school_id
            WHERE t.id = $1
        """, timetable_id)

        await email.notify_school(
            school_id, "notify_payg_charged", "payg_receipt", {
                "school_name": row["school"] if row else "your school",
                "timetable_name": row["timetable"] if row else "your timetable",
                "credits_used": credits,
                "amount_charged_dollars": f"{amount_cents / 100:,.2f}",
                "card_brand": billing_row["payg_card_brand"] or "Card",
                "card_last_four": billing_row["payg_card_last_four"],
                "fake_payment_id": charge_id,
                "billing_url": f"{config.APP_URL}/billing.html",
            })
    except Exception:  # noqa: BLE001
        log.exception("Could not send the payment receipt to %s", school_id)


async def resolve_outstanding(charge_id: str, resolved_by: str,
                              *, write_off: bool = False) -> dict:
    """
    Mark a debt settled, and unblock the school if it was its last one.

    A school stays blocked while any charge is outstanding, so this checks for
    others rather than unblocking unconditionally.
    """
    async with db.transaction() as conn:
        charge = await conn.fetchrow("""
            SELECT id, school_id, total_owed_cents, status
            FROM outstanding_charges WHERE id = $1 FOR UPDATE
        """, charge_id)
        if charge is None:
            raise BillingError("No such outstanding charge.", code="no_charge")
        if charge["status"] != "outstanding":
            return {"already_resolved": True, "status": charge["status"]}

        await conn.execute("""
            UPDATE outstanding_charges
            SET status = $2, resolved_at = now(), resolved_by = $3
            WHERE id = $1
        """, charge_id, "written_off" if write_off else "paid", resolved_by)

        school_id = charge["school_id"]
        remaining = await conn.fetchval("""
            SELECT coalesce(sum(total_owed_cents), 0) FROM outstanding_charges
            WHERE school_id = $1 AND status = 'outstanding'
        """, school_id)

        await conn.execute("""
            UPDATE school_billing
            SET outstanding_balance_cents = $2,
                account_blocked = CASE WHEN $2 > 0 THEN account_blocked ELSE false END,
                blocked_at = CASE WHEN $2 > 0 THEN blocked_at ELSE NULL END,
                blocked_reason = CASE WHEN $2 > 0 THEN blocked_reason ELSE NULL END,
                interest_started_at = CASE WHEN $2 > 0
                    THEN interest_started_at ELSE NULL END,
                updated_at = now()
            WHERE school_id = $1
        """, school_id, remaining)

    log.info("Outstanding charge %s resolved by %s (%s cents still owed)",
             charge_id, resolved_by, remaining)
    return {"resolved": True, "remaining_cents": remaining,
            "unblocked": remaining == 0}


async def release(school_id: str, timetable_id: str,
                  reason: str = "generation failed") -> dict:
    """
    Drop the hold for a generation that never completed.

    Nothing is charged. A failed generation the school did not get a timetable
    from is not billable, whoever caused it.
    """
    async with db.transaction() as conn:
        breakdown = await conn.fetchrow("""
            SELECT id, estimated_credits, status FROM generation_cost_breakdown
            WHERE timetable_id = $1 FOR UPDATE
        """, timetable_id)
        if breakdown is None or breakdown["status"] != "reserved":
            return {"released": 0}

        mode = await conn.fetchval(
            "SELECT billing_mode FROM school_billing WHERE school_id = $1",
            school_id)
        amount = breakdown["estimated_credits"] if mode == "credits" else 0

        if amount:
            await conn.execute("""
                UPDATE school_credits
                SET reserved = GREATEST(0, reserved - $2), updated_at = now()
                WHERE school_id = $1
            """, school_id, amount)

        await conn.execute("""
            UPDATE generation_cost_breakdown
            SET status = 'refunded', actual_credits = 0, settled_at = now()
            WHERE id = $1
        """, breakdown["id"])

        balance = await conn.fetchval(
            "SELECT balance FROM school_credits WHERE school_id = $1", school_id)

        await conn.execute("""
            INSERT INTO credit_transactions
                (school_id, type, amount, balance_after, timetable_id,
                 payment_method, note, created_by)
            VALUES ($1, 'hold_released', 0, $2, $3, 'system', $4, 'system')
        """, school_id, balance, timetable_id,
            f"Hold of {amount} credits released - {reason}")

    log.info("Released hold for timetable %s (%s)", timetable_id, reason)
    return {"released": amount}


# --- Adjustments -------------------------------------------------------------

async def adjust(school_id: str, amount: int, note: str, created_by: str,
                 *, kind: str = "manual_adjustment",
                 package_id: Optional[str] = None,
                 price_paid_cents: Optional[int] = None,
                 payment_method: str = "manual",
                 fake_payment_id: Optional[str] = None,
                 timetable_id: Optional[str] = None) -> dict:
    """
    Add or remove credits, always with a reason and an author.

    Used for dev portal top-ups, package purchases and goodwill refunds. The
    balance is not allowed below zero: a negative adjustment larger than the
    balance is clamped and the note records what happened.

    `timetable_id` attaches the movement to the generation it relates to. A
    refund without it is untraceable and, worse, indistinguishable from a
    second refund of the same run - which is how a school gets paid twice.
    """
    if amount == 0:
        raise BillingError("Adjustment must be non-zero.", code="zero_adjustment")

    async with db.transaction() as conn:
        credits = await conn.fetchrow("""
            SELECT balance FROM school_credits WHERE school_id = $1 FOR UPDATE
        """, school_id)
        if credits is None:
            raise BillingError("This school has no credit record.",
                               code="no_credit_record")

        applied = amount
        if amount < 0 and credits["balance"] + amount < 0:
            applied = -credits["balance"]
            note = f"{note} (reduced from {amount} to {applied}: balance floor)"

        new_balance = credits["balance"] + applied

        await conn.execute("""
            UPDATE school_credits
            SET balance = $2,
                lifetime_purchased = lifetime_purchased
                    + CASE WHEN $3 > 0 THEN $3 ELSE 0 END,
                updated_at = now()
            WHERE school_id = $1
        """, school_id, new_balance, applied)

        await conn.execute("""
            INSERT INTO credit_transactions
                (school_id, type, amount, balance_after, package_id,
                 price_paid_cents, payment_method, fake_payment_id, note,
                 created_by, timetable_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """, school_id, kind, applied, new_balance, package_id,
            price_paid_cents, payment_method, fake_payment_id, note, created_by,
            timetable_id)

    log.info("Adjusted %s by %s credits (%s) -> %s",
             school_id, applied, kind, new_balance)
    return {"applied": applied, "balance": new_balance}


async def transactions(school_id: str, limit: int = 50) -> list[dict]:
    rows = await db.fetch("""
        SELECT ct.id, ct.type, ct.amount, ct.balance_after, ct.price_paid_cents,
               ct.payment_method, ct.note, ct.created_by, ct.created_at,
               t.name AS timetable_name, p.display_name AS package_name
        FROM credit_transactions ct
        LEFT JOIN timetables t ON t.id = ct.timetable_id
        LEFT JOIN credit_packages p ON p.id = ct.package_id
        WHERE ct.school_id = $1
        ORDER BY ct.created_at DESC
        LIMIT $2
    """, school_id, limit)
    return [dict(r) | {"id": str(r["id"])} for r in rows]

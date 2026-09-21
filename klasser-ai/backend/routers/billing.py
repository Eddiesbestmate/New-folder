"""
Billing - balance, packages, purchases, cards and transaction history.

Purchases go through the simulated terminal (BILLING.md): no real payment is
processed anywhere in this file. Every credit movement goes through
services/billing.py so the transaction log stays a complete account.
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import config
from models import database as db
from routers.auth import CurrentUser, require_dev_role, require_owner
from services import billing as billing_service
from services import email as email_service
from services import fake_terminal
from services import jobs as jobs_service
from services import scheduler as scheduler_service
from services import settings as settings_service

log = logging.getLogger("klasser.billing")

router = APIRouter(tags=["billing"])


class CardIn(BaseModel):
    """Whatever the user typed. The terminal turns it into a card record."""
    raw_input: str = Field(min_length=1, max_length=200)


class PurchaseIn(BaseModel):
    package: str = Field(min_length=1, max_length=40)
    raw_input: Optional[str] = Field(default=None, max_length=200)


class AdjustIn(BaseModel):
    school_id: str
    amount: int = Field(ge=-100000, le=100000)
    note: str = Field(min_length=3, max_length=300)


class ModeIn(BaseModel):
    billing_mode: str

    @property
    def valid(self) -> bool:
        return self.billing_mode in ("credits", "payg", "invoiced")


# --- Account -----------------------------------------------------------------

@router.get("/account")
async def account(user: CurrentUser) -> dict:
    state = await billing_service.account_state(user["school_id"])
    estimate = await billing_service.estimate_cost(user["school_id"])

    runs_left = (state["available"] // estimate.total) if estimate.total else 0

    return state | {
        "estimated_generation_cost": estimate.total,
        "generations_remaining": max(0, runs_left),
        "low_balance": state["available"] < state["low_balance_threshold"],
        # The edit page quotes this before a school commits to a change.
        "edit_session_cost": await settings_service.get_int(
            "credit_edit_session", 10),
    }


@router.get("/packages")
async def packages(user: CurrentUser) -> dict:
    rows = await db.fetch("""
        SELECT name, display_name, credits, price_per_credit, total_price_cents,
               once_per_school, credits_expire_days
        FROM credit_packages WHERE is_active = true ORDER BY display_order
    """)
    state = await billing_service.account_state(user["school_id"])
    payg_rate = await settings_service.get_int("credit_payg_rate_cents", 230)

    return {
        "packages": [
            {
                "name": p["name"],
                "display_name": p["display_name"],
                "credits": p["credits"],
                "price_dollars": (p["total_price_cents"] or 0) / 100,
                "price_per_credit": float(p["price_per_credit"]),
                "once_per_school": p["once_per_school"],
                "expires_days": p["credits_expire_days"],
                # The trial is once per school, so it disappears after use.
                "available": not (p["once_per_school"] and state["trial_used"]),
            }
            for p in rows
        ],
        "payg_rate_dollars": payg_rate / 100,
        "billing_mode": state["billing_mode"],
    }


@router.get("/transactions")
async def transactions(user: CurrentUser,
                       limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    return await billing_service.transactions(user["school_id"], limit)


# --- Purchasing --------------------------------------------------------------

@router.post("/purchase")
async def purchase(payload: PurchaseIn,
                   user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Buy a credit package through the simulated terminal.

    No real payment is processed. The card is generated from whatever was typed,
    and the charge either succeeds or is declined according to the
    fake_terminal_success_rate setting.
    """
    school_id = user["school_id"]

    package = await db.fetchrow("""
        SELECT id, name, display_name, credits, total_price_cents,
               once_per_school
        FROM credit_packages WHERE name = $1 AND is_active = true
    """, payload.package)
    if package is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such package")

    if package["once_per_school"]:
        used = await db.fetchval(
            "SELECT trial_used FROM school_billing WHERE school_id = $1", school_id)
        if used:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"The {package['display_name']} package can only be bought once.")

    rate = await settings_service.get_float("fake_terminal_success_rate", 1.0)
    result = fake_terminal.charge(
        payload.raw_input or user["email"], package["total_price_cents"], rate)

    if not result["success"]:
        log.info("Simulated decline for %s buying %s", school_id, package["name"])
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, {
            "message": "The card was declined. No credits were added.",
            "code": result["failure_reason"],
        })

    outcome = await billing_service.adjust(
        school_id, package["credits"],
        f"Purchased {package['display_name']}",
        created_by=user["email"], kind="purchase",
        package_id=str(package["id"]),
        price_paid_cents=package["total_price_cents"],
        payment_method="fake_card", fake_payment_id=result["charge_id"])

    if package["once_per_school"]:
        await db.execute("""
            UPDATE school_billing
            SET trial_used = true, trial_purchased_at = now(), updated_at = now()
            WHERE school_id = $1
        """, school_id)

    await email_service.send_if_preferred(
        str(user["id"]), "notify_purchase_receipt", user["email"],
        "credit_purchase_receipt", {
            "first_name": user.get("first_name", ""),
            "school_name": await db.fetchval(
                "SELECT name FROM schools WHERE id = $1", school_id),
            "package_name": package["display_name"],
            "credits": package["credits"],
            "price_dollars": f"{package['total_price_cents'] / 100:,.2f}",
            "new_balance": outcome["balance"],
            "billing_url": f"{config.APP_URL}/billing.html",
        }, school_id=str(school_id))

    return {
        "credits_added": package["credits"],
        "balance": outcome["balance"],
        "charged_dollars": package["total_price_cents"] / 100,
        "payment_id": result["charge_id"],
        "simulated": True,
    }


# --- Card --------------------------------------------------------------------

@router.put("/card")
async def save_card(payload: CardIn,
                    user: Annotated[dict, Depends(require_owner)]) -> dict:
    """Save a simulated card for pay-as-you-go."""
    card = fake_terminal.process_fake_card(payload.raw_input)

    await db.execute("""
        UPDATE school_billing
        SET payg_card_last_four = $2, payg_card_brand = $3,
            payg_card_expires = $4, payg_fake_method_id = $5,
            payg_enabled = true, updated_at = now()
        WHERE school_id = $1
    """, user["school_id"], card["last_four"], card["brand"],
        card["expires"], card["payment_method_id"])

    # The generated number never leaves the server, even though it is fake.
    return {
        "brand": card["brand"],
        "last_four": card["last_four"],
        "expires": card["expires"],
        "masked": card["masked"],
        "simulated": True,
    }


@router.delete("/card", status_code=status.HTTP_204_NO_CONTENT)
async def remove_card(user: Annotated[dict, Depends(require_owner)]) -> None:
    mode = await db.fetchval(
        "SELECT billing_mode FROM school_billing WHERE school_id = $1",
        user["school_id"])
    if mode == "payg":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Switch off pay as you go before removing the card.")

    await db.execute("""
        UPDATE school_billing
        SET payg_card_last_four = NULL, payg_card_brand = NULL,
            payg_card_expires = NULL, payg_fake_method_id = NULL,
            payg_enabled = false, updated_at = now()
        WHERE school_id = $1
    """, user["school_id"])


@router.put("/mode")
async def set_mode(payload: ModeIn,
                   user: Annotated[dict, Depends(require_owner)]) -> dict:
    if not payload.valid:
        raise HTTPException(422, "billing_mode must be credits, payg or invoiced")

    if payload.billing_mode == "payg":
        card = await db.fetchval(
            "SELECT payg_card_last_four FROM school_billing WHERE school_id = $1",
            user["school_id"])
        if not card:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Add a card before switching to pay as you go.")

    if payload.billing_mode == "invoiced":
        approved = await db.fetchval(
            "SELECT invoiced_approved FROM school_billing WHERE school_id = $1",
            user["school_id"])
        if not approved:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Invoiced billing has not been approved for this school.")

    await db.execute("""
        UPDATE school_billing SET billing_mode = $2, updated_at = now()
        WHERE school_id = $1
    """, user["school_id"], payload.billing_mode)
    return {"billing_mode": payload.billing_mode}


# --- Applying for invoiced billing ----------------------------------------------

class ApplicationIn(BaseModel):
    billing_contact_name: str = Field(min_length=2, max_length=120)
    billing_contact_email: str = Field(min_length=5, max_length=200)
    billing_contact_phone: Optional[str] = Field(default=None, max_length=40)
    organisation_abn: Optional[str] = Field(default=None, max_length=20)
    reason: Optional[str] = Field(default=None, max_length=1000)


@router.post("/applications", status_code=status.HTTP_201_CREATED)
async def apply_for_invoicing(payload: ApplicationIn,
                              user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Ask to be invoiced monthly instead of prepaying.

    Most schools cannot pay by card at all - they raise a purchase order and pay
    on invoice - so this is the path that makes the product buyable by a real
    school. A dev approves it; approval grants permission rather than switching
    them over, so the school still chooses.
    """
    school_id = user["school_id"]

    approved = await db.fetchval(
        "SELECT invoiced_approved FROM school_billing WHERE school_id = $1",
        school_id)
    if approved:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Invoiced billing is already approved for this school. Choose it "
            "in payment method.")

    pending = await db.fetchval("""
        SELECT id FROM invoiced_billing_applications
        WHERE school_id = $1 AND status = 'pending'
    """, school_id)
    if pending:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "An application is already being reviewed.")

    application_id = await db.fetchval("""
        INSERT INTO invoiced_billing_applications
            (school_id, applied_by, billing_contact_name, billing_contact_email,
             billing_contact_phone, organisation_abn, reason)
        VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
    """, school_id, user["id"], payload.billing_contact_name,
        payload.billing_contact_email, payload.billing_contact_phone,
        payload.organisation_abn, payload.reason)

    log.info("Invoiced billing application %s from %s", application_id, school_id)
    return {"id": str(application_id), "status": "pending"}


@router.get("/applications")
async def my_applications(user: CurrentUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT id, billing_contact_name, billing_contact_email, status,
               created_at, reviewed_at, rejection_reason
        FROM invoiced_billing_applications
        WHERE school_id = $1 ORDER BY created_at DESC
    """, user["school_id"])
    return [dict(r) | {"id": str(r["id"])} for r in rows]


# --- Invoices and debts --------------------------------------------------------

@router.get("/invoices")
async def invoices(user: CurrentUser,
                   limit: Annotated[int, Query(ge=1, le=100)] = 24) -> list[dict]:
    rows = await db.fetch("""
        SELECT i.id, i.invoice_number, i.period_start, i.period_end,
               i.subtotal_credits, i.total_cents, i.status, i.due_date,
               i.days_overdue, i.sent_at, i.paid_at,
               (SELECT count(*) FROM invoice_line_items li
                 WHERE li.invoice_id = i.id) AS line_count
        FROM invoices i
        WHERE i.school_id = $1
        ORDER BY i.period_start DESC
        LIMIT $2
    """, user["school_id"], limit)
    return [
        dict(r) | {"id": str(r["id"]),
                   "period_start": r["period_start"].isoformat(),
                   "period_end": r["period_end"].isoformat(),
                   "due_date": r["due_date"].isoformat(),
                   "total_dollars": r["total_cents"] / 100}
        for r in rows
    ]


@router.get("/invoices/{invoice_id}")
async def invoice_detail(invoice_id: str, user: CurrentUser) -> dict:
    invoice = await db.fetchrow("""
        SELECT id, invoice_number, period_start, period_end, subtotal_credits,
               subtotal_cents, tax_cents, total_cents, status, due_date,
               days_overdue, sent_at, paid_at
        FROM invoices WHERE id = $1 AND school_id = $2
    """, invoice_id, user["school_id"])
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")

    lines = await db.fetch("""
        SELECT description, credits_used, amount_cents, generated_at
        FROM invoice_line_items WHERE invoice_id = $1 ORDER BY generated_at
    """, invoice_id)

    return {
        "invoice": dict(invoice) | {
            "id": str(invoice["id"]),
            "period_start": invoice["period_start"].isoformat(),
            "period_end": invoice["period_end"].isoformat(),
            "due_date": invoice["due_date"].isoformat(),
            "total_dollars": invoice["total_cents"] / 100,
        },
        "lines": [dict(line) | {"amount_dollars": line["amount_cents"] / 100}
                  for line in lines],
    }


@router.get("/outstanding")
async def outstanding(user: CurrentUser) -> dict:
    """
    What the school owes, and why it is blocked if it is.

    A blocked school lands here from every refused generation, so this answers
    the question it will actually have: how much, for what, and since when.
    """
    rows = await db.fetch("""
        SELECT oc.id, oc.charge_type, oc.original_amount_cents,
               oc.interest_accrued_cents, oc.total_owed_cents,
               oc.days_outstanding, oc.interest_rate_daily, oc.status,
               oc.created_at, t.name AS timetable_name, i.invoice_number
        FROM outstanding_charges oc
        LEFT JOIN timetables t ON t.id = oc.timetable_id
        LEFT JOIN invoices i ON i.id = oc.invoice_id
        WHERE oc.school_id = $1 AND oc.status = 'outstanding'
        ORDER BY oc.created_at
    """, user["school_id"])

    state = await billing_service.account_state(user["school_id"])
    return {
        "charges": [
            dict(r) | {"id": str(r["id"]),
                       "owed_dollars": r["total_owed_cents"] / 100,
                       "interest_rate_daily": float(r["interest_rate_daily"])}
            for r in rows
        ],
        "total_owed_cents": sum(r["total_owed_cents"] for r in rows),
        "account_blocked": state["account_blocked"],
        "blocked_reason": state["blocked_reason"],
    }


# --- Dev portal ---------------------------------------------------------------

@router.post("/dev/jobs/{job_name}")
async def run_job(job_name: str,
                  user: Annotated[dict, Depends(require_dev_role)],
                  force: Annotated[bool, Query()] = False) -> dict:
    """
    Run a scheduled billing job by hand.

    The jobs normally fire on a clock in the worker. This exists so a job can be
    demonstrated without waiting a day, and so a missed run can be caught up
    after an outage.

    `force` re-runs a job that already ran today. It is off by default because
    forcing interest accrual charges a school twice for the same day.
    """
    if job_name not in jobs_service.JOBS:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Unknown job. Known: {', '.join(jobs_service.JOBS)}")

    try:
        return await jobs_service.JOBS[job_name](
            triggered_by=user["email"], force=force)
    except Exception as exc:  # noqa: BLE001
        log.exception("Manual run of %s failed", job_name)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            f"{type(exc).__name__}: {exc}") from exc


@router.get("/dev/jobs")
async def job_history(user: Annotated[dict, Depends(require_dev_role)],
                      limit: Annotated[int, Query(ge=1, le=100)] = 30) -> dict:
    return {
        "known": sorted(jobs_service.JOBS),
        "scheduled": scheduler_service.scheduled(),
        "history": await jobs_service.history(limit),
    }


@router.post("/dev/outstanding/{charge_id}/resolve")
async def resolve_charge(charge_id: str,
                         user: Annotated[dict, Depends(require_dev_role)],
                         write_off: Annotated[bool, Query()] = False) -> dict:
    """
    Mark a debt paid or written off, unblocking the school if it was the last.

    Schools pay by bank transfer in the VCE build, so someone has to record it.
    """
    try:
        return await billing_service.resolve_outstanding(
            charge_id, user["email"], write_off=write_off)
    except billing_service.BillingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc



@router.post("/dev/adjust")
async def dev_adjust(payload: AdjustIn,
                     user: Annotated[dict, Depends(require_dev_role)]) -> dict:
    """
    Add or remove credits for any school.

    The only way credits enter an account in the VCE build without going
    through the simulated terminal. Every adjustment records who made it and
    why.
    """
    exists = await db.fetchval(
        "SELECT 1 FROM school_credits WHERE school_id = $1", payload.school_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such school")

    try:
        outcome = await billing_service.adjust(
            payload.school_id, payload.amount, payload.note,
            created_by=user["email"])
    except billing_service.BillingError as exc:
        raise HTTPException(422, exc.message) from exc

    return outcome


@router.get("/dev/schools")
async def dev_schools(user: Annotated[dict, Depends(require_dev_role)]) -> list[dict]:
    rows = await db.fetch("""
        SELECT s.id, s.name, c.balance, c.reserved, c.lifetime_spent,
               b.billing_mode, b.account_blocked,
               (SELECT count(*) FROM timetables t WHERE t.school_id = s.id)
                 AS timetables
        FROM schools s
        JOIN school_credits c ON c.school_id = s.id
        JOIN school_billing b ON b.school_id = s.id
        ORDER BY s.name
    """)
    return [dict(r) | {"id": str(r["id"])} for r in rows]

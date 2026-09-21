"""
Onboarding wizard - progress tracking and step completion.

Per ONBOARDING.md: school details and billing are mandatory and come first.
The remaining steps can be completed in any order. A school can generate a
timetable once school details, campuses, billing, teachers, students and rooms
are all done.

Step flags are derived from the data wherever possible rather than trusted from
the client - step_teachers is true because teachers exist, not because a browser
said so. Only billing, which has no rows of its own until Phase 10, is set
explicitly.
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

import config
from models import database as db
from routers.auth import CurrentUser, require_owner
from services import sandbox as sandbox_service

log = logging.getLogger("klasser.onboarding")

router = APIRouter(tags=["onboarding"])

VALID_TIMEZONES = {tz for tz, _ in config.SCHOOL_TIMEZONES}

# Steps that must be complete before a timetable can be generated.
REQUIRED_STEPS = [
    "step_school_details",
    "step_campuses",
    "step_billing",
    "step_teachers",
    "step_students",
    "step_rooms",
]

# Everything shown in the progress list, in display order.
ALL_STEPS = [
    ("step_school_details", "School details", True),
    ("step_campuses", "Campuses", True),
    ("step_billing", "Billing", True),
    ("step_layout", "Timetable layout", False),
    ("step_teachers", "Teachers", True),
    ("step_students", "Students", True),
    ("step_rooms", "Rooms", True),
    ("step_subjects", "Subjects", False),
    ("step_transport", "Transport", False),
]


# --- Schemas -----------------------------------------------------------------

class CampusIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: Optional[str] = Field(default=None, max_length=300)


class SchoolDetailsIn(BaseModel):
    school_name: str = Field(min_length=2, max_length=200)
    timezone: str
    campuses: list[CampusIn] = Field(min_length=1, max_length=20)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, v: str) -> str:
        if v not in VALID_TIMEZONES:
            raise ValueError("Unsupported timezone")
        return v

    @field_validator("campuses")
    @classmethod
    def unique_names(cls, v: list[CampusIn]) -> list[CampusIn]:
        names = [c.name.strip().lower() for c in v]
        if len(names) != len(set(names)):
            raise ValueError("Campus names must be unique")
        return v


class BillingChoiceIn(BaseModel):
    billing_mode: str

    @field_validator("billing_mode")
    @classmethod
    def known_mode(cls, v: str) -> str:
        if v not in ("credits", "payg", "invoiced"):
            raise ValueError("billing_mode must be credits, payg or invoiced")
        return v


class StepOut(BaseModel):
    key: str
    label: str
    required: bool
    complete: bool
    count: Optional[int] = None
    note: Optional[str] = None


class ProgressOut(BaseModel):
    steps: list[StepOut]
    completed: bool
    ready_to_generate: bool
    missing: list[str]


# --- Progress ----------------------------------------------------------------

async def recompute(school_id: str) -> dict:
    """
    Refresh the derived step flags from actual data, then return the row.

    Called after any change that could complete a step, and on every read of
    the progress endpoint so the dashboard cannot drift from reality.
    """
    counts = await db.fetchrow("""
        SELECT
            (SELECT count(*) FROM campuses         WHERE school_id = $1) AS campuses,
            (SELECT count(*) FROM teachers         WHERE school_id = $1) AS teachers,
            (SELECT count(*) FROM students         WHERE school_id = $1) AS students,
            (SELECT count(*) FROM rooms            WHERE school_id = $1) AS rooms,
            (SELECT count(*) FROM subject_settings WHERE school_id = $1) AS subjects,
            (SELECT count(*) FROM buses            WHERE school_id = $1) AS buses,
            (SELECT count(*) FROM timetable_layouts
              WHERE school_id = $1 AND is_active) AS layouts
    """, school_id)

    await db.execute("""
        UPDATE onboarding SET
            step_campuses  = $2,
            step_teachers  = $3,
            step_students  = $4,
            step_rooms     = $5,
            step_subjects  = $6,
            step_transport = $7,
            step_layout    = $8,
            updated_at     = now()
        WHERE school_id = $1
    """, school_id,
        counts["campuses"] > 0,
        counts["teachers"] > 0,
        counts["students"] > 0,
        counts["rooms"] > 0,
        counts["subjects"] > 0,
        counts["buses"] > 0,
        counts["layouts"] > 0,
    )

    row = await db.fetchrow(
        "SELECT * FROM onboarding WHERE school_id = $1", school_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Onboarding record missing")

    # Mark the whole thing complete once every required step is done.
    if all(row[s] for s in REQUIRED_STEPS) and not row["completed"]:
        await db.execute("""
            UPDATE onboarding SET completed = true, completed_at = now()
            WHERE school_id = $1
        """, school_id)
        row = await db.fetchrow(
            "SELECT * FROM onboarding WHERE school_id = $1", school_id)
        log.info("Onboarding complete for school %s", school_id)

    return {"row": dict(row), "counts": dict(counts)}


@router.get("/progress", response_model=ProgressOut)
async def progress(user: CurrentUser) -> ProgressOut:
    state = await recompute(user["school_id"])
    row, counts = state["row"], state["counts"]

    count_for = {
        "step_campuses": counts["campuses"],
        "step_teachers": counts["teachers"],
        "step_students": counts["students"],
        "step_rooms": counts["rooms"],
        "step_subjects": counts["subjects"],
        "step_transport": counts["buses"],
    }
    notes = {
        "step_subjects": "Optional - recommended",
        "step_transport": "Only needed if you run buses between campuses",
        "step_layout": "Needed before generating",
    }

    steps = [
        StepOut(
            key=key,
            label=label,
            required=required,
            complete=bool(row[key]),
            count=count_for.get(key),
            note=notes.get(key),
        )
        for key, label, required in ALL_STEPS
    ]

    missing = [
        label for key, label, _ in ALL_STEPS
        if key in REQUIRED_STEPS and not row[key]
    ]

    return ProgressOut(
        steps=steps,
        completed=bool(row["completed"]),
        ready_to_generate=not missing,
        missing=missing,
    )


# --- Step 1: school details and campuses -------------------------------------

@router.put("/school-details")
async def save_school_details(
    payload: SchoolDetailsIn,
    user: Annotated[dict, Depends(require_owner)],
) -> dict:
    """
    Save school name, timezone and campuses.

    Campuses already referenced by rooms, teachers, students or buses are
    updated rather than replaced, and are never deleted - removing one would
    orphan that data. Removal is a data-management action, not a wizard one.
    """
    school_id = user["school_id"]

    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE schools SET name = $2, timezone = $3 WHERE id = $1",
            school_id, payload.school_name, payload.timezone,
        )

        existing = await conn.fetch(
            "SELECT id, name FROM campuses WHERE school_id = $1 ORDER BY created_at",
            school_id,
        )
        by_name = {c["name"].strip().lower(): c["id"] for c in existing}

        for campus in payload.campuses:
            key = campus.name.strip().lower()
            if key in by_name:
                await conn.execute(
                    "UPDATE campuses SET name = $2, address = $3 WHERE id = $1",
                    by_name[key], campus.name, campus.address,
                )
            else:
                await conn.execute(
                    "INSERT INTO campuses (school_id, name, address) VALUES ($1, $2, $3)",
                    school_id, campus.name, campus.address,
                )

        await conn.execute("""
            UPDATE onboarding
            SET step_school_details = true, updated_at = now()
            WHERE school_id = $1
        """, school_id)

        await conn.execute("""
            UPDATE school_complexity
            SET campus_count = (SELECT count(*) FROM campuses WHERE school_id = $1),
                last_calculated_at = now()
            WHERE school_id = $1
        """, school_id)

    submitted = {c.name.strip().lower() for c in payload.campuses}
    dropped = [c["name"] for c in existing if c["name"].strip().lower() not in submitted]

    return {
        "saved": True,
        "kept_campuses": dropped,
        "message": (
            f"Saved. {len(dropped)} campus(es) not in the list were kept - "
            "remove them from the Campuses page if they are no longer used."
            if dropped else "Saved."
        ),
    }


@router.get("/school-details")
async def get_school_details(user: CurrentUser) -> dict:
    school = await db.fetchrow(
        "SELECT name, timezone FROM schools WHERE id = $1", user["school_id"])
    campuses = await db.fetch("""
        SELECT id, name, address FROM campuses
        WHERE school_id = $1 ORDER BY created_at
    """, user["school_id"])

    return {
        "school_name": school["name"],
        "timezone": school["timezone"],
        "campuses": [
            {"id": str(c["id"]), "name": c["name"], "address": c["address"]}
            for c in campuses
        ],
    }


# --- Step 2: billing ---------------------------------------------------------

@router.get("/billing-options")
async def billing_options(user: CurrentUser) -> dict:
    """Credit packages plus the school's current billing state."""
    packages = await db.fetch("""
        SELECT name, display_name, credits, price_per_credit, total_price_cents,
               once_per_school, credits_expire_days
        FROM credit_packages
        WHERE is_active = true
        ORDER BY display_order
    """)
    billing = await db.fetchrow(
        "SELECT billing_mode, trial_used FROM school_billing WHERE school_id = $1",
        user["school_id"])
    credits = await db.fetchrow(
        "SELECT balance FROM school_credits WHERE school_id = $1", user["school_id"])

    payg_rate = await db.fetchval(
        "SELECT value FROM settings WHERE key = 'credit_payg_rate_cents'")

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
                "available": not (p["once_per_school"] and billing["trial_used"]),
            }
            for p in packages
        ],
        "payg_rate_dollars": int(payg_rate or 230) / 100,
        "billing_mode": billing["billing_mode"],
        "trial_used": billing["trial_used"],
        "balance": credits["balance"] if credits else 0,
    }


@router.put("/billing-mode")
async def choose_billing_mode(
    payload: BillingChoiceIn,
    user: Annotated[dict, Depends(require_owner)],
) -> dict:
    """
    Record the school's billing choice and tick the billing step.

    Purchasing credits, entering a card and applying for invoiced billing are
    all Phase 10. This records the intent so onboarding can proceed; no money
    moves and no credits are granted here.
    """
    school_id = user["school_id"]

    async with db.transaction() as conn:
        await conn.execute("""
            UPDATE school_billing
            SET billing_mode = $2, updated_at = now()
            WHERE school_id = $1
        """, school_id, payload.billing_mode)

        await conn.execute("""
            UPDATE onboarding SET step_billing = true, updated_at = now()
            WHERE school_id = $1
        """, school_id)

    await recompute(school_id)
    return {"billing_mode": payload.billing_mode, "saved": True}


# --- The free sample run ------------------------------------------------------

@router.get("/sandbox")
async def sandbox_state(user: CurrentUser) -> dict:
    """Whether the free run is still available, and where it got to."""
    return await sandbox_service.state(user["school_id"])


@router.post("/sandbox", status_code=status.HTTP_202_ACCEPTED)
async def start_sandbox(user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Run a real generation on the demonstration school, free, once.

    A school that has just signed up has no data to generate from, so this runs
    against the seeded example school and grants read-only sight of the result.
    Nothing is charged and nothing is written to their own school.
    """
    try:
        return await sandbox_service.start(user["school_id"], user)
    except sandbox_service.SandboxError as exc:
        code = {
            "already_used": status.HTTP_409_CONFLICT,
            "is_sandbox": status.HTTP_409_CONFLICT,
            "no_sandbox": status.HTTP_503_SERVICE_UNAVAILABLE,
            "no_layout": status.HTTP_503_SERVICE_UNAVAILABLE,
            "sandbox_unhealthy": status.HTTP_503_SERVICE_UNAVAILABLE,
        }.get(exc.code, status.HTTP_400_BAD_REQUEST)
        raise HTTPException(code, exc.message) from exc

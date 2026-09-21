"""
Support tickets (SUPPORT.md).

A school raises a ticket from any page; the dev portal works a queue across all
schools. The two halves share a table and almost nothing else - what a school
may read, write and see is deliberately narrower than what a dev may.

The line that matters: `internal_notes` never appears in a school-facing
response. Every school-side query names its columns rather than selecting *,
so adding a dev-only column later cannot accidentally start leaking it.
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from models import database as db
from routers.auth import CurrentUser, require_dev_role

log = logging.getLogger("klasser.support")

router = APIRouter(tags=["support"])

CATEGORIES = ("billing", "generation", "data", "account", "other")
PRIORITIES = ("low", "normal", "high", "urgent")
STATUSES = ("open", "in_progress", "resolved", "closed")

# What a school sees. internal_notes is not on this list and must not be.
SCHOOL_COLUMNS = """
    t.id, t.category, t.subject, t.body, t.status, t.priority,
    t.timetable_id, t.created_at, t.updated_at, t.resolved_at
"""

# Phrases that mean the school cannot work. SUPPORT.md escalates billing
# tickets mentioning a block; these are the same idea, applied to the words a
# school actually uses when they are stuck.
URGENT_PHRASES = ("blocked", "cannot generate", "can't generate",
                  "locked out", "urgent", "not working at all")


class TicketIn(BaseModel):
    category: str
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=10, max_length=5000)
    timetable_id: Optional[str] = None


class TicketUpdateIn(BaseModel):
    """Dev-side changes. Every field optional; only what is sent is changed."""
    status: Optional[str] = None
    priority: Optional[str] = None
    internal_notes: Optional[str] = Field(default=None, max_length=5000)


def triage(category: str, body: str) -> str:
    """
    Opening priority, from what the ticket says.

    Deterministic, not AI: a school that says it is blocked must reach the top
    of the queue on the words alone, without waiting for a model or paying for
    a call.
    """
    text = body.lower()
    if any(phrase in text for phrase in URGENT_PHRASES):
        return "urgent" if category in ("billing", "generation") else "high"
    if category in ("billing", "generation"):
        return "high"
    return "normal"


# --- School side --------------------------------------------------------------

@router.post("/tickets", status_code=status.HTTP_201_CREATED)
async def create_ticket(payload: TicketIn, user: CurrentUser) -> dict:
    """
    Raise a ticket. Any signed-in user, not just the owner - the person hitting
    the problem is often not the person who pays.
    """
    if payload.category not in CATEGORIES:
        raise HTTPException(422, f"category must be one of {', '.join(CATEGORIES)}")

    if payload.timetable_id:
        owned = await db.fetchval(
            "SELECT 1 FROM timetables WHERE id = $1 AND school_id = $2",
            payload.timetable_id, user["school_id"])
        if not owned:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "That timetable does not belong to this school")

    priority = triage(payload.category, payload.body)

    # The most recent failed attempt on the attached timetable, so a dev opening
    # the ticket has the pipeline log without having to go looking.
    attempt_id = None
    if payload.timetable_id:
        attempt_id = await db.fetchval("""
            SELECT id FROM generation_attempts
            WHERE timetable_id = $1
            ORDER BY started_at DESC LIMIT 1
        """, payload.timetable_id)

    ticket_id = await db.fetchval("""
        INSERT INTO support_tickets
            (school_id, user_id, category, subject, body, timetable_id,
             attempt_id, priority)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id
    """, user["school_id"], user["id"], payload.category, payload.subject,
        payload.body, payload.timetable_id, attempt_id, priority)

    log.info("Ticket %s raised by %s (%s, %s)",
             ticket_id, user["email"], payload.category, priority)
    return {"id": str(ticket_id), "priority": priority, "status": "open"}


@router.get("/tickets")
async def list_tickets(user: CurrentUser,
                       limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict]:
    rows = await db.fetch(f"""
        SELECT {SCHOOL_COLUMNS},
               u.first_name || ' ' || u.surname AS raised_by,
               tt.name AS timetable_name
        FROM support_tickets t
        JOIN users u ON u.id = t.user_id
        LEFT JOIN timetables tt ON tt.id = t.timetable_id
        WHERE t.school_id = $1
        ORDER BY t.created_at DESC
        LIMIT $2
    """, user["school_id"], limit)  # noqa: S608 - SCHOOL_COLUMNS is a constant
    return [dict(r) | {"id": str(r["id"]),
                       "timetable_id": str(r["timetable_id"])
                       if r["timetable_id"] else None} for r in rows]


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, user: CurrentUser) -> dict:
    row = await db.fetchrow(f"""
        SELECT {SCHOOL_COLUMNS},
               u.first_name || ' ' || u.surname AS raised_by,
               tt.name AS timetable_name
        FROM support_tickets t
        JOIN users u ON u.id = t.user_id
        LEFT JOIN timetables tt ON tt.id = t.timetable_id
        WHERE t.id = $1 AND t.school_id = $2
    """, ticket_id, user["school_id"])  # noqa: S608
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return dict(row) | {"id": str(row["id"]),
                        "timetable_id": str(row["timetable_id"])
                        if row["timetable_id"] else None}


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(ticket_id: str, user: CurrentUser) -> dict:
    """
    A school withdrawing its own ticket.

    Distinct from 'resolved', which is the dev portal saying it was dealt with.
    Letting a school mark its own ticket resolved would make the queue's
    resolution numbers meaningless.
    """
    closed = await db.fetchval("""
        UPDATE support_tickets SET status = 'closed', updated_at = now()
        WHERE id = $1 AND school_id = $2 AND status IN ('open', 'in_progress')
        RETURNING id
    """, ticket_id, user["school_id"])
    if not closed:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No open ticket with that id")
    return {"closed": True, "id": ticket_id}


@router.get("/contact")
async def contact() -> dict:
    """The details on the support page. Public - a locked-out user needs them."""
    import config

    return {
        "support_email": config.SUPPORT_EMAIL,
        "billing_email": f"billing@{config.SUPPORT_EMAIL.split('@')[-1]}",
        "response_time": "within 1 business day",
    }


# --- Dev queue ----------------------------------------------------------------

@router.get("/dev/tickets")
async def dev_tickets(user: Annotated[dict, Depends(require_dev_role)],
                      ticket_status: Annotated[Optional[str], Query(alias="status")] = None,
                      category: Annotated[Optional[str], Query()] = None,
                      priority: Annotated[Optional[str], Query()] = None,
                      school_id: Annotated[Optional[str], Query()] = None,
                      limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
    """
    Every ticket, worst first.

    Ordered by priority then age, so the oldest urgent ticket is the first thing
    on the page rather than the newest of anything.
    """
    for value, allowed, name in ((ticket_status, STATUSES, "status"),
                                 (category, CATEGORIES, "category"),
                                 (priority, PRIORITIES, "priority")):
        if value is not None and value not in allowed:
            raise HTTPException(422, f"{name} must be one of {', '.join(allowed)}")

    rows = await db.fetch("""
        SELECT t.id, t.category, t.subject, t.status, t.priority,
               t.created_at, t.updated_at, t.resolved_at, t.first_response_at,
               t.internal_notes IS NOT NULL AS has_notes,
               s.name AS school, t.school_id,
               u.email AS raised_by
        FROM support_tickets t
        LEFT JOIN schools s ON s.id = t.school_id
        LEFT JOIN users u ON u.id = t.user_id
        WHERE ($1::text IS NULL OR t.status = $1)
          AND ($2::text IS NULL OR t.category = $2)
          AND ($3::text IS NULL OR t.priority = $3)
          AND ($4::uuid IS NULL OR t.school_id = $4)
        ORDER BY
            CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
            CASE t.priority
                WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                WHEN 'normal' THEN 2 ELSE 3 END,
            t.created_at
        LIMIT $5
    """, ticket_status, category, priority, school_id, limit)

    counts = await db.fetchrow("""
        SELECT count(*) FILTER (WHERE status = 'open') AS open,
               count(*) FILTER (WHERE status = 'in_progress') AS in_progress,
               count(*) FILTER (WHERE status = 'resolved') AS resolved,
               count(*) FILTER (WHERE status = 'closed') AS closed,
               count(*) FILTER (WHERE priority = 'urgent'
                                  AND status IN ('open', 'in_progress')) AS urgent
        FROM support_tickets
    """)

    return {
        "totals": dict(counts),
        "tickets": [dict(r) | {"id": str(r["id"]),
                               "school_id": str(r["school_id"])
                               if r["school_id"] else None} for r in rows],
    }


@router.get("/dev/tickets/{ticket_id}")
async def dev_ticket(ticket_id: str,
                     user: Annotated[dict, Depends(require_dev_role)]) -> dict:
    """One ticket in full, with the pipeline log if a timetable was attached."""
    row = await db.fetchrow("""
        SELECT t.*, s.name AS school,
               u.email AS raised_by, u.first_name || ' ' || u.surname AS raised_by_name,
               tt.name AS timetable_name
        FROM support_tickets t
        LEFT JOIN schools s ON s.id = t.school_id
        LEFT JOIN users u ON u.id = t.user_id
        LEFT JOIN timetables tt ON tt.id = t.timetable_id
        WHERE t.id = $1
    """, ticket_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")

    pipeline_log = []
    if row["attempt_id"]:
        pipeline_log = [dict(r) for r in await db.fetch("""
            SELECT stage, model_used, status, detail, tokens_used, latency_ms,
                   created_at
            FROM pipeline_log WHERE attempt_id = $1
            ORDER BY created_at DESC LIMIT 40
        """, row["attempt_id"])]

    errors = []
    if row["attempt_id"]:
        errors = [dict(r) for r in await db.fetch("""
            SELECT error_type, provider, stage, is_dev_fault, error_message,
                   created_at
            FROM error_log WHERE attempt_id = $1
            ORDER BY created_at DESC LIMIT 20
        """, row["attempt_id"])]

    ticket = dict(row)
    for key in ("id", "school_id", "user_id", "timetable_id", "attempt_id"):
        if ticket.get(key) is not None:
            ticket[key] = str(ticket[key])

    return {"ticket": ticket, "pipeline_log": pipeline_log, "errors": errors}


@router.patch("/dev/tickets/{ticket_id}")
async def update_ticket(ticket_id: str, payload: TicketUpdateIn,
                        user: Annotated[dict, Depends(require_dev_role)]) -> dict:
    """Work a ticket: change status or priority, or add an internal note."""
    if payload.status is not None and payload.status not in STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
    if payload.priority is not None and payload.priority not in PRIORITIES:
        raise HTTPException(422, f"priority must be one of {', '.join(PRIORITIES)}")

    existing = await db.fetchrow(
        "SELECT status, first_response_at FROM support_tickets WHERE id = $1",
        ticket_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")

    resolving = payload.status in ("resolved", "closed")

    await db.execute("""
        UPDATE support_tickets SET
            status   = coalesce($2, status),
            priority = coalesce($3, priority),
            internal_notes = coalesce($4, internal_notes),
            -- Stamped once, the first time a dev touches the ticket, so
            -- response time stays measurable after later edits.
            first_response_at = coalesce(first_response_at, now()),
            resolved_at = CASE WHEN $5 THEN coalesce(resolved_at, now())
                               ELSE resolved_at END,
            resolved_by = CASE WHEN $5 THEN coalesce(resolved_by, $6)
                               ELSE resolved_by END,
            updated_at = now()
        WHERE id = $1
    """, ticket_id, payload.status, payload.priority, payload.internal_notes,
        resolving, user["email"])

    log.info("Ticket %s updated by %s (%s)", ticket_id, user["email"],
             payload.status or "note")
    return {"updated": True, "id": ticket_id}

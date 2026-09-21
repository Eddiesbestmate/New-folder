"""
Timetables - cost estimate, listing, and the generated output.

The output endpoints serve the same entries grouped four ways: by class, by
teacher, by room and by student. A school reads a timetable differently
depending on who is asking, and the grouping is done here rather than in the
browser so a 1,750-entry cycle does not have to be shipped whole to render one
teacher's week.
"""

import logging
from collections import defaultdict
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from models import database as db
from routers.auth import CurrentUser, require_owner
from services import sandbox
from services import settings as settings_service
from services import versioning

log = logging.getLogger("klasser.timetables")

router = APIRouter(tags=["timetables"])

VIEWS = ("class", "teacher", "room", "student")


# --- Cost estimate -----------------------------------------------------------

@router.get("/estimate")
async def estimate(user: CurrentUser) -> dict:
    """
    What a generation would cost, itemised (BILLING.md).

    Recomputed from live counts rather than read from school_complexity, so the
    figure on the confirm screen matches the data as it stands right now.
    """
    school_id = user["school_id"]

    counts = await db.fetchrow("""
        SELECT
            (SELECT count(*) FROM students WHERE school_id = $1) AS students,
            (SELECT count(*) FROM teachers WHERE school_id = $1) AS teachers,
            (SELECT count(*) FROM campuses WHERE school_id = $1) AS campuses,
            (SELECT count(*) FROM routes   WHERE school_id = $1) AS routes,
            (SELECT count(*) FROM buses    WHERE school_id = $1) AS buses,
            (SELECT count(*) FROM subject_settings
              WHERE school_id = $1 AND is_double_period) AS doubles,
            (SELECT count(*) FROM duty_types WHERE school_id = $1) AS duty_types
    """, school_id)

    layout = await db.fetchrow("""
        SELECT days_in_cycle FROM timetable_layouts
        WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, school_id)
    cycle_days = layout["days_in_cycle"] if layout else 5

    async def number(key: str, default: float) -> float:
        return await settings_service.get_float(key, default)

    base = await number("credit_base_fee", 60)
    per_student = await number("credit_per_student", 0.20)
    teacher_free = await number("credit_teacher_threshold", 20)
    per_teacher = await number("credit_per_teacher_over", 0.40)
    per_campus = await number("credit_per_extra_campus", 30)
    transport_flat = await number("credit_transport_flat", 40)
    per_route = await number("credit_per_bus_route", 10)
    duties_flat = await number("credit_duties_flat", 20)
    day_free = await number("credit_day_threshold", 5)
    per_day = await number("credit_per_extra_day", 4)
    double_flat = await number("credit_double_period_flat", 10)

    transport_on = counts["buses"] > 0 and counts["routes"] > 0
    duties_on = counts["duty_types"] > 0

    lines = [
        {"label": "Base fee", "detail": "per generation", "credits": base},
        {"label": "Students", "detail": f"{counts['students']} x {per_student}",
         "credits": counts["students"] * per_student},
        {"label": "Teachers over threshold",
         "detail": f"{max(0, counts['teachers'] - int(teacher_free))} over "
                   f"{int(teacher_free)}",
         "credits": max(0, counts["teachers"] - int(teacher_free)) * per_teacher},
        {"label": "Extra campuses",
         "detail": f"{max(0, counts['campuses'] - 1)} beyond the first",
         "credits": max(0, counts["campuses"] - 1) * per_campus},
        {"label": "Transport", "detail": "enabled" if transport_on else "not used",
         "credits": transport_flat if transport_on else 0},
        {"label": "Bus routes", "detail": f"{counts['routes']} routes",
         "credits": counts["routes"] * per_route if transport_on else 0},
        {"label": "Duties", "detail": "enabled" if duties_on else "not used",
         "credits": duties_flat if duties_on else 0},
        {"label": "Extra cycle days",
         "detail": f"{max(0, cycle_days - int(day_free))} beyond {int(day_free)}",
         "credits": max(0, cycle_days - int(day_free)) * per_day},
        {"label": "Double periods",
         "detail": "in use" if counts["doubles"] else "not used",
         "credits": double_flat if counts["doubles"] else 0},
    ]

    total = round(sum(line["credits"] for line in lines))

    credits = await db.fetchrow("""
        SELECT balance, reserved FROM school_credits WHERE school_id = $1
    """, school_id)
    billing = await db.fetchrow("""
        SELECT billing_mode, account_blocked FROM school_billing WHERE school_id = $1
    """, school_id)

    available = (credits["balance"] - credits["reserved"]) if credits else 0
    mode = billing["billing_mode"] if billing else "credits"

    return {
        "lines": [line | {"credits": round(line["credits"], 2)} for line in lines],
        "total_credits": total,
        "balance": credits["balance"] if credits else 0,
        "reserved": credits["reserved"] if credits else 0,
        "available": available,
        "billing_mode": mode,
        "account_blocked": bool(billing["account_blocked"]) if billing else False,
        # Credit mode is the only mode that must be funded up front. PAYG and
        # invoiced schools are charged after the run (BILLING.md).
        "can_afford": mode != "credits" or available >= total,
        "cycle_days": cycle_days,
    }


# --- Listing -----------------------------------------------------------------

@router.get("")
async def list_timetables(user: CurrentUser,
                          limit: Annotated[int, Query(ge=1, le=100)] = 25) -> list[dict]:
    rows = await db.fetch("""
        SELECT t.id, t.name, t.status, t.created_at, t.completed_at,
               (SELECT count(*) FROM generation_attempts ga
                 WHERE ga.timetable_id = t.id) AS attempts,
               (SELECT count(*) FROM timetable_versions v
                 WHERE v.timetable_id = t.id) AS versions,
               (SELECT v.id FROM timetable_versions v
                 WHERE v.timetable_id = t.id
                 ORDER BY v.version_number DESC LIMIT 1) AS latest_version,
               (SELECT ga.failure_reason FROM generation_attempts ga
                 WHERE ga.timetable_id = t.id AND ga.failure_reason IS NOT NULL
                 ORDER BY ga.attempt_number DESC LIMIT 1) AS failure_reason
        FROM timetables t
        WHERE t.school_id = $1
        ORDER BY t.created_at DESC
        LIMIT $2
    """, user["school_id"], limit)

    return [
        dict(r) | {"id": str(r["id"]),
                   "latest_version": str(r["latest_version"])
                   if r["latest_version"] else None}
        for r in rows
    ]


async def owned_version(version_id: str, school_id: str, *,
                        write: bool = False) -> dict:
    """
    A version this school may see, or 404.

    `write=True` means only the school that owns it. The difference matters
    because of the free sample run: a school is granted sight of one timetable
    belonging to the demonstration school, and must never be able to publish,
    edit or discard it. Read paths pass nothing; anything that changes a version
    passes write=True and the grant does not apply.
    """
    row = await db.fetchrow("""
        SELECT v.id, v.timetable_id, v.version_number, v.status, v.notes,
               v.published_at, t.name, t.layout_id, v.school_id
        FROM timetable_versions v
        JOIN timetables t ON t.id = v.timetable_id
        WHERE v.id = $1
    """, version_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Timetable version not found")

    if str(row["school_id"]) == str(school_id):
        return dict(row)

    if not write and await sandbox.may_read(str(row["timetable_id"]), school_id):
        return dict(row) | {"is_sample": True}

    raise HTTPException(status.HTTP_404_NOT_FOUND, "Timetable version not found")


@router.get("/{timetable_id}/versions")
async def versions(timetable_id: str, user: CurrentUser) -> list[dict]:
    owned = await db.fetchval(
        "SELECT 1 FROM timetables WHERE id = $1 AND school_id = $2",
        timetable_id, user["school_id"])
    if not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Timetable not found")

    rows = await db.fetch("""
        SELECT v.id, v.version_number, v.status, v.notes, v.created_at,
               v.published_at,
               (SELECT count(*) FROM timetable_entries e WHERE e.version_id = v.id)
                 AS entries,
               u.first_name || ' ' || u.surname AS published_by
        FROM timetable_versions v
        LEFT JOIN users u ON u.id = v.published_by
        WHERE v.timetable_id = $1
        ORDER BY v.version_number DESC
    """, timetable_id)
    return [dict(r) | {"id": str(r["id"])} for r in rows]


# --- Output ------------------------------------------------------------------

@router.get("/version/{version_id}/summary")
async def summary(version_id: str, user: CurrentUser) -> dict:
    """Headline numbers plus the axes the grid needs."""
    version = await owned_version(version_id, user["school_id"])

    counts = await db.fetchrow("""
        SELECT count(*) AS entries,
               count(DISTINCT class_code) AS classes,
               count(DISTINCT teacher_id) AS teachers,
               count(DISTINCT room_id) AS rooms
        FROM timetable_entries WHERE version_id = $1
    """, version_id)

    periods = await db.fetch("""
        SELECT DISTINCT lp.day_number, lp.period_number, lp.label,
               to_char(lp.start_time, 'HH24:MI') AS start_time,
               to_char(lp.end_time, 'HH24:MI') AS end_time
        FROM layout_periods lp
        WHERE lp.layout_id = $1 AND lp.period_type = 'teaching'
        ORDER BY lp.day_number, lp.period_number
    """, version["layout_id"])

    students = await db.fetchval("""
        SELECT count(DISTINCT tes.student_id)
        FROM timetable_entry_students tes
        JOIN timetable_entries te ON te.id = tes.timetable_entry_id
        WHERE te.version_id = $1
    """, version_id)

    transport = await db.fetchval(
        "SELECT count(*) FROM transport_schedule WHERE version_id = $1", version_id)
    duties = await db.fetchval(
        "SELECT count(*) FROM duty_assignments WHERE version_id = $1", version_id)

    return {
        "version_id": str(version["id"]),
        "timetable_id": str(version["timetable_id"]),
        "name": version["name"],
        "version_number": version["version_number"],
        "status": version["status"],
        "notes": version["notes"],
        "published_at": version["published_at"],
        # The watermark. A sample belongs to the demonstration school, so it can
        # never be published, edited or exported as the viewer's own - but it
        # has to *say* so, or it looks like a timetable they can use.
        "is_sample": bool(version.get("is_sample")),
        "counts": dict(counts) | {"students": students,
                                  "transport": transport, "duties": duties},
        "days": sorted({p["day_number"] for p in periods}),
        "periods": sorted({p["period_number"] for p in periods}),
        "period_labels": {
            str(p["period_number"]): p["label"] or f"P{p['period_number']}"
            for p in periods
        },
    }


@router.get("/version/{version_id}/options")
async def options(version_id: str, user: CurrentUser,
                  view: Annotated[str, Query()] = "class") -> list[dict]:
    """The selectable items for a view - which class, teacher, room or student."""
    await owned_version(version_id, user["school_id"])
    if view not in VIEWS:
        raise HTTPException(422, f"view must be one of {VIEWS}")

    if view == "class":
        rows = await db.fetch("""
            SELECT DISTINCT class_code AS id, class_code AS label, subject AS detail
            FROM timetable_entries WHERE version_id = $1 ORDER BY class_code
        """, version_id)
    elif view == "teacher":
        rows = await db.fetch("""
            SELECT DISTINCT t.id::text AS id, t.full_name AS label,
                   count(*) OVER (PARTITION BY t.id)::text || ' periods' AS detail
            FROM timetable_entries te
            JOIN teachers t ON t.id = te.teacher_id
            WHERE te.version_id = $1 ORDER BY t.full_name
        """, version_id)
    elif view == "room":
        rows = await db.fetch("""
            SELECT DISTINCT r.id::text AS id, r.name AS label,
                   c.name AS detail
            FROM timetable_entries te
            JOIN rooms r ON r.id = te.room_id
            JOIN campuses c ON c.id = r.campus_id
            WHERE te.version_id = $1 ORDER BY r.name
        """, version_id)
    else:
        rows = await db.fetch("""
            SELECT DISTINCT s.id::text AS id, s.full_name AS label,
                   s.year_group AS detail
            FROM timetable_entry_students tes
            JOIN timetable_entries te ON te.id = tes.timetable_entry_id
            JOIN students s ON s.id = tes.student_id
            WHERE te.version_id = $1 ORDER BY s.year_group, s.full_name
        """, version_id)

    return [dict(r) for r in rows]


@router.get("/version/{version_id}/grid")
async def grid(version_id: str, user: CurrentUser,
               view: Annotated[str, Query()] = "class",
               key: Annotated[Optional[str], Query()] = None) -> dict:
    """
    One timetable grid: entries for a single class, teacher, room or student.

    Returned keyed by "day-period" so the page can lay out a grid without
    scanning the list for every cell.
    """
    await owned_version(version_id, user["school_id"])
    if view not in VIEWS:
        raise HTTPException(422, f"view must be one of {VIEWS}")
    if not key:
        raise HTTPException(422, "key is required")

    where = {
        "class": "te.class_code = $2",
        "teacher": "te.teacher_id = $2::uuid",
        "room": "te.room_id = $2::uuid",
        "student": ("EXISTS (SELECT 1 FROM timetable_entry_students x "
                    "WHERE x.timetable_entry_id = te.id AND x.student_id = $2::uuid)"),
    }[view]

    rows = await db.fetch(f"""
        SELECT te.id, te.class_code, te.subject, te.day_number,
               lp.period_number, lp.label AS period_label,
               t.full_name AS teacher, r.name AS room, c.name AS campus,
               (SELECT count(*) FROM timetable_entry_students tes
                 WHERE tes.timetable_entry_id = te.id) AS students
        FROM timetable_entries te
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        JOIN teachers t ON t.id = te.teacher_id
        JOIN rooms r ON r.id = te.room_id
        JOIN campuses c ON c.id = te.campus_id
        WHERE te.version_id = $1 AND {where}
        ORDER BY te.day_number, lp.period_number
    """, version_id, key)

    cells: dict[str, Any] = {}
    for row in rows:
        cells[f"{row['day_number']}-{row['period_number']}"] = dict(row) | {
            "id": str(row["id"])
        }

    return {"view": view, "key": key, "cells": cells, "count": len(rows)}


@router.get("/version/{version_id}/transport")
async def transport(version_id: str, user: CurrentUser) -> list[dict]:
    await owned_version(version_id, user["school_id"])
    rows = await db.fetch("""
        SELECT ts.day_number,
               to_char(ts.departure_time, 'HH24:MI') AS departure,
               to_char(ts.arrival_time, 'HH24:MI') AS arrival,
               b.name AS bus, f.name AS from_campus, tc.name AS to_campus,
               ts.passenger_count, ts.is_empty_leg
        FROM transport_schedule ts
        JOIN buses b ON b.id = ts.bus_id
        JOIN campuses f ON f.id = ts.from_campus_id
        JOIN campuses tc ON tc.id = ts.to_campus_id
        WHERE ts.version_id = $1
        ORDER BY ts.day_number, ts.departure_time
    """, version_id)
    return [dict(r) for r in rows]


@router.get("/version/{version_id}/duties")
async def duties(version_id: str, user: CurrentUser) -> list[dict]:
    await owned_version(version_id, user["school_id"])
    rows = await db.fetch("""
        SELECT lds.day_number, dt.name AS duty, lds.timing,
               to_char(lds.start_time, 'HH24:MI') AS start_time,
               to_char(lds.end_time, 'HH24:MI') AS end_time,
               t.full_name AS teacher, c.name AS campus
        FROM duty_assignments da
        JOIN layout_duty_slots lds ON lds.id = da.duty_slot_id
        JOIN duty_types dt ON dt.id = lds.duty_type_id
        JOIN teachers t ON t.id = da.teacher_id
        JOIN campuses c ON c.id = lds.campus_id
        WHERE da.version_id = $1
        ORDER BY lds.day_number, lds.start_time, t.surname
    """, version_id)
    return [dict(r) for r in rows]


@router.post("/version/{version_id}/publish")
async def publish(version_id: str,
                  user: Annotated[dict, Depends(require_owner)],
                  notes: Annotated[Optional[str], Query(max_length=500)] = None) -> dict:
    """
    Make this version the live one.

    Also the rollback path: publishing an archived version restores it and
    archives whatever was live. One operation, so the two cannot drift apart
    (VERSIONING.md).
    """
    try:
        return await versioning.publish(version_id, user, notes)
    except versioning.VersioningError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND if exc.code == "not_found"
            else status.HTTP_409_CONFLICT, exc.message) from exc


@router.delete("/version/{version_id}")
async def discard(version_id: str,
                  user: Annotated[dict, Depends(require_owner)]) -> dict:
    """Throw away a draft. Published and archived versions cannot be deleted."""
    try:
        return await versioning.discard(version_id, user)
    except versioning.VersioningError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND if exc.code == "not_found"
            else status.HTTP_409_CONFLICT, exc.message) from exc


@router.get("/version/{version_id}/compare/{other_id}")
async def compare(version_id: str, other_id: str, user: CurrentUser) -> dict:
    """
    What changed between two versions of the same timetable.

    Computed here rather than in the browser: the diff needs names rather than
    ids to be readable, and a large timetable is thousands of entries to ship
    twice.
    """
    try:
        return await versioning.compare(version_id, other_id, user["school_id"])
    except versioning.VersioningError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.message) from exc

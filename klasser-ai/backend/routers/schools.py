"""
School data management - teachers, students, rooms, subjects and the timetable
layout.

Every query filters on school_id taken from the authenticated user, never from
the request body. A caller cannot read or write another school's rows even if
they guess an id.

Deletes are guarded: a row referenced by a published or draft timetable is not
removed, because that would leave entries pointing at nothing. The endpoint
says what is referencing it instead.
"""

import logging
from datetime import time as dtime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from models import database as db
from routers.auth import CurrentUser, require_owner
from routers.onboarding import recompute

log = logging.getLogger("klasser.schools")

router = APIRouter(tags=["schools"])

PERIOD_TYPES = {"teaching", "mentor", "break", "lunch", "assembly", "blocked"}


# --- Helpers -----------------------------------------------------------------

async def owned_campus(school_id: str, campus_id: Optional[str]) -> Optional[str]:
    """Reject a campus id belonging to another school."""
    if campus_id is None:
        return None
    ok = await db.fetchval(
        "SELECT 1 FROM campuses WHERE id = $1 AND school_id = $2",
        campus_id, school_id)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown campus for this school")
    return campus_id


# A table name cannot be a bound parameter, so it is interpolated - but only
# from this list. Every caller currently passes a literal; the allowlist means a
# future caller passing anything else fails loudly rather than reaching SQL.
OWNED_TABLES = frozenset({
    "teachers", "students", "rooms", "subject_settings", "campuses",
    "buses", "routes", "timetable_layouts",
})


async def owned_row(table: str, row_id: str, school_id: str) -> dict:
    if table not in OWNED_TABLES:
        raise ValueError(f"owned_row called with unknown table {table!r}")

    row = await db.fetchrow(
        f"SELECT * FROM {table} WHERE id = $1 AND school_id = $2",  # noqa: S608
        row_id, school_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return dict(row)


async def block_if_referenced(kind: str, row_id: str) -> None:
    """
    Refuse to delete something a timetable still points at.

    timetable_entries has FKs to teachers and rooms, so the delete would fail
    anyway - this turns a database error into a clear explanation.
    """
    column = {"teacher": "teacher_id", "room": "room_id"}[kind]
    n = await db.fetchval(
        f"SELECT count(*) FROM timetable_entries WHERE {column} = $1", row_id)
    if n:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot delete: this {kind} appears in {n} timetable entr"
            f"{'y' if n == 1 else 'ies'}. Archive the timetable first.",
        )


def split_name(full: str) -> tuple[str, str]:
    """'Anna Maria Patel' -> ('Anna Maria', 'Patel'). See IMPORT.md."""
    parts = full.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


# --- Schemas -----------------------------------------------------------------

class TeacherIn(BaseModel):
    first_name: Optional[str] = Field(default=None, max_length=100)
    surname: Optional[str] = Field(default=None, max_length=100)
    full_name: Optional[str] = Field(default=None, max_length=200)
    subjects: list[str] = Field(default_factory=list, max_length=40)
    campus_id: Optional[str] = None
    max_blocks: Optional[int] = Field(default=None, ge=1, le=20)
    max_duties_per_cycle: Optional[int] = Field(default=None, ge=0, le=100)
    gender: Optional[str] = Field(default=None, max_length=20)

    @field_validator("subjects")
    @classmethod
    def clean_subjects(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s and s.strip()]

    def names(self) -> tuple[str, str]:
        """Accept either split names or one full name."""
        if self.first_name and self.surname:
            return self.first_name.strip(), self.surname.strip()
        if self.full_name:
            first, last = split_name(self.full_name)
            if not last:
                raise HTTPException(
                    422,
                    "Enter a first name and a surname",
                )
            return first, last
        raise HTTPException(
            422,
            "first_name and surname are required",
        )


class StudentIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    year_group: str = Field(min_length=1, max_length=40)
    campus_id: Optional[str] = None
    gender: Optional[str] = Field(default=None, max_length=20)
    ability_band: Optional[str] = Field(default=None, max_length=20)
    subjects: list[str] = Field(default_factory=list, max_length=40)


class RoomIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    campus_id: str
    capacity: int = Field(ge=1, le=2000)
    preferred_min_capacity: Optional[int] = Field(default=None, ge=0, le=2000)
    room_type: Optional[str] = Field(default=None, max_length=50)
    allows_split: bool = False


class SubjectIn(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    year_level: Optional[str] = Field(default=None, max_length=40)
    hard_max_size: Optional[int] = Field(default=None, ge=1, le=500)
    soft_max_size: Optional[int] = Field(default=None, ge=1, le=500)
    min_size: Optional[int] = Field(default=None, ge=0, le=500)
    default_room_type: Optional[str] = Field(default=None, max_length=50)
    campus_locked_id: Optional[str] = None
    is_double_period: bool = False
    code_prefix: Optional[str] = Field(default=None, max_length=10)
    # How many times a class in this subject meets per cycle. Null means use the
    # school-wide default. The allocator must hit at least min and may go to max.
    min_periods_per_cycle: Optional[int] = Field(default=None, ge=1, le=60)
    max_periods_per_cycle: Optional[int] = Field(default=None, ge=1, le=60)

    @field_validator("soft_max_size")
    @classmethod
    def soft_not_above_hard(cls, v, info):
        hard = info.data.get("hard_max_size")
        if v is not None and hard is not None and v > hard:
            raise ValueError("soft_max_size cannot exceed hard_max_size")
        return v

    @field_validator("max_periods_per_cycle")
    @classmethod
    def max_not_below_min(cls, v, info):
        lo = info.data.get("min_periods_per_cycle")
        if v is not None and lo is not None and v < lo:
            raise ValueError(
                "max_periods_per_cycle cannot be less than min_periods_per_cycle")
        return v


class PeriodIn(BaseModel):
    day_number: int = Field(ge=1, le=20)
    period_number: int = Field(ge=1, le=30)
    period_type: str
    # Declared as time, not str: asyncpg's TIME codec requires a datetime.time,
    # and a "::time" cast on the parameter does not convert one for it.
    # Pydantic parses "09:00" from JSON into this.
    start_time: dtime
    end_time: dtime
    label: Optional[str] = Field(default=None, max_length=60)

    @field_validator("period_type")
    @classmethod
    def known_type(cls, v: str) -> str:
        if v not in PERIOD_TYPES:
            raise ValueError(f"period_type must be one of {sorted(PERIOD_TYPES)}")
        return v


class LayoutIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    days_in_cycle: int = Field(ge=1, le=20)
    periods: list[PeriodIn] = Field(min_length=1, max_length=400)

    @field_validator("periods")
    @classmethod
    def unique_slots(cls, v: list[PeriodIn]) -> list[PeriodIn]:
        seen = {(p.day_number, p.period_number) for p in v}
        if len(seen) != len(v):
            raise ValueError("Each day and period number combination must appear once")
        return v


# --- Campuses ----------------------------------------------------------------

@router.get("/campuses")
async def list_campuses(user: CurrentUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT c.id, c.name, c.address,
               (SELECT count(*) FROM rooms    r WHERE r.campus_id = c.id) AS rooms,
               (SELECT count(*) FROM teachers t WHERE t.campus_id = c.id) AS teachers,
               (SELECT count(*) FROM students s WHERE s.campus_id = c.id) AS students
        FROM campuses c
        WHERE c.school_id = $1
        ORDER BY c.created_at
    """, user["school_id"])
    return [dict(r) | {"id": str(r["id"])} for r in rows]


# --- Teachers ----------------------------------------------------------------

@router.get("/teachers")
async def list_teachers(
    user: CurrentUser,
    search: Annotated[Optional[str], Query(max_length=100)] = None,
) -> list[dict]:
    rows = await db.fetch("""
        SELECT t.id, t.first_name, t.surname, t.full_name, t.subjects,
               t.campus_id, c.name AS campus_name, t.max_blocks,
               t.max_duties_per_cycle, t.gender,
               EXISTS (SELECT 1 FROM teacher_duty_exemptions e
                       WHERE e.teacher_id = t.id) AS duty_exempt
        FROM teachers t
        LEFT JOIN campuses c ON c.id = t.campus_id
        WHERE t.school_id = $1
          AND ($2::text IS NULL OR t.full_name ILIKE '%' || $2 || '%')
        ORDER BY t.surname, t.first_name
    """, user["school_id"], search)
    return [
        dict(r) | {"id": str(r["id"]),
                   "campus_id": str(r["campus_id"]) if r["campus_id"] else None}
        for r in rows
    ]


@router.post("/teachers", status_code=status.HTTP_201_CREATED)
async def create_teacher(payload: TeacherIn,
                         user: Annotated[dict, Depends(require_owner)]) -> dict:
    first, surname = payload.names()
    school_id = user["school_id"]
    await owned_campus(school_id, payload.campus_id)

    dupe = await db.fetchval("""
        SELECT 1 FROM teachers
        WHERE school_id = $1 AND lower(first_name) = lower($2)
          AND lower(surname) = lower($3)
    """, school_id, first, surname)
    if dupe:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{first} {surname} already exists")

    row = await db.fetchrow("""
        INSERT INTO teachers (school_id, first_name, surname, subjects, campus_id,
                              max_blocks, max_duties_per_cycle, gender)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id, full_name
    """, school_id, first, surname, payload.subjects, payload.campus_id,
        payload.max_blocks, payload.max_duties_per_cycle, payload.gender)

    await recompute(school_id)
    return {"id": str(row["id"]), "full_name": row["full_name"]}


@router.patch("/teachers/{teacher_id}")
async def update_teacher(teacher_id: str, payload: TeacherIn,
                         user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_row("teachers", teacher_id, school_id)
    first, surname = payload.names()
    await owned_campus(school_id, payload.campus_id)

    await db.execute("""
        UPDATE teachers SET first_name = $3, surname = $4, subjects = $5,
                            campus_id = $6, max_blocks = $7,
                            max_duties_per_cycle = $8, gender = $9
        WHERE id = $1 AND school_id = $2
    """, teacher_id, school_id, first, surname, payload.subjects,
        payload.campus_id, payload.max_blocks, payload.max_duties_per_cycle,
        payload.gender)
    return {"updated": True}


@router.delete("/teachers/{teacher_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_teacher(teacher_id: str,
                         user: Annotated[dict, Depends(require_owner)]) -> None:
    school_id = user["school_id"]
    await owned_row("teachers", teacher_id, school_id)
    await block_if_referenced("teacher", teacher_id)

    async with db.transaction() as conn:
        await conn.execute(
            "DELETE FROM teacher_duty_exemptions WHERE teacher_id = $1", teacher_id)
        await conn.execute(
            "DELETE FROM teacher_availability WHERE teacher_id = $1", teacher_id)
        await conn.execute(
            "DELETE FROM teachers WHERE id = $1 AND school_id = $2",
            teacher_id, school_id)
    await recompute(school_id)


@router.put("/teachers/{teacher_id}/duty-exempt")
async def set_duty_exempt(teacher_id: str, exempt: bool,
                          user: Annotated[dict, Depends(require_owner)],
                          reason: Annotated[Optional[str], Query(max_length=200)] = None,
                          ) -> dict:
    school_id = user["school_id"]
    await owned_row("teachers", teacher_id, school_id)

    if exempt:
        await db.execute("""
            INSERT INTO teacher_duty_exemptions (teacher_id, school_id, reason)
            VALUES ($1, $2, $3)
            ON CONFLICT (teacher_id) DO UPDATE SET reason = EXCLUDED.reason
        """, teacher_id, school_id, reason)
    else:
        await db.execute(
            "DELETE FROM teacher_duty_exemptions WHERE teacher_id = $1", teacher_id)
    return {"duty_exempt": exempt}


# --- Students ----------------------------------------------------------------

@router.get("/students")
async def list_students(
    user: CurrentUser,
    search: Annotated[Optional[str], Query(max_length=100)] = None,
    year_group: Annotated[Optional[str], Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    # Fixed placeholder positions, so paging never has to be interpolated:
    # $1 school, $2 search, $3 year group, then $4 limit and $5 offset on the
    # page query only. Passing NULL for an unused filter lets one query cover
    # every combination. The filters come first because the count below reuses
    # this fragment and must not be handed parameters it never mentions -
    # Postgres cannot infer their type and rejects the statement.
    where = ("s.school_id = $1"
             " AND ($2::text IS NULL OR s.full_name ILIKE '%' || $2 || '%')"
             " AND ($3::text IS NULL OR s.year_group = $3)")
    filters: list[Any] = [user["school_id"], search, year_group]
    args: list[Any] = [*filters, limit, offset]

    total = await db.fetchval(
        f"SELECT count(*) FROM students s WHERE {where}",  # noqa: S608
        *filters)

    rows = await db.fetch(f"""
        SELECT s.id, s.full_name, s.year_group, s.campus_id, c.name AS campus_name,
               s.gender, s.ability_band,
               ARRAY(SELECT subject FROM student_subjects ss
                     WHERE ss.student_id = s.id ORDER BY subject) AS subjects
        FROM students s
        LEFT JOIN campuses c ON c.id = s.campus_id
        WHERE {where}
        ORDER BY s.year_group, s.full_name
        LIMIT $4 OFFSET $5
    """, *args)  # noqa: S608 - `where` is a constant fragment, values are $n

    return {
        "total": total,
        "students": [
            dict(r) | {"id": str(r["id"]),
                       "campus_id": str(r["campus_id"]) if r["campus_id"] else None}
            for r in rows
        ],
    }


@router.post("/students", status_code=status.HTTP_201_CREATED)
async def create_student(payload: StudentIn,
                         user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_campus(school_id, payload.campus_id)

    dupe = await db.fetchval("""
        SELECT 1 FROM students
        WHERE school_id = $1 AND lower(full_name) = lower($2) AND year_group = $3
    """, school_id, payload.full_name, payload.year_group)
    if dupe:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{payload.full_name} already exists in {payload.year_group}")

    async with db.transaction() as conn:
        student_id = await conn.fetchval("""
            INSERT INTO students (school_id, full_name, year_group, campus_id,
                                  gender, ability_band)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """, school_id, payload.full_name, payload.year_group, payload.campus_id,
            payload.gender, payload.ability_band)

        for subject in {s.strip() for s in payload.subjects if s.strip()}:
            await conn.execute("""
                INSERT INTO student_subjects (student_id, school_id, subject)
                VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
            """, student_id, school_id, subject)

    await recompute(school_id)
    return {"id": str(student_id)}


@router.patch("/students/{student_id}")
async def update_student(student_id: str, payload: StudentIn,
                         user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_row("students", student_id, school_id)
    await owned_campus(school_id, payload.campus_id)

    async with db.transaction() as conn:
        await conn.execute("""
            UPDATE students SET full_name = $3, year_group = $4, campus_id = $5,
                                gender = $6, ability_band = $7
            WHERE id = $1 AND school_id = $2
        """, student_id, school_id, payload.full_name, payload.year_group,
            payload.campus_id, payload.gender, payload.ability_band)

        await conn.execute(
            "DELETE FROM student_subjects WHERE student_id = $1", student_id)
        for subject in {s.strip() for s in payload.subjects if s.strip()}:
            await conn.execute("""
                INSERT INTO student_subjects (student_id, school_id, subject)
                VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
            """, student_id, school_id, subject)

    return {"updated": True}


@router.delete("/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_student(student_id: str,
                         user: Annotated[dict, Depends(require_owner)]) -> None:
    school_id = user["school_id"]
    await owned_row("students", student_id, school_id)

    n = await db.fetchval(
        "SELECT count(*) FROM timetable_entry_students WHERE student_id = $1",
        student_id)
    if n:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot delete: this student appears in {n} timetable entries.")

    async with db.transaction() as conn:
        await conn.execute(
            "DELETE FROM student_subjects WHERE student_id = $1", student_id)
        await conn.execute(
            "DELETE FROM students WHERE id = $1 AND school_id = $2",
            student_id, school_id)
    await recompute(school_id)


# --- Rooms -------------------------------------------------------------------

@router.get("/rooms")
async def list_rooms(user: CurrentUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT r.id, r.name, r.campus_id, c.name AS campus_name, r.capacity,
               r.preferred_min_capacity, r.room_type, r.allows_split
        FROM rooms r
        JOIN campuses c ON c.id = r.campus_id
        WHERE r.school_id = $1
        ORDER BY c.name, r.name
    """, user["school_id"])
    return [dict(r) | {"id": str(r["id"]), "campus_id": str(r["campus_id"])}
            for r in rows]


@router.post("/rooms", status_code=status.HTTP_201_CREATED)
async def create_room(payload: RoomIn,
                      user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_campus(school_id, payload.campus_id)

    if (payload.preferred_min_capacity is not None
            and payload.preferred_min_capacity > payload.capacity):
        raise HTTPException(422,
                            "preferred_min_capacity cannot exceed capacity")

    dupe = await db.fetchval("""
        SELECT 1 FROM rooms
        WHERE school_id = $1 AND campus_id = $2 AND lower(name) = lower($3)
    """, school_id, payload.campus_id, payload.name)
    if dupe:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Room {payload.name} already exists on that campus")

    room_id = await db.fetchval("""
        INSERT INTO rooms (school_id, campus_id, name, capacity,
                           preferred_min_capacity, room_type, allows_split)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
    """, school_id, payload.campus_id, payload.name, payload.capacity,
        payload.preferred_min_capacity, payload.room_type, payload.allows_split)

    await recompute(school_id)
    return {"id": str(room_id)}


@router.patch("/rooms/{room_id}")
async def update_room(room_id: str, payload: RoomIn,
                      user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_row("rooms", room_id, school_id)
    await owned_campus(school_id, payload.campus_id)

    await db.execute("""
        UPDATE rooms SET name = $3, campus_id = $4, capacity = $5,
                         preferred_min_capacity = $6, room_type = $7,
                         allows_split = $8
        WHERE id = $1 AND school_id = $2
    """, room_id, school_id, payload.name, payload.campus_id, payload.capacity,
        payload.preferred_min_capacity, payload.room_type, payload.allows_split)
    return {"updated": True}


@router.delete("/rooms/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room(room_id: str,
                      user: Annotated[dict, Depends(require_owner)]) -> None:
    school_id = user["school_id"]
    await owned_row("rooms", room_id, school_id)
    await block_if_referenced("room", room_id)
    await db.execute("DELETE FROM rooms WHERE id = $1 AND school_id = $2",
                     room_id, school_id)
    await recompute(school_id)


# --- Subjects ----------------------------------------------------------------

@router.get("/subjects")
async def list_subjects(user: CurrentUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT s.id, s.subject, s.year_level, s.hard_max_size, s.soft_max_size,
               s.min_size, s.default_room_type, s.campus_locked_id,
               c.name AS campus_locked_name, s.is_double_period, s.code_prefix,
               s.min_periods_per_cycle, s.max_periods_per_cycle
        FROM subject_settings s
        LEFT JOIN campuses c ON c.id = s.campus_locked_id
        WHERE s.school_id = $1
        ORDER BY s.subject, s.year_level NULLS FIRST
    """, user["school_id"])
    return [
        dict(r) | {"id": str(r["id"]),
                   "campus_locked_id": str(r["campus_locked_id"])
                   if r["campus_locked_id"] else None}
        for r in rows
    ]


@router.post("/subjects", status_code=status.HTTP_201_CREATED)
async def create_subject(payload: SubjectIn,
                         user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_campus(school_id, payload.campus_locked_id)

    try:
        subject_id = await db.fetchval("""
            INSERT INTO subject_settings
                (school_id, subject, year_level, hard_max_size, soft_max_size,
                 min_size, default_room_type, campus_locked_id, is_double_period,
                 code_prefix, min_periods_per_cycle, max_periods_per_cycle)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id
        """, school_id, payload.subject, payload.year_level, payload.hard_max_size,
            payload.soft_max_size, payload.min_size, payload.default_room_type,
            payload.campus_locked_id, payload.is_double_period, payload.code_prefix,
            payload.min_periods_per_cycle, payload.max_periods_per_cycle)
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{payload.subject} is already configured for "
                f"{payload.year_level or 'all year levels'}") from exc
        raise

    await recompute(school_id)
    return {"id": str(subject_id)}


@router.patch("/subjects/{subject_id}")
async def update_subject(subject_id: str, payload: SubjectIn,
                         user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_row("subject_settings", subject_id, school_id)
    await owned_campus(school_id, payload.campus_locked_id)

    await db.execute("""
        UPDATE subject_settings
        SET subject = $3, year_level = $4, hard_max_size = $5, soft_max_size = $6,
            min_size = $7, default_room_type = $8, campus_locked_id = $9,
            is_double_period = $10, code_prefix = $11,
            min_periods_per_cycle = $12, max_periods_per_cycle = $13
        WHERE id = $1 AND school_id = $2
    """, subject_id, school_id, payload.subject, payload.year_level,
        payload.hard_max_size, payload.soft_max_size, payload.min_size,
        payload.default_room_type, payload.campus_locked_id,
        payload.is_double_period, payload.code_prefix,
        payload.min_periods_per_cycle, payload.max_periods_per_cycle)
    return {"updated": True}


@router.delete("/subjects/{subject_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subject(subject_id: str,
                         user: Annotated[dict, Depends(require_owner)]) -> None:
    school_id = user["school_id"]
    await owned_row("subject_settings", subject_id, school_id)
    await db.execute(
        "DELETE FROM subject_settings WHERE id = $1 AND school_id = $2",
        subject_id, school_id)
    await recompute(school_id)


# --- Timetable layout --------------------------------------------------------

@router.get("/layout")
async def get_layout(user: CurrentUser) -> Optional[dict]:
    layout = await db.fetchrow("""
        SELECT id, name, days_in_cycle FROM timetable_layouts
        WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, user["school_id"])
    if layout is None:
        return None

    periods = await db.fetch("""
        SELECT id, day_number, period_number, period_type,
               to_char(start_time, 'HH24:MI') AS start_time,
               to_char(end_time,   'HH24:MI') AS end_time, label
        FROM layout_periods
        WHERE layout_id = $1
        ORDER BY day_number, period_number
    """, layout["id"])

    return {
        "id": str(layout["id"]),
        "name": layout["name"],
        "days_in_cycle": layout["days_in_cycle"],
        "periods": [dict(p) | {"id": str(p["id"])} for p in periods],
    }


@router.put("/layout")
async def save_layout(payload: LayoutIn,
                      user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Replace the active layout.

    A layout already used by a timetable is never rewritten - timetable_entries
    reference individual periods, so editing them would silently move classes.
    A new layout is created instead and the old one deactivated, leaving
    existing timetables intact.
    """
    school_id = user["school_id"]

    for p in payload.periods:
        if p.day_number > payload.days_in_cycle:
            raise HTTPException(
                422,
                f"Period on day {p.day_number} exceeds the {payload.days_in_cycle}-day cycle")
        if p.start_time >= p.end_time:
            raise HTTPException(
                422,
                f"Day {p.day_number} period {p.period_number}: "
                f"start time must be before end time")

    current = await db.fetchrow("""
        SELECT id FROM timetable_layouts
        WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, school_id)

    in_use = False
    if current:
        in_use = bool(await db.fetchval(
            "SELECT count(*) FROM timetables WHERE layout_id = $1", current["id"]))

    async with db.transaction() as conn:
        if current and not in_use:
            await conn.execute(
                "DELETE FROM layout_periods WHERE layout_id = $1", current["id"])
            await conn.execute("""
                UPDATE timetable_layouts SET name = $2, days_in_cycle = $3,
                                             updated_at = now()
                WHERE id = $1
            """, current["id"], payload.name, payload.days_in_cycle)
            layout_id = current["id"]
        else:
            if current:
                await conn.execute(
                    "UPDATE timetable_layouts SET is_active = false WHERE id = $1",
                    current["id"])
            layout_id = await conn.fetchval("""
                INSERT INTO timetable_layouts (school_id, name, days_in_cycle, is_active)
                VALUES ($1, $2, $3, true) RETURNING id
            """, school_id, payload.name, payload.days_in_cycle)

        for p in payload.periods:
            await conn.execute("""
                INSERT INTO layout_periods
                    (layout_id, school_id, day_number, period_number, period_type,
                     start_time, end_time, label)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """, layout_id, school_id, p.day_number, p.period_number,
                p.period_type, p.start_time, p.end_time, p.label)

        await conn.execute("""
            UPDATE school_complexity SET cycle_days = $2, last_calculated_at = now()
            WHERE school_id = $1
        """, school_id, payload.days_in_cycle)

    await recompute(school_id)
    return {
        "id": str(layout_id),
        "replaced": bool(current and in_use),
        "message": ("Existing timetables use the old layout, so a new version was "
                    "created and the old one kept." if current and in_use
                    else "Layout saved."),
    }


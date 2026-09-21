"""
Deterministic constraint checks.

These are the hard rules. They run between pipeline layers so a bad AI response
is caught before the next layer builds on it, and again on the finished solution
(VALIDATION.md). No ambiguity and no judgement: if a check fails, the solution
is wrong.

The division of labour, from ARCHITECTURE.md: deterministic code enforces hard
constraints, AI handles judgement calls.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from models import database as db

log = logging.getLogger("klasser.deterministic")


@dataclass
class CheckResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.passed = False
        self.failures.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def merge(self, other: "CheckResult") -> None:
        if not other.passed:
            self.passed = False
        self.failures.extend(other.failures)
        self.warnings.extend(other.warnings)

    @property
    def summary(self) -> str:
        if self.passed:
            return "pass" if not self.warnings else f"pass with {len(self.warnings)} warning(s)"
        return self.failures[0] if self.failures else "failed"


def ok() -> CheckResult:
    return CheckResult(passed=True)


# --- Pre-generation ----------------------------------------------------------

async def pre_validate(school_id: str, layout_id: str) -> CheckResult:
    """
    Catch contradictions in the school's own data before any AI runs.

    Everything here is school-fault: the data cannot produce a timetable no
    matter how good the allocation is. Failing now means no credits are spent
    (PIPELINE.md, "fail loudly, fail cheaply").
    """
    result = ok()

    counts = await db.fetchrow("""
        SELECT
            (SELECT count(*) FROM teachers WHERE school_id = $1) AS teachers,
            (SELECT count(*) FROM students WHERE school_id = $1) AS students,
            (SELECT count(*) FROM rooms    WHERE school_id = $1) AS rooms,
            (SELECT count(*) FROM campuses WHERE school_id = $1) AS campuses
    """, school_id)

    if not counts["teachers"]:
        result.fail("No teachers. Add or import teachers before generating.")
    if not counts["students"]:
        result.fail("No students. Add or import students before generating.")
    if not counts["rooms"]:
        result.fail("No rooms. Add or import rooms before generating.")

    teaching = await db.fetchval("""
        SELECT count(*) FROM layout_periods
        WHERE layout_id = $1 AND period_type = 'teaching'
    """, layout_id)
    if not teaching:
        result.fail("The timetable layout has no teaching periods.")

    # A subject nobody can teach will never be allocatable.
    orphans = await db.fetch("""
        SELECT DISTINCT ss.subject
        FROM student_subjects ss
        WHERE ss.school_id = $1
          AND NOT EXISTS (
              SELECT 1 FROM teachers t
              WHERE t.school_id = $1 AND ss.subject = ANY(t.subjects))
    """, school_id)
    for row in orphans:
        result.fail(f"No teacher is listed as teaching '{row['subject']}'.")

    # A class that cannot fit in any room is unsatisfiable.
    too_big = await db.fetch("""
        SELECT ss.subject, ss.hard_max_size, ss.default_room_type,
               (SELECT max(capacity) FROM rooms r
                WHERE r.school_id = $1
                  AND (ss.default_room_type IS NULL
                       OR r.room_type = ss.default_room_type)) AS biggest
        FROM subject_settings ss
        WHERE ss.school_id = $1 AND ss.hard_max_size IS NOT NULL
    """, school_id)
    for row in too_big:
        if row["biggest"] is None:
            result.fail(
                f"'{row['subject']}' needs a {row['default_room_type']} room "
                "but the school has none.")
        elif row["hard_max_size"] > row["biggest"]:
            result.fail(
                f"'{row['subject']}' allows up to {row['hard_max_size']} students "
                f"but the largest suitable room holds {row['biggest']}.")

    if not counts["teachers"]:
        return result

    per_teacher = await db.fetchval("""
        SELECT coalesce(sum(coalesce(max_blocks, 5)), 0) FROM teachers
        WHERE school_id = $1
    """, school_id)
    if per_teacher == 0:
        result.fail("Every teacher has a maximum of 0 blocks per day.")

    # A subject cannot be asked to meet more often than the cycle has periods.
    slots_per_cycle = teaching or 0
    demanding = await db.fetch("""
        SELECT subject, year_level, min_periods_per_cycle, max_periods_per_cycle
        FROM subject_settings
        WHERE school_id = $1 AND min_periods_per_cycle IS NOT NULL
    """, school_id)
    for row in demanding:
        if row["min_periods_per_cycle"] > slots_per_cycle:
            result.fail(
                f"'{row['subject']}' must meet {row['min_periods_per_cycle']} times "
                f"per cycle but the layout has only {slots_per_cycle} teaching periods.")

    # A student's subjects together cannot need more periods than exist.
    overloaded = await db.fetch("""
        SELECT s.full_name, s.year_group, sum(
                   coalesce(ss.min_periods_per_cycle, $2)) AS needed
        FROM students s
        JOIN student_subjects sub ON sub.student_id = s.id
        LEFT JOIN subject_settings ss
               ON ss.school_id = s.school_id AND ss.subject = sub.subject
              AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id = $1
        GROUP BY s.id, s.full_name, s.year_group
        HAVING sum(coalesce(ss.min_periods_per_cycle, $2)) > $3
        LIMIT 5
    """, school_id,
        int(await db.fetchval(
            "SELECT value FROM settings WHERE key = 'default_min_periods_per_cycle'")
            or 4),
        slots_per_cycle)
    for row in overloaded:
        result.fail(
            f"{row['full_name']} ({row['year_group']}) needs {row['needed']} periods "
            f"a cycle for their subjects, but the cycle has only {slots_per_cycle}.")

    return result


# --- Helpers -----------------------------------------------------------------

def _clashes(assignments: Iterable[tuple[Any, Any]], label: str,
             result: CheckResult) -> None:
    """Flag any key used more than once."""
    seen: dict[Any, Any] = {}
    for key, who in assignments:
        if key in seen:
            result.fail(f"{label}: {who} and {seen[key]} both use {key}")
        else:
            seen[key] = who


# --- After Layer 2: class group formation ------------------------------------

async def check_class_groups(attempt_id: str) -> CheckResult:
    result = ok()

    dupes = await db.fetch("""
        SELECT cg.subject, cgs.student_id, count(*) AS n
        FROM class_group_students cgs
        JOIN class_groups cg ON cg.id = cgs.class_group_id
        WHERE cg.attempt_id = $1
        GROUP BY cg.subject, cgs.student_id
        HAVING count(*) > 1
    """, attempt_id)
    for row in dupes:
        result.fail(
            f"A student is in {row['n']} different {row['subject']} classes.")

    oversize = await db.fetch("""
        SELECT cg.class_code, cg.subject, cg.student_count, ss.hard_max_size
        FROM class_groups cg
        JOIN subject_settings ss
          ON ss.school_id = cg.school_id AND ss.subject = cg.subject
         AND ss.year_level IS NOT DISTINCT FROM cg.year_level
        WHERE cg.attempt_id = $1
          AND ss.hard_max_size IS NOT NULL
          AND cg.student_count > ss.hard_max_size
    """, attempt_id)
    for row in oversize:
        result.fail(
            f"{row['class_code']} has {row['student_count']} students, "
            f"over the hard maximum of {row['hard_max_size']}.")

    code_dupes = await db.fetch("""
        SELECT class_code, count(*) AS n FROM class_groups
        WHERE attempt_id = $1 GROUP BY class_code HAVING count(*) > 1
    """, attempt_id)
    for row in code_dupes:
        result.fail(f"Class code {row['class_code']} is used {row['n']} times.")

    empty = await db.fetch("""
        SELECT class_code FROM class_groups
        WHERE attempt_id = $1 AND student_count = 0
    """, attempt_id)
    for row in empty:
        result.warn(f"{row['class_code']} has no students.")

    return result


# --- After Layer 3: period assignment ----------------------------------------

async def check_period_assignment(attempt_id: str) -> CheckResult:
    """No student may be in two classes in the same period slot."""
    result = ok()

    clashes = await db.fetch("""
        SELECT cgs.student_id, cg2.period_slot, count(*) AS n
        FROM class_group_students cgs
        JOIN class_groups cg ON cg.id = cgs.class_group_id
        JOIN class_group_slots cg2 ON cg2.class_group_id = cg.id
        WHERE cg.attempt_id = $1
        GROUP BY cgs.student_id, cg2.period_slot
        HAVING count(*) > 1
    """, attempt_id) if await _has_slots_table() else []

    for row in clashes:
        result.fail(
            f"A student has {row['n']} classes in period {row['period_slot']}.")
    return result


async def _has_slots_table() -> bool:
    return bool(await db.fetchval("SELECT to_regclass('public.class_group_slots')"))


# --- After Layer 5: teacher assignment ---------------------------------------

async def check_teacher_assignment(attempt_id: str) -> CheckResult:
    result = ok()

    wrong_subject = await db.fetch("""
        SELECT cg.class_code, cg.subject, t.full_name
        FROM class_consistency_registry ccr
        JOIN class_groups cg ON cg.id = ccr.class_group_id
        JOIN teachers t ON t.id = ccr.teacher_id
        WHERE ccr.attempt_id = $1
          AND NOT (cg.subject = ANY(t.subjects))
    """, attempt_id)
    for row in wrong_subject:
        result.fail(
            f"{row['full_name']} is assigned {row['class_code']} but does not "
            f"teach {row['subject']}.")

    return result


# --- After Layer 6 and 8: duties ---------------------------------------------

async def check_duties(attempt_id: str) -> CheckResult:
    result = ok()

    exempt = await db.fetch("""
        SELECT t.full_name FROM allocation_registry_duties ard
        JOIN teacher_duty_exemptions e ON e.teacher_id = ard.teacher_id
        JOIN teachers t ON t.id = ard.teacher_id
        WHERE ard.attempt_id = $1
    """, attempt_id)
    for row in exempt:
        result.fail(f"{row['full_name']} is duty-exempt but was given a duty.")

    over = await db.fetch("""
        SELECT t.full_name, c.duties_assigned,
               coalesce(t.max_duties_per_cycle, 10) AS allowed
        FROM allocation_registry_duty_counts c
        JOIN teachers t ON t.id = c.teacher_id
        WHERE c.attempt_id = $1
          AND c.duties_assigned > coalesce(t.max_duties_per_cycle, 10)
    """, attempt_id)
    for row in over:
        result.fail(
            f"{row['full_name']} has {row['duties_assigned']} duties, "
            f"over their limit of {row['allowed']}.")

    # One teacher, two duties, overlapping times. The AI validators caught this
    # on a live run while every deterministic check passed - nothing here
    # compared one duty against another.
    clashing = await db.fetch("""
        SELECT t.full_name, a.day_number, a.duty_a, a.duty_b
        FROM (
            SELECT d1.teacher_id, s1.day_number,
                   dt1.name AS duty_a, dt2.name AS duty_b
            FROM allocation_registry_duties d1
            JOIN allocation_registry_duties d2
              ON d2.attempt_id = d1.attempt_id
             AND d2.teacher_id = d1.teacher_id
             AND d2.duty_slot_id > d1.duty_slot_id
            JOIN layout_duty_slots s1 ON s1.id = d1.duty_slot_id
            JOIN layout_duty_slots s2 ON s2.id = d2.duty_slot_id
            JOIN duty_types dt1 ON dt1.id = s1.duty_type_id
            JOIN duty_types dt2 ON dt2.id = s2.duty_type_id
            WHERE d1.attempt_id = $1
              AND s1.day_number = s2.day_number
              AND s1.start_time < s2.end_time
              AND s1.end_time > s2.start_time
        ) a
        JOIN teachers t ON t.id = a.teacher_id
    """, attempt_id)
    for row in clashing:
        result.fail(
            f"{row['full_name']} has two duties at the same time on day "
            f"{row['day_number']}: {row['duty_a']} and {row['duty_b']}.")

    # A duty during a period the teacher is teaching.
    teaching_clash = await db.fetch("""
        SELECT DISTINCT t.full_name, lds.day_number, dt.name AS duty
        FROM allocation_registry_duties ard
        JOIN layout_duty_slots lds ON lds.id = ard.duty_slot_id
        JOIN duty_types dt ON dt.id = lds.duty_type_id
        JOIN teachers t ON t.id = ard.teacher_id
        JOIN allocation_registry_teachers art
          ON art.attempt_id = ard.attempt_id
         AND art.teacher_id = ard.teacher_id
        JOIN layout_periods lp
          ON lp.id = art.layout_period_id
         AND lp.day_number = lds.day_number
         AND lp.start_time < lds.end_time
         AND lp.end_time > lds.start_time
        WHERE ard.attempt_id = $1
    """, attempt_id)
    for row in teaching_clash:
        result.fail(
            f"{row['full_name']} has {row['duty']} on day {row['day_number']} "
            "while teaching.")

    understaffed = await db.fetch("""
        SELECT lds.id, lds.day_number, lds.min_staff,
               count(ard.teacher_id) AS assigned
        FROM layout_duty_slots lds
        LEFT JOIN allocation_registry_duties ard
               ON ard.duty_slot_id = lds.id AND ard.attempt_id = $1
        WHERE lds.layout_id = $2
        GROUP BY lds.id, lds.day_number, lds.min_staff
        HAVING count(ard.teacher_id) < lds.min_staff
    """, attempt_id, await _layout_for(attempt_id))
    for row in understaffed:
        result.fail(
            f"A duty slot on day {row['day_number']} has {row['assigned']} "
            f"staff but needs {row['min_staff']}.")

    return result


async def _layout_for(attempt_id: str) -> Optional[str]:
    return await db.fetchval("""
        SELECT t.layout_id FROM generation_attempts ga
        JOIN timetables t ON t.id = ga.timetable_id
        WHERE ga.id = $1
    """, attempt_id)


# --- Final solution ----------------------------------------------------------

async def check_solution(version_id: str, conn: Any = None) -> CheckResult:
    """
    Every hard rule, re-run on the finished timetable.

    This is the check the quorum validator cannot override: if it fails, the
    solution is rejected regardless of what the AI validators say.

    `conn` runs the checks on an open transaction instead of the pool. Phase 11
    uses it to apply a proposed edit, check the result, and roll back if it
    fails - so an edit is judged by exactly the same rules as a generation, and
    cannot introduce a clash that generation would have rejected.
    """
    result = ok()
    x = conn or db

    entries = await x.fetch("""
        SELECT te.id, te.day_number, te.layout_period_id, te.teacher_id,
               te.room_id, te.class_code, te.subject,
               t.full_name AS teacher_name, r.name AS room_name,
               r.capacity, lp.period_number
        FROM timetable_entries te
        JOIN teachers t ON t.id = te.teacher_id
        JOIN rooms r ON r.id = te.room_id
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        WHERE te.version_id = $1
    """, version_id)

    if not entries:
        result.fail("The timetable has no entries.")
        return result

    # Teacher and room double-booking, per period slot.
    teacher_slots: list[tuple[Any, Any]] = []
    room_slots: list[tuple[Any, Any]] = []
    for e in entries:
        slot = (e["layout_period_id"],)
        teacher_slots.append(((e["teacher_id"], slot), e["class_code"]))
        room_slots.append(((e["room_id"], slot), e["class_code"]))

    _clashes(teacher_slots, "Teacher double-booked", result)
    _clashes(room_slots, "Room double-booked", result)

    # The denormalised day_number must agree with its period (DATABASE.md).
    drift = await x.fetchval("""
        SELECT count(*) FROM timetable_entries te
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        WHERE te.version_id = $1 AND te.day_number <> lp.day_number
    """, version_id)
    if drift:
        result.fail(
            f"{drift} entries have a day_number that disagrees with their period.")

    # Room capacity, counted per entry.
    #
    # Grouping by class_code instead would sum the same students once per
    # meeting: a class of 16 running four times a cycle reports 64 and fails
    # against any room. Capacity applies to one sitting, so the group is the
    # entry.
    over_capacity = await x.fetch("""
        SELECT te.class_code, lp.day_number, lp.period_number,
               r.name, r.capacity, count(tes.student_id) AS students
        FROM timetable_entries te
        JOIN rooms r ON r.id = te.room_id
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        LEFT JOIN timetable_entry_students tes ON tes.timetable_entry_id = te.id
        WHERE te.version_id = $1
        GROUP BY te.id, te.class_code, lp.day_number, lp.period_number,
                 r.name, r.capacity
        HAVING count(tes.student_id) > r.capacity
    """, version_id)
    for row in over_capacity:
        result.fail(
            f"{row['class_code']} has {row['students']} students in "
            f"{row['name']} (day {row['day_number']} period "
            f"{row['period_number']}), which holds {row['capacity']}.")

    # Student double-booking.
    student_clashes = await x.fetch("""
        SELECT tes.student_id, te.layout_period_id, count(*) AS n
        FROM timetable_entry_students tes
        JOIN timetable_entries te ON te.id = tes.timetable_entry_id
        WHERE te.version_id = $1
        GROUP BY tes.student_id, te.layout_period_id
        HAVING count(*) > 1
    """, version_id)
    for row in student_clashes:
        result.fail(
            f"A student has {row['n']} classes in the same period.")

    # Teachers only teach their own subjects.
    wrong = await x.fetch("""
        SELECT DISTINCT te.class_code, te.subject, t.full_name
        FROM timetable_entries te
        JOIN teachers t ON t.id = te.teacher_id
        WHERE te.version_id = $1 AND NOT (te.subject = ANY(t.subjects))
    """, version_id)
    for row in wrong:
        result.fail(
            f"{row['full_name']} teaches {row['class_code']} but "
            f"{row['subject']} is not one of their subjects.")

    # Teacher availability.
    unavailable = await x.fetch("""
        SELECT DISTINCT t.full_name, te.class_code
        FROM timetable_entries te
        JOIN teachers t ON t.id = te.teacher_id
        JOIN teacher_availability ta
          ON ta.teacher_id = te.teacher_id
         AND ta.layout_period_id = te.layout_period_id
        WHERE te.version_id = $1 AND ta.available = false
    """, version_id)
    for row in unavailable:
        result.fail(
            f"{row['full_name']} is marked unavailable but teaches "
            f"{row['class_code']} then.")

    log.info("Solution check for version %s: %s", version_id, result.summary)
    return result


async def validation_days(version_id: str) -> list[int]:
    """The days present in a version, so validation can be chunked over them."""
    rows = await db.fetch("""
        SELECT DISTINCT lp.day_number
        FROM timetable_entries te
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        WHERE te.version_id = $1
        ORDER BY lp.day_number
    """, version_id)
    return [r["day_number"] for r in rows]


async def global_summary(version_id: str) -> dict:
    """
    Cycle-wide aggregates, computed over every entry.

    Sent alongside each day chunk so a validator can judge fairness and
    consistency questions that only make sense across the whole cycle.
    """
    rows = await db.fetch("""
        SELECT t.full_name AS teacher, te.subject, te.class_code,
               (SELECT count(*) FROM timetable_entry_students tes
                WHERE tes.timetable_entry_id = te.id) AS students
        FROM timetable_entries te
        JOIN teachers t ON t.id = te.teacher_id
        WHERE te.version_id = $1
    """, version_id)

    loads: dict[str, int] = defaultdict(int)
    sizes: dict[str, list[int]] = defaultdict(list)
    seen_class: set[str] = set()
    for r in rows:
        loads[r["teacher"]] += 1
        if r["class_code"] not in seen_class:
            seen_class.add(r["class_code"])
            sizes[r["subject"]].append(r["students"])

    return {
        "total_entries": len(rows),
        "teacher_periods_per_cycle": dict(sorted(loads.items())),
        "class_sizes_by_subject": {
            subject: {"min": min(v), "max": max(v), "classes": len(v)}
            for subject, v in sorted(sizes.items())
        },
    }


async def summarise_day(version_id: str, day: int) -> dict:
    """
    Every entry for one day of the cycle.

    Validation is chunked by day rather than truncated. An earlier version sent
    the first 400 entries of the whole cycle with no indication it was a sample,
    so a validator would report "no teacher is double-booked" having seen a
    quarter of them - false confidence, which is worse than no validator.
    """
    # Times, not just period numbers. Without them a validator cannot tell
    # whether "yard duty at lunch" overlaps "period 5" and has to guess - and
    # several did exactly that, failing valid timetables with reasons that said
    # "typically overlaps" and "assuming". Anything they are asked to judge has
    # to be answerable from what they are shown.
    entries = await db.fetch("""
        SELECT te.class_code, te.subject, lp.period_number,
               to_char(lp.start_time, 'HH24:MI') AS starts,
               to_char(lp.end_time, 'HH24:MI') AS ends,
               t.full_name AS teacher, r.name AS room, c.name AS campus,
               (SELECT count(*) FROM timetable_entry_students tes
                WHERE tes.timetable_entry_id = te.id) AS students
        FROM timetable_entries te
        JOIN teachers t ON t.id = te.teacher_id
        JOIN rooms r ON r.id = te.room_id
        JOIN campuses c ON c.id = te.campus_id
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        WHERE te.version_id = $1 AND lp.day_number = $2
        ORDER BY lp.period_number, te.class_code
    """, version_id, day)

    duties = await db.fetch("""
        SELECT t.full_name AS teacher, dt.name AS duty, lds.timing,
               to_char(lds.start_time, 'HH24:MI') AS starts,
               to_char(lds.end_time, 'HH24:MI') AS ends
        FROM duty_assignments da
        JOIN layout_duty_slots lds ON lds.id = da.duty_slot_id
        JOIN duty_types dt ON dt.id = lds.duty_type_id
        JOIN teachers t ON t.id = da.teacher_id
        WHERE da.version_id = $1 AND lds.day_number = $2
    """, version_id, day)

    return {
        "day": day,
        "complete": True,  # every entry for this day, not a sample
        "entries": [dict(e) for e in entries],
        "duties": [dict(d) for d in duties],
    }

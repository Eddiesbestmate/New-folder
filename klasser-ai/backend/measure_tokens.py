"""
Measure the token volume one generation actually sends, per task and per model.

Runs the real pipeline against a school with a recording stub in place of the
provider, so every prompt is built exactly as it would be in production but
nothing is sent. Output tokens are estimated from the shape of the reply each
layer expects, since no model actually runs.

    python measure_tokens.py [--school "Westfield College"]

Counts are characters / 4, the usual rough token ratio for English + JSON.
Actual usage is recorded per call in pipeline_log.tokens_used once real
generations run - that is the number to trust.
"""

import argparse
import asyncio
import json
import logging
from collections import defaultdict

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from models import database as db
from services import ai_cluster, pipeline
from services import settings as settings_service

logging.getLogger("klasser.pipeline").setLevel(logging.ERROR)
logging.getLogger("klasser.ai").setLevel(logging.ERROR)

CHARS_PER_TOKEN = 4

calls: list[dict] = []


class Recorder:
    """Records each prompt, then returns an unusable answer so the pipeline
    falls back to its deterministic path and keeps going."""

    @staticmethod
    async def call(task_key, prompt, **kwargs):
        calls.append({
            "task": task_key,
            "stage": kwargs.get("stage", ""),
            "prompt_chars": len(prompt),
        })
        if task_key.startswith("validator_"):
            return json.dumps({"result": "pass", "reason": None,
                               "confidence": "high"})
        if "subject_map" in prompt:
            return json.dumps({"subject_map": []})
        if '"groups"' in prompt:
            return json.dumps({"groups": []})
        return json.dumps({"assignments": {}})


async def model_for(task_key: str) -> str:
    return await settings_service.get(task_key) or "?"


SUBJECTS = ["English", "Mathematics", "Science", "History", "Geography",
            "Computing", "Art", "Music", "PE", "Languages"]

YEARS = ["Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"]


async def build_synthetic(students: int, teachers: int, rooms: int,
                          campuses: int, days: int, periods: int,
                          duties: bool) -> str:
    """
    Create a throwaway school of a given shape.

    The sandbox is too small to show what Layer 2 costs: with 48 students every
    cohort fits one class, so the split decision - the most token-hungry call in
    the pipeline - never happens.

    Set-based inserts throughout: row-at-a-time over this connection meant
    thousands of round trips and minutes of wall time.
    """
    school_id = await db.fetchval("""
        INSERT INTO schools (name, timezone)
        VALUES ('Token Measurement School', 'Australia/Sydney') RETURNING id
    """)

    campus_ids = [
        await db.fetchval(
            "INSERT INTO campuses (school_id, name) VALUES ($1,$2) RETURNING id",
            school_id, f"Campus {chr(65 + i)}")
        for i in range(campuses)
    ]

    layout_id = await db.fetchval("""
        INSERT INTO timetable_layouts (school_id, name, days_in_cycle, is_active)
        VALUES ($1, $2, $3, true) RETURNING id
    """, school_id, f"Measurement {days}-day", days)

    await db.execute("""
        INSERT INTO layout_periods
            (layout_id, school_id, day_number, period_number, period_type,
             start_time, end_time, label)
        SELECT $1, $2, d, p, 'teaching',
               make_time(8 + p, 0, 0), make_time(8 + p, 50, 0), 'P' || p
        FROM generate_series(1, $3) AS d, generate_series(1, $4) AS p
    """, layout_id, school_id, days, periods)

    # Teachers spread across campuses, each covering two subjects. asyncpg uses
    # $n placeholders, so % is a literal modulo here - no doubling.
    await db.execute("""
        INSERT INTO teachers (school_id, first_name, surname, subjects,
                              campus_id, max_blocks, max_duties_per_cycle)
        SELECT $1, 'Teach' || i, 'Surname' || i,
               ARRAY[($2::text[])[1 + (i % $3)],
                     ($2::text[])[1 + ((i + 3) % $3)]],
               ($4::uuid[])[1 + (i % $5)], $6, 10
        FROM generate_series(0, $7 - 1) AS i
    """, school_id, SUBJECTS, len(SUBJECTS), campus_ids, campuses,
        periods, teachers)

    await db.execute("""
        INSERT INTO rooms (school_id, campus_id, name, capacity, room_type)
        SELECT $1, ($2::uuid[])[1 + (i % $3)], 'R' || i, 30, 'classroom'
        FROM generate_series(0, $4 - 1) AS i
    """, school_id, campus_ids, campuses, rooms)

    await db.execute("""
        INSERT INTO subject_settings
            (school_id, subject, hard_max_size, soft_max_size,
             min_periods_per_cycle, max_periods_per_cycle, code_prefix)
        SELECT $1, s, 30, 25, $2, $3, upper(left(s, 3))
        FROM unnest($4::text[]) AS s
    """, school_id, max(2, days // 2), max(3, days // 2 + 1), SUBJECTS)

    await db.execute("""
        INSERT INTO students (school_id, full_name, year_group, campus_id,
                              gender, ability_band)
        SELECT $1, 'Student ' || i || ' Surname' || i,
               ($2::text[])[1 + (i % $3)],
               ($4::uuid[])[1 + (i % $5)],
               CASE WHEN i % 2 = 0 THEN 'M' ELSE 'F' END,
               (ARRAY['high','mid','low'])[1 + (i % 3)]
        FROM generate_series(0, $6 - 1) AS i
    """, school_id, YEARS, len(YEARS), campus_ids, campuses, students)

    # Five subjects each, offset by row number so cohorts differ per year.
    await db.execute("""
        INSERT INTO student_subjects (student_id, school_id, subject)
        SELECT s.id, $1, ($2::text[])[1 + ((s.rn + k) % $3)]
        FROM (SELECT id, row_number() OVER (ORDER BY full_name) AS rn
              FROM students WHERE school_id = $1) s,
             generate_series(0, 4) AS k
        ON CONFLICT DO NOTHING
    """, school_id, SUBJECTS, len(SUBJECTS))

    if duties:
        yard = await db.fetchval("""
            INSERT INTO duty_types (school_id, name, min_staff, is_bus_duty)
            VALUES ($1, 'Yard duty', 2, false) RETURNING id
        """, school_id)
        bus = await db.fetchval("""
            INSERT INTO duty_types (school_id, name, min_staff, is_bus_duty)
            VALUES ($1, 'Bus supervision', 1, true) RETURNING id
        """, school_id)
        for duty_id, timing, staff in ((yard, "break", 2), (yard, "lunch", 2),
                                       (bus, "after_school", 1)):
            await db.execute("""
                INSERT INTO layout_duty_slots
                    (layout_id, school_id, duty_type_id, day_number, timing,
                     start_time, end_time, campus_id, min_staff)
                SELECT $1, $2, $3, d, $4, '11:00', '11:20', c, $5
                FROM generate_series(1, $6) AS d, unnest($7::uuid[]) AS c
            """, layout_id, school_id, duty_id, timing, staff, days, campus_ids)

    if campuses > 1:
        await db.execute("""
            INSERT INTO buses (school_id, name, capacity, home_campus_id)
            SELECT $1, 'Bus ' || i, 45, ($2::uuid[])[1]
            FROM generate_series(1, 3) AS i
        """, school_id, campus_ids)
        await db.execute("""
            INSERT INTO routes (school_id, from_campus_id, to_campus_id,
                                travel_minutes)
            SELECT $1, a, b, 12
            FROM unnest($2::uuid[]) a, unnest($2::uuid[]) b
            WHERE a <> b
        """, school_id, campus_ids)

    await db.execute("INSERT INTO school_credits (school_id) VALUES ($1)", school_id)
    await db.execute("INSERT INTO school_billing (school_id) VALUES ($1)", school_id)
    await db.execute("""
        INSERT INTO school_complexity
            (school_id, student_count, teacher_count, campus_count,
             transport_enabled, duties_enabled, cycle_days)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
    """, school_id, students, teachers, campuses, campuses > 1, duties, days)
    await db.execute("INSERT INTO onboarding (school_id) VALUES ($1)", school_id)

    return str(school_id)


async def drop_synthetic(school_id: str) -> None:
    async with db.transaction() as conn:
        # Children before parents: layout_duty_slots and duty_types both
        # reference the layout, and buses/routes reference campuses.
        for sql in (
            "DELETE FROM student_subjects WHERE school_id = $1",
            "DELETE FROM students WHERE school_id = $1",
            "DELETE FROM teacher_duty_exemptions WHERE school_id = $1",
            "DELETE FROM teachers WHERE school_id = $1",
            "DELETE FROM rooms WHERE school_id = $1",
            "DELETE FROM subject_settings WHERE school_id = $1",
            "DELETE FROM layout_duty_slots WHERE school_id = $1",
            "DELETE FROM duty_types WHERE school_id = $1",
            "DELETE FROM routes WHERE school_id = $1",
            "DELETE FROM buses WHERE school_id = $1",
            "DELETE FROM layout_periods WHERE school_id = $1",
            "DELETE FROM timetable_layouts WHERE school_id = $1",
            "DELETE FROM onboarding WHERE school_id = $1",
            "DELETE FROM school_complexity WHERE school_id = $1",
            "DELETE FROM school_billing WHERE school_id = $1",
            "DELETE FROM school_credits WHERE school_id = $1",
            "DELETE FROM campuses WHERE school_id = $1",
            "DELETE FROM schools WHERE id = $1",
        ):
            await conn.execute(sql, school_id)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--school", default="Westfield College")
    parser.add_argument("--synthetic", type=int,
                        help="Build a throwaway school of this many students")
    parser.add_argument("--teachers", type=int, default=120)
    parser.add_argument("--rooms", type=int, default=80)
    parser.add_argument("--campuses", type=int, default=2)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--periods", type=int, default=7)
    parser.add_argument("--no-duties", action="store_true")
    args = parser.parse_args()

    await db.connect()

    synthetic_id = None
    if args.synthetic:
        print(f"Building: {args.synthetic} students, {args.teachers} teachers, "
              f"{args.rooms} rooms, {args.campuses} campuses, "
              f"{args.days}-day cycle x {args.periods} periods"
              f"{'' if args.no_duties else ', duties'}...")
        synthetic_id = await build_synthetic(
            args.synthetic, args.teachers, args.rooms, args.campuses,
            args.days, args.periods, not args.no_duties)
        args.school = "Token Measurement School"
    original = ai_cluster.call
    ai_cluster.call = Recorder.call  # type: ignore[assignment]

    attempt_id = timetable_id = None
    try:
        school = await db.fetchrow(
            "SELECT id, name FROM schools WHERE name = $1", args.school)
        if school is None:
            print(f"No school named {args.school!r}")
            return

        counts = await db.fetchrow("""
            SELECT (SELECT count(*) FROM students WHERE school_id = $1) AS students,
                   (SELECT count(*) FROM teachers WHERE school_id = $1) AS teachers,
                   (SELECT count(*) FROM student_subjects
                     WHERE school_id = $1) AS enrolments
        """, school["id"])

        layout_id = await db.fetchval("""
            SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
            ORDER BY created_at DESC LIMIT 1
        """, school["id"])

        timetable_id = await db.fetchval("""
            INSERT INTO timetables (school_id, name, layout_id, status)
            VALUES ($1,'Token measurement',$2,'processing') RETURNING id
        """, school["id"], layout_id)
        attempt_id = await db.fetchval("""
            INSERT INTO generation_attempts (timetable_id, attempt_number, locked_model)
            VALUES ($1, 1, 'magistral') RETURNING id
        """, timetable_id)
        await db.execute("""
            INSERT INTO generation_jobs (attempt_id, status, notify_email)
            VALUES ($1,'queued','measure@example.com')
        """, attempt_id)

        await pipeline.run(str(attempt_id))

        # --- Report ---------------------------------------------------------
        by_task: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, "chars": 0})
        for c in calls:
            by_task[c["task"]]["calls"] += 1
            by_task[c["task"]]["chars"] += c["prompt_chars"]

        print(f"\nSchool: {school['name']}  "
              f"({counts['students']} students, {counts['teachers']} teachers, "
              f"{counts['enrolments']} enrolments)\n")

        print(f"{'TASK':<32} {'MODEL':<14} {'CALLS':>6} {'IN TOKENS':>11}")
        by_model: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, "tokens": 0})

        for task in sorted(by_task):
            model = await model_for(task)
            tokens = by_task[task]["chars"] // CHARS_PER_TOKEN
            n = by_task[task]["calls"]
            print(f"{task:<32} {model:<14} {n:>6} {tokens:>11,}")
            by_model[model]["calls"] += n
            by_model[model]["tokens"] += tokens

        print(f"\n{'MODEL':<14} {'CALLS':>6} {'IN TOKENS':>11}")
        for model in sorted(by_model):
            print(f"{model:<14} {by_model[model]['calls']:>6} "
                  f"{by_model[model]['tokens']:>11,}")

        total = sum(m["tokens"] for m in by_model.values())
        print(f"\nTotal input tokens for one generation: {total:,}")
        print("Output tokens are far smaller - every layer returns ids or index")
        print("lists, not prose. Expect roughly 5-10% of input.")
        print("\nReal per-call usage is logged to pipeline_log.tokens_used once")
        print("generations run against live providers.")

    finally:
        ai_cluster.call = original  # type: ignore[assignment]
        if attempt_id:
            for sql in (
                "DELETE FROM timetable_entry_students WHERE timetable_entry_id IN "
                "  (SELECT id FROM timetable_entries WHERE attempt_id = $1)",
                "DELETE FROM timetable_entries WHERE attempt_id = $1",
                "DELETE FROM duty_assignments WHERE attempt_id = $1",
                "DELETE FROM class_group_slots WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_students WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_teachers WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_rooms WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_duties WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_duty_counts WHERE attempt_id = $1",
                "DELETE FROM class_consistency_registry WHERE attempt_id = $1",
                "DELETE FROM class_group_students WHERE class_group_id IN "
                "  (SELECT id FROM class_groups WHERE attempt_id = $1)",
                "DELETE FROM class_groups WHERE attempt_id = $1",
                "DELETE FROM generation_events WHERE attempt_id = $1",
                "DELETE FROM pipeline_state WHERE attempt_id = $1",
                "DELETE FROM generation_jobs WHERE attempt_id = $1",
                "DELETE FROM pipeline_log WHERE attempt_id = $1",
                "DELETE FROM error_log WHERE attempt_id = $1",
                "DELETE FROM validation_results WHERE attempt_id = $1",
                "DELETE FROM partial_solutions WHERE attempt_id = $1",
            ):
                await db.execute(sql, attempt_id)
        if timetable_id:
            await db.execute(
                "DELETE FROM transport_schedule WHERE timetable_id = $1", timetable_id)
            await db.execute(
                "DELETE FROM timetable_versions WHERE timetable_id = $1", timetable_id)
            await db.execute(
                "DELETE FROM generation_attempts WHERE timetable_id = $1", timetable_id)
            await db.execute("DELETE FROM timetables WHERE id = $1", timetable_id)
        if synthetic_id:
            await drop_synthetic(synthetic_id)
        await db.disconnect()


asyncio.run(main())

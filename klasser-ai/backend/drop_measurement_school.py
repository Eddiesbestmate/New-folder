"""Remove any leftover 'Token Measurement School' from a failed measurement run."""

import asyncio

from models import database as db

# Children before parents, matching the FK graph.
ORDER = [
    "DELETE FROM timetable_entry_students WHERE timetable_entry_id IN "
    "  (SELECT te.id FROM timetable_entries te "
    "   JOIN timetables t ON t.id = te.timetable_id WHERE t.school_id = $1)",
    "DELETE FROM timetable_entries WHERE timetable_id IN "
    "  (SELECT id FROM timetables WHERE school_id = $1)",
    "DELETE FROM transport_schedule WHERE timetable_id IN "
    "  (SELECT id FROM timetables WHERE school_id = $1)",
    "DELETE FROM duty_assignments WHERE timetable_id IN "
    "  (SELECT id FROM timetables WHERE school_id = $1)",
    "DELETE FROM class_group_slots WHERE attempt_id IN "
    "  (SELECT ga.id FROM generation_attempts ga JOIN timetables t "
    "   ON t.id = ga.timetable_id WHERE t.school_id = $1)",
    "DELETE FROM class_group_students WHERE class_group_id IN "
    "  (SELECT id FROM class_groups WHERE school_id = $1)",
    "DELETE FROM class_consistency_registry WHERE class_group_id IN "
    "  (SELECT id FROM class_groups WHERE school_id = $1)",
    "DELETE FROM class_groups WHERE school_id = $1",
]

ATTEMPT_SCOPED = [
    "allocation_registry_students", "allocation_registry_teachers",
    "allocation_registry_rooms", "allocation_registry_buses",
    "allocation_registry_duties", "allocation_registry_duty_counts",
    "generation_events", "pipeline_state", "generation_jobs",
    "pipeline_log", "error_log", "partial_solutions",
    "validation_results",
]

SCHOOL_SCOPED = [
    "student_subjects", "students", "teacher_duty_exemptions",
    "teacher_availability", "teachers", "rooms", "subject_settings",
    "layout_duty_slots", "duty_types", "routes", "buses",
    "layout_periods", "onboarding", "school_complexity",
    "school_billing", "school_credits",
]


async def go() -> None:
    await db.connect()
    rows = await db.fetch(
        "SELECT id, name FROM schools WHERE name = 'Token Measurement School'")
    if not rows:
        print("No measurement schools to remove.")
        await db.disconnect()
        return

    for row in rows:
        school_id = row["id"]
        async with db.transaction() as conn:
            for sql in ORDER:
                await conn.execute(sql, school_id)
            for table in ATTEMPT_SCOPED:
                await conn.execute(f"""
                    DELETE FROM {table} WHERE attempt_id IN
                      (SELECT ga.id FROM generation_attempts ga
                       JOIN timetables t ON t.id = ga.timetable_id
                       WHERE t.school_id = $1)
                """, school_id)
            await conn.execute("""
                DELETE FROM timetable_versions WHERE timetable_id IN
                  (SELECT id FROM timetables WHERE school_id = $1)
            """, school_id)
            await conn.execute("""
                DELETE FROM generation_attempts WHERE timetable_id IN
                  (SELECT id FROM timetables WHERE school_id = $1)
            """, school_id)
            await conn.execute(
                "DELETE FROM timetables WHERE school_id = $1", school_id)
            for table in SCHOOL_SCOPED:
                await conn.execute(
                    f"DELETE FROM {table} WHERE school_id = $1", school_id)
            await conn.execute(
                "DELETE FROM timetable_layouts WHERE school_id = $1", school_id)
            await conn.execute(
                "DELETE FROM campuses WHERE school_id = $1", school_id)
            await conn.execute("DELETE FROM schools WHERE id = $1", school_id)
        print(f"Removed {row['name']} ({school_id})")

    await db.disconnect()


asyncio.run(go())

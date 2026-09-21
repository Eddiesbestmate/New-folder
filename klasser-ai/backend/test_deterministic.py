"""
Deliberate-failure tests for the deterministic checker.

Every rule in check_solution() is fed a timetable that breaks it, and must
report it. A checker that has only ever passed is untested: check_solution()
shipped with a capacity rule that counted students once per meeting instead of
once per sitting, and nothing caught it because nothing ever fed it a bad
timetable.

    python test_deterministic.py
"""

import asyncio
import logging
import sys
import uuid

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from models import database as db
from services import deterministic

logging.getLogger("klasser.deterministic").setLevel(logging.ERROR)

results: list[tuple[bool, str]] = []
timetable_id: str | None = None
version_id: str | None = None
attempt_id: str | None = None


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def fixture() -> dict:
    """A minimal, valid two-entry timetable on the sandbox school."""
    global timetable_id, version_id, attempt_id

    school_id = await db.fetchval(
        "SELECT value FROM settings WHERE key = 'sandbox_school_id'")
    layout_id = await db.fetchval("""
        SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
        ORDER BY created_at DESC LIMIT 1
    """, school_id)

    periods = await db.fetch("""
        SELECT id, day_number, period_number FROM layout_periods
        WHERE layout_id = $1 AND period_type = 'teaching'
        ORDER BY day_number, period_number LIMIT 2
    """, layout_id)
    # Two teachers who both teach Mathematics, so a swap stays legal.
    teachers = await db.fetch("""
        SELECT id, full_name, campus_id FROM teachers
        WHERE school_id = $1 AND 'Mathematics' = ANY(subjects) LIMIT 2
    """, school_id)
    rooms = await db.fetch("""
        SELECT id, name, capacity, campus_id FROM rooms
        WHERE school_id = $1 ORDER BY capacity DESC LIMIT 2
    """, school_id)
    students = await db.fetch(
        "SELECT id FROM students WHERE school_id = $1 LIMIT 3", school_id)

    timetable_id = str(await db.fetchval("""
        INSERT INTO timetables (school_id, name, layout_id, status)
        VALUES ($1, 'Deterministic check fixture', $2, 'complete') RETURNING id
    """, school_id, layout_id))
    attempt_id = str(await db.fetchval("""
        INSERT INTO generation_attempts (timetable_id, attempt_number)
        VALUES ($1, 1) RETURNING id
    """, timetable_id))
    version_id = str(await db.fetchval("""
        INSERT INTO timetable_versions
            (timetable_id, school_id, version_number, generation_attempt_id)
        VALUES ($1, $2, 1, $3) RETURNING id
    """, timetable_id, school_id, attempt_id))

    entries = []
    for i, period in enumerate(periods):
        entry_id = uuid.uuid4()
        await db.execute("""
            INSERT INTO timetable_entries
                (id, timetable_id, version_id, attempt_id, layout_period_id,
                 day_number, room_id, teacher_id, campus_id, subject, class_code)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'Mathematics',$10)
        """, entry_id, timetable_id, version_id, attempt_id, period["id"],
            period["day_number"], rooms[i]["id"], teachers[i]["id"],
            rooms[i]["campus_id"], f"TESTMAT{i + 1}")
        await db.execute("""
            INSERT INTO timetable_entry_students (timetable_entry_id, student_id)
            VALUES ($1, $2)
        """, entry_id, students[i]["id"])
        entries.append(entry_id)

    return {"entries": entries, "periods": [dict(p) for p in periods],
            "teachers": [dict(t) for t in teachers],
            "rooms": [dict(r) for r in rooms],
            "students": [dict(s) for s in students],
            "school_id": school_id}


async def expect_fail(label: str, needle: str) -> None:
    """Run the checker and require a failure mentioning needle."""
    result = await deterministic.check_solution(version_id)
    if result.passed:
        check(False, label, "checker passed a broken timetable")
        return
    hit = any(needle.lower() in f.lower() for f in result.failures)
    check(hit, label,
          "" if hit else f"caught something else: {result.failures[0][:70]}")


async def cleanup() -> None:
    if version_id:
        await db.execute("""
            DELETE FROM timetable_entry_students WHERE timetable_entry_id IN
              (SELECT id FROM timetable_entries WHERE version_id = $1)
        """, version_id)
        await db.execute(
            "DELETE FROM timetable_entries WHERE version_id = $1", version_id)
        await db.execute(
            "DELETE FROM timetable_versions WHERE id = $1", version_id)
    if attempt_id:
        await db.execute(
            "DELETE FROM validation_results WHERE attempt_id = $1", attempt_id)
        await db.execute(
            "DELETE FROM generation_attempts WHERE id = $1", attempt_id)
    if timetable_id:
        await db.execute("DELETE FROM timetables WHERE id = $1", timetable_id)


async def main_test() -> None:
    await db.connect()
    try:
        f = await fixture()
        a, b = f["entries"]

        # --- Baseline -------------------------------------------------------
        clean = await deterministic.check_solution(version_id)
        check(clean.passed, "a valid timetable passes",
              "; ".join(clean.failures[:2]))

        # --- Teacher double-booked -------------------------------------------
        await db.execute(
            "UPDATE timetable_entries SET teacher_id = $2 WHERE id = $1",
            b, f["teachers"][0]["id"])
        await db.execute("""
            UPDATE timetable_entries SET layout_period_id = $2, day_number = $3
            WHERE id = $1
        """, b, f["periods"][0]["id"], f["periods"][0]["day_number"])
        await expect_fail("teacher double-booking caught", "teacher double-booked")

        # --- Room double-booked ------------------------------------------------
        await db.execute(
            "UPDATE timetable_entries SET teacher_id = $2 WHERE id = $1",
            b, f["teachers"][1]["id"])
        await db.execute(
            "UPDATE timetable_entries SET room_id = $2 WHERE id = $1",
            b, f["rooms"][0]["id"])
        await expect_fail("room double-booking caught", "room double-booked")

        # --- Student in two classes at once --------------------------------------
        await db.execute(
            "UPDATE timetable_entries SET room_id = $2 WHERE id = $1",
            b, f["rooms"][1]["id"])
        await db.execute("""
            INSERT INTO timetable_entry_students (timetable_entry_id, student_id)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
        """, b, f["students"][0]["id"])
        await expect_fail("student clash caught", "same period")

        # Put entry b back where it started.
        await db.execute("""
            DELETE FROM timetable_entry_students
            WHERE timetable_entry_id = $1 AND student_id = $2
        """, b, f["students"][0]["id"])
        await db.execute("""
            UPDATE timetable_entries SET layout_period_id = $2, day_number = $3
            WHERE id = $1
        """, b, f["periods"][1]["id"], f["periods"][1]["day_number"])
        restored = await deterministic.check_solution(version_id)
        check(restored.passed, "timetable is valid again after undoing the clash",
              "; ".join(restored.failures[:2]))

        # --- day_number drift ------------------------------------------------------
        other_day = await db.fetchval("""
            SELECT day_number FROM layout_periods
            WHERE id = $1
        """, f["periods"][1]["id"])
        await db.execute(
            "UPDATE timetable_entries SET day_number = $2 WHERE id = $1",
            b, other_day + 5)
        await expect_fail("denormalised day_number drift caught", "day_number")
        await db.execute(
            "UPDATE timetable_entries SET day_number = $2 WHERE id = $1",
            b, other_day)

        # --- Teacher teaching a subject they do not hold ------------------------------
        await db.execute(
            "UPDATE timetable_entries SET subject = 'Underwater Basketry' WHERE id = $1",
            b)
        await expect_fail("unqualified teacher caught", "not one of their subjects")
        await db.execute(
            "UPDATE timetable_entries SET subject = 'Mathematics' WHERE id = $1", b)

        # --- Room over capacity for one sitting -----------------------------------------
        small = await db.fetchrow("""
            SELECT id, capacity FROM rooms WHERE school_id = $1
            ORDER BY capacity ASC LIMIT 1
        """, f["school_id"])
        await db.execute(
            "UPDATE timetable_entries SET room_id = $2 WHERE id = $1",
            b, small["id"])
        extra = await db.fetch("""
            SELECT id FROM students WHERE school_id = $1 LIMIT $2
        """, f["school_id"], small["capacity"] + 5)
        await db.executemany("""
            INSERT INTO timetable_entry_students (timetable_entry_id, student_id)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
        """, [(b, r["id"]) for r in extra])
        await expect_fail("over-capacity room caught", "which holds")

        # The same students across several meetings must NOT trip the rule.
        await db.execute("""
            DELETE FROM timetable_entry_students WHERE timetable_entry_id = $1
        """, b)
        await db.executemany("""
            INSERT INTO timetable_entry_students (timetable_entry_id, student_id)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
        """, [(b, r["id"]) for r in extra[:small["capacity"] - 1]])
        await db.execute(
            "UPDATE timetable_entries SET room_id = $2 WHERE id = $1",
            b, small["id"])

        third = uuid.uuid4()
        third_period = await db.fetchrow("""
            SELECT id, day_number FROM layout_periods
            WHERE layout_id = (SELECT layout_id FROM timetables WHERE id = $1)
              AND period_type = 'teaching'
            ORDER BY day_number DESC, period_number DESC LIMIT 1
        """, timetable_id)
        await db.execute("""
            INSERT INTO timetable_entries
                (id, timetable_id, version_id, attempt_id, layout_period_id,
                 day_number, room_id, teacher_id, campus_id, subject, class_code)
            SELECT $1, timetable_id, version_id, attempt_id, $2, $3, room_id,
                   teacher_id, campus_id, subject, class_code
            FROM timetable_entries WHERE id = $4
        """, third, third_period["id"], third_period["day_number"], b)
        await db.executemany("""
            INSERT INTO timetable_entry_students (timetable_entry_id, student_id)
            VALUES ($1, $2) ON CONFLICT DO NOTHING
        """, [(third, r["id"]) for r in extra[:small["capacity"] - 1]])

        repeated = await deterministic.check_solution(version_id)
        check(repeated.passed,
              "a class meeting twice does not double-count against capacity",
              "; ".join(repeated.failures[:2]))

        # --- Teacher marked unavailable -----------------------------------------------------
        teacher_b = await db.fetchval(
            "SELECT teacher_id FROM timetable_entries WHERE id = $1", b)
        period_b = await db.fetchval(
            "SELECT layout_period_id FROM timetable_entries WHERE id = $1", b)
        await db.execute("""
            INSERT INTO teacher_availability
                (teacher_id, school_id, layout_period_id, available)
            VALUES ($1, $2, $3, false)
            ON CONFLICT (teacher_id, layout_period_id)
            DO UPDATE SET available = false
        """, teacher_b, f["school_id"], period_b)
        await expect_fail("unavailable teacher caught", "unavailable")
        await db.execute("""
            DELETE FROM teacher_availability
            WHERE teacher_id = $1 AND layout_period_id = $2
        """, teacher_b, period_b)

        # --- Empty timetable ------------------------------------------------------------------
        await db.execute("""
            DELETE FROM timetable_entry_students WHERE timetable_entry_id IN
              (SELECT id FROM timetable_entries WHERE version_id = $1)
        """, version_id)
        await db.execute(
            "DELETE FROM timetable_entries WHERE version_id = $1", version_id)
        await expect_fail("empty timetable caught", "no entries")

    finally:
        await cleanup()
        left = await db.fetchval("""
            SELECT count(*) FROM timetables WHERE name = 'Deterministic check fixture'
        """)
        check(left == 0, "fixture cleaned up", f"{left} left")
        await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nDeterministic checker - deliberate failures\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

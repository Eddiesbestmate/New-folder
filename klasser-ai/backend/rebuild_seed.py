"""
Rebuild the test school into something that looks like a real one.

Three problems with the old seed:
  * 183 teachers on a median load of 5 periods a fortnight. A 700-student
    school runs on about 70.
  * Years 7-10 carried 30 of 70 periods, so juniors had 40 free periods. Real
    junior students have none.
  * Every class was a single, because nothing asked for doubles.

Run with --apply. Without it, nothing is written and the plan is printed.
"""

import asyncio
import random
import sys
from collections import Counter

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
APPLY = "--apply" in sys.argv

random.seed(20260922)

KEEP_TEACHERS = 70
MAX_BLOCKS = 40

# Years 7-10 are full: the periods below add up to the whole 70-slot cycle.
# Years 11-12 stop short on purpose - senior frees are real, not a gap.
CURRICULUM = {
    "Year 7": {
        "core": {"English": 8, "Mathematics": 8, "Science": 8, "History": 6,
                 "Geography": 6, "Physical Education": 6, "Art": 5,
                 "Music": 5, "Design Technology": 5, "Computing": 5},
        "lines": [({"French", "Spanish", "German"}, 8)],
    },
    "Year 9": {
        "core": {"English": 8, "Mathematics": 8, "Science": 8,
                 "Physical Education": 6, "History": 6, "Geography": 6},
        "lines": [({"Art", "Music", "French"}, 7),
                  ({"Computing", "Design Technology", "Spanish"}, 7),
                  ({"Business Studies", "German", "Sociology"}, 7),
                  ({"Economics", "Biology", "Chemistry"}, 7)],
    },
    "Year 11": {
        "core": {"English": 8},
        "lines": [({"Mathematics", "Art", "Sociology"}, 8),
                  ({"Physics", "Business Studies", "Geography"}, 8),
                  ({"Chemistry", "Economics", "History"}, 8),
                  ({"Biology", "Computing", "Design Technology"}, 8)],
    },
}
CURRICULUM["Year 8"] = CURRICULUM["Year 7"]
CURRICULUM["Year 10"] = CURRICULUM["Year 9"]
CURRICULUM["Year 12"] = CURRICULUM["Year 11"]

# Which subjects are worth running as doubles. Practical subjects benefit from
# a long block; a language does not.
DOUBLE_SUBJECTS = {"Science", "Physics", "Chemistry", "Biology", "Art",
                   "Design Technology", "Computing", "Physical Education"}

ROOM_FOR = {
    "Science": "lab", "Physics": "lab", "Chemistry": "lab", "Biology": "lab",
    "Music": "music", "Art": "art", "Physical Education": "gym",
    "Computing": "lab", "Design Technology": "lab",
}


def pick(year: str) -> dict[str, int]:
    """One student's subjects and how often each meets."""
    plan = CURRICULUM[year]
    chosen = dict(plan["core"])
    for options, periods in plan["lines"]:
        chosen[random.choice(sorted(options))] = periods
    return chosen


def cycle_load(year: str) -> int:
    plan = CURRICULUM[year]
    return sum(plan["core"].values()) + sum(p for _, p in plan["lines"])


def periods_for(subject: str, year: str) -> int:
    plan = CURRICULUM.get(year)
    if not plan:
        return 5
    if subject in plan["core"]:
        return plan["core"][subject]
    for options, periods in plan["lines"]:
        if subject in options:
            return periods
    return 5


async def main() -> None:
    await db.connect()

    print("Periods per student per 70-slot cycle:")
    for year in ["Year 7", "Year 9", "Year 11"]:
        load = cycle_load(year)
        print(f"  {year:9} {load}  {'FULL' if load >= 70 else f'{70 - load} free'}")

    teachers = [dict(r) for r in await db.fetch(
        "SELECT id, full_name FROM teachers WHERE school_id = $1 "
        "ORDER BY created_at", SCHOOL)]
    students = [dict(r) for r in await db.fetch(
        "SELECT id, year_group FROM students WHERE school_id = $1", SCHOOL)]
    print(f"\n{len(teachers)} teachers, {len(students)} students")
    print(f"teachers to remove: {max(0, len(teachers) - KEEP_TEACHERS)}")

    # What the new curriculum will demand, so the roster can be sized against
    # it rather than guessed at.
    demand: Counter = Counter()
    for student in students:
        for subject, periods in pick(student["year_group"]).items():
            demand[(student["year_group"], subject)] += 1

    streams = 0
    class_periods = 0
    for (year, subject), heads in demand.items():
        n = -(-heads // 25)
        streams += n
        class_periods += n * periods_for(subject, year)
    print(f"projected: {streams} classes, {class_periods} class-periods")
    print(f"capacity:  {KEEP_TEACHERS} teachers x {MAX_BLOCKS} = "
          f"{KEEP_TEACHERS * MAX_BLOCKS}")
    if class_periods > KEEP_TEACHERS * MAX_BLOCKS:
        print("  *** NOT ENOUGH TEACHERS - raise KEEP_TEACHERS ***")

    if not APPLY:
        print("\nDry run. Re-run with --apply to write.")
        await db.disconnect()
        return

    # --- Clear the old run so teachers stop being referenced ---------------
    #
    # Only what actually points at a teacher, in child-before-parent order.
    # A full teardown of `timetables` would drag credit_transactions,
    # invoices and outstanding_charges with it, and deleting a school's
    # billing history to tidy up test data is not a trade worth making - the
    # timetable rows stay as history, emptied of their allocations.
    async with db.transaction() as conn:
        tts = [r["id"] for r in await conn.fetch(
            "SELECT id FROM timetables WHERE school_id = $1", SCHOOL)]
        for tt in tts:
            await conn.execute("""
                DELETE FROM timetable_entry_students WHERE timetable_entry_id IN
                (SELECT id FROM timetable_entries WHERE timetable_id = $1)
            """, tt)
            await conn.execute(
                "DELETE FROM timetable_entries WHERE timetable_id = $1", tt)

        attempts = [r["id"] for r in await conn.fetch("""
            SELECT ga.id FROM generation_attempts ga
            JOIN timetables t ON t.id = ga.timetable_id
            WHERE t.school_id = $1
        """, SCHOOL)]
        if attempts:
            for table in ("allocation_registry_teachers",
                          "allocation_registry_duties",
                          "allocation_registry_duty_counts",
                          "class_consistency_registry"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE attempt_id = ANY($1)",  # noqa: S608
                    attempts)
        await conn.execute(
            "DELETE FROM teacher_duty_exemptions WHERE school_id = $1", SCHOOL)

        # A timetable with no entries is not a timetable any more; say so
        # rather than leaving it advertising itself as complete.
        await conn.execute("""
            UPDATE timetables SET status = 'cancelled'
            WHERE school_id = $1 AND status <> 'cancelled'
        """, SCHOOL)
        print(f"emptied {len(tts)} timetable(s), cleared "
              f"{len(attempts)} attempt registr(ies)")

    # --- Trim the roster ----------------------------------------------------
    doomed = [t["id"] for t in teachers[KEEP_TEACHERS:]]
    if doomed:
        async with db.transaction() as conn:
            await conn.execute(
                "DELETE FROM teacher_duty_exemptions WHERE teacher_id = ANY($1)",
                doomed)
            await conn.execute(
                "DELETE FROM teacher_availability WHERE teacher_id = ANY($1)",
                doomed)
            await conn.execute("DELETE FROM teachers WHERE id = ANY($1)", doomed)
        print(f"removed {len(doomed)} teachers")

    kept = teachers[:KEEP_TEACHERS]

    # --- Staff against demand, not at random -------------------------------
    taught = sorted({subject for _, subject in demand})
    assignments: list[set[str]] = [set() for _ in kept]
    weighted: list[str] = []
    for (year, subject), heads in demand.items():
        weighted += [subject] * max(1, heads // 20)

    for i, subject in enumerate(taught):          # everyone covered at least twice
        for k in range(2):
            assignments[(i * 2 + k) % len(kept)].add(subject)
    for spec in assignments:                      # then fill toward demand
        while len(spec) < 3:
            spec.add(random.choice(weighted))

    for teacher, spec in zip(kept, assignments):
        await db.execute("""
            UPDATE teachers SET subjects = $2, max_blocks = $3,
                                max_duties_per_cycle = 6
            WHERE id = $1
        """, teacher["id"], sorted(spec), MAX_BLOCKS)
    print(f"restaffed {len(kept)} teachers across {len(taught)} subjects")

    # --- Enrolments ---------------------------------------------------------
    for n, student in enumerate(students, start=1):
        subjects = sorted(pick(student["year_group"]))
        async with db.transaction() as conn:
            await conn.execute(
                "DELETE FROM student_subjects WHERE student_id = $1",
                student["id"])
            await conn.executemany("""
                INSERT INTO student_subjects (student_id, school_id, subject)
                VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
            """, [(student["id"], SCHOOL, s) for s in subjects])
        if n % 200 == 0:
            print(f"  enrolled {n}/{len(students)}")

    # --- Subject settings ---------------------------------------------------
    rows = await db.fetch(
        "SELECT id, subject, year_level FROM subject_settings WHERE school_id = $1",
        SCHOOL)
    for row in rows:
        year = row["year_level"] or "Year 7"
        periods = periods_for(row["subject"], year)
        await db.execute("""
            UPDATE subject_settings
            SET min_periods_per_cycle = $2, max_periods_per_cycle = $2,
                soft_max_size = 25, hard_max_size = 30, min_size = 5,
                default_room_type = $3, is_double_period = $4
            WHERE id = $1
        """, row["id"], periods, ROOM_FOR.get(row["subject"], "classroom"),
            row["subject"] in DOUBLE_SUBJECTS)
    print(f"updated {len(rows)} subject settings")

    print("\nDone.")
    await db.disconnect()


asyncio.run(main())

"""
Years 10-12: elective lines, with students carrying 5 to 8 subjects.

Everything on a line runs at the same time and a student takes at most one
subject from each, so line-mates never share a student and can share slots.
A student carrying fewer subjects simply sits out some lines - which is what
a senior free period actually is. Drawn from one open pool instead, students
pick arbitrary combinations, every elective ends up sharing students with
every other, and two dozen classes all need different slots in a 66-slot
cycle, which cannot be solved at any ordering.
"""
import asyncio
from collections import Counter, defaultdict

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
PERIODS = 8          # every senior subject, so 8 subjects = 64 of 66

PLAN = {
    "Year 10": {
        "compulsory": ["English", "Mathematics", "Science"],
        "lines": [["Humanities", "Economics", "Business Studies"],
                  ["Physical Education", "Art", "Drama"],
                  ["Biology", "Computing", "Sociology"],
                  ["Chemistry", "Music", "German"],
                  ["French", "Spanish", "Design Technology"]],
    },
    "Year 11": {
        "compulsory": ["English"],
        "lines": [["Mathematics", "Art"], ["Biology", "History"],
                  ["Chemistry", "Business Studies"], ["Physics", "Economics"],
                  ["Computing", "Sociology"], ["Geography", "Music"],
                  ["Design Technology", "Drama"]],
    },
    "Year 12": {
        "compulsory": ["English"],
        "lines": [["Mathematics", "Art"], ["Biology", "History"],
                  ["Chemistry", "Business Studies"], ["Physics", "Economics"],
                  ["Computing", "Sociology"], ["Geography", "Music"],
                  ["Design Technology", "Drama"]],
    },
}

ROOM_TYPE = {
    "Science": "lab", "Biology": "lab", "Chemistry": "lab", "Physics": "lab",
    "Art": "art", "Music": "music", "Drama": "music",
    "Design Technology": "workshop", "Computing": "computer_lab",
    "Digital Technologies": "computer_lab", "Physical Education": "gym",
}


async def main() -> None:
    await db.connect()

    await db.execute("""
        UPDATE teachers SET subjects = (
            SELECT array_agg(DISTINCT x)
            FROM unnest(subjects || ARRAY['Humanities']) AS x)
        WHERE school_id = $1 AND (subjects && ARRAY['History','Geography'])
          AND NOT (subjects && ARRAY['Humanities'])""", SCHOOL)

    for year, spec in PLAN.items():
        lines = spec["lines"]
        subjects = set(spec["compulsory"])
        for line in lines:
            subjects.update(line)

        await db.execute("""DELETE FROM subject_settings
                            WHERE school_id=$1 AND year_level=$2""",
                         SCHOOL, year)
        for subject in sorted(subjects):
            await db.execute("""
                INSERT INTO subject_settings
                    (school_id, subject, year_level, hard_max_size,
                     soft_max_size, min_size, default_room_type,
                     min_periods_per_cycle, max_periods_per_cycle)
                VALUES ($1,$2,$3,30,25,5,$4,$5,$5)""",
                SCHOOL, subject, year,
                ROOM_TYPE.get(subject, "classroom"), PERIODS)

        await db.execute("""
            DELETE FROM student_subjects
            WHERE student_id IN (SELECT id FROM students
                                 WHERE school_id=$1 AND year_group=$2)""",
                         SCHOOL, year)

        students = await db.fetch("""
            SELECT id FROM students WHERE school_id=$1 AND year_group=$2
            ORDER BY id""", SCHOOL, year)

        fixed = len(spec["compulsory"])
        lo = max(0, 5 - fixed)                 # lines needed for 5 subjects
        hi = min(len(lines), 8 - fixed)        # lines allowed before 8
        rows, counts = [], Counter()
        for n, s in enumerate(students):
            take = list(spec["compulsory"])
            # Spread the load evenly across 5..8 subjects rather than giving
            # every student the same number.
            want = lo + (n % (hi - lo + 1))
            chosen = [(n + i) % len(lines) for i in range(want)]
            for li in sorted(chosen):
                line = lines[li]
                take.append(line[(n + li) % len(line)])
            counts[len(take)] += 1
            for subject in take:
                rows.append((str(s["id"]), SCHOOL, subject))

        await db.executemany(
            "INSERT INTO student_subjects (student_id, school_id, subject)"
            " VALUES ($1,$2,$3)", rows)
        print(f"{year}: {len(students)} students, {len(subjects)} subjects, "
              f"subjects per student {dict(sorted(counts.items()))}")

    print()
    for year in ("Year 7", "Year 8", "Year 9",
                 "Year 10", "Year 11", "Year 12"):
        r = await db.fetch("""
            SELECT s.id, coalesce(ss.max_periods_per_cycle,0) p
            FROM students s JOIN student_subjects sub ON sub.student_id=s.id
            LEFT JOIN subject_settings ss ON ss.school_id=s.school_id
                 AND ss.subject=sub.subject AND ss.year_level=s.year_group
            WHERE s.school_id=$1 AND s.year_group=$2""", SCHOOL, year)
        tot = defaultdict(int)
        for x in r:
            tot[x["id"]] += x["p"]
        v = sorted(tot.values())
        print(f"  {year:8} n={len(v):>3} periods {v[0]}-{v[-1]} of 66")

    await db.disconnect()

asyncio.run(main())

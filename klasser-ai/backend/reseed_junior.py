"""
Reshape Years 7-9 to the school's stated curriculum.

Y7/8: English, Maths, Science, Humanities, a Language, PE, Digital
      Technologies, (Art or Design Technology), (Music or Drama)
Y9:   English, Maths, Science, Humanities, a Language, PE, two electives

Humanities replaces History and Geography for these years, and Digital
Technologies replaces Computing. Choice blocks are split evenly by a fixed
order so the halves are balanced and the run is reproducible.
"""
import asyncio
from collections import Counter, defaultdict

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"

LANGUAGES = ["French", "German", "Spanish"]
CREATIVE = ["Art", "Design Technology"]          # Y7/8 choice block
PERFORMING = ["Music", "Drama"]                  # Y7/8 choice block
# Elective LINES, not a pool. Everything on a line runs at the same time and
# a student takes one subject from each, so line-mates never share a student
# and can share slots. Drawn from one pool instead, students pick arbitrary
# pairs, every elective ends up sharing students with every other, and the ten
# classes need ten separate blocks - 80 periods into a 66-period cycle, which
# is simply infeasible.
Y9_LINE_A = ["Art", "Music", "Drama", "Design Technology",
             "Digital Technologies"]
Y9_LINE_B = ["Business Studies", "Economics", "Sociology", "Biology",
             "Chemistry"]
Y9_ELECTIVES = Y9_LINE_A + Y9_LINE_B

PERIODS = {
    "Year 7": {"English": 8, "Mathematics": 8, "Science": 8, "Humanities": 7,
               "Physical Education": 7, "Digital Technologies": 7,
               **{s: 7 for s in LANGUAGES + CREATIVE + PERFORMING}},
    "Year 8": {"English": 8, "Mathematics": 8, "Science": 8, "Humanities": 7,
               "Physical Education": 7, "Digital Technologies": 7,
               **{s: 7 for s in LANGUAGES + CREATIVE + PERFORMING}},
    "Year 9": {"English": 9, "Mathematics": 9, "Science": 9, "Humanities": 8,
               "Physical Education": 7,
               **{s: 8 for s in LANGUAGES}, **{s: 8 for s in Y9_ELECTIVES}},
}

ROOM_TYPE = {
    "Humanities": "classroom", "Digital Technologies": "computer_lab",
    "Drama": "music", "Art": "art", "Design Technology": "workshop",
    "Music": "music", "Physical Education": "gym", "Science": "lab",
    "Biology": "lab", "Chemistry": "lab",
}

# Who can teach the new subjects. Humanities is the History/Geography staff;
# Digital Technologies is the Computing staff; Drama is taught by the
# performing-arts and English staff, as it usually is.
NEW_FROM = {
    "Humanities": {"History", "Geography"},
    "Digital Technologies": {"Computing"},
    "Drama": {"Music", "English"},
}


async def main() -> None:
    await db.connect()

    # --- teachers: qualify staff for the new subjects ---------------------
    teachers = await db.fetch(
        "SELECT id, subjects FROM teachers WHERE school_id = $1", SCHOOL)
    added = Counter()
    for t in teachers:
        have = set(t["subjects"] or [])
        new = set(have)
        for subject, sources in NEW_FROM.items():
            if have & sources:
                new.add(subject)
        if new != have:
            await db.execute(
                "UPDATE teachers SET subjects = $2 WHERE id = $1",
                t["id"], sorted(new))
            for s in new - have:
                added[s] += 1
    print("teachers qualified:",
          ", ".join(f"{k} +{v}" for k, v in sorted(added.items())))

    # --- subject settings -------------------------------------------------
    for year, plan in PERIODS.items():
        await db.execute("""DELETE FROM subject_settings
                            WHERE school_id = $1 AND year_level = $2""",
                         SCHOOL, year)
        for subject, periods in plan.items():
            await db.execute("""
                INSERT INTO subject_settings
                    (school_id, subject, year_level, hard_max_size,
                     soft_max_size, min_size, default_room_type,
                     min_periods_per_cycle, max_periods_per_cycle)
                VALUES ($1,$2,$3,30,25,5,$4,$5,$5)
            """, SCHOOL, subject, year,
                 ROOM_TYPE.get(subject, "classroom"), periods)
    print(f"subject settings rewritten for {len(PERIODS)} year levels")

    # --- enrolments -------------------------------------------------------
    await db.execute("""
        DELETE FROM student_subjects
        WHERE student_id IN (SELECT id FROM students
                             WHERE school_id = $1 AND year_group = ANY($2::text[]))
    """, SCHOOL, list(PERIODS))

    rows = []
    for year in PERIODS:
        students = await db.fetch("""
            SELECT id FROM students WHERE school_id = $1 AND year_group = $2
            ORDER BY id""", SCHOOL, year)
        for n, s in enumerate(students):
            take = ["English", "Mathematics", "Science", "Humanities",
                    "Physical Education", LANGUAGES[n % len(LANGUAGES)]]
            if year == "Year 9":
                # One subject from each line, so the two choices can never
                # collide and each line fills evenly.
                take += [Y9_LINE_A[n % len(Y9_LINE_A)],
                         Y9_LINE_B[n % len(Y9_LINE_B)]]
            else:
                take += ["Digital Technologies",
                         CREATIVE[n % len(CREATIVE)],
                         PERFORMING[n % len(PERFORMING)]]
            for subject in take:
                rows.append((str(s["id"]), SCHOOL, subject))

    await db.executemany(
        "INSERT INTO student_subjects (student_id, school_id, subject)"
        " VALUES ($1, $2, $3)", rows)
    print(f"{len(rows)} enrolments written")

    # --- verify every student lands on exactly 66 -------------------------
    print()
    for year in PERIODS:
        r = await db.fetch("""
            SELECT s.id, coalesce(ss.max_periods_per_cycle, 0) p
            FROM students s JOIN student_subjects sub ON sub.student_id = s.id
            LEFT JOIN subject_settings ss ON ss.school_id = s.school_id
                 AND ss.subject = sub.subject AND ss.year_level = s.year_group
            WHERE s.school_id = $1 AND s.year_group = $2""", SCHOOL, year)
        tot = defaultdict(int)
        for x in r:
            tot[x["id"]] += x["p"]
        v = sorted(tot.values())
        print(f"  {year:8} n={len(v):>3} min={v[0]} max={v[-1]} "
              f"exactly-66={sum(1 for x in v if x == 66)}")

    print()
    for year in PERIODS:
        r = await db.fetch("""
            SELECT sub.subject, count(*) n FROM student_subjects sub
            JOIN students s ON s.id = sub.student_id
            WHERE s.school_id = $1 AND s.year_group = $2
            GROUP BY 1 ORDER BY 2 DESC""", SCHOOL, year)
        print(f"  {year}: " + ", ".join(f"{x['subject']} {x['n']}" for x in r))

    await db.disconnect()

asyncio.run(main())

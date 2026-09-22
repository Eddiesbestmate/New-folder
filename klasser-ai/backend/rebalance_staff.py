"""Spread teacher expertise in proportion to how many classes each subject runs.

The first staffing pass weighted by raw enrolments, which left Geography with
25 teachers for 27 classes and Mathematics with 11 for the same number. Layer 3
now refuses to run more concurrent classes of a subject than it has teachers,
so a thin subject throttles the whole cohort.
"""
import asyncio
import random
from collections import defaultdict

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT_MAX = 25
PER_TEACHER = 4          # subjects each teacher can cover
FLOOR = 3                # nobody below this, however small the subject

random.seed(20260922)


async def main():
    await db.connect()
    classes = defaultdict(int)
    for r in await db.fetch("""
        SELECT sub.subject, s.year_group, coalesce(c.name,'-') AS campus,
               count(*) AS heads
        FROM student_subjects sub
        JOIN students s ON s.id = sub.student_id
        LEFT JOIN campuses c ON c.id = s.campus_id
        WHERE s.school_id = $1 GROUP BY 1,2,3
    """, SCHOOL):
        classes[r["subject"]] += max(1, -(-r["heads"] // SOFT_MAX))

    teachers = [r["id"] for r in await db.fetch(
        "SELECT id FROM teachers WHERE school_id=$1 ORDER BY created_at", SCHOOL)]
    budget = len(teachers) * PER_TEACHER
    total = sum(classes.values())

    target = {s: max(FLOOR, round(budget * n / total)) for s, n in classes.items()}
    # Trim proportionally if the floors pushed us over budget.
    while sum(target.values()) > budget:
        biggest = max(target, key=lambda s: target[s] - FLOOR)
        if target[biggest] <= FLOOR:
            break
        target[biggest] -= 1

    slots = []
    for subject, n in target.items():
        slots += [subject] * n
    random.shuffle(slots)

    spec = defaultdict(set)
    i = 0
    for subject in slots:
        for _ in range(len(teachers)):
            who = teachers[i % len(teachers)]
            i += 1
            if subject not in spec[who] and len(spec[who]) < PER_TEACHER:
                spec[who].add(subject)
                break

    for who in teachers:
        await db.execute("UPDATE teachers SET subjects=$2 WHERE id=$1",
                         who, sorted(spec[who]))

    print(f"{'subject':22} {'classes':>8} {'teachers':>9}")
    got = defaultdict(int)
    for who in teachers:
        for s in spec[who]:
            got[s] += 1
    tight = 0
    for s in sorted(classes, key=lambda x: -classes[x]):
        mark = "" if got[s] >= classes[s] / 3 else "  thin"
        if mark:
            tight += 1
        print(f"{s:22} {classes[s]:>8} {got[s]:>9}{mark}")
    print(f"\n{sum(len(v) for v in spec.values())} assignments over "
          f"{len(teachers)} teachers; {tight} subject(s) still thin")
    await db.disconnect()


asyncio.run(main())

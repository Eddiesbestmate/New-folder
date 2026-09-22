"""Raise the roster to 100 and re-deal subjects by class count."""
import asyncio, random
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
TARGET = 100
PER_TEACHER = 4
SOFT_MAX = 25
random.seed(20260922)

FIRST = ["James","Mary","Robert","Patricia","Michael","Jennifer","William","Linda",
         "David","Barbara","Richard","Elizabeth","Joseph","Susan","Thomas","Jessica",
         "Charles","Sarah","Daniel","Nancy","Matthew","Lisa","Anthony","Betty",
         "Mark","Margaret","Paul","Sandra","Steven","Ashley","Kevin","Emily"]
LAST = ["Nguyen","Tran","Okafor","Kaur","Singh","Rossi","Kowalski","Silva","Haddad",
        "Novak","Petrov","Andersen","Bergman","Fontaine","Moreau","Costa","Dimitriou",
        "Yilmaz","Fernandez","Ivanov","Larsen","Muller","Schneider","Weber","Hoffmann",
        "Bauer","Vogel","Keller","Brandt","Sommer","Winter","Frost"]


async def main():
    await db.connect()
    existing = {r["full_name"] for r in await db.fetch(
        "SELECT full_name FROM teachers WHERE school_id=$1", SCHOOL)}
    campuses = [r["id"] for r in await db.fetch(
        "SELECT id FROM campuses WHERE school_id=$1", SCHOOL)]
    have = len(existing)
    print(f"have {have} teachers, target {TARGET}")

    made = 0
    while have + made < TARGET:
        name = f"{random.choice(FIRST)} {random.choice(LAST)}"
        if name in existing:
            continue
        existing.add(name)
        first, last = name.split(" ", 1)
        await db.execute("""
            INSERT INTO teachers (school_id, first_name, surname, subjects,
                                  campus_id, max_blocks, max_duties_per_cycle)
            VALUES ($1,$2,$3,'{}',$4,40,6)
        """, SCHOOL, first, last, campuses[made % len(campuses)])
        made += 1
    print(f"added {made}")

    # re-deal every teacher's subjects against class counts
    classes = defaultdict(int)
    for r in await db.fetch("""
        SELECT sub.subject, s.year_group, coalesce(c.name,'-') campus, count(*) heads
        FROM student_subjects sub JOIN students s ON s.id=sub.student_id
        LEFT JOIN campuses c ON c.id=s.campus_id
        WHERE s.school_id=$1 GROUP BY 1,2,3""", SCHOOL):
        classes[r["subject"]] += max(1, -(-r["heads"]//SOFT_MAX))

    teachers = [r["id"] for r in await db.fetch(
        "SELECT id FROM teachers WHERE school_id=$1 ORDER BY created_at", SCHOOL)]
    budget = len(teachers) * PER_TEACHER
    total = sum(classes.values())
    target = {s: max(3, round(budget*n/total)) for s, n in classes.items()}
    pool = [s for s, n in target.items() for _ in range(n)]
    random.shuffle(pool)

    spec = defaultdict(set); i = 0
    for subject in pool:
        for _ in range(len(teachers)):
            who = teachers[i % len(teachers)]; i += 1
            if subject not in spec[who] and len(spec[who]) < PER_TEACHER:
                spec[who].add(subject); break
    for who in teachers:
        await db.execute("UPDATE teachers SET subjects=$2, max_blocks=40 WHERE id=$1",
                         who, sorted(spec[who]))

    got = defaultdict(int)
    for v in spec.values():
        for s in v: got[s] += 1
    print(f"\n{'subject':22} {'classes':>8} {'teachers':>9}")
    for s in sorted(classes, key=lambda x: -classes[x]):
        print(f"{s:22} {classes[s]:>8} {got[s]:>9}")
    print(f"\n{len(teachers)} teachers, {sum(len(v) for v in spec.values())} assignments")
    await db.disconnect()


asyncio.run(main())

"""Teachers per subject vs how many classes of it exist."""
import asyncio
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT_MAX = 25

async def main():
    await db.connect()
    staff = defaultdict(int)
    for r in await db.fetch(
            "SELECT unnest(subjects) AS s, count(*) AS n FROM teachers "
            "WHERE school_id=$1 GROUP BY 1", SCHOOL):
        staff[r["s"]] = r["n"]

    classes = defaultdict(int)
    for r in await db.fetch("""
        SELECT sub.subject, s.year_group, coalesce(c.name,'-') AS campus,
               count(*) AS heads
        FROM student_subjects sub
        JOIN students s ON s.id = sub.student_id
        LEFT JOIN campuses c ON c.id = s.campus_id
        WHERE s.school_id=$1 GROUP BY 1,2,3
    """, SCHOOL):
        classes[r["subject"]] += max(1, -(-r["heads"] // SOFT_MAX))

    print(f"{'subject':22} {'classes':>8} {'teachers':>9}  verdict")
    for subj in sorted(classes, key=lambda s: staff.get(s, 0) - classes[s]):
        c, t = classes[subj], staff.get(subj, 0)
        flag = "TIGHT" if t < c else ""
        print(f"{subj:22} {c:>8} {t:>9}  {flag}")
    await db.disconnect()

asyncio.run(main())

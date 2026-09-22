import asyncio
from collections import defaultdict
from models import database as db
SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"

async def main():
    await db.connect()
    print("lab capacities per campus:")
    for r in await db.fetch("""
        SELECT c.name campus, r.capacity, count(*) n
        FROM rooms r JOIN campuses c ON c.id=r.campus_id
        WHERE r.school_id=$1 AND r.room_type='lab'
        GROUP BY 1,2 ORDER BY 1,2""", SCHOOL):
        print(f"  {r['campus']:9} capacity {r['capacity']:>3}  x{r['n']}")

    print("\nhow many labs can hold a class of each size (berwick):")
    caps = [r["capacity"] for r in await db.fetch("""
        SELECT r.capacity FROM rooms r JOIN campuses c ON c.id=r.campus_id
        WHERE r.school_id=$1 AND r.room_type='lab' AND c.name='berwick'""", SCHOOL)]
    for size in (20, 22, 24, 25, 26, 28, 30):
        print(f"  class of {size:>2}: {sum(1 for c in caps if c >= size)} of {len(caps)} labs")

    print("\nprojected class sizes for lab subjects (berwick):")
    for r in await db.fetch("""
        SELECT sub.subject, s.year_group, count(*) heads
        FROM student_subjects sub JOIN students s ON s.id=sub.student_id
        LEFT JOIN campuses c ON c.id=s.campus_id
        LEFT JOIN subject_settings ss ON ss.school_id=s.school_id
             AND ss.subject=sub.subject AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id=$1 AND c.name='berwick'
          AND coalesce(ss.default_room_type,'')='lab'
        GROUP BY 1,2 ORDER BY 1,2""", SCHOOL):
        n = max(1, -(-r["heads"]//25))
        print(f"  {r['year_group']:8} {r['subject']:20} {r['heads']:>3} heads -> "
              f"{n} classes of ~{-(-r['heads']//n)}")
    await db.disconnect()

asyncio.run(main())

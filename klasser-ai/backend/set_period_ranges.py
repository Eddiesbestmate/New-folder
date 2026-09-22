import asyncio
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
FLOOR = 0.80

async def main():
    await db.connect()
    rows = await db.fetch("""
        SELECT id, subject, year_level, max_periods_per_cycle AS target
        FROM subject_settings WHERE school_id = $1
    """, SCHOOL)
    changed = 0
    for r in rows:
        target = r["target"] or 4
        lo = max(1, round(target * FLOOR))
        await db.execute("""
            UPDATE subject_settings
            SET min_periods_per_cycle = $2, max_periods_per_cycle = $3
            WHERE id = $1
        """, r["id"], lo, target)
        changed += 1
    print(f"set a min/max range on {changed} subject settings (floor {FLOOR:.0%})")
    for r in await db.fetch("""
        SELECT subject, year_level, min_periods_per_cycle AS lo,
               max_periods_per_cycle AS hi
        FROM subject_settings
        WHERE school_id = $1 AND year_level IN ('Year 9','Year 11')
        ORDER BY year_level, subject LIMIT 8
    """, SCHOOL):
        print(f"  {r['year_level']:8} {r['subject']:20} {r['lo']}-{r['hi']}")
    await db.disconnect()

asyncio.run(main())

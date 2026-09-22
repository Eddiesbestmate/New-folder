import asyncio
from models import database as db
from services import deterministic

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"

async def main():
    await db.connect()
    layout = await db.fetchval("""
        SELECT id FROM timetable_layouts
        WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, SCHOOL)
    r = await deterministic.pre_validate(SCHOOL, str(layout))
    print("passed:", r.passed)
    for f in r.failures:
        print("  FAIL:", f)
    for w in r.warnings:
        print("  warn:", w)
    if not r.failures and not r.warnings:
        print("  (no findings)")
    await db.disconnect()

asyncio.run(main())

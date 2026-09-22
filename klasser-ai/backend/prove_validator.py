import asyncio
from models import database as db
from services import deterministic

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"

async def report(label):
    layout = await db.fetchval("""
        SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, SCHOOL)
    r = await deterministic.pre_validate(SCHOOL, str(layout))
    print(f"--- {label}: passed={r.passed}")
    for f in r.failures: print("   FAIL:", f)
    for w in r.warnings: print("   warn:", w)

async def main():
    await db.connect()
    try:
        await db.execute("UPDATE teachers SET max_blocks = 8 WHERE school_id = $1", SCHOOL)
        await report("teachers throttled to 8 blocks")
        await db.execute("""
            UPDATE rooms SET room_type = 'classroom'
            WHERE school_id = $1 AND room_type = 'lab'
        """, SCHOOL)
        await report("every lab reclassified as a classroom")
    finally:
        await db.execute("UPDATE teachers SET max_blocks = 40 WHERE school_id = $1", SCHOOL)
        print("\nrestored max_blocks = 40 (labs are rebuilt by the next step)")
    await db.disconnect()

asyncio.run(main())

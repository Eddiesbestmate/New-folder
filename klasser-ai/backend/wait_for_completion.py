import asyncio
from models import database as db
SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"

async def main():
    await db.connect()
    for _ in range(120):
        r = await db.fetchrow("""
            SELECT t.status, ga.status attempt_status
            FROM timetables t JOIN generation_attempts ga ON ga.timetable_id = t.id
            WHERE t.school_id=$1 ORDER BY t.created_at DESC LIMIT 1
        """, SCHOOL)
        if r["attempt_status"] in ("complete", "failed"):
            print(f"DONE: timetable={r['status']} attempt={r['attempt_status']}")
            await db.disconnect()
            return
        await asyncio.sleep(10)
    print("still running after 20 minutes of polling")
    await db.disconnect()

asyncio.run(main())

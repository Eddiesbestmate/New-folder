import asyncio
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"

async def main():
    await db.connect()
    await db.execute("""
        INSERT INTO school_preferences (school_id, allow_cross_campus_travel)
        VALUES ($1, true)
        ON CONFLICT (school_id) DO UPDATE SET allow_cross_campus_travel = true
    """, SCHOOL)
    row = await db.fetchrow(
        "SELECT allow_cross_campus_travel FROM school_preferences WHERE school_id = $1",
        SCHOOL)
    print("allow_cross_campus_travel =", row["allow_cross_campus_travel"])
    n = await db.fetchval("SELECT count(*) FROM teachers WHERE school_id = $1", SCHOOL)
    e = await db.fetchval("""
        SELECT count(*) FROM student_subjects WHERE school_id = $1
    """, SCHOOL)
    print(f"teachers={n} enrolments={e}")
    await db.disconnect()

asyncio.run(main())

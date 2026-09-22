import asyncio
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
LABS_PER_CAMPUS = 11

async def main():
    await db.connect()
    # Rooms whose name says what they are can be restored exactly.
    n = await db.fetchval("""
        UPDATE rooms SET room_type = 'lab'
        WHERE school_id = $1 AND name ILIKE '%lab%' AND room_type <> 'lab'
        RETURNING 1
    """, SCHOOL)
    by_name = await db.fetchval("""
        SELECT count(*) FROM rooms WHERE school_id = $1 AND room_type = 'lab'
    """, SCHOOL)
    print(f"labs restored by name: {by_name}")

    # The rest were typed at random by the original seed, so the old mapping
    # is not recoverable - top each campus back up to a sensible count.
    for campus in await db.fetch(
            "SELECT id, name FROM campuses WHERE school_id = $1", SCHOOL):
        have = await db.fetchval("""
            SELECT count(*) FROM rooms
            WHERE school_id = $1 AND campus_id = $2 AND room_type = 'lab'
        """, SCHOOL, campus["id"])
        short = LABS_PER_CAMPUS - have
        if short > 0:
            await db.execute("""
                UPDATE rooms SET room_type = 'lab'
                WHERE id IN (SELECT id FROM rooms
                             WHERE school_id = $1 AND campus_id = $2
                               AND room_type = 'classroom'
                             ORDER BY name LIMIT $3)
            """, SCHOOL, campus["id"], short)
        print(f"  {campus['name']}: had {have}, topped up by {max(0, short)}")

    for r in await db.fetch("""
        SELECT c.name AS campus, r.room_type, count(*) AS n
        FROM rooms r JOIN campuses c ON c.id = r.campus_id
        WHERE r.school_id = $1 GROUP BY 1,2 ORDER BY 1,2
    """, SCHOOL):
        print(f"  {r['campus']:10} {r['room_type']:10} {r['n']}")
    await db.disconnect()

asyncio.run(main())

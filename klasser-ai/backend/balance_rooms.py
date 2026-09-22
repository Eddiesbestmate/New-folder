"""Give both campuses the same specialist facilities.

The original seed typed rooms at random, so officer ended up with three art
rooms to berwick's six. Classrooms are the donor - 41 per campus against a
peak demand of 17.
"""
import asyncio
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
WANT = {"art": 7, "music": 7, "lab": 11, "computer_lab": 7,
        "workshop": 7, "gym": 10}


async def main():
    await db.connect()
    for campus in await db.fetch(
            "SELECT id, name FROM campuses WHERE school_id=$1", SCHOOL):
        for kind, target in WANT.items():
            have = await db.fetchval("""
                SELECT count(*) FROM rooms
                WHERE school_id=$1 AND campus_id=$2 AND room_type=$3
            """, SCHOOL, campus["id"], kind)
            if have >= target:
                continue
            await db.execute("""
                UPDATE rooms SET room_type=$3
                WHERE id IN (SELECT id FROM rooms
                             WHERE school_id=$1 AND campus_id=$2
                               AND room_type='classroom'
                             ORDER BY capacity DESC LIMIT $4)
            """, SCHOOL, campus["id"], kind, target - have)
            print(f"  {campus['name']:9} {kind:13} {have} -> {target}")

    print("\nrooms now:")
    for r in await db.fetch("""
        SELECT c.name campus, r.room_type, count(*) n
        FROM rooms r JOIN campuses c ON c.id=r.campus_id
        WHERE r.school_id=$1 GROUP BY 1,2 ORDER BY 1,2""", SCHOOL):
        print(f"  {r['campus']:9} {r['room_type']:13} {r['n']}")
    await db.disconnect()


asyncio.run(main())

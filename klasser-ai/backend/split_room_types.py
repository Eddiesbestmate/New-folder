"""Give Computing and Design Technology their own rooms.

Both were mapped to 'lab', so they queued behind Science, Physics, Chemistry
and Biology for eleven science labs per campus - 41 classes for 11 rooms, with
peak concurrency exactly equal to supply, which is the one case greedy room
colouring cannot reliably solve.

Classrooms are the donor: 53 per campus against a peak demand of 17.
"""
import asyncio
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
CONVERT = {"computer_lab": 6, "workshop": 6}
SUBJECT_ROOM = {"Computing": "computer_lab", "Design Technology": "workshop"}


async def main():
    await db.connect()
    for campus in await db.fetch(
            "SELECT id, name FROM campuses WHERE school_id=$1", SCHOOL):
        for kind, n in CONVERT.items():
            have = await db.fetchval("""
                SELECT count(*) FROM rooms
                WHERE school_id=$1 AND campus_id=$2 AND room_type=$3
            """, SCHOOL, campus["id"], kind)
            short = n - have
            if short > 0:
                await db.execute("""
                    UPDATE rooms SET room_type=$3
                    WHERE id IN (SELECT id FROM rooms
                                 WHERE school_id=$1 AND campus_id=$2
                                   AND room_type='classroom'
                                 ORDER BY capacity DESC LIMIT $4)
                """, SCHOOL, campus["id"], kind, short)
            print(f"  {campus['name']:9} {kind:13} {have} -> {max(have, n)}")

    for subject, kind in SUBJECT_ROOM.items():
        n = await db.fetchval("""
            UPDATE subject_settings SET default_room_type=$3
            WHERE school_id=$1 AND subject=$2 RETURNING 1
        """, SCHOOL, subject, kind)
        print(f"  {subject} -> {kind}")

    print("\nrooms now:")
    for r in await db.fetch("""
        SELECT c.name campus, r.room_type, count(*) n
        FROM rooms r JOIN campuses c ON c.id=r.campus_id
        WHERE r.school_id=$1 GROUP BY 1,2 ORDER BY 1,2""", SCHOOL):
        print(f"  {r['campus']:9} {r['room_type']:13} {r['n']}")
    await db.disconnect()


asyncio.run(main())

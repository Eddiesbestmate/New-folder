"""Does the timetable respect which campus a teacher belongs to?"""

import asyncio

from models import database as db

VERSION = "5c955c87-6892-4da6-b4a8-eb4c8b435c32"


async def main() -> None:
    await db.connect()
    row = await db.fetchrow("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE t.campus_id IS NULL) AS teacher_unassigned,
               count(*) FILTER (WHERE t.campus_id IS NOT NULL
                                  AND t.campus_id <> e.campus_id) AS away
        FROM timetable_entries e
        JOIN teachers t ON t.id = e.teacher_id
        WHERE e.version_id = $1
    """, VERSION)
    print(f"entries                      {row['total']}")
    print(f"teacher has no home campus   {row['teacher_unassigned']}")
    print(f"teacher taught away from it  {row['away']}")

    # Same question for students: is anyone scheduled at the wrong campus?
    srow = await db.fetchrow("""
        SELECT count(*) AS links,
               count(*) FILTER (WHERE s.campus_id IS NOT NULL
                                  AND s.campus_id <> e.campus_id) AS away
        FROM timetable_entry_students es
        JOIN timetable_entries e ON e.id = es.timetable_entry_id
        JOIN students s ON s.id = es.student_id
        WHERE e.version_id = $1
    """, VERSION)
    print(f"\nstudent-entry links          {srow['links']}")
    print(f"students taught away from it {srow['away']}")
    await db.disconnect()


asyncio.run(main())

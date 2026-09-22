"""Summarise the most recent completed timetable."""

import asyncio

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"


async def main() -> None:
    await db.connect()

    v = await db.fetchrow("""
        SELECT v.id, v.status, t.name, t.status AS tt_status, t.completed_at
        FROM timetable_versions v
        JOIN generation_attempts ga ON ga.id = v.generation_attempt_id
        JOIN timetables t ON t.id = ga.timetable_id
        WHERE t.school_id = $1
        ORDER BY t.completed_at DESC NULLS LAST
        LIMIT 1
    """, SCHOOL)
    if not v:
        print("No generated version found.")
        await db.disconnect()
        return

    print(f"{v['name']}  timetable={v['tt_status']}  version={v['status']}")
    print(f"version id: {v['id']}")

    n = await db.fetchval(
        "SELECT count(*) FROM timetable_entries WHERE version_id = $1", v["id"])
    print(f"entries: {n}")

    by_year = await db.fetch("""
        SELECT split_part(class_code, ' ', 1) AS bucket, count(*) AS n
        FROM timetable_entries WHERE version_id = $1
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5
    """, v["id"])
    if by_year:
        print("busiest class codes:")
        for row in by_year:
            print(f"  {row['bucket']:12} {row['n']}")

    print(f"\n  http://127.0.0.1:5500/output.html?version={v['id']}")
    await db.disconnect()


asyncio.run(main())

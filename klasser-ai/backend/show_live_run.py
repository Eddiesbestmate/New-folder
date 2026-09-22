"""Where to watch whatever this school currently has in flight."""

import asyncio

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"


async def main() -> None:
    await db.connect()
    rows = await db.fetch("""
        SELECT j.id AS job_id, j.status AS job_status, j.timetable_id,
               t.name, t.status AS tt_status,
               ga.id AS attempt_id, gj.progress_pct, gj.current_stage
        FROM job_queue j
        JOIN timetables t ON t.id = j.timetable_id
        LEFT JOIN generation_attempts ga ON ga.timetable_id = t.id
        LEFT JOIN generation_jobs gj ON gj.attempt_id = ga.id
        WHERE j.school_id = $1 AND j.status IN ('queued', 'running')
        ORDER BY ga.attempt_number DESC
    """, SCHOOL)

    if not rows:
        print("Nothing in flight.")
    for r in rows:
        print(f"{r['name']}  [{r['job_status']}] {r['progress_pct']}% "
              f"{r['current_stage']}")
        print(f"  http://127.0.0.1:5500/progress.html?timetable={r['timetable_id']}")
    print("\n  queue: http://127.0.0.1:5500/queue.html")
    await db.disconnect()


asyncio.run(main())

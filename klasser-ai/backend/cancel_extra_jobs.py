"""Cancel every in-flight generation except the newest one."""

import asyncio

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"


async def main() -> None:
    await db.connect()

    live = await db.fetch("""
        SELECT id, timetable_id, status, attempts, created_at
        FROM job_queue
        WHERE school_id = $1 AND status IN ('queued', 'running')
        ORDER BY created_at DESC
    """, SCHOOL)

    print(f"{len(live)} job(s) in flight:")
    for job in live:
        print(f"  {job['id']}  {job['status']:8} attempt {job['attempts']}  "
              f"{job['created_at']}")

    if len(live) <= 1:
        print("Nothing to cancel.")
        await db.disconnect()
        return

    keep, *extra = live
    print(f"\nKeeping {keep['id']} ({keep['status']})")

    for job in extra:
        await db.execute("""
            UPDATE job_queue SET status = 'cancelled', completed_at = now()
            WHERE id = $1
        """, job["id"])
        print(f"Cancelled {job['id']} - its worker should notice within ~3s")

    await db.disconnect()


asyncio.run(main())

"""Show what the real validators actually said about the live test timetable."""

import asyncio

from models import database as db


async def go() -> None:
    await db.connect()

    tid = await db.fetchval(
        "SELECT id FROM timetables WHERE name = 'Live model test' LIMIT 1")
    if not tid:
        print("No live test timetable found (already cleaned up).")
        await db.disconnect()
        return

    rows = await db.fetch("""
        SELECT ga.attempt_number, vr.validator, vr.result, vr.detail
        FROM validation_results vr
        JOIN generation_attempts ga ON ga.id = vr.attempt_id
        WHERE ga.timetable_id = $1
        ORDER BY ga.attempt_number, vr.responded_at
    """, tid)

    for r in rows:
        detail = (r["detail"] or "").replace("\n", " ")
        print(f"\nattempt {r['attempt_number']}  {r['validator']:<16} {r['result']}")
        if detail:
            print(f"    {detail}")

    print("\n--- token usage so far ---")
    usage = await db.fetch("""
        SELECT model_used, count(*) AS calls,
               sum(coalesce(tokens_used, 0)) AS tokens,
               sum(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS ok,
               sum(CASE WHEN status = 'rate_limited' THEN 1 ELSE 0 END) AS limited,
               sum(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
               avg(latency_ms)::int AS avg_ms
        FROM pipeline_log WHERE timetable_id = $1
        GROUP BY model_used ORDER BY model_used
    """, tid)
    print(f"{'MODEL':<16} {'CALLS':>6} {'OK':>4} {'429':>4} {'ERR':>4} "
          f"{'TOKENS':>9} {'AVG ms':>8}")
    for u in usage:
        print(f"{u['model_used'] or '?':<16} {u['calls']:>6} {u['ok']:>4} "
              f"{u['limited']:>4} {u['errors']:>4} {u['tokens']:>9,} "
              f"{u['avg_ms'] or 0:>8,}")

    print("\n--- errors in full ---")
    for e in await db.fetch("""
        SELECT DISTINCT provider, left(error_message, 300) AS msg
        FROM error_log WHERE timetable_id = $1 AND error_message IS NOT NULL
        LIMIT 10
    """, tid):
        print(f"  {e['provider']}: {e['msg']}")

    await db.disconnect()


asyncio.run(go())

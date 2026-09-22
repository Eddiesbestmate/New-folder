"""Where a generation actually spends its time."""

import asyncio

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"


async def main() -> None:
    await db.connect()

    rows = await db.fetch("""
        SELECT ps.stage, ps.status,
               extract(epoch FROM (ps.completed_at - ps.started_at)) AS seconds
        FROM pipeline_state ps
        JOIN generation_attempts ga ON ga.id = ps.attempt_id
        JOIN timetables t ON t.id = ga.timetable_id
        WHERE t.school_id = $1 AND ps.completed_at IS NOT NULL
    """, SCHOOL)

    if not rows:
        print("No completed stages recorded yet.")
        await db.disconnect()
        return

    by_stage: dict[str, list[float]] = {}
    for r in rows:
        by_stage.setdefault(r["stage"], []).append(float(r["seconds"] or 0))

    total = sum(sum(v) for v in by_stage.values())
    print(f"{len(rows)} completed stage runs, {total / 60:.1f} minutes total\n")
    print(f"{'stage':28} {'runs':>5} {'median':>8} {'max':>8} {'total':>9} {'share':>7}")

    ordered = sorted(by_stage.items(), key=lambda kv: -sum(kv[1]))
    for stage, times in ordered:
        times.sort()
        median = times[len(times) // 2]
        share = sum(times) / total * 100 if total else 0
        print(f"{stage:28} {len(times):>5} {median:>7.1f}s {times[-1]:>7.1f}s "
              f"{sum(times):>8.1f}s {share:>6.1f}%")

    # AI calls are the suspected cost; count them per stage where logged.
    calls = await db.fetch("""
        SELECT pl.stage, count(*) AS n,
               sum(coalesce(pl.duration_ms, 0)) / 1000.0 AS seconds
        FROM pipeline_log pl
        JOIN timetables t ON t.id = pl.timetable_id
        WHERE t.school_id = $1
        GROUP BY 1 ORDER BY 3 DESC NULLS LAST
    """, SCHOOL)
    if calls:
        print(f"\n{'stage':28} {'ai calls':>9} {'ai seconds':>11}")
        for c in calls:
            print(f"{c['stage'] or '(none)':28} {c['n']:>9} "
                  f"{float(c['seconds'] or 0):>10.1f}s")

    await db.disconnect()


asyncio.run(main())

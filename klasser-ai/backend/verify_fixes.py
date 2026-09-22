"""Check the three things reported as broken, against the latest generated version."""
import asyncio
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"


async def main():
    await db.connect()
    v = await db.fetchrow("""
        SELECT v.id, t.name, t.completed_at
        FROM timetable_versions v
        JOIN generation_attempts ga ON ga.id = v.generation_attempt_id
        JOIN timetables t ON t.id = ga.timetable_id
        WHERE t.school_id = $1
        ORDER BY t.completed_at DESC NULLS LAST LIMIT 1
    """, SCHOOL)
    if not v:
        print("no completed version found")
        return
    V = str(v["id"])
    print(f"checking version {V}  ({v['name']}, completed {v['completed_at']})\n")

    # --- 1. multi-period check ---
    dist = await db.fetch("""
        SELECT n, count(*) c FROM (
          SELECT class_code, day_number, count(*) n
          FROM timetable_entries WHERE version_id = $1 GROUP BY 1,2
        ) x GROUP BY n ORDER BY n
    """, V)
    print("=== periods of the same class on the same day ===")
    for r in dist:
        print(f"  {r['n']} periods/day: {r['c']} instances")
    over2 = sum(r['c'] for r in dist if r['n'] > 2)
    print(f"  instances over 2 (more than one double): {over2}\n")

    # --- 2. gap check, Y7-9 ---
    teaching = [r['period_number'] for r in await db.fetch("""
        SELECT DISTINCT lp.period_number FROM layout_periods lp
        JOIN timetable_versions tv ON tv.id = $1
        JOIN generation_attempts ga ON ga.id = tv.generation_attempt_id
        JOIN timetables t ON t.id = ga.timetable_id
        WHERE lp.school_id = t.school_id AND lp.period_type = 'teaching'
        ORDER BY 1
    """, V)]
    rows = await db.fetch("""
        SELECT es.student_id, s.year_group, e.day_number, lp.period_number
        FROM timetable_entry_students es
        JOIN timetable_entries e ON e.id = es.timetable_entry_id
        JOIN layout_periods lp ON lp.id = e.layout_period_id
        JOIN students s ON s.id = es.student_id
        WHERE e.version_id = $1 AND s.year_group IN ('Year 7','Year 8','Year 9')
    """, V)
    by_sd = defaultdict(set)
    for r in rows:
        by_sd[(r['student_id'], r['day_number'])].add(r['period_number'])
    gap_instances = 0
    gap_days = 0
    for (sid, day), periods in by_sd.items():
        lo, hi = min(periods), max(periods)
        missing = [p for p in teaching if lo <= p <= hi and p not in periods]
        if missing:
            gap_days += 1
            gap_instances += len(missing)
    print("=== Y7-9 mid-day gaps ===")
    print(f"  student-days with a gap: {gap_days} of {len(by_sd)}")
    print(f"  total gap-period instances: {gap_instances}\n")

    # --- gap repair stage output ---
    stage = await db.fetchrow("""
        SELECT ps.output_data, ps.status,
               extract(epoch FROM (ps.completed_at - ps.started_at)) secs
        FROM pipeline_state ps
        JOIN generation_attempts ga ON ga.id = ps.attempt_id
        WHERE ga.id = (SELECT generation_attempt_id FROM timetable_versions WHERE id = $1)
          AND ps.stage = 'layer5b_gap_repair'
    """, V)
    print("=== gap-repair stage ===")
    print(f"  {dict(stage) if stage else 'STAGE DID NOT RUN'}\n")

    # --- 3. timing ---
    stages = await db.fetch("""
        SELECT ps.stage, extract(epoch FROM (ps.completed_at - ps.started_at)) secs
        FROM pipeline_state ps
        WHERE ps.attempt_id = (SELECT generation_attempt_id FROM timetable_versions WHERE id = $1)
          AND ps.completed_at IS NOT NULL
        ORDER BY ps.started_at
    """, V)
    total = sum(float(r['secs'] or 0) for r in stages)
    print(f"=== total generation time: {total/60:.1f} min ===")
    for r in stages:
        print(f"  {r['stage']:24} {float(r['secs'] or 0):>7.1f}s")

    await db.disconnect()


asyncio.run(main())

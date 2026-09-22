import asyncio
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"

async def main():
    await db.connect()
    rows = await db.fetch("""
        SELECT pl.stage, count(*) AS n,
               sum(coalesce(pl.latency_ms,0))/1000.0 AS secs,
               sum(coalesce(pl.tokens_used,0)) AS tokens
        FROM pipeline_log pl
        JOIN timetables t ON t.id = pl.timetable_id
        WHERE t.school_id = $1
        GROUP BY 1 ORDER BY 2 DESC
    """, SCHOOL)
    print(f"{'stage':28} {'calls':>6} {'ai secs':>9} {'tokens':>10}")
    tc = 0; tk = 0
    for r in rows:
        tc += r["n"]; tk += int(r["tokens"] or 0)
        print(f"{r['stage'] or '(none)':28} {r['n']:>6} {float(r['secs'] or 0):>8.0f}s {int(r['tokens'] or 0):>10,}")
    print(f"{'TOTAL':28} {tc:>6} {'':>9} {tk:>10,}")
    await db.disconnect()

asyncio.run(main())

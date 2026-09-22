import asyncio
from models import database as db

async def main():
    await db.connect()
    rows = await db.fetch("""
        SELECT c.conrelid::regclass::text AS child, a.attname AS col
        FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'f' AND c.confrelid::regclass::text = 'teachers'
        ORDER BY 1
    """)
    for r in rows:
        n = await db.fetchval(f"SELECT count(*) FROM {r['child']}")
        print(f"{r['child']}.{r['col']}  rows={n}")
    await db.disconnect()

asyncio.run(main())

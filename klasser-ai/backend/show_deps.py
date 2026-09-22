"""What references the tables a timetable teardown has to clear?"""

import asyncio

from models import database as db

TARGETS = ("timetables", "generation_attempts", "timetable_versions",
           "class_groups", "timetable_entries")


async def main() -> None:
    await db.connect()
    rows = await db.fetch("""
        SELECT c.conrelid::regclass::text AS child,
               c.confrelid::regclass::text AS parent,
               a.attname AS column_name
        FROM pg_constraint c
        JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'f'
          AND c.confrelid::regclass::text = ANY($1)
        ORDER BY 2, 1
    """, list(TARGETS))
    for r in rows:
        print(f"{r['parent']:22} <- {r['child']}.{r['column_name']}")
    await db.disconnect()


asyncio.run(main())

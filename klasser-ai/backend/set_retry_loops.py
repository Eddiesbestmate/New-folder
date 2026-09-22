"""Lower the pipeline's retry loops while debugging, to conserve AI quota."""

import asyncio
import sys

from models import database as db

KEY = "allocation_max_loops"
VALUE = sys.argv[1] if len(sys.argv) > 1 else "1"


async def main() -> None:
    await db.connect()

    row = await db.fetchrow(
        "SELECT key, value FROM settings WHERE key = $1", KEY)
    print(f"before: {dict(row) if row else '(unset - code default 5)'}")

    await db.execute("""
        INSERT INTO settings (key, value, category, description)
        VALUES ($1, $2, 'allocation', $3)
        ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()
    """, KEY, VALUE,
        "Pipeline retry loops per generation. Production default is 5.")

    row = await db.fetchrow(
        "SELECT key, value FROM settings WHERE key = $1", KEY)
    print(f"after:  {dict(row)}")
    await db.disconnect()


asyncio.run(main())

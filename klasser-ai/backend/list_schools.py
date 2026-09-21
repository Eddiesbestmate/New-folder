"""List schools in the database, to spot leftovers from crashed test runs."""

import asyncio

from models import database as db


async def go() -> None:
    await db.connect()
    rows = await db.fetch("SELECT id, name, created_at FROM schools ORDER BY created_at")
    for r in rows:
        users = await db.fetch(
            "SELECT email, role FROM users WHERE school_id = $1", r["id"])
        print(f"{r['created_at']:%Y-%m-%d %H:%M}  {r['name']}")
        print(f"    id={r['id']}")
        for u in users:
            print(f"    user: {u['email']} ({u['role']})")
        if not users:
            print("    no users")
    await db.disconnect()


asyncio.run(go())

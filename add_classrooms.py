#!/usr/bin/env python3
"""Add general classrooms. The solver ran out of them at 35%."""

import asyncio

import aiohttp

API_BASE = "http://localhost:8000"
EMAIL = "test@test.com"
PASSWORD = "A@2024Lu"
SUPABASE_URL = "https://zglgkwhocldlvrfhikfd.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpnbGdrd2hvY2xkbHZyZmhpa2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzOTk2NzIsImV4cCI6MjEwMzk3NTY3Mn0.-PKTIaaotkt63P7BW-oMdp_jMTlr1wTbVSXpsITHMy8"

PER_CAMPUS = 25


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                          json={"email": EMAIL, "password": PASSWORD},
                          headers={"apikey": SUPABASE_KEY}) as r:
            token = (await r.json())["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{API_BASE}/onboarding/school-details", headers=auth) as r:
            campuses = (await r.json())["campuses"]
        async with s.get(f"{API_BASE}/schools/rooms", headers=auth) as r:
            rooms = await r.json()
        if isinstance(rooms, dict):
            rooms = rooms.get("rooms", [])

        by_type: dict[str, int] = {}
        for room in rooms:
            by_type[room.get("room_type") or "none"] = \
                by_type.get(room.get("room_type") or "none", 0) + 1
        print(f"Before: {len(rooms)} rooms {by_type}")

        made = 0
        for campus in campuses:
            tag = campus["name"][:3].upper()
            for i in range(1, PER_CAMPUS + 1):
                body = {"name": f"{tag} General {i}", "campus_id": campus["id"],
                        "capacity": 30, "room_type": "classroom"}
                async with s.post(f"{API_BASE}/schools/rooms", json=body,
                                  headers=auth) as r:
                    if r.status >= 400:
                        text = await r.text()
                        if "already exists" not in text:
                            print(f"  {body['name']}: {r.status} {text[:120]}")
                    else:
                        made += 1
        print(f"Added {made} classrooms")

        async with s.get(f"{API_BASE}/schools/rooms", headers=auth) as r:
            rooms = await r.json()
        if isinstance(rooms, dict):
            rooms = rooms.get("rooms", [])
        after: dict[str, int] = {}
        for room in rooms:
            after[room.get("room_type") or "none"] = \
                after.get(room.get("room_type") or "none", 0) + 1
        print(f"After: {len(rooms)} rooms {after}")


asyncio.run(main())

#!/usr/bin/env python3
"""Seed the timetable layout (10-day cycle) and rooms for test@test.com."""

import asyncio
import random

import aiohttp

API_BASE = "http://localhost:8000"
EMAIL = "test@test.com"
PASSWORD = "A@2024Lu"
SUPABASE_URL = "https://zglgkwhocldlvrfhikfd.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpnbGdrd2hvY2xkbHZyZmhpa2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzOTk2NzIsImV4cCI6MjEwMzk3NTY3Mn0.-PKTIaaotkt63P7BW-oMdp_jMTlr1wTbVSXpsITHMy8"

# 7 teaching periods, recess after P2, lunch after P4.
DAY_TEMPLATE = [
    ("teaching", "08:50", "09:45", "Period 1"),
    ("teaching", "09:45", "10:40", "Period 2"),
    ("break",    "10:40", "11:00", "Recess"),
    ("teaching", "11:00", "11:55", "Period 3"),
    ("teaching", "11:55", "12:50", "Period 4"),
    ("lunch",    "12:50", "13:35", "Lunch"),
    ("teaching", "13:35", "14:30", "Period 5"),
    ("teaching", "14:30", "15:25", "Period 6"),
    ("teaching", "15:25", "16:20", "Period 7"),
]

DAYS_IN_CYCLE = 10
ROOMS_PER_CAMPUS = 30


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                          json={"email": EMAIL, "password": PASSWORD},
                          headers={"apikey": SUPABASE_KEY}) as r:
            token = (await r.json())["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        print("Authenticated")

        periods = [
            {"day_number": day, "period_number": slot, "period_type": kind,
             "start_time": start, "end_time": end, "label": label}
            for day in range(1, DAYS_IN_CYCLE + 1)
            for slot, (kind, start, end, label) in enumerate(DAY_TEMPLATE, start=1)
        ]
        layout = {"name": f"{DAYS_IN_CYCLE}-day cycle",
                  "days_in_cycle": DAYS_IN_CYCLE, "periods": periods}

        async with s.put(f"{API_BASE}/schools/layout", json=layout, headers=auth) as r:
            body = await r.text()
            print(f"Layout: {r.status} {body[:200]}")

        async with s.get(f"{API_BASE}/onboarding/school-details", headers=auth) as r:
            campuses = (await r.json())["campuses"]
        async with s.get(f"{API_BASE}/schools/rooms", headers=auth) as r:
            existing = {row["name"] for row in await r.json()}
        print(f"{len(campuses)} campuses, {len(existing)} rooms already present")

        made = 0
        for campus in campuses:
            for i in range(1, ROOMS_PER_CAMPUS + 1):
                if i <= 20:
                    name = f"{campus['name'][:3].upper()} Room {i}"
                    kind = "classroom"
                elif i <= 25:
                    name = f"{campus['name'][:3].upper()} Lab {i - 20}"
                    kind = "lab"
                else:
                    name = f"{campus['name'][:3].upper()} Studio {i - 25}"
                    kind = random.choice(["music", "art", "gym"])
                if name in existing:
                    continue
                room = {"name": name, "campus_id": campus["id"],
                        "capacity": random.randint(24, 32), "room_type": kind}
                async with s.post(f"{API_BASE}/schools/rooms", json=room,
                                  headers=auth) as r:
                    if r.status >= 400:
                        print(f"  room {name}: {r.status} {(await r.text())[:120]}")
                    else:
                        made += 1
        print(f"Rooms created: {made}")

        async with s.get(f"{API_BASE}/onboarding/progress", headers=auth) as r:
            progress = await r.json()
        for step in progress["steps"]:
            mark = "x" if step["complete"] else " "
            print(f"  [{mark}] {step['label']} {step.get('count', '')}")
        print(f"ready_to_generate: {progress['ready_to_generate']}")
        if progress.get("missing"):
            print(f"still missing: {progress['missing']}")


asyncio.run(main())

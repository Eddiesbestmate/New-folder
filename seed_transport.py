#!/usr/bin/env python3
"""Seed inter-campus transport: a 20-minute route plus buses at each campus."""

import asyncio

import aiohttp

API_BASE = "http://localhost:8000"
EMAIL = "test@test.com"
PASSWORD = "A@2024Lu"
SUPABASE_URL = "https://zglgkwhocldlvrfhikfd.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpnbGdrd2hvY2xkbHZyZmhpa2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzOTk2NzIsImV4cCI6MjEwMzk3NTY3Mn0.-PKTIaaotkt63P7BW-oMdp_jMTlr1wTbVSXpsITHMy8"

TRAVEL_MINUTES = 20
BUSES_PER_CAMPUS = 2
BUS_CAPACITY = 45


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                          json={"email": EMAIL, "password": PASSWORD},
                          headers={"apikey": SUPABASE_KEY}) as r:
            token = (await r.json())["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        async with s.get(f"{API_BASE}/onboarding/school-details", headers=auth) as r:
            campuses = (await r.json())["campuses"]
        print(f"Campuses: {[c['name'] for c in campuses]}")

        # The endpoint mirrors a route back the other way itself, so one post
        # per unordered pair covers both directions.
        for i, origin in enumerate(campuses):
            for destination in campuses[i + 1:]:
                route = {"from_campus_id": origin["id"],
                         "to_campus_id": destination["id"],
                         "travel_minutes": TRAVEL_MINUTES}
                async with s.post(f"{API_BASE}/transport/routes", json=route,
                                  headers=auth) as r:
                    print(f"Route {origin['name']} <-> {destination['name']}: "
                          f"{r.status} {(await r.text())[:160]}")

        for campus in campuses:
            for n in range(1, BUSES_PER_CAMPUS + 1):
                bus = {"name": f"{campus['name'].title()} Bus {n}",
                       "capacity": BUS_CAPACITY,
                       "home_campus_id": campus["id"]}
                async with s.post(f"{API_BASE}/transport/buses", json=bus,
                                  headers=auth) as r:
                    print(f"Bus {bus['name']}: {r.status} {(await r.text())[:160]}")

        async with s.get(f"{API_BASE}/transport/routes", headers=auth) as r:
            routes = await r.json()
        print(f"\nRoutes now on file: {len(routes)}")
        for route in routes:
            print(f"  {route}")

        async with s.get(f"{API_BASE}/onboarding/progress", headers=auth) as r:
            progress = await r.json()
        for step in progress["steps"]:
            print(f"  [{'x' if step['complete'] else ' '}] {step['label']} "
                  f"{step.get('count', '')}")
        print(f"ready_to_generate: {progress['ready_to_generate']}")


asyncio.run(main())

#!/usr/bin/env python3
"""Quick rooms and subjects seeding."""

import asyncio
import random
import aiohttp

API_BASE = "http://localhost:8000"
TEST_EMAIL = "test@test.com"
TEST_PASSWORD = "A@2024Lu"
SUPABASE_URL = "https://zglgkwhocldlvrfhikfd.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpnbGdrd2hvY2xkbHZyZmhpa2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzOTk2NzIsImV4cCI6MjEwMzk3NTY3Mn0.-PKTIaaotkt63P7BW-oMdp_jMTlr1wTbVSXpsITHMy8"

SUBJECTS = [
    "Mathematics", "English", "Science", "Physics", "Chemistry", "Biology",
    "History", "Geography", "Languages", "French", "Spanish", "German",
    "Art", "Music", "Physical Education", "Design Technology", "Computing",
    "Business Studies", "Economics", "Sociology"
]

YEAR_LEVELS = ["Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"]
ROOM_TYPES = ["classroom", "lab", "music", "art", "gym"]


async def login(session: aiohttp.ClientSession) -> str:
    url = f"{SUPABASE_URL}/auth/v1/token?grant_type=password"
    async with session.post(url, json={"email": TEST_EMAIL, "password": TEST_PASSWORD}, headers={"apikey": SUPABASE_KEY}) as resp:
        data = await resp.json()
        return data["access_token"]


async def api_call(session: aiohttp.ClientSession, method: str, path: str, token: str, data=None):
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    async with session.post(url, json=data, headers=headers) as resp:
        if resp.status >= 400:
            text = await resp.text()
            if "already exists" not in text:
                print(f"Error: {resp.status} {text[:100]}")
        return resp.status


async def seed():
    async with aiohttp.ClientSession() as session:
        token = await login(session)
        print("Authenticated")

        # Get campuses
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(f"{API_BASE}/onboarding/school-details", headers=headers) as resp:
            details = await resp.json()

        campus_ids = [c["id"] for c in details["campuses"]]
        print(f"Found {len(campus_ids)} campuses")

        # Seed rooms
        print("\nSeeding 60 rooms...")
        for campus_id in campus_ids:
            for i in range(30):
                room = {
                    "name": f"Room {i + 1}" if i < 20 else f"Lab {i - 19}" if i < 25 else f"Studio {i - 24}",
                    "campus_id": campus_id,
                    "capacity": random.randint(20, 35),
                    "room_type": random.choice(ROOM_TYPES),
                }
                await api_call(session, "POST", "/schools/rooms", token, room)
            print(f"  • Campus seeded with 30 rooms")

        # Seed subjects
        print(f"\nSeeding subjects...")
        count = 0
        for subject in SUBJECTS:
            for year in YEAR_LEVELS:
                await api_call(session, "POST", "/schools/subjects", token, {"subject": subject, "year_level": year})
                count += 1
        print(f"✓ Complete! Seeded 60 rooms and {count} subject/year combinations")


asyncio.run(seed())

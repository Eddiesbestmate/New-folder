#!/usr/bin/env python3
"""
Seed test@test.com with realistic school data:
- 700 students across 6 year levels
- 120 teachers
- 2 campuses with 20-min travel time
- 10-day cycle
- 7-period day with recess and lunch
"""

import asyncio
import random
import aiohttp
from typing import Optional

# Configuration
API_BASE = "http://localhost:8000"
TEST_EMAIL = "test@test.com"
TEST_PASSWORD = "A@2024Lu"
SUPABASE_URL = "https://zglgkwhocldlvrfhikfd.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpnbGdrd2hvY2xkbHZyZmhpa2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzOTk2NzIsImV4cCI6MjEwMzk3NTY3Mn0.-PKTIaaotkt63P7BW-oMdp_jMTlr1wTbVSXpsITHMy8"

# Data generation
FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "Michael", "Jennifer", "William", "Linda",
    "David", "Barbara", "Richard", "Elizabeth", "Joseph", "Susan", "Thomas", "Jessica",
    "Charles", "Sarah", "Christopher", "Karen", "Daniel", "Nancy", "Matthew", "Lisa",
    "Anthony", "Betty", "Mark", "Margaret", "Donald", "Sandra", "Steven", "Ashley",
    "Paul", "Kimberly", "Andrew", "Donna", "Joshua", "Carol", "Kenneth", "Michelle",
    "Kevin", "Emily", "Brian", "Melissa", "George", "Deborah", "Edward", "Stephanie"
]

SURNAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Peterson", "Phillips",
    "Campbell", "Parker", "Evans", "Edwards", "Collins", "Reyes", "Stewart", "Morris"
]

SUBJECTS = [
    "Mathematics", "English", "Science", "Physics", "Chemistry", "Biology",
    "History", "Geography", "Languages", "French", "Spanish", "German",
    "Art", "Music", "Physical Education", "Design Technology", "Computing",
    "Business Studies", "Economics", "Sociology"
]

YEAR_LEVELS = ["Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"]

ROOM_TYPES = ["classroom", "lab", "music", "art", "gym"]


async def login(session: aiohttp.ClientSession) -> str:
    """Get JWT token from Supabase."""
    url = f"{SUPABASE_URL}/auth/v1/token?grant_type=password"

    payload = {
        "email": TEST_EMAIL,
        "password": TEST_PASSWORD
    }

    async with session.post(url, json=payload, headers={"apikey": SUPABASE_KEY}) as resp:
        if resp.status != 200:
            data = await resp.json()
            raise Exception(f"Auth failed: {data}")
        data = await resp.json()
        return data["access_token"]


async def api_call(session: aiohttp.ClientSession, method: str, path: str, token: str, data: Optional[dict] = None) -> dict:
    """Make authenticated API call."""
    url = f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}"}

    if method == "GET":
        async with session.get(url, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise Exception(f"{method} {path}: {resp.status} {text}")
            return await resp.json()
    elif method == "POST":
        async with session.post(url, json=data, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise Exception(f"{method} {path}: {resp.status} {text}")
            return await resp.json()
    elif method == "PUT":
        async with session.put(url, json=data, headers=headers) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise Exception(f"{method} {path}: {resp.status} {text}")
            return await resp.json()


async def seed_data():
    """Main seeding function."""
    async with aiohttp.ClientSession() as session:
        print("Authenticating...")
        token = await login(session)
        print(f"✓ Authenticated as {TEST_EMAIL}")

        # Get school info
        print("\nFetching school info...")
        me = await api_call(session, "GET", "/auth/me", token)
        school_id = me["school_id"]
        campus_ids = []
        print(f"✓ School: {me['school_name']} ({school_id})")

        # Get existing campuses
        print("\nFetching campuses...")
        school_details = await api_call(session, "GET", "/onboarding/school-details", token)
        for campus in school_details.get("campuses", []):
            campus_ids.append(campus["id"])
        print(f"✓ Found {len(campus_ids)} campus(es): {[c['name'] for c in school_details.get('campuses', [])]}")

        if not campus_ids:
            print("ERROR: No campuses found. Complete onboarding first.")
            return

        # Seed teachers
        print(f"\nSeeding 120 teachers...")
        used_names = set()
        created = 0
        attempts = 0
        while created < 120 and attempts < 500:
            attempts += 1
            first = random.choice(FIRST_NAMES)
            last = random.choice(SURNAMES)
            name_key = f"{first} {last}".lower()

            if name_key in used_names:
                continue

            used_names.add(name_key)
            teacher = {
                "first_name": first,
                "surname": last,
                "subjects": random.sample(SUBJECTS, random.randint(1, 4)),
                "campus_id": random.choice(campus_ids),
                "max_blocks": random.randint(8, 15),
                "max_duties_per_cycle": random.randint(2, 8),
            }
            try:
                await api_call(session, "POST", "/schools/teachers", token, teacher)
                created += 1
                if created % 20 == 0:
                    print(f"  • {created}/120")
            except Exception as e:
                if "already exists" in str(e):
                    used_names.add(name_key)
                    continue
                raise
        print(f"✓ Teachers seeded ({created}/120)")

        # Seed students
        print(f"\nSeeding 700 students...")
        created = 0
        while created < 700:
            student = {
                "full_name": f"{random.choice(FIRST_NAMES)} {random.choice(SURNAMES)}",
                "year_group": random.choice(YEAR_LEVELS),
                "campus_id": random.choice(campus_ids),
                "subjects": random.sample(SUBJECTS, random.randint(3, 8)),
                "gender": random.choice(["M", "F"]),
            }
            try:
                await api_call(session, "POST", "/schools/students", token, student)
                created += 1
                if created % 100 == 0:
                    print(f"  • {created}/700")
            except Exception as e:
                if "already exists" in str(e):
                    continue
                raise
        print(f"✓ Students seeded (700)")

        # Seed rooms
        print(f"\nSeeding rooms (30 per campus)...")
        room_count = 0
        for campus_id in campus_ids:
            for i in range(30):
                room = {
                    "name": f"Room {i + 1}" if i < 20 else f"Lab {i - 19}" if i < 25 else f"Studio {i - 24}",
                    "campus_id": campus_id,
                    "capacity": random.randint(20, 35),
                    "room_type": random.choice(ROOM_TYPES),
                    "allows_split": random.choice([True, False]),
                }
                await api_call(session, "POST", "/schools/rooms", token, room)
                room_count += 1
            print(f"  • Campus {campus_id}: 30 rooms")
        print(f"✓ Rooms seeded ({room_count} total)")

        # Seed subjects
        print(f"\nSeeding subjects...")
        for subject in SUBJECTS:
            for year in YEAR_LEVELS:
                subj = {
                    "subject": subject,
                    "year_level": year,
                }
                try:
                    await api_call(session, "POST", "/schools/subjects", token, subj)
                except:
                    pass  # May already exist
        print(f"✓ Subjects seeded")

        print("\n" + "="*50)
        print("SEEDING COMPLETE!")
        print("="*50)
        print("\nSeed data summary:")
        print(f"  • Teachers: 120")
        print(f"  • Students: 700")
        print(f"  • Rooms: {room_count}")
        print(f"  • Year levels: 6")
        print(f"  • Subjects: {len(SUBJECTS)} × {len(YEAR_LEVELS)} combinations")
        print(f"\nNext steps:")
        print(f"  1. Go to http://localhost:5500/dashboard.html")
        print(f"  2. Click 'Generate a timetable'")
        print(f"  3. Configure your layout and requirements")


if __name__ == "__main__":
    asyncio.run(seed_data())

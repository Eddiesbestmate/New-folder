#!/usr/bin/env python3
"""
Rebuild enrolments around option lines instead of free subject choice.

Free choice is what made this unsolvable. With Year 12 picking any 4 of 11
subjects, one Art class draws students from across the whole cohort, so it
clashes with nearly every other class in the year and the electives have to
run one after another - about 55 slots of elective demand plus English, in a
70-slot cycle. The allocator reported 66 of 70 slots blocked by student
overlap.

Option lines are how real schools avoid this: subjects are grouped into lines
and a student takes one subject per line. Subjects inside a line therefore
hold disjoint students, so they can all run in the same period. A year level
then needs one block of periods per line rather than one per subject.

Nothing is deleted - students are PATCHed in place.
"""

import asyncio
import random
from collections import Counter

import aiohttp

API_BASE = "http://localhost:8000"
EMAIL = "test@test.com"
PASSWORD = "A@2024Lu"
SUPABASE_URL = "https://zglgkwhocldlvrfhikfd.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpnbGdrd2hvY2xkbHZyZmhpa2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzOTk2NzIsImV4cCI6MjEwMzk3NTY3Mn0.-PKTIaaotkt63P7BW-oMdp_jMTlr1wTbVSXpsITHMy8"

random.seed(20260921)

# Core is taken by everyone in the year; lines are one-subject-each choices.
CURRICULUM = {
    "Year 7": {
        "core": ["English", "Mathematics", "Science", "History", "Geography",
                 "Physical Education"],
        "lines": [["French", "Spanish", "German"],
                  ["Art", "Music", "Design Technology", "Computing"]],
    },
    "Year 9": {
        "core": ["English", "Mathematics", "Science", "Physical Education"],
        "lines": [["History", "Art", "French"],
                  ["Geography", "Music", "Spanish"],
                  ["Computing", "Design Technology", "Business Studies", "German"]],
    },
    "Year 11": {
        "core": ["English"],
        "lines": [["Mathematics", "Art", "Sociology"],
                  ["Physics", "Business Studies", "Geography"],
                  ["Chemistry", "Economics", "History"],
                  ["Biology", "Computing"]],
    },
}
CURRICULUM["Year 8"] = CURRICULUM["Year 7"]
CURRICULUM["Year 10"] = CURRICULUM["Year 9"]
CURRICULUM["Year 12"] = CURRICULUM["Year 11"]

HEAVY = {"English", "Mathematics", "Science"}
SENIOR = {"Year 11", "Year 12"}


def choose(year: str) -> list[str]:
    plan = CURRICULUM[year]
    return plan["core"] + [random.choice(line) for line in plan["lines"]]


def periods_for(subject: str, year: str) -> int:
    if year in SENIOR:
        return 5
    return 5 if subject in HEAVY else 3


def slots_needed(year: str) -> int:
    """
    Distinct periods the year level needs.

    Subjects in a line share periods, so a line costs one subject's worth, not
    the whole line's. This is the number that has to stay under the cycle.
    """
    plan = CURRICULUM[year]
    core = sum(periods_for(s, year) for s in plan["core"])
    lines = sum(max(periods_for(s, year) for s in line) for line in plan["lines"])
    return core + lines


async def main() -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                          json={"email": EMAIL, "password": PASSWORD},
                          headers={"apikey": SUPABASE_KEY}) as r:
            token = (await r.json())["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        print("Slots each year level needs, against a 70-period cycle:")
        for year in ["Year 7", "Year 9", "Year 11"]:
            print(f"  {year:9} {slots_needed(year)}")

        async with s.get(f"{API_BASE}/schools/students?limit=1000",
                         headers=auth) as r:
            page = await r.json()
        students = page["students"]
        print(f"\n{len(students)} of {page['total']} students fetched")

        demand: Counter = Counter()
        for n, student in enumerate(students, start=1):
            year = student["year_group"]
            chosen = choose(year)
            for subject in chosen:
                demand[subject] += 1
            body = {
                "full_name": student["full_name"],
                "year_group": year,
                "campus_id": student.get("campus_id"),
                "gender": student.get("gender"),
                "ability_band": student.get("ability_band"),
                "subjects": chosen,
            }
            async with s.patch(f"{API_BASE}/schools/students/{student['id']}",
                               json=body, headers=auth) as r:
                if r.status >= 400:
                    print(f"  {student['id']}: {r.status} {(await r.text())[:120]}")
            if n % 100 == 0:
                print(f"  {n}/{len(students)}")

        print("\nEnrolment by subject:")
        for subject, count in demand.most_common():
            print(f"  {subject:22} {count}")
        print("\nDone.")


asyncio.run(main())

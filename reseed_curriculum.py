#!/usr/bin/env python3
"""
Rewrite test@test.com's enrolments into a solvable curriculum.

The first seed gave every student a random 3-8 subjects out of all 20, which
makes the conflict graph dense enough that no timetable exists: almost every
class shares students with almost every other class, so nothing can run in
parallel. This replaces that with a real cohort structure - a shared core per
year level plus electives from a small pool - so classes can be blocked into
parallel lines.

Nothing is deleted. Students, teachers and subject settings are PATCHed in
place, so ids, names and campuses all survive.
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

JUNIOR = {"Year 7", "Year 8"}
MIDDLE = {"Year 9", "Year 10"}
SENIOR = {"Year 11", "Year 12"}

LANGUAGES = ["French", "Spanish", "German"]
JUNIOR_CREATIVE = ["Art", "Music", "Design Technology", "Computing"]
MIDDLE_ELECTIVES = ["History", "Geography", "Art", "Music", "Design Technology",
                    "Computing", "Business Studies", "French", "Spanish", "German"]
SENIOR_ELECTIVES = ["Mathematics", "Physics", "Chemistry", "Biology", "Economics",
                    "Business Studies", "Sociology", "History", "Geography",
                    "Computing", "Art"]

HEAVY = {"English", "Mathematics", "Science"}

SOFT_MAX, HARD_MAX, MIN_SIZE = 25, 30, 5
TEACHER_MAX_BLOCKS = 20
MIN_TEACHERS_PER_SUBJECT = 4

ROOM_FOR = {
    "Science": "lab", "Physics": "lab", "Chemistry": "lab", "Biology": "lab",
    "Music": "music", "Art": "art", "Physical Education": "gym",
    "Computing": "lab", "Design Technology": "lab",
}


def curriculum_for(year: str) -> list[str]:
    """The subjects one student in this year level takes."""
    if year in JUNIOR:
        return (["English", "Mathematics", "Science", "History", "Geography",
                 "Physical Education"]
                + [random.choice(LANGUAGES), random.choice(JUNIOR_CREATIVE)])
    if year in MIDDLE:
        return (["English", "Mathematics", "Science", "Physical Education"]
                + random.sample(MIDDLE_ELECTIVES, 3))
    return ["English"] + random.sample(SENIOR_ELECTIVES, 4)


def periods_for(subject: str, year: str) -> int:
    """Senior study is heavier; juniors carry more subjects more lightly."""
    if year in SENIOR:
        return 5
    return 5 if subject in HEAVY else 3


async def main() -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                          json={"email": EMAIL, "password": PASSWORD},
                          headers={"apikey": SUPABASE_KEY}) as r:
            token = (await r.json())["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        print("Authenticated")

        # This endpoint pages and wraps: {"total": n, "students": [...]}.
        async with s.get(f"{API_BASE}/schools/students?limit=1000",
                         headers=auth) as r:
            page = await r.json()
        students = page["students"]
        if len(students) < page["total"]:
            raise SystemExit(
                f"Only fetched {len(students)} of {page['total']} students; "
                "the reseed would leave the rest on random subjects.")
        async with s.get(f"{API_BASE}/schools/teachers", headers=auth) as r:
            teachers = await r.json()
        async with s.get(f"{API_BASE}/schools/subjects", headers=auth) as r:
            subjects = await r.json()
        print(f"{len(students)} students, {len(teachers)} teachers, "
              f"{len(subjects)} subject settings")

        # --- Students ------------------------------------------------------
        demand: Counter = Counter()
        print("\nRewriting student enrolments...")
        for n, student in enumerate(students, start=1):
            year = student["year_group"]
            chosen = curriculum_for(year)
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
                    print(f"  student {student['id']}: {r.status} "
                          f"{(await r.text())[:140]}")
            if n % 100 == 0:
                print(f"  {n}/{len(students)}")

        print("\nEnrolment demand:")
        for subject, count in demand.most_common():
            print(f"  {subject:22} {count}")

        # --- Teachers ------------------------------------------------------
        # Cover every taught subject with a floor of specialists, then hand out
        # what is left in proportion to demand, so English and Maths end up with
        # the staff their enrolments actually require.
        taught = [x for x in demand if demand[x] > 0]
        assignments: list[list[str]] = [[] for _ in teachers]
        slot = 0
        for subject in taught:
            for _ in range(MIN_TEACHERS_PER_SUBJECT):
                assignments[slot % len(teachers)].append(subject)
                slot += 1

        weighted: list[str] = []
        for subject, count in demand.items():
            weighted += [subject] * max(1, count // 25)

        for spec in assignments:
            while len(spec) < 2:
                pick = random.choice(weighted)
                if pick not in spec:
                    spec.append(pick)

        print("\nRewriting teacher subjects...")
        for n, (teacher, spec) in enumerate(zip(teachers, assignments), start=1):
            body = {
                "first_name": teacher["first_name"],
                "surname": teacher["surname"],
                "subjects": sorted(set(spec)),
                "campus_id": teacher.get("campus_id"),
                "max_blocks": TEACHER_MAX_BLOCKS,
                "max_duties_per_cycle": teacher.get("max_duties_per_cycle") or 5,
                "gender": teacher.get("gender"),
            }
            async with s.patch(f"{API_BASE}/schools/teachers/{teacher['id']}",
                               json=body, headers=auth) as r:
                if r.status >= 400:
                    print(f"  teacher {teacher['id']}: {r.status} "
                          f"{(await r.text())[:140]}")
            if n % 40 == 0:
                print(f"  {n}/{len(teachers)}")

        coverage = Counter(x for spec in assignments for x in set(spec))
        print("\nTeachers per subject:")
        for subject in taught:
            print(f"  {subject:22} {coverage[subject]}")

        # --- Subject settings ----------------------------------------------
        print("\nRewriting subject settings...")
        for n, setting in enumerate(subjects, start=1):
            subject, year = setting["subject"], setting.get("year_level")
            periods = periods_for(subject, year or "Year 7")
            body = {
                "subject": subject,
                "year_level": year,
                "hard_max_size": HARD_MAX,
                "soft_max_size": SOFT_MAX,
                "min_size": MIN_SIZE,
                "default_room_type": ROOM_FOR.get(subject, "classroom"),
                "is_double_period": False,
                "min_periods_per_cycle": periods,
                "max_periods_per_cycle": periods,
            }
            async with s.patch(f"{API_BASE}/schools/subjects/{setting['id']}",
                               json=body, headers=auth) as r:
                if r.status >= 400:
                    print(f"  subject {subject}/{year}: {r.status} "
                          f"{(await r.text())[:140]}")
            if n % 40 == 0:
                print(f"  {n}/{len(subjects)}")

        # --- Sanity check ---------------------------------------------------
        print("\nPer-student period load (must stay under 70 teaching slots):")
        for year in ["Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"]:
            sample = curriculum_for(year)
            load = sum(periods_for(x, year) for x in sample)
            print(f"  {year:9} {len(sample)} subjects, {load} periods")

        print("\nDone.")


asyncio.run(main())

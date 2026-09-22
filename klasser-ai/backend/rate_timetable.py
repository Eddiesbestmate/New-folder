"""How good is the generated timetable, as a timetable?"""

import asyncio
from collections import Counter, defaultdict

from models import database as db

VERSION = "5c955c87-6892-4da6-b4a8-eb4c8b435c32"


async def main() -> None:
    await db.connect()

    entries = await db.fetch("""
        SELECT e.day_number, lp.period_number, e.class_code, e.subject,
               e.teacher_id, e.room_id, e.campus_id,
               t.full_name AS teacher, r.name AS room, c.name AS campus
        FROM timetable_entries e
        JOIN layout_periods lp ON lp.id = e.layout_period_id
        JOIN teachers t ON t.id = e.teacher_id
        JOIN rooms r ON r.id = e.room_id
        JOIN campuses c ON c.id = e.campus_id
        WHERE e.version_id = $1
    """, VERSION)
    print(f"{len(entries)} entries")

    # --- Hard clashes: the things that make a timetable unusable ------------
    teacher_slots = Counter()
    room_slots = Counter()
    class_slots = Counter()
    for e in entries:
        slot = (e["day_number"], e["period_number"])
        teacher_slots[(e["teacher_id"], slot)] += 1
        room_slots[(e["room_id"], slot)] += 1
        class_slots[(e["class_code"], slot)] += 1

    t_clash = sum(v - 1 for v in teacher_slots.values() if v > 1)
    r_clash = sum(v - 1 for v in room_slots.values() if v > 1)
    c_clash = sum(v - 1 for v in class_slots.values() if v > 1)
    print(f"\nHARD CLASHES  teacher: {t_clash}  room: {r_clash}  class: {c_clash}")

    # --- Did every class get the periods it was promised? -------------------
    per_class = Counter(e["class_code"] for e in entries)
    print(f"classes placed: {len(per_class)}")
    counts = Counter(per_class.values())
    print("periods per class:", dict(sorted(counts.items())))

    # --- Teacher load spread -------------------------------------------------
    load = Counter(e["teacher"] for e in entries)
    if load:
        values = sorted(load.values())
        print(f"\nTeachers used: {len(load)}")
        print(f"  load min {values[0]}  median {values[len(values)//2]}  "
              f"max {values[-1]}  (periods per 70-slot cycle)")
        busiest = load.most_common(3)
        print(f"  busiest: {busiest}")

    # --- Gaps in a teacher's day (the usual quality complaint) --------------
    by_teacher_day = defaultdict(list)
    for e in entries:
        by_teacher_day[(e["teacher"], e["day_number"])].append(e["period_number"])
    gaps = 0
    windows = 0
    for periods in by_teacher_day.values():
        periods = sorted(periods)
        if len(periods) < 2:
            continue
        span = periods[-1] - periods[0] + 1
        holes = span - len(periods)
        gaps += holes
        if holes:
            windows += 1
    print(f"\nTeacher gap periods: {gaps} across {windows} teacher-days "
          f"({len(by_teacher_day)} teacher-days total)")

    # --- Cross-campus moves in a day ----------------------------------------
    moves = 0
    by_teacher_day_campus = defaultdict(set)
    for e in entries:
        by_teacher_day_campus[(e["teacher"], e["day_number"])].add(e["campus"])
    moves = sum(1 for v in by_teacher_day_campus.values() if len(v) > 1)
    print(f"Teacher-days spanning both campuses: {moves}")

    # --- Spread across the cycle --------------------------------------------
    per_day = Counter(e["day_number"] for e in entries)
    print(f"\nEntries per day: {[per_day[d] for d in sorted(per_day)]}")
    per_period = Counter(e["period_number"] for e in entries)
    print(f"Entries per period slot: {[per_period[p] for p in sorted(per_period)]}")

    await db.disconnect()


asyncio.run(main())

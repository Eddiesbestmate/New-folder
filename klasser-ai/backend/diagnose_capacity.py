"""Is the room shortage real? Compare class-period demand against capacity."""

import asyncio

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"


async def main() -> None:
    await db.connect()

    layout = await db.fetchrow("""
        SELECT id, days_in_cycle FROM timetable_layouts
        WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, SCHOOL)
    teaching = await db.fetchval("""
        SELECT count(*) FROM layout_periods
        WHERE layout_id = $1 AND period_type = 'teaching'
    """, layout["id"])
    print(f"Layout: {layout['days_in_cycle']} days, {teaching} teaching slots "
          f"per cycle")

    rooms = await db.fetch("""
        SELECT c.name AS campus, coalesce(r.room_type, 'none') AS kind,
               count(*) AS n
        FROM rooms r JOIN campuses c ON c.id = r.campus_id
        WHERE r.school_id = $1 GROUP BY 1, 2 ORDER BY 1, 2
    """, SCHOOL)
    print("\nRooms by campus and type:")
    for r in rooms:
        print(f"  {r['campus']:10} {r['kind']:10} {r['n']}")

    # The allocator builds its classes in memory, so they cannot be read back.
    # Rebuild the same demand from enrolments: one class per soft_max students
    # in each year/subject/campus, each meeting min_periods_per_cycle times.
    demand = await db.fetch("""
        SELECT s.year_group, ss.subject,
               coalesce(c.name, '(no campus)') AS campus,
               coalesce(ss.default_room_type, 'classroom') AS room_type,
               coalesce(ss.soft_max_size, 25) AS soft_max,
               coalesce(ss.min_periods_per_cycle, 4) AS periods,
               count(*) AS students
        FROM student_subjects sub
        JOIN students s ON s.id = sub.student_id
        LEFT JOIN campuses c ON c.id = s.campus_id
        LEFT JOIN subject_settings ss
               ON ss.school_id = s.school_id
              AND ss.subject = sub.subject
              AND ss.year_level = s.year_group
        WHERE s.school_id = $1
        GROUP BY 1, 2, 3, 4, 5, 6
        ORDER BY 3, 1, 2
    """, SCHOOL)

    by_room: dict[str, dict] = {}
    per_campus: dict[tuple[str, str], int] = {}
    for row in demand:
        kind = row["room_type"]
        classes = -(-row["students"] // row["soft_max"])  # ceil
        periods = classes * row["periods"]
        bucket = by_room.setdefault(kind, {"classes": 0, "periods": 0})
        bucket["classes"] += classes
        bucket["periods"] += periods
        key = (row["campus"], kind)
        per_campus[key] = per_campus.get(key, 0) + periods

    room_counts: dict[str, int] = {}
    for r in rooms:
        room_counts[r["kind"]] = room_counts.get(r["kind"], 0) + r["n"]

    print(f"\n{'room type':12} {'classes':>8} {'periods':>8} {'rooms':>6} "
          f"{'capacity':>9} {'used':>6}")
    for kind, bucket in sorted(by_room.items()):
        have = room_counts.get(kind, 0)
        capacity = have * teaching
        pct = (bucket["periods"] / capacity * 100) if capacity else float("inf")
        print(f"{kind:12} {bucket['classes']:>8} {bucket['periods']:>8} "
              f"{have:>6} {capacity:>9} {pct:>5.0f}%")

    total_classes = sum(b["classes"] for b in by_room.values())
    total_periods = sum(b["periods"] for b in by_room.values())
    print(f"\nTotal: {total_classes} classes, {total_periods} class-periods")
    print(f"Average classes running at once: "
          f"{total_periods / teaching:.1f} across the whole school")

    # Capacity is per campus - a berwick class cannot borrow an officer room.
    print("\nPer campus and room type (periods needed vs slots available):")
    campus_rooms: dict[tuple[str, str], int] = {}
    for r in rooms:
        campus_rooms[(r["campus"], r["kind"])] = r["n"]
    for (campus, kind), periods in sorted(per_campus.items()):
        have = campus_rooms.get((campus, kind), 0)
        capacity = have * teaching
        note = ""
        if capacity == 0:
            note = "  <-- NO ROOMS OF THIS TYPE"
        elif periods > capacity:
            note = "  <-- OVER CAPACITY"
        pct = f"{periods / capacity * 100:.0f}%" if capacity else "n/a"
        print(f"  {campus:10} {kind:10} {periods:>5} / {capacity:<5} "
              f"({pct}){note}")

    await db.disconnect()


asyncio.run(main())

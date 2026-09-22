"""Do students have to cross campuses during a day, and is it survivable?"""

import asyncio
from collections import defaultdict

from models import database as db

VERSION = "5c955c87-6892-4da6-b4a8-eb4c8b435c32"


async def main() -> None:
    await db.connect()
    rows = await db.fetch("""
        SELECT es.student_id, e.day_number, lp.period_number,
               c.name AS campus
        FROM timetable_entry_students es
        JOIN timetable_entries e ON e.id = es.timetable_entry_id
        JOIN layout_periods lp ON lp.id = e.layout_period_id
        JOIN campuses c ON c.id = e.campus_id
        WHERE e.version_id = $1
    """, VERSION)

    day = defaultdict(dict)
    for r in rows:
        day[(r["student_id"], r["day_number"])][r["period_number"]] = r["campus"]

    multi = 0
    back_to_back = 0
    worst = []
    for key, periods in day.items():
        campuses = set(periods.values())
        if len(campuses) < 2:
            continue
        multi += 1
        order = sorted(periods)
        hops = 0
        for a, b in zip(order, order[1:]):
            if periods[a] != periods[b]:
                hops += 1
                # Consecutive periods with no break between them: a 20 minute
                # trip has to fit inside the bell.
                if b == a + 1:
                    back_to_back += 1
        worst.append((hops, key))

    print(f"student-days total            {len(day)}")
    print(f"  spanning both campuses      {multi} "
          f"({multi / max(1, len(day)) * 100:.1f}%)")
    print(f"  campus change back-to-back  {back_to_back}  "
          "<-- 20 min trip with no gap")

    if worst:
        worst.sort(reverse=True)
        print(f"  most changes in one day     {worst[0][0]}")

    await db.disconnect()


asyncio.run(main())

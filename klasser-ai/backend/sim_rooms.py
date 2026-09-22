"""Two-pass placement with room capacity modelled, as the real allocator has.

The earlier simulation said zero failures and the real run still failed. It
omitted two things: the AI's proposed slots (now moved to the top-up pass) and
room-type capacity. This adds the rooms.
"""
import asyncio
from collections import defaultdict

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT_MAX = 25
SLOTS = [(d, p) for d in range(1, 11) for p in range(1, 8)]


async def main():
    await db.connect()
    rows = [dict(r) for r in await db.fetch("""
        SELECT s.id AS student, s.year_group, coalesce(c.name,'-') AS campus,
               sub.subject,
               coalesce(ss.min_periods_per_cycle, 4) AS lo,
               coalesce(ss.max_periods_per_cycle, 5) AS hi,
               coalesce(ss.default_room_type,'classroom') AS room_type
        FROM student_subjects sub
        JOIN students s ON s.id = sub.student_id
        LEFT JOIN campuses c ON c.id = s.campus_id
        LEFT JOIN subject_settings ss
               ON ss.school_id = s.school_id AND ss.subject = sub.subject
              AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id = $1
    """, SCHOOL)]

    supply = defaultdict(int)
    for r in await db.fetch("""
        SELECT coalesce(c.name,'-') AS campus,
               coalesce(r.room_type,'classroom') AS t, count(*) AS n
        FROM rooms r LEFT JOIN campuses c ON c.id = r.campus_id
        WHERE r.school_id = $1 GROUP BY 1,2
    """, SCHOOL):
        supply[(r["campus"], r["t"])] = r["n"]

    cohort, meta = defaultdict(list), {}
    for r in rows:
        k = (r["year_group"], r["campus"], r["subject"])
        cohort[k].append(r["student"])
        meta[k] = (r["lo"], r["hi"], r["room_type"])

    classes = {}
    for k, studs in cohort.items():
        n = max(1, -(-len(studs) // SOFT_MAX))
        for i in range(n):
            classes[f"{k[0]}|{k[1]}|{k[2]}|{i+1}"] = (set(studs[i::n]), meta[k], k)

    by_student = defaultdict(list)
    for code, (studs, _, _) in classes.items():
        for s in studs:
            by_student[s].append(code)
    graph = defaultdict(set)
    for codes in by_student.values():
        for a in codes:
            graph[a].update(c for c in codes if c != a)

    order = sorted(classes, key=lambda c: (-len(graph[c]), -classes[c][1][0], c))
    assigned = {c: [] for c in classes}
    load = defaultdict(int)
    slot_rooms = defaultdict(lambda: defaultdict(int))

    def ok(code, slot, chosen, rt):
        rt = rt  # rt is already the (campus, type) key
        if slot in chosen:
            return False
        if any(slot in assigned[o] for o in graph[code]):
            return False
        return not (supply.get(rt, 0) and slot_rooms[slot][rt] >= supply[rt])

    short = []
    for required in (True, False):
        for code in order:
            studs, (lo, hi, rtype), key = classes[code]
            rt = (key[1], rtype)
            target = lo if required else hi
            chosen = assigned[code]
            sib = {s for o, (_, _, k) in classes.items()
                   if k == key and o != code for s in assigned[o]}
            while len(chosen) < target:
                free = [s for s in SLOTS if ok(code, s, chosen, rt)]
                if not free:
                    if required:
                        short.append((code, lo, len(chosen)))
                    break
                free.sort(key=lambda s: (s not in sib, -load[s], s))
                pick = free[0]
                chosen.append(pick)
                load[pick] += 1
                slot_rooms[pick][rt] += 1

    filled = sum(len(v) for v in assigned.values())
    asked = sum(m[1] for _, m, _ in classes.values())
    print(f"{len(classes)} classes, rooms modelled")
    print(f"below minimum: {len(short)}")
    for c, lo, got in short[:5]:
        print(f"   {c}  min {lo}, got {got}")
    print(f"fill: {filled}/{asked} ({filled/asked:.0%} of maximum)")
    await db.disconnect()


asyncio.run(main())

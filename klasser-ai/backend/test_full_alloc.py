"""
Cohort-first allocation with every constraint layer 3 actually enforces:
student clashes, the per-day cap, room supply per campus and type, and
teacher supply per campus and subject.

If this reaches zero stranded and zero junior gaps, the same ordering can go
into layer 3 with its existing checks left in place.
"""
import asyncio
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
CAP = 3


async def load():
    layout = await db.fetchrow("""SELECT id FROM timetable_layouts
        WHERE school_id=$1 AND is_active=true
        ORDER BY created_at DESC LIMIT 1""", SCHOOL)
    slots = [(r["day_number"], r["period_number"]) for r in await db.fetch(
        """SELECT day_number, period_number FROM layout_periods
           WHERE layout_id=$1 AND period_type='teaching'
           ORDER BY day_number, period_number""", layout["id"])]
    a = await db.fetchrow("""SELECT a.id FROM generation_attempts a
        JOIN timetables t ON t.id=a.timetable_id
        WHERE t.school_id=$1 ORDER BY a.started_at DESC LIMIT 1""", SCHOOL)
    rows = await db.fetch("""
        SELECT cg.class_code, cg.subject, cg.year_level, cgs.student_id,
               c.name AS campus,
               coalesce(ss.default_room_type,'classroom') AS rt,
               coalesce(ss.max_periods_per_cycle,0) AS periods
        FROM class_groups cg
        JOIN campuses c ON c.id=cg.campus_id
        JOIN class_group_students cgs ON cgs.class_group_id=cg.id
        LEFT JOIN subject_settings ss ON ss.school_id=$2
             AND ss.subject=cg.subject AND ss.year_level=cg.year_level
        WHERE cg.attempt_id=$1""", a["id"], SCHOOL)
    cls = {}
    for r in rows:
        c = cls.setdefault(r["class_code"], {
            "students": set(), "periods": r["periods"], "year": r["year_level"],
            "campus": r["campus"], "rt": r["rt"], "subject": r["subject"]})
        c["students"].add(r["student_id"])

    rooms = await db.fetch("""SELECT c.name campus, r.room_type, count(*) n
        FROM rooms r JOIN campuses c ON c.id=r.campus_id
        WHERE r.school_id=$1 GROUP BY 1,2""", SCHOOL)
    supply = {(r["campus"], r["room_type"]): r["n"] for r in rooms}
    staff = await db.fetch("""SELECT t.subjects, c.name campus FROM teachers t
        LEFT JOIN campuses c ON c.id=t.campus_id WHERE t.school_id=$1""", SCHOOL)
    tsup = defaultdict(int)
    for t in staff:
        for s in (t["subjects"] or []):
            tsup[(t["campus"], s)] += 1
    return slots, cls, supply, tsup


def blocks_of(cls):
    by_student = defaultdict(set)
    for code, c in cls.items():
        for s in c["students"]:
            by_student[s].add(code)
    parent = {c: c for c in cls}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for codes in by_student.values():
        codes = list(codes)
        for c in codes[1:]:
            ra, rb = find(codes[0]), find(c)
            if ra != rb:
                parent[rb] = ra
    out = defaultdict(list)
    for c in cls:
        out[find(c)].append(c)
    return list(out.values())


def allocate(slots, cls, supply, tsup, cap=CAP):
    day_slots = defaultdict(list)
    for d, p in slots:
        day_slots[d].append((d, p))

    assigned = {c: [] for c in cls}
    owed = {c: cls[c]["periods"] for c in cls}
    # Resource use is shared across every block, so it is tracked globally.
    room_use = defaultdict(int)      # (campus, rt, slot) -> count
    teach_use = defaultdict(int)     # (campus, subject, slot) -> count
    slot_students = defaultdict(set)  # slot -> students already busy

    order = blocks_of(cls)
    # Fullest cohorts first: they have the least freedom.
    order.sort(key=lambda b: -sum(cls[c]["periods"] for c in b))

    for block in order:
        for day in sorted(day_slots):
            used_today = defaultdict(int)
            for slot in day_slots[day]:
                for code in sorted(block, key=lambda c: (-owed[c], c)):
                    c = cls[code]
                    if owed[code] <= 0 or used_today[code] >= cap:
                        continue
                    if c["students"] & slot_students[slot]:
                        continue
                    rk = (c["campus"], c["rt"], slot)
                    if room_use[rk] >= supply.get((c["campus"], c["rt"]), 0):
                        continue
                    tk = (c["campus"], c["subject"], slot)
                    if teach_use[tk] >= tsup.get((c["campus"], c["subject"]), 0):
                        continue
                    assigned[code].append(slot)
                    owed[code] -= 1
                    used_today[code] += 1
                    room_use[rk] += 1
                    teach_use[tk] += 1
                    slot_students[slot] |= c["students"]
    failures = [(c, cls[c]["periods"], cls[c]["periods"] - owed[c])
                for c in cls if owed[c] > 0]
    return assigned, failures


async def main():
    await db.connect()
    slots, cls, supply, tsup = await load()
    print(f"{len(cls)} classes, {len(blocks_of(cls))} blocks, {len(slots)} slots\n")
    assigned, failures = allocate(slots, cls, supply, tsup)
    print(f"stranded: {len(failures)}")
    for c, w, g in failures[:8]:
        print(f"    {c} ({cls[c]['subject']}) wanted {w}, got {g}")

    student_slots = defaultdict(set)
    year_of = {}
    for code, c in cls.items():
        for s in c["students"]:
            student_slots[s].update(assigned[code])
            year_of[s] = c["year"]
    print()
    for y in ("Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"):
        v = [len(slots) - len(student_slots[s])
             for s in student_slots if year_of[s] == y]
        if v:
            print(f"  {y}: avg gaps={sum(v)/len(v):.2f} "
                  f"zero-gap={sum(1 for x in v if x==0)}/{len(v)}")

    seen = defaultdict(lambda: defaultdict(int))
    for code, c in cls.items():
        for slot in assigned[code]:
            for s in c["students"]:
                seen[s][slot] += 1
    print(f"  double-booked student-slots: "
          f"{sum(1 for m in seen.values() for v in m.values() if v > 1)}")
    worst = max((sum(1 for s in assigned[c] if s[0] == d)
                 for c in cls for d in range(1, 11)), default=0)
    print(f"  most periods of one class in a day: {worst}")
    await db.disconnect()

asyncio.run(main())

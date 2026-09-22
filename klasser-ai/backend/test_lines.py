"""
Schedule blocks, not individual classes.

Classes that share no students - the subjects on a choice line, and the
parallel classes of one subject - can run in the same period, and a real
timetable makes them do exactly that. Scattering them instead burns a
separate slot for each, and then a universal subject like Year 11 English,
which clashes with every class in its year, finds all 66 slots consumed and
comes up one period short. Grouping them first is what makes the cycle fit.
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


def build_blocks(cls):
    """Pack mutually non-clashing classes of equal length into one block."""
    blocks = []
    for key in sorted({(c["year"], c["campus"], c["periods"])
                       for c in cls.values()}):
        year, campus, periods = key
        members = sorted(c for c, v in cls.items()
                         if (v["year"], v["campus"], v["periods"]) == key)
        for code in members:
            for b in blocks:
                if b["key"] != key:
                    continue
                if any(cls[code]["students"] & cls[o]["students"]
                       for o in b["classes"]):
                    continue
                b["classes"].append(code)
                break
            else:
                blocks.append({"key": key, "periods": periods,
                               "classes": [code]})
    return blocks


def allocate(slots, cls, supply, tsup, blocks, cap=CAP):
    day_slots = defaultdict(list)
    for d, p in slots:
        day_slots[d].append((d, p))

    assigned = {c: [] for c in cls}
    owed = {i: b["periods"] for i, b in enumerate(blocks)}
    room_use = defaultdict(int)
    teach_use = defaultdict(int)
    slot_students = defaultdict(set)
    used_today = defaultdict(lambda: defaultdict(int))

    # Longest blocks first - they have the least room to manoeuvre.
    order = sorted(range(len(blocks)), key=lambda i: -blocks[i]["periods"])

    for day in sorted(day_slots):
        for slot in day_slots[day]:
            for i in order:
                b = blocks[i]
                if owed[i] <= 0 or used_today[day][i] >= cap:
                    continue
                members = [cls[c] for c in b["classes"]]
                if any(m["students"] & slot_students[slot] for m in members):
                    continue
                need_r = defaultdict(int)
                need_t = defaultdict(int)
                for m in members:
                    need_r[(m["campus"], m["rt"])] += 1
                    need_t[(m["campus"], m["subject"])] += 1
                if any(room_use[(camp, rt, slot)] + n > supply.get((camp, rt), 0)
                       for (camp, rt), n in need_r.items()):
                    continue
                if any(teach_use[(camp, sub, slot)] + n > tsup.get((camp, sub), 0)
                       for (camp, sub), n in need_t.items()):
                    continue
                for code in b["classes"]:
                    assigned[code].append(slot)
                    m = cls[code]
                    room_use[(m["campus"], m["rt"], slot)] += 1
                    teach_use[(m["campus"], m["subject"], slot)] += 1
                    slot_students[slot] |= m["students"]
                owed[i] -= 1
                used_today[day][i] += 1
    short = [(i, blocks[i], owed[i]) for i in owed if owed[i] > 0]
    return assigned, short


async def main():
    await db.connect()
    slots, cls, supply, tsup = await load()
    blocks = build_blocks(cls)
    print(f"{len(cls)} classes packed into {len(blocks)} blocks, "
          f"{len(slots)} slots")
    per_cohort = defaultdict(int)
    for b in blocks:
        per_cohort[(b['key'][0], b['key'][1])] += b["periods"]
    print("  periods demanded per cohort (must be <= 66):")
    for k in sorted(per_cohort):
        print(f"    {k[0]:8} {k[1]:9} {per_cohort[k]}")
    print()

    assigned, short = allocate(slots, cls, supply, tsup, blocks)
    print(f"blocks short: {len(short)}")
    for i, b, owe in short[:6]:
        print(f"    {b['key']} short {owe} of {b['periods']}: "
              f"{', '.join(b['classes'][:4])}")

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
          f"{sum(1 for m in seen.values() for v in m.values() if v>1)}")
    worst = max((sum(1 for s in assigned[c] if s[0]==d)
                 for c in cls for d in range(1,11)), default=0)
    print(f"  most periods of one class in a day: {worst}")
    await db.disconnect()

asyncio.run(main())

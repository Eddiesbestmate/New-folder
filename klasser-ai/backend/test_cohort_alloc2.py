"""
Allocate each form group's day-by-day load directly, instead of letting a
global greedy discover the residue by accident.

Form groups share no students, so inside layer 3 they are independent
problems: a group's subjects have to divide exactly the 66 slots, no more
than `cap` of any one subject in a day. Filling each day with the subjects
that have the most periods still owed is the standard way to keep such an
allocation balanced, and it never paints the last subject into a corner the
way "take the fullest slot" does.
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
               coalesce(ss.max_periods_per_cycle,0) AS periods
        FROM class_groups cg
        JOIN class_group_students cgs ON cgs.class_group_id=cg.id
        LEFT JOIN subject_settings ss ON ss.school_id=$2
             AND ss.subject=cg.subject AND ss.year_level=cg.year_level
        WHERE cg.attempt_id=$1""", a["id"], SCHOOL)
    classes = defaultdict(lambda: [set(), 0, None])
    for r in rows:
        c = classes[r["class_code"]]
        c[0].add(r["student_id"])
        c[1] = r["periods"]
        c[2] = r["year_level"]
    return slots, {k: tuple(v) for k, v in classes.items()}


def cohorts(classes):
    """Group classes into blocks that share students - i.e. form groups."""
    by_student = defaultdict(set)
    for code, (st, _, _) in classes.items():
        for s in st:
            by_student[s].add(code)

    parent = {c: c for c in classes}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for codes in by_student.values():
        codes = list(codes)
        for c in codes[1:]:
            union(codes[0], c)

    blocks = defaultdict(list)
    for c in classes:
        blocks[find(c)].append(c)
    return list(blocks.values())


def allocate(classes, slots, cap=CAP):
    day_slots = defaultdict(list)
    for d, p in slots:
        day_slots[d].append((d, p))

    assigned = {c: [] for c in classes}
    failures = []

    for block in cohorts(classes):
        # How many periods each class in this block still owes.
        owed = {c: classes[c][1] for c in block}
        # Which classes in the block actually share students with each other
        # (an elective line's subjects do not, so they can share a slot).
        by_student = defaultdict(set)
        for c in block:
            for s in classes[c][0]:
                by_student[s].add(c)
        clash = defaultdict(set)
        for codes in by_student.values():
            for a in codes:
                clash[a].update(x for x in codes if x != a)

        for day in sorted(day_slots):
            used_today = defaultdict(int)
            for slot in day_slots[day]:
                here = []          # classes already taking this exact slot
                # Most periods still owed goes first; that is what keeps any
                # one class from being left with an impossible remainder.
                for c in sorted(block, key=lambda c: (-owed[c], c)):
                    if owed[c] <= 0 or used_today[c] >= cap:
                        continue
                    if any(o in clash[c] for o in here):
                        continue
                    here.append(c)
                    assigned[c].append(slot)
                    owed[c] -= 1
                    used_today[c] += 1
        for c in block:
            if owed[c] > 0:
                failures.append((c, classes[c][1], classes[c][1] - owed[c]))
    return assigned, failures


async def main():
    await db.connect()
    slots, classes = await load()
    blocks = cohorts(classes)
    print(f"{len(classes)} classes in {len(blocks)} independent blocks, "
          f"{len(slots)} slots\n")
    assigned, failures = allocate(classes, slots)
    print(f"cohort allocation (cap={CAP}): stranded={len(failures)}")
    for c, w, g in failures[:6]:
        print(f"    {c} wanted {w}, got {g}")

    student_slots = defaultdict(set)
    year_of = {}
    for code, (st, _, yr) in classes.items():
        for s in st:
            student_slots[s].update(assigned[code])
            year_of[s] = yr
    for y in ("Year 7", "Year 8", "Year 9", "Year 10", "Year 11", "Year 12"):
        v = [len(slots) - len(student_slots[s])
             for s in student_slots if year_of[s] == y]
        if v:
            print(f"    {y}: avg gaps={sum(v)/len(v):.2f} "
                  f"zero-gap={sum(1 for x in v if x==0)}/{len(v)}")

    # Would the rooms and staff actually cope with this allocation?
    import asyncio as _a
    meta = await db.fetch("""
        SELECT cg.class_code, c.name AS campus, cg.subject, cg.year_level,
               coalesce(ss.default_room_type,'classroom') AS rt
        FROM class_groups cg
        JOIN campuses c ON c.id=cg.campus_id
        LEFT JOIN subject_settings ss ON ss.school_id=$2
             AND ss.subject=cg.subject AND ss.year_level=cg.year_level
        WHERE cg.attempt_id=(SELECT a.id FROM generation_attempts a
            JOIN timetables t ON t.id=a.timetable_id
            WHERE t.school_id=$1 ORDER BY a.started_at DESC LIMIT 1)""",
        SCHOOL, SCHOOL)
    info = {m["class_code"]: m for m in meta}
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

    room_peak = defaultdict(int)
    teach_peak = defaultdict(int)
    for code, slotlist in assigned.items():
        m = info.get(code)
        if not m:
            continue
        for slot in slotlist:
            room_peak[(m["campus"], m["rt"], slot)] += 1
            teach_peak[(m["campus"], m["subject"], slot)] += 1

    over_r = defaultdict(int)
    for (camp, rt, slot), n in room_peak.items():
        have = supply.get((camp, rt), 0)
        if n > have:
            over_r[(camp, rt)] = max(over_r[(camp, rt)], n - have)
    over_t = defaultdict(int)
    for (camp, sub, slot), n in teach_peak.items():
        have = tsup.get((camp, sub), 0)
        if n > have:
            over_t[(camp, sub)] = max(over_t[(camp, sub)], n - have)

    print()
    print(f"    room-type overflows: {len(over_r)}")
    for (camp, rt), short in sorted(over_r.items(), key=lambda kv: -kv[1])[:8]:
        print(f"      {camp:9} {rt:13} short by {short} at peak "
              f"(have {supply.get((camp,rt),0)})")
    print(f"    teacher overflows: {len(over_t)}")
    for (camp, sub), short in sorted(over_t.items(), key=lambda kv: -kv[1])[:8]:
        print(f"      {camp:9} {sub:20} short by {short} at peak "
              f"(have {tsup.get((camp,sub),0)})")

    # No student may sit in two classes at once.
    dbl = 0
    seen = defaultdict(lambda: defaultdict(int))
    for code, (st, _, _) in classes.items():
        for slot in assigned[code]:
            for s in st:
                seen[s][slot] += 1
    for s, m in seen.items():
        dbl += sum(1 for v in m.values() if v > 1)
    print(f"    double-booked student-slots: {dbl}")
    await db.disconnect()

asyncio.run(main())

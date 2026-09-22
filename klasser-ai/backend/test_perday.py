"""
Is the per-day cap what strands the last class placed?

Neither rooms (29% peak) nor teachers (36% peak) are scarce, so the slots
the allocator calls blocked are not blocked by a resource. With Years 7-9
packed to exactly 66 of 66, the last subject in the order finds its
remaining free slots bunched onto days where it already sits twice.

The school's own requirement is no more than three periods of one class in
a day, so a cap of 2 is stricter than asked.
"""
import asyncio
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"


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
    # The real groups the pipeline just wrote - no reconstruction.
    rows = await db.fetch("""
        SELECT cg.class_code, cg.subject, cg.year_level,
               cgs.student_id,
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


def place(classes, slots, max_per_day):
    by_student = defaultdict(list)
    for code, (st, _, _) in classes.items():
        for s in st:
            by_student[s].append(code)
    graph = defaultdict(set)
    for codes in by_student.values():
        for a in codes:
            graph[a].update(x for x in codes if x != a)

    order = sorted(classes, key=lambda c: (-len(graph[c]), -classes[c][1], c))
    assigned = {c: [] for c in classes}
    load = defaultdict(int)
    failures = []
    for code in order:
        st, want, _ = classes[code]
        chosen = assigned[code]
        while len(chosen) < want:
            taken = set()
            for o in graph[code]:
                taken.update(assigned[o])
            per_day = defaultdict(int)
            for d, p in chosen:
                per_day[d] += 1
            free = [s for s in slots
                    if s not in chosen and s not in taken
                    and per_day[s[0]] < max_per_day]
            if not free:
                failures.append((code, want, len(chosen)))
                break
            free.sort(key=lambda s: (-load[s], s))
            chosen.append(free[0])
            load[free[0]] += 1
    return assigned, failures


async def main():
    await db.connect()
    slots, classes = await load()
    print(f"{len(classes)} real class groups, {len(slots)} slots\n")
    for cap in (2, 3):
        assigned, failures = place(classes, slots, cap)
        student_slots = defaultdict(set)
        year_of = {}
        for code, (st, _, yr) in classes.items():
            for s in st:
                student_slots[s].update(assigned[code])
                year_of[s] = yr
        print(f"MAX_PER_DAY = {cap}:  stranded={len(failures)}")
        for c, w, g in failures[:4]:
            print(f"    {c} wanted {w}, got {g}")
        for y in ("Year 7", "Year 8", "Year 9"):
            v = [len(slots) - len(student_slots[s])
                 for s in student_slots if year_of[s] == y]
            if v:
                print(f"    {y}: avg gaps={sum(v)/len(v):.2f} "
                      f"zero-gap={sum(1 for x in v if x==0)}/{len(v)}")
        print()
    await db.disconnect()

asyncio.run(main())

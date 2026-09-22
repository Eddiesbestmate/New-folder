"""
Form groups where they help, independent splitting where they hurt.

Pure form groups fix the gaps but shatter the electives into 83 classes of
under ten students. Universal subjects are where the crossings actually
cost - an elective already draws a different population, so splitting it
independently costs nothing structurally and keeps the classes a sane size.
"""
import asyncio
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT = 25
JUNIOR = {"Year 7", "Year 8", "Year 9"}
UNIVERSAL = 0.95          # taken by at least this share of the cohort


async def load():
    layout = await db.fetchrow("""SELECT id FROM timetable_layouts
        WHERE school_id=$1 ORDER BY created_at DESC LIMIT 1""", SCHOOL)
    slots = [(r["day_number"], r["period_number"]) for r in await db.fetch(
        """SELECT day_number, period_number FROM layout_periods
           WHERE layout_id=$1 AND period_type='teaching'
           ORDER BY day_number, period_number""", layout["id"])]
    rows = await db.fetch("""
        SELECT s.id AS student, s.year_group, coalesce(c.name,'-') AS campus,
               sub.subject,
               coalesce(ss.max_periods_per_cycle,
                        ss.min_periods_per_cycle,4) AS periods
        FROM student_subjects sub
        JOIN students s ON s.id=sub.student_id
        LEFT JOIN campuses c ON c.id=s.campus_id
        LEFT JOIN subject_settings ss ON ss.school_id=s.school_id
             AND ss.subject=sub.subject
             AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id=$1""", SCHOOL)
    return slots, [dict(r) for r in rows]


def build(rows, mode):
    cohort, periods = defaultdict(list), {}
    for r in rows:
        k = (r["year_group"], r["campus"], r["subject"])
        cohort[k].append(r["student"])
        periods[k] = r["periods"]

    pool = defaultdict(set)
    for (y, c, _), st in cohort.items():
        if y in JUNIOR:
            pool[(y, c)].update(st)
    form = {}
    for (y, c), st in pool.items():
        o = sorted(st)
        n = max(1, -(-len(o) // SOFT))
        for i, s in enumerate(o):
            form[s] = i % n

    classes = {}
    for k, st in cohort.items():
        y, c, subject = k
        share = len(st) / len(pool[(y, c)]) if (y, c) in pool else 0
        use_form = (mode != "split" and y in JUNIOR
                    and (mode == "all" or share >= UNIVERSAL))
        if use_form:
            g = defaultdict(list)
            for s in st:
                g[form[s]].append(s)
            for i, members in sorted(g.items()):
                classes[f"{y}|{c}|{subject}|{i+1}"] = (set(members), periods[k], k)
        else:
            n = max(1, -(-len(st) // SOFT))
            for i in range(n):
                classes[f"{y}|{c}|{subject}|{i+1}"] = (set(st[i::n]), periods[k], k)
    return classes


def place(classes, slots):
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
        st, want, key = classes[code]
        sib = {s for o, (_, _, k) in classes.items()
               if k == key and o != code for s in assigned[o]}
        chosen = assigned[code]
        while len(chosen) < want:
            taken = set()
            for o in graph[code]:
                taken.update(assigned[o])
            free = [s for s in slots if s not in chosen and s not in taken]
            if not free:
                failures.append((code, want, len(chosen)))
                break
            free.sort(key=lambda s: (s not in sib, -load[s], s))
            chosen.append(free[0])
            load[free[0]] += 1
    return assigned, failures


async def main():
    await db.connect()
    slots, rows = await load()
    for mode, label in (("split", "per-subject split (shipped)"),
                        ("all", "form groups everywhere"),
                        ("universal", "form groups for universal subjects only")):
        classes = build(rows, mode)
        assigned, failures = place(classes, slots)
        sizes = [len(s) for s, _, _ in classes.values()]
        student_slots = defaultdict(set)
        year_of = {}
        for code, (st, _, k) in classes.items():
            for s in st:
                student_slots[s].update(assigned[code])
                year_of[s] = k[0]
        print(f"{label}")
        print(f"  classes={len(classes)}  tiny(<10)={sum(1 for x in sizes if x < 10)}"
              f"  stranded={len(failures)}")
        for y in ("Year 7", "Year 8", "Year 9"):
            v = [len(slots) - len(student_slots[s])
                 for s in student_slots if year_of[s] == y]
            if v:
                print(f"    {y}: avg gaps={sum(v)/len(v):.2f}  "
                      f"zero-gap={sum(1 for x in v if x == 0)}/{len(v)}")
        print()
    await db.disconnect()

asyncio.run(main())

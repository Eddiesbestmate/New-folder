"""
Does keeping a junior cohort together in fixed form groups remove the gaps?

Today each subject splits its cohort independently (students[i::n]), so a
student can sit in stream 1 for English and stream 3 for Maths. Every such
crossing adds an edge to the conflict graph, and the packing gets harder
for no educational reason - real schools run 7A, 7B, 7C through their core
subjects as a block.

Offline, real enrolments, no credits.
"""

import asyncio
from collections import defaultdict

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT_MAX = 25
JUNIOR = {"Year 7", "Year 8", "Year 9"}


async def load():
    layout = await db.fetchrow("""
        SELECT id FROM timetable_layouts WHERE school_id = $1
        ORDER BY created_at DESC LIMIT 1""", SCHOOL)
    periods = await db.fetch("""
        SELECT day_number, period_number FROM layout_periods
        WHERE layout_id = $1 AND period_type = 'teaching'
        ORDER BY day_number, period_number""", layout["id"])
    rows = await db.fetch("""
        SELECT s.id AS student, s.year_group, coalesce(c.name,'-') AS campus,
               sub.subject,
               coalesce(ss.max_periods_per_cycle,
                        ss.min_periods_per_cycle, 4) AS periods
        FROM student_subjects sub
        JOIN students s ON s.id = sub.student_id
        LEFT JOIN campuses c ON c.id = s.campus_id
        LEFT JOIN subject_settings ss
               ON ss.school_id = s.school_id AND ss.subject = sub.subject
              AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id = $1""", SCHOOL)
    return ([(r["day_number"], r["period_number"]) for r in periods],
            [dict(r) for r in rows])


def build(rows, form_groups: bool):
    cohort = defaultdict(list)
    periods = {}
    for r in rows:
        key = (r["year_group"], r["campus"], r["subject"])
        cohort[key].append(r["student"])
        periods[key] = r["periods"]

    # Fixed form group per (year, campus) - the same block of students moves
    # through every subject together, instead of being re-diced per subject.
    form_of = {}
    if form_groups:
        pool = defaultdict(set)
        for (year, campus, _), students in cohort.items():
            if year in JUNIOR:
                pool[(year, campus)].update(students)
        for (year, campus), students in pool.items():
            ordered = sorted(students)
            n = max(1, -(-len(ordered) // SOFT_MAX))
            for i, sid in enumerate(ordered):
                form_of[sid] = (year, campus, i % n)

    classes = {}
    for key, students in cohort.items():
        year, campus, subject = key
        if form_groups and year in JUNIOR:
            groups = defaultdict(list)
            for sid in students:
                groups[form_of[sid][2]].append(sid)
            for idx, members in sorted(groups.items()):
                code = f"{year}|{campus}|{subject}|{idx + 1}"
                classes[code] = (set(members), periods[key], key)
        else:
            n = max(1, -(-len(students) // SOFT_MAX))
            for i in range(n):
                code = f"{year}|{campus}|{subject}|{i + 1}"
                classes[code] = (set(students[i::n]), periods[key], key)
    return classes


def place(classes, slots):
    by_student = defaultdict(list)
    for code, (students, _, _) in classes.items():
        for s in students:
            by_student[s].append(code)
    graph = defaultdict(set)
    for codes in by_student.values():
        for a in codes:
            graph[a].update(c for c in codes if c != a)

    order = sorted(classes, key=lambda c: (-len(graph[c]), -classes[c][1], c))
    assigned = {c: [] for c in classes}
    load = defaultdict(int)
    failures = []
    for code in order:
        students, want, key = classes[code]
        siblings = {s for other, (_, _, k) in classes.items()
                    if k == key and other != code for s in assigned[other]}
        chosen = assigned[code]
        while len(chosen) < want:
            taken = set()
            for o in graph[code]:
                taken.update(assigned[o])
            free = [s for s in slots if s not in chosen and s not in taken]
            if not free:
                failures.append((code, want, len(chosen)))
                break
            free.sort(key=lambda s: (s not in siblings, -load[s], s))
            chosen.append(free[0])
            load[free[0]] += 1
    return assigned, failures, graph


def report(classes, assigned, slots, graph, label):
    student_slots = defaultdict(set)
    year_of = {}
    for code, (students, _, key) in classes.items():
        for s in students:
            student_slots[s].update(assigned[code])
            year_of[s] = key[0]
    print(label)
    print(f"  classes stranded: {len(failures)}")
    for code, want, got in failures[:4]:
        print(f"    {code}  wanted {want}, got {got}")
    edges = sum(len(v) for v in graph.values()) // 2
    print(f"  conflict edges: {edges}")
    for year in ("Year 7", "Year 8", "Year 9"):
        vals = [len(slots) - len(student_slots[s])
                for s in student_slots if year_of[s] == year]
        if vals:
            print(f"    {year}: n={len(vals)} avg gaps={sum(vals)/len(vals):.1f} "
                  f"zero-gap students={sum(1 for v in vals if v == 0)}")
    print()


async def main():
    await db.connect()
    slots, rows = await load()
    global failures
    for fg in (False, True):
        classes = build(rows, fg)
        assigned, failures, graph = place(classes, slots)
        report(classes, assigned, slots, graph,
               "FORM GROUPS (junior cohort stays together)" if fg
               else "PER-SUBJECT SPLIT - shipped")
    await db.disconnect()

asyncio.run(main())


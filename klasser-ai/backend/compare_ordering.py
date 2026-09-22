"""
Does layer 3 order its classes by the right notion of "most constrained"?

It currently sorts by raw conflict-graph degree - how many other classes
share a student. The run keeps dying on a core class (10SCI1, 10HIS4,
10MAT2) that found the cycle nearly full, which suggests degree is the
wrong weight: what actually eats a class's 66 slots is how many PERIODS
its neighbours demand, not how many neighbours it has.

Offline, against the real enrolments and the real layout. No credits.
"""

import asyncio
from collections import defaultdict

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT_MAX = 25


async def load():
    layout = await db.fetchrow("""
        SELECT id, days_in_cycle FROM timetable_layouts
        WHERE school_id = $1 ORDER BY created_at DESC LIMIT 1
    """, SCHOOL)
    periods = await db.fetch("""
        SELECT day_number, period_number FROM layout_periods
        WHERE layout_id = $1 AND period_type = 'teaching'
        ORDER BY day_number, period_number
    """, layout["id"])
    slots = [(r["day_number"], r["period_number"]) for r in periods]

    rows = await db.fetch("""
        SELECT s.id AS student, s.year_group,
               coalesce(c.name, '-') AS campus, sub.subject,
               coalesce(ss.max_periods_per_cycle,
                        ss.min_periods_per_cycle, 4) AS periods
        FROM student_subjects sub
        JOIN students s ON s.id = sub.student_id
        LEFT JOIN campuses c ON c.id = s.campus_id
        LEFT JOIN subject_settings ss
               ON ss.school_id = s.school_id AND ss.subject = sub.subject
              AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id = $1
    """, SCHOOL)
    return slots, [dict(r) for r in rows]


def build_classes(rows):
    cohort = defaultdict(list)
    periods = {}
    for r in rows:
        key = (r["year_group"], r["campus"], r["subject"])
        cohort[key].append(r["student"])
        periods[key] = r["periods"]

    classes = {}
    for key, students in cohort.items():
        year, campus, subject = key
        n = max(1, -(-len(students) // SOFT_MAX))
        for i in range(n):
            code = f"{year}|{campus}|{subject}|{i + 1}"
            classes[code] = (set(students[i::n]), periods[key], key)
    return classes


def build_graph(classes):
    by_student = defaultdict(list)
    for code, (students, _, _) in classes.items():
        for s in students:
            by_student[s].append(code)
    graph = defaultdict(set)
    for codes in by_student.values():
        for a in codes:
            graph[a].update(c for c in codes if c != a)
    return graph


def place(classes, slots, graph, weighted: bool):
    """Greedy placement, packing the fullest legal slot.

    weighted=False reproduces the shipped ordering (raw degree).
    weighted=True orders by the periods a class's neighbours demand.
    """
    if weighted:
        key_fn = lambda c: (-sum(classes[n][1] for n in graph[c]),
                            -classes[c][1], c)
    else:
        key_fn = lambda c: (-len(graph[c]), -classes[c][1], c)
    order = sorted(classes, key=key_fn)

    assigned = {c: [] for c in classes}
    load = defaultdict(int)
    failures = []

    for code in order:
        students, want, key = classes[code]
        siblings = {s for other, (_, _, k) in classes.items()
                    if k == key and other != code
                    for s in assigned[other]}
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
    return assigned, failures


def gaps_report(classes, assigned, slots):
    """How many of each junior student's slots end up empty."""
    student_slots = defaultdict(set)
    for code, (students, _, key) in classes.items():
        for s in students:
            student_slots[s].update(assigned[code])
    year_of = {}
    for code, (students, _, key) in classes.items():
        for s in students:
            year_of[s] = key[0]

    out = {}
    for year in ("Year 7", "Year 8", "Year 9"):
        vals = [len(slots) - len(student_slots[s])
                for s in student_slots if year_of[s] == year]
        if vals:
            out[year] = (len(vals), sum(vals) / len(vals),
                         sum(1 for v in vals if v == 0))
    return out


async def main() -> None:
    await db.connect()
    slots, rows = await load()
    classes = build_classes(rows)
    graph = build_graph(classes)
    print(f"{len(classes)} classes, {len(slots)} teaching slots, "
          f"{len(rows)} enrolments\n")

    for weighted in (False, True):
        label = "WEIGHTED degree (neighbour periods)" if weighted \
                else "RAW degree (neighbour count) - shipped"
        assigned, failures = place(classes, slots, graph, weighted)
        print(label)
        print(f"  classes stranded: {len(failures)}")
        for code, want, got in failures[:5]:
            print(f"    {code}  wanted {want}, got {got}")
        for year, (n, avg, zero) in gaps_report(classes, assigned, slots).items():
            print(f"    {year}: n={n} avg gaps={avg:.1f} "
                  f"students with 0 gaps={zero}")
        print()

    await db.disconnect()


asyncio.run(main())

"""
Does layer 2 mixing campuses in a class explain the 9MAT2 failure?

The offline model split every subject per campus and found nothing stranded;
the real run died on Year 9 Maths. The pipeline groups a subject's whole year
cohort at once and only afterwards labels the class with its dominant campus,
so a French class can hold berwick and officer students. Every such class
conflicts with the form groups on BOTH sites at once.
"""
import asyncio
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT = 25
UNIVERSAL_SHARE = 0.95


async def load():
    layout = await db.fetchrow("""SELECT id FROM timetable_layouts
        WHERE school_id=$1 AND is_active=true
        ORDER BY created_at DESC LIMIT 1""", SCHOOL)
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


def build(rows, split_campus: bool):
    """split_campus=False reproduces the pipeline: a subject's whole year
    cohort is grouped at once, regardless of site."""
    year_students = defaultdict(set)
    for r in rows:
        year_students[r["year_group"]].add(r["student"])

    # form groups, always per campus (that part the pipeline does do)
    campus_of = {r["student"]: r["campus"] for r in rows}
    periods, takers = {}, defaultdict(list)
    for r in rows:
        takers[(r["year_group"], r["subject"])].append(r["student"])
        periods[(r["year_group"], r["subject"])] = r["periods"]

    form = {}
    pool = defaultdict(set)
    for r in rows:
        pool[(r["year_group"], campus_of[r["student"]])].add(r["student"])
    offset = defaultdict(int)
    for (year, campus), st in sorted(pool.items(), key=lambda kv: str(kv[0])):
        o = sorted(st)
        n = max(1, -(-len(o) // SOFT))
        base = offset[year]
        for i, s in enumerate(o):
            form[s] = base + (i % n)
        offset[year] = base + n

    classes = {}
    for (year, subject), st in takers.items():
        share = len(st) / len(year_students[year])
        p = periods[(year, subject)]
        if share >= UNIVERSAL_SHARE:
            g = defaultdict(list)
            for s in st:
                g[form[s]].append(s)
            for i, m in sorted(g.items()):
                classes[f"{year}|{subject}|{i}"] = (set(m), p)
        elif split_campus:
            per = defaultdict(list)
            for s in st:
                per[campus_of[s]].append(s)
            for camp, m in sorted(per.items()):
                n = max(1, -(-len(m) // SOFT))
                for i in range(n):
                    classes[f"{year}|{subject}|{camp}|{i}"] = (set(m[i::n]), p)
        else:
            n = max(1, -(-len(st) // SOFT))
            for i in range(n):
                classes[f"{year}|{subject}|{i}"] = (set(st[i::n]), p)
    return classes


def place(classes, slots):
    by_student = defaultdict(list)
    for code, (st, _) in classes.items():
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
        st, want = classes[code]
        chosen = assigned[code]
        while len(chosen) < want:
            taken = set()
            for o in graph[code]:
                taken.update(assigned[o])
            free = [s for s in slots if s not in chosen and s not in taken]
            if not free:
                failures.append((code, want, len(chosen)))
                break
            free.sort(key=lambda s: (-load[s], s))
            chosen.append(free[0])
            load[free[0]] += 1
    return assigned, failures, graph


async def main():
    await db.connect()
    slots, rows = await load()
    for split in (False, True):
        classes = build(rows, split)
        assigned, failures, graph = place(classes, slots)
        edges = sum(len(v) for v in graph.values()) // 2
        print("SPLIT BY CAMPUS" if split
              else "MIXED CAMPUSES (what the pipeline does)")
        print(f"  classes={len(classes)}  edges={edges}  stranded={len(failures)}")
        for c, w, g in failures[:5]:
            print(f"    {c}  wanted {w}, got {g}")
        print()
    await db.disconnect()

asyncio.run(main())

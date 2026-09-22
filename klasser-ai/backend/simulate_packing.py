"""
Replay layer 3's placement in plain Python, against the real enrolments.

No AI and no credits, so the heuristic can be argued with cheaply. The
question is narrow: with a full 70-period curriculum, can a greedy one-pass
allocator colour this cohort at all, or is the timetable asking for a perfect
packing that greedy cannot reach?
"""

import asyncio
from collections import defaultdict

from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT_MAX = 25
SLOTS = [(d, p) for d in range(1, 11) for p in range(1, 8)]   # 70


async def load():
    rows = await db.fetch("""
        SELECT s.id AS student, s.year_group,
               coalesce(c.name, '-') AS campus, sub.subject,
               coalesce(ss.min_periods_per_cycle, 4) AS periods
        FROM student_subjects sub
        JOIN students s ON s.id = sub.student_id
        LEFT JOIN campuses c ON c.id = s.campus_id
        LEFT JOIN subject_settings ss
               ON ss.school_id = s.school_id AND ss.subject = sub.subject
              AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id = $1
    """, SCHOOL)
    return [dict(r) for r in rows]


def build_classes(rows):
    """Split each year/subject/campus cohort into parallel streams."""
    cohort = defaultdict(list)
    periods = {}
    for r in rows:
        key = (r["year_group"], r["campus"], r["subject"])
        cohort[key].append(r["student"])
        periods[key] = r["periods"]

    classes = {}                       # code -> (students, periods, key)
    for key, students in cohort.items():
        year, campus, subject = key
        n = max(1, -(-len(students) // SOFT_MAX))
        for i in range(n):
            code = f"{year}|{campus}|{subject}|{i + 1}"
            classes[code] = (set(students[i::n]), periods[key], key)
    return classes


def place(classes, pack: bool):
    """Greedy placement. `pack` picks the fullest legal slot, else the emptiest."""
    by_student = defaultdict(list)
    for code, (students, _, _) in classes.items():
        for s in students:
            by_student[s].append(code)

    graph = defaultdict(set)
    for codes in by_student.values():
        for a in codes:
            graph[a].update(c for c in codes if c != a)

    order = sorted(classes, key=lambda c: (-len(graph[c]), -classes[c][1], c))

    assigned = {}
    load = defaultdict(int)
    failures = []
    for code in order:
        students, want, key = classes[code]
        siblings = {s for other, (_, _, k) in classes.items()
                    if k == key and other != code
                    for s in assigned.get(other, ())}
        chosen = []
        while len(chosen) < want:
            free = [s for s in SLOTS
                    if s not in chosen
                    and not any(s in assigned.get(o, ()) for o in graph[code])]
            if not free:
                failures.append((code, want, len(chosen)))
                break
            free.sort(key=lambda s: (s not in siblings,
                                     -load[s] if pack else load[s], s))
            chosen.append(free[0])
        assigned[code] = chosen
        for s in chosen:
            load[s] += 1
    return assigned, failures


async def main() -> None:
    await db.connect()
    rows = await load()
    classes = build_classes(rows)
    print(f"{len(classes)} classes from {len(rows)} enrolments\n")

    for pack in (False, True):
        assigned, failures = place(classes, pack)
        label = "PACK (fullest slot)" if pack else "SPREAD (emptiest slot)"
        used = defaultdict(set)
        for code, slots in assigned.items():
            year, campus, _, _ = code.split("|")
            used[(year, campus)].update(slots)
        worst = sorted(used.items(), key=lambda kv: -len(kv[1]))[:3]
        print(f"{label}")
        print(f"  classes that could not be placed: {len(failures)}")
        for code, want, got in failures[:3]:
            print(f"    {code}  wanted {want}, got {got}")
        print("  busiest cohorts (distinct slots of 70):")
        for (year, campus), slots in worst:
            print(f"    {year:9} {campus:9} {len(slots)}")
        print()

    # How much slack does greedy need? Trim the junior load a step at a time
    # and find where it stops stranding classes.
    # --- Two passes: every class gets its minimum, then the rest is filled --
    #
    # The school states a range per subject, not a number. Guaranteeing every
    # class its minimum before anyone takes a second helping is what stops a
    # class late in the order finding the cycle already full.
    def place_two_pass(classes, floor: float):
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

        def legal(code, chosen):
            return [s for s in SLOTS
                    if s not in chosen
                    and not any(s in assigned.get(o, ()) for o in graph[code])]

        def rank(code, key, chosen):
            siblings = {s for other, (_, _, k) in classes.items()
                        if k == key and other != code
                        for s in assigned.get(other, ())}
            return lambda s: (s not in siblings, -load[s], s)

        short = []
        for target_is_min in (True, False):
            for code in order:
                students, want, key = classes[code]
                lo = max(1, round(want * floor))
                target = lo if target_is_min else want
                chosen = assigned[code]
                while len(chosen) < target:
                    free = legal(code, chosen)
                    if not free:
                        if target_is_min:
                            short.append((code, lo, len(chosen)))
                        break
                    free.sort(key=rank(code, key, chosen))
                    chosen.append(free[0])
                    load[free[0]] += 1
        filled = sum(len(v) for v in assigned.values())
        asked = sum(w for _, w, _ in classes.values())
        return short, filled, asked

    print("Two-pass: guarantee a minimum, then fill toward the maximum")
    for floor in (1.00, 0.90, 0.80, 0.70):
        short, filled, asked = place_two_pass(classes, floor)
        print(f"  minimum = {floor:.0%} of target -> "
              f"{len(short)} below minimum, "
              f"overall fill {filled}/{asked} ({filled / asked:.0%})")
        if not short:
            print("    ^ every class reached its minimum")
            break

    print()
    print("Junior load vs placement success (packing on):")
    JUNIOR = {"Year 7", "Year 8", "Year 9", "Year 10"}
    for keep in (1.00, 0.95, 0.90, 0.85, 0.80, 0.75):
        scaled = {}
        for code, (students, want, key) in classes.items():
            reduced = (max(1, round(want * keep)) if key[0] in JUNIOR else want)
            scaled[code] = (students, reduced, key)

        # What one junior student now carries, which is the number that
        # decides whether the cohort can fit at all.
        per_student = defaultdict(int)
        for code, (students, want, key) in scaled.items():
            if key[0] in JUNIOR and students:
                per_student[key[0]] = per_student[key[0]] + want

        _, failures = place(scaled, True)
        sample = per_student.get("Year 9", 0)
        print(f"  junior load x{keep:.2f}  (~{sample} periods)  -> "
              f"{len(failures)} classes stranded")
        if not failures:
            print("    ^ first load that places cleanly")
            break

    await db.disconnect()


asyncio.run(main())

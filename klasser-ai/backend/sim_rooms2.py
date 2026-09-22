"""Does layer 4 have a valid room assignment to find?

Layer 4 gives a class ONE room for all of its periods. So per campus and room
type, classes that share any slot conflict, and the rooms are colours. This
asks whether a colouring exists at all, and whether greedy finds it.
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
               coalesce(ss.min_periods_per_cycle,4) AS lo,
               coalesce(ss.max_periods_per_cycle,5) AS hi,
               coalesce(ss.default_room_type,'classroom') AS rt
        FROM student_subjects sub
        JOIN students s ON s.id = sub.student_id
        LEFT JOIN campuses c ON c.id = s.campus_id
        LEFT JOIN subject_settings ss ON ss.school_id=s.school_id
             AND ss.subject=sub.subject
             AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id=$1
    """, SCHOOL)]

    supply = defaultdict(int)
    for r in await db.fetch("""
        SELECT coalesce(c.name,'-') campus, coalesce(r.room_type,'classroom') t,
               count(*) n FROM rooms r LEFT JOIN campuses c ON c.id=r.campus_id
        WHERE r.school_id=$1 GROUP BY 1,2""", SCHOOL):
        supply[(r["campus"], r["t"])] = r["n"]

    # Leave one room spare so layer 4 has somewhere to move a class to.
    cap = {k: (v - 1 if v > 2 else v) for k, v in supply.items()}

    cohort, meta = defaultdict(list), {}
    for r in rows:
        k = (r["year_group"], r["campus"], r["subject"])
        cohort[k].append(r["student"]); meta[k] = (r["lo"], r["hi"], r["rt"])
    classes = {}
    for k, st in cohort.items():
        n = max(1, -(-len(st)//SOFT_MAX))
        for i in range(n):
            classes[f"{k[0]}|{k[1]}|{k[2]}|{i+1}"] = (set(st[i::n]), meta[k], k)

    by_student = defaultdict(list)
    for c,(st,_,_) in classes.items():
        for s in st: by_student[s].append(c)
    graph = defaultdict(set)
    for cs in by_student.values():
        for a in cs: graph[a].update(x for x in cs if x != a)

    # layer 3
    order = sorted(classes, key=lambda c: (-len(graph[c]), -classes[c][1][0], c))
    assigned = {c: [] for c in classes}; load=defaultdict(int)
    sr = defaultdict(lambda: defaultdict(int))
    for required in (True, False):
        for c in order:
            st,(lo,hi,rt),k = classes[c]; key=(k[1],rt); tgt = lo if required else hi
            ch = assigned[c]
            sib = {s for o,(_,_,kk) in classes.items() if kk==k and o!=c for s in assigned[o]}
            while len(ch) < tgt:
                free=[s for s in SLOTS if s not in ch
                      and not any(s in assigned[o] for o in graph[c])
                      and not (cap.get(key,0) and sr[s][key]>=cap[key])]
                if not free: break
                free.sort(key=lambda s:(s not in sib, -load[s], s))
                ch.append(free[0]); load[free[0]]+=1; sr[free[0]][key]+=1

    # layer 4: one room per class, greedy, most-constrained first
    print(f"{'campus':10} {'type':10} {'classes':>8} {'rooms':>6} {'peak':>5} {'unplaced':>9}")
    total_fail = 0
    for (campus, rt), n_rooms in sorted(supply.items()):
        mine = [c for c,( _,(_,_,r),k) in classes.items() if k[1]==campus and r==rt]
        if not mine: continue
        peak = 0
        for s in SLOTS:
            peak = max(peak, sum(1 for c in mine if s in assigned[c]))
        busy = defaultdict(set)     # room index -> slots used
        fails = 0
        for c in sorted(mine, key=lambda c: -len(assigned[c])):
            want = set(assigned[c])
            for i in range(n_rooms):
                if not (busy[i] & want):
                    busy[i] |= want; break
            else:
                fails += 1
        total_fail += fails
        flag = "  <-- FAILS" if fails else ""
        print(f"{campus:10} {rt:10} {len(mine):>8} {n_rooms:>6} {peak:>5} {fails:>9}{flag}")
    print(f"\ntotal classes with no room: {total_fail}")
    await db.disconnect()

asyncio.run(main())

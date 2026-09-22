"""Does layer 5 have a valid teacher assignment to find?

Same colouring as rooms, but harder: a teacher covers several subjects, so
being spent on one subject removes them from another.
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
        SELECT s.id student, s.year_group, coalesce(c.name,'-') campus,
               sub.subject, coalesce(ss.min_periods_per_cycle,4) lo,
               coalesce(ss.max_periods_per_cycle,5) hi,
               coalesce(ss.default_room_type,'classroom') rt
        FROM student_subjects sub JOIN students s ON s.id=sub.student_id
        LEFT JOIN campuses c ON c.id=s.campus_id
        LEFT JOIN subject_settings ss ON ss.school_id=s.school_id
             AND ss.subject=sub.subject
             AND ss.year_level IS NOT DISTINCT FROM s.year_group
        WHERE s.school_id=$1""", SCHOOL)]
    supply = defaultdict(int)
    for r in await db.fetch("""
        SELECT coalesce(c.name,'-') campus, coalesce(r.room_type,'classroom') t,
               count(*) n FROM rooms r LEFT JOIN campuses c ON c.id=r.campus_id
        WHERE r.school_id=$1 GROUP BY 1,2""", SCHOOL):
        supply[(r["campus"], r["t"])] = r["n"]
    teachers = {str(r["id"]): set(r["subjects"] or []) for r in await db.fetch(
        "SELECT id, subjects FROM teachers WHERE school_id=$1", SCHOOL)}

    cohort, meta = defaultdict(list), {}
    for r in rows:
        k = (r["year_group"], r["campus"], r["subject"])
        cohort[k].append(r["student"]); meta[k] = (r["lo"], r["hi"], r["rt"])
    classes = {}
    for k, st in cohort.items():
        n = max(1, -(-len(st)//SOFT_MAX))
        for i in range(n):
            classes[f"{k[0]}|{k[1]}|{k[2]}|{i+1}"] = (set(st[i::n]), meta[k], k)

    g = defaultdict(set); bys = defaultdict(list)
    for c,(st,_,_) in classes.items():
        for s in st: bys[s].append(c)
    for cs in bys.values():
        for a in cs: g[a].update(x for x in cs if x != a)

    # layer 3 with the subject-teacher cap the real one now has
    tsupply = defaultdict(int)
    for subs in teachers.values():
        for s in subs: tsupply[s] += 1
    order = sorted(classes, key=lambda c: (-len(g[c]), -classes[c][1][0], c))
    assigned = {c: [] for c in classes}; load=defaultdict(int)
    sr = defaultdict(lambda: defaultdict(int)); ss_ = defaultdict(lambda: defaultdict(int))
    for required in (True, False):
        for c in order:
            st,(lo,hi,rt),k = classes[c]; subj=k[2]; key=(k[1],rt)
            tgt = lo if required else hi; ch = assigned[c]
            sib = {s for o,(_,_,kk) in classes.items() if kk==k and o!=c for s in assigned[o]}
            while len(ch) < tgt:
                free=[s for s in SLOTS if s not in ch
                      and not any(s in assigned[o] for o in g[c])
                      and not (supply.get(key,0) and sr[s][key]>=supply[key])
                      and not (tsupply.get(subj,0) and ss_[s][subj]>=tsupply[subj])]
                if not free: break
                free.sort(key=lambda s:(s not in sib, -load[s], s))
                ch.append(free[0]); load[free[0]]+=1
                sr[free[0]][key]+=1; ss_[free[0]][subj]+=1

    by_subject = defaultdict(list)
    for c,(_,_,k) in classes.items(): by_subject[k[2]].append(c)
    peak = defaultdict(int)
    for subj in by_subject:
        for s in SLOTS:
            peak[subj] = max(peak[subj], sum(1 for c in by_subject[subj] if s in assigned[c]))

    ORDERS = {
        "fewest teachers": lambda s: (tsupply.get(s,0), s),
        "least slack":     lambda s: (tsupply.get(s,0) - peak[s], s),
        "most pressure":   lambda s: (-(peak[s] / max(1, tsupply.get(s,0))), s),
        "most classes":    lambda s: (-len(by_subject[s]), s),
    }
    results = {}
    for label, keyfn in ORDERS.items():
      busy = defaultdict(set)
      fails = []
      for subj in sorted(by_subject, key=keyfn):
        qualified = [t for t,subs in teachers.items() if subj in subs]
        mine = by_subject[subj]
        ent = {c: sum(1 for o in mine if o!=c and set(assigned[c]) & set(assigned[o]))
               for c in mine}
        for c in sorted(mine, key=lambda c: -ent[c]):
            want = set(assigned[c])
            for t in sorted(qualified, key=lambda t: len(busy[t])):
                if not (busy[t] & want):
                    busy[t] |= want; break
            else:
                fails.append((subj, c, len(qualified)))
    print(f"classes with no free teacher: {len(fails)}")
    for subj, c, q in fails[:8]:
        print(f"   {c}   ({q} qualified teachers)")
    peak = defaultdict(int)
    for subj in by_subject:
        for s in SLOTS:
            peak[subj] = max(peak[subj], sum(1 for c in by_subject[subj] if s in assigned[c]))
    print(f"\n{'subject':22} {'classes':>8} {'teachers':>9} {'peak':>5}")
    for subj in sorted(by_subject, key=lambda s: tsupply.get(s,0)):
        mark = "  <-- tight" if peak[subj] >= tsupply.get(subj,0) else ""
        print(f"{subj:22} {len(by_subject[subj]):>8} {tsupply.get(subj,0):>9} {peak[subj]:>5}{mark}")
    await db.disconnect()

asyncio.run(main())

"""How much teacher slack does layer 5 need?

Layer 5 gives a class one teacher for all of its periods, and a teacher covers
several subjects, so being spent on one removes them from another. This sweeps
the roster size to find where the greedy assignment stops stranding classes.
"""
import asyncio, random
from collections import defaultdict
from models import database as db

SCHOOL = "8f209622-6848-4388-a885-95fba51fb873"
SOFT_MAX = 25
SLOTS = [(d, p) for d in range(1, 11) for p in range(1, 8)]
random.seed(7)


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

    by_subject = defaultdict(list)
    for c,(_,_,k) in classes.items(): by_subject[k[2]].append(c)
    subjects = sorted(by_subject)

    def staff(n_teachers, per_teacher=4):
        """Deal subjects to n teachers in proportion to class count."""
        budget = n_teachers * per_teacher
        total = sum(len(v) for v in by_subject.values())
        target = {s: max(3, round(budget*len(by_subject[s])/total)) for s in subjects}
        pool = [s for s, n in target.items() for _ in range(n)]
        random.shuffle(pool)
        spec = defaultdict(set); i = 0
        for s in pool:
            for _ in range(n_teachers):
                t = i % n_teachers; i += 1
                if s not in spec[t] and len(spec[t]) < per_teacher:
                    spec[t].add(s); break
        return spec

    def trial(n_teachers):
        spec = staff(n_teachers)
        tsupply = defaultdict(int)
        for subs in spec.values():
            for s in subs: tsupply[s] += 1

        assigned = {c: [] for c in classes}; load=defaultdict(int)
        sr = defaultdict(lambda: defaultdict(int)); su = defaultdict(lambda: defaultdict(int))
        order = sorted(classes, key=lambda c: (-len(g[c]), -classes[c][1][0], c))
        for required in (True, False):
            for c in order:
                st,(lo,hi,rt),k = classes[c]; subj=k[2]; key=(k[1],rt)
                tgt = lo if required else hi; ch = assigned[c]
                sib = {s for o,(_,_,kk) in classes.items() if kk==k and o!=c for s in assigned[o]}
                while len(ch) < tgt:
                    free=[s for s in SLOTS if s not in ch
                          and not any(s in assigned[o] for o in g[c])
                          and not (supply.get(key,0) and sr[s][key]>=supply[key])
                          and not (tsupply.get(subj,0) and su[s][subj]>=tsupply[subj])]
                    if not free: break
                    free.sort(key=lambda s:(s not in sib, -load[s], s))
                    ch.append(free[0]); load[free[0]]+=1
                    sr[free[0]][key]+=1; su[free[0]][subj]+=1

        short = sum(1 for c,(_,(lo,_,_),_) in classes.items() if len(assigned[c]) < lo)
        peak = {s: max((sum(1 for c in by_subject[s] if sl in assigned[c])
                        for sl in SLOTS), default=0) for s in subjects}
        busy = defaultdict(set); fails = 0
        for subj in sorted(subjects, key=lambda s: tsupply.get(s,0)-peak[s]):
            qual = [t for t,subs in spec.items() if subj in subs]
            mine = by_subject[subj]
            ent = {c: sum(1 for o in mine if o!=c and set(assigned[c]) & set(assigned[o]))
                   for c in mine}
            for c in sorted(mine, key=lambda c: -ent[c]):
                want = set(assigned[c])
                for t in sorted(qual, key=lambda t: len(busy[t])):
                    if not (busy[t] & want): busy[t] |= want; break
                else: fails += 1
        return short, fails

    print(f"{'teachers':>9} {'below min':>10} {'no teacher':>11}")
    for n in (70, 80, 90, 100, 115):
        short, fails = trial(n)
        print(f"{n:>9} {short:>10} {fails:>11}")
        if short == 0 and fails == 0:
            print("   ^ places cleanly")
            break
    await db.disconnect()

asyncio.run(main())

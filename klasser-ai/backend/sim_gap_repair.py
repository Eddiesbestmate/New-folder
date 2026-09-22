"""
Prototype gap-repair against the real generated timetable.

A gap is a free period sandwiched between two classes a student attends on
the same day. Layer 3 assigns slots to classes, never to a student's day as a
whole, so nothing today notices when the union of one student's classes
leaves a hole in the middle rather than at the edges.

The repair move: relocate ONE occurrence of a class the gapped student takes
- from wherever else it meets to the gap slot - provided that is legal for
EVERY student in that class, not just the one with the gap. A class's total
period count never changes, only where one of its occurrences sits, so it
never violates the min/max the school asked for.
"""
import asyncio
from collections import defaultdict

from models import database as db

V = "7cb39b21-1703-415f-b334-eac752aff92b"
SLOTS = [(d, p) for d in range(1, 11) for p in (1, 2, 4, 5, 7, 8, 9)]


async def main():
    await db.connect()

    rows = await db.fetch("""
        SELECT e.id entry_id, e.class_code, e.day_number, lp.period_number,
               e.teacher_id, e.room_id, e.campus_id, e.subject
        FROM timetable_entries e
        JOIN layout_periods lp ON lp.id = e.layout_period_id
        WHERE e.version_id = $1
    """, V)
    links = await db.fetch("""
        SELECT es.timetable_entry_id, es.student_id
        FROM timetable_entry_students es
        JOIN timetable_entries e ON e.id = es.timetable_entry_id
        WHERE e.version_id = $1
    """, V)
    student_names = {r["id"]: r["year_group"] for r in await db.fetch(
        "SELECT id, year_group FROM students "
        "WHERE school_id = '8f209622-6848-4388-a885-95fba51fb873'")}

    # ONE dict per entry, shared by both indexes - a prior version built these
    # from separate dict(r) copies of the same row, so mutating one during a
    # move never showed up in the other and "AFTER" silently reported the
    # unmodified "BEFORE" state despite moves reporting as applied.
    entry = {r["entry_id"]: dict(r) for r in rows}
    by_class = defaultdict(list)      # class_code -> [entry dicts]
    for e in entry.values():
        by_class[e["class_code"]].append(e)

    students_of_entry = defaultdict(set)
    entries_of_student = defaultdict(set)
    for l in links:
        students_of_entry[l["timetable_entry_id"]].add(l["student_id"])
        entries_of_student[l["student_id"]].add(l["timetable_entry_id"])

    students_of_class = defaultdict(set)
    for code, ents in by_class.items():
        for e in ents:
            students_of_class[code] |= students_of_entry[e["entry_id"]]

    def classes_of_student(sid):
        codes = set()
        for eid in entries_of_student[sid]:
            codes.add(entry[eid]["class_code"])
        return codes

    # Busy trackers, derived from the real placement, kept in sync as we move things.
    teacher_busy = defaultdict(set)   # teacher_id -> {(day,period)}
    room_busy = defaultdict(set)
    class_slots = defaultdict(set)    # class_code -> {(day,period)}
    for code, ents in by_class.items():
        for e in ents:
            slot = (e["day_number"], e["period_number"])
            teacher_busy[e["teacher_id"]].add(slot)
            room_busy[e["room_id"]].add(slot)
            class_slots[code].add(slot)

    def student_slots(sid):
        return {(entry[eid]["day_number"], entry[eid]["period_number"])
                for eid in entries_of_student[sid]}

    def gaps_for(sid):
        occ = {p for (d, p) in student_slots(sid)}
        by_day = defaultdict(set)
        for (d, p) in student_slots(sid):
            by_day[d].add(p)
        found = []
        for d, periods in by_day.items():
            lo, hi = min(periods), max(periods)
            for p in SLOTS_ORDER:
                if p[0] != d:
                    continue
            span = [pp for (dd, pp) in SLOTS if dd == d and lo <= pp <= hi]
            for pp in span:
                if pp not in periods:
                    found.append((d, pp))
        return found

    SLOTS_ORDER = SLOTS  # placeholder not used above, cleanup

    junior = sorted({sid for sid, yr in student_names.items() if yr in ("Year 7", "Year 8", "Year 9")})
    all_gaps = []
    for sid in junior:
        for g in gaps_for(sid):
            all_gaps.append((sid, g))
    print(f"BEFORE: {len(all_gaps)} gap instances across {len(junior)} Y7-9 students")

    fixed = 0
    unfixed = 0
    reject_reasons = defaultdict(int)
    no_candidates_at_all = 0
    for sid, (day, period) in all_gaps:
        tried_any_slot = False
        target = (day, period)
        done = False
        for code in sorted(classes_of_student(sid)):
            if target in class_slots[code]:
                continue  # already meets then, weird but skip
            # count this class's periods already on `day`
            same_day = sum(1 for (d, p) in class_slots[code] if d == day)
            if same_day >= 2:
                continue
            for (d2, p2) in sorted(class_slots[code]):
                if d2 == day:
                    continue
                # find one representative entry for this class on (d2,p2) for
                # teacher/room/campus
                rep = next(e for e in by_class[code]
                          if e["day_number"] == d2 and e["period_number"] == p2)
                teacher, room, campus = rep["teacher_id"], rep["room_id"], rep["campus_id"]

                tried_any_slot = True
                if target in teacher_busy[teacher]:
                    reject_reasons['teacher_busy'] += 1
                    continue
                if target in room_busy[room]:
                    reject_reasons['room_busy'] += 1
                    continue
                # every OTHER student in this class must also be legal at target:
                # no clash with any of their other classes at that slot.
                ok = True
                for other_sid in sorted(students_of_class[code]):
                    if target in student_slots(other_sid) and other_sid != sid:
                        ok = False
                        break
                if not ok:
                    reject_reasons['other_student_clash'] += 1
                    continue
                # campus consistency: this student's other classes on `day`
                # must already be at the same campus (avoid inventing a
                # cross-campus jump the travel layer never validated).
                same_day_campuses = {entry[eid]["campus_id"]
                                     for eid in entries_of_student[sid]
                                     if entry[eid]["day_number"] == day}
                if same_day_campuses and campus not in same_day_campuses:
                    reject_reasons['campus_mismatch'] += 1
                    continue

                # --- apply the move (in-memory) ---
                teacher_busy[teacher].discard((d2, p2))
                teacher_busy[teacher].add(target)
                room_busy[room].discard((d2, p2))
                room_busy[room].add(target)
                class_slots[code].discard((d2, p2))
                class_slots[code].add(target)
                for e in by_class[code]:
                    if e["day_number"] == d2 and e["period_number"] == p2:
                        e["day_number"], e["period_number"] = target
                # keep entry[] and entries_of_student in sync via e mutation
                # (entry dict is the same object referenced in entry{})
                fixed += 1
                done = True
                break
            if done:
                break
        if not done:
            unfixed += 1
            if not tried_any_slot:
                no_candidates_at_all += 1

    remaining = []
    for sid in junior:
        for g in gaps_for(sid):
            remaining.append((sid, g))
    print(f"no candidate slot even attempted (student has no other-day class "
          f"free to move): {no_candidates_at_all}")
    print("rejection reasons among attempted moves:")
    for k, v in sorted(reject_reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {k:20} {v}")
    print(f"moves applied: {fixed}  (attempted {len(all_gaps)}, no legal move found for {unfixed})")
    print(f"AFTER:  {len(remaining)} gap instances remain")

    await db.disconnect()


asyncio.run(main())

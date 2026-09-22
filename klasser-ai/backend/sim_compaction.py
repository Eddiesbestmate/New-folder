"""
Measure same-day compaction against the real, currently-completed timetable.

Instead of hunting cross-day for a class willing to relocate into a gap
(collision-prone: most slots are already busy for someone in a 25-30 person
class), pull the class from the LATEST occupied period of that same day into
the gap. This never needs a different day's room/teacher to cooperate - only
the gap slot itself needs to be free for that class's own teacher and room -
and it genuinely shrinks the day rather than just relocating the hole,
because removing the tail can never create a new internal gap.

Processed latest-gap-first per (student, day) so repeated gaps in one day
compact correctly instead of undoing each other.
"""
import asyncio
from collections import defaultdict
from models import database as db

V = "1e076072-b652-4e20-bb2a-aa2ba0c15878"   # latest completed version
TEACHING = [(d, p) for d in range(1, 11) for p in (1, 2, 4, 5, 7, 8, 9)]


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
    students = {r["id"]: r["year_group"] for r in await db.fetch(
        "SELECT id, year_group FROM students "
        "WHERE school_id = '8f209622-6848-4388-a885-95fba51fb873'")}

    entry = {r["entry_id"]: dict(r) for r in rows}
    by_class = defaultdict(list)
    for e in entry.values():
        by_class[e["class_code"]].append(e)

    entries_of_student = defaultdict(set)
    students_of_entry = defaultdict(set)
    for l in links:
        entries_of_student[l["student_id"]].add(l["timetable_entry_id"])
        students_of_entry[l["timetable_entry_id"]].add(l["student_id"])

    students_of_class = defaultdict(set)
    for code, ents in by_class.items():
        for e in ents:
            students_of_class[code] |= students_of_entry[e["entry_id"]]

    teacher_busy = defaultdict(set)
    room_busy = defaultdict(set)
    class_slots = defaultdict(set)
    for code, ents in by_class.items():
        for e in ents:
            slot = (e["day_number"], e["period_number"])
            teacher_busy[e["teacher_id"]].add(slot)
            room_busy[e["room_id"]].add(slot)
            class_slots[code].add(slot)

    def student_slots(sid):
        return {(entry[eid]["day_number"], entry[eid]["period_number"])
                for eid in entries_of_student[sid]}

    def classes_of(sid):
        return {entry[eid]["class_code"] for eid in entries_of_student[sid]}

    def gaps_for(sid):
        occ = student_slots(sid)
        by_day = defaultdict(set)
        for d, p in occ:
            by_day[d].add(p)
        found = []
        for d, periods in by_day.items():
            lo, hi = min(periods), max(periods)
            for dd, pp in TEACHING:
                if dd == d and lo <= pp <= hi and pp not in periods:
                    found.append((d, pp))
        return found

    junior = sorted(sid for sid, yr in students.items()
                    if yr in ("Year 7", "Year 8", "Year 9"))
    before = sum(len(gaps_for(sid)) for sid in junior)
    before_days = sum(1 for sid in junior if gaps_for(sid))
    print(f"BEFORE: {before} gap instances, {before_days} student-days affected "
          f"(of {len(junior)} Y7-9 students)")

    def try_fill(sid, target):
        day = target[0]
        if target in student_slots(sid):
            return False
        for code in sorted(classes_of(sid)):
            rep = next(e for e in by_class[code]
                      if (e["day_number"], e["period_number"]) in class_slots[code])
            teacher, room, campus = rep["teacher_id"], rep["room_id"], rep["campus_id"]
            if target in class_slots[code]:
                continue
            cands = sorted(class_slots[code],
                           key=lambda s: (s[0] != day, -s[1] if s[0] == day else 0, s))
            for origin in cands:
                same_day = origin[0] == day
                if not same_day:
                    if sum(1 for d, _ in class_slots[code] if d == day) >= 2:
                        continue
                if target in teacher_busy[teacher] or target in room_busy[room]:
                    continue
                if any(o != sid and target in student_slots(o)
                      for o in sorted(students_of_class[code])):
                    continue
                same_day_campuses = {
                    entry[eid]["campus_id"] for eid in entries_of_student[sid]
                    if entry[eid]["day_number"] == day
                }
                if same_day_campuses and campus not in same_day_campuses:
                    continue

                teacher_busy[teacher].discard(origin); teacher_busy[teacher].add(target)
                room_busy[room].discard(origin); room_busy[room].add(target)
                class_slots[code].discard(origin); class_slots[code].add(target)
                for e in by_class[code]:
                    if (e["day_number"], e["period_number"]) == origin:
                        e["day_number"], e["period_number"] = target
                return True
        return False

    gaps_by_day = defaultdict(list)
    for sid in junior:
        for d, p in gaps_for(sid):
            gaps_by_day[(sid, d)].append(p)

    fixed = 0
    for (sid, day), periods in gaps_by_day.items():
        for target_period in sorted(set(periods), reverse=True):
            if try_fill(sid, (day, target_period)):
                fixed += 1

    after = sum(len(gaps_for(sid)) for sid in junior)
    after_days = sum(1 for sid in junior if gaps_for(sid))
    print(f"moves applied: {fixed}")
    print(f"AFTER:  {after} gap instances, {after_days} student-days affected")
    await db.disconnect()


asyncio.run(main())

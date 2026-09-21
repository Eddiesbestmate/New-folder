"""
Generation pipeline - the nine layers that turn school data into a timetable.

Structure follows PIPELINE.md. The division of labour is the important part:

  AI decides       - how to group students, which slot a class takes, which
                     teacher and room, who covers which duty
  Deterministic    - what the AI is allowed to choose from, whether its answer
                     is legal, and everything written to the database

The AI never writes to a registry. Every layer validates the model's response
before committing, so a bad response fails that layer rather than corrupting the
solution. Registries are the shared state each later layer reads.

Chunking keeps each prompt small enough to be reliable: by year level for class
formation, by campus for slots and rooms, by subject group for teachers, by day
for duties.
"""

import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Optional

from models import database as db
from services import ai_cluster, deterministic
from services import settings as settings_service

log = logging.getLogger("klasser.pipeline")

# Stage name -> share of the progress bar. Ordered.
STAGES: list[tuple[str, str, int]] = [
    ("layer1_subject_map", "Mapping subjects to campuses", 5),
    ("layer2_class_groups", "Forming class groups", 20),
    ("layer3_period_slots", "Assigning periods", 20),
    ("layer4_rooms", "Assigning rooms", 12),
    ("layer5_teachers", "Assigning teachers", 18),
    ("layer6_duties", "Assigning duties", 8),
    ("layer7_transport", "Solving transport", 5),
    ("layer8_bus_duties", "Assigning bus duties", 4),
    ("layer9_mentor", "Assigning mentor groups", 4),
    ("write_solution", "Saving the timetable", 2),
    ("layer10_validation", "Validating the timetable", 2),
]


class PipelineError(Exception):
    def __init__(self, message: str, *, stage: str = "", school_fault: bool = False):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.school_fault = school_fault


@dataclass
class Context:
    """Everything a layer needs, loaded once per attempt."""
    attempt_id: str
    timetable_id: str
    school_id: str
    layout_id: str
    locked_model: Optional[str] = None

    campuses: list[dict] = field(default_factory=list)
    teachers: list[dict] = field(default_factory=list)
    students: list[dict] = field(default_factory=list)
    rooms: list[dict] = field(default_factory=list)
    subjects: list[dict] = field(default_factory=list)
    periods: list[dict] = field(default_factory=list)
    duty_slots: list[dict] = field(default_factory=list)
    enrolments: dict[str, list[str]] = field(default_factory=dict)  # student -> subjects
    default_min: int = 4
    default_max: int = 5

    subject_map: dict = field(default_factory=dict)
    groups: list[dict] = field(default_factory=list)  # in-memory class groups
    transport: list[dict] = field(default_factory=list)
    mentor: list[dict] = field(default_factory=list)
    # Varies tie-breaking between retries. A retry that makes exactly the same
    # choices as the attempt before it fails in exactly the same way.
    seed: int = 1

    @property
    def teaching_periods(self) -> list[dict]:
        return [p for p in self.periods if p["period_type"] == "teaching"]

    @property
    def days(self) -> list[int]:
        return sorted({p["day_number"] for p in self.periods})

    def subject_setting(self, subject: str, year_level: Optional[str]) -> dict:
        for s in self.subjects:
            if s["subject"] == subject and s["year_level"] == year_level:
                return s
        for s in self.subjects:
            if s["subject"] == subject and s["year_level"] is None:
                return s
        return {}

    def periods_for(self, subject: str, year_level: Optional[str]) -> tuple[int, int]:
        s = self.subject_setting(subject, year_level)
        lo = s.get("min_periods_per_cycle") or self.default_min
        hi = s.get("max_periods_per_cycle") or self.default_max
        return lo, max(lo, hi)

    def room_type_for(self, subject: str,
                      year_level: Optional[str]) -> Optional[str]:
        """
        The room type this subject needs, or None if any room will do.

        Layer 3 needs this as well as layer 4: how many rooms of a type exist
        limits how many of those classes can run at once, whatever the period
        allocation would otherwise prefer.
        """
        return self.subject_setting(subject, year_level).get("default_room_type")


# --- Loading -----------------------------------------------------------------

async def load_context(attempt_id: str) -> Context:
    attempt = await db.fetchrow("""
        SELECT ga.id, ga.timetable_id, ga.locked_model,
               t.school_id, t.layout_id
        FROM generation_attempts ga
        JOIN timetables t ON t.id = ga.timetable_id
        WHERE ga.id = $1
    """, attempt_id)
    if attempt is None:
        raise PipelineError("Generation attempt not found")

    ctx = Context(
        attempt_id=str(attempt["id"]),
        timetable_id=str(attempt["timetable_id"]),
        school_id=str(attempt["school_id"]),
        layout_id=str(attempt["layout_id"]),
        locked_model=attempt["locked_model"],
    )

    ctx.campuses = [dict(r) for r in await db.fetch(
        "SELECT id, name FROM campuses WHERE school_id = $1 ORDER BY created_at",
        ctx.school_id)]
    # The school-wide default comes from settings, not a hardcoded 10. It was
    # hardcoded here while `duty_default_max_per_cycle` sat in the settings
    # table doing nothing - a dev portal control that silently had no effect.
    default_duties = await settings_service.get_int(
        "duty_default_max_per_cycle", 10)

    ctx.teachers = [dict(r) for r in await db.fetch("""
        SELECT t.id, t.full_name, t.subjects, t.campus_id, t.max_blocks,
               coalesce(t.max_duties_per_cycle, $2) AS max_duties,
               EXISTS (SELECT 1 FROM teacher_duty_exemptions e
                       WHERE e.teacher_id = t.id) AS duty_exempt
        FROM teachers t WHERE t.school_id = $1 ORDER BY t.surname, t.first_name
    """, ctx.school_id, default_duties)]
    ctx.students = [dict(r) for r in await db.fetch("""
        SELECT id, full_name, year_group, campus_id, gender, ability_band
        FROM students WHERE school_id = $1 ORDER BY year_group, full_name
    """, ctx.school_id)]
    ctx.rooms = [dict(r) for r in await db.fetch("""
        SELECT id, name, campus_id, capacity, room_type, allows_split
        FROM rooms WHERE school_id = $1 ORDER BY name
    """, ctx.school_id)]
    ctx.subjects = [dict(r) for r in await db.fetch("""
        SELECT subject, year_level, hard_max_size, soft_max_size, min_size,
               default_room_type, campus_locked_id, is_double_period, code_prefix,
               min_periods_per_cycle, max_periods_per_cycle
        FROM subject_settings WHERE school_id = $1
    """, ctx.school_id)]
    ctx.periods = [dict(r) for r in await db.fetch("""
        SELECT id, day_number, period_number, period_type, label,
               start_time, end_time
        FROM layout_periods WHERE layout_id = $1
        ORDER BY day_number, period_number
    """, ctx.layout_id)]
    ctx.duty_slots = [dict(r) for r in await db.fetch("""
        SELECT lds.id, lds.day_number, lds.timing, lds.campus_id, lds.min_staff,
               lds.start_time, lds.end_time,
               dt.name AS duty_name, dt.is_bus_duty
        FROM layout_duty_slots lds
        JOIN duty_types dt ON dt.id = lds.duty_type_id
        WHERE lds.layout_id = $1
        ORDER BY lds.day_number
    """, ctx.layout_id)]

    for row in await db.fetch("""
        SELECT student_id, subject FROM student_subjects WHERE school_id = $1
    """, ctx.school_id):
        ctx.enrolments.setdefault(str(row["student_id"]), []).append(row["subject"])

    ctx.default_min = await settings_service.get_int("default_min_periods_per_cycle", 4)
    ctx.default_max = await settings_service.get_int("default_max_periods_per_cycle", 5)
    return ctx


# --- Progress and state ------------------------------------------------------

async def emit(attempt_id: str, event_type: str, message: str,
               detail: Optional[dict] = None) -> None:
    await db.execute("""
        INSERT INTO generation_events (attempt_id, event_type, message, detail)
        VALUES ($1, $2, $3, $4)
    """, attempt_id, event_type, message,
        json.dumps(detail) if detail else None)


async def set_progress(attempt_id: str, stage: str, chunk: Optional[str],
                       pct: int) -> None:
    await db.execute("""
        UPDATE generation_jobs
        SET current_stage = $2, current_chunk = $3, progress_pct = $4,
            status = 'running', updated_at = now()
        WHERE attempt_id = $1
    """, attempt_id, stage, chunk, pct)


async def stage_start(attempt_id: str, stage: str) -> None:
    await db.execute("""
        INSERT INTO pipeline_state (attempt_id, stage, status)
        VALUES ($1, $2, 'in_progress')
    """, attempt_id, stage)


async def stage_done(attempt_id: str, stage: str, output: Optional[dict] = None) -> None:
    await db.execute("""
        UPDATE pipeline_state
        SET status = 'complete', output_data = $3, completed_at = now()
        WHERE attempt_id = $1 AND stage = $2 AND status = 'in_progress'
    """, attempt_id, stage, json.dumps(output) if output else None)


async def stage_failed(attempt_id: str, stage: str, reason: str) -> None:
    await db.execute("""
        UPDATE pipeline_state
        SET status = 'failed', failure_reason = $3, completed_at = now()
        WHERE attempt_id = $1 AND stage = $2 AND status = 'in_progress'
    """, attempt_id, stage, reason[:2000])


# --- AI helper ---------------------------------------------------------------

def parse_json(text: str, stage: str) -> dict:
    """Models sometimes wrap JSON in prose or code fences. Recover what we can."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise PipelineError(
        f"{stage}: the model did not return usable JSON", stage=stage)


# Only the allocation layers participate in model locking. PIPELINE.md locks the
# model that does the heavy allocation so an attempt does not swap mid-chunk; it
# says nothing about interpretation or validation, which have their own
# assignments and their own providers.
LOCKED_TASKS = {
    "task_class_group_formation",
    "task_block_campus_primary",
    "task_teacher_primary",
}


async def ask(ctx: Context, task_key: str, prompt: str, stage: str,
              system: Optional[str] = None) -> dict:
    """
    One AI call, returning parsed JSON.

    locked_model is passed only for the allocation tasks. Applying it to every
    call made the lock override each task's configured model - so a run with
    task_interpret set to Gemini still called Magistral, and changing a task
    assignment appeared to do nothing.
    """
    text = await ai_cluster.call(
        task_key, prompt, system=system, json_mode=True,
        timetable_id=ctx.timetable_id, attempt_id=ctx.attempt_id,
        stage=stage,
        locked_model=ctx.locked_model if task_key in LOCKED_TASKS else None)
    return parse_json(text, stage)


async def lock_model(ctx: Context, alias: str) -> None:
    """Pin the attempt to one model for every remaining chunk (PIPELINE.md)."""
    if ctx.locked_model == alias:
        return
    ctx.locked_model = alias
    await db.execute(
        "UPDATE generation_attempts SET locked_model = $2 WHERE id = $1",
        ctx.attempt_id, alias)


# --- Layer 1: subject and campus mapping -------------------------------------

async def layer1_subject_map(ctx: Context) -> None:
    subjects = sorted({s["subject"] for s in ctx.subjects}
                      | {sub for subs in ctx.enrolments.values() for sub in subs})

    prompt = f"""Map each subject to how it should be timetabled.

Campuses: {json.dumps([c["name"] for c in ctx.campuses])}
Room types available: {json.dumps(sorted({r["room_type"] for r in ctx.rooms if r["room_type"]}))}

Subjects and their configured settings:
{json.dumps([
    {
        "subject": s,
        "settings": {
            k: v for k, v in (ctx.subject_setting(s, None) or {}).items()
            if k in ("default_room_type", "is_double_period", "campus_locked_id")
        },
    } for s in subjects
], indent=2, default=str)}

Return ONLY JSON:
{{"subject_map": [
  {{"subject": "...", "room_type": "type or null",
    "campus": "campus name or flexible", "is_double": false}}
]}}"""

    # The school's own subject_settings override anything the model says, so a
    # failure here costs only the campus hint. Falling back beats failing the
    # whole generation at the first step.
    try:
        result = await ask(ctx, "task_interpret", prompt, "layer1_subject_map")
        entries = result.get("subject_map") or []
    except (ai_cluster.AIError, PipelineError) as exc:
        log.warning("Subject mapping unavailable (%s) - using configured settings", exc)
        await emit(ctx.attempt_id, "chunk_complete",
                   "Subject mapping used the school's own settings")
        entries = []

    # Deterministic settings win over anything the model says.
    by_subject = {}
    for entry in entries:
        name = entry.get("subject")
        if name not in subjects:
            continue
        configured = ctx.subject_setting(name, None)
        by_subject[name] = {
            "room_type": configured.get("default_room_type") or entry.get("room_type"),
            "campus": entry.get("campus", "flexible"),
            "is_double": bool(configured.get("is_double_period")
                              or entry.get("is_double")),
        }

    for name in subjects:
        by_subject.setdefault(name, {
            "room_type": (ctx.subject_setting(name, None) or {}).get("default_room_type"),
            "campus": "flexible",
            "is_double": bool((ctx.subject_setting(name, None) or {}).get("is_double_period")),
        })

    ctx.subject_map = by_subject
    await stage_done(ctx.attempt_id, "layer1_subject_map",
                     {"subjects": len(by_subject)})


# --- Layer 2: class group formation ------------------------------------------

def make_class_code(ctx: Context, subject: str, year_level: Optional[str],
                    index: int, used: set[str]) -> str:
    """Deterministic, so codes are always unique regardless of the model."""
    setting = ctx.subject_setting(subject, year_level)
    prefix = (setting.get("code_prefix")
              or re.sub(r"[^A-Za-z]", "", subject)[:3].upper() or "SUB")
    year = re.sub(r"[^0-9]", "", year_level or "") or ""
    base = f"{year}{prefix}"
    code = f"{base}{index}"
    n = index
    while code in used:
        n += 1
        code = f"{base}{n}"
    used.add(code)
    return code


async def layer2_class_groups(ctx: Context) -> None:
    year_levels = sorted({s["year_group"] for s in ctx.students if s["year_group"]})
    used_codes: set[str] = set()
    total = 0

    for i, year in enumerate(year_levels):
        await set_progress(ctx.attempt_id, "layer2_class_groups",
                           f"{year} ({i + 1}/{len(year_levels)})",
                           _pct("layer2_class_groups", i / max(1, len(year_levels))))

        cohort = [s for s in ctx.students if s["year_group"] == year]
        by_subject: dict[str, list[dict]] = defaultdict(list)
        for student in cohort:
            for subject in ctx.enrolments.get(str(student["id"]), []):
                by_subject[subject].append(student)

        for subject, taking in sorted(by_subject.items()):
            if not taking:
                continue
            setting = ctx.subject_setting(subject, year)
            hard = setting.get("hard_max_size") or 30
            soft = setting.get("soft_max_size") or hard

            # Deterministic code decides how many groups are needed; the model
            # only decides who goes in which, which is the judgement call.
            needed = max(1, -(-len(taking) // soft))
            roster = [
                {"i": n, "name": s["full_name"], "gender": s["gender"],
                 "band": s["ability_band"]}
                for n, s in enumerate(taking)
            ]

            assignment = await _ask_groups(ctx, subject, year, roster, needed,
                                           hard, soft)

            for gi, members in enumerate(assignment, start=1):
                code = make_class_code(ctx, subject, year, gi, used_codes)
                students = [taking[n] for n in members if 0 <= n < len(taking)]
                ctx.groups.append({
                    "code": code, "subject": subject, "year_level": year,
                    "students": students,
                    "campus_id": _dominant_campus(students),
                })
                total += 1

    await _write_groups(ctx)
    check = await deterministic.check_class_groups(ctx.attempt_id)
    if not check.passed:
        raise PipelineError(check.failures[0], stage="layer2_class_groups",
                            school_fault=True)

    await stage_done(ctx.attempt_id, "layer2_class_groups", {"groups": total})


async def _ask_groups(ctx: Context, subject: str, year: str, roster: list[dict],
                      needed: int, hard: int, soft: int) -> list[list[int]]:
    if needed == 1:
        return [[r["i"] for r in roster]]

    balancing = await db.fetchrow(
        "SELECT * FROM balancing_settings WHERE school_id = $1", ctx.school_id)

    prompt = f"""Split these {len(roster)} students into exactly {needed} balanced
{subject} classes for {year}.

Students (i is the index you must use):
{json.dumps(roster, indent=2)}

Rules:
- Every student appears exactly once, across all groups
- No group may exceed {hard} students
- Aim for about {soft} per group
- Balance {'gender, ' if balancing and balancing['balance_gender'] else ''}
  {'ability band, ' if balancing and balancing['balance_ability'] else ''}size

Return ONLY JSON:
{{"groups": [[0, 3, 7], [1, 2, 4]]}}"""

    try:
        result = await ask(ctx, "task_class_group_formation", prompt,
                           "layer2_class_groups")
        groups = result.get("groups") or []
        cleaned = _repair_partition(groups, len(roster), needed, hard)
        if cleaned:
            return cleaned
        log.warning("Group split for %s %s was unusable - falling back to even split",
                    year, subject)
    except (ai_cluster.AIError, PipelineError) as exc:
        log.warning("Group split for %s %s failed (%s) - even split", year, subject, exc)

    # An even round-robin split always satisfies the hard constraints.
    out: list[list[int]] = [[] for _ in range(needed)]
    for r in roster:
        out[r["i"] % needed].append(r["i"])
    return out


def _repair_partition(groups: list, size: int, needed: int,
                      hard: int) -> Optional[list[list[int]]]:
    """
    Accept the model's split only if it is a genuine partition.

    Duplicates and omissions are the two failure modes that would silently
    corrupt enrolment, so anything not exactly covering every student once is
    rejected rather than patched.
    """
    if not isinstance(groups, list) or not groups:
        return None
    out: list[list[int]] = []
    seen: set[int] = set()
    for g in groups:
        if not isinstance(g, list):
            return None
        members = []
        for n in g:
            if not isinstance(n, int) or n < 0 or n >= size or n in seen:
                return None
            seen.add(n)
            members.append(n)
        if len(members) > hard:
            return None
        out.append(members)
    if len(seen) != size:
        return None
    return [g for g in out if g] or None


def _dominant_campus(students: list[dict]) -> Optional[str]:
    counts: dict[Any, int] = defaultdict(int)
    for s in students:
        if s.get("campus_id"):
            counts[s["campus_id"]] += 1
    return max(counts, key=counts.get) if counts else None


async def _write_groups(ctx: Context) -> None:
    """
    Write the class groups and their members.

    Membership is written with executemany rather than a statement per student:
    a 700-student school produces several thousand rows, and one round trip each
    exceeds the connection's command timeout long before it finishes.
    """
    async with db.transaction() as conn:
        for group in ctx.groups:
            group_id = await conn.fetchval("""
                INSERT INTO class_groups
                    (attempt_id, school_id, class_code, subject, year_level,
                     campus_id, student_count, formation_type)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'rule') RETURNING id
            """, ctx.attempt_id, ctx.school_id, group["code"], group["subject"],
                group["year_level"], group["campus_id"], len(group["students"]))
            group["id"] = str(group_id)

        members = [
            (group["id"], student["id"])
            for group in ctx.groups
            for student in group["students"]
        ]
        if members:
            await conn.executemany("""
                INSERT INTO class_group_students
                    (class_group_id, student_id, entry_type)
                VALUES ($1, $2, 'rule_assigned')
                ON CONFLICT DO NOTHING
            """, members)


# --- Layer 3: period slots ---------------------------------------------------

def _conflict_graph(ctx: Context) -> dict[str, set[str]]:
    """Class groups sharing a student can never occupy the same slot."""
    by_student: dict[Any, list[str]] = defaultdict(list)
    for group in ctx.groups:
        for student in group["students"]:
            by_student[student["id"]].append(group["code"])

    graph: dict[str, set[str]] = {g["code"]: set() for g in ctx.groups}
    for codes in by_student.values():
        for a in codes:
            for b in codes:
                if a != b:
                    graph[a].add(b)
    return graph


async def layer3_period_slots(ctx: Context) -> None:
    slots = [(p["day_number"], p["period_number"]) for p in ctx.teaching_periods]
    if not slots:
        raise PipelineError("The layout has no teaching periods",
                            stage="layer3_period_slots", school_fault=True)

    graph = _conflict_graph(ctx)
    assigned: dict[str, list[tuple[int, int]]] = {}
    slot_load: dict[tuple[int, int], int] = defaultdict(int)

    wanted: dict[str, tuple[int, int]] = {}
    for group in ctx.groups:
        wanted[group["code"]] = ctx.periods_for(group["subject"],
                                                group["year_level"])

    # Collect the model's suggestions per campus, as PIPELINE.md chunks it.
    proposals: dict[str, list[tuple[int, int]]] = {}
    campuses = ctx.campuses or [{"id": None, "name": "All"}]
    for i, campus in enumerate(campuses):
        codes = [g["code"] for g in ctx.groups if g["campus_id"] == campus["id"]]
        if not codes and i == 0:
            codes = [g["code"] for g in ctx.groups]
        if not codes:
            continue
        await set_progress(ctx.attempt_id, "layer3_period_slots",
                           f"{campus['name']} ({i + 1}/{len(campuses)})",
                           _pct("layer3_period_slots", i / max(1, len(campuses))))
        proposals.update(
            await _ask_slots(ctx, codes, wanted, slots, graph, assigned))

    # Assign globally, most constrained first.
    #
    # Processing in arbitrary order fails on a dense conflict graph: classes
    # handled late find every slot taken by a class sharing their students, and
    # the run dies with "could only be given 0 of 3 periods". Ordering by how
    # many other classes a group clashes with - the standard graph-colouring
    # heuristic - places the hardest classes while there is still room.
    # Most constrained first. The seed only breaks ties between equally
    # constrained classes, so a retry explores a different ordering without
    # abandoning the heuristic that makes the placement work at all.
    order = sorted(
        ctx.groups,
        key=lambda g: (-len(graph.get(g["code"], ())), -wanted[g["code"]][0],
                       _jitter(g["code"], ctx.seed)),
    )

    # What each class needs a room of, and how many of each type exist. Layer 4
    # assigns the actual rooms, but it can only succeed if this layer respects
    # how many exist - three PE classes and one gym cannot run at once.
    room_need = {
        g["code"]: ctx.room_type_for(g["subject"], g["year_level"])
        for g in ctx.groups
    }
    room_supply: dict[str, int] = defaultdict(int)
    for room in ctx.rooms:
        if room["room_type"]:
            room_supply[room["room_type"]] += 1
    slot_rooms: dict[tuple[int, int], dict[str, int]] = defaultdict(
        lambda: defaultdict(int))

    for group in order:
        code = group["code"]
        lo, hi = wanted[code]
        chosen: list[tuple[int, int]] = []

        # Honour the model's suggestion where it is legal.
        for slot in proposals.get(code, []):
            if len(chosen) >= hi:
                break
            if _slot_ok(code, slot, graph, assigned, chosen,
                        room_need, room_supply, slot_rooms):
                chosen.append(slot)

        # Fill the rest with the least contended slot, preferring a day this
        # class is not already on so its periods spread across the cycle.
        while len(chosen) < lo:
            used_days = {d for d, _ in chosen}
            candidates = [
                s for s in slots
                if _slot_ok(code, s, graph, assigned, chosen,
                            room_need, room_supply, slot_rooms)
            ]
            if not candidates:
                needed = room_need.get(code)
                if needed and room_supply.get(needed):
                    raise PipelineError(
                        f"{code} needs {lo} periods in a {needed}, but the "
                        f"school has {room_supply[needed]} of them and every "
                        "slot is already taken by another class that needs one. "
                        f"Add a {needed}, or reduce how often the subjects "
                        "using it meet.",
                        stage="layer3_period_slots", school_fault=True)
                raise PipelineError(
                    f"{code} needs {lo} periods but only {len(chosen)} slots are "
                    "free once every class sharing its students is placed. The "
                    "cycle is too short for the subject load, or classes are too "
                    "large to split across periods.",
                    stage="layer3_period_slots", school_fault=True)

            candidates.sort(key=lambda s: (s[0] in used_days, slot_load[s], s))
            chosen.append(candidates[0])

        assigned[code] = chosen
        needed = room_need.get(code)
        for slot in chosen:
            slot_load[slot] += 1
            if needed:
                slot_rooms[slot][needed] += 1

    await _write_slots(ctx, assigned)

    spread = sum(len(v) for v in assigned.values())
    await stage_done(ctx.attempt_id, "layer3_period_slots",
                     {"slots": spread, "classes": len(assigned)})


def _slot_ok(code: str, slot: tuple[int, int], graph: dict[str, set[str]],
             assigned: dict[str, list[tuple[int, int]]],
             chosen: list[tuple[int, int]],
             room_need: Optional[dict[str, str]] = None,
             room_supply: Optional[dict[str, int]] = None,
             slot_rooms: Optional[dict] = None) -> bool:
    """
    Whether this class can take this slot.

    Two constraints, not one. Students sharing a class is the obvious one. The
    second is room-type scarcity: a school with three PE classes and one gym
    cannot run two of them at once, however free the slot looks here.

    Layer 4 assigns rooms, so layer 3 used to place periods without knowing any
    of this - and handed layer 4 an impossible problem. A live run failed four
    times in a row on exactly that: "Every suitable room is already busy when
    12PED1 meets", after this function put 12PED1 and 10PED1 in the same
    period with one gym between them. Retrying cannot fix it, because the
    constraint was never modelled.
    """
    if slot in chosen:
        return False
    for other in graph.get(code, ()):  # a class sharing students
        if slot in assigned.get(other, ()):
            return False

    if room_need and room_supply is not None and slot_rooms is not None:
        needed = room_need.get(code)
        if needed:
            supply = room_supply.get(needed, 0)
            if supply and slot_rooms[slot][needed] >= supply:
                return False

    return True


def _free_slots(slots: list[tuple[int, int]], load: dict, capacity: int):
    """Least-loaded slots first, so classes spread across the cycle."""
    return sorted(slots, key=lambda s: (load[s], s))


async def _ask_slots(ctx: Context, codes: list[str], wanted: dict,
                     slots: list, graph: dict,
                     assigned: dict) -> dict[str, list[tuple[int, int]]]:
    compact = {
        c: {"needs": wanted[c][0], "up_to": wanted[c][1],
            "clashes_with": sorted(graph.get(c, set()))[:40]}
        for c in codes
    }
    taken = {c: [list(s) for s in v] for c, v in assigned.items() if v}

    prompt = f"""Assign period slots to classes.

Available slots as [day, period]:
{json.dumps([list(s) for s in slots])}

Classes needing slots:
{json.dumps(compact, indent=2)}

Already assigned (must not clash):
{json.dumps(taken, indent=2)}

Rules:
- Two classes that clash_with each other must never share a slot
- Give each class at least "needs" slots, at most "up_to"
- Spread each class across different days where possible

Return ONLY JSON:
{{"assignments": {{"11COM1": [[1,3],[2,3]], "11MAT1": [[1,4]]}}}}"""

    try:
        result = await ask(ctx, "task_block_campus_primary", prompt,
                           "layer3_period_slots")
        raw = result.get("assignments") or {}
        out: dict[str, list[tuple[int, int]]] = {}
        for code, pairs in raw.items():
            if code not in codes or not isinstance(pairs, list):
                continue
            out[code] = [
                (int(p[0]), int(p[1])) for p in pairs
                if isinstance(p, (list, tuple)) and len(p) == 2
                and (int(p[0]), int(p[1])) in set(slots)
            ]
        return out
    except (ai_cluster.AIError, PipelineError) as exc:
        log.warning("Slot assignment fell back to deterministic fill: %s", exc)
        return {}


async def _write_slots(ctx: Context,
                       assigned: dict[str, list[tuple[int, int]]]) -> None:
    period_by_slot = {
        (p["day_number"], p["period_number"]): p["id"] for p in ctx.periods
    }

    slot_rows = []
    registry_rows = []
    for group in ctx.groups:
        for day, number in assigned.get(group["code"], []):
            period_id = period_by_slot.get((day, number))
            slot_rows.append((ctx.attempt_id, group["id"], day, number, period_id))
            for student in group["students"]:
                registry_rows.append(
                    (ctx.attempt_id, student["id"], period_id, group["id"]))

    # One executemany each. Written per row this is tens of thousands of round
    # trips for a large school, which times out the connection.
    async with db.transaction() as conn:
        if slot_rows:
            await conn.executemany("""
                INSERT INTO class_group_slots
                    (attempt_id, class_group_id, day_number, period_number,
                     layout_period_id)
                VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING
            """, slot_rows)
        if registry_rows:
            await conn.executemany("""
                INSERT INTO allocation_registry_students
                    (attempt_id, student_id, layout_period_id, class_group_id)
                VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING
            """, registry_rows)


def _jitter(key: str, seed: int) -> str:
    """
    A stable per-attempt shuffle key.

    Same seed gives the same order, so a run is reproducible; a different seed
    gives a different order, so a retry actually tries something else.
    """
    return hashlib.md5(f"{seed}:{key}".encode()).hexdigest()


def _pct(stage: str, fraction: float) -> int:
    done = 0
    for name, _, weight in STAGES:
        if name == stage:
            return min(99, int(done + weight * max(0.0, min(1.0, fraction))))
        done += weight
    return done


# --- Layer 4: rooms ----------------------------------------------------------

def rank_rooms(rooms: list[dict], size: int, room_type: Optional[str]) -> list[dict]:
    """
    Rank rooms for a class, excluding any that cannot hold it.

    PIPELINE.md: the AI is never offered an undersized room. Capacity is a hard
    constraint and is enforced here, before the model sees the options.
    """
    out = []
    for room in rooms:
        if room["capacity"] < size:
            continue
        if room_type and room["room_type"] and room["room_type"] != room_type:
            continue
        if room["capacity"] <= size * 1.25:
            rank = "perfect"
        elif room["capacity"] <= size * 1.6:
            rank = "good"
        else:
            rank = "oversized"
        out.append({**room, "rank": rank})

    order = {"perfect": 0, "good": 1, "oversized": 2}
    return sorted(out, key=lambda r: (order[r["rank"]], r["capacity"]))


async def layer4_rooms(ctx: Context) -> None:
    slots_by_group = await _slots_by_group(ctx)
    room_busy: dict[tuple[Any, int, int], str] = {}
    chosen: dict[str, dict] = {}

    for group in sorted(ctx.groups, key=lambda g: -len(g["students"])):
        size = len(group["students"])
        wanted_type = (ctx.subject_map.get(group["subject"]) or {}).get("room_type")
        candidates = rank_rooms(ctx.rooms, size, wanted_type)

        if not candidates and wanted_type:
            # Relax the type rather than fail: a warning is better than no timetable.
            candidates = rank_rooms(ctx.rooms, size, None)
            if candidates:
                await emit(ctx.attempt_id, "chunk_complete",
                           f"No {wanted_type} big enough for {group['code']} - "
                           "used a general room")

        if not candidates:
            raise PipelineError(
                f"No room can hold {group['code']} ({size} students).",
                stage="layer4_rooms", school_fault=True)

        # A class keeps one room across all its meetings (consistency registry).
        pick = None
        for room in candidates:
            if all((room["id"], d, p) not in room_busy
                   for d, p in slots_by_group.get(group["id"], [])):
                pick = room
                break
        if pick is None:
            raise PipelineError(
                f"Every suitable room is already busy when {group['code']} meets.",
                stage="layer4_rooms", school_fault=True)

        for d, p in slots_by_group.get(group["id"], []):
            room_busy[(pick["id"], d, p)] = group["code"]
        chosen[group["id"]] = pick
        group["room_id"] = pick["id"]

    rows = [
        (ctx.attempt_id, room["id"], _period_id(ctx, d, p))
        for group_id, room in chosen.items()
        for d, p in slots_by_group.get(group_id, [])
    ]
    if rows:
        async with db.transaction() as conn:
            await conn.executemany("""
                INSERT INTO allocation_registry_rooms
                    (attempt_id, room_id, layout_period_id)
                VALUES ($1,$2,$3) ON CONFLICT DO NOTHING
            """, rows)

    await stage_done(ctx.attempt_id, "layer4_rooms", {"rooms_used": len(set(
        str(r["id"]) for r in chosen.values()))})


# --- Layer 5: teachers -------------------------------------------------------

async def layer5_teachers(ctx: Context) -> None:
    slots_by_group = await _slots_by_group(ctx)
    teacher_busy: dict[tuple[Any, int, int], str] = {}
    load: dict[Any, int] = defaultdict(int)

    subject_groups: dict[str, list[dict]] = defaultdict(list)
    for group in ctx.groups:
        subject_groups[group["subject"]].append(group)

    for i, (subject, groups) in enumerate(sorted(subject_groups.items())):
        await set_progress(ctx.attempt_id, "layer5_teachers",
                           f"{subject} ({i + 1}/{len(subject_groups)})",
                           _pct("layer5_teachers", i / max(1, len(subject_groups))))

        qualified = [t for t in ctx.teachers if subject in (t["subjects"] or [])]
        if not qualified:
            raise PipelineError(
                f"No teacher is listed as teaching {subject}.",
                stage="layer5_teachers", school_fault=True)

        preference = await _ask_teachers(ctx, subject, groups, qualified, load)

        for group in groups:
            slots = slots_by_group.get(group["id"], [])
            pick = None

            ordered = sorted(
                qualified,
                key=lambda t: (0 if str(t["id"]) == preference.get(group["code"]) else 1,
                               load[t["id"]],
                               _jitter(str(t["id"]), ctx.seed)))
            for teacher in ordered:
                if all((teacher["id"], d, p) not in teacher_busy for d, p in slots):
                    pick = teacher
                    break

            if pick is None:
                raise PipelineError(
                    f"Every {subject} teacher is already busy when "
                    f"{group['code']} meets.",
                    stage="layer5_teachers", school_fault=True)

            for d, p in slots:
                teacher_busy[(pick["id"], d, p)] = group["code"]
            load[pick["id"]] += len(slots)
            group["teacher_id"] = pick["id"]

    teacher_rows = [
        (ctx.attempt_id, group["teacher_id"], _period_id(ctx, d, p))
        for group in ctx.groups
        for d, p in slots_by_group.get(group["id"], [])
        if group.get("teacher_id")
    ]
    consistency_rows = [
        (ctx.attempt_id, group["id"], group["teacher_id"], group["room_id"])
        for group in ctx.groups
        if group.get("teacher_id") and group.get("room_id")
    ]

    async with db.transaction() as conn:
        if teacher_rows:
            await conn.executemany("""
                INSERT INTO allocation_registry_teachers
                    (attempt_id, teacher_id, layout_period_id)
                VALUES ($1,$2,$3) ON CONFLICT DO NOTHING
            """, teacher_rows)
        if consistency_rows:
            await conn.executemany("""
                INSERT INTO class_consistency_registry
                    (attempt_id, class_group_id, teacher_id, room_id)
                VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING
            """, consistency_rows)

    check = await deterministic.check_teacher_assignment(ctx.attempt_id)
    if not check.passed:
        raise PipelineError(check.failures[0], stage="layer5_teachers")

    await stage_done(ctx.attempt_id, "layer5_teachers",
                     {"teachers_used": len(load)})


async def _ask_teachers(ctx: Context, subject: str, groups: list[dict],
                        qualified: list[dict], load: dict) -> dict[str, str]:
    """The model expresses a preference; deterministic code decides."""
    if len(qualified) == 1:
        return {g["code"]: str(qualified[0]["id"]) for g in groups}

    prompt = f"""Choose a teacher for each {subject} class.

Teachers:
{json.dumps([
    {"id": str(t["id"]), "name": t["full_name"],
     "campus": str(t["campus_id"]), "periods_already": load.get(t["id"], 0)}
    for t in qualified
], indent=2)}

Classes:
{json.dumps([
    {"code": g["code"], "year": g["year_level"], "students": len(g["students"]),
     "campus": str(g["campus_id"])}
    for g in groups
], indent=2)}

Balance workload across teachers and keep teachers on their own campus where
possible.

Return ONLY JSON: {{"assignments": {{"11COM1": "<teacher id>"}}}}"""

    try:
        result = await ask(ctx, "task_teacher_primary", prompt, "layer5_teachers")
        valid = {str(t["id"]) for t in qualified}
        return {
            code: tid for code, tid in (result.get("assignments") or {}).items()
            if tid in valid
        }
    except (ai_cluster.AIError, PipelineError) as exc:
        log.warning("Teacher preference unavailable for %s (%s) - balancing by load",
                    subject, exc)
        return {}


# --- Layer 6 and 8: duties ---------------------------------------------------

async def _assign_duties(ctx: Context, bus: bool, stage: str) -> int:
    slots = [d for d in ctx.duty_slots if bool(d["is_bus_duty"]) == bus]
    if not slots:
        await stage_done(ctx.attempt_id, stage, {"assigned": 0})
        return 0

    eligible = [t for t in ctx.teachers if not t["duty_exempt"]]
    if not eligible:
        raise PipelineError("Every teacher is exempt from duties.",
                            stage=stage, school_fault=True)

    teaching_at = await _teacher_busy_map(ctx)
    counts: dict[Any, int] = defaultdict(int)
    for row in await db.fetch("""
        SELECT teacher_id, duties_assigned FROM allocation_registry_duty_counts
        WHERE attempt_id = $1
    """, ctx.attempt_id):
        counts[row["teacher_id"]] = row["duties_assigned"]

    assigned = 0
    shortfalls: list[str] = []
    rows: list[tuple] = []

    # Duties already held by a teacher, as (day, start, end), so a second duty
    # overlapping the first can be refused. Both duty layers share this, since
    # Layer 8's bus duties must not collide with Layer 6's yard duties.
    held: dict[Any, list[tuple[int, Any, Any]]] = defaultdict(list)
    for row in await db.fetch("""
        SELECT ard.teacher_id, lds.day_number, lds.start_time, lds.end_time
        FROM allocation_registry_duties ard
        JOIN layout_duty_slots lds ON lds.id = ard.duty_slot_id
        WHERE ard.attempt_id = $1
    """, ctx.attempt_id):
        held[row["teacher_id"]].append(
            (row["day_number"], row["start_time"], row["end_time"]))

    def double_booked(teacher_id: Any, slot: dict) -> bool:
        for day, start, end in held[teacher_id]:
            if (day == slot["day_number"]
                    and start < slot["end_time"]
                    and end > slot["start_time"]):
                return True
        return False

    by_day: dict[int, list[dict]] = defaultdict(list)
    for slot in slots:
        by_day[slot["day_number"]].append(slot)

    for i, (day, day_slots) in enumerate(sorted(by_day.items())):
        await set_progress(ctx.attempt_id, stage, f"Day {day}",
                           _pct(stage, i / max(1, len(by_day))))

        for slot in day_slots:
            # Only periods whose time actually overlaps the duty conflict with
            # it. Excluding anyone who teaches at any point that day - as an
            # earlier version did - rules out nearly every teacher, because
            # recess and lunch duties never clash with a teaching period.
            clashing = {
                p["period_number"] for p in ctx.periods
                if p["day_number"] == day
                and p["start_time"] < slot["end_time"]
                and p["end_time"] > slot["start_time"]
            }

            free = [
                t for t in eligible
                if counts[t["id"]] < t["max_duties"]
                and not any((t["id"], day, p) in teaching_at for p in clashing)
                and not double_booked(t["id"], slot)
                and (t["campus_id"] is None or slot["campus_id"] is None
                     or t["campus_id"] == slot["campus_id"])
            ]
            # Fewest duties first, then a seeded tiebreak so a retry spreads
            # them differently rather than reproducing the same roster.
            free.sort(key=lambda t: (counts[t["id"]],
                                     _jitter(str(t["id"]), ctx.seed)))

            chosen = free[:slot["min_staff"]]
            if len(chosen) < slot["min_staff"]:
                shortfalls.append(
                    f"{slot['duty_name']} on day {day} ({slot['timing']}) needs "
                    f"{slot['min_staff']} staff but only {len(chosen)} are free")

            for teacher in chosen:
                rows.append((ctx.attempt_id, teacher["id"], slot["id"]))
                counts[teacher["id"]] += 1
                held[teacher["id"]].append(
                    (day, slot["start_time"], slot["end_time"]))
                assigned += 1

    if rows:
        async with db.transaction() as conn:
            await conn.executemany("""
                INSERT INTO allocation_registry_duties
                    (attempt_id, teacher_id, duty_slot_id)
                VALUES ($1,$2,$3) ON CONFLICT DO NOTHING
            """, rows)

    # Minimum staffing is a hard rule (VALIDATION.md). Under-filling silently
    # would produce a timetable that leaves duty slots uncovered.
    if shortfalls:
        raise PipelineError(
            f"Not enough staff for {len(shortfalls)} duty slot(s). "
            f"First: {shortfalls[0]}. Reduce the minimum staffing, add duty "
            "capacity, or remove duty exemptions.",
            stage=stage, school_fault=True)

    for teacher_id, n in counts.items():
        await db.execute("""
            INSERT INTO allocation_registry_duty_counts
                (attempt_id, teacher_id, duties_assigned)
            VALUES ($1,$2,$3)
            ON CONFLICT (attempt_id, teacher_id)
            DO UPDATE SET duties_assigned = EXCLUDED.duties_assigned
        """, ctx.attempt_id, teacher_id, n)

    await stage_done(ctx.attempt_id, stage, {"assigned": assigned})
    return assigned


async def layer6_duties(ctx: Context) -> None:
    await _assign_duties(ctx, bus=False, stage="layer6_duties")


async def layer8_bus_duties(ctx: Context) -> None:
    await _assign_duties(ctx, bus=True, stage="layer8_bus_duties")

    # Both duty layers are done, so the duty rules can be checked as a whole:
    # exemptions honoured, per-teacher caps respected, every slot staffed.
    check = await deterministic.check_duties(ctx.attempt_id)
    if not check.passed:
        raise PipelineError(check.failures[0], stage="layer8_bus_duties",
                            school_fault=True)


# --- Layer 7: transport (deterministic) --------------------------------------

def _period_start(ctx: Context, day: int, period: int) -> time:
    """When the bell goes for a slot. 09:00 if the layout has no time on it."""
    match = next((p for p in ctx.periods
                  if p["day_number"] == day and p["period_number"] == period), None)
    return (match or {}).get("start_time") or time(9, 0)


def _minus_minutes(at: time, minutes: int) -> time:
    total = at.hour * 60 + at.minute - minutes
    # A very early first period with a long route can run past midnight; the
    # bus leaves at 00:00 rather than wrapping to the previous evening.
    total = max(0, total)
    return time(total // 60, total % 60)


async def layer7_transport(ctx: Context) -> None:
    """
    Work out cross-campus travel. No AI in this layer (TRANSPORT.md).

    Demand is computed from where a student's class is versus their home campus.
    """
    if len(ctx.campuses) < 2:
        await stage_done(ctx.attempt_id, "layer7_transport", {"movements": 0})
        return

    buses = [dict(r) for r in await db.fetch(
        "SELECT id, name, capacity, home_campus_id FROM buses WHERE school_id = $1",
        ctx.school_id)]
    routes = {
        (str(r["from_campus_id"]), str(r["to_campus_id"])): r["travel_minutes"]
        for r in await db.fetch(
            "SELECT from_campus_id, to_campus_id, travel_minutes FROM routes "
            "WHERE school_id = $1", ctx.school_id)
    }
    if not buses or not routes:
        await stage_done(ctx.attempt_id, "layer7_transport", {"movements": 0})
        return

    slots_by_group = await _slots_by_group(ctx)
    demand: dict[tuple[int, int, str, str], int] = defaultdict(int)

    for group in ctx.groups:
        class_campus = str(group["campus_id"]) if group["campus_id"] else None
        if class_campus is None:
            continue
        for student in group["students"]:
            home = str(student["campus_id"]) if student["campus_id"] else None
            if home and home != class_campus:
                for day, period in slots_by_group.get(group["id"], []):
                    demand[(day, period, home, class_campus)] += 1

    ctx.transport = []  # type: ignore[attr-defined]
    movements = 0
    bus_at: dict[Any, str] = {b["id"]: str(b["home_campus_id"]) for b in buses}

    for (day, period, origin, destination), count in sorted(demand.items()):
        if (origin, destination) not in routes:
            await emit(ctx.attempt_id, "chunk_complete",
                       f"No route between campuses for {count} students on day {day}")
            continue

        # Students have to be there when the period starts, so the bus works
        # backwards from it: arrive at the bell, leave travel_minutes earlier.
        # Layer 8 reads these times to place bus duties, and the day chain in
        # TRANSPORT.md is only meaningful if they are real.
        arrive_by = _period_start(ctx, day, period)
        leave_at = _minus_minutes(arrive_by, routes[(origin, destination)])

        remaining = count
        while remaining > 0:
            bus = min(buses, key=lambda b: (bus_at[b["id"]] != origin, -b["capacity"]))

            if bus_at[bus["id"]] != origin:
                # The repositioning leg has to land before the passengers board,
                # so it is timed off the passenger departure the same way.
                back = routes.get((bus_at[bus["id"]], origin),
                                  routes[(origin, destination)])
                ctx.transport.append({  # type: ignore[attr-defined]
                    "bus_id": bus["id"], "from": bus_at[bus["id"]], "to": origin,
                    "day": day, "period": period, "passengers": 0, "empty": True,
                    "departure": _minus_minutes(leave_at, back),
                    "arrival": leave_at})
                movements += 1

            carried = min(bus["capacity"], remaining)
            ctx.transport.append({  # type: ignore[attr-defined]
                "bus_id": bus["id"], "from": origin, "to": destination,
                "day": day, "period": period, "passengers": carried, "empty": False,
                "departure": leave_at, "arrival": arrive_by})
            bus_at[bus["id"]] = destination
            remaining -= carried
            movements += 1

    await stage_done(ctx.attempt_id, "layer7_transport", {"movements": movements})


# --- Layer 9: mentor groups --------------------------------------------------

async def layer9_mentor(ctx: Context) -> None:
    mentor_periods = [p for p in ctx.periods if p["period_type"] == "mentor"]
    if not mentor_periods:
        await stage_done(ctx.attempt_id, "layer9_mentor", {"groups": 0})
        return

    # Form groups by year level, sized to the teachers available.
    eligible = [t for t in ctx.teachers]
    by_year: dict[str, list[dict]] = defaultdict(list)
    for student in ctx.students:
        by_year[student["year_group"] or "Unassigned"].append(student)

    ctx.mentor = []  # type: ignore[attr-defined]
    index = 0
    for year, cohort in sorted(by_year.items()):
        per_group = max(1, -(-len(cohort) // max(1, len(eligible) // max(1, len(by_year)))))
        for start in range(0, len(cohort), per_group):
            teacher = eligible[index % len(eligible)] if eligible else None
            room = ctx.rooms[index % len(ctx.rooms)] if ctx.rooms else None
            if teacher and room:
                ctx.mentor.append({  # type: ignore[attr-defined]
                    "year": year,
                    "students": cohort[start:start + per_group],
                    "teacher_id": teacher["id"],
                    "room_id": room["id"],
                })
            index += 1

    await stage_done(ctx.attempt_id, "layer9_mentor",
                     {"groups": len(getattr(ctx, "mentor", []))})


# --- Writing the solution ----------------------------------------------------

async def write_solution(ctx: Context) -> str:
    """Create the draft version and write every entry. Returns the version id."""
    slots_by_group = await _slots_by_group(ctx)

    version_number = await db.fetchval("""
        SELECT coalesce(max(version_number), 0) + 1 FROM timetable_versions
        WHERE timetable_id = $1
    """, ctx.timetable_id)

    async with db.transaction() as conn:
        version_id = await conn.fetchval("""
            INSERT INTO timetable_versions
                (timetable_id, school_id, version_number, status,
                 generation_attempt_id)
            VALUES ($1,$2,$3,'draft',$4) RETURNING id
        """, ctx.timetable_id, ctx.school_id, version_number, ctx.attempt_id)

        # Insert every entry in one statement and get the ids back, then attach
        # students with a single executemany. Row-at-a-time here is tens of
        # thousands of round trips for a large school.
        # Ids are generated here rather than read back from RETURNING: that would
        # depend on the returned rows matching insert order, which Postgres does
        # not guarantee, and a mismatch would attach students to the wrong class.
        entry_rows = []
        student_rows = []
        for group in ctx.groups:
            if not group.get("teacher_id") or not group.get("room_id"):
                continue
            for day, period in slots_by_group.get(group["id"], []):
                entry_id = uuid.uuid4()
                entry_rows.append((
                    entry_id, ctx.timetable_id, version_id, ctx.attempt_id,
                    _period_id(ctx, day, period), day, group["room_id"],
                    group["teacher_id"],
                    group["campus_id"] or ctx.campuses[0]["id"],
                    group["id"], group["subject"], group["code"]))
                student_rows.extend(
                    (entry_id, student["id"]) for student in group["students"])

        if entry_rows:
            await conn.executemany("""
                INSERT INTO timetable_entries
                    (id, timetable_id, version_id, attempt_id, layout_period_id,
                     day_number, room_id, teacher_id, campus_id,
                     class_group_id, subject, class_code)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """, entry_rows)
        if student_rows:
            await conn.executemany("""
                INSERT INTO timetable_entry_students
                    (timetable_entry_id, student_id)
                VALUES ($1,$2) ON CONFLICT DO NOTHING
            """, student_rows)

        for move in getattr(ctx, "transport", []):
            period = next((p for p in ctx.periods
                           if p["day_number"] == move["day"]
                           and p["period_number"] == move["period"]), None)
            await conn.execute("""
                INSERT INTO transport_schedule
                    (timetable_id, version_id, bus_id, from_campus_id,
                     to_campus_id, departure_time, arrival_time,
                     layout_period_id, day_number, passenger_count, is_empty_leg)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """, ctx.timetable_id, version_id, move["bus_id"], move["from"],
                move["to"], move["departure"], move["arrival"],
                period["id"] if period else None, move["day"],
                move["passengers"], move["empty"])

        for row in await db.fetch("""
            SELECT teacher_id, duty_slot_id FROM allocation_registry_duties
            WHERE attempt_id = $1
        """, ctx.attempt_id):
            await conn.execute("""
                INSERT INTO duty_assignments
                    (timetable_id, version_id, attempt_id, duty_slot_id, teacher_id)
                VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING
            """, ctx.timetable_id, version_id, ctx.attempt_id,
                row["duty_slot_id"], row["teacher_id"])

    await stage_done(ctx.attempt_id, "write_solution",
                     {"version": version_number})
    return str(version_id)


# --- Shared helpers ----------------------------------------------------------

async def _slots_by_group(ctx: Context) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for row in await db.fetch("""
        SELECT class_group_id, day_number, period_number
        FROM class_group_slots WHERE attempt_id = $1
        ORDER BY day_number, period_number
    """, ctx.attempt_id):
        out[str(row["class_group_id"])].append(
            (row["day_number"], row["period_number"]))
    return out


async def _teacher_busy_map(ctx: Context) -> set[tuple[Any, int, int]]:
    busy = set()
    slots_by_group = await _slots_by_group(ctx)
    for group in ctx.groups:
        if not group.get("teacher_id"):
            continue
        for day, period in slots_by_group.get(group["id"], []):
            busy.add((group["teacher_id"], day, period))
    return busy


def _period_id(ctx: Context, day: int, number: int) -> Optional[str]:
    for p in ctx.periods:
        if p["day_number"] == day and p["period_number"] == number:
            return p["id"]
    return None


# --- Orchestration -----------------------------------------------------------

LAYERS = [
    ("layer1_subject_map", layer1_subject_map),
    ("layer2_class_groups", layer2_class_groups),
    ("layer3_period_slots", layer3_period_slots),
    ("layer4_rooms", layer4_rooms),
    ("layer5_teachers", layer5_teachers),
    ("layer6_duties", layer6_duties),
    ("layer7_transport", layer7_transport),
    ("layer8_bus_duties", layer8_bus_duties),
    ("layer9_mentor", layer9_mentor),
]

LAYER_INDEX = {name: i for i, (name, _) in enumerate(LAYERS)}

# Which layer to restart from, given what validation objected to. A teacher
# clash is Layer 5's doing; re-running Layer 2 as well would throw away correct
# work and cost tokens for nothing (PIPELINE.md: "reuse unchanged layers").
FAILURE_ROUTES: list[tuple[str, str]] = [
    ("teacher double-booked", "layer5_teachers"),
    ("not one of their subjects", "layer5_teachers"),
    ("unavailable", "layer5_teachers"),
    ("teacher already assigned", "layer5_teachers"),
    ("room double-booked", "layer4_rooms"),
    ("which holds", "layer4_rooms"),          # over capacity
    ("no room can hold", "layer2_class_groups"),
    ("same period", "layer3_period_slots"),   # student clash
    ("day_number", "layer3_period_slots"),
    ("periods", "layer3_period_slots"),
    ("hard maximum", "layer2_class_groups"),
    ("class code", "layer2_class_groups"),
    ("duty", "layer6_duties"),
]


def restart_stage(failure: str, failed_stage: str) -> str:
    """
    The earliest layer that could have caused this failure.

    A layer that failed outright restarts itself. A validation failure is
    routed by what the validator complained about, defaulting to class group
    formation - the earliest layer that could be responsible - so a retry is
    never a no-op.
    """
    if failed_stage in LAYER_INDEX:
        return failed_stage

    text = (failure or "").lower()
    for needle, stage in FAILURE_ROUTES:
        if needle in text:
            return stage
    return "layer2_class_groups"


async def copy_forward(previous: str, current: str, start_stage: str) -> None:
    """
    Copy the still-valid layers from a failed attempt into its replacement.

    Retrying from Layer 5 means Layers 1-4 were fine; redoing them wastes tokens
    and can only produce a different, equally arbitrary starting point. Each
    attempt gets its own rows so the history of what was tried stays intact.
    """
    index = LAYER_INDEX.get(start_stage, 0)

    if index > LAYER_INDEX["layer2_class_groups"]:
        # class_groups get new ids; everything below is remapped through them.
        await db.execute("""
            INSERT INTO class_groups
                (id, attempt_id, class_group_def_id, school_id, class_code,
                 subject, year_level, campus_id, student_count,
                 is_double_period, size_warning, balance_score, formation_type)
            SELECT gen_random_uuid(), $2, class_group_def_id, school_id,
                   class_code, subject, year_level, campus_id, student_count,
                   is_double_period, size_warning, balance_score, formation_type
            FROM class_groups WHERE attempt_id = $1
        """, previous, current)

        # Match old to new by class_code, which is unique within an attempt.
        await db.execute("""
            INSERT INTO class_group_students (class_group_id, student_id, entry_type)
            SELECT new.id, cgs.student_id, cgs.entry_type
            FROM class_group_students cgs
            JOIN class_groups old ON old.id = cgs.class_group_id
            JOIN class_groups new ON new.class_code = old.class_code
                                 AND new.attempt_id = $2
            WHERE old.attempt_id = $1
            ON CONFLICT DO NOTHING
        """, previous, current)

    if index > LAYER_INDEX["layer3_period_slots"]:
        await db.execute("""
            INSERT INTO class_group_slots
                (attempt_id, class_group_id, day_number, period_number,
                 layout_period_id, is_double_first)
            SELECT $2, new.id, s.day_number, s.period_number,
                   s.layout_period_id, s.is_double_first
            FROM class_group_slots s
            JOIN class_groups old ON old.id = s.class_group_id
            JOIN class_groups new ON new.class_code = old.class_code
                                 AND new.attempt_id = $2
            WHERE s.attempt_id = $1
            ON CONFLICT DO NOTHING
        """, previous, current)
        await db.execute("""
            INSERT INTO allocation_registry_students
                (attempt_id, student_id, layout_period_id, class_group_id)
            SELECT $2, r.student_id, r.layout_period_id, new.id
            FROM allocation_registry_students r
            LEFT JOIN class_groups old ON old.id = r.class_group_id
            LEFT JOIN class_groups new ON new.class_code = old.class_code
                                      AND new.attempt_id = $2
            WHERE r.attempt_id = $1
            ON CONFLICT DO NOTHING
        """, previous, current)

    if index > LAYER_INDEX["layer4_rooms"]:
        await db.execute("""
            INSERT INTO allocation_registry_rooms
                (attempt_id, room_id, layout_period_id)
            SELECT $2, room_id, layout_period_id
            FROM allocation_registry_rooms WHERE attempt_id = $1
            ON CONFLICT DO NOTHING
        """, previous, current)

    if index > LAYER_INDEX["layer5_teachers"]:
        await db.execute("""
            INSERT INTO allocation_registry_teachers
                (attempt_id, teacher_id, layout_period_id)
            SELECT $2, teacher_id, layout_period_id
            FROM allocation_registry_teachers WHERE attempt_id = $1
            ON CONFLICT DO NOTHING
        """, previous, current)

    # The consistency registry carries the room and teacher a class kept, and is
    # how a restart above Layer 4 recovers them.
    if index > LAYER_INDEX["layer4_rooms"]:
        await db.execute("""
            INSERT INTO class_consistency_registry
                (attempt_id, class_group_id, teacher_id, room_id)
            SELECT $2, new.id, c.teacher_id, c.room_id
            FROM class_consistency_registry c
            JOIN class_groups old ON old.id = c.class_group_id
            JOIN class_groups new ON new.class_code = old.class_code
                                 AND new.attempt_id = $2
            WHERE c.attempt_id = $1
            ON CONFLICT DO NOTHING
        """, previous, current)


async def hydrate_groups(ctx: Context) -> None:
    """
    Rebuild the in-memory class groups from the database.

    A retry that starts above Layer 2 never runs the code that populates
    ctx.groups, so it has to be reconstructed - including the room and teacher
    already chosen, where those layers are being reused.
    """
    rows = await db.fetch("""
        SELECT cg.id, cg.class_code, cg.subject, cg.year_level, cg.campus_id,
               c.teacher_id, c.room_id
        FROM class_groups cg
        LEFT JOIN class_consistency_registry c
               ON c.class_group_id = cg.id AND c.attempt_id = cg.attempt_id
        WHERE cg.attempt_id = $1
        ORDER BY cg.class_code
    """, ctx.attempt_id)

    members: dict[str, list[dict]] = defaultdict(list)
    for row in await db.fetch("""
        SELECT cgs.class_group_id, s.id, s.full_name, s.campus_id, s.gender,
               s.ability_band, s.year_group
        FROM class_group_students cgs
        JOIN students s ON s.id = cgs.student_id
        JOIN class_groups cg ON cg.id = cgs.class_group_id
        WHERE cg.attempt_id = $1
    """, ctx.attempt_id):
        members[str(row["class_group_id"])].append(dict(row))

    ctx.groups = []
    for row in rows:
        group = {
            "id": str(row["id"]),
            "code": row["class_code"],
            "subject": row["subject"],
            "year_level": row["year_level"],
            "campus_id": row["campus_id"],
            "students": members.get(str(row["id"]), []),
        }
        if row["teacher_id"]:
            group["teacher_id"] = row["teacher_id"]
        if row["room_id"]:
            group["room_id"] = row["room_id"]
        ctx.groups.append(group)


async def discard_version(attempt_id: str) -> None:
    """
    Remove the draft a failed attempt wrote.

    write_solution runs before validation, so a rejected timetable has already
    been written. Leaving it would show the school a draft that failed review.
    """
    for row in await db.fetch(
            "SELECT id FROM timetable_versions WHERE generation_attempt_id = $1",
            attempt_id):
        version_id = row["id"]
        await db.execute("""
            DELETE FROM timetable_entry_students WHERE timetable_entry_id IN
              (SELECT id FROM timetable_entries WHERE version_id = $1)
        """, version_id)
        await db.execute(
            "DELETE FROM timetable_entries WHERE version_id = $1", version_id)
        await db.execute(
            "DELETE FROM transport_schedule WHERE version_id = $1", version_id)
        await db.execute(
            "DELETE FROM duty_assignments WHERE version_id = $1", version_id)
        await db.execute(
            "DELETE FROM timetable_versions WHERE id = $1", version_id)


async def save_partial(ctx: Context) -> None:
    """Record what has been decided, so a retry can show what was reused."""
    await db.execute("""
        INSERT INTO partial_solutions
            (attempt_id, period_campus_allocation, room_allocation,
             teacher_allocation, transport_solution, updated_at)
        VALUES ($1,$2,$3,$4,$5, now())
    """, ctx.attempt_id,
        json.dumps({g["code"]: g.get("campus_id") and str(g["campus_id"])
                    for g in ctx.groups}),
        json.dumps({g["code"]: str(g["room_id"]) for g in ctx.groups
                    if g.get("room_id")}),
        json.dumps({g["code"]: str(g["teacher_id"]) for g in ctx.groups
                    if g.get("teacher_id")}),
        json.dumps(getattr(ctx, "transport", [])[:200], default=str))


async def run_with_retries(timetable_id: str, notify_email: str) -> dict:
    """
    Generate a timetable, retrying from the layer that caused any failure.

    PIPELINE.md: on failure, loop back to the relevant layer rather than
    starting over, up to `allocation_max_loops` times. Each loop reuses every
    layer before the failure point, so a teacher clash costs one layer's work,
    not a whole generation.

    Each loop is one row in generation_attempts, so the history of what was
    tried is visible, and billing can tell school-caused retries (charged) from
    dev-caused ones (refunded).
    """
    max_loops = await settings_service.get_int("allocation_max_loops", 5)

    attempt_number = 0
    last_error: Optional[PipelineError] = None
    previous_attempt: Optional[str] = None
    school_faults = 0
    dev_faults = 0

    while attempt_number < max_loops:
        attempt_number += 1

        # The locked model comes from the configured primary, not a hardcoded
        # name: hardcoding 'magistral' meant reassigning the task in settings
        # had no effect, because the lock overrode it on every call.
        primary = await settings_service.get("task_block_campus_primary") or "magistral"

        attempt_id = str(await db.fetchval("""
            INSERT INTO generation_attempts
                (timetable_id, attempt_number, locked_model)
            VALUES ($1, $2, $3) RETURNING id
        """, timetable_id, attempt_number, primary))
        await db.execute("""
            INSERT INTO generation_jobs (attempt_id, status, notify_email)
            VALUES ($1, 'queued', $2)
        """, attempt_id, notify_email)

        start_stage = None
        if last_error is not None and previous_attempt:
            start_stage = restart_stage(last_error.message, last_error.stage)
            # Carry the layers before the failure into this attempt, so they are
            # reused rather than recomputed.
            await copy_forward(previous_attempt, attempt_id, start_stage)
            await emit(attempt_id, "retry",
                       f"Attempt {attempt_number}: retrying from "
                       f"{start_stage} after: {last_error.message[:160]}",
                       {"from_stage": start_stage,
                        "reused_layers": LAYER_INDEX.get(start_stage, 0),
                        "previous_failure": last_error.message[:500],
                        "school_fault": last_error.school_fault})

        try:
            summary = await run(attempt_id, seed=attempt_number,
                                start_stage=start_stage)
            summary.update({
                "attempts": attempt_number,
                "school_retries": school_faults,
                "dev_retries": dev_faults,
            })
            return summary
        except PipelineError as exc:
            last_error = exc
            previous_attempt = attempt_id
            if exc.school_fault:
                school_faults += 1
            else:
                dev_faults += 1
            # The draft this attempt wrote was rejected; do not leave it for the
            # school to find.
            await discard_version(attempt_id)
            log.warning("Attempt %s failed at %s: %s",
                        attempt_number, exc.stage, exc.message)

    await db.execute("""
        UPDATE timetables SET status = 'failed' WHERE id = $1
    """, timetable_id)

    raise PipelineError(
        f"Generation failed after {attempt_number} attempts. "
        f"Last problem: {last_error.message if last_error else 'unknown'}",
        stage=last_error.stage if last_error else "",
        school_fault=bool(last_error and last_error.school_fault))


async def run(attempt_id: str, seed: int = 1,
              start_stage: Optional[str] = None) -> dict:
    """
    Run every layer for one attempt.

    `seed` varies deterministic tie-breaking between attempts. Without it a
    retry reruns identical inputs through identical logic and fails identically,
    which makes the retry loop pointless.

    Returns a summary on success. Raises PipelineError on failure, with
    school_fault set so billing knows whether to charge for the retry
    (ARCHITECTURE.md: school-caused retries charged, dev-caused refunded).
    """
    started = datetime.now(timezone.utc)
    ctx = await load_context(attempt_id)
    ctx.seed = seed

    await emit(attempt_id, "stage_start", "Generation started")
    await db.execute("""
        UPDATE generation_jobs SET status = 'running', progress_pct = 0
        WHERE attempt_id = $1
    """, attempt_id)

    # A retry starts partway through, with the earlier layers copied in. Their
    # results have to be loaded back into memory before the next layer runs.
    first_index = LAYER_INDEX.get(start_stage, 0) if start_stage else 0
    if first_index > 0:
        await hydrate_groups(ctx)
        # Layer 1's output is derived from the school's own settings, so it can
        # be rebuilt without an AI call rather than carried forward.
        ctx.subject_map = {
            s["subject"]: {
                "room_type": s.get("default_room_type"),
                "campus": "flexible",
                "is_double": bool(s.get("is_double_period")),
            }
            for s in ctx.subjects
        }
        await emit(attempt_id, "stage_start",
                   f"Reusing {first_index} layer(s) from the previous attempt")

    try:
        for stage, fn in LAYERS[first_index:]:
            label = next(l for n, l, _ in STAGES if n == stage)
            await stage_start(attempt_id, stage)
            await set_progress(attempt_id, stage, None, _pct(stage, 0.0))
            await emit(attempt_id, "stage_start", label)

            try:
                await fn(ctx)
            except PipelineError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Unhandled error in %s", stage)
                raise PipelineError(f"{label} failed: {exc}", stage=stage) from exc

            await emit(attempt_id, "stage_complete", f"{label} complete")

        await stage_start(attempt_id, "write_solution")
        await set_progress(attempt_id, "write_solution", None, _pct("write_solution", 0.0))
        version_id = await write_solution(ctx)

        # Layer 10: deterministic check plus the AI quorum, chunked by day.
        await stage_start(attempt_id, "layer10_validation")
        await set_progress(attempt_id, "layer10_validation", None,
                           _pct("layer10_validation", 0.0))
        await emit(attempt_id, "stage_start", "Validating the timetable")

        from services import validators

        verdict = await validators.run_validation(
            version_id, attempt_id, ctx.timetable_id)

        if verdict["result"] != "pass":
            await stage_failed(attempt_id, "layer10_validation",
                               verdict.get("reason") or verdict["result"])
            # A hard-constraint failure is the pipeline's fault; a quorum that
            # could not assemble is an availability problem, not the school's.
            raise PipelineError(
                verdict.get("reason") or "Validation did not pass",
                stage="layer10_validation",
                school_fault=verdict.get("source") == "deterministic")

        await stage_done(attempt_id, "layer10_validation", verdict)
        await emit(attempt_id, "stage_complete",
                   f"Validated by {verdict['responded']} of "
                   f"{len(verdict['validators'])} validators "
                   f"across {verdict['days']} days")

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        summary = {
            "version_id": version_id,
            "class_groups": len(ctx.groups),
            "entries": await db.fetchval(
                "SELECT count(*) FROM timetable_entries WHERE version_id = $1",
                version_id),
            "seconds": round(elapsed, 1),
        }

        await db.execute("""
            UPDATE generation_attempts
            SET status = 'complete', completed_at = now() WHERE id = $1
        """, attempt_id)
        await db.execute("""
            UPDATE generation_jobs SET status = 'complete', progress_pct = 100,
                                       updated_at = now()
            WHERE attempt_id = $1
        """, attempt_id)
        await emit(attempt_id, "complete", "Timetable generated", summary)
        return summary

    except PipelineError as exc:
        await stage_failed(attempt_id, exc.stage or "unknown", exc.message)
        await db.execute("""
            UPDATE generation_attempts
            SET status = 'failed', failure_reason = $2, completed_at = now()
            WHERE id = $1
        """, attempt_id, exc.message[:2000])
        await db.execute("""
            UPDATE generation_jobs SET status = 'failed', updated_at = now()
            WHERE attempt_id = $1
        """, attempt_id)
        await db.execute("""
            INSERT INTO error_log
                (school_id, timetable_id, attempt_id, error_type, stage,
                 is_dev_fault, error_message)
            VALUES ($1,$2,$3,'pipeline',$4,$5,$6)
        """, ctx.school_id, ctx.timetable_id, attempt_id, exc.stage,
            not exc.school_fault, exc.message[:2000])
        await emit(attempt_id, "failed", exc.message,
                   {"stage": exc.stage, "school_fault": exc.school_fault})
        raise

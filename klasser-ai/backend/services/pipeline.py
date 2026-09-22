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
    ("layer5b_gap_repair", "Closing timetable gaps", 3),
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


def _minutes(value) -> int:
    """A time of day as minutes past midnight."""
    return value.hour * 60 + value.minute


async def _travel_rules(ctx: Context) -> tuple[bool, dict, Any]:
    """
    What the school allows, and how long it actually takes to get between
    campuses.

    routes.travel_minutes has always held the answer; nothing consulted it, so
    timetables were produced that put a student in two campuses in back-to-back
    periods with no time to travel.
    """
    allow = bool(await db.fetchval("""
        SELECT allow_cross_campus_travel FROM school_preferences
        WHERE school_id = $1
    """, ctx.school_id))

    travel: dict[tuple[str, str], int] = {}
    for row in await db.fetch("""
        SELECT from_campus_id, to_campus_id, travel_minutes
        FROM routes WHERE school_id = $1
    """, ctx.school_id):
        travel[(str(row["from_campus_id"]), str(row["to_campus_id"]))] = \
            row["travel_minutes"]

    period_at = {(p["day_number"], p["period_number"]): p for p in ctx.periods}

    def gap_minutes(day: int, a: int, b: int) -> int:
        """Clock minutes between the end of the earlier period and the start
        of the later one. Counts the breaks in between, because that is where
        the travelling happens."""
        lo, hi = (a, b) if a <= b else (b, a)
        first, second = period_at.get((day, lo)), period_at.get((day, hi))
        if not first or not second:
            return 0
        return _minutes(second["start_time"]) - _minutes(first["end_time"])

    return allow, travel, gap_minutes


def _teacher_ok(code, slot, subject_of, teacher_supply, slot_subjects) -> bool:
    """
    Are there enough teachers of this subject to run another class now?

    The room check below already refuses to put three PE classes in one period
    when the school has one gym. A subject's teachers are just as finite, and
    were never counted: packing parallel streams into a shared slot raised the
    peak demand for every subject at once, and layer 5 then failed with "every
    Economics teacher is already busy when 12ECO2 meets" - after layer 3 had
    already committed to the clash.

    This is a necessary condition, not a sufficient one. A teacher of two
    subjects may still be taken by the other one, which only layer 5 can know.
    Catching the impossible cases here is what stops layer 5 being handed a
    problem with no answer.
    """
    subject = subject_of.get(code)
    if not subject:
        return True
    supply = teacher_supply.get(subject, 0)
    return not (supply and slot_subjects[slot][subject] >= supply)


def _travel_ok(code, slot, graph, assigned, campus_of, travel, gap_minutes,
               allow_cross) -> bool:
    """
    Whether a student in this class could physically be here.

    Classes sharing students already cannot share a slot. This asks the next
    question: if the other class is at the other campus on the same day, is
    there enough of a break between the two to get there?
    """
    mine = campus_of.get(code)
    if mine is None:
        return True

    day, period = slot
    for other in graph.get(code, ()):
        theirs = campus_of.get(other)
        if theirs is None or theirs == mine:
            continue
        for other_day, other_period in assigned.get(other, ()):
            if other_day != day:
                continue
            if not allow_cross:
                # Crossing mid-day is switched off, so these two classes have
                # to fall on different days entirely.
                return False
            need = (travel.get((mine, theirs))
                    or travel.get((theirs, mine)) or 0)
            if gap_minutes(day, period, other_period) < need:
                return False
    return True


async def layer3_period_slots(ctx: Context) -> None:
    slots = [(p["day_number"], p["period_number"]) for p in ctx.teaching_periods]
    if not slots:
        raise PipelineError("The layout has no teaching periods",
                            stage="layer3_period_slots", school_fault=True)

    allow_cross, travel, gap_minutes = await _travel_rules(ctx)
    campus_of = {g["code"]: (str(g["campus_id"]) if g.get("campus_id") else None)
                 for g in ctx.groups}
    prefer_doubles = await settings_service.get_bool("prefer_double_periods", True)

    # Classes that are the same subject, year and campus are parallel streams
    # of one cohort: their students are disjoint, so they may share a slot.
    sibling_key = {
        g["code"]: (g["subject"], g["year_level"], campus_of[g["code"]])
        for g in ctx.groups
    }

    graph = _conflict_graph(ctx)
    # Every class starts with an empty list rather than being absent, so the
    # placement helpers can read a class's slots without guarding for it.
    assigned: dict[str, list[tuple[int, int]]] = {
        g["code"]: [] for g in ctx.groups
    }
    slot_load: dict[tuple[int, int], int] = defaultdict(int)

    wanted: dict[str, tuple[int, int]] = {}
    for group in ctx.groups:
        wanted[group["code"]] = ctx.periods_for(group["subject"],
                                                group["year_level"])

    # Collect the model's suggestions per campus, as PIPELINE.md chunks it -
    # but only if a school has actually asked for it. Since minimums moved to
    # a guaranteed deterministic pass, these proposals only ever influence the
    # best-effort top-up, and measured against today's runs that influence
    # was marginal: three generations placed cleanly on the packer alone. The
    # call itself was 16.8% of total wall-clock time for one stage of ten.
    # Off by default; a school that wants the AI's opinion on slot placement
    # can turn it back on and pay for it deliberately.
    use_proposals = await settings_service.get_bool(
        "layer3_use_ai_proposals", False)
    proposals: dict[str, list[tuple[int, int]]] = {}
    if use_proposals:
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
    # Keyed by campus as well as type, because a room cannot be borrowed from
    # the other site. Counted school-wide, 22 labs split 11 and 11 read as 22
    # available in every slot, so layer 3 would stack more lab classes at one
    # campus than it owns and layer 4 then failed with "every suitable room is
    # already busy when 8DES6 meets" - a clash layer 3 had already committed to.
    room_need = {
        g["code"]: (campus_of[g["code"]],
                    ctx.room_type_for(g["subject"], g["year_level"]))
        for g in ctx.groups
        if ctx.room_type_for(g["subject"], g["year_level"])
    }
    room_supply: dict[tuple, int] = defaultdict(int)
    for room in ctx.rooms:
        if room["room_type"]:
            campus = str(room["campus_id"]) if room["campus_id"] else None
            room_supply[(campus, room["room_type"])] += 1
    slot_rooms: dict[tuple[int, int], dict[str, int]] = defaultdict(
        lambda: defaultdict(int))

    # The same accounting for teachers: how many of each subject exist, and
    # how many classes of it are already running in each slot.
    subject_of = {g["code"]: g["subject"] for g in ctx.groups}
    teacher_supply: dict[str, int] = defaultdict(int)
    for teacher in ctx.teachers:
        for subject in (teacher.get("subjects") or []):
            teacher_supply[subject] += 1
    slot_subjects: dict[tuple[int, int], dict[str, int]] = defaultdict(
        lambda: defaultdict(int))

    def take(code: str, slot: tuple[int, int]) -> None:
        """Commit a slot, keeping the load and room tallies in step."""
        assigned[code].append(slot)
        slot_load[slot] += 1
        needed = room_need.get(code)
        if needed:
            slot_rooms[slot][needed] += 1
        subject = subject_of.get(code)
        if subject:
            slot_subjects[slot][subject] += 1

    def fill(code: str, target: int, *, required: bool) -> None:
        chosen = assigned[code]

        # A double covers the pedagogical case for meeting twice in a day.
        # Nothing capped a class at that, and once sibling-packing and the
        # doubles preference both outrank "avoid reusing a day" in the sort
        # below, a class that needs many periods a cycle can end up doubled
        # twice into the same day - 7SPA1 got period 1-2 AND period 4-5 on
        # day 1, a school day that is three quarters Spanish.
        MAX_PER_DAY = 2

        def placeable(slot: tuple[int, int]) -> bool:
            day = slot[0]
            same_day = sum(1 for d, _ in chosen if d == day)
            if same_day >= MAX_PER_DAY:
                return False
            return (_slot_ok(code, slot, graph, assigned, chosen,
                             room_need, room_supply, slot_rooms)
                    and _travel_ok(code, slot, graph, assigned, campus_of,
                                   travel, gap_minutes, allow_cross)
                    and _teacher_ok(code, slot, subject_of, teacher_supply,
                                    slot_subjects))

        # Where the parallel classes of this same subject and year already
        # sit. They hold disjoint students by construction, so sharing a slot
        # with one is always legal - and it is the difference between a year
        # level costing one block of periods per subject or one per class.
        mine = sibling_key[code]
        sibling_slots = {
            slot
            for other, key in sibling_key.items()
            if key == mine and other != code
            for slot in assigned[other]
        }

        # The model's suggestions shape the surplus, never the floor. Taken
        # first they scatter a cohort across the cycle - the model is asked
        # one campus at a time and has no notion of packing - and the classes
        # placed last then cannot reach their minimum at all. The
        # deterministic packer guarantees the floor; the AI gets the top-up,
        # where there is slack for a preference to be worth honouring.
        if not required:
            for slot in proposals.get(code, []):
                if len(chosen) >= target:
                    break
                if placeable(slot):
                    take(code, slot)

        while len(chosen) < target:
            used_days = {d for d, _ in chosen}
            candidates = [s for s in slots if placeable(s)]
            if not candidates:
                if not required:
                    # Topping up is best-effort. A class that already has its
                    # minimum and cannot find another slot simply keeps what
                    # it has rather than failing the whole generation.
                    return
                # Ask which constraint actually did the rejecting rather than
                # inferring it from the class needing a room type at all. The
                # two causes call for opposite responses, and guessing sent a
                # real school off adding 50 classrooms to a timetable whose
                # rooms were 8% used - the blockage was student overlap, and
                # the extra rooms changed nothing.
                by_clash, by_room = _why_blocked(
                    code, slots, graph, assigned, chosen,
                    room_need, room_supply, slot_rooms)
                needed = room_need.get(code)

                if by_room and by_room >= by_clash and needed:
                    raise PipelineError(
                        f"{code} needs {target} periods in a {needed[1]}, "
                        f"but its campus has {room_supply[needed]} of them and "
                        "every slot is already taken by another class that "
                        f"needs one. Add a {needed[1]} there, or reduce how "
                        "often the subjects using it meet.",
                        stage="layer3_period_slots", school_fault=True)
                raise PipelineError(
                    f"{code} needs {target} periods but only {len(chosen)} slots "
                    f"are free: {by_clash} of {len(slots)} in the cycle are "
                    "already taken by another class sharing its students"
                    + (f", and {by_room} by a shortage of {needed[1]}s"
                       if by_room
                       else "")
                    + ". The cycle is too short for the subject load, or too "
                    "many subjects draw on the same students.",
                    stage="layer3_period_slots", school_fault=True)

            # A double is two teaching periods back to back on the same day
            # with nothing between them - so the gap has to be zero. Adjacent
            # period numbers are not enough: periods 2 and 3 straddle recess.
            def doubles_with_chosen(slot: tuple[int, int]) -> bool:
                day, period = slot
                return any(d == day and abs(p - period) == 1
                           and gap_minutes(day, period, p) <= 0
                           for d, p in chosen)

            # Pack, do not spread. Every candidate here has already passed
            # _slot_ok, which means no class sharing our students is in it -
            # so joining a busy slot costs nothing and leaves an empty one
            # free for somebody else. Preferring the emptiest slot, as this
            # once did, scatters a cohort's parallel classes across the cycle:
            # a year level with three streams a subject then needs 210 slots
            # out of 70 and cannot be placed at all.
            candidates.sort(key=lambda s: (
                s not in sibling_slots,
                not (prefer_doubles and doubles_with_chosen(s)),
                -slot_load[s],
                s[0] in used_days,
                s,
            ))
            take(code, candidates[0])

    # Two passes, not one. A school states a range per subject, and letting an
    # early class sit on the top of its range before a later one has reached
    # its floor is how a cohort ends up with a class it cannot place at all -
    # 9ENG3 was refused all 70 slots while earlier classes held their maximum.
    # Everyone reaches their minimum first and the surplus is shared out
    # afterwards, which on this school's data still fills 99% of the target.
    for group in order:
        fill(group["code"], wanted[group["code"]][0], required=True)
    for group in order:
        fill(group["code"], wanted[group["code"]][1], required=False)

    await _write_slots(ctx, assigned)

    spread = sum(len(v) for v in assigned.values())
    await stage_done(ctx.attempt_id, "layer3_period_slots",
                     {"slots": spread, "classes": len(assigned)})


def _why_blocked(code: str, slots: list, graph: dict[str, set[str]],
                 assigned: dict[str, list[tuple[int, int]]],
                 chosen: list[tuple[int, int]],
                 room_need: dict[str, str], room_supply: dict[str, int],
                 slot_rooms: dict) -> tuple[int, int]:
    """
    Count why every slot was refused: student overlap, or room scarcity.

    _slot_ok answers yes or no, which is all the placement loop needs but not
    enough to tell a school what to change. A class blocked by overlap needs
    the timetable loosened; one blocked by rooms needs a room. The advice is
    opposite, so the failure path counts rather than assumes.
    """
    needed = room_need.get(code)
    supply = room_supply.get(needed, 0) if needed else 0

    by_clash = by_room = 0
    for slot in slots:
        if slot in chosen:
            continue
        if any(slot in assigned.get(other, ()) for other in graph.get(code, ())):
            by_clash += 1
        elif supply and slot_rooms[slot][needed] >= supply:
            by_room += 1
    return by_clash, by_room


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

    ends_at = {(p["day_number"], p["period_number"]): p["end_time"]
               for p in ctx.periods}
    starts_at = {(p["day_number"], p["period_number"]): p["start_time"]
                 for p in ctx.periods}

    def opens_a_double(day: int, number: int, taken: set) -> bool:
        """This period runs straight into the class's next one, with no break."""
        after = ends_at.get((day, number))
        before = starts_at.get((day, number + 1))
        return ((day, number + 1) in taken and after is not None
                and before is not None and _minutes(before) <= _minutes(after))

    slot_rows = []
    registry_rows = []
    for group in ctx.groups:
        placed = set(assigned.get(group["code"], []))
        for day, number in assigned.get(group["code"], []):
            period_id = period_by_slot.get((day, number))
            slot_rows.append((ctx.attempt_id, group["id"], day, number,
                              period_id, opens_a_double(day, number, placed)))
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
                     layout_period_id, is_double_first)
                VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING
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

def rank_rooms(rooms: list[dict], size: int, room_type: Optional[str],
               campus: Optional[Any] = None) -> list[dict]:
    """
    Rank rooms for a class, excluding any that cannot hold it.

    PIPELINE.md: the AI is never offered an undersized room. Capacity is a hard
    constraint and is enforced here, before the model sees the options.
    """
    out = []
    for room in rooms:
        if room["capacity"] < size:
            continue
        # A room cannot be borrowed from the other site. Without this a class
        # at officer was handed a berwick room and the timetable asked its
        # students to be in two places at once.
        if (campus is not None and room["campus_id"] is not None
                and str(room["campus_id"]) != str(campus)):
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

    def options(group) -> int:
        wanted = (ctx.subject_map.get(group["subject"]) or {}).get("room_type")
        return len(rank_rooms(ctx.rooms, len(group["students"]), wanted,
                              group.get("campus_id")))

    # Giving a class one room for all of its periods is a graph colouring:
    # classes competing for the same room type at the same campus conflict
    # whenever they share a slot, and the rooms are the colours. Greedy
    # colouring only works if the most entangled class picks first - ordering
    # by class size instead left four lab classes at berwick with no room
    # that was free across all their periods, even though the campus had
    # enough labs. Colouring by conflicts places every one of them.
    def rivals(group) -> int:
        wanted = (ctx.subject_map.get(group["subject"]) or {}).get("room_type")
        mine = set(slots_by_group.get(group["id"], []))
        return sum(
            1 for other in ctx.groups
            if other["id"] != group["id"]
            and other.get("campus_id") == group.get("campus_id")
            and (ctx.subject_map.get(other["subject"]) or {}).get("room_type") == wanted
            and mine & set(slots_by_group.get(other["id"], [])))

    # Fewest usable rooms first, then most entangled: a specialist class with
    # three rooms on its campus must choose before a class with fifty-three.
    for group in sorted(ctx.groups,
                        key=lambda g: (options(g), -rivals(g),
                                       -len(g["students"]))):
        size = len(group["students"])
        wanted_type = (ctx.subject_map.get(group["subject"]) or {}).get("room_type")
        campus = group.get("campus_id")
        candidates = rank_rooms(ctx.rooms, size, wanted_type, campus)

        if not candidates and wanted_type:
            # Relax the type rather than fail: a warning is better than no timetable.
            candidates = rank_rooms(ctx.rooms, size, None, campus)
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

    # max_blocks limits a teacher over the whole cycle, and nothing limited a
    # single day. A validator rejected an otherwise clean timetable because
    # one teacher drew all seven periods of Day 5 - no clash, so the
    # deterministic checks passed it, but not a day anyone could teach.
    per_day: dict[tuple[Any, int], int] = defaultdict(int)
    max_per_day = await settings_service.get_int("teacher_max_periods_per_day", 5)

    def day_has_room(teacher, slots) -> bool:
        wanted: dict[int, int] = defaultdict(int)
        for day, _ in slots:
            wanted[day] += 1
        return all(per_day[(teacher["id"], day)] + n <= max_per_day
                   for day, n in wanted.items())

    subject_groups: dict[str, list[dict]] = defaultdict(list)
    for group in ctx.groups:
        subject_groups[group["subject"]].append(group)

    # Scarcest subject first, for the reason layer 3 places its hardest
    # classes first: a teacher covering both English and German is spent by
    # whichever is handled sooner, and alphabetical order handed them all to
    # English. German, with nine teachers to English's twenty-nine, then had
    # none left - "every German teacher is already busy when 9GER2 meets".
    # Fewest qualified teachers goes first, so the subjects with the least
    # room to manoeuvre choose while they still can.
    def scarcity(item: tuple[str, list[dict]]) -> tuple:
        subject, groups = item
        qualified = sum(1 for t in ctx.teachers
                        if subject in (t["subjects"] or []))
        return (qualified, -len(groups), subject)

    for i, (subject, groups) in enumerate(
            sorted(subject_groups.items(), key=scarcity)):
        await set_progress(ctx.attempt_id, "layer5_teachers",
                           f"{subject} ({i + 1}/{len(subject_groups)})",
                           _pct("layer5_teachers", i / max(1, len(subject_groups))))

        qualified = [t for t in ctx.teachers if subject in (t["subjects"] or [])]
        if not qualified:
            raise PipelineError(
                f"No teacher is listed as teaching {subject}.",
                stage="layer5_teachers", school_fault=True)

        preference = await _ask_teachers(ctx, subject, groups, qualified, load)

        # Giving a class one teacher for all of its periods is the same
        # colouring problem layer 4 has with rooms: classes of this subject
        # conflict when they share a slot, and the teachers are the colours.
        # Assigned in arbitrary order, 9BIO2 found all seven Biology teachers
        # taken across its periods even though an assignment existed. The most
        # entangled class has to choose first.
        def entangled(group) -> int:
            mine = set(slots_by_group.get(group["id"], []))
            return sum(1 for other in groups
                       if other["id"] != group["id"]
                       and mine & set(slots_by_group.get(other["id"], [])))

        for group in sorted(groups, key=lambda g: (-entangled(g),
                                                   -len(g["students"]))):
            slots = slots_by_group.get(group["id"], [])
            pick = None

            ordered = sorted(
                qualified,
                key=lambda t: (0 if str(t["id"]) == preference.get(group["code"]) else 1,
                               load[t["id"]],
                               _jitter(str(t["id"]), ctx.seed)))
            for teacher in ordered:
                if (all((teacher["id"], d, p) not in teacher_busy
                        for d, p in slots)
                        and day_has_room(teacher, slots)):
                    pick = teacher
                    break

            # A teacher free at the right times but already full on one of
            # those days is better than no teacher at all, so the day cap
            # gives way rather than failing the run.
            if pick is None:
                for teacher in ordered:
                    if all((teacher["id"], d, p) not in teacher_busy
                           for d, p in slots):
                        pick = teacher
                        break

            if pick is None:
                raise PipelineError(
                    f"Every {subject} teacher is already busy when "
                    f"{group['code']} meets.",
                    stage="layer5_teachers", school_fault=True)

            for d, p in slots:
                teacher_busy[(pick["id"], d, p)] = group["code"]
                per_day[(pick["id"], d)] += 1
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


async def layer5b_gap_repair(ctx: Context) -> None:
    """
    Close mid-day gaps in a student's own schedule where a cheap move exists.

    Layer 3 packs periods to fit classes into the cycle; it has no notion of
    any one student's day as a whole, so a period can be full of OTHER
    students' classes while empty for this one. A gap here means a free
    period sitting between two of a student's classes on the same day - fine
    at the edges, disruptive in the middle, and never intended for the junior
    years a school has deliberately timetabled full.

    This can only run here, after rooms and teachers are assigned: the repair
    move needs to know a class's teacher and room to check the move is legal,
    and neither exists until Layer 4 and Layer 5.

    The move relocates ONE occurrence of a class the gapped student takes -
    from wherever else it meets to the gap slot - never touching the class's
    teacher, room, campus, or total period count. Because the count never
    changes, it cannot violate a min/max another layer already enforced; it
    can only ever be at least as good as what Layer 3 produced.

    Measured against a real generation before this was written: roughly a
    quarter of gaps close. The rest are blocked by the classes themselves -
    with 25-30 students in a class, most candidate slots are already occupied
    by someone in it via a different subject. This is a genuine improvement,
    not a guarantee of a gap-free timetable.
    """
    stage = "layer5b_gap_repair"
    slots_by_group = await _slots_by_group(ctx)
    teaching = [(p["day_number"], p["period_number"]) for p in ctx.teaching_periods]
    by_id = {g["id"]: g for g in ctx.groups}

    teacher_busy: dict[Any, set] = defaultdict(set)
    room_busy: dict[Any, set] = defaultdict(set)
    class_slots: dict[str, set] = {}
    student_slots: dict[str, set] = defaultdict(set)
    student_groups: dict[str, set] = defaultdict(set)
    group_students: dict[str, set] = {}

    for group in ctx.groups:
        gid = group["id"]
        gslots = set(slots_by_group.get(gid, []))
        class_slots[gid] = gslots
        if group.get("teacher_id"):
            teacher_busy[group["teacher_id"]] |= gslots
        if group.get("room_id"):
            room_busy[group["room_id"]] |= gslots
        roster = {s["id"] for s in group["students"]}
        group_students[gid] = roster
        for sid in roster:
            student_slots[sid] |= gslots
            student_groups[sid].add(gid)

    def gaps_for(sid: str) -> list[tuple[int, int]]:
        by_day: dict[int, set] = defaultdict(set)
        for d, p in student_slots[sid]:
            by_day[d].add(p)
        found = []
        for d, periods in by_day.items():
            lo, hi = min(periods), max(periods)
            for dd, pp in teaching:
                if dd == d and lo <= pp <= hi and pp not in periods:
                    found.append((d, pp))
        return found

    before = [(sid, g) for sid in sorted(student_slots) for g in gaps_for(sid)]

    moves: list[tuple[str, tuple[int, int], tuple[int, int]]] = []
    for sid, target in before:
        # `before` is a snapshot taken once, up front. Moving a class affects
        # every one of its students at once, so an earlier move made for a
        # DIFFERENT student's gap can already have filled this one as a side
        # effect - this ran live against real data and hit exactly that:
        # `allocation_registry_students_pkey` collided because the code below
        # excluded this student from its own clash check, trusting `target`
        # was still empty for them. Re-verify against current state first.
        if target in student_slots[sid]:
            continue
        day, _ = target
        moved = False
        for gid in sorted(student_groups[sid]):
            if moved:
                break
            group = by_id[gid]
            teacher, room, campus = (group.get("teacher_id"),
                                     group.get("room_id"), group.get("campus_id"))
            if not teacher or not room or target in class_slots[gid]:
                continue
            # At most a double on any one day - the cap layer 3 enforces on
            # first placement applies here too, or a repair could recreate
            # the "two doubles in a day" problem it was added to prevent.
            if sum(1 for d, _ in class_slots[gid] if d == day) >= 2:
                continue

            for origin in sorted(s for s in class_slots[gid] if s[0] != day):
                if target in teacher_busy[teacher] or target in room_busy[room]:
                    continue
                if any(other != sid and target in student_slots[other]
                      for other in sorted(group_students[gid])):
                    continue
                # The student's other classes already on this day must be at
                # the same campus - never invent a cross-campus jump the
                # travel layer has not validated.
                same_day_campuses = {
                    by_id[g2]["campus_id"]
                    for g2 in student_groups[sid]
                    for d3, _ in class_slots[g2] if d3 == day
                }
                if same_day_campuses and campus not in same_day_campuses:
                    continue

                teacher_busy[teacher].discard(origin); teacher_busy[teacher].add(target)
                room_busy[room].discard(origin); room_busy[room].add(target)
                class_slots[gid].discard(origin); class_slots[gid].add(target)
                for other in group_students[gid]:
                    student_slots[other].discard(origin)
                    student_slots[other].add(target)
                moves.append((gid, origin, target))
                moved = True
                break

    if moves:
        async with db.transaction() as conn:
            for gid, origin, target in moves:
                old_pid = _period_id(ctx, *origin)
                new_pid = _period_id(ctx, *target)
                if not old_pid or not new_pid:
                    continue
                await conn.execute("""
                    UPDATE class_group_slots
                    SET day_number = $3, period_number = $4, layout_period_id = $5
                    WHERE attempt_id = $1 AND class_group_id = $2
                      AND day_number = $6 AND period_number = $7
                """, ctx.attempt_id, gid, target[0], target[1], new_pid,
                    origin[0], origin[1])

                group = by_id[gid]
                if group.get("teacher_id"):
                    await conn.execute("""
                        UPDATE allocation_registry_teachers
                        SET layout_period_id = $1
                        WHERE attempt_id = $2 AND teacher_id = $3
                          AND layout_period_id = $4
                    """, new_pid, ctx.attempt_id, group["teacher_id"], old_pid)
                if group.get("room_id"):
                    await conn.execute("""
                        UPDATE allocation_registry_rooms
                        SET layout_period_id = $1
                        WHERE attempt_id = $2 AND room_id = $3
                          AND layout_period_id = $4
                    """, new_pid, ctx.attempt_id, group["room_id"], old_pid)
                await conn.execute("""
                    UPDATE allocation_registry_students
                    SET layout_period_id = $1
                    WHERE attempt_id = $2 AND layout_period_id = $3
                      AND student_id = ANY($4)
                """, new_pid, ctx.attempt_id, old_pid,
                    list(group_students[gid]))

    after = sum(len(gaps_for(sid)) for sid in student_slots)
    log.info("Gap repair: %d moves closed %d of %d gap instances "
             "(%d remain)", len(moves), len(before) - after, len(before), after)
    await stage_done(ctx.attempt_id, stage, {
        "moves": len(moves), "gaps_before": len(before), "gaps_after": after,
    })


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
    ("layer5b_gap_repair", layer5b_gap_repair),
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

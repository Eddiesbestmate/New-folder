"""
Requirements intake (PIPELINE.md steps 1-4).

A school describes how classes should be formed in their own words - "Year 11
Maths runs as three parallel classes, keep the accelerated group together,
Chemistry needs a lab" - and the AI turns that into structured class group
definitions. The school checks the interpretation, corrects anything wrong, and
confirms.

Two decisions shape this:

**Requirements belong to the school; definitions belong to a run.** The
`requirements` table is school-scoped and reusable - the same rules apply term
after term - while `class_group_definitions` carries a `timetable_id`. So
confirmed requirements are *materialised* into definitions when a generation
starts, rather than being tied to one timetable when they are written.

**Inferred values are marked as inferred.** The AI fills gaps the school did not
mention - a room type, a year level. Those are recorded in `inferred_fields` so
the confirmation screen can show them differently from what the school actually
said. A guess presented as a statement is how a timetable ends up quietly wrong.
"""

import json
import logging
import re
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from models import database as db
from routers.auth import CurrentUser, require_owner
from services import ai_cluster

log = logging.getLogger("klasser.intake")

router = APIRouter(tags=["intake"])

FORMATION_TYPES = ("explicit", "rule", "mixed", "raw_list")

# What the AI may fill in. Anything else it returns is ignored rather than
# stored - the same rule the editor and the exporter follow.
DEFINITION_FIELDS = ("subject", "year_level", "room_type", "is_double",
                     "is_accelerated", "formation_type", "class_count",
                     "max_size", "notes")


class RequirementIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    raw_input: str = Field(min_length=10, max_length=20000)


class InterpretationIn(BaseModel):
    """The school's corrections to what the AI understood."""
    groups: list[dict]


class IntakeError(Exception):
    def __init__(self, message: str, *, code: str = "intake_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def build_prompt(raw: str, subjects: list[str], campuses: list[str]) -> str:
    return f"""
You are reading a school's description of how their classes should be formed,
and turning it into structured definitions.

What the school wrote:
"{raw}"

Subjects this school teaches: {json.dumps(subjects)}
Campuses: {json.dumps(campuses)}

Return ONLY a JSON object:
{{
  "groups": [
    {{
      "subject": "Mathematics",
      "year_level": "Year 11",
      "class_count": 3,
      "max_size": 25,
      "room_type": "classroom",
      "is_double": false,
      "is_accelerated": false,
      "formation_type": "rule",
      "notes": "three parallel classes",
      "inferred": ["room_type", "max_size"]
    }}
  ],
  "unclear": ["anything you could not turn into a definition"],
  "summary": "one sentence describing what you understood"
}}

Rules:
- "subject" must be one of the subjects listed above. If they mention something
  that is not, leave it out of groups and name it in unclear.
- "formation_type" is one of: {', '.join(FORMATION_TYPES)}.
- "inferred" lists every field you filled in that the school did not actually
  state. Be honest about this - the school is shown these differently.
- If they described nothing you can structure, return an empty groups list.
Do not include any text before or after the JSON.
""".strip()


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def clean_groups(groups: list, subjects: set[str]) -> tuple[list[dict], list[str]]:
    """
    Keep what the AI returned that is real, and report what was not.

    A subject the school does not teach cannot be timetabled, so it is moved to
    `unclear` rather than saved - otherwise the first sign of trouble is a
    generation that cannot allocate it.
    """
    kept, rejected = [], []

    for group in groups or []:
        if not isinstance(group, dict):
            continue

        subject = str(group.get("subject") or "").strip()
        if subject not in subjects:
            rejected.append(subject or "(unnamed)")
            continue

        formation = group.get("formation_type")
        clean = {
            "subject": subject,
            "year_level": (str(group.get("year_level")).strip()
                           if group.get("year_level") else None),
            "room_type": (str(group.get("room_type")).strip()
                          if group.get("room_type") else None),
            "is_double": bool(group.get("is_double")),
            "is_accelerated": bool(group.get("is_accelerated")),
            "formation_type": formation if formation in FORMATION_TYPES else "rule",
            "notes": (str(group.get("notes"))[:300]
                      if group.get("notes") else None),
            "inferred": [f for f in (group.get("inferred") or [])
                         if f in DEFINITION_FIELDS],
        }
        for numeric in ("class_count", "max_size"):
            try:
                value = int(group.get(numeric))
                clean[numeric] = value if 0 < value <= 100 else None
            except (TypeError, ValueError):
                clean[numeric] = None

        kept.append(clean)

    return kept, rejected


# --- Submitting ---------------------------------------------------------------

@router.post("/requirements", status_code=status.HTTP_201_CREATED)
async def submit(payload: RequirementIn,
                 user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Interpret a plain-English description of how classes should be formed.

    Stored unconfirmed. Nothing reaches a generation until the school has looked
    at the interpretation and said yes.
    """
    school_id = user["school_id"]

    subjects = [r["subject"] for r in await db.fetch(
        "SELECT DISTINCT subject FROM subject_settings WHERE school_id = $1 "
        "ORDER BY subject", school_id)]
    if not subjects:
        raise HTTPException(
            422, "Add your subjects before describing how classes are formed.")

    campuses = [r["name"] for r in await db.fetch(
        "SELECT name FROM campuses WHERE school_id = $1 ORDER BY created_at",
        school_id)]

    try:
        raw = await ai_cluster.call(
            "task_interpret", build_prompt(payload.raw_input, subjects, campuses),
            stage="intake_interpret")
        parsed = _extract_json(raw)
    except (ai_cluster.AIError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not interpret that. Try describing one subject at a time, "
            "naming the subjects exactly as they appear in your subject list."
        ) from exc

    groups, rejected = clean_groups(parsed.get("groups"), set(subjects))
    unclear = [str(u) for u in (parsed.get("unclear") or [])] + [
        f"{name} is not one of your subjects" for name in rejected]

    interpretation = {
        "groups": groups,
        "unclear": unclear,
        "summary": str(parsed.get("summary") or "")[:500],
    }

    requirement_id = await db.fetchval("""
        INSERT INTO requirements (school_id, name, raw_input, interpreted_input)
        VALUES ($1, $2, $3, $4) RETURNING id
    """, school_id, payload.name, payload.raw_input,
        json.dumps(interpretation))

    log.info("Requirements %s interpreted for %s: %s group(s), %s unclear",
             requirement_id, school_id, len(groups), len(unclear))

    return {"id": str(requirement_id), "confirmed": False, **interpretation}


@router.get("/requirements")
async def list_requirements(user: CurrentUser,
                            limit: Annotated[int, Query(ge=1, le=100)] = 50
                            ) -> list[dict]:
    rows = await db.fetch("""
        SELECT id, name, raw_input, interpreted_input, confirmed, created_at,
               confirmed_at
        FROM requirements WHERE school_id = $1
        ORDER BY created_at DESC LIMIT $2
    """, user["school_id"], limit)

    return [
        {
            "id": str(r["id"]), "name": r["name"],
            "raw_input": r["raw_input"],
            "confirmed": r["confirmed"],
            "created_at": r["created_at"],
            "confirmed_at": r["confirmed_at"],
            "groups": (json.loads(r["interpreted_input"]).get("groups", [])
                       if r["interpreted_input"] else []),
        }
        for r in rows
    ]


async def owned(requirement_id: str, school_id: str) -> dict:
    row = await db.fetchrow("""
        SELECT id, name, raw_input, interpreted_input, confirmed, created_at,
               confirmed_at
        FROM requirements WHERE id = $1 AND school_id = $2
    """, requirement_id, school_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Requirements not found")
    return dict(row)


@router.get("/requirements/{requirement_id}")
async def get_requirement(requirement_id: str, user: CurrentUser) -> dict:
    row = await owned(requirement_id, user["school_id"])
    interpretation = (json.loads(row["interpreted_input"])
                      if row["interpreted_input"] else
                      {"groups": [], "unclear": [], "summary": ""})
    return {
        "id": str(row["id"]), "name": row["name"],
        "raw_input": row["raw_input"], "confirmed": row["confirmed"],
        "created_at": row["created_at"], "confirmed_at": row["confirmed_at"],
        **interpretation,
    }


@router.put("/requirements/{requirement_id}")
async def correct(requirement_id: str, payload: InterpretationIn,
                  user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Save the school's corrections to what the AI understood.

    A corrected field stops being inferred: the school has now said it, so the
    confirmation screen should stop flagging it as a guess.
    """
    school_id = user["school_id"]
    row = await owned(requirement_id, school_id)
    if row["confirmed"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "These requirements are confirmed. Submit new ones to change them.")

    subjects = {r["subject"] for r in await db.fetch(
        "SELECT DISTINCT subject FROM subject_settings WHERE school_id = $1",
        school_id)}
    groups, rejected = clean_groups(payload.groups, subjects)
    if rejected:
        raise HTTPException(
            422, f"Not subjects at this school: {', '.join(rejected)}")

    existing = (json.loads(row["interpreted_input"])
                if row["interpreted_input"] else {})
    interpretation = {
        "groups": groups,
        "unclear": existing.get("unclear", []),
        "summary": existing.get("summary", ""),
    }

    await db.execute("""
        UPDATE requirements SET interpreted_input = $3
        WHERE id = $1 AND school_id = $2
    """, requirement_id, school_id, json.dumps(interpretation))

    return {"id": requirement_id, "saved": True, **interpretation}


@router.post("/requirements/{requirement_id}/confirm")
async def confirm(requirement_id: str,
                  user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Accept the interpretation. From here it shapes the next generation.

    Confirming supersedes any earlier confirmed set: a school has one live
    description of how its classes are formed, not a growing pile.
    """
    school_id = user["school_id"]
    row = await owned(requirement_id, school_id)
    if row["confirmed"]:
        return {"id": requirement_id, "confirmed": True, "unchanged": True}

    interpretation = (json.loads(row["interpreted_input"])
                      if row["interpreted_input"] else {})
    groups = interpretation.get("groups", [])
    if not groups:
        raise HTTPException(
            422,
            "There is nothing to confirm - none of that could be turned into "
            "class definitions.")

    async with db.transaction() as conn:
        await conn.execute("""
            UPDATE requirements SET confirmed = false
            WHERE school_id = $1 AND confirmed = true
        """, school_id)
        await conn.execute("""
            UPDATE requirements SET confirmed = true, confirmed_at = now()
            WHERE id = $1 AND school_id = $2
        """, requirement_id, school_id)

    log.info("Requirements %s confirmed for %s (%s groups)",
             requirement_id, school_id, len(groups))
    return {"id": requirement_id, "confirmed": True, "groups": len(groups)}


@router.delete("/requirements/{requirement_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_requirement(requirement_id: str,
                             user: Annotated[dict, Depends(require_owner)]) -> None:
    await owned(requirement_id, user["school_id"])
    await db.execute("DELETE FROM requirements WHERE id = $1 AND school_id = $2",
                     requirement_id, user["school_id"])


# --- Feeding a generation -----------------------------------------------------

async def confirmed_for(school_id: str) -> Optional[dict]:
    """The school's live requirements, if they have confirmed any."""
    row = await db.fetchrow("""
        SELECT id, name, interpreted_input FROM requirements
        WHERE school_id = $1 AND confirmed = true
        ORDER BY confirmed_at DESC LIMIT 1
    """, school_id)
    if row is None or not row["interpreted_input"]:
        return None
    return {"id": str(row["id"]), "name": row["name"],
            **json.loads(row["interpreted_input"])}


async def materialise(timetable_id: str, school_id: str) -> int:
    """
    Write the confirmed requirements into this run's class group definitions.

    Called when a generation starts. Requirements are school-level and outlive
    any one timetable; definitions belong to the run that used them, so each
    generation gets its own copy and the history of what a run was told stays
    intact even if the requirements change afterwards.
    """
    confirmed = await confirmed_for(school_id)
    if not confirmed or not confirmed.get("groups"):
        return 0

    campuses = {r["name"].lower(): r["id"] for r in await db.fetch(
        "SELECT id, name FROM campuses WHERE school_id = $1", school_id)}

    rows = []
    for group in confirmed["groups"]:
        campus_id = campuses.get(str(group.get("campus") or "").lower())
        rows.append((
            school_id, timetable_id, group["subject"], group.get("year_level"),
            campus_id, group.get("room_type"), bool(group.get("is_double")),
            bool(group.get("is_accelerated")),
            group.get("formation_type") or "rule",
            json.dumps(group.get("inferred") or []),
        ))

    await db.executemany("""
        INSERT INTO class_group_definitions
            (school_id, timetable_id, subject, year_level, campus_id, room_type,
             is_double, is_accelerated, formation_type, inferred_fields)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
    """, rows)

    log.info("Materialised %s class group definition(s) for timetable %s",
             len(rows), timetable_id)
    return len(rows)

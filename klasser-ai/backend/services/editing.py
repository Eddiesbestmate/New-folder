"""
AI-assisted timetable editing (EDITING.md).

A school types a change in plain English. The AI turns it into a structured
proposal; deterministic code decides whether it is allowed; the school sees a
diff and approves it. Credits are charged on apply, not on asking.

Three rules shape this file:

**The AI never writes to the database.** It returns a proposal. Every field it
names is checked against an allowlist, every id it names is checked to belong to
this version, and the change is applied by code here.

**The proposal is validated by applying it.** Rather than reimplementing the
clash rules (which EDITING.md sketches and which would then drift from the
generator's), the change is applied inside a transaction, the full deterministic
checker runs on the result, and the transaction is rolled back. An edit is
therefore judged by exactly the rules a generation is judged by, and cannot
introduce something generation would have rejected.

**Only drafts are editable.** A published timetable is what the school is
running on. Editing it in place would change people's days with no record; they
publish a new version instead.
"""

import json
import logging
import re
from typing import Any, Optional

import config
from models import database as db
from services import ai_cluster, billing, deterministic
from services import settings as settings_service

log = logging.getLogger("klasser.editing")

# The only columns an edit may touch. `field` comes from AI output and is
# interpolated into SQL, so it is checked against this set first - never passed
# through (EDITING.md).
EDITABLE_FIELDS = {"layout_period_id", "room_id", "teacher_id"}

# The table each field's new value must exist in, so an invented UUID is caught
# before it reaches a foreign key.
FIELD_TABLES = {
    "layout_period_id": "layout_periods",
    "room_id": "rooms",
    "teacher_id": "teachers",
}

CHANGE_TYPES = {
    "move_class", "swap_rooms", "change_teacher", "change_room",
    "remove_class", "block_teacher",
}


class EditError(Exception):
    def __init__(self, message: str, *, code: str = "edit_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --- Context -----------------------------------------------------------------

async def build_context(version_id: str, request_text: str) -> dict:
    """
    The slice of the timetable the request is about.

    Sending the whole timetable would be tens of thousands of tokens and mostly
    irrelevant. Entities are matched by name against the request text, and only
    their entries - plus the periods those entities are free in - are sent.

    If nothing matches, a small sample goes instead so the model can say what it
    could not find rather than receiving nothing and guessing.
    """
    version = await db.fetchrow("""
        SELECT v.id, v.timetable_id, v.school_id, t.name, t.layout_id,
               l.days_in_cycle
        FROM timetable_versions v
        JOIN timetables t ON t.id = v.timetable_id
        JOIN timetable_layouts l ON l.id = t.layout_id
        WHERE v.id = $1
    """, version_id)
    if version is None:
        raise EditError("Timetable version not found", code="not_found")

    entries = await db.fetch("""
        SELECT te.id, te.class_code, te.subject, te.day_number,
               te.layout_period_id, te.teacher_id, te.room_id,
               lp.period_number, lp.label AS period_label,
               t.full_name AS teacher, r.name AS room, r.capacity,
               c.name AS campus,
               (SELECT count(*) FROM timetable_entry_students tes
                 WHERE tes.timetable_entry_id = te.id) AS students
        FROM timetable_entries te
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        JOIN teachers t ON t.id = te.teacher_id
        JOIN rooms r ON r.id = te.room_id
        JOIN campuses c ON c.id = te.campus_id
        WHERE te.version_id = $1
        ORDER BY te.class_code, te.day_number, lp.period_number
    """, version_id)

    words = set(re.findall(r"[A-Za-z0-9']+", request_text.lower()))

    def mentioned(entry) -> bool:
        haystacks = (entry["class_code"], entry["subject"], entry["teacher"],
                     entry["room"], entry["campus"])
        for value in haystacks:
            if not value:
                continue
            low = value.lower()
            if low in request_text.lower():
                return True
            # A surname on its own is how people refer to teachers.
            if any(part in words for part in low.split() if len(part) > 2):
                return True
        return False

    relevant = [e for e in entries if mentioned(e)]
    truncated = False
    if not relevant:
        relevant = list(entries[:25])
        truncated = len(entries) > 25

    # Periods nothing in the relevant set is already using, which is where a
    # move can plausibly go.
    periods = await db.fetch("""
        SELECT id, day_number, period_number, label
        FROM layout_periods
        WHERE layout_id = $1 AND period_type = 'teaching'
        ORDER BY day_number, period_number
    """, version["layout_id"])

    teacher_ids = {e["teacher_id"] for e in relevant}
    room_ids = {e["room_id"] for e in relevant}

    busy_teacher = {(e["teacher_id"], e["layout_period_id"]) for e in entries}
    busy_room = {(e["room_id"], e["layout_period_id"]) for e in entries}

    available = [
        {"layout_period_id": str(p["id"]), "day": p["day_number"],
         "period": p["period_number"],
         "label": p["label"] or f"P{p['period_number']}",
         "free_for_teachers": [str(t) for t in teacher_ids
                               if (t, p["id"]) not in busy_teacher],
         "free_rooms": [str(r) for r in room_ids
                        if (r, p["id"]) not in busy_room]}
        for p in periods
    ]

    unavailable = await db.fetch("""
        SELECT ta.teacher_id, ta.layout_period_id
        FROM teacher_availability ta
        WHERE ta.teacher_id = ANY($1::uuid[]) AND ta.available = false
    """, list(teacher_ids) or [])

    return {
        "timetable_name": version["name"],
        "days_in_cycle": version["days_in_cycle"],
        "periods_per_day": len({p["period_number"] for p in periods}),
        "total_entries": len(entries),
        "matched_by_name": not truncated and bool(
            [e for e in entries if mentioned(e)]),
        "relevant_entries": [
            {"entry_id": str(e["id"]), "class_code": e["class_code"],
             "subject": e["subject"], "day": e["day_number"],
             "period": e["period_number"],
             "layout_period_id": str(e["layout_period_id"]),
             "teacher": e["teacher"], "teacher_id": str(e["teacher_id"]),
             "room": e["room"], "room_id": str(e["room_id"]),
             "room_capacity": e["capacity"], "campus": e["campus"],
             "students": e["students"]}
            for e in relevant
        ],
        "available_periods": available,
        "constraints": {
            "teacher_unavailable": [
                {"teacher_id": str(u["teacher_id"]),
                 "layout_period_id": str(u["layout_period_id"])}
                for u in unavailable
            ],
            "editable_fields": sorted(EDITABLE_FIELDS),
            "not_editable": [
                "transport schedule - regenerate instead",
                "duty assignments - regenerate instead",
                "adding or removing a class - regenerate instead",
            ],
        },
        "_version": dict(version),
    }


def build_prompt(request_text: str, context: dict) -> str:
    """The interpretation prompt from EDITING.md."""
    payload = {k: v for k, v in context.items() if not k.startswith("_")}
    return f"""
You are interpreting a timetable edit request for a school timetabling system.

The user's request: "{request_text}"

Current timetable context:
{json.dumps(payload, indent=2)}

Return ONLY a JSON object describing the proposed change:
{{
  "interpretation": "plain English description of what you understood",
  "change_type": "move_class | swap_rooms | change_teacher | change_room | remove_class | block_teacher",
  "changes": [
    {{
      "entry_id": "uuid of the timetable entry being changed",
      "entity_name": "11COM1",
      "field": "layout_period_id | room_id | teacher_id",
      "old_value": "uuid",
      "new_value": "uuid",
      "day_number": 4,
      "description": "Move 11COM1 from Period 3 to Period 5 on Day 4"
    }}
  ],
  "confidence": "high | medium | low",
  "clarification_needed": null
}}

Rules:
- entry_id and new_value must be ids that appear in the context above. Never
  invent one.
- Only these fields may change: layout_period_id, room_id, teacher_id.
- If the request is ambiguous, set clarification_needed to a question and
  return an empty changes list.
- If you cannot fulfil the request, explain why in interpretation and return an
  empty changes list.
Do not include any text before or after the JSON.
""".strip()


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a response that may have prose around it."""
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


# --- Interpreting ------------------------------------------------------------

async def interpret(version_id: str, request_text: str, user: dict) -> dict:
    """
    Turn a request into a checked, costed proposal. Charges nothing.

    The row is written whether or not the proposal is valid, so a school can see
    what was tried and the dev portal can see what the model is getting wrong.
    """
    version = await db.fetchrow("""
        SELECT id, school_id, status, timetable_id FROM timetable_versions
        WHERE id = $1 AND school_id = $2
    """, version_id, user["school_id"])
    if version is None:
        raise EditError("Timetable version not found", code="not_found")
    if version["status"] != "draft":
        raise EditError(
            f"Only a draft can be edited. This version is {version['status']}. "
            "Generate a new version, or roll back and edit that draft.",
            code="not_a_draft")

    context = await build_context(version_id, request_text)

    try:
        raw = await ai_cluster.call(
            "task_edit_interpret", build_prompt(request_text, context),
            stage="edit_interpret", timetable_id=str(version["timetable_id"]))
        proposal = _extract_json(raw)
    except (ai_cluster.AIError, json.JSONDecodeError, ValueError) as exc:
        raise EditError(
            "The request could not be interpreted. Try rewording it, naming the "
            "class, teacher or room exactly as it appears in the timetable.",
            code="interpretation_failed") from exc

    interpretation = str(proposal.get("interpretation") or "").strip()
    clarification = proposal.get("clarification_needed")
    changes = proposal.get("changes") or []

    verdict, detail = await _vet(changes, version_id, bool(clarification))
    if verdict == "pass":
        verdict, detail = await _dry_run(changes, version_id)

    edit_id = await db.fetchval("""
        INSERT INTO timetable_edits
            (version_id, school_id, requested_by, request_text,
             ai_interpretation, proposed_changes, status, validator_result,
             validator_detail, credits_charged)
        VALUES ($1,$2,$3,$4,$5,$6,'pending',$7,$8,$9)
        RETURNING id
    """, version_id, user["school_id"], user["id"], request_text,
        interpretation or "(no interpretation returned)",
        json.dumps(changes), verdict, detail,
        await settings_service.get_int("credit_edit_session", 10))

    log.info("Edit %s interpreted for version %s: %s (%s)",
             edit_id, version_id, verdict, detail or "ok")

    return await describe(str(edit_id), user["school_id"])


async def _vet(changes: list, version_id: str,
               clarification: bool) -> tuple[str, Optional[str]]:
    """
    Everything checkable without touching the timetable.

    This runs before the dry run because these failures have clear explanations,
    and because an invented UUID would otherwise surface as a foreign key error.
    """
    if clarification:
        return "clarify", None
    if not changes:
        return "fail", "No change was proposed."

    max_entries = 20  # EDITING.md: more than this means regenerating
    if len(changes) > max_entries:
        return "fail", (
            f"This would change {len(changes)} entries. Changes affecting more "
            f"than {max_entries} are a regeneration, not an edit.")

    for change in changes:
        field = change.get("field")
        if field not in EDITABLE_FIELDS:
            return "fail", (
                f"'{field}' cannot be edited. Only "
                f"{', '.join(sorted(EDITABLE_FIELDS))} can change.")

        entry_id = change.get("entry_id")
        owns = await db.fetchval("""
            SELECT 1 FROM timetable_entries WHERE id = $1::uuid AND version_id = $2
        """, entry_id, version_id) if _looks_like_uuid(entry_id) else None
        if not owns:
            return "fail", (
                "The proposed change refers to a timetable entry that is not in "
                "this version.")

        new_value = change.get("new_value")
        if not _looks_like_uuid(new_value):
            return "fail", f"'{new_value}' is not a valid {field}."

        exists = await db.fetchval(
            f"SELECT 1 FROM {FIELD_TABLES[field]} WHERE id = $1::uuid",  # noqa: S608
            new_value)
        if not exists:
            return "fail", (
                f"The proposed {field.replace('_id', '').replace('_', ' ')} "
                "does not exist.")

    return "pass", None


def _looks_like_uuid(value: Any) -> bool:
    return bool(value) and bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", str(value)))


async def _dry_run(changes: list, version_id: str) -> tuple[str, Optional[str]]:
    """
    Apply the change, run every hard rule, roll back.

    This is why edits cannot introduce a clash: the checker is the same one that
    decides whether a generated timetable is acceptable, so there is no second
    set of rules to keep in step.
    """
    try:
        async with db.transaction() as conn:
            await _write(conn, changes, version_id)
            result = await deterministic.check_solution(version_id, conn)
            # Whatever the outcome, this was only ever a rehearsal.
            raise _Rollback(result)
    except _Rollback as done:
        result = done.result

    if result.passed:
        return "pass", None
    return "fail", result.failures[0]


class _Rollback(Exception):
    """Carries the check result back out of the aborted transaction."""

    def __init__(self, result) -> None:
        super().__init__("dry run complete")
        self.result = result


async def _write(conn, changes: list, version_id: str) -> None:
    for change in changes:
        field = change["field"]
        if field not in EDITABLE_FIELDS:
            # Belt and braces: _vet has already refused these, but this is the
            # statement that interpolates, so it checks for itself.
            raise EditError(f"Field not editable: {field}", code="bad_field")

        if field == "layout_period_id":
            # The denormalised day_number has to move with the period, or
            # transport demand and the next edit's validation read a stale day
            # (EDITING.md, DATABASE.md).
            await conn.execute("""
                UPDATE timetable_entries
                SET layout_period_id = $1::uuid,
                    day_number = (SELECT day_number FROM layout_periods
                                   WHERE id = $1::uuid)
                WHERE id = $2::uuid AND version_id = $3
            """, change["new_value"], change["entry_id"], version_id)
        else:
            await conn.execute(f"""
                UPDATE timetable_entries SET {field} = $1::uuid
                WHERE id = $2::uuid AND version_id = $3
            """, change["new_value"], change["entry_id"], version_id)  # noqa: S608


# --- Applying ----------------------------------------------------------------

async def apply_edit(edit_id: str, user: dict) -> dict:
    """
    Commit a proposal the school has approved, and charge for it.

    Validation runs again here rather than trusting the verdict stored at
    interpretation time: another edit may have been applied in between, which
    can turn a clean proposal into a clash.
    """
    edit = await db.fetchrow("""
        SELECT e.id, e.version_id, e.school_id, e.request_text, e.status,
               e.proposed_changes, e.validator_result, e.credits_charged,
               v.status AS version_status, v.timetable_id
        FROM timetable_edits e
        JOIN timetable_versions v ON v.id = e.version_id
        WHERE e.id = $1 AND e.school_id = $2
    """, edit_id, user["school_id"])
    if edit is None:
        raise EditError("Edit not found", code="not_found")
    if edit["status"] == "applied":
        return {"already_applied": True, "edit_id": edit_id}
    if edit["status"] == "rejected":
        raise EditError("This edit was rejected.", code="rejected")
    if edit["version_status"] != "draft":
        raise EditError(
            f"This version is {edit['version_status']} and can no longer be "
            "edited.", code="not_a_draft")

    changes = json.loads(edit["proposed_changes"])
    verdict, detail = await _vet(changes, str(edit["version_id"]), False)
    if verdict == "pass":
        verdict, detail = await _dry_run(changes, str(edit["version_id"]))
    if verdict != "pass":
        await db.execute("""
            UPDATE timetable_edits
            SET validator_result = $2, validator_detail = $3 WHERE id = $1
        """, edit_id, verdict, detail)
        raise EditError(
            f"This change is no longer valid: {detail}", code="validation_failed")

    cost = edit["credits_charged"] or await settings_service.get_int(
        "credit_edit_session", 10)

    # Checked before the change lands, not after. Charging is deliberately
    # outside the write transaction below so a billing error cannot undo an
    # edit the school can already see - which means affordability has to be
    # settled first, or an edit could be applied and never paid for.
    state = await billing.account_state(str(edit["school_id"]))
    if state["account_blocked"]:
        raise billing.AccountBlocked(state["blocked_reason"])
    if state["billing_mode"] == "credits" and state["available"] < cost:
        raise billing.InsufficientCredits(cost, state["available"])

    async with db.transaction() as conn:
        await _write(conn, changes, str(edit["version_id"]))
        await conn.execute("""
            UPDATE timetable_edits
            SET status = 'applied', applied_at = now(),
                validator_result = 'pass', validator_detail = NULL
            WHERE id = $1
        """, edit_id)

    # Charged after the change lands, and outside its transaction: a billing
    # failure must not undo an edit the school can already see.
    charged = 0
    try:
        outcome = await billing.adjust(
            str(edit["school_id"]), -cost,
            f"Edit: {edit['request_text'][:60]}",
            created_by=user["email"], kind="edit_session")
        charged = -outcome["applied"]
    except billing.BillingError:
        log.exception("Could not charge for edit %s", edit_id)

    log.info("Edit %s applied to version %s by %s (%s credits)",
             edit_id, edit["version_id"], user["email"], charged)

    try:
        from services import email

        row = await db.fetchrow("""
            SELECT t.name AS timetable, s.name AS school, v.version_number
            FROM timetable_versions v
            JOIN timetables t ON t.id = v.timetable_id
            JOIN schools s ON s.id = v.school_id
            WHERE v.id = $1
        """, edit["version_id"])

        await email.send_if_preferred(
            str(user["id"]), "notify_edit_applied", user["email"],
            "edit_applied", {
                "first_name": user.get("first_name", ""),
                "school_name": row["school"] if row else "your school",
                "timetable_name": row["timetable"] if row else "your timetable",
                "version_number": row["version_number"] if row else 1,
                "request_text": edit["request_text"],
                "ai_interpretation": await db.fetchval(
                    "SELECT ai_interpretation FROM timetable_edits WHERE id = $1",
                    edit_id) or "",
                "changes_summary": f"{len(changes)} "
                                   f"{'change' if len(changes) == 1 else 'changes'}",
                "credits_charged": charged,
                "view_url": f"{config.APP_URL}/output.html"
                            f"?version={edit['version_id']}",
            },
            school_id=str(edit["school_id"]))
    except Exception:  # noqa: BLE001
        log.exception("Could not send the edit-applied email for %s", edit_id)

    return {"applied": True, "edit_id": edit_id, "credits_charged": charged,
            "changes": len(changes)}


async def reject(edit_id: str, user: dict) -> dict:
    updated = await db.fetchval("""
        UPDATE timetable_edits SET status = 'rejected'
        WHERE id = $1 AND school_id = $2 AND status = 'pending'
        RETURNING id
    """, edit_id, user["school_id"])
    if not updated:
        raise EditError("No pending edit with that id.", code="not_found")
    return {"rejected": True, "edit_id": edit_id}


# --- Reading -----------------------------------------------------------------

async def describe(edit_id: str, school_id: str) -> dict:
    """One edit, with the before/after a school needs to judge it."""
    edit = await db.fetchrow("""
        SELECT e.id, e.version_id, e.request_text, e.ai_interpretation,
               e.proposed_changes, e.status, e.validator_result,
               e.validator_detail, e.credits_charged, e.applied_at,
               e.created_at, u.first_name || ' ' || u.surname AS requested_by
        FROM timetable_edits e
        JOIN users u ON u.id = e.requested_by
        WHERE e.id = $1 AND e.school_id = $2
    """, edit_id, school_id)
    if edit is None:
        raise EditError("Edit not found", code="not_found")

    changes = json.loads(edit["proposed_changes"])
    diff = []
    for change in changes:
        entry_id = change.get("entry_id")
        if not _looks_like_uuid(entry_id):
            continue
        before = await db.fetchrow("""
            SELECT te.class_code, te.subject, te.day_number,
                   lp.period_number, lp.label AS period_label,
                   t.full_name AS teacher, r.name AS room
            FROM timetable_entries te
            JOIN layout_periods lp ON lp.id = te.layout_period_id
            JOIN teachers t ON t.id = te.teacher_id
            JOIN rooms r ON r.id = te.room_id
            WHERE te.id = $1::uuid
        """, entry_id)
        if before is None:
            continue

        diff.append({
            "class_code": before["class_code"],
            "field": change.get("field"),
            "description": change.get("description"),
            "before": _describe_entry(dict(before)),
            "after": await _describe_after(dict(before), change),
        })

    return {
        "id": str(edit["id"]),
        "version_id": str(edit["version_id"]),
        "request_text": edit["request_text"],
        "interpretation": edit["ai_interpretation"],
        "status": edit["status"],
        "validator_result": edit["validator_result"],
        "validator_detail": edit["validator_detail"],
        "credits": edit["credits_charged"],
        "requested_by": edit["requested_by"],
        "created_at": edit["created_at"],
        "applied_at": edit["applied_at"],
        "can_apply": edit["status"] == "pending"
                     and edit["validator_result"] == "pass",
        "changes": diff,
        "change_count": len(changes),
    }


def _describe_entry(entry: dict) -> dict:
    return {
        "day": entry["day_number"],
        "period": entry["period_label"] or f"P{entry['period_number']}",
        "teacher": entry["teacher"],
        "room": entry["room"],
    }


async def _describe_after(before: dict, change: dict) -> dict:
    """The same entry as it would read once the change is applied."""
    after = _describe_entry(before)
    field, value = change.get("field"), change.get("new_value")
    if not _looks_like_uuid(value):
        return after

    if field == "layout_period_id":
        period = await db.fetchrow("""
            SELECT day_number, period_number, label FROM layout_periods
            WHERE id = $1::uuid
        """, value)
        if period:
            after["day"] = period["day_number"]
            after["period"] = period["label"] or f"P{period['period_number']}"
    elif field == "room_id":
        after["room"] = await db.fetchval(
            "SELECT name FROM rooms WHERE id = $1::uuid", value) or after["room"]
    elif field == "teacher_id":
        after["teacher"] = await db.fetchval(
            "SELECT full_name FROM teachers WHERE id = $1::uuid",
            value) or after["teacher"]
    return after


async def history(version_id: str, school_id: str) -> list[dict]:
    rows = await db.fetch("""
        SELECT e.id, e.request_text, e.ai_interpretation, e.status,
               e.validator_result, e.validator_detail, e.credits_charged,
               e.created_at, e.applied_at,
               u.first_name || ' ' || u.surname AS requested_by
        FROM timetable_edits e
        JOIN users u ON u.id = e.requested_by
        WHERE e.version_id = $1 AND e.school_id = $2
        ORDER BY e.created_at DESC
    """, version_id, school_id)
    return [dict(r) | {"id": str(r["id"])} for r in rows]

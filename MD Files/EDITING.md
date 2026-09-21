# AI-Assisted Timetable Editing

## Overview

After a timetable is generated, schools can request changes in plain English. The AI interprets the request, proposes a structured change, the deterministic validator checks it, and a diff is shown before committing.

The price is the `credit_edit_session` setting, currently **10 credits**, charged
on apply and not on asking. (Earlier drafts of this file said 5; the settings
table is authoritative and the dev portal can change it without a redeploy.)

---

## Edit flow

```
1. User views timetable (any version in draft status)
2. Clicks "Request edit"
3. Types request in natural language:
   e.g. "Move 11COM1 out of Period 3 on Day 4"
        "Mr Smith shouldn't teach on Fridays"
        "Swap the rooms for 11PHY1 and 11BIO1 on Day 6"
        "The Year 10 English classes need to be on Campus A"
4. AI interprets request → proposes structured change
5. Diff shown to user:
   Before: 11COM1 — Period 3 Day 4 — Room C14 — Patel
   After:  11COM1 — Period 5 Day 4 — Room C14 — Patel
6. Deterministic validator checks the proposed change
   - No double-bookings introduced
   - No constraint violations
7. If validator passes: user sees "Apply change" button
8. If validator fails: user sees reason, can try different request
9. User approves → change applied to timetable_entries
10. 5 credits deducted
11. New version NOT automatically created — user must publish
```

---

## AI interpretation prompt

```python
def build_edit_prompt(request_text: str, timetable_context: dict) -> str:
    return f"""
You are interpreting a timetable edit request for a school timetabling system.

The user's request: "{request_text}"

Current timetable context:
{json.dumps(timetable_context, indent=2)}

Return ONLY a JSON object describing the proposed change:
{{
  "interpretation": "plain English description of what you understood",
  "change_type": "move_class | swap_rooms | change_teacher | change_room | remove_class | block_teacher",
  "changes": [
    {{
      "entity": "class | teacher | room",
      "entity_id": "uuid",
      "entity_name": "11COM1",
      "field": "layout_period_id | room_id | teacher_id",
      "old_value": "uuid or value",
      "new_value": "uuid or value",
      "day_number": 4,
      "description": "Move 11COM1 from Period 3 to Period 5 on Day 4"
    }}
  ],
  "confidence": "high | medium | low",
  "clarification_needed": null
}}

If the request is ambiguous, set clarification_needed to a question.
If you cannot fulfil the request (e.g. no free period exists), explain in interpretation.
Do not include any text before or after the JSON.
"""
```

---

## Timetable context sent to AI

Only the relevant slice is sent — not the entire timetable (too many tokens).

```python
async def build_edit_context(timetable_id: str, version_id: str, request_text: str) -> dict:
    # Extract entity names mentioned in request
    # Pull only relevant classes, teachers, rooms, layout_periods
    # Include their current assignments for context

    return {
        "timetable_name": timetable.name,
        "days_in_cycle": layout.days_in_cycle,
        "periods_per_day": len(layout.periods),
        "relevant_entries": [...],   # classes/teachers/rooms mentioned in request
        "available_periods": [...],  # layout_periods free for the relevant entities
        "constraints": {
            "teacher_availability": [...],
            "room_capacity": [...],
            "campus_locks": [...]
        }
    }
```

---

## Deterministic validation of proposed change

```python
async def validate_edit(changes: list, version_id: str) -> tuple[bool, str]:
    for change in changes:
        if change['field'] == 'layout_period_id':
            # Check teacher not double-booked in new period
            teacher_clash = await db.fetchrow("""
                SELECT id FROM timetable_entries
                WHERE version_id = $1
                AND teacher_id = $2
                AND layout_period_id = $3
                AND day_number = $4
                AND id != $5
            """, version_id, change['teacher_id'],
                change['new_value'], change['day_number'],
                change['entry_id'])

            if teacher_clash:
                return False, f"Teacher already assigned in that period"

            # Check room not double-booked in new period
            room_clash = await db.fetchrow("""
                SELECT id FROM timetable_entries
                WHERE version_id = $1
                AND room_id = $2
                AND layout_period_id = $3
                AND day_number = $4
                AND id != $5
            """, version_id, change['room_id'],
                change['new_value'], change['day_number'],
                change['entry_id'])

            if room_clash:
                return False, f"Room already occupied in that period"

    return True, None
```

---

## Applying the change

`change['field']` comes from AI output and is interpolated into SQL, so it is checked
against a fixed allowlist first — never passed through.

When `layout_period_id` changes, the denormalised `day_number` on the entry must be
updated in the same statement, or transport demand and later edit validation will read
a stale day.

```python
EDITABLE_FIELDS = {'layout_period_id', 'room_id', 'teacher_id'}

async def apply_edit(edit_id: str, version_id: str, current_user):
    edit = await db.fetchrow("SELECT * FROM timetable_edits WHERE id = $1", edit_id)
    changes = json.loads(edit['proposed_changes'])

    for change in changes:
        if change['field'] not in EDITABLE_FIELDS:
            raise ValueError(f"Field not editable: {change['field']}")

    async with db.transaction():
        for change in changes:
            if change['field'] == 'layout_period_id':
                # Keep day_number in sync with the new period
                await db.execute("""
                    UPDATE timetable_entries
                    SET layout_period_id = $1,
                        day_number = (SELECT day_number FROM layout_periods WHERE id = $1)
                    WHERE id = $2 AND version_id = $3
                """, change['new_value'], change['entry_id'], version_id)
            else:
                await db.execute(f"""
                    UPDATE timetable_entries
                    SET {change['field']} = $1
                    WHERE id = $2 AND version_id = $3
                """, change['new_value'], change['entry_id'], version_id)

        await db.execute("""
            UPDATE timetable_edits
            SET status = 'applied', applied_at = now()
            WHERE id = $1
        """, edit_id)

        # Deduct 5 credits
        await deduct_credits(current_user['school_id'], 5,
            timetable_id=edit['timetable_id'],
            note=f"Edit: {edit['request_text'][:50]}")

    # Send notification
    await send_if_preferred(current_user['id'], 'notify_edit_applied',
        to=current_user['email'],
        template_name='edit_applied',
        context={...})
```

---

---

## As built

`services/editing.py` and `routers/editing.py`. Three differences from the
sketches above, each deliberate:

**The proposal is validated by applying it.** Rather than the two hand-written
clash queries above, the change is applied inside a transaction,
`deterministic.check_solution()` runs on the result, and the transaction is
rolled back. That is the same function that decides whether a *generated*
timetable is acceptable, so an edit is judged by exactly the rules a generation
is judged by and cannot introduce a clash the generator would have refused. It
also catches what the sketch does not: student clashes, room capacity, teacher
qualification, teacher availability, and `day_number` drift. `check_solution`
takes an optional connection for this.

**Validation runs twice** — once when the request is interpreted, and again when
it is applied. A proposal that was clean when it was made can be stale by the
time it is approved, because another edit may have been applied in between.

**Asking is free; applying is charged.** A school can try three wordings of the
same request without paying for the two that turned out to mean something else.
Affordability is checked before the change lands, because the charge is
deliberately outside the write transaction — a billing failure must not undo an
edit the school can already see.

Every request is stored whether or not it was valid, so the school can see what
was tried and the dev portal can see what the model is getting wrong.

**Only drafts are editable.** A published timetable is what the school is
running on; changing it in place would move people's classes with no record.
They roll back or generate a new version instead.

---

## What cannot be edited via AI

- Changes that would require re-running the full allocation (e.g. "remove this entire subject from the timetable")
- Changes that would cascade to more than 20 entries at once
- Changes to transport schedule (must regenerate)
- Changes to duty assignments (must regenerate)

For these, the AI tells the user to regenerate with updated requirements.

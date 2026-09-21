# Onboarding Wizard

## Overview

Mix of mandatory and optional steps. School name, campuses, and billing are mandatory before anything else. All other steps can be completed in any order and returned to later. A progress indicator shows completion status. Schools cannot generate a timetable until all mandatory steps plus at least teachers, students, and rooms are complete.

---

## Steps

### Mandatory (must complete in order before optional steps unlock)

**Step 1 — School details** (mandatory)
- School name
- Timezone (dropdown: NSW/VIC/TAS/ACT, QLD, SA, NT, WA)
- Number of campuses
- Campus names and addresses
- Ticks step_school_details, step_campuses

**Step 2 — Billing** (mandatory)
- Show credit packages
- Trial option if not yet used
- PAYG option (add fake card)
- Invoiced billing option (application form)
- Ticks step_billing

---

### Optional (any order, but all needed before generating)

**Step 3 — Timetable layout**
- Days in cycle
- Periods per day
- Period times
- Break and lunch times
- Duty slots
- Ticks step_layout

**Step 4 — Import teachers**
- Upload any format
- AI interprets
- Confirm and import
- Or: manual entry
- Ticks step_teachers (when at least 1 teacher exists)

**Step 5 — Import students**
- Same flow as teachers
- Ticks step_students

**Step 6 — Import rooms**
- Same flow
- Ticks step_rooms

**Step 7 — Configure subjects** (optional but recommended)
- Set hard/soft max sizes
- Set room type requirements
- Set campus locks
- Set code prefixes
- Ticks step_subjects

**Step 8 — Configure transport** (optional — only if multi-campus)
- Add bus fleet
- Set routes and travel times
- Ticks step_transport

---

## Progress indicator

Shown on dashboard and at top of onboarding wizard:

```
School details  ✓
Billing         ✓
Layout          ✓
Teachers        ✓  (98 teachers)
Students        ✓  (712 students)
Rooms           ✓  (64 rooms)
Subjects        ○  Optional — recommended
Transport       ✓

Ready to generate? ✓ Yes
```

---

## Sandbox mode

Available from the onboarding welcome screen. One sandbox run per school, free (no credits).

**What it does:**
- Loads pre-seeded Westfield College dummy data (included in database seed)
- Runs a real generation using the sandbox school's data
- Shows the full pipeline progress
- Shows the full timetable output
- Output is clearly watermarked "SANDBOX — not a real timetable"
- User cannot publish or export sandbox output
- Sandbox output is deleted after 24 hours

**What it does not do:**
- Use the school's own data
- Count toward any credit balance
- Allow editing or exporting

**Implementation:**

```python
# A sandbox school is pre-seeded in the database with id stored in settings
# When sandbox is requested:
async def run_sandbox(requesting_school_id: str):
    sandbox_school_id = await get_setting('sandbox_school_id')

    # Create a generation attempt linked to sandbox school
    # but associate it with the requesting school for visibility
    attempt_id = await create_sandbox_attempt(
        sandbox_school_id, requesting_school_id
    )

    # Run full pipeline (same code, just different school_id)
    await pipeline.run(attempt_id)

    # Mark onboarding sandbox_used
    await db.execute("""
        UPDATE onboarding
        SET sandbox_used = true, sandbox_used_at = now()
        WHERE school_id = $1
    """, requesting_school_id)
```

---

## Completion

When all mandatory steps plus teachers, students, and rooms are done:

```python
async def check_onboarding_complete(school_id: str):
    ob = await db.fetchrow(
        "SELECT * FROM onboarding WHERE school_id = $1", school_id
    )
    required = ['step_school_details', 'step_campuses', 'step_billing',
                'step_teachers', 'step_students', 'step_rooms']
    if all(ob[step] for step in required):
        await db.execute("""
            UPDATE onboarding
            SET completed = true, completed_at = now()
            WHERE school_id = $1
        """, school_id)
```

After completion, the onboarding banner on the dashboard disappears and the Generate button becomes active.

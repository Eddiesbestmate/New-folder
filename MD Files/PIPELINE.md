# AI Pipeline

## Overview

The pipeline runs as a background job. The user submits requirements, confirms the AI interpretation, and can leave the page. They receive an email when complete.

Progress is streamed to the frontend via SSE while they are on the progress page.

---

## Pre-generation

```
1. User submits requirements (text, uploaded files, or both)
2. Gemini 2.5 Flash interprets requirements into structured class group definitions
3. Interpretation shown to user on confirmation screen
   - Confirmed values shown clearly
   - Inferred values highlighted with explanation
   - User corrects anything wrong
4. User confirms — class_group_definitions written to database
5. Deterministic pre-validator checks for contradictions
   - Duplicate class codes
   - Students enrolled in conflicting subjects
   - Room type requirements that cannot be met
   - Teacher availability conflicts obvious from data
6. If pre-validator fails → error shown to user, no credits charged
7. Cost estimated from school_complexity profile
8. User sees cost breakdown and confirms generation
9. Credits reserved (soft hold on school_credits.reserved)
10. Background job created and queued
```

---

## Model locking

At the start of each generation attempt, the primary model is locked:

```python
async def start_attempt(timetable_id, attempt_number):
    primary = await get_setting('task_block_campus_primary')  # 'magistral'
    attempt = await db.fetchrow("""
        INSERT INTO generation_attempts
        (timetable_id, attempt_number, locked_model)
        VALUES ($1, $2, $3)
        RETURNING *
    """, timetable_id, attempt_number, primary)
    return attempt
```

If the primary model rate limits during any chunk, the fallback model is locked for all remaining chunks in that attempt:

```python
async def handle_rate_limit(attempt_id, stage):
    fallback = await get_setting('task_block_campus_fallback')  # 'gemini'
    await db.execute("""
        UPDATE generation_attempts
        SET locked_model = $1
        WHERE id = $2
    """, fallback, attempt_id)
    await log_event(attempt_id, 'model_switch',
        f'Rate limit hit at {stage} — switching to {fallback} for remainder of attempt')
```

---

## Layer 1 — Subject and campus mapping

One chunk. Small context. Runs once.

**Input:** All subject_settings, campus list, layout_periods
**Output:** subject_map — which subjects run at which campus, room type requirements, double period flags

```json
{
  "subject_map": [
    {
      "subject": "Computing",
      "year_levels": ["Year 10", "Year 11", "Year 12"],
      "campus": "flexible",
      "room_type": "computer_lab",
      "is_double": false,
      "accelerated_allowed": true
    }
  ]
}
```

---

## Layer 2 — Class group formation

Chunked by year level (6 chunks for a 6-year-level school). Accelerated students appear in both their home year chunk and the higher year chunk with a flag preventing double-allocation.

**Per chunk input:**
- Class group definitions for this year level
- Eligible student pool (pre-filtered by deterministic code)
- Size constraints (class → subject → school default hierarchy)
- Balancing settings
- Code pattern from class_code_settings
- Existing codes (to avoid conflicts)
- Allocation registry state (which students already committed)

**Per chunk output:**
- Formed class groups with codes, student lists, balance scores
- Size warnings (soft max exceeded)

**After each chunk:** Deterministic code writes confirmed students to allocation_registry_students.

**Hard constraints enforced deterministically:**
- Hard max never exceeded
- No student appears twice in same block
- Class codes unique

**Soft constraints handled by AI:**
- Gender balance
- Ability balance
- Campus balance
- Size tolerance

---

## Layer 3 — Block assignment

Chunked by campus (Campus A chunk, Campus B chunk, cross-campus reconciliation chunk).

**Per chunk input:**
- Class groups for this campus
- Layout periods (which blocks exist)
- Double period pairs (consecutive blocks needed)
- Campus-locked subjects
- Existing block assignments

**Per chunk output:**
- Block assignment per class group

**Consistency feasibility check:** After this layer, deterministic code checks that every class group's block assignments are feasible across all 10 days given teacher and room constraints. If infeasible, loop back here before teacher/room work is done.

---

## Layer 4 — Room assignment

Chunked by campus. Fixed rooms pre-assigned by deterministic code before AI sees the problem.

**Room ranking (deterministic, done before AI call):**
```python
def rank_rooms(rooms, class_group, block):
    for room in rooms:
        if room.capacity < class_group.student_count:
            room.rank = 'undersized'  # excluded
        elif room.capacity < class_group.hard_max:
            room.rank = 'too_small'   # soft fail
        elif room.capacity <= class_group.hard_max * 1.1:
            room.rank = 'perfect'
        elif room.capacity <= class_group.hard_max * 1.25:
            room.rank = 'good'
        else:
            room.rank = 'oversized'
    return [r for r in rooms if r.rank != 'undersized']
```

AI picks from ranked list. Never offered an undersized room.

---

## Layer 5 — Teacher assignment

Chunked by subject group (all Science subjects together, all Maths subjects together, etc.) so a teacher who teaches multiple subjects has consistent availability across all their classes.

**Per chunk input:**
- Class groups needing teacher assignment for this subject group
- Available teachers with subjects, availability, max blocks
- Current allocation registry (teacher blocks already committed)
- Consistency registry (teacher-class pins from previous timetable)
- Hard pins pre-assigned by deterministic code before AI sees problem

**After each chunk:** Deterministic code writes to allocation_registry_teachers and class_consistency_registry.

---

## Layer 6 — Non-bus duty assignment

Chunked by day (10 chunks for 10-day cycle).

**Per chunk input:**
- All duty slots for this day
- Teachers available for each slot (not teaching, correct campus)
- Each teacher's duty count so far this cycle
- Teacher duty exemptions (`teacher_duty_exemptions` — exempt teachers are filtered out
  by deterministic code before the AI sees the pool)
- Max duties per teacher (`teachers.max_duties_per_cycle`, falling back to the
  `duty_default_max_per_cycle` setting when null)

**Constraints:**
- Hard: teacher not teaching during slot
- Hard: teacher must be at correct campus
- Hard: exemptions respected
- Hard: min staff per slot met
- Hard: max duties per cycle not exceeded
- Soft: duties spread evenly across cycle
- Soft: duty types rotated

---

## Layer 7 — Transport solver

Deterministic. No AI in this layer.

```python
async def solve_transport(timetable_id, attempt_id):
    # 1. For each block and route, count students who need transport
    demand = calculate_demand(allocation_registry_students)

    # 2. For each period with demand, assign buses
    for period in layout_periods:
        for route in routes:
            students_needed = demand[period][route]
            if students_needed == 0:
                continue
            trips_needed = ceil(students_needed / bus_capacity)
            buses_needed = assign_buses(route, period, trips_needed)

    # 3. Build full day chain per bus (including empty legs)
    for bus in buses:
        build_day_chain(bus, all_assignments)

    # 4. Write transport_schedule rows
    write_schedule(transport_schedule)
```

---

## Layer 8 — Bus duty assignment

Same structure as Layer 6 but after transport schedule is known. Bus duty slot times reference transport departure/arrival times. Otherwise identical logic.

---

## Layer 9 — Mentor periods

One chunk. Small. Form groups already known from Layer 2.

Assigns mentor teacher and room to each form group for each day's mentor period. Tries to keep the same teacher and room across all days.

---

## Layer 10 — Full validation

All 5 validators run simultaneously (4 AI + 1 deterministic).

**Validators — VCE build (free tier):**
1. Gemini 2.5 Flash via OpenRouter (main)
2. GPT-OSS via OpenRouter (main) — different model family catches different issues
3. Groq (backup)
4. Mistral Small (last resort)

**Commercial upgrade:** swap validator 1 to Claude Sonnet, validator 2 to GPT-4o — one settings table change, no code changes.

```python
async def run_validation(attempt_id, solution):
    results = await asyncio.gather(
        run_ai_validator('gemini',       solution, attempt_id),  # main 1
        run_ai_validator('gpt_oss',      solution, attempt_id),  # main 2
        run_ai_validator('groq',         solution, attempt_id),  # backup
        run_ai_validator('mistral_small',solution, attempt_id),  # last resort
        run_deterministic_validator(     solution, attempt_id),
        return_exceptions=True
    )
    return evaluate_quorum(results)
```

**Quorum evaluation:**
```python
def evaluate_quorum(results):
    ai_responses = [r for r in results[:4]
                    if not isinstance(r, Exception)
                    and r.result not in ('rate_limited', 'error')]
    deterministic = results[4]

    # Deterministic must pass
    if deterministic.result == 'fail':
        return 'fail', deterministic.detail

    # Must have at least 2 AI responses
    if len(ai_responses) < 2:
        return 'insufficient_validators', 'Fewer than 2 validators responded'

    # Any fail blocks
    for r in ai_responses:
        if r.result == 'fail':
            return 'fail', r.detail

    return 'pass', None
```

---

## Failure and retry

```
Validation fails:
  → Log failure reason
  → Increment retry counter
  → If retries >= max_allocation_loops → surface error to user, refund dev errors
  → Otherwise: determine which layer caused the failure
  → Reuse unchanged layers from partial_solutions
  → Re-run from the failed layer
  → Charge school_retry credits if school-caused
```

**School-caused failures:**
- Teacher constraint contradictions from school data
- Room capacity insufficient for class sizes the school defined
- Too many constraints for the available resources

**Dev-caused failures:**
- AI model rate limits
- API timeouts
- Schema violations (AI returned wrong format)
- Any unhandled exception in pipeline code

---

## Post-generation

```
Validation passes:
  → Write timetable_entries and timetable_entry_students
      day_number is denormalised — set it from the row's layout_period_id here,
      in this one place. deterministic.py re-checks the two agree.
  → Write transport_schedule
  → Write duty_assignments
  → Create timetable_version (version_number = previous + 1, status = 'draft')
  → Settle credits (deduct actual, refund difference from estimated)
  → Refund any dev-caused retry credits
  → Update generation_jobs status to 'complete'
  → Send generation_complete email (if user preference enabled)
  → SSE event: complete
```

---

## Global registry pattern

All registries are written by deterministic code only. AI reads them as context, never writes to them directly.

```python
# After AI returns a chunk result, deterministic code validates and writes:
async def commit_teacher_assignments(chunk_result, attempt_id):
    errors = validate_teacher_assignments(chunk_result)
    if errors:
        raise ValidationError(errors)

    for assignment in chunk_result['assignments']:
        await db.execute("""
            INSERT INTO allocation_registry_teachers
            (attempt_id, teacher_id, layout_period_id)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
        """, attempt_id, assignment['teacher_id'], assignment['layout_period_id'])

    # Also write to class_consistency_registry
    for group in chunk_result['class_groups']:
        if group.get('teacher_id') and group.get('room_id'):
            await db.execute("""
                INSERT INTO class_consistency_registry
                (attempt_id, class_group_id, teacher_id, room_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT DO NOTHING
            """, attempt_id, group['class_group_id'],
                group['teacher_id'], group['room_id'])
```

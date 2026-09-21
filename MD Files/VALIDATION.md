# Validation

## Two types of validation

**Deterministic validation** — mathematical rules. No ambiguity. Runs after each pipeline layer and again at the end. If it fails, it always fails.

**AI quorum validation** — AI models review the whole timetable for sense, fairness, and soft constraint satisfaction. A quorum must agree it passes.

---

## Deterministic checks — full list

### After Layer 2 (class group formation)
- [ ] No student appears in two class groups for the same subject
- [ ] No class group exceeds hard max size
- [ ] Class codes are unique within the timetable
- [ ] Accelerated students not double-assigned in same block

### After Layer 3 (block assignment)
- [ ] No student has two classes in the same block on the same day
- [ ] Double periods occupy consecutive blocks
- [ ] Campus-locked subjects only assigned blocks on their campus

### After Layer 4 (room assignment)
- [ ] No room assigned to two classes in the same block on the same day
- [ ] Room type matches class requirement (no computing class in a general room)
- [ ] Student count does not exceed hard max room capacity

### After Layer 5 (teacher assignment)
- [ ] No teacher assigned to two classes in the same block on the same day
- [ ] Teacher only assigned subjects in their subject list
- [ ] Teacher availability not violated (unavailable blocks)
- [ ] Teacher max blocks per day not exceeded

### After Layer 6 (duties)
- [ ] Teacher not assigned a duty during a block they are teaching
- [ ] Teacher on correct campus for duty slot
- [ ] Minimum staff count met for every duty slot
- [ ] Teacher max duties per cycle not exceeded
- [ ] Teacher duty exemptions respected

### After Layer 9 (full solution)
- [ ] All above checks (re-run on complete solution)
- [ ] Every `timetable_entries.day_number` matches its `layout_period_id`'s day_number
      (the denormalised column must not drift)
- [ ] Every class group has a teacher, room, and block for every day
- [ ] Mentor periods assigned for every form group for every day
- [ ] No student without a timetable entry for any block where they should have a class
- [ ] Total duty assignments match expected count for cycle

---

## AI quorum validation

Four AI validators run simultaneously (asyncio.gather). Each receives the full timetable solution and checks it independently.

**Validators — VCE build (free tier):**
1. Gemini 2.5 Flash via OpenRouter (main)
2. GPT-OSS via OpenRouter (main) — different model family, different failure modes
3. Groq (backup)
4. Mistral Small (last resort)

**Commercial upgrade path:** Swap validator 1 to Claude Sonnet and validator 2 to
GPT-4o. Both have stronger reasoning ability for spotting subtle scheduling issues.
One settings table change, no code changes needed.

**What AI validators check** (things deterministic cannot judge):
- Does the overall distribution of classes look fair across the cycle?
- Are there any obvious clustering problems (all difficult subjects on same day)?
- Does teacher workload look balanced across the cycle?
- Are class sizes reasonable and consistent across groups for the same subject?
- Does the transport schedule make logistical sense?
- Are there any patterns that look like a mistake?

**Quorum rules:**
- At least 2 AI validators must respond (not rate-limit or error)
- Deterministic must also pass
- Any single AI validator fail blocks regardless of others passing
- If fewer than 2 respond → retry validation once → if still insufficient → generation fails with 'insufficient_validators'

---

## As built — and why the prompt below is not what shipped

The first live run with real models failed **ten times in a row**. The
deterministic checker passed every one of them, and direct SQL against the
produced timetables confirmed it was right: zero teacher double-bookings, zero
room double-bookings, zero student clashes, zero duties overlapping a lesson.
All four AI validators were rejecting correct timetables.

```
deterministic  pass 5        <- and SQL agreed
gemini_free    fail 5
gpt_oss        fail 3, pass 2
groq           fail 5
mistral_small  fail 5
```

Two causes, both in this file's own design.

**The prompt asked them to re-check hard rules.** "A teacher in two places in
the same period", "a room used by two classes" — deterministic code already
enforces those against the whole database and is authoritative ("if it fails,
it always fails"). Asking a model to repeat that check adds nothing it can get
right and everything it can get wrong: validators reported room clashes that
provably did not exist. The prompt now tells them clashes are already ruled
out, and that seeing one means they have misread the data.

**Soft judgement and hard violation shared one verdict.** "Duty allocation that
looks unfair" sat in the same list as double-bookings, feeding one binary
pass/fail, with `quorum_any_fail_blocks` turning any of it into a hard stop. So
a validator noticing that one teacher had two duties in a day — which is
allowed, and which they described as "unfair compared to others" — refused the
school a timetable. Validators now return `concerns` separately: recorded,
shown, never blocking. `fail` is reserved for something that makes the timetable
unusable and that they can point at exact rows to prove.

**They were also guessing.** `summarise_day` sent period *numbers* and duty
*timings* ("lunch", "after_school") with no times, so no validator could tell
whether lunch duty overlapped period 5. Several said so outright — "typically
overlaps", "assuming". Both now carry real start and end times.

After those three changes the same generation passed on the first attempt.

**Validator prompt (original design — superseded, see above):**

```python
def build_validator_prompt(solution_summary: dict) -> str:
    return f"""
You are validating a school timetable. Review the following timetable solution and determine if it is acceptable.

{json.dumps(solution_summary, indent=2)}

Check for:
- Any teacher assigned to two classes at the same time
- Any room used by two classes at the same time  
- Any student enrolled in two classes at the same time
- Unreasonable class size distribution
- Obvious fairness problems in duty allocation
- Any patterns that suggest a scheduling error

Return ONLY a JSON object:
{{
  "result": "pass" | "fail",
  "reason": "null if pass, description of first problem found if fail",
  "confidence": "high | medium"
}}

Do not include any text before or after the JSON.
"""
```

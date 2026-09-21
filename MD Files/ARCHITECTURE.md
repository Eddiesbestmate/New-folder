# Architecture

## Four surfaces

```
1. Marketing site     — public, klasser.ai
                        Hero, features, how it works, testimonials, pricing
                        Sign up and login buttons

2. School portal      — authenticated school users
                        Google / Microsoft / password login via Supabase Auth
                        Owner and staff roles
                        Timetable generation, data management, billing

3. Dev portal         — authorised team members only
                        Google / password login, role = 'dev' in Supabase
                        System overview, all schools, all generations, errors
                        Billing management, credit adjustments
                        AI model assignments, API key pools
                        Validator settings, system settings
                        API health monitor, system logs

4. Fake card terminal — embedded in billing screens
                        Simulates card entry and payment processing
                        Randomises user input into realistic card data
                        Generates fake Stripe-format IDs
                        Dev portal toggle to simulate declined charges
```

---

## Multi-tenancy

Every school is an isolated unit. `school_id` appears on nearly every table. Supabase Row Level Security (RLS) enforces isolation at the database level — a misconfigured API call cannot leak another school's data.

```
schools
├── campuses          (school_id)
├── users             (school_id)
├── timetables        (school_id)
├── teachers          (school_id)
├── students          (school_id)
├── rooms             (school_id)
└── ... all data tables carry school_id
```

Dev portal has a special role that bypasses RLS for admin operations.

---

## Authentication architecture

```
School users:
  Supabase Auth → Google OAuth
               → Microsoft OAuth
               → Email + password
  All methods produce a Supabase JWT
  JWT verified on every FastAPI endpoint
  users table extends auth.users with school_id, role, login_method

Dev portal users:
  Supabase Auth → Google OAuth
               → Email + password
  users table role = 'dev'
  Separate login page, separate route guards

Invite flow:
  Owner enters email in Users page
  FastAPI calls Supabase Admin invite_user_by_email
  Supabase sends magic link email
  User clicks → /accept-invite page
  User sets name, chooses login method
  Account created, linked to school via invites table
```

---

## Backend structure

FastAPI with async throughout. Every route is async to support parallel AI calls.

```python
# main.py registers all routers
app.include_router(auth.router,          prefix="/auth")
app.include_router(onboarding.router,    prefix="/onboarding")
app.include_router(schools.router,       prefix="/schools")
app.include_router(intake.router,        prefix="/intake")
app.include_router(allocation.router,    prefix="/allocation")
app.include_router(transport.router,     prefix="/transport")
app.include_router(validation.router,    prefix="/validation")
app.include_router(editing.router,       prefix="/editing")
app.include_router(timetables.router,    prefix="/timetables")
app.include_router(import_router.router, prefix="/import")
app.include_router(export.router,        prefix="/export")
app.include_router(billing.router,       prefix="/billing")
app.include_router(support.router,       prefix="/support")
app.include_router(notifications.router, prefix="/notifications")
```

---

## Generation pipeline overview

See `PIPELINE.md` for full detail. Summary:

```
User submits requirements
  → Gemini 2.5 Flash interprets into structured tasks
  → User confirms interpretation
  → Deterministic pre-validator checks for contradictions
  → Pipeline starts (background job)

Layer 1  Subject and campus mapping       (1 chunk)   — Magistral
Layer 2  Class group formation            (chunked by year level) — Magistral
Layer 3  Block assignment                 (chunked by campus) — Magistral
Layer 4  Room assignment                  (chunked by campus) — Groq
Layer 5  Teacher assignment               (chunked by subject group) — Magistral
Layer 6  Non-bus duty assignment          (chunked by day) — Magistral
Layer 7  Transport solver                 (deterministic)
Layer 8  Bus duty assignment              (chunked by day) — Magistral
Layer 9  Mentor periods                   (1 chunk) — Magistral
Layer 10 Full validation                  (4 AI validators + deterministic, parallel)
           Validator 1: Gemini 2.5 Flash  — via OpenRouter
           Validator 2: GPT-OSS           — via OpenRouter
           Validator 3: Groq              — backup
           Validator 4: Mistral Small     — last resort

  → Pass: data formatted and saved as timetable version draft
  → Fail: loop back to relevant layer (up to 5 times)
  → Email sent on complete or failed
```

All AI calls go through `services/ai_cluster.py` which handles:
- Model selection per task from config
- Rate limit detection and fallback
- Round-robin key rotation within provider
- Retry logic with configurable delay
- Logging every call to pipeline_log

## Model selection — VCE build (free tier only)

All providers used in the VCE build are free-tier or free-to-access:

**Mistral Magistral** — free tier via api.mistral.ai. 2 RPM per key. Use 3 keys for
6 effective RPM. Handles all heavy allocation work. Optimal for structured JSON output
from large contexts — upgrading to a more expensive model here delivers no quality gain.

**Gemini 2.5 Flash via OpenRouter** — free tier. Handles interpretation, import,
export mapping, and editing. Also the allocation fallback if Magistral rate limits.

**Groq** — free tier. 30 RPM. Room allocation, transport formatting, backup validation.
Fast inference, sufficient for simpler structured output tasks.

**GPT-OSS via OpenRouter** — free tier. Second main validator. Different model family
to Gemini means different failure modes — two validators that disagree catch more issues
than two instances of the same model.

**Mistral Small** — free tier. Last resort validator only. Rarely called.

## Commercial upgrade path (post-VCE)

When going commercial, swap these two assignments in the settings table:
- `task_interpret` and `task_edit_interpret`: gemini → claude (Claude Sonnet 5)
- `validator_1`: gemini → claude
- `validator_2`: gpt_oss → gpt_4o

Add ANTHROPIC_API_KEY to .env. No other code changes needed — the ai_cluster
service reads model assignments from the database at runtime.

Rationale: Claude Sonnet handles ambiguous natural language better than Gemini
for interpretation and editing. GPT-4o is a stronger validator than GPT-OSS.
Combined additional cost: ~$0.10–0.15 per generation. Negligible at revised pricing.

## Commercial launch infrastructure additions

At commercial launch, add a proper job queue before any other infrastructure work:

```
Redis + RQ (Python Redis Queue)
  → Replaces FastAPI background_tasks
  → Handles concurrent generations safely
  → Automatic retry on infrastructure failures
  → Priority queues (urgent vs standard)
  → RQ Dashboard for visibility
```

Rate limit upgrades at commercial launch:
- OpenRouter: add payment method (free, 20→200 RPM)
- Mistral: committed contract at $100/month (→Startup tier, 20 RPM per key)
- Groq: add payment method (→Production tier, 600 RPM, no daily limit)
- Mistral keys: 3 at launch, add 2 more approaching 50 schools

---

## SSE progress updates

Generation runs as a background task. Frontend receives live updates via Server-Sent Events.

```python
# FastAPI SSE endpoint
@router.get("/generation/{timetable_id}/progress")
async def generation_progress(timetable_id: str):
    async def event_stream():
        while True:
            events = await get_new_events(timetable_id)
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

Frontend JS listens and updates the progress screen in real time.

---

## Frontend architecture

Plain HTML, CSS, Vanilla JS. No framework. Each page is a separate `.html` file. All API calls go through `js/api.js` which handles auth token injection and error handling centrally.

```javascript
// js/api.js
async function apiCall(method, path, body = null) {
    const token = supabase.auth.getSession()?.access_token;
    const res = await fetch(`${API_BASE}${path}`, {
        method,
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`
        },
        body: body ? JSON.stringify(body) : null
    });
    if (!res.ok) throw await res.json();
    return res.json();
}
```

---

## Key design principles

**Deterministic code enforces hard constraints. AI handles judgement calls.**
- Room capacity limits: deterministic
- Teacher double-booking: deterministic
- Class balancing: AI
- Constraint satisfaction: AI (with deterministic post-check)

**Model consistency within a generation attempt.**
- At attempt start, primary model is locked
- If it rate limits, fallback model is locked for the rest of that attempt
- Never swap mid-chunk

**Global registries prevent AI conflicts across chunks.**
- Deterministic code writes to registries after each chunk
- Next chunk reads current registry state
- AI never writes directly to registries

**Every AI call is logged.**
- Provider, model, key used, tokens, latency, result
- Errors flagged as dev-fault or school-fault
- Dev-fault errors trigger automatic credit refund

**Fail loudly, fail cheaply.**
- Deterministic pre-checks catch contradictions before AI runs
- Mid-pipeline deterministic checks catch AI errors before next layer
- School-caused retries charged. Dev-caused retries refunded.

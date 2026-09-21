# Build order

Build in this sequence. Each phase produces something testable before the next begins.

---

## Phase 1 — Project skeleton

Status: complete and verified against the live Supabase project, except the
backend's own direct Postgres connection (needs `DATABASE_URL`).

Verification scripts in `backend/`, all currently passing:

| Script | Checks |
|---|---|
| `check_schema_order.py` | 66 tables in 001, 149 FKs, dependency order (offline) |
| `smoke_test.py` | app boots, 16 routers register |
| `check_rls_leak.py` | all 70 live tables, enumerated from `pg_class` |
| `check_seed_data.py` | true row counts via service role - settings 62, sandbox complete |
| `check_constraints.py` | generated column, FK, NOT NULL, UNIQUE, PK all enforced |
| `check_pricing.py` | the marketing page quotes what `credit_packages` bills |
| `check_xss.py` | no school-supplied value reaches an HTML attribute unescaped |
| `audit.py` | tests, router auth, SQL interpolation, secrets, settings, portability |
| `verify_install.py` | constraints via direct SQL - **needs `DATABASE_URL`** |

- [x] Create `klasser-ai/` folder structure
- [x] `backend/main.py` — FastAPI app, CORS, router registration stubs
- [x] `backend/keys.py` — load from .env
- [x] `backend/.env` — Supabase URL, anon key, service role key, DATABASE_URL,
      ENCRYPTION_KEY. Resend still empty (Phase 12).
- [x] `backend/requirements.txt`:

```
fastapi
uvicorn[standard]
asyncpg
supabase
python-dotenv
resend
jinja2
cryptography
openai
httpx
apscheduler
python-multipart
openpyxl
```

`openai` is required — Mistral, OpenRouter and Groq all go through `AsyncOpenAI` with a
custom `base_url`. `zoneinfo` is stdlib on Python 3.9+ and must not be listed.
- [x] `backend/config.py` — PROVIDER_URLS, APP_NAME, FROM_EMAIL
- [x] Migrations written — `migrations/001_schema.sql` (66 tables, dependency
      order verified by `check_schema_order.py`), `002_rls.sql`,
      `003_seed_settings.sql`, `004_seed_sandbox.sql`, applied by `migrate.py`
- [x] **Run** the migrations against Supabase — all four applied, verified
- [x] Test: FastAPI starts, all 14 routers register, `/health` reports honestly
- [x] Test: database connects — PostgreSQL 17.6, 66 tables

**Network note.** This development machine sits behind a TLS-intercepting proxy.
Two consequences, both handled in code:

- HTTPS to Supabase fails certifi verification, so `truststore` is installed and
  `truststore.inject_into_ssl()` runs at the top of `main.py`. Verification still
  happens, against the OS trust store, rather than being disabled.
- The proxy resets TLS on port 5432, and `db.<ref>.supabase.co` is IPv6-only so it
  does not resolve here at all. `DATABASE_SSL=disable` is set in `.env` as the only
  way to reach Postgres from this network. **It sends the password and all rows in
  cleartext — local development only. Never set it on Render.** `models/database.py`
  logs a warning on every startup while it is set.

Run it:

```bash
cd klasser-ai/backend
python -m venv venv && venv\Scripts\activate     # Windows
pip install -r requirements.txt
copy .env.example .env                           # then fill in the values
python migrate.py --status                       # what would run
python migrate.py                                # apply
uvicorn main:app --reload --port 8000
```

---

## Phase 2 — Authentication

Password login only for now, by decision. OAuth is deferred, not designed around —
Supabase issues the same JWT whichever method is used, so enabling a provider and
calling `loginWithProvider()` is the whole change.

- [ ] Enable Google OAuth in Supabase dashboard — deferred
- [ ] Enable Microsoft OAuth in Supabase dashboard — deferred
- [x] `backend/services/supabase_client.py` — token verification (30s cache),
      admin client kept separate from the anon client
- [x] `backend/routers/auth.py` — get_current_user, require_owner, require_dev_role,
      `/signup`, `/me`, `/timezones`, `/check-dev`
- [x] `frontend/js/api.js` — token injection, flattens FastAPI validation errors
- [x] `frontend/js/auth.js` — login, signup, logout, route guards, OAuth stub
- [x] `frontend/css/style.css` — BRAND.md design system
- [x] `frontend/index.html` — marketing site with login/signup buttons
- [x] School signup page — creates school + owner + all 5 provisioning rows
- [x] School login page (password; OAuth buttons drop in later)
- [x] Dev portal login page with role check
- [x] `frontend/dashboard.html` — minimal landing page after login
- [x] Test: `test_auth_flow.py` — 26 checks against live Supabase, all passing
- [x] Manual browser test — signup and login confirmed working end to end

**Signup is not atomic across two systems.** The Supabase auth user is created
first, then a single database transaction provisions the school. If that
transaction fails, the auth user is deleted so the email is not left stranded as
"already registered". Verified by the `rejected signup created no school` check.

**Email confirmation is off** (`email_confirm=True` on user creation) because no
mail provider exists until Phase 12. Turn it off in `services/supabase_client.py`
once Resend is configured.

---

## Phase 3 — Onboarding wizard

- [x] `backend/routers/onboarding.py` — `/progress`, `/school-details`,
      `/billing-options`, `/billing-mode`
- [x] `frontend/onboarding.html` + `js/onboarding.js` — wizard UI
- [x] Step 1: school details + campuses
- [x] Step 2: billing — packages shown, mode recorded (fake terminal is Phase 10)
- [x] Steps 3-8: layout, data import, subjects, transport — the screens live in
      Phases 4 and 9 (`layout.html`, `import.html`, `data.html`,
      `transport.html`) and all exist. Progress tracks them from row counts, so
      they tick themselves once the data is there.
- [x] Dashboard progress indicator — reads `/onboarding/progress`
- [x] Sandbox: free run, watermarked output — `services/sandbox.py`,
      `GET`/`POST /onboarding/sandbox`, `migrations/013_sandbox_run.sql`.
      Test: `test_sandbox.py` — 38 checks, all passing.

**The marketing site was promising this before it existed.** Phase 17 put
"every new school gets a free run on sample data" on the front page in three
places while no router or service read `sandbox_used` at all — every reference
outside the test files was in `verify_install.py`. A school would have signed up
on that promise and found nothing. Caught by auditing the claims on the page
against the code rather than against the docs.

**It runs on the demonstration school's data, not the new school's.** Copying 48
sample students into a real school to show them the product would leave them
deleting demo data before they could start, and anyone who abandoned setup would
be left with a fake roll. So the generation happens on Westfield College and the
requesting school is granted read-only sight of that one timetable, recorded in
`onboarding.sandbox_timetable_id`.

**The grant is read-only, and that is enforced rather than assumed.**
`owned_version()` grew a `write=True` flag: read paths accept the grant, and
anything that changes a version does not. Publish, discard, edit and export were
already safe because each does its own `AND school_id = $2` lookup, but the test
asserts all four are refused and that the version is unchanged afterwards —
because "it happens to be safe today" is not the same as "it cannot happen".

**Once, claimed transactionally.** `sandbox_used` is set in the same
transaction that creates the timetable, so two clicks or two tabs cannot both
get a run. If queueing then fails, the run is given back rather than burned.
- [x] Test: `test_onboarding.py` — 34 checks against live Supabase, all passing
- [ ] Manual browser test of the wizard — needs a human

**Step flags are derived, not trusted.** `recompute()` sets each flag from a
count of the underlying rows on every read, so importing teachers by any route
ticks the Teachers step, and deleting the last room un-ticks Rooms. Only
`step_billing` is set explicitly, because it has no rows of its own until
Phase 10. Verified by the `step reverts when the data is removed` check.

**Campuses are never deleted by the wizard.** A campus missing from a submitted
list is kept, not removed — rooms, teachers, students and buses reference it.
The response says which were kept.

---

## Phase 4 — School data management

- [x] `backend/routers/schools.py` — CRUD for teachers, students, rooms, subjects,
      campuses, plus the timetable layout
- [x] `frontend/data.html` + `js/data.js` — tabbed data pages
- [x] `frontend/layout.html` + `js/layout.js` — layout builder
- [x] Test: `test_schools.py` — 43 checks against live Supabase, all passing
- [ ] Manual browser test of the data and layout pages — needs a human

**Import is blocked on Phase 5, not skipped.** `services/importer.py` calls
`ai_cluster.call('task_import_interpret', ...)`, and `ai_cluster` is Phase 5 —
with no rows in `api_keys` there is no provider to call. These three move to
directly after Phase 5:

- [x] `backend/services/importer.py` — parsing, AI mapping, validation, writing
- [x] `backend/routers/import_router.py` — upload, confirm, history
- [x] `frontend/import.html` + `js/import.js` — upload, mapping confirmation, summary
- [x] Test: `test_import.py` — 41 checks, all passing

**The AI only maps column headers.** It never sees the whole file, never decides
what to write, and never touches the database. Parsing, validation, duplicate
matching and writing are all deterministic, so a bad AI response produces a
wrong *mapping* — which the user reviews before anything is saved — never a
corrupt import.

**Import works with no AI at all.** If the provider is unreachable or rate
limited, `guess_mapping()` matches headers by normalised name and alias, and the
mapping screen says so. Verified by forcing the AI call to fail mid-test.

**PDF and image import is refused, not faked.** Those need the model to extract
the rows themselves rather than map columns — a much larger job. The uploader
returns a clear message telling the user to export CSV instead.

**Deletes are guarded, not cascaded.** A teacher or room referenced by
`timetable_entries` returns 409 naming what still points at it, rather than
deleting and orphaning a timetable. Deleting a teacher does remove their duty
exemption and availability rows, which belong to the teacher and nothing else.

**Layouts are versioned, not edited in place.** `timetable_entries` reference
individual `layout_periods` rows, so rewriting a layout already used by a
timetable would silently move classes. If the active layout is in use, saving
deactivates it and creates a new one; if unused, it is updated in place.

---

## Phase 5 — AI cluster service

- [x] `backend/services/encryption.py` — Fernet encrypt/decrypt, masking
- [x] `backend/services/settings.py` — cached settings reads, invalidated on write
- [x] `backend/services/ai_cluster.py` — key pools, round-robin, fallback, retry, logging
- [x] `backend/routers/dev.py` — key pools, model assignments, settings, health
- [x] Key pools loaded once at startup in `main.py` lifespan
- [x] Test: `test_ai_cluster.py` — 50 checks, all passing. Retry, rotation and
      fallback are driven through an injected fake client, so the logic is
      verified without credentials or network access.
- [x] Dev portal API key pools **page** (frontend) — built in Phase 16 with
      the rest of the dev portal; the endpoints it needs are done and tested.
- [ ] Real provider keys — needs Mistral x3, Groq, OpenRouter accounts.
      `ENCRYPTION_KEY` is generated and in `.env`.

**Key material never leaves the server.** Keys are stored Fernet-encrypted,
decrypted into memory at startup, and returned only as a masked hint
(`sk-mis...0000`). There is deliberately no reveal endpoint — a lost key is
rotated at the provider.

**Model locking is honoured.** `call()` takes `locked_model`; when set it
suppresses fallback entirely, so an attempt that has switched models stays
switched for every remaining chunk (PIPELINE.md).

**Retries rotate keys.** Each retry uses the next key in the provider's pool, so
three Mistral keys and three retries means every key is tried before the model
is abandoned. Non-retryable errors (400, 401) raise immediately rather than
burning the pool.

---

## Phase 6 — Core generation pipeline

Build layers in order. Test each before moving to next.

- [x] `backend/services/pipeline.py` — orchestrator, 11 stages
- [x] Layer 1: subject and campus mapping (AI, falls back to configured settings)
- [x] Layer 2: class group formation (AI, chunked by year level and subject)
- [x] Layer 3: period slot assignment (AI proposal, deterministic repair)
- [x] Layer 4: room assignment (deterministic — see note)
- [x] Layer 5: teacher assignment (AI preference, deterministic decision)
- [x] Layer 6: duty assignment (deterministic — see note)
- [x] Layer 7: transport solver (deterministic, per TRANSPORT.md)
- [x] Layer 8: bus duty assignment (deterministic — see note)
- [x] Layer 9: mentor periods
- [x] Global registries written after each layer
- [x] `backend/routers/allocation.py` — start, status, events, SSE progress
- [x] Migration 005 `class_group_slots` — Layer 3's output had nowhere to live
- [x] Test: `test_pipeline.py` — 44 checks, all passing
- [x] **Retry loop** — `run_with_retries()` loops back to the layer responsible,
      up to `allocation_max_loops`, carrying the earlier layers forward
- [x] Test: `test_retry.py` — 29 checks, including that reuse actually happens
- [x] `backend/routers/intake.py` — requirements submission and confirmation

**Requirements belong to the school; definitions belong to a run.** The
`requirements` table is school-scoped and reusable — the same rules apply term
after term — while `class_group_definitions` carries a `timetable_id`. So
confirmed requirements are *materialised* into definitions when a generation
starts, and each run keeps its own copy: the record of what a generation was
told survives the requirements being changed afterwards.

**Inferred values are marked as inferred.** The AI fills gaps the school did not
mention — a room type, a year level — and those go in `inferred_fields` so the
confirmation screen can show them differently from what the school actually
said. A guess presented as a statement is how a timetable ends up quietly wrong.

**Retries reuse, they do not restart.** `restart_stage()` routes a failure to the
earliest layer that could have caused it (teacher clash → Layer 5, student clash
→ Layer 3, oversize class → Layer 2), `copy_forward()` copies the still-valid
layers into the new attempt remapped onto fresh class group ids, and
`hydrate_groups()` rebuilds the in-memory state a mid-pipeline start never
constructed. Each try is its own `generation_attempts` row, so the history
stays visible.

**Every attempt uses a different seed.** A deterministic pipeline re-run on
identical inputs fails identically, so retrying would be pointless. The seed
breaks ties only — between equally constrained classes in Layer 3, between
equally loaded teachers in Layer 5 — leaving the heuristics intact. Measured
effect: 24 of 54 teacher assignments differ between seeds.

**A rejected draft is discarded.** `write_solution` runs before validation, so a
failed attempt has already written a version; `discard_version()` removes it
rather than leaving the school a timetable that failed review.

**A caution from building this.** The first implementation computed the restart
layer, logged it, and never passed it to `run()` — every retry silently re-ran
all nine layers while the event log claimed otherwise. Recovery tests passed
throughout. `test_retry.py` now asserts that Layers 2 and 3 do **not** appear in
the retry's `pipeline_state`, which is the only check that would have caught it.

**Layers 4, 6 and 8 are deterministic, where PIPELINE.md specifies AI.** Room
ranking excludes anything undersized or the wrong type before a choice is made,
and every duty rule is hard (not teaching, right campus, not exempt, under cap),
so in both cases filtering leaves no judgement for a model to exercise. The
`task_room_allocation`, `task_duty_assignment` and `task_transport_format`
settings exist and are currently unused. This is a deliberate divergence and
should be confirmed or reversed rather than left as drift.

**Scale bugs found by measuring a 700-student school** — none were visible on the
48-student sandbox:
- Layer 3 assigned slots greedily in arbitrary order and could not colour a dense
  conflict graph. Now most-constrained-first.
- Every write was row-at-a-time; tens of thousands of round trips blew the
  connection's command timeout. Now batched with `executemany`.
- Duty assignment excluded any teacher who taught at any point that day, rather
  than during the duty itself. Now checks real time overlap.

---

## Phase 7 — Validation

- [x] `backend/services/deterministic.py` — pre-validation, per-layer checks,
      full solution check
- [x] `backend/services/validators.py` — AI quorum, four validators in parallel
- [x] Quorum evaluation logic (deterministic authoritative, min responders,
      any fail blocks)
- [x] Dev-fault vs school-fault detection and logging
- [x] Test: `test_deterministic.py` — 12 checks, every rule fed a timetable that
      breaks it
- [x] Test: contradictory data → school-fault detected, no AI called
- [x] Retry loop (up to `allocation_max_loops`) — built in Phase 6 as
      `pipeline.run_with_retries()`; covered by `test_retry.py`. This line sat
      unticked long after the work was done.

**Validation is chunked by day, not truncated.** An earlier `summarise_for_validators()`
sent the first 400 entries of the whole cycle with nothing marking it a sample,
so a validator would report "no teacher is double-booked" having seen a quarter
of them. Each validator now gets one call per day — every entry for that day,
marked complete, plus cycle-wide aggregates computed over all entries.

Cost: validation is roughly 4x the generation. For a 700-student, 10-day school
that is ~440k input tokens across four validators, against ~121k for the
allocation itself.

**First live run against real providers** (`test_live.py`, sandbox school):
succeeded on attempt 3 in 397s, 54 entries, all hard constraints passed.
59,808 tokens billed across 70 calls — validation was ~55k of that, allocation
close to zero because the allocation models were unavailable and the
deterministic fallbacks carried it.

Two bugs surfaced that 274 passing stub tests had not:

- `locked_model` was applied to **every** AI call, not just the allocation
  layers, so the attempt's locked model overrode each task's configured model.
  A task reassigned in settings had no effect. Locking is now scoped to the
  three allocation tasks, and the initial lock reads
  `task_block_campus_primary` instead of a hardcoded `'magistral'`.
- A teacher could hold two duties at overlapping times — Layer 8 assigned bus
  duties without seeing Layer 6's yard duties. Both layers now share a `held`
  map, and `check_duties()` gained two checks it never had: duty-versus-duty
  overlap, and duty-versus-teaching overlap.

The second was found by the AI validators (`gpt_oss` and `groq` both flagged
it) while every deterministic check passed — the quorum doing exactly what
VALIDATION.md describes, on its first live run.

**Provider notes as at the live run.** `google/gemini-2.5-flash` on OpenRouter
returns 402 without credits, and no free OpenRouter model tested returns
reliable structured JSON (`try_free_models.py`: connection errors, malformed
JSON, 429s, or 403s across six candidates). Mistral's free tier is
discontinued. Groq works and was rate-limited on 7 of 18 calls.

**The deterministic checker had shipped with a bug.** Room capacity was grouped
by `class_code`, summing the same students once per meeting, so a class of 16
running four times reported 64 and failed against any room. Nothing caught it
because nothing called `check_solution()` until Layer 10 existed. Now grouped by
entry, with a regression test.

---

## Phase 8 — Generation progress and output

- [x] SSE progress endpoint — `routers/allocation.py` (`/progress`), plus polling
      endpoints for clients that cannot hold a stream open
- [x] `backend/routers/timetables.py` — estimate, listing, versions, summary,
      options, grid, transport, duties, publish
- [x] `frontend/progress.html` + `js/progress.js` — live progress with SSE
- [x] `frontend/generate.html` — readiness, cost breakdown, confirm
- [x] `frontend/output.html` + `js/output.js` — class, teacher, room, student,
      transport and duty views
- [x] `frontend/timetables.html` — history and version list
- [x] Timetable versioning — draft created after generation, publish archives
      the previous published version in one transaction
- [x] Test: `test_output.py` — 58 checks, all passing
- [x] Test: `test_sse.py` — 27 checks, all passing. The live progress stream:
      who may watch, what is sent, in what order, and when it stops.
- [ ] Manual browser test of the progress *page* — needs a human (EventSource,
      reconnection, the UI itself)

**"Needs a human" was covering too much.** The SSE stream sat untested for
months on the grounds that EventSource needs a browser. The browser half does —
but the server half is where every decision lives, and it is ordinary HTTP:
`httpx` streams it over the ASGI transport, and the stream ends by itself once
the job is terminal. That was the largest untested area in the codebase.

**The stream had no maximum lifetime.** If a job never reached a terminal state
— a worker killed mid-run, a job row deleted — it polled the database every
second for as long as the tab stayed open. Closing the tab cancels the
generator, so it was not a leak in normal use, but "not a leak in normal use" is
not a reason to run forever. `sse_max_seconds` (migration 014) now closes it
with a `stream_timeout` frame the page can act on, rather than going silent —
a silent stop looks exactly like a hung generation.

**Grouping is done server-side.** `/grid?view=teacher&key=<id>` returns one
teacher's week keyed `day-period`. Shipping a 1,750-entry cycle to the browser
to render one teacher's timetable would be slow and would duplicate grouping
logic that already exists in SQL.

**Progress survives a page reload.** The page reconstructs state from
`/status` and `/events` on load rather than assuming it saw the run from the
start, and picks the most recent attempt so a retry is visible. If the SSE
stream fails it falls back to polling instead of freezing.

**One test worth keeping.** `every teacher grid has one class per slot` compares
entry count to cell count: if two entries collapsed onto the same `day-period`
key the UI would silently hide a double-booking by overwriting the cell. The
deterministic checker forbids the clash; this checks the output layer cannot
conceal one.

---

## Phase 9 — Transport

- [x] Transport solver — demand calculation, bus assignment, empty leg chains.
      Lives in `pipeline.layer7_transport`, not a separate `transport_solver.py`:
      it needs the same `Context` (groups, slots, campuses) every other layer
      has, and passing that across a module boundary bought nothing.
- [x] `backend/routers/transport.py`
- [x] `frontend/transport.html` — fleet, routes, schedule view
- [x] Test: `test_transport.py` — 36 checks, all passing. Two-campus school with
      a class split across campuses, empty legs and day chains verified.

**Departure times are real.** The solver works backwards from the bell: arrival
is the period's `start_time`, departure is `travel_minutes` earlier, and an
empty repositioning leg lands just as the passenger leg departs. They were
placeholder `08:00`/`08:20` literals until the Phase 9 test caught it — which
would also have put every bus duty in Layer 8 at the wrong time.

---

## Phase 9.5 — Job queue (add before billing, required for commercial launch)

- [x] Postgres-backed queue — `job_queue` table, `FOR UPDATE SKIP LOCKED` to
      claim, `LISTEN`/`NOTIFY` to wake workers. **Redis/RQ was dropped:** RQ
      forks with `os.fork()`, so it does not run on Windows, and it is sync
      while the entire codebase is async. Postgres is already a dependency.
- [x] Replace FastAPI `background_tasks` with the queue in the allocation router
- [x] Worker process: `python worker.py`
- [x] Priority queues: `urgent` (school blocked, needs help) and `standard`
- [x] Dead letter queue for jobs that fail after max retries
- [x] Queue visibility at `GET /allocation/queue`
- [x] Test: `test_queue.py` — 37 checks, all passing. Concurrent claiming,
      backoff, dead-lettering, stale-worker recovery, tenant isolation.
- [ ] Rate limit upgrades:
  - [ ] OpenRouter: add payment method (free, 20→200 RPM — do this before demo)
  - [ ] Mistral: committed contract at $100/month (→Startup tier, 20 RPM per key)
  - [ ] Groq: add payment method (→Production tier, 600 RPM, no daily limit)
- [ ] Mistral keys: confirm 3 keys at launch, plan to add 2 more at 50 schools

## Phase 10 — Billing

- [x] `backend/services/billing.py` — estimate, reserve, settle, release, adjust
- [x] **Server-side enforcement** on `/allocation/start` — credits are checked
      and held before a generation begins
- [x] `backend/services/fake_terminal.py` — Luhn-valid card generation, seeded
      from input, simulated charges honouring `fake_terminal_success_rate`
- [x] `backend/routers/billing.py` — account, packages, purchase, card, mode,
      transactions, dev adjustment
- [x] Credit purchase flow through the simulated terminal
- [x] Dev portal credit management (`/billing/dev/adjust`, `/billing/dev/schools`)
- [x] Test: `test_billing.py` — 58 checks, all passing
- [x] PAYG post-generation charge — `billing.charge_payg()`, called by the
      worker after `settle`. A decline blocks the account and opens an
      outstanding charge at 20%/day.
- [x] Interest accrual job (APScheduler) — `jobs.accrue_interest`
- [x] Monthly invoice generation job — `jobs.generate_invoices`
- [x] Invoice enforcement job — `jobs.enforce_invoices`
- [x] `backend/services/scheduler.py` — APScheduler in the worker, not the API
- [x] `migrations/010_scheduled_jobs.sql` — `scheduled_job_runs` plus the unique
      indexes that make the jobs safe to repeat
- [x] Dev job control — `POST /billing/dev/jobs/{name}`, `GET /billing/dev/jobs`,
      `POST /billing/dev/outstanding/{id}/resolve`
- [x] `frontend/billing.html` — balance, packages, card terminal, history,
      invoices and outstanding debts
- [x] Test: `test_payg_jobs.py` — 74 checks, all passing

**This closed a real hole.** Before this phase `/allocation/start` never looked
at the balance — `can_afford` was computed for the confirm screen and nothing
enforced it, so a school on zero credits could generate without limit by
calling the API directly.

**Reserving locks the balance row.** `check_and_reserve` takes `FOR UPDATE` on
both the billing and credit rows, so two generations started in the same moment
cannot both pass a check only one of them can afford. The test funds a school
for exactly one run, fires two reservations concurrently, and asserts one wins
and one is refused.

**A failed generation is never charged.** The hold is released and the reason
recorded. School-caused *retries* within a successful run are surcharged
(ARCHITECTURE.md); a run that produced no timetable is free regardless of
fault.

**Settling is idempotent.** Background tasks and retries make double-invocation
realistic, and double-charging a school is the worst failure this file could
have.

**The scheduler is not where correctness lives.** APScheduler only decides
when to call a job. Whether the work happens is decided by `scheduled_job_runs`:
each job claims one row per `(job, date)` with `INSERT ... ON CONFLICT DO
NOTHING`, so exactly one caller proceeds. That makes the jobs safe to run from
several workers, to trigger by hand from the dev portal, and to call again after
a crash — none of which a cron entry alone can promise. Interest applied twice
in a day overcharges a school; a second invoice for the same month bills them
again.

**Interest is charged for the days that actually elapsed**, not one per run.
`last_interest_date` on each charge means a job that did not run for three days
— a deploy, an outage — catches up rather than quietly forgiving the debt.

**One timezone decides what "today" is.** `config.billing_date()` is the single
source, and the scheduler fires on the same zone. The first version stamped a
new debt with Postgres `CURRENT_DATE` (UTC) while the job compared against the
machine's local date; for most of the Australian day those differ by one, so the
first accrual charged two days' interest instead of one. It would have been
permanent in production, where servers run UTC. The test now asserts the debt's
date directly rather than only the arithmetic that follows from it.

**The scheduler runs in the worker, not the API.** Web processes are scaled
horizontally and restarted on every deploy; the worker is the one long-lived
process. Extra workers are harmless because of the day-claim above.

**A declined card never takes the timetable away.** The generation is finished
and the school keeps it. What a decline does is block *future* generations and
open a debt — enforcement, not clawback. Same for an overdue invoice: revoking
invoiced billing drops the school to prepaid credits, and the debt survives the
change.

**Two tests in this suite originally passed without testing anything** — a
refusal assertion that accepted `402 or 422` and was satisfied by the
pre-validator rejecting the school for having no teachers, and a concurrency
test that skipped itself because the school could afford both runs. Both now
assert exactly one outcome. Worth watching for the same shape elsewhere:
loose status-code sets and self-skipping conditionals are assertions that
cannot fail.

---

## Phase 11 — Versioning and editing

- [x] `backend/services/versioning.py` — publish, roll back, discard, compare
- [x] `backend/services/editing.py` — AI interpret, validate, apply
- [x] `backend/routers/editing.py` — request, apply, reject, history
- [x] Version comparison — `GET /timetables/version/{a}/compare/{b}`
- [x] Discard a draft — `DELETE /timetables/version/{id}`
- [x] Timetable version list and comparison UI (`timetables.html`)
- [x] Edit request form + diff view (`edit.html`, `js/edit.js`)
- [x] Test: `test_editing.py` — 64 checks, all passing

**An edit is validated by applying it and rolling back.** The proposal is
written inside a transaction, `deterministic.check_solution()` runs on the
result, and the transaction is aborted. That is the same function that decides
whether a *generated* timetable is acceptable, so an edit cannot introduce a
clash the generator would have refused — and there is no second set of rules to
keep in step. EDITING.md sketches two hand-written clash queries; those would
have missed student clashes, room capacity, teacher qualification, teacher
availability, and `day_number` drift.

**The AI never writes.** It returns a proposal. Every field it names is checked
against an allowlist before being interpolated into SQL, every id is checked to
exist and to belong to this version, and anything touching more than 20 entries
is refused as a regeneration. The test suite asserts all four refusals with a
stubbed model returning deliberately hostile output.

**Validation runs again at apply time.** A proposal that was clean when made can
be stale by the time it is approved, because another edit may have landed in
between.

**Asking is free; applying is charged.** A school can try three wordings without
paying for the two that meant something else. Affordability is checked before
the write, because the charge is deliberately outside the write transaction — a
billing error must not undo an edit the school can already see.

**Rollback is publish.** Publishing an archived version archives the live one
and restores it, so there is one code path rather than two that can drift.
Publishing is scoped per timetable, not per school: a school can run several
timetables, each with its own live version.

**Suites must leave the sandbox school as they found it.** This one credits it
so an edit is affordable, and restores the balance in cleanup. Without that,
`test_output`'s "cannot afford with a zero balance" check passed for the wrong
reason — it is the same failure mode as a global count in a cleanup assertion:
shared state that one suite writes and another reads.

**Publishing an empty version is refused.** `test_output` had been publishing a
bare version row with no entries, which the Phase 11 guard now rejects: making
it live would leave the school with an empty timetable. The fixture now copies
real entries, and the suite asserts the refusal too.

**Two test suites were leaking users into the sandbox school.** `test_output.py`
and `test_editing.py` move their user onto the sandbox school to get at its
data, then deleted users `WHERE school_id = <throwaway>`, which no longer
matched. Because `users.id REFERENCES auth.users(id)`, the orphaned row also
made the Supabase auth delete fail with a 500 — so every run stranded both a
database row and an auth account. Thirteen had accumulated. Both suites now
delete by user id, and the 500 is gone with it.

---

## Phase 12 — Email

- [x] `backend/services/email.py` — Resend integration, template rendering
- [x] `backend/templates/email/` — 25 templates plus a base layout and macros
- [x] Wire up email sends throughout the codebase — signup, invite, accept,
      deactivation, generation complete and failed, PAYG receipt and decline,
      purchase receipt, version published, edit applied, and all five sends
      from the scheduled billing jobs
- [x] `migrations/015_email_log.sql` — every attempt recorded
- [x] Test: `test_email.py` — 29 checks, all passing
- [x] **Delivery proven.** A real welcome email was sent and received.
- [ ] Verify `klasser.ai` at resend.com/domains — the last step, needs DNS

**A Resend key was already in `.env`.** Phase 12 had been treated as blocked on
an account that turned out to exist. The only remaining blocker is that
`klasser.ai` is not a verified sending domain: the API authenticates, the call
reaches Resend, and it answers *"The klasser.ai domain is not verified"*. Setting
`RESEND_FROM=onboarding@resend.dev` sends for real without a domain — which is
how delivery was proven end to end.

**Tests were firing real API calls at the provider.** Every suite signs up
`@example.com` addresses, and Resend rejects those explicitly. A full run was
making a hundred live calls against the daily quota and logging a wall of
failures. `email.deliverable()` now refuses RFC 2606 reserved domains before the
provider is ever contacted — they exist precisely so they can never receive
mail, so an attempt is a guaranteed bounce either way.

**An email can never break what triggered it.** Every send is wrapped and
`send()` swallows its own errors: a provider outage must not fail a generation
that succeeded or undo a refund that was paid. Wiring the welcome email into
signup broke this on the first attempt — an `else:` clause left the failure
`HTTPException` on the success path, which would have 500'd every signup. Caught
by reading the result rather than trusting the edit.

**`StrictUndefined` is on.** A template referencing a variable nobody passed
raises at render instead of quietly emailing "Hi ," to a school.
`check_templates()` renders all 25 with sample context, so a typo is found on
deploy rather than the first time a card declines.

**The email log must not pin the rows it describes.** Migration 015 gave
`email_log` plain foreign keys, which meant deleting a user failed because an
email had once been sent to them — it broke the cleanup in every existing
suite. Both halves of that were wrong: a record of "we emailed this person"
should neither block deleting them nor vanish with them. Migration 016 makes
both `ON DELETE SET NULL`; the recipient address is plain text and survives, so
the record stays meaningful.

**A glibc-only date format was crashing on Windows.** The deactivation email
used `strftime` with the no-pad flag, which strips a leading zero on Linux and
raises `ValueError: Invalid format string` on Windows. It would have worked on
Render and failed on every developer machine — here it failed locally and would
have worked in production, which is the same bug wearing the other face.
`config.long_date()` replaces all three uses, and `audit.py` now fails on the
directive so it cannot come back. Verified by reintroducing the bug and
confirming the audit catches it.

---

## Phase 13 — Notifications and support

- [x] `backend/routers/notifications.py` — preferences, plus `wants()` /
      `recipients()` / `owner_of()` for Phase 12 to call
- [x] `frontend/notifications.html` — preference toggles, debounced auto-save
- [x] `backend/routers/support.py` — raise, list, withdraw; dev queue
- [x] `frontend/support.html` — ticket form, ticket list, contact details
- [x] Dev portal support queue — `frontend/dev-support.html`, with the pipeline
      log and error log for an attached timetable
- [x] `migrations/012_support_notes.sql` — internal notes, resolved_by,
      first_response_at
- [x] Test: `test_support.py` — 60 checks, all passing

**Internal notes must not leak, so the query cannot select them.** Every
school-facing query names its columns through one `SCHOOL_COLUMNS` constant that
does not include `internal_notes`, rather than selecting `*` and deleting the
key afterwards. Adding a dev-only column later therefore cannot start leaking it
by default. The test asserts the note is absent from the school's response body,
not merely that the API did not mean to send it.

**Triage is deterministic, not AI.** A school that says it is blocked reaches
the top of the queue on the words alone — no model call, no cost, no waiting.
The same phrases score lower on a data ticket than a billing one, because
"blocked" means something different there.

**'Closed' and 'resolved' are different things.** A school may withdraw its own
ticket (closed); only the dev portal may mark one resolved. Letting a school
resolve its own would make the queue's resolution figures meaningless.

**`first_response_at` is stamped once.** It uses `coalesce`, so a later edit
does not overwrite it and response time stays measurable.

**Preferences are columns, not a JSON blob.** A typo in a key is then a database
error rather than a setting that silently does nothing. Every statement names
every column and passes NULL for the ones a request did not send, so a partial
update needs no dynamic SQL at all — and adding a preference fails loudly in the
update statement instead of quietly not saving.

---

## Phase 14 — CSV export templates

- [x] `backend/services/exporter.py` — template-driven CSV generation
- [x] `backend/routers/export.py` — fields, interpret, templates, preview, download
- [x] `frontend/export-templates.html` — define, preview, save templates
- [x] Export button on output page, with preview and download
- [x] Test: `test_export.py` — 54 checks, all passing. A Sentral-format template
      against a real generated timetable, verified by parsing the CSV back.

**A template is a presentation instruction, never a query.** One fixed SQL
statement per output type fetches everything; columns are resolved from the
resulting row in Python. A hostile or broken template can therefore produce a
wrong-looking CSV but cannot reach data the school does not own. The test feeds
the mapper `teacher.salary` and `'; DROP TABLE users; --` as field names and
asserts both are dropped and reported rather than saved.

**A pasted header row costs nothing.** `ClassCode,TeacherSurname,RoomName,...`
is matched by name, with no AI call — which is the common case, since it is
exactly what a school's SIS documentation gives them. Only an unrecognised prose
description reaches the model, and if the model is unavailable the deterministic
matches are still offered rather than an error page.

**Written with the `csv` module, not by joining strings.** A room called
`Hall, "Main" O'Brien` has to be quoted correctly or every later column lands one
place out and the school's SIS silently imports rubbish. The test renames a real
room to exactly that and asserts the parsed row still has five fields.

**Downloads go through `fetch` with the auth header, not a plain link.** A link
cannot carry a bearer token, and putting one in the query string would write it
into every proxy log between the school and us.

---

## Phase 15 — Users and invite flow

- [x] `backend/routers/users.py` — invite, accept, roles, deactivation.
      **Not in `schools.py`** as planned: that file is already 600+ lines of
      school *data*, and this is account management. Mounted at `/users`.
- [x] Supabase Admin calls — `invite_auth_user`, `find_auth_user`,
      `set_password` in `services/supabase_client.py`
- [x] `frontend/users.html` — user list, invite form, pending invites
- [x] `frontend/accept-invite.html` — accept and set up account
- [x] Account deactivation, and reactivation
- [x] Test: `test_users.py` — 70 checks, all passing. The whole cycle against
      live Supabase: invite, accept, sign in as the new person, read the
      school's data, deactivate, confirm access stops.

**The invite flow does not depend on email.** Supabase sends the link when the
project has a mail provider; measured today it returns
`HTTP 429: email rate limit exceeded` on the free tier, so the fallback is the
path that actually runs — the account is still created and the accept link is
returned to the owner to pass on. The UI says which happened. Phase 12 improves
this rather than unblocking it.

**The token is ours, not the Supabase user id.** AUTH.md specifies
`token = supabase user id`, but the accept page is reached *before* the invitee
has any session, so the token is the only credential protecting it — and a user
id is not a secret. It is 32 bytes from `secrets.token_urlsafe`, single-use, and
marked accepted inside the same transaction that creates the user, so two
simultaneous requests cannot both redeem it.

**A school can never be locked out of its own account.** The last active owner
cannot be demoted, deactivated, or deactivate themselves. Without those guards a
single click leaves a school with nobody who can invite, buy credits or publish,
and no self-service way back.

**Deactivation does not delete the auth user.** AUTH.md sketches
`admin.delete_user()` to revoke sessions; that is neither reversible nor
possible — `users.id` references `auth.users(id)`, so the delete fails while our
row exists (the same 500 the Phase 11 audit traced). It is not needed either:
`get_current_user` checks `is_active` against our own database on every request,
so a still-valid JWT buys nothing. The test asserts the deactivated user's
existing token is refused on the very next call, not merely at expiry.

**A revoked invite cleans up the account Supabase made**, but only if it was
never accepted — otherwise that is a real user. Without this the address could
never be invited again.

---

## Phase 16 — Dev portal

- [x] Model assignment management
- [x] API key pools management
- [x] Validator settings
- [x] System settings
- [x] API health monitor
- [x] System logs — plus token spend per model over 7 days
- [x] All schools view
- [x] All generations view
- [x] Error log with refund actions
- [x] All users view
- [x] Credits management (manual adjustment)
- [x] Invoices view
- [x] Outstanding charges view
- [x] Invoiced billing applications — both halves: schools apply through
      `POST /billing/applications`, devs decide at `/dev/applications/{id}/decide`
- [x] `frontend/dev.html` + `js/dev.js` — eight tabs over one page
- [x] Test: `test_dev_portal.py` — 51 checks, all passing

**Refunds were not idempotent, and the test caught it.** `billing.adjust()` had
no `timetable_id` parameter, so a refund wrote a `credit_transactions` row with
a null timetable — and the duplicate check, which looks for an existing refund
against that timetable, could never match. A dev clicking Refund twice would
have paid the school twice. `adjust()` now takes `timetable_id`, the refund
passes it, and the suite asserts the second attempt returns `already_refunded`
with the balance unmoved.

**`invoiced_billing_applications` existed since migration 001 with nothing
reading or writing it.** Most schools cannot pay by card at all — they raise a
purchase order — so this was the missing path that makes the product buyable by
a real school. Approval grants permission rather than switching the school over;
they still choose, which keeps a record of who decided what they are on. The
test asserts a school cannot select invoiced billing until approved.

**Two of my own checkers caught my own new code.** `audit.py` flagged a
`for/else` in the new test whose else branch reported an unconditional pass —
the same "assertion that cannot fail" shape this file keeps warning about —
and `check_xss.py` flagged three attribute interpolations in `dev.js`. Both
fixed; `Number()`, `parseInt()` and `parseFloat()` are now recognised as safe
by the XSS checker, since none can produce a quote.

---

## Phase 17 — Marketing site

- [x] `frontend/index.html` — full scrollable marketing site
- [x] Hero, the problem, features, how it works, pricing, FAQ, CTA, footer
- [x] Connect sign up and login buttons
- [x] `backend/check_pricing.py` — the page versus `credit_packages`

**No testimonials.** The phase called for them, and inventing quotes from
made-up deputy principals at made-up schools would be fabricating endorsements
for a product with no customers yet — on the page an assessor reads first. The
slot is filled by a "who it's for" section instead, which does the same job
honestly: it names the three people the product is for and what each one gets.
Real quotes can replace it the moment there are any.

**Every number on the page is something the product does.** The first draft of
the hero claimed "1,500 students per run" and "~7 min typical generation";
neither was measured — the only timed run was 397s against the 48-student
sandbox. They were replaced with four figures that are true and checkable: 11
generation stages, 4 AI validators plus the deterministic checker, $0 for a
failed run, 1 free sample run. A number on a pricing page that turns out to be
aspirational is the fastest way to lose a school.

**The pricing page is checked against the database.** `check_pricing.py` reads
`credit_packages` and the PAYG rate and fails if an active package is missing
from the page, or is quoted with the wrong credits or per-credit rate. It caught
one on the first run: Enterprise is an active, purchasable package at $12,800,
but the page implied enterprise pricing was on request — so a school would have
been quoted one thing and charged another. Nothing otherwise keeps a hand-written
page in step with a table the dev portal can edit at runtime.

---

## Live-model findings

The first end-to-end run with real models — no stubs — found three bugs that
every stubbed test had passed over. Worth recording, because all three were
invisible until real models were doing the work.

**Layer 3 did not model room scarcity.** It assigns periods; layer 4 assigns
rooms. With three PE classes and one gym, layer 3 put two of them in the same
period and handed layer 4 an impossible problem: *"Every suitable room is
already busy when 12PED1 meets"*, four attempts running. Retrying cannot help,
because the constraint was never expressed. `_slot_ok()` now counts how many
classes needing each room type are already in a slot and refuses to exceed the
number of rooms that exist. The error message, when it is genuinely
over-subscribed, now names the room type and says what to do about it.

**The AI quorum rejected ten valid timetables.** Detailed in VALIDATION.md. In
short: the prompt asked models to re-check clashes that deterministic code
already owns, which is where they hallucinated; soft judgements like "duty
allocation looks unfair" shared a verdict with hard violations and hard-blocked
generation; and `summarise_day` gave period numbers and duty timings with no
times, so overlap had to be guessed. Ground truth was established with SQL
before changing anything — 0 clashes of any kind — rather than assuming either
side was right.

**OpenRouter 402s when `max_tokens` is unset.** It reserves credit for the
model's whole context window. The pipeline sent no limit, so every call to
Gemini through OpenRouter failed — while `admin.py test-all` reported it
healthy, because the connectivity test does pass one. A green check masking a
broken path. `ai_max_output_tokens` (migration 017) now applies a default.

**A generation now completes on the first attempt: 272s, 54 entries, 31 model
calls, 63,569 tokens**, across magistral, gemini_free, gpt_oss, groq and
mistral_small.

Two things worth watching rather than fixing now: `gpt_oss` averages ~46s a
call, which is most of the wall time; and Groq's free tier rate-limits often
enough that a validator is regularly lost to it, though the quorum minimum of 2
absorbs that.

---

## Phase 18 — Polish and testing

- [x] Error handling throughout (API error responses, frontend error states) —
      all 18 fetching pages catch and surface through `showAlert`
- [x] Loading states on all async operations — all 18 pages
- [x] Empty states — audited page by page rather than guessed at. Five were
      flagged; four were false positives (dashboard, notifications and layout
      always render fixed content, generate is a form). `progress.html` was
      real: you land there the moment a run starts and watched an empty box for
      the first half-minute, which reads as nothing happening.
- [ ] Mobile-responsive CSS checks — needs a browser
- [x] RLS verification — done in the Phase 11 audit, which found 17 tables with
      no row security and closed them. `check_rls_leak.py` now enumerates all 70
      from `pg_class` rather than a hand-written list.
- [ ] End-to-end walkthrough: signup → onboard → import data → generate → view → export
- [ ] VCE demonstration walkthrough with sandbox

---

## Immediate actions before building anything

These take minutes and unblock work later:

- [x] Create Supabase project, copy URL and anon key to .env
- [x] Create Groq account, generate key, add to api_keys table — **working**
- [x] Create 3 Mistral accounts, generate keys, add to api_keys table — keys
      stored but **all 429**: the free tier now requires enabling pay-as-you-go,
      which needs a card
- [x] Create OpenRouter account, key added — `gpt-oss-120b` works;
      `google/gemini-2.5-flash` returns 402 without credits
- [ ] Add payment method to OpenRouter — free, unlocks 20→200 RPM
- [ ] **Google AI Studio key** — free Gemini without a card, replacing the
      OpenRouter Gemini that bills. Provider and `gemini_free` alias are wired
      up (migration 008); only the key is missing.
- [ ] Create Resend account, verify sending domain or email, add API key to .env
- [ ] Create Render account and connect repo for auto-deploy
- [ ] Create Netlify account and connect repo for frontend auto-deploy

**Provider status as measured** (`test_live.py`, `try_free_models.py`):

| Provider | Works | Card needed |
|---|---|---|
| Groq | yes, 1.4s average, rate-limits on ~40% of a burst | no |
| OpenRouter `gpt-oss-120b` | yes, 13/13 calls | no |
| OpenRouter `gemini-2.5-flash` | 402 | yes |
| Mistral (all three accounts) | 429 on every call | yes, now |
| Google AI Studio | untested — no key yet | no |

No free OpenRouter model returns reliable structured JSON: six candidates
tested, all failed with connection errors, malformed JSON, 429s or 403s.

Post-VCE commercial upgrade (no code changes needed):
- Create Anthropic account, add ANTHROPIC_API_KEY to .env
- Update validator_1 → claude, validator_2 → gpt_4o in settings table
- Update task_interpret, task_edit_interpret → claude in settings table

---

## Always pass school_id

Even for features not yet multi-tenant, always include `school_id` on every table insert and every query filter. This makes RLS and future multi-tenancy trivial.

```python
# Good
await db.fetch("SELECT * FROM teachers WHERE school_id = $1", school_id)

# Bad — even in early dev
await db.fetch("SELECT * FROM teachers")
```

# Database schema

Database: Supabase (PostgreSQL). All timestamps stored in UTC. All IDs are UUIDs.

Tables below appear in **strict dependency order** — run them top to bottom and every
foreign key target already exists. Do not reorder.

---

## Schema decisions

**Periods, not blocks.** There is one period model: `layout_periods`, keyed by
`layout_id + day_number + period_number`. The old school-wide `blocks` table has been
removed. Everything that used to reference `block_id` now references
`layout_period_id`. The word "block" still appears in prose and UI labels (Block 3,
block assignment) — it means a period slot on a specific day.

**Teacher names are split.** `teachers.first_name` and `teachers.surname` are stored;
`full_name` is a generated column. Imports that supply only a full name split on the
last space.

**`timetable_entries.day_number` is denormalised** from `layout_periods.day_number` so
transport demand and edit-validation queries can filter without a join. It must be set
from the entry's `layout_period_id` at insert time, in one place
(`services/pipeline.py` write step), and re-checked by `services/deterministic.py`.

---

## 1. Schools and users

```sql
CREATE TABLE schools (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    timezone        TEXT NOT NULL DEFAULT 'Australia/Sydney',
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Extends Supabase auth.users
CREATE TABLE users (
    id              UUID PRIMARY KEY REFERENCES auth.users(id),
    school_id       UUID NOT NULL REFERENCES schools(id),
    first_name      TEXT NOT NULL,
    surname         TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    role            TEXT NOT NULL DEFAULT 'staff',  -- 'owner' | 'staff' | 'dev'
    login_method    TEXT NOT NULL,                  -- 'password' | 'google' | 'microsoft'
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now(),
    deactivated_at  TIMESTAMPTZ
);

CREATE TABLE invites (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    invited_by      UUID NOT NULL REFERENCES users(id),
    email           TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'staff',
    status          TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'accepted' | 'expired'
    token           TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days',
    accepted_at     TIMESTAMPTZ
);
```

---

## 2. Campuses, rooms, teachers

```sql
CREATE TABLE campuses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    name            TEXT NOT NULL,
    address         TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE rooms (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campus_id               UUID NOT NULL REFERENCES campuses(id),
    school_id               UUID NOT NULL REFERENCES schools(id),
    name                    TEXT NOT NULL,
    capacity                INTEGER NOT NULL,
    preferred_min_capacity  INTEGER,
    room_type               TEXT,
    allows_split            BOOLEAN DEFAULT false,
    created_at              TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE teachers (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id               UUID NOT NULL REFERENCES schools(id),
    first_name              TEXT NOT NULL,
    surname                 TEXT NOT NULL,
    full_name               TEXT GENERATED ALWAYS AS (first_name || ' ' || surname) STORED,
    subjects                TEXT[],
    campus_id               UUID REFERENCES campuses(id),
    max_blocks              INTEGER,
    max_duties_per_cycle    INTEGER DEFAULT 10,
    gender                  TEXT,
    created_at              TIMESTAMPTZ DEFAULT now()
);
```

`teacher_availability` is defined in section 6, after `layout_periods` exists.

---

## 3. Students, buses, routes

```sql
CREATE TABLE students (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    full_name       TEXT NOT NULL,
    campus_id       UUID REFERENCES campuses(id),
    year_group      TEXT,
    gender          TEXT,
    ability_band    TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE student_subjects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id  UUID NOT NULL REFERENCES students(id),
    school_id   UUID NOT NULL REFERENCES schools(id),
    subject     TEXT NOT NULL,
    UNIQUE (student_id, subject)
);

CREATE TABLE buses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    name            TEXT NOT NULL,
    capacity        INTEGER NOT NULL,
    home_campus_id  UUID NOT NULL REFERENCES campuses(id),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE routes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    from_campus_id  UUID NOT NULL REFERENCES campuses(id),
    to_campus_id    UUID NOT NULL REFERENCES campuses(id),
    travel_minutes  INTEGER NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

---

## 4. Subject settings

```sql
CREATE TABLE subject_settings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id           UUID NOT NULL REFERENCES schools(id),
    subject             TEXT NOT NULL,
    year_level          TEXT,
    hard_max_size       INTEGER,
    soft_max_size       INTEGER,
    min_size            INTEGER,
    default_room_type   TEXT,
    campus_locked_id    UUID REFERENCES campuses(id),
    is_double_period    BOOLEAN DEFAULT false,
    code_prefix         TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (school_id, subject, year_level)
);

CREATE TABLE class_code_settings (
    school_id           UUID PRIMARY KEY REFERENCES schools(id),
    pattern             TEXT NOT NULL,
    custom_pattern      TEXT,
    subject_code_length INTEGER DEFAULT 3,
    uppercase           BOOLEAN DEFAULT true,
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE balancing_settings (
    school_id               UUID PRIMARY KEY REFERENCES schools(id),
    balance_size            BOOLEAN DEFAULT true,
    balance_gender          BOOLEAN DEFAULT false,
    balance_ability         BOOLEAN DEFAULT false,
    balance_campus          BOOLEAN DEFAULT true,
    size_tolerance_pct      INTEGER DEFAULT 10,
    updated_at              TIMESTAMPTZ DEFAULT now()
);
```

---

## 5. Teacher duty exemptions

Referenced by Layer 6 (duty assignment). A teacher with a row here is never assigned
a duty. Per-teacher duty caps live on `teachers.max_duties_per_cycle`; the school-wide
default is the `duty_default_max_per_cycle` setting.

```sql
CREATE TABLE teacher_duty_exemptions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    teacher_id  UUID NOT NULL REFERENCES teachers(id),
    school_id   UUID NOT NULL REFERENCES schools(id),
    reason      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (teacher_id)
);
```

---

## 6. Timetable layout

`layout_periods` is the single period model. Every allocation, availability and
timetable-entry row references it. It carries `school_id` so the standard RLS policy
applies without a join.

```sql
CREATE TABLE timetable_layouts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    name            TEXT NOT NULL,
    days_in_cycle   INTEGER NOT NULL,
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE layout_periods (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    layout_id       UUID NOT NULL REFERENCES timetable_layouts(id),
    school_id       UUID NOT NULL REFERENCES schools(id),
    day_number      INTEGER NOT NULL,
    period_number   INTEGER NOT NULL,
    period_type     TEXT NOT NULL, -- 'teaching' | 'mentor' | 'break' | 'lunch' | 'assembly' | 'blocked'
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    label           TEXT,
    UNIQUE (layout_id, day_number, period_number)
);

CREATE TABLE teacher_availability (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    teacher_id          UUID NOT NULL REFERENCES teachers(id),
    school_id           UUID NOT NULL REFERENCES schools(id),
    layout_period_id    UUID NOT NULL REFERENCES layout_periods(id),
    available           BOOLEAN NOT NULL DEFAULT true,
    note                TEXT,
    UNIQUE (teacher_id, layout_period_id)
);

CREATE TABLE duty_types (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    name            TEXT NOT NULL,
    campus_id       UUID REFERENCES campuses(id),
    min_staff       INTEGER NOT NULL DEFAULT 1,
    is_bus_duty     BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE layout_duty_slots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    layout_id       UUID NOT NULL REFERENCES timetable_layouts(id),
    school_id       UUID NOT NULL REFERENCES schools(id),
    duty_type_id    UUID NOT NULL REFERENCES duty_types(id),
    day_number      INTEGER NOT NULL,
    timing          TEXT NOT NULL, -- 'before_school' | 'break' | 'lunch' | 'after_school' | 'between_periods'
    between_periods TEXT,
    start_time      TIME NOT NULL,
    end_time        TIME NOT NULL,
    campus_id       UUID NOT NULL REFERENCES campuses(id),
    min_staff       INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

---

## 7. Requirements

```sql
CREATE TABLE requirements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id           UUID NOT NULL REFERENCES schools(id),
    name                TEXT NOT NULL,
    raw_input           TEXT NOT NULL,
    interpreted_input   TEXT,
    confirmed           BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now(),
    confirmed_at        TIMESTAMPTZ
);
```

---

## 8. Timetables and generation attempts

`layout_id` is NOT NULL — a timetable is always generated against a known layout.

```sql
CREATE TABLE timetables (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    name            TEXT NOT NULL,
    layout_id       UUID NOT NULL REFERENCES timetable_layouts(id),
    status          TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'processing' | 'complete' | 'failed'
    created_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE TABLE generation_attempts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timetable_id    UUID NOT NULL REFERENCES timetables(id),
    attempt_number  INTEGER NOT NULL,
    locked_model    TEXT NOT NULL DEFAULT 'magistral',
    status          TEXT NOT NULL DEFAULT 'in_progress', -- 'in_progress' | 'complete' | 'failed'
    failure_reason  TEXT,
    started_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
```

---

## 9. Class group definitions

```sql
CREATE TABLE class_group_definitions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    timetable_id    UUID NOT NULL REFERENCES timetables(id),
    subject         TEXT NOT NULL,
    year_level      TEXT,
    campus_id       UUID REFERENCES campuses(id),
    room_type       TEXT,
    is_double       BOOLEAN DEFAULT false,
    is_accelerated  BOOLEAN DEFAULT false,
    formation_type  TEXT NOT NULL, -- 'explicit' | 'rule' | 'mixed' | 'raw_list'
    inferred_fields JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE class_group_explicit_students (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    class_group_def_id      UUID NOT NULL REFERENCES class_group_definitions(id),
    student_id              UUID NOT NULL REFERENCES students(id),
    UNIQUE (class_group_def_id, student_id)
);

CREATE TABLE class_group_rules (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    class_group_def_id  UUID NOT NULL REFERENCES class_group_definitions(id),
    rule_type           TEXT NOT NULL, -- 'include_year' | 'exclude_subject' | 'exclude_student' | 'include_subject' | 'max_size' | 'min_size'
    rule_value          TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now()
);
```

---

## 10. Timetable versions

```sql
CREATE TABLE timetable_versions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timetable_id            UUID NOT NULL REFERENCES timetables(id),
    school_id               UUID NOT NULL REFERENCES schools(id),
    version_number          INTEGER NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'draft', -- 'draft' | 'published' | 'archived'
    generation_attempt_id   UUID REFERENCES generation_attempts(id),
    published_at            TIMESTAMPTZ,
    published_by            UUID REFERENCES users(id),
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT now(),
    UNIQUE (timetable_id, version_number)
);
```

---

## 11. Pipeline state

```sql
CREATE TABLE pipeline_state (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id      UUID NOT NULL REFERENCES generation_attempts(id),
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL, -- 'pending' | 'in_progress' | 'complete' | 'failed'
    model_used      TEXT,
    input_data      JSONB,
    output_data     JSONB,
    failure_reason  TEXT,
    started_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE TABLE partial_solutions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id              UUID NOT NULL REFERENCES generation_attempts(id),
    period_campus_allocation JSONB,
    room_allocation         JSONB,
    teacher_allocation      JSONB,
    duty_assignment         JSONB,
    bus_duty_assignment     JSONB,
    transport_solution      JSONB,
    updated_at              TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE generation_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id      UUID NOT NULL REFERENCES generation_attempts(id),
    event_type      TEXT NOT NULL, -- 'stage_start' | 'stage_complete' | 'chunk_complete' | 'model_switch' | 'retry' | 'failed' | 'complete'
    message         TEXT NOT NULL,
    detail          JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE generation_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id      UUID NOT NULL REFERENCES generation_attempts(id),
    status          TEXT NOT NULL DEFAULT 'queued', -- 'queued' | 'running' | 'complete' | 'failed'
    current_stage   TEXT,
    current_chunk   TEXT,
    progress_pct    INTEGER DEFAULT 0,
    notify_email    TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
```

---

## 12. Allocation registries

Written by deterministic code only. AI reads them as context and never writes to them.

```sql
CREATE TABLE allocation_registry_students (
    attempt_id          UUID NOT NULL REFERENCES generation_attempts(id),
    student_id          UUID NOT NULL REFERENCES students(id),
    layout_period_id    UUID NOT NULL REFERENCES layout_periods(id),
    class_group_id      UUID,
    PRIMARY KEY (attempt_id, student_id, layout_period_id)
);

CREATE TABLE allocation_registry_teachers (
    attempt_id          UUID NOT NULL REFERENCES generation_attempts(id),
    teacher_id          UUID NOT NULL REFERENCES teachers(id),
    layout_period_id    UUID NOT NULL REFERENCES layout_periods(id),
    PRIMARY KEY (attempt_id, teacher_id, layout_period_id)
);

CREATE TABLE allocation_registry_rooms (
    attempt_id          UUID NOT NULL REFERENCES generation_attempts(id),
    room_id             UUID NOT NULL REFERENCES rooms(id),
    layout_period_id    UUID NOT NULL REFERENCES layout_periods(id),
    PRIMARY KEY (attempt_id, room_id, layout_period_id)
);

-- NOTE: this primary key allows one bus movement per period. The day chain in
-- TRANSPORT.md can put an empty leg and a passenger trip in the same period for the
-- same bus. Confirm before Phase 9 whether this table should carry a surrogate id
-- plus a unique key including from/to campus, or whether the solver guarantees one
-- movement per bus per period.
CREATE TABLE allocation_registry_buses (
    attempt_id          UUID NOT NULL REFERENCES generation_attempts(id),
    bus_id              UUID NOT NULL REFERENCES buses(id),
    layout_period_id    UUID NOT NULL REFERENCES layout_periods(id),
    from_campus_id      UUID NOT NULL REFERENCES campuses(id),
    to_campus_id        UUID NOT NULL REFERENCES campuses(id),
    PRIMARY KEY (attempt_id, bus_id, layout_period_id)
);

CREATE TABLE allocation_registry_duties (
    attempt_id      UUID NOT NULL REFERENCES generation_attempts(id),
    teacher_id      UUID NOT NULL REFERENCES teachers(id),
    duty_slot_id    UUID NOT NULL REFERENCES layout_duty_slots(id),
    PRIMARY KEY (attempt_id, teacher_id, duty_slot_id)
);

CREATE TABLE allocation_registry_duty_counts (
    attempt_id      UUID NOT NULL REFERENCES generation_attempts(id),
    teacher_id      UUID NOT NULL REFERENCES teachers(id),
    duties_assigned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (attempt_id, teacher_id)
);
```

---

## 13. Class groups

```sql
CREATE TABLE class_groups (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id          UUID NOT NULL REFERENCES generation_attempts(id),
    class_group_def_id  UUID REFERENCES class_group_definitions(id),
    school_id           UUID NOT NULL REFERENCES schools(id),
    class_code          TEXT NOT NULL,
    subject             TEXT NOT NULL,
    year_level          TEXT,
    campus_id           UUID REFERENCES campuses(id),
    student_count       INTEGER NOT NULL,
    is_double_period    BOOLEAN DEFAULT false,
    size_warning        BOOLEAN DEFAULT false,
    balance_score       NUMERIC,
    formation_type      TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE class_group_students (
    class_group_id  UUID NOT NULL REFERENCES class_groups(id),
    student_id      UUID NOT NULL REFERENCES students(id),
    entry_type      TEXT NOT NULL, -- 'explicit' | 'rule_assigned' | 'balanced_fill'
    PRIMARY KEY (class_group_id, student_id)
);
```

---

## 14. Consistency registry and pins

```sql
CREATE TABLE class_consistency_registry (
    attempt_id      UUID NOT NULL REFERENCES generation_attempts(id),
    class_group_id  UUID NOT NULL REFERENCES class_groups(id),
    teacher_id      UUID NOT NULL REFERENCES teachers(id),
    room_id         UUID NOT NULL REFERENCES rooms(id),
    locked_at       TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (attempt_id, class_group_id)
);

CREATE TABLE consistency_pins (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id             UUID NOT NULL REFERENCES schools(id),
    timetable_id          UUID NOT NULL REFERENCES timetables(id),
    subject               TEXT NOT NULL,
    class_code            TEXT,
    year_level            TEXT,
    pin_type              TEXT NOT NULL, -- 'teacher' | 'room' | 'both'
    teacher_id            UUID REFERENCES teachers(id),
    room_id               UUID REFERENCES rooms(id),
    strength              TEXT NOT NULL DEFAULT 'soft', -- 'hard' | 'soft'
    source                TEXT NOT NULL, -- 'previous_timetable' | 'manual' | 'system'
    previous_timetable_id UUID REFERENCES timetables(id),
    created_at            TIMESTAMPTZ DEFAULT now()
);
```

---

## 15. Final timetable output

`day_number` is denormalised from `layout_periods` — see Schema decisions above.

```sql
CREATE TABLE timetable_entries (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timetable_id        UUID NOT NULL REFERENCES timetables(id),
    version_id          UUID NOT NULL REFERENCES timetable_versions(id),
    attempt_id          UUID REFERENCES generation_attempts(id),
    layout_period_id    UUID NOT NULL REFERENCES layout_periods(id),
    day_number          INTEGER NOT NULL,
    room_id             UUID NOT NULL REFERENCES rooms(id),
    teacher_id          UUID NOT NULL REFERENCES teachers(id),
    campus_id           UUID NOT NULL REFERENCES campuses(id),
    class_group_id      UUID REFERENCES class_groups(id),
    subject             TEXT NOT NULL,
    class_code          TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_entries_version_day ON timetable_entries (version_id, day_number);
CREATE INDEX idx_entries_attempt     ON timetable_entries (attempt_id);

CREATE TABLE timetable_entry_students (
    timetable_entry_id  UUID NOT NULL REFERENCES timetable_entries(id),
    student_id          UUID NOT NULL REFERENCES students(id),
    PRIMARY KEY (timetable_entry_id, student_id)
);

-- layout_period_id is nullable: some movements (before school, after school) do not
-- sit inside a period. day_number is always set.
CREATE TABLE transport_schedule (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timetable_id        UUID NOT NULL REFERENCES timetables(id),
    version_id          UUID NOT NULL REFERENCES timetable_versions(id),
    bus_id              UUID NOT NULL REFERENCES buses(id),
    from_campus_id      UUID NOT NULL REFERENCES campuses(id),
    to_campus_id        UUID NOT NULL REFERENCES campuses(id),
    departure_time      TIME NOT NULL,
    arrival_time        TIME NOT NULL,
    layout_period_id    UUID REFERENCES layout_periods(id),
    day_number          INTEGER NOT NULL,
    passenger_count     INTEGER NOT NULL DEFAULT 0,
    is_empty_leg        BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE duty_assignments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timetable_id    UUID NOT NULL REFERENCES timetables(id),
    version_id      UUID NOT NULL REFERENCES timetable_versions(id),
    attempt_id      UUID NOT NULL REFERENCES generation_attempts(id),
    duty_slot_id    UUID NOT NULL REFERENCES layout_duty_slots(id),
    teacher_id      UUID NOT NULL REFERENCES teachers(id),
    assignment_type TEXT NOT NULL DEFAULT 'ai_assigned', -- 'ai_assigned' | 'fixed' | 'manual_override'
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (attempt_id, duty_slot_id, teacher_id)
);

CREATE TABLE validation_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    attempt_id      UUID NOT NULL REFERENCES generation_attempts(id),
    validator       TEXT NOT NULL, -- 'gemini' | 'gpt_oss' | 'groq' | 'mistral_small' | 'deterministic'
    result          TEXT NOT NULL, -- 'pass' | 'fail' | 'rate_limited' | 'error'
    detail          TEXT,
    responded_at    TIMESTAMPTZ DEFAULT now()
);
```

---

## 16. AI-assisted editing

```sql
CREATE TABLE timetable_edits (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id          UUID NOT NULL REFERENCES timetable_versions(id),
    school_id           UUID NOT NULL REFERENCES schools(id),
    requested_by        UUID NOT NULL REFERENCES users(id),
    request_text        TEXT NOT NULL,
    ai_interpretation   TEXT NOT NULL,
    proposed_changes    JSONB NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'approved' | 'rejected' | 'applied'
    validator_result    TEXT,
    validator_detail    TEXT,
    credits_charged     INTEGER DEFAULT 5,
    applied_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now()
);
```

---

## 17. Billing

```sql
CREATE TABLE credit_packages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL, -- 'trial' | 'standard' | 'annual' | 'ultra' | 'enterprise'
    display_name        TEXT NOT NULL,
    credits             INTEGER,
    price_per_credit    NUMERIC NOT NULL,
    total_price_cents   INTEGER,
    is_payg             BOOLEAN NOT NULL DEFAULT false,
    once_per_school     BOOLEAN NOT NULL DEFAULT false,
    credits_expire_days INTEGER,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    display_order       INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT now()
);

INSERT INTO credit_packages
    (name, display_name, credits, price_per_credit, total_price_cents, once_per_school, credits_expire_days, display_order)
VALUES
('trial',      'Trial',       300,  1.60, 48000,   true,  365, 1),
('standard',   'Standard',    800,  2.00, 160000,  false, null, 2),
('annual',     'Annual',      1500, 1.90, 285000,  false, null, 3),
('ultra',      'Ultra',       3000, 1.80, 540000,  false, null, 4),
('enterprise', 'Enterprise',  8000, 1.60, 1280000, false, null, 5);

CREATE TABLE school_credits (
    school_id           UUID PRIMARY KEY REFERENCES schools(id),
    balance             INTEGER NOT NULL DEFAULT 0,
    reserved            INTEGER NOT NULL DEFAULT 0,
    lifetime_purchased  INTEGER NOT NULL DEFAULT 0,
    lifetime_spent      INTEGER NOT NULL DEFAULT 0,
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE school_billing (
    school_id                   UUID PRIMARY KEY REFERENCES schools(id),
    billing_mode                TEXT NOT NULL DEFAULT 'credits', -- 'credits' | 'payg' | 'invoiced'
    trial_used                  BOOLEAN NOT NULL DEFAULT false,
    trial_purchased_at          TIMESTAMPTZ,
    -- PAYG (fake terminal in VCE version)
    payg_enabled                BOOLEAN NOT NULL DEFAULT false,
    payg_card_last_four         TEXT,
    payg_card_brand             TEXT,
    payg_card_expires           TEXT,
    payg_fake_method_id         TEXT,   -- fake Stripe-format ID from terminal
    payg_last_charged_at        TIMESTAMPTZ,
    -- Invoiced billing
    invoiced_approved           BOOLEAN NOT NULL DEFAULT false,
    invoiced_approved_at        TIMESTAMPTZ,
    invoiced_approved_by        TEXT,
    invoiced_billing_email      TEXT,
    invoiced_billing_contact    TEXT,
    invoiced_payment_days       INTEGER DEFAULT 30,
    invoiced_applied_at         TIMESTAMPTZ,
    invoiced_revoked            BOOLEAN DEFAULT false,
    invoiced_revoked_at         TIMESTAMPTZ,
    invoiced_revoked_reason     TEXT,
    -- Account standing
    account_blocked             BOOLEAN NOT NULL DEFAULT false,
    blocked_at                  TIMESTAMPTZ,
    blocked_reason              TEXT,
    outstanding_balance_cents   INTEGER DEFAULT 0,
    interest_started_at         TIMESTAMPTZ,
    -- Notifications
    low_balance_threshold       INTEGER DEFAULT 300,
    notify_low_balance          BOOLEAN DEFAULT true,
    notify_on_charge            BOOLEAN DEFAULT true,
    updated_at                  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE credit_transactions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id           UUID NOT NULL REFERENCES schools(id),
    type                TEXT NOT NULL,
    amount              INTEGER NOT NULL,
    balance_after       INTEGER NOT NULL,
    package_id          UUID REFERENCES credit_packages(id),
    timetable_id        UUID REFERENCES timetables(id),
    attempt_id          UUID REFERENCES generation_attempts(id),
    price_paid_cents    INTEGER,
    payment_method      TEXT, -- 'fake_card' | 'manual' | 'system'
    fake_payment_id     TEXT,
    note                TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE generation_cost_breakdown (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timetable_id        UUID NOT NULL REFERENCES timetables(id),
    school_id           UUID NOT NULL REFERENCES schools(id),
    estimated_credits   INTEGER NOT NULL,
    actual_credits      INTEGER,
    base_fee            INTEGER NOT NULL,
    student_cost        NUMERIC NOT NULL,
    teacher_cost        NUMERIC NOT NULL DEFAULT 0,
    campus_cost         INTEGER NOT NULL DEFAULT 0,
    transport_cost      INTEGER NOT NULL DEFAULT 0,
    bus_route_cost      INTEGER NOT NULL DEFAULT 0,
    duties_cost         INTEGER NOT NULL DEFAULT 0,
    day_cost            INTEGER NOT NULL DEFAULT 0,
    accelerated_cost    INTEGER NOT NULL DEFAULT 0,
    double_period_cost  INTEGER NOT NULL DEFAULT 0,
    school_retry_cost   INTEGER DEFAULT 0,
    dev_retry_cost      INTEGER DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'reserved', -- 'reserved' | 'charged' | 'paused' | 'refunded'
    paused_at           TIMESTAMPTZ,
    paused_stage        TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    settled_at          TIMESTAMPTZ
);

CREATE TABLE school_complexity (
    school_id               UUID PRIMARY KEY REFERENCES schools(id),
    student_count           INTEGER NOT NULL DEFAULT 0,
    teacher_count           INTEGER NOT NULL DEFAULT 0,
    campus_count            INTEGER NOT NULL DEFAULT 0,
    transport_enabled       BOOLEAN NOT NULL DEFAULT false,
    bus_route_count         INTEGER NOT NULL DEFAULT 0,
    duties_enabled          BOOLEAN NOT NULL DEFAULT false,
    cycle_days              INTEGER NOT NULL DEFAULT 5,
    accelerated_enabled     BOOLEAN NOT NULL DEFAULT false,
    double_periods_enabled  BOOLEAN NOT NULL DEFAULT false,
    estimated_cost_credits  INTEGER NOT NULL DEFAULT 0,
    last_calculated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE invoices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id           UUID NOT NULL REFERENCES schools(id),
    invoice_number      TEXT NOT NULL UNIQUE,
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    subtotal_credits    INTEGER NOT NULL DEFAULT 0,
    subtotal_cents      INTEGER NOT NULL DEFAULT 0,
    tax_cents           INTEGER NOT NULL DEFAULT 0,
    total_cents         INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'draft', -- 'draft' | 'sent' | 'paid' | 'overdue'
    due_date            DATE NOT NULL,
    days_overdue        INTEGER DEFAULT 0,
    sent_at             TIMESTAMPTZ,
    paid_at             TIMESTAMPTZ,
    overdue_at          TIMESTAMPTZ,
    blocked_at          TIMESTAMPTZ,
    invoiced_revoked_at TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE outstanding_charges (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id               UUID NOT NULL REFERENCES schools(id),
    charge_type             TEXT NOT NULL, -- 'payg_failed' | 'invoice_overdue'
    original_amount_cents   INTEGER NOT NULL,
    interest_rate_daily     NUMERIC NOT NULL DEFAULT 0.10,
    interest_accrued_cents  INTEGER NOT NULL DEFAULT 0,
    total_owed_cents        INTEGER NOT NULL,
    days_outstanding        INTEGER NOT NULL DEFAULT 0,
    timetable_id            UUID REFERENCES timetables(id),
    invoice_id              UUID REFERENCES invoices(id),
    status                  TEXT NOT NULL DEFAULT 'outstanding', -- 'outstanding' | 'paid' | 'written_off'
    created_at              TIMESTAMPTZ DEFAULT now(),
    resolved_at             TIMESTAMPTZ,
    resolved_by             TEXT
);

CREATE TABLE invoice_line_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID NOT NULL REFERENCES invoices(id),
    timetable_id    UUID NOT NULL REFERENCES timetables(id),
    description     TEXT NOT NULL,
    credits_used    INTEGER NOT NULL,
    amount_cents    INTEGER NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE payg_charge_log (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id               UUID NOT NULL REFERENCES schools(id),
    timetable_id            UUID NOT NULL REFERENCES timetables(id),
    actual_credits          INTEGER NOT NULL,
    amount_charged_cents    INTEGER NOT NULL,
    fake_payment_id         TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'success' | 'failed' | 'simulated_fail'
    failure_reason          TEXT,
    school_blocked          BOOLEAN DEFAULT false,
    outstanding_charge_id   UUID REFERENCES outstanding_charges(id),
    retry_count             INTEGER DEFAULT 0,
    last_retry_at           TIMESTAMPTZ,
    resolved_at             TIMESTAMPTZ,
    resolved_by             TEXT,
    charged_at              TIMESTAMPTZ,
    created_at              TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE invoiced_billing_applications (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id               UUID NOT NULL REFERENCES schools(id),
    applied_by              UUID NOT NULL REFERENCES users(id),
    billing_contact_name    TEXT NOT NULL,
    billing_contact_email   TEXT NOT NULL,
    billing_contact_phone   TEXT,
    organisation_abn        TEXT,
    reason                  TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'approved' | 'rejected'
    reviewed_at             TIMESTAMPTZ,
    reviewed_by             TEXT,
    rejection_reason        TEXT,
    created_at              TIMESTAMPTZ DEFAULT now()
);
```

`invoices` is created before `outstanding_charges` so that `outstanding_charges.invoice_id`
can be a real foreign key rather than a loose UUID.

---

## 18. Import and export

```sql
CREATE TABLE import_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id           UUID NOT NULL REFERENCES schools(id),
    imported_by         UUID NOT NULL REFERENCES users(id),
    import_type         TEXT NOT NULL, -- 'teachers' | 'students' | 'rooms' | 'subjects' | 'mixed'
    original_filename   TEXT NOT NULL,
    original_format     TEXT NOT NULL, -- 'csv' | 'xlsx' | 'pdf' | 'image'
    ai_mapping          JSONB,
    confirmed_mapping   JSONB,
    rows_total          INTEGER,
    rows_imported       INTEGER DEFAULT 0,
    rows_skipped        INTEGER DEFAULT 0,
    rows_failed         INTEGER DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'mapping' | 'confirmed' | 'importing' | 'complete' | 'failed'
    warnings            JSONB,
    created_at          TIMESTAMPTZ DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE TABLE csv_output_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    name            TEXT NOT NULL,
    description     TEXT,
    columns         JSONB NOT NULL,
    delimiter       TEXT DEFAULT ',',
    include_header  BOOLEAN DEFAULT true,
    output_type     TEXT NOT NULL DEFAULT 'timetable_entries', -- 'timetable_entries' | 'transport' | 'duties' | 'students'
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
```

---

## 19. Onboarding and notifications

```sql
CREATE TABLE onboarding (
    school_id               UUID PRIMARY KEY REFERENCES schools(id),
    sandbox_used            BOOLEAN DEFAULT false,
    sandbox_used_at         TIMESTAMPTZ,
    step_school_details     BOOLEAN DEFAULT false,
    step_campuses           BOOLEAN DEFAULT false,
    step_billing            BOOLEAN DEFAULT false,
    step_layout             BOOLEAN DEFAULT false,
    step_teachers           BOOLEAN DEFAULT false,
    step_students           BOOLEAN DEFAULT false,
    step_rooms              BOOLEAN DEFAULT false,
    step_subjects           BOOLEAN DEFAULT false,
    step_transport          BOOLEAN DEFAULT false,
    completed               BOOLEAN DEFAULT false,
    completed_at            TIMESTAMPTZ,
    updated_at              TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE notification_preferences (
    user_id                     UUID PRIMARY KEY REFERENCES users(id),
    notify_generation_complete  BOOLEAN DEFAULT true,
    notify_generation_failed    BOOLEAN DEFAULT true,
    notify_generation_paused    BOOLEAN DEFAULT true,
    notify_low_balance          BOOLEAN DEFAULT true,
    notify_purchase_receipt     BOOLEAN DEFAULT true,
    notify_payg_charged         BOOLEAN DEFAULT true,
    notify_payg_failed          BOOLEAN DEFAULT true,
    notify_invoice_sent         BOOLEAN DEFAULT true,
    notify_invoice_overdue      BOOLEAN DEFAULT true,
    notify_account_blocked      BOOLEAN DEFAULT true,
    notify_version_published    BOOLEAN DEFAULT true,
    notify_edit_applied         BOOLEAN DEFAULT true,
    notify_user_invited         BOOLEAN DEFAULT true,
    notify_user_joined          BOOLEAN DEFAULT true,
    updated_at                  TIMESTAMPTZ DEFAULT now()
);
```

---

## 20. Support

```sql
CREATE TABLE support_tickets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID REFERENCES schools(id),
    user_id         UUID REFERENCES users(id),
    category        TEXT NOT NULL, -- 'billing' | 'generation' | 'data' | 'account' | 'other'
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    timetable_id    UUID REFERENCES timetables(id),
    attempt_id      UUID REFERENCES generation_attempts(id),
    status          TEXT NOT NULL DEFAULT 'open', -- 'open' | 'in_progress' | 'resolved' | 'closed'
    priority        TEXT NOT NULL DEFAULT 'normal', -- 'low' | 'normal' | 'high' | 'urgent'
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);
```

---

## 21. System tables

```sql
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    category    TEXT NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE api_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider        TEXT NOT NULL, -- 'mistral' | 'openrouter' | 'groq'
    key_label       TEXT NOT NULL, -- 'Key 1', 'Key 2' etc
    encrypted_key   TEXT NOT NULL,
    account_label   TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE error_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID REFERENCES schools(id),
    timetable_id    UUID REFERENCES timetables(id),
    attempt_id      UUID REFERENCES generation_attempts(id),
    error_type      TEXT NOT NULL,
    provider        TEXT,
    api_key_id      UUID REFERENCES api_keys(id),
    stage           TEXT,
    is_dev_fault    BOOLEAN NOT NULL,
    error_message   TEXT,
    resolved        BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE pipeline_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timetable_id    UUID NOT NULL REFERENCES timetables(id),
    attempt_id      UUID REFERENCES generation_attempts(id),
    stage           TEXT NOT NULL,
    model_used      TEXT,
    api_key_id      UUID REFERENCES api_keys(id),
    status          TEXT NOT NULL,
    detail          TEXT,
    tokens_used     INTEGER,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

`api_keys` is created before `error_log` and `pipeline_log` so their `api_key_id`
foreign keys resolve.

Added by migration 010, alongside the Phase 10 billing jobs:

```sql
CREATE TABLE scheduled_job_runs (
    job_name        TEXT NOT NULL,
    run_date        DATE NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',
                    -- 'running' | 'complete' | 'failed'
    triggered_by    TEXT NOT NULL DEFAULT 'scheduler',
    summary         JSONB,
    error           TEXT,
    PRIMARY KEY (job_name, run_date)
);
```

The primary key is the point of the table, not an afterthought. A job claims its
day with `INSERT ... ON CONFLICT DO NOTHING`; exactly one caller inserts and does
the work, and the rest stop. That is what makes daily interest and monthly
invoicing safe to run from several workers, to trigger by hand, and to retry
after a crash. It doubles as the run history.

Migration 010 also adds `outstanding_charges.last_interest_date` (so accrual
charges for the days that actually elapsed rather than one per run) and three
unique indexes that make repetition harmless rather than merely unlikely:
one invoice per `(school_id, period_start)`, one PAYG charge per `timetable_id`,
and partial indexes on the rows the daily jobs scan.

---

## Row Level Security policies

**Every public table has RLS enabled.** Migration 002 covers the tables it lists
by name; migration 011 covers the seventeen it did not, which were the ones with
no `school_id` of their own — pipeline internals, the class-group link tables,
and invoice lines. Until 011 those were readable by anyone holding the
publishable anon key, proven directly:

```
anon GET /rest/v1/generation_events -> HTTP 200, 1 row
```

They had looked safe only because the test suites clean up after themselves, so
the tables sit empty between runs; a school running a real generation fills them
with the mapping of which student is in which class at which time.

**A new table needs a policy, not just a create statement.** The rule for any
table added from here: if it has `school_id`, add it to the array in 002's first
`DO` block. If it does not, scope it through its parent the way 011 does.
`check_rls_leak.py` enumerates tables from `pg_class` rather than a list, so it
will fail on a table that has neither.

Enable RLS on every school-data table. Core pattern:

```sql
ALTER TABLE campuses ENABLE ROW LEVEL SECURITY;
CREATE POLICY school_isolation ON campuses
    USING (school_id = (SELECT school_id FROM users WHERE id = auth.uid()));

-- Dev role bypasses RLS
CREATE POLICY dev_access ON campuses
    USING ((SELECT role FROM users WHERE id = auth.uid()) = 'dev');
```

Apply this pattern to: campuses, rooms, teachers, teacher_availability,
teacher_duty_exemptions, students, student_subjects, buses, routes, subject_settings,
class_code_settings, balancing_settings, timetable_layouts, layout_periods, duty_types,
layout_duty_slots, requirements, class_group_definitions, timetables,
timetable_versions, generation_attempts, timetable_entries, timetable_entry_students,
transport_schedule, duty_assignments, school_credits, school_billing,
credit_transactions, import_jobs, csv_output_templates, support_tickets.

`layout_periods` and `layout_duty_slots` carry `school_id` directly so the same policy
applies without joining through `timetable_layouts`.

-- =============================================================================
-- KLASSER AI - 003 SETTINGS AND PRICING SEED
-- Everything here is editable from the dev portal at runtime. Re-runnable:
-- existing rows are left alone so a re-migration never overwrites live values.
-- =============================================================================

-- --- Credit packages ---------------------------------------------------------

INSERT INTO credit_packages
    (name, display_name, credits, price_per_credit, total_price_cents,
     once_per_school, credits_expire_days, display_order)
SELECT * FROM (VALUES
    ('trial',      'Trial',       300,  1.60, 48000,   true,  365,      1),
    ('standard',   'Standard',    800,  2.00, 160000,  false, NULL::INT, 2),
    ('annual',     'Annual',      1500, 1.90, 285000,  false, NULL::INT, 3),
    ('ultra',      'Ultra',       3000, 1.80, 540000,  false, NULL::INT, 4),
    ('enterprise', 'Enterprise',  8000, 1.60, 1280000, false, NULL::INT, 5)
) AS v(name, display_name, credits, price_per_credit, total_price_cents,
       once_per_school, credits_expire_days, display_order)
WHERE NOT EXISTS (SELECT 1 FROM credit_packages cp WHERE cp.name = v.name);


-- --- AI model strings --------------------------------------------------------
-- Defined exactly once. (The design docs had this block twice, which would fail
-- on the settings primary key.)

INSERT INTO settings (key, value, category, description) VALUES
('model_magistral',     'magistral-medium-latest', 'model', 'Mistral Magistral model string'),
('model_gemini',        'google/gemini-2.5-flash', 'model', 'Gemini 2.5 Flash via OpenRouter'),
('model_gpt_oss',       'openai/gpt-oss-120b',     'model', 'GPT-OSS via OpenRouter'),
('model_groq',          'llama-3.3-70b-versatile', 'model', 'Groq model string'),
('model_mistral_small', 'mistral-small-latest',    'model', 'Mistral Small model string')
ON CONFLICT (key) DO NOTHING;


-- --- AI task assignments -----------------------------------------------------
-- VCE build: free-tier providers only. Commercial upgrade path in ARCHITECTURE.md.

INSERT INTO settings (key, value, category, description) VALUES
('task_interpret',             'gemini',    'ai_assignment', 'Parses user requirements'),
('task_divide',                'groq',      'ai_assignment', 'Routes tasks between AI agents'),
('task_class_group_formation', 'magistral', 'ai_assignment', 'Forms and balances class groups'),
('task_block_campus_primary',  'magistral', 'ai_assignment', 'Period and campus allocation primary model'),
('task_block_campus_fallback', 'gemini',    'ai_assignment', 'Period and campus allocation fallback if Magistral rate limits'),
('task_room_allocation',       'groq',      'ai_assignment', 'Room allocation - fast and sufficient'),
('task_teacher_primary',       'magistral', 'ai_assignment', 'Teacher allocation primary model'),
('task_teacher_fallback',      'gemini',    'ai_assignment', 'Teacher allocation fallback if Magistral rate limits'),
('task_duty_assignment',       'magistral', 'ai_assignment', 'Duty assignment model'),
('task_transport_format',      'groq',      'ai_assignment', 'Transport schedule formatter'),
('task_import_interpret',      'gemini',    'ai_assignment', 'AI data import interpreter'),
('task_edit_interpret',        'gemini',    'ai_assignment', 'Timetable edit interpreter'),
('task_export_map',            'gemini',    'ai_assignment', 'CSV output template mapper')
ON CONFLICT (key) DO NOTHING;


-- --- Validators --------------------------------------------------------------

INSERT INTO settings (key, value, category, description) VALUES
('validator_1', 'gemini',        'validator', 'Main validator 1 - Gemini 2.5 Flash'),
('validator_2', 'gpt_oss',       'validator', 'Main validator 2 - GPT-OSS via OpenRouter'),
('validator_3', 'groq',          'validator', 'Backup validator'),
('validator_4', 'mistral_small', 'validator', 'Last resort validator')
ON CONFLICT (key) DO NOTHING;

INSERT INTO settings (key, value, category, description) VALUES
('quorum_minimum',               '2',    'validator', 'Minimum AI validators required to pass'),
('quorum_require_deterministic', 'true', 'validator', 'Deterministic validator must also pass'),
('quorum_any_fail_blocks',       'true', 'validator', 'Any single fail blocks regardless of quorum')
ON CONFLICT (key) DO NOTHING;


-- --- Retry -------------------------------------------------------------------

INSERT INTO settings (key, value, category, description) VALUES
('max_retries',          '3', 'retry', 'Max retries per AI call before fallback'),
('retry_delay_seconds',  '5', 'retry', 'Seconds between retries'),
('allocation_max_loops', '5', 'retry', 'Max allocation loops before surfacing error to user')
ON CONFLICT (key) DO NOTHING;


-- --- Credit pricing ----------------------------------------------------------

INSERT INTO settings (key, value, category, description) VALUES
('credit_base_fee',            '60',   'pricing', 'Flat credits per generation'),
('credit_per_student',         '0.20', 'pricing', 'Credits per student'),
('credit_teacher_threshold',   '20',   'pricing', 'Free teacher threshold'),
('credit_per_teacher_over',    '0.40', 'pricing', 'Credits per teacher over threshold'),
('credit_per_extra_campus',    '30',   'pricing', 'Credits per campus beyond first'),
('credit_transport_flat',      '40',   'pricing', 'Flat fee if transport enabled'),
('credit_per_bus_route',       '10',   'pricing', 'Credits per bus route'),
('credit_duties_flat',         '20',   'pricing', 'Flat fee if duties enabled'),
('credit_day_threshold',       '5',    'pricing', 'Free cycle day threshold'),
('credit_per_extra_day',       '4',    'pricing', 'Credits per cycle day beyond threshold'),
('credit_accelerated_flat',    '20',   'pricing', 'Flat fee if accelerated subjects'),
('credit_double_period_flat',  '10',   'pricing', 'Flat fee if double periods'),
('credit_school_retry',        '16',   'pricing', 'Credits per school-caused retry loop'),
('credit_edit_session',        '10',   'pricing', 'Credits per AI-assisted edit session'),
('credit_payg_rate_cents',     '230',  'pricing', 'PAYG rate in cents per credit ($2.30)'),
('credit_invoiced_rate_cents', '230',  'pricing', 'Invoiced billing rate in cents per credit')
ON CONFLICT (key) DO NOTHING;


-- --- Duties ------------------------------------------------------------------

INSERT INTO settings (key, value, category, description) VALUES
('duty_default_max_per_cycle', '10', 'duties', 'Default max duties per teacher per cycle if not set on teacher row')
ON CONFLICT (key) DO NOTHING;


-- --- Billing enforcement -----------------------------------------------------

INSERT INTO settings (key, value, category, description) VALUES
('invoice_sent_day',      '1',    'billing', 'Day of month invoices are sent'),
('invoice_due_day',       '14',   'billing', 'Day of month invoices are due'),
('invoice_block_days',    '1',    'billing', 'Days overdue before generation blocked'),
('invoice_revoke_days',   '10',   'billing', 'Days after block before invoiced billing revoked'),
('invoice_interest_rate', '0.10', 'billing', 'Daily interest rate for overdue invoices'),
('payg_interest_rate',    '0.20', 'billing', 'Daily interest rate for failed PAYG charges')
ON CONFLICT (key) DO NOTHING;


-- --- Dev / sandbox -----------------------------------------------------------

INSERT INTO settings (key, value, category, description) VALUES
('fake_terminal_success_rate', '1.0', 'dev',     'Fake card terminal success rate (1.0 = always success, 0.0 = always fail)'),
('sandbox_school_id',          '',    'sandbox', 'School ID of the pre-loaded sandbox school - set by 004_seed_sandbox.sql')
ON CONFLICT (key) DO NOTHING;

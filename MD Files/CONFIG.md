# Configuration

All settings below are stored in the `settings` table and managed via the dev portal.
No redeployment needed to change any of these values.

---

## config.py

App-level constants that never change at runtime. Model strings and task assignments come from the database.

```python
# backend/config.py

# App
APP_NAME = "Klasser AI"
APP_URL  = "https://klasser.ai"
FROM_EMAIL = "Klasser <notifications@klasser.ai>"
SUPPORT_EMAIL = "support@klasser.ai"

# Provider base URLs
PROVIDER_URLS = {
    'mistral':    'https://api.mistral.ai/v1',
    'openrouter': 'https://openrouter.ai/api/v1',
    'groq':       'https://api.groq.com/openai/v1',
}

# Rate limit status codes that trigger fallback
RATE_LIMIT_CODES = [429, 503]

# Fake terminal config
FAKE_TERMINAL_BRANDS = ['Visa', 'Mastercard', 'Amex']
FAKE_TERMINAL_SUCCESS_RATE = 1.0  # 1.0 = always succeeds, 0.8 = 80% success
# Dev portal can override FAKE_TERMINAL_SUCCESS_RATE to simulate failures
```

---

## Settings table — initial values

Run this SQL after creating the settings table.

```sql
-- AI model strings
INSERT INTO settings (key, value, category, description) VALUES
('model_magistral',       'magistral-medium-latest',      'model', 'Mistral Magistral model string'),
('model_gemini',          'google/gemini-2.5-flash',       'model', 'Gemini model string via OpenRouter'),
('model_gpt_oss',         'openai/gpt-oss-120b',           'model', 'GPT-OSS model string via OpenRouter'),
('model_groq',            'llama-3.3-70b-versatile',       'model', 'Groq model string'),
('model_mistral_small',   'mistral-small-latest',          'model', 'Mistral Small model string');

-- AI task assignments
-- VCE build: all free-tier providers only (Gemini via OpenRouter, Groq, Mistral)
-- Commercial upgrade path documented in ARCHITECTURE.md
INSERT INTO settings (key, value, category, description) VALUES
('task_interpret',               'gemini',    'ai_assignment', 'Parses user requirements — Gemini 2.5 Flash'),
('task_divide',                  'groq',      'ai_assignment', 'Routes tasks between AI agents'),
('task_class_group_formation',   'magistral', 'ai_assignment', 'Forms and balances class groups'),
('task_block_campus_primary',    'magistral', 'ai_assignment', 'Block and campus allocation primary model'),
('task_block_campus_fallback',   'gemini',    'ai_assignment', 'Block and campus allocation fallback if Magistral rate limits'),
('task_room_allocation',         'groq',      'ai_assignment', 'Room allocation — fast and sufficient'),
('task_teacher_primary',         'magistral', 'ai_assignment', 'Teacher allocation primary model'),
('task_teacher_fallback',        'gemini',    'ai_assignment', 'Teacher allocation fallback if Magistral rate limits'),
('task_duty_assignment',         'magistral', 'ai_assignment', 'Duty assignment model'),
('task_transport_format',        'groq',      'ai_assignment', 'Transport schedule formatter'),
('task_import_interpret',        'gemini',    'ai_assignment', 'AI data import interpreter'),
('task_edit_interpret',          'gemini',    'ai_assignment', 'Timetable edit interpreter'),
('task_export_map',              'gemini',    'ai_assignment', 'CSV output template mapper');

-- Validator assignments
-- VCE build: free-tier validators only
-- Commercial upgrade: swap validator_1 to claude, validator_2 to gpt_4o
INSERT INTO settings (key, value, category, description) VALUES
('validator_1',   'gemini',       'validator', 'Main validator 1 — Gemini 2.5 Flash'),
('validator_2',   'gpt_oss',      'validator', 'Main validator 2 — GPT-OSS via OpenRouter'),
('validator_3',   'groq',         'validator', 'Backup validator'),
('validator_4',   'mistral_small','validator', 'Last resort validator');

-- Quorum settings
INSERT INTO settings (key, value, category, description) VALUES
('quorum_minimum',              '2',    'validator', 'Minimum AI validators required to pass'),
('quorum_require_deterministic','true', 'validator', 'Deterministic validator must also pass'),
('quorum_any_fail_blocks',      'true', 'validator', 'Any single fail blocks regardless of quorum');

-- Retry settings
INSERT INTO settings (key, value, category, description) VALUES
('max_retries',          '3', 'retry', 'Max retries per AI call before fallback'),
('retry_delay_seconds',  '5', 'retry', 'Seconds between retries'),
('allocation_max_loops', '5', 'retry', 'Max allocation loops before surfacing error to user');

-- Credit pricing
-- NOTE: All values doubled from initial design after pricing review.
-- Schools save ~$33,600/year in staff time. At these prices Klasser
-- delivers 10-15x ROI and is still 50-75% cheaper than Edval.
INSERT INTO settings (key, value, category, description) VALUES
('credit_base_fee',             '60',   'pricing', 'Flat credits per generation'),
('credit_per_student',          '0.20', 'pricing', 'Credits per student'),
('credit_teacher_threshold',    '20',   'pricing', 'Free teacher threshold'),
('credit_per_teacher_over',     '0.40', 'pricing', 'Credits per teacher over threshold'),
('credit_per_extra_campus',     '30',   'pricing', 'Credits per campus beyond first'),
('credit_transport_flat',       '40',   'pricing', 'Flat fee if transport enabled'),
('credit_per_bus_route',        '10',   'pricing', 'Credits per bus route'),
('credit_duties_flat',          '20',   'pricing', 'Flat fee if duties enabled'),
('credit_day_threshold',        '5',    'pricing', 'Free cycle day threshold'),
('credit_per_extra_day',        '4',    'pricing', 'Credits per cycle day beyond threshold'),
('credit_accelerated_flat',     '20',   'pricing', 'Flat fee if accelerated subjects'),
('credit_double_period_flat',   '10',   'pricing', 'Flat fee if double periods'),
('credit_school_retry',         '16',   'pricing', 'Credits per school-caused retry loop'),
('credit_edit_session',         '10',   'pricing', 'Credits per AI-assisted edit session'),
('credit_payg_rate_cents',      '230',  'pricing', 'PAYG rate in cents per credit ($2.30)'),
('credit_invoiced_rate_cents',  '230',  'pricing', 'Invoiced billing rate in cents per credit');

-- Duties
INSERT INTO settings (key, value, category, description) VALUES
('duty_default_max_per_cycle', '10', 'duties', 'Default max duties per teacher per cycle if not set on teacher row');

-- Billing enforcement
INSERT INTO settings (key, value, category, description) VALUES
('invoice_sent_day',          '1',    'billing', 'Day of month invoices are sent'),
('invoice_due_day',           '14',   'billing', 'Day of month invoices are due'),
('invoice_block_days',        '1',    'billing', 'Days overdue before generation blocked'),
('invoice_revoke_days',       '10',   'billing', 'Days after block before invoiced billing revoked'),
('invoice_interest_rate',     '0.10', 'billing', 'Daily interest rate for overdue invoices'),
('payg_interest_rate',        '0.20', 'billing', 'Daily interest rate for failed PAYG charges');

-- Fake terminal
INSERT INTO settings (key, value, category, description) VALUES
('fake_terminal_success_rate', '1.0', 'dev', 'Fake card terminal success rate (1.0 = always success, 0.0 = always fail)');

-- Sandbox
INSERT INTO settings (key, value, category, description) VALUES
('sandbox_school_id', '', 'sandbox', 'School ID of the pre-loaded sandbox school');
```

---

## Loading settings at runtime

```python
# services/settings.py
_cache = {}

async def get_setting(key: str) -> str:
    if key not in _cache:
        row = await db.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        if row:
            _cache[key] = row['value']
    return _cache.get(key)

async def get_settings(keys: list) -> dict:
    return {k: await get_setting(k) for k in keys}

def invalidate_cache():
    _cache.clear()

# Call invalidate_cache() whenever dev portal updates a setting
```

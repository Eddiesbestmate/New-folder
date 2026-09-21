# Keys and environment variables

## keys.py

Only secrets that must exist before the database is available live here.
Everything else (AI API keys, model assignments, settings) is stored in the database and managed via the dev portal.

```python
# backend/keys.py
# =============================================================================
# KLASSER AI — ENVIRONMENT SECRETS
# Only Supabase connection details, encryption key, and admin credentials
# live here. All AI API keys and app settings are stored in the database.
# =============================================================================

from dotenv import load_dotenv
import os

load_dotenv()

# --- Supabase (must stay here — needed before DB is available) ---
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY   = os.getenv("SUPABASE_ANON_KEY")
DATABASE_URL        = os.getenv("DATABASE_URL")

# --- Supabase service role key ---
# Required for admin operations: invite_user_by_email, delete_user / session
# revocation on deactivation. BYPASSES ROW LEVEL SECURITY.
# Server-side only. Never send to the frontend. Never log it.
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# --- Encryption key for AI API keys stored in database ---
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY      = os.getenv("ENCRYPTION_KEY")

# --- Email ---
RESEND_API_KEY      = os.getenv("RESEND_API_KEY")

# --- Anthropic (Claude Sonnet) ---
# NOT used in VCE build — all AI via free-tier providers (Mistral, OpenRouter, Groq)
# Add post-VCE for commercial upgrade: interpretation, editing, validation 1
# ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# --- Dev portal ---
# Dev portal uses Supabase Auth (role = 'dev') — no separate password needed
# Super dashboard section of dev portal also uses role-based access
```

---

## .env

```bash
# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your_anon_key_here
# Service role key — bypasses RLS. Server-side only, never expose to frontend.
SUPABASE_SERVICE_KEY=your_service_role_key_here
DATABASE_URL=postgresql+asyncpg://postgres:password@db.your-project.supabase.co:5432/postgres

# Encryption
ENCRYPTION_KEY=your_fernet_key_here

# Email
RESEND_API_KEY=re_your_key_here

# Anthropic — not used in VCE build, add post-VCE for commercial upgrade
# ANTHROPIC_API_KEY=sk-ant-your_key_here
```

---

## .env.example

Commit this file (no real values). Developers copy it to `.env` and fill in values.

```bash
# Supabase
SUPABASE_URL=
SUPABASE_ANON_KEY=
# Service role key — bypasses RLS. Server-side only, never expose to frontend.
SUPABASE_SERVICE_KEY=
DATABASE_URL=

# Encryption (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
ENCRYPTION_KEY=

# Email (Resend)
RESEND_API_KEY=

# Anthropic — not used in VCE build
# ANTHROPIC_API_KEY=
```

---

## .gitignore

```
.env
__pycache__/
*.pyc
venv/
.venv/
*.egg-info/
dist/
build/
.pytest_cache/
```

---

## AI API keys — stored in database

AI API keys are stored in the `api_keys` table, encrypted using Fernet with `ENCRYPTION_KEY`.
They are managed exclusively through the dev portal API key pools page.
Multiple keys per provider are supported for round-robin rotation.

```python
# services/encryption.py
from cryptography.fernet import Fernet
from keys import ENCRYPTION_KEY

fernet = Fernet(ENCRYPTION_KEY.encode())

def encrypt_key(plain_text: str) -> str:
    return fernet.encrypt(plain_text.encode()).decode()

def decrypt_key(encrypted: str) -> str:
    return fernet.decrypt(encrypted.encode()).decode()
```

On app startup, all active API keys are decrypted into memory and stored in a provider pool dictionary. They are never written to disk or logged.

```python
# services/ai_cluster.py — startup
async def load_key_pools():
    keys = await db.fetch("SELECT * FROM api_keys WHERE is_active = true")
    pools = {}
    for key in keys:
        provider = key['provider']
        if provider not in pools:
            pools[provider] = []
        pools[provider].append({
            'id': key['id'],
            'key': decrypt_key(key['encrypted_key']),
            'label': key['key_label']
        })
    return pools
```

---

## Provider URLs

```python
# config.py — VCE build (free tier only)
PROVIDER_URLS = {
    'mistral':    'https://api.mistral.ai/v1',
    'openrouter': 'https://openrouter.ai/api/v1',
    'groq':       'https://api.groq.com/openai/v1',
    # 'anthropic': 'https://api.anthropic.com/v1',  # add post-VCE for commercial upgrade
}
```

All three VCE providers use an OpenAI-compatible SDK interface. **One package —
`openai` — serves all three**; only the `base_url` differs. It is a required entry in
`requirements.txt`, not an optional extra. Note that `zoneinfo` is Python 3.9+ standard
library and must never appear in `requirements.txt` — pip will fail on it.

```python
# services/ai_cluster.py
from openai import AsyncOpenAI

clients = {
    'mistral':    AsyncOpenAI(api_key=key, base_url=PROVIDER_URLS['mistral']),
    'openrouter': AsyncOpenAI(api_key=key, base_url=PROVIDER_URLS['openrouter']),
    'groq':       AsyncOpenAI(api_key=key, base_url=PROVIDER_URLS['groq']),
}
# One SDK, three providers — simpler than mixing SDKs
```

---

## Resend setup

1. Sign up at resend.com — free tier is 3,000 emails/month
2. Add and verify your sending domain (e.g. notifications@klasser.ai)
3. Create an API key in the Resend dashboard
4. Add to .env as RESEND_API_KEY

For development, Resend allows sending to your own verified email without a domain.

# Klasser AI

AI-powered school timetabling platform. Multi-AI generation, built-in validation, cross-campus transport scheduling, credit-based billing, and a full school management portal.

---

## Project structure

```
klasser-ai/
├── backend/
│   ├── main.py
│   ├── keys.py                  # All secrets — loads from .env
│   ├── config.py                # App settings, model names, thresholds
│   ├── requirements.txt
│   ├── .env                     # Never committed
│   ├── .gitignore
│   ├── routers/
│   │   ├── auth.py
│   │   ├── onboarding.py
│   │   ├── schools.py
│   │   ├── intake.py
│   │   ├── allocation.py
│   │   ├── transport.py
│   │   ├── validation.py
│   │   ├── editing.py
│   │   ├── timetables.py
│   │   ├── import_router.py
│   │   ├── export.py
│   │   ├── billing.py
│   │   ├── support.py
│   │   └── notifications.py
│   ├── services/
│   │   ├── ai_cluster.py        # Model fallback logic
│   │   ├── validators.py        # Quorum validator
│   │   ├── deterministic.py     # Deterministic checks
│   │   ├── transport_solver.py
│   │   ├── pipeline.py          # Orchestrates full generation
│   │   ├── versioning.py
│   │   ├── editing.py
│   │   ├── importer.py
│   │   ├── exporter.py
│   │   ├── email.py             # Resend integration
│   │   ├── billing.py
│   │   ├── fake_terminal.py     # Simulated card terminal
│   │   ├── timezone.py
│   │   └── encryption.py
│   └── models/
│       └── database.py
├── frontend/
│   ├── index.html               # Marketing site
│   ├── onboarding.html
│   ├── dashboard.html
│   ├── timetables.html
│   ├── layout.html
│   ├── data.html
│   ├── transport.html
│   ├── generate.html
│   ├── progress.html
│   ├── output.html
│   ├── import.html
│   ├── export-templates.html
│   ├── users.html
│   ├── billing.html
│   ├── notifications.html
│   ├── support.html
│   ├── dev-portal.html
│   ├── css/
│   │   └── style.css
│   └── js/
│       ├── api.js
│       ├── auth.js
│       ├── onboarding.js
│       ├── pipeline.js
│       ├── editor.js
│       ├── import.js
│       ├── export.js
│       └── billing.js
└── README.md
```

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, asyncpg |
| Database | Supabase (PostgreSQL) |
| Frontend | HTML, CSS, Vanilla JS |
| Auth | Supabase Auth (Google, Microsoft, password) |
| AI — allocation | Mistral Magistral (primary), Gemini 2.5 Flash via OpenRouter (fallback) |
| AI — formatting/routing | Groq (llama-3.3-70b-versatile) |
| AI — validators | Gemini 2.5 Flash, GPT-OSS via OpenRouter, Groq, Mistral Small |
| AI — import/interpret/edit | Gemini 2.5 Flash via OpenRouter |
| Email | Resend |
| Frontend hosting | Netlify |
| Backend hosting | Render (or local for dev) |

---

## Quick start

```bash
# 1. Clone and set up
git clone <repo>
cd klasser-ai/backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. Copy env template and fill in values
cp .env.example .env

# 3. Run migrations against Supabase
python migrate.py

# 4. Start backend
uvicorn main:app --reload --port 8000

# 5. Open frontend
# Open frontend/index.html in browser
# Or serve with: python -m http.server 3000 (from frontend/)
```

---

## Environment variables

See `KEYS.md` for full documentation of every variable required in `.env`.

---

## Documentation index

| File | Contents |
|---|---|
| `ARCHITECTURE.md` | System design, surfaces, pipeline overview |
| `DATABASE.md` | Full schema with all tables |
| `PIPELINE.md` | AI pipeline, chunking, fallback logic |
| `VALIDATION.md` | Quorum rules, deterministic checks |
| `TRANSPORT.md` | Bus scheduling, empty legs |
| `BILLING.md` | Credits, PAYG, invoiced billing, fake terminal |
| `AUTH.md` | Supabase auth, OAuth, invite flow |
| `IMPORT.md` | AI data import, any-format handling |
| `EXPORT.md` | CSV output templates |
| `EDITING.md` | AI-assisted timetable editing |
| `VERSIONING.md` | Timetable versions and publishing |
| `ONBOARDING.md` | Wizard flow, sandbox mode |
| `EMAIL.md` | All 22 email templates, Resend API |
| `NOTIFICATIONS.md` | User notification preferences |
| `SUPPORT.md` | Support ticketing system |
| `TIMEZONE.md` | Timezone handling strategy |
| `KEYS.md` | keys.py structure, .env reference |
| `CONFIG.md` | config.py settings, all configurable values |
| `BRAND.md` | Design system, colours, fonts, logo usage |
| `BUILD_ORDER.md` | Recommended build sequence |

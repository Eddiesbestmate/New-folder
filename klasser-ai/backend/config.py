# =============================================================================
# KLASSER AI - APP CONSTANTS
# Values that never change at runtime. Model strings, task assignments, pricing
# and thresholds all live in the `settings` table and are edited from the dev
# portal without a redeploy - they are NOT here.
# =============================================================================

# --- App ---
APP_NAME = "Klasser AI"
APP_URL = "https://klasser.ai"
FROM_EMAIL = "Klasser <notifications@klasser.ai>"
SUPPORT_EMAIL = "support@klasser.ai"

# --- Provider base URLs (VCE build - free tier only) ---
# All three use the OpenAI-compatible interface, so one SDK (openai.AsyncOpenAI)
# serves all of them; only base_url differs.
PROVIDER_URLS = {
    "mistral": "https://api.mistral.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    # Google AI Studio's OpenAI-compatible endpoint. Gemini through OpenRouter
    # is billed; the same models on a Google AI Studio key have a free tier.
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    # Anthropic's OpenAI-compatible endpoint, so Claude needs no separate SDK.
    "anthropic": "https://api.anthropic.com/v1",
}

# --- HTTP status codes that trigger provider fallback ---
RATE_LIMIT_CODES = [429, 503]

# --- Fake card terminal ---
FAKE_TERMINAL_BRANDS = ["Visa", "Mastercard", "Amex"]
# Default only. The live value is the `fake_terminal_success_rate` setting, which
# the dev portal can change to simulate declines.
FAKE_TERMINAL_SUCCESS_RATE = 1.0

# --- Billing calendar ---
# One timezone decides what "today" means for every daily billing job. Without
# it, a job comparing Python's local date against Postgres CURRENT_DATE (UTC)
# charges a day of interest twice whenever the two disagree - which is most of
# the Australian day, and always in production, where servers run UTC.
BILLING_TIMEZONE = "Australia/Sydney"


def billing_date():
    """Today, in the timezone billing is reckoned in."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(BILLING_TIMEZONE)).date()


def long_date(value) -> str:
    """
    A date as a person reads it: "3 April 2026".

    Built by hand rather than with strftime's no-pad flag (a dash after the
    percent). That flag is a glibc extension: it strips the leading zero on
    Linux and raises ValueError("Invalid format string") on Windows. Depending
    on it means code that works on the server and crashes on a developer's
    machine - or the reverse, which is how it was found here.
    """
    return f"{value.day} {value.strftime('%B %Y')}"


# --- CORS ---
# Netlify serves the frontend in production; the rest are local dev servers.
CORS_ORIGINS = [
    "https://klasser.ai",
    "https://www.klasser.ai",
    "https://klasser-test.netlify.app",
    "https://claris-hamamelidaceous-persistently.ngrok-free.dev",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]

# --- Timezones offered during onboarding (TIMEZONE.md) ---
# IANA names only. Never abbreviations - AEST/AEDT change with daylight saving.
SCHOOL_TIMEZONES = [
    ("Australia/Sydney", "NSW, VIC, TAS, ACT"),
    ("Australia/Brisbane", "Queensland"),
    ("Australia/Adelaide", "South Australia"),
    ("Australia/Darwin", "Northern Territory"),
    ("Australia/Perth", "Western Australia"),
]

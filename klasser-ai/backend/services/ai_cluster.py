"""
AI provider cluster - model selection, key rotation, fallback and logging.

Every AI call in the application goes through call(). It:

  1. resolves the model for a task from the settings table
  2. picks a key from that provider's pool, round-robin
  3. retries on transient errors, rotating to the next key each time
  4. falls back to the configured fallback model when the provider rate limits
  5. logs provider, model, key, tokens, latency and result to pipeline_log

Mistral, OpenRouter and Groq all speak the OpenAI protocol, so one SDK serves
all three - only base_url differs.

Model locking (PIPELINE.md): once an attempt falls back, the caller passes
locked_model on every subsequent call so the whole attempt stays on one model.
Never swap mid-chunk.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openai import AsyncOpenAI

import config
from models import database as db
from services import encryption
from services import settings as settings_service

log = logging.getLogger("klasser.ai")

# Model alias -> (provider, settings key holding the model string)
MODEL_REGISTRY: dict[str, tuple[str, str]] = {
    "magistral": ("mistral", "model_magistral"),
    "mistral_small": ("mistral", "model_mistral_small"),
    "gemini": ("openrouter", "model_gemini"),
    "gpt_oss": ("openrouter", "model_gpt_oss"),
    "groq": ("groq", "model_groq"),
    # Gemini on a Google AI Studio key, which has a free tier. The same models
    # through OpenRouter are billed.
    "gemini_free": ("google", "model_gemini_free"),
    # Claude via Anthropic's OpenAI-compatible endpoint.
    "claude": ("anthropic", "model_claude"),
    "claude_fast": ("anthropic", "model_claude_fast"),
}

# Tasks whose fallback model is configured separately (PIPELINE.md model locking).
FALLBACK_FOR: dict[str, str] = {
    "task_block_campus_primary": "task_block_campus_fallback",
    "task_teacher_primary": "task_teacher_fallback",
}


class AIError(Exception):
    """An AI call failed in a way the caller must handle."""

    def __init__(self, message: str, *, dev_fault: bool = True,
                 provider: str = "", model: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.dev_fault = dev_fault
        self.provider = provider
        self.model = model


class NoKeysConfigured(AIError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            f"No active API keys for {provider}. Add one in the dev portal.",
            dev_fault=True, provider=provider)


class RateLimited(AIError):
    pass


@dataclass
class KeyEntry:
    id: str
    label: str
    key: str
    provider: str


@dataclass
class Pool:
    """Keys for one provider, handed out round-robin."""
    provider: str
    entries: list[KeyEntry] = field(default_factory=list)
    cursor: int = 0

    def next(self) -> KeyEntry:
        if not self.entries:
            raise NoKeysConfigured(self.provider)
        entry = self.entries[self.cursor % len(self.entries)]
        self.cursor += 1
        return entry


_pools: dict[str, Pool] = {}
_loaded = False

# Swappable so tests can drive the retry and fallback logic without network
# calls or real credentials.
# The SDK defaults to a ten minute timeout, which is not a timeout so much as
# a hang: one stalled request held the whole validation stage silent for
# minutes with nothing in the log to say why. A provider that has not answered
# in two minutes is not about to.
AI_REQUEST_TIMEOUT = 120.0

_client_factory: Callable[[str, str], Any] = lambda api_key, base_url: AsyncOpenAI(
    api_key=api_key, base_url=base_url, max_retries=0,
    timeout=AI_REQUEST_TIMEOUT)


def set_client_factory(factory: Callable[[str, str], Any]) -> None:
    global _client_factory
    _client_factory = factory


# --- Key pools ---------------------------------------------------------------

async def load_key_pools() -> dict[str, int]:
    """
    Decrypt every active key into memory. Returns a count per provider.

    A key that fails to decrypt is skipped and logged rather than aborting
    startup - one bad row should not take down every provider.
    """
    global _loaded
    _pools.clear()

    if not encryption.is_configured():
        log.warning("ENCRYPTION_KEY not set - AI provider keys cannot be loaded")
        _loaded = True
        return {}

    rows = await db.fetch("""
        SELECT id, provider, key_label, encrypted_key
        FROM api_keys WHERE is_active = true
        ORDER BY provider, key_label
    """)

    for row in rows:
        try:
            plain = encryption.decrypt_key(row["encrypted_key"])
        except encryption.EncryptionUnavailable as exc:
            log.error("Skipping key %s/%s: %s",
                      row["provider"], row["key_label"], exc)
            continue

        pool = _pools.setdefault(row["provider"], Pool(provider=row["provider"]))
        pool.entries.append(KeyEntry(
            id=str(row["id"]), label=row["key_label"],
            key=plain, provider=row["provider"]))

    _loaded = True
    counts = {p: len(pool.entries) for p, pool in _pools.items()}
    log.info("AI key pools loaded: %s", counts or "none")
    return counts


async def ensure_loaded() -> None:
    if not _loaded:
        await load_key_pools()


def pool_status() -> dict[str, int]:
    return {p: len(pool.entries) for p, pool in _pools.items()}


# --- Model resolution --------------------------------------------------------

async def resolve_model(task_key: str) -> str:
    """The model alias assigned to a task, e.g. 'task_room_allocation' -> 'groq'."""
    alias = await settings_service.get(task_key)
    if not alias:
        raise AIError(f"No model assigned for {task_key}", dev_fault=True)
    if alias not in MODEL_REGISTRY:
        raise AIError(
            f"{task_key} is assigned to unknown model '{alias}'. "
            f"Known: {sorted(MODEL_REGISTRY)}", dev_fault=True)
    return alias


async def model_string(alias: str) -> str:
    """The provider's actual model name for an alias."""
    _, setting_key = MODEL_REGISTRY[alias]
    value = await settings_service.get(setting_key)
    if not value:
        raise AIError(f"Setting {setting_key} is not set", dev_fault=True)
    return value


async def fallback_for(task_key: str) -> Optional[str]:
    fallback_key = FALLBACK_FOR.get(task_key)
    if not fallback_key:
        return None
    alias = await settings_service.get(fallback_key)
    return alias if alias in MODEL_REGISTRY else None


# --- Error classification ----------------------------------------------------

def status_of(exc: Exception) -> Optional[int]:
    for attr in ("status_code", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None and isinstance(getattr(response, "status_code", None), int):
        return response.status_code
    return None


def is_rate_limit(exc: Exception) -> bool:
    if status_of(exc) in config.RATE_LIMIT_CODES:
        return True
    text = str(exc).lower()
    return "rate limit" in text or "too many requests" in text or "quota" in text


def is_retryable(exc: Exception) -> bool:
    status = status_of(exc)
    if status is not None and (status in config.RATE_LIMIT_CODES or status >= 500):
        return True
    text = str(exc).lower()
    return any(w in text for w in ("timeout", "timed out", "connection", "temporarily"))


# --- The call ----------------------------------------------------------------

async def call(
    task_key: str,
    prompt: str,
    *,
    system: Optional[str] = None,
    json_mode: bool = True,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    timetable_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    stage: Optional[str] = None,
    locked_model: Optional[str] = None,
) -> str:
    """
    Run one AI call for a task and return the raw text response.

    locked_model overrides the task's configured model. The pipeline passes it
    once an attempt has fallen back, so every remaining chunk uses the same
    model - see PIPELINE.md.
    """
    await ensure_loaded()

    alias = locked_model or await resolve_model(task_key)
    tried: list[str] = []

    while True:
        tried.append(alias)
        try:
            return await _call_model(
                alias, prompt, system=system, json_mode=json_mode,
                temperature=temperature, max_tokens=max_tokens,
                timetable_id=timetable_id, attempt_id=attempt_id,
                stage=stage or task_key)
        except RateLimited:
            # Fall back only if a fallback is configured and unused, and the
            # caller has not locked a model for this attempt.
            fallback = None if locked_model else await fallback_for(task_key)
            if fallback and fallback not in tried:
                log.warning("%s rate limited on %s - falling back to %s",
                            task_key, alias, fallback)
                if attempt_id:
                    await _log_event(attempt_id, "model_switch",
                                     f"Rate limit at {stage or task_key} - "
                                     f"switching to {fallback} for the rest of this attempt")
                alias = fallback
                continue
            raise


async def _call_model(
    alias: str,
    prompt: str,
    *,
    system: Optional[str],
    json_mode: bool,
    temperature: float,
    max_tokens: Optional[int],
    timetable_id: Optional[str],
    attempt_id: Optional[str],
    stage: str,
) -> str:
    provider, _ = MODEL_REGISTRY[alias]
    model_name = await model_string(alias)

    pool = _pools.get(provider)
    if pool is None or not pool.entries:
        raise NoKeysConfigured(provider)

    max_retries = await settings_service.get_int("max_retries", 3)
    delay = await settings_service.get_float("retry_delay_seconds", 5)

    # An unbounded output budget is not neutral. OpenRouter reserves credit for
    # the model's whole context window when max_tokens is absent and refuses
    # the request outright - "402: requires more credits, or fewer max_tokens"
    # - so every call with no limit failed there while the connectivity test,
    # which does pass one, reported the model healthy. It also leaves nothing
    # bounding a runaway response on any provider.
    if max_tokens is None:
        max_tokens = await settings_service.get_int("ai_max_output_tokens", 8000)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # OpenAI-compatible JSON mode rejects the request outright unless the word
    # "json" appears somewhere in the messages. Providers differ on how loudly
    # they say so - Groq returns a 400, which would otherwise look like a bad
    # prompt. Adding the instruction here means no caller has to remember.
    if json_mode and not any("json" in m["content"].lower() for m in messages):
        messages.insert(0, {
            "role": "system",
            "content": "Respond with a single valid JSON object and nothing else.",
        })

    last_exc: Optional[Exception] = None

    # One attempt per retry, each on the next key in the pool. With three keys
    # and three retries every key is tried before the model is given up on.
    for attempt in range(max_retries):
        entry = pool.next()
        client = _client_factory(entry.key, config.PROVIDER_URLS[provider])
        started = time.monotonic()

        try:
            kwargs: dict[str, Any] = {
                "model": model_name,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            response = await client.chat.completions.create(**kwargs)
            latency = int((time.monotonic() - started) * 1000)

            text = response.choices[0].message.content or ""
            tokens = getattr(getattr(response, "usage", None), "total_tokens", None)

            await _log_call(timetable_id, attempt_id, stage, alias, entry.id,
                            "success", None, tokens, latency)
            return text

        except Exception as exc:  # noqa: BLE001 - provider SDKs raise many types
            latency = int((time.monotonic() - started) * 1000)
            last_exc = exc
            rate_limited = is_rate_limit(exc)
            status = "rate_limited" if rate_limited else "error"

            await _log_call(timetable_id, attempt_id, stage, alias, entry.id,
                            status, str(exc)[:500], None, latency)

            if not is_retryable(exc):
                # A bad request or auth failure will not improve on retry.
                await _log_error(timetable_id, attempt_id, provider, entry.id,
                                 stage, str(exc)[:500])
                raise AIError(f"{provider}/{model_name}: {exc}",
                              dev_fault=True, provider=provider, model=alias) from exc

            if attempt < max_retries - 1:
                log.warning("%s/%s attempt %d failed (%s) - retrying on next key",
                            provider, alias, attempt + 1, status)
                await asyncio.sleep(delay)

    await _log_error(timetable_id, attempt_id, provider, None, stage,
                     f"exhausted {max_retries} attempts: {last_exc}")

    if last_exc is not None and is_rate_limit(last_exc):
        raise RateLimited(
            f"{provider}/{model_name} rate limited after {max_retries} attempts",
            dev_fault=True, provider=provider, model=alias) from last_exc

    raise AIError(
        f"{provider}/{model_name} failed after {max_retries} attempts: {last_exc}",
        dev_fault=True, provider=provider, model=alias)


# --- Logging -----------------------------------------------------------------

async def _log_call(timetable_id, attempt_id, stage, alias, api_key_id,
                    status, detail, tokens, latency_ms) -> None:
    """
    Record the call in pipeline_log.

    timetable_id is NOT NULL on that table, so calls made outside a generation
    (import, editing, a dev portal connectivity test) are logged at INFO
    instead of being dropped or crashing the caller.
    """
    if timetable_id is None:
        log.info("ai %s %s %s %sms%s", alias, stage, status,
                 latency_ms, f" - {detail}" if detail else "")
        return

    try:
        await db.execute("""
            INSERT INTO pipeline_log
                (timetable_id, attempt_id, stage, model_used, api_key_id,
                 status, detail, tokens_used, latency_ms)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, timetable_id, attempt_id, stage, alias, api_key_id,
            status, detail, tokens, latency_ms)
    except Exception:
        # Logging must never break the pipeline.
        log.exception("Could not write pipeline_log entry")


async def _log_error(timetable_id, attempt_id, provider, api_key_id,
                     stage, message) -> None:
    """Dev-fault by definition: an AI or infrastructure failure, not school data."""
    try:
        await db.execute("""
            INSERT INTO error_log
                (timetable_id, attempt_id, error_type, provider, api_key_id,
                 stage, is_dev_fault, error_message)
            VALUES ($1,$2,'ai_call',$3,$4,$5,true,$6)
        """, timetable_id, attempt_id, provider, api_key_id, stage, message)
    except Exception:
        log.exception("Could not write error_log entry")


async def _log_event(attempt_id, event_type, message) -> None:
    try:
        await db.execute("""
            INSERT INTO generation_events (attempt_id, event_type, message)
            VALUES ($1, $2, $3)
        """, attempt_id, event_type, message)
    except Exception:
        log.exception("Could not write generation_events entry")


# --- Connectivity test (dev portal) ------------------------------------------

async def test_provider(alias: str) -> dict:
    """Send a trivial prompt to confirm a provider actually answers."""
    if alias not in MODEL_REGISTRY:
        return {"ok": False, "error": f"Unknown model '{alias}'"}

    provider, _ = MODEL_REGISTRY[alias]
    started = time.monotonic()
    try:
        await ensure_loaded()
        # Generous token budget: reasoning models spend tokens before emitting
        # anything, and a cap too low fails as "could not validate JSON", which
        # looks like a broken provider rather than a short leash.
        text = await _call_model(
            alias, 'Reply with the JSON object {"ok": true} and nothing else.',
            system=None, json_mode=True, temperature=0, max_tokens=800,
            timetable_id=None, attempt_id=None, stage="connectivity_test")
        return {
            "ok": True, "provider": provider, "model": alias,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "reply": text[:120],
        }
    except AIError as exc:
        return {"ok": False, "provider": provider, "model": alias,
                "error": exc.message,
                "latency_ms": int((time.monotonic() - started) * 1000)}

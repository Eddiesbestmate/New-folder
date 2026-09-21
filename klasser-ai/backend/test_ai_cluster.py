"""
Phase 5 AI cluster test.

Provider calls are driven through a fake client, so retry, key rotation,
fallback and logging are all verified without real credentials or network
access. Encryption and the dev portal endpoints run against the live database.

    python test_ai_cluster.py
"""

import asyncio
import logging
import ssl
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import httpx

import keys
from models import database as db

try:
    import truststore

    truststore.inject_into_ssl()
    SSL_CONTEXT: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    SSL_CONTEXT = True

import main  # noqa: E402
import config  # noqa: E402
from services import ai_cluster, encryption  # noqa: E402
from services import settings as settings_service  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.ai").setLevel(logging.ERROR)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
created_key_ids: list[str] = []
created_schools: list[tuple[str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


# --- Fake provider -----------------------------------------------------------

class FakeStatusError(Exception):
    """Mimics an SDK error carrying an HTTP status."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class FakeUsage:
    total_tokens: int = 42


class FakeClient:
    """
    Records every call and replays a scripted sequence of outcomes.

    script entries: 'ok', an int HTTP status, or an Exception instance.
    """

    calls: list[dict[str, Any]] = []
    script: list[Any] = []

    def __init__(self, api_key: str, base_url: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.chat = self  # so client.chat.completions.create resolves
        self.completions = self

    async def create(self, **kwargs) -> Any:
        FakeClient.calls.append({
            "api_key": self.api_key,
            "base_url": self.base_url,
            "model": kwargs.get("model"),
        })

        outcome = FakeClient.script.pop(0) if FakeClient.script else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            raise FakeStatusError(outcome, f"HTTP {outcome} from fake provider")

        return _FakeResponse()

    @classmethod
    def reset(cls, script: Optional[list[Any]] = None) -> None:
        cls.calls = []
        cls.script = list(script or [])


class _FakeChoice:
    def __init__(self) -> None:
        self.message = type("M", (), {"content": '{"ok":true}'})()


class _FakeResponse:
    def __init__(self) -> None:
        self.choices = [_FakeChoice()]
        self.usage = FakeUsage()


# --- Helpers -----------------------------------------------------------------

async def sign_in(email: str) -> Optional[str]:
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def make_user(client: httpx.AsyncClient, tag: str, role: str) -> dict:
    email = f"phase5.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Phase 5 {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": "User", "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}],
    })
    if r.status_code != 201:
        raise RuntimeError(f"signup failed: {r.status_code} {r.text[:150]}")
    created_schools.append((r.json()["school_id"], r.json()["user_id"]))

    if role != "owner":
        await db.execute("UPDATE users SET role = $2 WHERE id = $1",
                         r.json()["user_id"], role)

    token = await sign_in(email)
    return {"Authorization": f"Bearer {token}"}


async def cleanup() -> None:
    for key_id in created_key_ids:
        await db.execute("UPDATE pipeline_log SET api_key_id = NULL WHERE api_key_id = $1", key_id)
        await db.execute("UPDATE error_log SET api_key_id = NULL WHERE api_key_id = $1", key_id)
        await db.execute("DELETE FROM api_keys WHERE id = $1", key_id)

    for school_id, user_id in created_schools:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM notification_preferences WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM onboarding WHERE school_id = $1",
                "DELETE FROM school_complexity WHERE school_id = $1",
                "DELETE FROM school_billing WHERE school_id = $1",
                "DELETE FROM school_credits WHERE school_id = $1",
                "DELETE FROM campuses WHERE school_id = $1",
                "DELETE FROM users WHERE school_id = $1",
                "DELETE FROM schools WHERE id = $1",
            ):
                await conn.execute(sql, school_id)
        await sb.delete_auth_user(user_id)


# --- Test --------------------------------------------------------------------

async def main_test() -> None:
    await db.connect()
    ai_cluster.set_client_factory(lambda k, u: FakeClient(k, u))
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            # --- Encryption ------------------------------------------------
            check(encryption.is_configured(), "ENCRYPTION_KEY is configured")

            secret = "sk-test-abcdefghijklmnop-1234"
            token = encryption.encrypt_key(secret)
            check(token != secret, "encrypt produces different text")
            check(encryption.decrypt_key(token) == secret, "decrypt round-trips")
            check(encryption.encrypt_key(secret) != token,
                  "same input encrypts differently each time (random IV)")
            check(encryption.mask(secret) == "sk-tes...1234",
                  "mask shows only the ends", encryption.mask(secret))
            check(encryption.mask("short") == "*****",
                  "short keys fully masked", encryption.mask("short"))

            # --- Dev role guard ---------------------------------------------
            owner_auth = await make_user(client, "owner", "owner")
            dev_auth = await make_user(client, "dev", "dev")

            r = await client.get("/dev/keys", headers=owner_auth)
            check(r.status_code == 403, "owner blocked from dev endpoints",
                  f"got {r.status_code}")
            r = await client.get("/dev/keys")
            check(r.status_code == 401, "dev endpoints need auth", f"got {r.status_code}")
            r = await client.get("/dev/keys", headers=dev_auth)
            check(r.status_code == 200, "dev can list keys", f"got {r.status_code}")

            # --- Key management ----------------------------------------------
            # Real provider keys may already be stored on this instance, so every
            # pool assertion below is a delta from here rather than an absolute
            # count - otherwise adding a live key breaks the suite.
            baseline = (await client.get("/dev/pools",
                                         headers=dev_auth)).json()["pools"]

            r = await client.post("/dev/keys", headers=dev_auth, json={
                "provider": "mistral", "key_label": f"Test 1 {RUN}",
                "api_key": "sk-mistral-key-one-000000", "account_label": "acct1"})
            check(r.status_code == 201, "add provider key", f"HTTP {r.status_code} {r.text[:100]}")
            created_key_ids.append(r.json()["id"])
            check(r.json()["hint"] == "sk-mis...0000", "response returns a masked hint",
                  r.json().get("hint"))

            listed = (await client.get("/dev/keys", headers=dev_auth)).json()
            mine = [k for k in listed if RUN in k["key_label"]]
            check(all("api_key" not in k and "encrypted_key" not in k for k in mine),
                  "listing never returns the key material")

            stored = await db.fetchval(
                "SELECT encrypted_key FROM api_keys WHERE id = $1", created_key_ids[0])
            check("sk-mistral-key-one" not in stored,
                  "key is stored encrypted, not in plaintext")

            r = await client.post("/dev/keys", headers=dev_auth, json={
                "provider": "mistral", "key_label": f"Test 1 {RUN}",
                "api_key": "sk-duplicate-label-00000"})
            check(r.status_code == 409, "duplicate label per provider rejected",
                  f"got {r.status_code}")

            r = await client.post("/dev/keys", headers=dev_auth, json={
                "provider": "openai", "key_label": "x", "api_key": "sk-nope-000000"})
            check(r.status_code == 422, "unknown provider rejected", f"got {r.status_code}")

            # Two more keys so rotation is observable.
            for n in (2, 3):
                r = await client.post("/dev/keys", headers=dev_auth, json={
                    "provider": "mistral", "key_label": f"Test {n} {RUN}",
                    "api_key": f"sk-mistral-key-{n}-000000"})
                created_key_ids.append(r.json()["id"])

            # The fallback model (gemini) lives on OpenRouter, so that pool needs
            # a key too or the fallback path cannot be exercised.
            r = await client.post("/dev/keys", headers=dev_auth, json={
                "provider": "openrouter", "key_label": f"OR 1 {RUN}",
                "api_key": "sk-or-fallback-key-000000"})
            created_key_ids.append(r.json()["id"])

            # Groq backs task_room_allocation, used below to test a task that has
            # keys but no configured fallback.
            r = await client.post("/dev/keys", headers=dev_auth, json={
                "provider": "groq", "key_label": f"GQ 1 {RUN}",
                "api_key": "gsk-groq-key-0000000000"})
            groq_key_id = r.json()["id"]
            created_key_ids.append(groq_key_id)

            def added(pools: dict, provider: str) -> int:
                return pools.get(provider, 0) - baseline.get(provider, 0)

            pools = (await client.get("/dev/pools", headers=dev_auth)).json()
            check(added(pools["pools"], "mistral") == 3,
                  "three keys loaded into the mistral pool",
                  str(pools["pools"]))
            check(added(pools["pools"], "openrouter") == 1,
                  "openrouter pool loaded", str(pools["pools"]))

            # --- Round-robin rotation -----------------------------------------
            FakeClient.reset()
            for _ in range(3):
                await ai_cluster.call("task_class_group_formation", "hi",
                                      stage="rotation_test")
            used = [c["api_key"] for c in FakeClient.calls]
            check(len(set(used)) == 3, "three calls use three different keys",
                  f"{len(set(used))} distinct")

            check(FakeClient.calls[0]["base_url"] == "https://api.mistral.ai/v1",
                  "mistral base_url used", FakeClient.calls[0]["base_url"])
            check(FakeClient.calls[0]["model"] == "magistral-medium-latest",
                  "model string resolved from settings",
                  FakeClient.calls[0]["model"])

            # --- Retry on transient failure -------------------------------------
            await settings_service.set_value("retry_delay_seconds", "0")
            settings_service.invalidate("retry_delay_seconds")

            FakeClient.reset([500, "ok"])
            text = await ai_cluster.call("task_class_group_formation", "hi",
                                         stage="retry_test")
            check(text == '{"ok":true}', "retries a 500 and returns the result")
            check(len(FakeClient.calls) == 2, "took two attempts",
                  f"{len(FakeClient.calls)}")
            check(FakeClient.calls[0]["api_key"] != FakeClient.calls[1]["api_key"],
                  "retry rotates to a different key")

            # --- Non-retryable error stops immediately ---------------------------
            FakeClient.reset([400, "ok"])
            try:
                await ai_cluster.call("task_class_group_formation", "hi",
                                      stage="bad_request_test")
                check(False, "400 raises rather than retrying")
            except ai_cluster.AIError as exc:
                check(not isinstance(exc, ai_cluster.RateLimited),
                      "400 raises AIError, not RateLimited")
                check(len(FakeClient.calls) == 1, "400 is not retried",
                      f"{len(FakeClient.calls)} attempts")

            # --- Rate limit exhausts retries then falls back ----------------------
            # Three 429s exhaust the primary, then the fallback answers.
            #
            # Which provider that is comes from settings, not from this test.
            # An earlier version asserted "OpenRouter" and "google/gemini-2.5-
            # flash" literally, and started failing the moment the fallback was
            # repointed at a different provider - reporting a configuration
            # change as a broken fallback. What matters is that it switched
            # away from the primary, to whatever is configured.
            primary_alias = await settings_service.get("task_block_campus_primary")
            fallback_alias = await settings_service.get("task_block_campus_fallback")
            fallback_provider, fallback_setting = \
                ai_cluster.MODEL_REGISTRY[fallback_alias]
            fallback_model = await settings_service.get(fallback_setting)
            primary_provider = ai_cluster.MODEL_REGISTRY[primary_alias][0]

            FakeClient.reset([429, 429, 429, "ok"])
            text = await ai_cluster.call("task_block_campus_primary", "hi",
                                         stage="fallback_test")
            check(text == '{"ok":true}', "falls back after rate limits")
            check(len(FakeClient.calls) == 4, "three primary attempts then one fallback",
                  f"{len(FakeClient.calls)}")
            check(FakeClient.calls[-1]["base_url"]
                  == config.PROVIDER_URLS[fallback_provider],
                  f"fallback switched provider to {fallback_provider}",
                  FakeClient.calls[-1]["base_url"])
            check(FakeClient.calls[-1]["base_url"]
                  != config.PROVIDER_URLS[primary_provider],
                  "and away from the primary's provider - the point of a fallback")
            check(FakeClient.calls[-1]["model"] == fallback_model,
                  f"fallback used the {fallback_alias} model string",
                  FakeClient.calls[-1]["model"])

            # --- locked_model suppresses fallback ----------------------------------
            FakeClient.reset([429, 429, 429, "ok"])
            try:
                await ai_cluster.call("task_block_campus_primary", "hi",
                                      stage="locked_test", locked_model="magistral")
                check(False, "locked model still raised after rate limits")
            except ai_cluster.RateLimited:
                check(len(FakeClient.calls) == 3,
                      "locked model does not fall back", f"{len(FakeClient.calls)}")

            # --- A task with no fallback configured ---------------------------------
            FakeClient.reset([429, 429, 429, "ok"])
            try:
                await ai_cluster.call("task_room_allocation", "hi", stage="no_fallback")
                check(False, "task without a fallback raised")
            except ai_cluster.RateLimited:
                check(len(FakeClient.calls) == 3,
                      "no fallback configured means no extra call",
                      f"{len(FakeClient.calls)}")

            # --- Misconfiguration ----------------------------------------------------
            try:
                await ai_cluster.call("task_does_not_exist", "hi")
                check(False, "unknown task rejected")
            except ai_cluster.AIError as exc:
                check("No model assigned" in exc.message, "unknown task explains itself")

            r = await client.put("/dev/settings/task_room_allocation",
                                 headers=dev_auth, json={"value": "not_a_model"})
            check(r.status_code == 422, "assignment to unknown model rejected",
                  f"got {r.status_code}")

            r = await client.put("/dev/settings/task_room_allocation",
                                 headers=dev_auth, json={"value": "groq"})
            check(r.status_code == 200, "valid assignment accepted", f"got {r.status_code}")

            # --- Deactivating a key ------------------------------------------------------
            before = (await client.get("/dev/pools", headers=dev_auth)).json()["pools"]
            r = await client.put(f"/dev/keys/{groq_key_id}/active",
                                 headers=dev_auth, params={"active": "false"})
            check(r.status_code == 200, "deactivate key", f"got {r.status_code}")
            check(r.json()["pools"].get("groq", 0) == before.get("groq", 0) - 1,
                  "deactivated key leaves the pool", str(r.json()["pools"]))

            r = await client.put(f"/dev/keys/{groq_key_id}/active",
                                 headers=dev_auth, params={"active": "true"})
            check(r.json()["pools"].get("groq", 0) == before.get("groq", 0),
                  "reactivating restores the pool", str(r.json()["pools"]))

            # --- Provider with no usable keys ---------------------------------------------
            # Point a task at a model whose provider currently has no key at all,
            # rather than assuming any particular provider is empty - this
            # instance may hold real keys for several of them.
            live = (await client.get("/dev/pools", headers=dev_auth)).json()["pools"]
            empty = next(
                ((alias, provider)
                 for alias, (provider, _) in sorted(ai_cluster.MODEL_REGISTRY.items())
                 if not live.get(provider)), None)
            check(empty is not None, "a provider with no keys exists to test against",
                  str(live))
            if empty:
                alias, provider = empty
                r = await client.put("/dev/settings/task_room_allocation",
                                     headers=dev_auth, json={"value": alias})
                check(r.status_code == 200, f"room allocation pointed at {alias}",
                      f"got {r.status_code}")

                FakeClient.reset()
                try:
                    await ai_cluster.call("task_room_allocation", "hi",
                                          stage="no_keys")
                    check(False, "provider with no keys raises")
                except ai_cluster.NoKeysConfigured as exc:
                    check(provider in exc.message.lower(),
                          "missing-keys error names the provider", exc.message)
                check(len(FakeClient.calls) == 0,
                      "no provider call attempted without a key",
                      f"{len(FakeClient.calls)}")

                await client.put("/dev/settings/task_room_allocation",
                                 headers=dev_auth, json={"value": "groq"})

            # --- Logging ----------------------------------------------------------------
            n_before = await db.fetchval(
                "SELECT count(*) FROM error_log WHERE error_type = 'ai_call'")
            FakeClient.reset([400, "ok"])
            raised = False
            try:
                await ai_cluster.call("task_class_group_formation", "hi",
                                      stage="error_log_test")
            except ai_cluster.AIError:
                raised = True
            # Asserted rather than swallowed: if a 400 ever stopped raising,
            # the log count below would still be checked but the reason for
            # checking it would have quietly disappeared.
            check(raised, "a 400 raises AIError")

            n_after = await db.fetchval(
                "SELECT count(*) FROM error_log WHERE error_type = 'ai_call'")
            check(n_after == n_before + 1, "hard failure written to error_log",
                  f"{n_before} -> {n_after}")

            row = await db.fetchrow("""
                SELECT is_dev_fault, provider FROM error_log
                WHERE error_type = 'ai_call' ORDER BY created_at DESC LIMIT 1
            """)
            check(row["is_dev_fault"] is True, "AI failures are dev-fault")
            check(row["provider"] == "mistral", "error_log records the provider")

            # --- Deleting a key clears log references -------------------------------------
            # created_key_ids[0] is the first mistral key, and the one the 400 test
            # above logged against - so this also proves the log rows are cleared.
            key_id = created_key_ids[0]
            r = await client.delete(f"/dev/keys/{key_id}", headers=dev_auth)
            check(r.status_code == 204, "delete key", f"got {r.status_code}")
            created_key_ids.remove(key_id)
            left = await db.fetchval(
                "SELECT count(*) FROM error_log WHERE api_key_id = $1", key_id)
            check(left == 0, "log rows no longer reference the deleted key", f"{left}")

            pools = (await client.get("/dev/pools", headers=dev_auth)).json()
            check(added(pools["pools"], "mistral") == 2,
                  "mistral pool reloaded after delete", str(pools["pools"]))

            # --- Health -----------------------------------------------------------------
            r = await client.get("/dev/health", headers=dev_auth)
            check(r.status_code == 200 and r.json()["encryption_configured"],
                  "dev health reports encryption configured")

        finally:
            ai_cluster.set_client_factory(
                lambda k, u: (_ for _ in ()).throw(RuntimeError("factory not restored")))
            await settings_service.set_value("retry_delay_seconds", "5")
            await cleanup()
            leftover = await db.fetchval(
                "SELECT count(*) FROM api_keys WHERE key_label LIKE $1", f"%{RUN}")
            check(leftover == 0, "test keys cleaned up", f"{leftover} left")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 5 - AI cluster\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

"""
Phase 2 end-to-end auth test against the live Supabase project.

Creates a real school and owner, logs in, calls the guarded endpoints, checks
the role guards reject correctly, then deletes everything it created.

Drives the app through httpx's ASGI transport rather than Starlette's
TestClient: TestClient runs the app in its own thread and event loop, which
cannot share an asyncpg pool created out here.

    python test_auth_flow.py
"""

import asyncio
import logging
import ssl
import sys
import uuid

import httpx

import keys
from models import database as db

try:
    import truststore

    truststore.inject_into_ssl()
    SSL_CONTEXT: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    SSL_CONTEXT = True

import main  # noqa: E402  (imported after truststore injection)
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

SUFFIX = uuid.uuid4().hex[:8]
# example.com is the RFC 2606 documentation domain. A reserved TLD such as
# .invalid is correctly rejected by EmailStr, so it cannot be used here.
EMAIL = f"phase2.{SUFFIX}@example.com"
PASSWORD = "TestPass123"
SCHOOL = f"Phase 2 Test School {SUFFIX}"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(email: str, password: str) -> str | None:
    """Get an access token the way the browser does."""
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post(
            "/auth/v1/token",
            params={"grant_type": "password"},
            headers={"apikey": keys.SUPABASE_ANON_KEY},
            json={"email": email, "password": password},
        )
    return r.json().get("access_token") if r.status_code == 200 else None


async def cleanup(user_id: str | None, school_id: str | None) -> None:
    """Remove everything the test created, children first."""
    if school_id:
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
    if user_id:
        await sb.delete_auth_user(user_id)


async def main_test() -> None:
    await db.connect()

    transport = httpx.ASGITransport(app=main.app)
    user_id = school_id = None

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            # --- Unauthenticated access ---------------------------------------
            r = await client.get("/auth/me")
            check(r.status_code == 401, "GET /auth/me without a token is 401",
                  f"got {r.status_code}")

            r = await client.get("/auth/me",
                                 headers={"Authorization": "Bearer not-a-real-token"})
            check(r.status_code == 401, "invalid token rejected", f"got {r.status_code}")

            r = await client.get("/auth/timezones")
            check(r.status_code == 200 and len(r.json()) == 5,
                  "GET /auth/timezones is public", f"{len(r.json())} zones")

            # --- Signup validation --------------------------------------------
            r = await client.post("/auth/signup", json={
                "school_name": SCHOOL, "timezone": "Mars/Olympus",
                "first_name": "A", "surname": "B",
                "email": EMAIL, "password": PASSWORD, "campuses": [],
            })
            check(r.status_code == 422, "unknown timezone rejected", f"got {r.status_code}")

            r = await client.post("/auth/signup", json={
                "school_name": SCHOOL, "timezone": "Australia/Sydney",
                "first_name": "A", "surname": "B",
                "email": EMAIL, "password": "short", "campuses": [],
            })
            check(r.status_code == 422, "weak password rejected", f"got {r.status_code}")

            r = await client.post("/auth/signup", json={
                "school_name": SCHOOL, "timezone": "Australia/Sydney",
                "first_name": "A", "surname": "B",
                "email": EMAIL, "password": "alllettersnodigits", "campuses": [],
            })
            check(r.status_code == 422, "password without a digit rejected",
                  f"got {r.status_code}")

            # --- Signup ---------------------------------------------------------
            r = await client.post("/auth/signup", json={
                "school_name": SCHOOL,
                "timezone": "Australia/Sydney",
                "first_name": "Test",
                "surname": "Owner",
                "email": EMAIL,
                "password": PASSWORD,
                "campuses": [{"name": "Main Campus", "address": "1 Test Street"}],
            })
            check(r.status_code == 201, "signup creates school",
                  f"HTTP {r.status_code} {r.text[:120]}")
            if r.status_code != 201:
                return

            school_id = r.json()["school_id"]
            user_id = r.json()["user_id"]

            # Every provisioning row must exist.
            for table, label in (
                ("school_credits", "credits row"),
                ("school_billing", "billing row"),
                ("school_complexity", "complexity row"),
                ("onboarding", "onboarding row"),
            ):
                n = await db.fetchval(
                    f"SELECT count(*) FROM {table} WHERE school_id = $1", school_id)
                check(n == 1, f"signup created {label}", f"{n}")

            n = await db.fetchval(
                "SELECT count(*) FROM notification_preferences WHERE user_id = $1",
                user_id)
            check(n == 1, "signup created notification preferences", f"{n}")

            n = await db.fetchval(
                "SELECT count(*) FROM campuses WHERE school_id = $1", school_id)
            check(n == 1, "signup created campus", f"{n}")

            role = await db.fetchval("SELECT role FROM users WHERE id = $1", user_id)
            check(role == "owner", "first user is owner", f"role={role}")

            # --- Duplicate signup -----------------------------------------------
            r = await client.post("/auth/signup", json={
                "school_name": "Another School", "timezone": "Australia/Perth",
                "first_name": "Dupe", "surname": "User",
                "email": EMAIL, "password": PASSWORD, "campuses": [],
            })
            check(r.status_code == 409, "duplicate email rejected", f"got {r.status_code}")

            n = await db.fetchval(
                "SELECT count(*) FROM schools WHERE name = 'Another School'")
            check(n == 0, "rejected signup created no school", f"{n}")

            # --- Login and authenticated calls ------------------------------------
            token = await sign_in(EMAIL, PASSWORD)
            check(token is not None, "password login returns a token")
            if not token:
                return

            auth = {"Authorization": f"Bearer {token}"}

            r = await client.get("/auth/me", headers=auth)
            check(r.status_code == 200, "GET /auth/me with token", f"HTTP {r.status_code}")
            if r.status_code == 200:
                me = r.json()
                check(me["school_name"] == SCHOOL, "profile returns school name")
                check(me["role"] == "owner", "profile returns role")
                check(me["login_method"] == "password", "login_method recorded")
                check(me["onboarding_complete"] is False, "onboarding not yet complete")

            # --- Role guard ---------------------------------------------------------
            r = await client.get("/auth/check-dev", headers=auth)
            check(r.status_code == 403, "owner blocked from dev endpoint",
                  f"got {r.status_code}")

            # --- Wrong password -----------------------------------------------------
            bad = await sign_in(EMAIL, "WrongPassword123")
            check(bad is None, "wrong password rejected")

            # --- Deactivation revokes access ----------------------------------------
            await db.execute(
                "UPDATE users SET is_active = false, deactivated_at = now() WHERE id = $1",
                user_id)
            sb.forget_token(token)  # bypass the 30s verification cache
            r = await client.get("/auth/me", headers=auth)
            check(r.status_code == 403,
                  "deactivated account blocked with a valid token", f"got {r.status_code}")

        finally:
            await cleanup(user_id, school_id)
            left = await db.fetchval(
                "SELECT count(*) FROM schools WHERE name = $1", SCHOOL)
            check(left == 0, "test data cleaned up", f"{left} school(s) left")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 2 - authentication\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

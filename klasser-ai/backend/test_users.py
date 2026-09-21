"""
Phase 15 - users, invites and deactivation.

Runs the whole invite cycle against live Supabase: invite, accept, sign in as
the new person, verify they see the school's data, then deactivate and confirm
access stops.

The cases that matter are the ones that lock a school out: the last owner must
not be removable, and an invite token must work exactly once.

    python test_users.py
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

import main  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.users").setLevel(logging.CRITICAL)
logging.getLogger("klasser.supabase").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"
NEW_PASSWORD = "Joined2026x"

results: list[tuple[bool, str]] = []
schools: list[str] = []
auth_users: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(email: str, password: str = PASSWORD):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": password})
    return r.json().get("access_token") if r.status_code == 200 else None


async def make_school(client, tag: str) -> tuple[dict, str, str, str]:
    email = f"p15.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Users {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": tag.title(), "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append(school_id)
    auth_users.append(user_id)
    return ({"Authorization": f"Bearer {await sign_in(email)}"},
            school_id, user_id, email)


async def cleanup() -> None:
    for school_id in schools:
        people = [str(r["id"]) for r in await db.fetch(
            "SELECT id FROM users WHERE school_id = $1", school_id)]
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM notification_preferences WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM invites WHERE school_id = $1",
                "DELETE FROM onboarding WHERE school_id = $1",
                "DELETE FROM school_complexity WHERE school_id = $1",
                "DELETE FROM school_billing WHERE school_id = $1",
                "DELETE FROM school_credits WHERE school_id = $1",
                "DELETE FROM campuses WHERE school_id = $1",
                "DELETE FROM users WHERE school_id = $1",
                "DELETE FROM schools WHERE id = $1",
            ):
                await conn.execute(sql, school_id)
        for person in people:
            await sb.delete_auth_user(person)

    # Auth users created by an invite that was never accepted have no row of
    # their own, so they have to be cleared by address.
    for tag in ("invitee", "second", "revoked", "expired"):
        found = await sb.find_auth_user(f"p15.{tag}.{RUN}@example.com")
        if found:
            await sb.delete_auth_user(found["id"])


async def main_test() -> None:
    await db.connect()
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            auth, school_id, owner_id, owner_email = await make_school(client, "a")
            other_auth, other_school, other_id, _ = await make_school(client, "b")

            # --- Listing -------------------------------------------------------
            r = await client.get("/users", headers=auth)
            check(r.status_code == 200, "GET users", f"HTTP {r.status_code}")
            data = r.json()
            check(len(data["users"]) == 1, "the owner is the only user",
                  str(len(data["users"])))
            check(data["users"][0]["is_you"] is True, "and is marked as you")
            check(data["active_owners"] == 1, "one active owner")
            check(data["can_manage"] is True, "an owner can manage")

            # --- Inviting -------------------------------------------------------
            invitee_email = f"p15.invitee.{RUN}@example.com"
            r = await client.post("/users/invite", headers=auth,
                                  json={"email": invitee_email, "role": "staff"})
            check(r.status_code == 201, "send an invite",
                  f"HTTP {r.status_code} {r.text[:140]}")
            invite = r.json()
            check("accept-invite.html?token=" in invite["accept_url"],
                  "an accept link is returned", invite["accept_url"][:60])
            check(invite["expires_in_days"] == 7, "it lasts a week")

            token = invite["accept_url"].split("token=")[1]
            check(len(token) > 30, "the token is long enough to be unguessable",
                  f"{len(token)} chars")

            r = await client.post("/users/invite", headers=auth,
                                  json={"email": invitee_email, "role": "staff"})
            check(r.status_code == 409, "a second invite to the same address "
                                        "is refused", f"got {r.status_code}")

            r = await client.post("/users/invite", headers=auth,
                                  json={"email": owner_email, "role": "staff"})
            check(r.status_code == 409, "inviting an existing member is refused",
                  f"got {r.status_code}")

            r = await client.post("/users/invite", headers=auth,
                                  json={"email": invitee_email, "role": "admin"})
            check(r.status_code == 422, "an unknown role is refused",
                  f"got {r.status_code}")

            r = await client.post("/users/invite", headers=auth,
                                  json={"email": "not-an-email", "role": "staff"})
            check(r.status_code == 422, "a malformed address is refused",
                  f"got {r.status_code}")

            r = await client.get("/users", headers=auth)
            check(len(r.json()["invites"]) == 1, "the invite is listed as pending",
                  str(len(r.json()["invites"])))

            # --- Looking up the invite (signed out) --------------------------------
            r = await client.get(f"/users/invite/{token}")
            check(r.status_code == 200, "the accept page can read the invite "
                                        "without a session", f"HTTP {r.status_code}")
            check(r.json()["email"] == invitee_email, "it shows the address")
            check(r.json()["school"] == f"Users a {RUN}", "and the school",
                  r.json()["school"])

            r = await client.get("/users/invite/not-a-real-token-at-all-12345")
            check(r.status_code == 404, "an unknown token is refused",
                  f"got {r.status_code}")

            # --- Accepting -----------------------------------------------------------
            r = await client.post("/users/accept", json={
                "token": token, "first_name": "New", "surname": "Person",
                "password": "short"})
            check(r.status_code == 422, "a weak password is refused",
                  f"got {r.status_code}")

            r = await client.post("/users/accept", json={
                "token": token, "first_name": "New", "surname": "Person",
                "password": "alllettersonly"})
            check(r.status_code == 422,
                  "a password with no digit is refused", f"got {r.status_code}")

            r = await client.post("/users/accept", json={
                "token": token, "first_name": "New", "surname": "Person",
                "password": NEW_PASSWORD})
            check(r.status_code == 200, "accept the invite",
                  f"HTTP {r.status_code} {r.text[:140]}")
            check(r.json()["school"] == f"Users a {RUN}", "joined the right school")

            joined = await db.fetchrow("""
                SELECT id, school_id, first_name, surname, role, is_active,
                       login_method
                FROM users WHERE lower(email) = $1
            """, invitee_email)
            check(joined is not None, "a user row exists")
            check(str(joined["school_id"]) == school_id,
                  "on the inviting school")
            check(joined["role"] == "staff", "with the invited role",
                  joined["role"])
            check(joined["first_name"] == "New" and joined["surname"] == "Person",
                  "and the name they gave")
            check(joined["login_method"] == "password", "login method recorded")

            prefs = await db.fetchval(
                "SELECT 1 FROM notification_preferences WHERE user_id = $1",
                joined["id"])
            check(bool(prefs), "notification preferences were provisioned")

            invite_row = await db.fetchrow(
                "SELECT status, accepted_at FROM invites WHERE token = $1", token)
            check(invite_row["status"] == "accepted", "the invite is marked used")
            check(invite_row["accepted_at"] is not None, "with a timestamp")

            # A token must work exactly once.
            r = await client.post("/users/accept", json={
                "token": token, "first_name": "Impostor", "surname": "Person",
                "password": "Another123"})
            check(r.status_code == 404, "the token cannot be used twice",
                  f"got {r.status_code}")
            r = await client.get(f"/users/invite/{token}")
            check(r.status_code == 404, "and the accept page no longer loads it",
                  f"got {r.status_code}")

            # --- The new person can actually use the product --------------------------
            new_token = await sign_in(invitee_email, NEW_PASSWORD)
            check(new_token is not None,
                  "the new user can sign in with the password they chose")
            new_auth = {"Authorization": f"Bearer {new_token}"}

            r = await client.get("/auth/me", headers=new_auth)
            check(r.status_code == 200, "and reach the API", f"HTTP {r.status_code}")
            check(r.json()["school_name"] == f"Users a {RUN}",
                  "seeing the right school", r.json().get("school_name"))
            check(r.json()["role"] == "staff", "as staff")

            r = await client.get("/schools/teachers", headers=new_auth)
            check(r.status_code == 200, "they can read the school's data",
                  f"HTTP {r.status_code}")

            r = await client.post("/users/invite", headers=new_auth,
                                  json={"email": f"p15.second.{RUN}@example.com",
                                        "role": "staff"})
            check(r.status_code == 403, "but staff cannot invite anyone",
                  f"got {r.status_code}")

            r = await client.get("/users", headers=new_auth)
            check(r.status_code == 200 and r.json()["can_manage"] is False,
                  "and are told they cannot manage")

            # --- Roles -------------------------------------------------------------------
            new_id = str(joined["id"])
            r = await client.patch(f"/users/{new_id}/role", headers=auth,
                                   json={"role": "owner"})
            check(r.status_code == 200, "an owner promotes them")
            check(await db.fetchval(
                "SELECT role FROM users WHERE id = $1", new_id) == "owner",
                "the role changed")

            r = await client.patch(f"/users/{owner_id}/role", headers=auth,
                                   json={"role": "staff"})
            check(r.status_code == 200,
                  "the original owner can now step down, since there are two",
                  f"got {r.status_code}")

            # Now exactly one owner remains - the new person.
            r = await client.patch(f"/users/{new_id}/role", headers=new_auth,
                                   json={"role": "staff"})
            check(r.status_code == 409,
                  "the last owner cannot demote themselves",
                  f"got {r.status_code}")
            check(await db.fetchval(
                "SELECT role FROM users WHERE id = $1", new_id) == "owner",
                "and the school keeps an owner")

            # --- Deactivation ---------------------------------------------------------------
            r = await client.post(f"/users/{new_id}/deactivate", headers=new_auth)
            check(r.status_code == 409, "you cannot deactivate yourself",
                  f"got {r.status_code}")

            r = await client.post(f"/users/{owner_id}/deactivate", headers=new_auth)
            check(r.status_code == 200, "an owner deactivates a staff member",
                  f"HTTP {r.status_code} {r.text[:120]}")

            row = await db.fetchrow(
                "SELECT is_active, deactivated_at FROM users WHERE id = $1",
                owner_id)
            check(row["is_active"] is False, "the account is switched off")
            check(row["deactivated_at"] is not None, "with a timestamp")

            # Their existing token must stop working - is_active is checked on
            # every request, so this does not wait for the JWT to expire.
            sb.forget_token(auth["Authorization"].split()[1])
            r = await client.get("/auth/me", headers=auth)
            check(r.status_code == 403,
                  "their existing session stops working immediately",
                  f"got {r.status_code}")

            r = await client.get("/schools/teachers", headers=auth)
            check(r.status_code == 403, "on every endpoint, not just /me",
                  f"got {r.status_code}")

            r = await client.get("/users", headers=new_auth)
            deactivated = [u for u in r.json()["users"] if not u["is_active"]]
            check(len(deactivated) == 1, "they are listed as deactivated",
                  str(len(deactivated)))

            # The last active owner is protected.
            r = await client.post(f"/users/{new_id}/deactivate", headers=new_auth)
            check(r.status_code == 409, "the last owner cannot be deactivated",
                  f"got {r.status_code}")

            r = await client.post(f"/users/{owner_id}/reactivate", headers=new_auth)
            check(r.status_code == 200, "reactivate them")
            sb.forget_token(auth["Authorization"].split()[1])
            r = await client.get("/auth/me", headers=auth)
            check(r.status_code == 200, "and their access comes straight back",
                  f"got {r.status_code}")

            # A deactivated address cannot be re-invited; it is reactivated.
            await client.post(f"/users/{owner_id}/deactivate", headers=new_auth)
            r = await client.post("/users/invite", headers=new_auth,
                                  json={"email": owner_email, "role": "staff"})
            check(r.status_code == 409,
                  "a deactivated member is reactivated, not re-invited",
                  f"got {r.status_code}")
            check("reactivate" in r.text.lower(), "and the message says so",
                  r.text[:120])
            await client.post(f"/users/{owner_id}/reactivate", headers=new_auth)

            # --- Revoking ----------------------------------------------------------------------
            revoked_email = f"p15.revoked.{RUN}@example.com"
            r = await client.post("/users/invite", headers=new_auth,
                                  json={"email": revoked_email, "role": "staff"})
            revoked_id = r.json()["id"]
            revoked_token = r.json()["accept_url"].split("token=")[1]

            r = await client.delete(f"/users/invites/{revoked_id}", headers=new_auth)
            check(r.status_code == 204, "revoke a pending invite",
                  f"got {r.status_code}")
            r = await client.get(f"/users/invite/{revoked_token}")
            check(r.status_code == 404, "the link stops working",
                  f"got {r.status_code}")
            check(await sb.find_auth_user(revoked_email) is None,
                  "and the half-made account is cleaned up, so the address can "
                  "be invited again")

            r = await client.delete(f"/users/invites/{revoked_id}", headers=new_auth)
            check(r.status_code == 404, "revoking it twice does nothing",
                  f"got {r.status_code}")

            # --- Expiry -----------------------------------------------------------------------------
            expired_email = f"p15.expired.{RUN}@example.com"
            r = await client.post("/users/invite", headers=new_auth,
                                  json={"email": expired_email, "role": "staff"})
            expired_id = r.json()["id"]
            expired_token = r.json()["accept_url"].split("token=")[1]

            await db.execute("""
                UPDATE invites SET expires_at = now() - interval '1 day'
                WHERE id = $1
            """, expired_id)

            r = await client.get(f"/users/invite/{expired_token}")
            check(r.status_code == 404, "an expired invite cannot be read",
                  f"got {r.status_code}")
            r = await client.post("/users/accept", json={
                "token": expired_token, "first_name": "Too", "surname": "Late",
                "password": NEW_PASSWORD})
            check(r.status_code == 404, "nor accepted", f"got {r.status_code}")

            r = await client.post(f"/users/invites/{expired_id}/resend",
                                  headers=new_auth)
            check(r.status_code == 200, "but it can be resent",
                  f"got {r.status_code}")
            r = await client.get(f"/users/invite/{expired_token}")
            check(r.status_code == 200, "and then works again",
                  f"got {r.status_code}")

            # --- Isolation -----------------------------------------------------------------------------
            r = await client.get("/users", headers=other_auth)
            check(len(r.json()["users"]) == 1,
                  "another school sees only its own people",
                  str(len(r.json()["users"])))
            check(r.json()["invites"] == [], "and none of the invites")

            r = await client.patch(f"/users/{new_id}/role", headers=other_auth,
                                   json={"role": "staff"})
            check(r.status_code == 404,
                  "another school cannot change a role", f"got {r.status_code}")
            r = await client.post(f"/users/{new_id}/deactivate",
                                  headers=other_auth)
            check(r.status_code == 404,
                  "nor deactivate anyone", f"got {r.status_code}")
            r = await client.delete(f"/users/invites/{expired_id}",
                                    headers=other_auth)
            check(r.status_code == 404,
                  "nor revoke an invite", f"got {r.status_code}")

        finally:
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM schools WHERE name LIKE $1",
                    f"Users%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 15 - users, invites and deactivation\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

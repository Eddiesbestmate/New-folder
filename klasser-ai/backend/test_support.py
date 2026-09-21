"""
Phase 13 - notification preferences and support tickets.

The cases that matter: a school must never see another school's tickets or the
dev portal's internal notes, and a preference a user turns off must actually be
read as off by the code that will send the email.

    python test_support.py
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
from routers import notifications, support  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.support").setLevel(logging.CRITICAL)
logging.getLogger("klasser.notifications").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(email: str):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def make_school(client, tag: str, role: str = "owner") -> tuple[dict, str, str]:
    email = f"p13.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Support {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": tag.title(), "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))

    if role != "owner":
        await db.execute("UPDATE users SET role = $2 WHERE id = $1", user_id, role)

    return ({"Authorization": f"Bearer {await sign_in(email)}"},
            school_id, user_id)


async def cleanup() -> None:
    for school_id, user_id in schools:
        await db.execute(
            "DELETE FROM notification_preferences WHERE user_id = $1", user_id)
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM support_tickets WHERE school_id = $1",
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


async def main_test() -> None:
    await db.connect()
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            auth, school_id, user_id = await make_school(client, "a")
            other_auth, other_school, other_user = await make_school(client, "b")
            dev_auth, _, _ = await make_school(client, "dev", role="dev")

            # --- Notification preferences -------------------------------------
            r = await client.get("/notifications", headers=auth)
            check(r.status_code == 200, "GET preferences", f"HTTP {r.status_code}")
            data = r.json()
            check(len(data["preferences"]) == len(notifications.PREFERENCES),
                  "every preference is returned",
                  f"{len(data['preferences'])}")
            check(all(v is True for v in data["preferences"].values()),
                  "all default to on")
            check([g["group"] for g in data["groups"]]
                  == notifications.GROUP_ORDER,
                  "grouped in the documented order",
                  str([g["group"] for g in data["groups"]]))

            r = await client.put("/notifications", headers=auth, json={
                "preferences": {"notify_low_balance": False,
                                "notify_edit_applied": False}})
            check(r.status_code == 200 and r.json()["updated"] == 2,
                  "two preferences saved", str(r.json()))

            r = await client.get("/notifications", headers=auth)
            prefs = r.json()["preferences"]
            check(prefs["notify_low_balance"] is False, "the change stuck")
            check(prefs["notify_generation_complete"] is True,
                  "untouched preferences are unchanged")

            # The function the email layer will actually call.
            check(await notifications.wants(user_id, "notify_low_balance") is False,
                  "wants() reports the preference as off")
            check(await notifications.wants(user_id, "notify_generation_complete")
                  is True, "wants() reports an untouched one as on")
            check(await notifications.wants(user_id, "notify_nonsense") is True,
                  "an unknown preference defaults to sending")

            r = await client.put("/notifications", headers=auth, json={
                "preferences": {"notify_nonsense": True}})
            check(r.status_code == 422, "an unknown preference is refused",
                  f"got {r.status_code}")

            people = await notifications.recipients(school_id,
                                                    "notify_generation_complete")
            check(len(people) == 1, "recipients() finds the school's user",
                  f"{len(people)}")
            people = await notifications.recipients(school_id, "notify_low_balance")
            check(people == [], "and excludes the one who opted out",
                  f"{len(people)}")

            await db.execute("UPDATE users SET is_active = false WHERE id = $1",
                             user_id)
            people = await notifications.recipients(school_id,
                                                    "notify_generation_complete")
            check(people == [], "a deactivated user is never emailed",
                  f"{len(people)}")
            await db.execute("UPDATE users SET is_active = true WHERE id = $1",
                             user_id)

            owner = await notifications.owner_of(school_id)
            check(owner and owner["id"] == user_id, "owner_of finds the owner")

            # --- Triage --------------------------------------------------------
            check(support.triage("billing", "we are blocked") == "urgent",
                  "a blocked billing ticket is urgent")
            check(support.triage("generation", "cannot generate at all")
                  == "urgent", "cannot generate is urgent")
            check(support.triage("data", "we are blocked") == "high",
                  "the same words on a data ticket are high, not urgent")
            check(support.triage("billing", "a question about credits") == "high",
                  "billing defaults to high")
            check(support.triage("other", "just wondering") == "normal",
                  "everything else is normal")

            # --- Raising a ticket ------------------------------------------------
            r = await client.post("/support/tickets", headers=auth, json={
                "category": "generation",
                "subject": "Generation produced odd results",
                "body": "Two classes ended up in the same room on day 3."})
            check(r.status_code == 201, "raise a ticket", f"HTTP {r.status_code}")
            ticket_id = r.json()["id"]
            check(r.json()["priority"] == "high",
                  "a generation ticket is prioritised", r.json()["priority"])

            r = await client.post("/support/tickets", headers=auth, json={
                "category": "billing", "subject": "Cannot generate",
                "body": "Our account seems to be blocked and we cannot generate."})
            urgent_id = r.json()["id"]
            check(r.json()["priority"] == "urgent",
                  "a blocked school goes to the top", r.json()["priority"])

            r = await client.post("/support/tickets", headers=auth, json={
                "category": "nonsense", "subject": "Hello",
                "body": "This category does not exist at all."})
            check(r.status_code == 422, "an unknown category is refused",
                  f"got {r.status_code}")

            r = await client.post("/support/tickets", headers=auth, json={
                "category": "other", "subject": "Hi", "body": "too short"})
            check(r.status_code == 422, "a body that says nothing is refused",
                  f"got {r.status_code}")

            # A timetable from another school cannot be attached.
            layout_id = await db.fetchval("""
                INSERT INTO timetable_layouts (school_id, name, days_in_cycle,
                                               is_active)
                VALUES ($1, 'T', 5, true) RETURNING id
            """, other_school)
            foreign_tid = str(await db.fetchval("""
                INSERT INTO timetables (school_id, name, layout_id, status)
                VALUES ($1, $2, $3, 'complete') RETURNING id
            """, other_school, f"Other {RUN}", layout_id))

            r = await client.post("/support/tickets", headers=auth, json={
                "category": "data", "subject": "Attaching someone else's",
                "body": "Trying to attach a timetable we do not own.",
                "timetable_id": foreign_tid})
            check(r.status_code == 400,
                  "a timetable from another school cannot be attached",
                  f"got {r.status_code}")

            # --- Reading tickets ---------------------------------------------------
            r = await client.get("/support/tickets", headers=auth)
            check(r.status_code == 200 and len(r.json()) == 2,
                  "a school sees its own tickets", f"{len(r.json())}")
            check(all("internal_notes" not in t for t in r.json()),
                  "internal notes are never in a school-facing list")

            r = await client.get(f"/support/tickets/{ticket_id}", headers=auth)
            check(r.status_code == 200, "GET one ticket")
            check("internal_notes" not in r.json(),
                  "nor in a single school-facing ticket")

            r = await client.get("/support/tickets", headers=other_auth)
            check(r.json() == [], "another school sees none of them",
                  f"{len(r.json())}")
            r = await client.get(f"/support/tickets/{ticket_id}",
                                 headers=other_auth)
            check(r.status_code == 404, "and cannot open one by id",
                  f"got {r.status_code}")

            r = await client.get("/support/contact")
            check(r.status_code == 200 and "@" in r.json()["support_email"],
                  "contact details are available without logging in")

            # --- Dev queue ----------------------------------------------------------
            r = await client.get("/support/dev/tickets", headers=auth)
            check(r.status_code == 403, "a school cannot read the dev queue",
                  f"got {r.status_code}")

            r = await client.get("/support/dev/tickets", headers=dev_auth)
            check(r.status_code == 200, "dev reads the queue", f"HTTP {r.status_code}")
            queue = r.json()
            check(queue["totals"]["open"] >= 2, "open tickets counted",
                  str(queue["totals"]["open"]))

            ours = [t for t in queue["tickets"]
                    if t["id"] in (ticket_id, urgent_id)]
            check(len(ours) == 2, "both tickets are in the queue", str(len(ours)))
            check(ours[0]["id"] == urgent_id,
                  "the urgent one is listed first",
                  f"{[t['priority'] for t in ours]}")

            r = await client.get("/support/dev/tickets", headers=dev_auth,
                                 params={"priority": "urgent"})
            check(all(t["priority"] == "urgent" for t in r.json()["tickets"]),
                  "filtering by priority works")

            r = await client.get("/support/dev/tickets", headers=dev_auth,
                                 params={"status": "nonsense"})
            check(r.status_code == 422, "an unknown status filter is refused",
                  f"got {r.status_code}")

            r = await client.get(f"/support/dev/tickets/{ticket_id}",
                                 headers=dev_auth)
            check(r.status_code == 200, "dev opens a ticket")
            check("internal_notes" in r.json()["ticket"],
                  "the dev view does include internal notes")
            check("pipeline_log" in r.json(), "and the pipeline log")

            # --- Working a ticket -----------------------------------------------------
            r = await client.patch(f"/support/dev/tickets/{ticket_id}",
                                   headers=dev_auth,
                                   json={"status": "in_progress",
                                         "internal_notes": "Looking into it."})
            check(r.status_code == 200, "dev updates a ticket")

            row = await db.fetchrow("""
                SELECT status, internal_notes, first_response_at, resolved_at
                FROM support_tickets WHERE id = $1
            """, ticket_id)
            check(row["status"] == "in_progress", "status changed")
            check(row["internal_notes"] == "Looking into it.", "note saved")
            check(row["first_response_at"] is not None,
                  "first response time recorded")
            check(row["resolved_at"] is None, "not resolved yet")

            # The school still must not see the note.
            r = await client.get(f"/support/tickets/{ticket_id}", headers=auth)
            check("Looking into it." not in r.text,
                  "the internal note does not reach the school", r.text[:80])

            first_response = row["first_response_at"]
            r = await client.patch(f"/support/dev/tickets/{ticket_id}",
                                   headers=dev_auth, json={"status": "resolved"})
            row = await db.fetchrow("""
                SELECT status, resolved_at, resolved_by, first_response_at
                FROM support_tickets WHERE id = $1
            """, ticket_id)
            check(row["status"] == "resolved", "ticket resolved")
            check(row["resolved_at"] is not None, "resolution time recorded")
            check(row["resolved_by"] is not None, "and who resolved it",
                  str(row["resolved_by"]))
            check(row["first_response_at"] == first_response,
                  "first response time is not overwritten by later edits")

            r = await client.patch(f"/support/dev/tickets/{ticket_id}",
                                   headers=dev_auth, json={"status": "nonsense"})
            check(r.status_code == 422, "an unknown status is refused",
                  f"got {r.status_code}")

            r = await client.patch(f"/support/dev/tickets/{ticket_id}",
                                   headers=auth, json={"status": "open"})
            check(r.status_code == 403, "a school cannot work a ticket",
                  f"got {r.status_code}")

            # --- Withdrawing -------------------------------------------------------------
            r = await client.post(f"/support/tickets/{urgent_id}/close",
                                  headers=auth)
            check(r.status_code == 200, "a school withdraws its own ticket")
            check(await db.fetchval(
                "SELECT status FROM support_tickets WHERE id = $1", urgent_id)
                == "closed", "it is closed, not resolved - the two mean "
                             "different things")

            r = await client.post(f"/support/tickets/{urgent_id}/close",
                                  headers=auth)
            check(r.status_code == 404, "closing it twice does nothing",
                  f"got {r.status_code}")

            r = await client.post(f"/support/tickets/{ticket_id}/close",
                                  headers=other_auth)
            check(r.status_code == 404,
                  "another school cannot close it", f"got {r.status_code}")

        finally:
            try:
                await db.execute(
                    "DELETE FROM timetables WHERE name LIKE $1", f"Other {RUN}")
                await db.execute("""
                    DELETE FROM timetable_layouts WHERE school_id = ANY($1::uuid[])
                """, [s for s, _ in schools])
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM schools WHERE name LIKE $1",
                    f"Support%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 13 - notifications and support\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

"""
Phase 16 - the dev portal's operational views and actions.

Everything here is cross-tenant by design, which makes the guard the whole
point: a school must never reach any of it. The two that move money - refunding
a failed generation and deciding an invoiced-billing application - are checked
hardest, including that a refund cannot be taken twice.

    python test_dev_portal.py
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
from services import billing  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.dev").setLevel(logging.CRITICAL)
logging.getLogger("klasser.billing").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []
timetables: list[str] = []
errors: list[str] = []
applications: list[str] = []


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
    email = f"p16.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Portal {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": tag.title(), "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))

    if role != "owner":
        await db.execute("UPDATE users SET role = $2 WHERE id = $1", user_id, role)

    return ({"Authorization": f"Bearer {await sign_in(email)}"},
            school_id, user_id)


async def cleanup() -> None:
    for error_id in errors:
        await db.execute("DELETE FROM error_log WHERE id = $1", error_id)
    for app_id in applications:
        await db.execute(
            "DELETE FROM invoiced_billing_applications WHERE id = $1", app_id)

    for tid in timetables:
        for sql in (
            "DELETE FROM error_log WHERE timetable_id = $1",
            "DELETE FROM credit_transactions WHERE timetable_id = $1",
            "DELETE FROM generation_cost_breakdown WHERE timetable_id = $1",
            "DELETE FROM generation_attempts WHERE timetable_id = $1",
            "DELETE FROM timetables WHERE id = $1",
        ):
            await db.execute(sql, tid)

    for school_id, user_id in schools:
        await db.execute(
            "DELETE FROM notification_preferences WHERE user_id = $1", user_id)
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM invoiced_billing_applications WHERE school_id = $1",
                "DELETE FROM credit_transactions WHERE school_id = $1",
                "DELETE FROM outstanding_charges WHERE school_id = $1",
                "DELETE FROM invoices WHERE school_id = $1",
                "DELETE FROM error_log WHERE school_id = $1",
                "DELETE FROM onboarding WHERE school_id = $1",
                "DELETE FROM school_complexity WHERE school_id = $1",
                "DELETE FROM school_billing WHERE school_id = $1",
                "DELETE FROM school_credits WHERE school_id = $1",
                "DELETE FROM timetable_layouts WHERE school_id = $1",
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
            auth, school_id, user_id = await make_school(client, "school")
            dev_auth, dev_school, dev_user = await make_school(
                client, "dev", role="dev")

            # --- A school reaches none of it -----------------------------------
            # Counted rather than a for/else: an else branch reporting a pass
            # is an assertion that cannot fail if the loop body never runs.
            guarded = []
            for path in ("/dev/generations", "/dev/logs", "/dev/errors",
                         "/dev/users", "/dev/invoices", "/dev/outstanding",
                         "/dev/applications"):
                r = await client.get(path, headers=auth)
                guarded.append((path, r.status_code))

            refused = [p for p, code in guarded if code == 403]
            check(len(refused) == len(guarded) and len(guarded) == 7,
                  "a school is refused every operational view",
                  f"{len(refused)} of {len(guarded)} refused: "
                  + str([g for g in guarded if g[1] != 403]))

            r = await client.get("/dev/generations")
            check(r.status_code == 401, "and so is an anonymous caller",
                  f"got {r.status_code}")

            # --- A failed generation to work with --------------------------------
            layout_id = await db.fetchval("""
                INSERT INTO timetable_layouts (school_id, name, days_in_cycle,
                                               is_active)
                VALUES ($1, 'L', 5, true) RETURNING id
            """, school_id)
            tid = str(await db.fetchval("""
                INSERT INTO timetables (school_id, name, layout_id, status)
                VALUES ($1, $2, $3, 'failed') RETURNING id
            """, school_id, f"Broken run {RUN}", layout_id))
            timetables.append(tid)

            await db.execute("""
                INSERT INTO generation_attempts (timetable_id, attempt_number,
                                                 status, failure_reason,
                                                 locked_model)
                VALUES ($1, 1, 'failed', 'Day 3: teacher double-booked',
                        'magistral')
            """, tid)

            # Charged, so there is something to refund.
            await billing.adjust(school_id, 500, f"Funding {RUN}",
                                 created_by="test")
            await db.execute("""
                INSERT INTO generation_cost_breakdown
                    (timetable_id, school_id, estimated_credits, actual_credits,
                     base_fee, student_cost, status, settled_at)
                VALUES ($1, $2, 120, 120, 60, 10, 'charged', now())
            """, tid, school_id)
            await db.execute("""
                UPDATE school_credits SET balance = balance - 120,
                                          lifetime_spent = lifetime_spent + 120
                WHERE school_id = $1
            """, school_id)

            error_id = str(await db.fetchval("""
                INSERT INTO error_log
                    (school_id, timetable_id, error_type, provider, stage,
                     is_dev_fault, error_message)
                VALUES ($1,$2,'ai_call','mistral','layer5_teachers',true,
                        'Provider rate limited on every key')
                RETURNING id
            """, school_id, tid))
            errors.append(error_id)

            # --- Generations view ---------------------------------------------------
            r = await client.get("/dev/generations", headers=dev_auth)
            check(r.status_code == 200, "GET generations",
                  f"HTTP {r.status_code} {r.text[:120]}")
            gens = r.json()
            mine = next((g for g in gens["generations"] if g["id"] == tid), None)
            check(mine is not None, "the failed run is listed")
            check(mine and mine["school"] == f"Portal school {RUN}",
                  "with the school it belongs to")
            check(mine and mine["last_failure"].startswith("Day 3"),
                  "and why it failed - the reason to open this page at all",
                  str(mine and mine["last_failure"]))
            check(mine and mine["attempts"] == 1, "and its attempt count")
            check(mine and mine["actual_credits"] == 120,
                  "and what it charged", str(mine and mine["actual_credits"]))
            check(gens["totals"]["failed"] >= 1, "failures are counted",
                  str(gens["totals"]["failed"]))

            r = await client.get("/dev/generations", headers=dev_auth,
                                 params={"state": "complete"})
            check(all(g["status"] == "complete" for g in r.json()["generations"]),
                  "filtering by status works")

            r = await client.get("/dev/generations", headers=dev_auth,
                                 params={"state": "nonsense"})
            check(r.status_code == 422, "an unknown status filter is refused",
                  f"got {r.status_code}")

            # --- Error log -----------------------------------------------------------
            r = await client.get("/dev/errors", headers=dev_auth)
            check(r.status_code == 200, "GET errors", f"HTTP {r.status_code}")
            found = next((e for e in r.json()["errors"] if e["id"] == error_id),
                         None)
            check(found is not None, "the error is listed")
            check(found and found["is_dev_fault"] is True,
                  "marked as our fault - the column a refund turns on")
            check(found and found["actual_credits"] == 120,
                  "with what the school was charged",
                  str(found and found["actual_credits"]))
            check(r.json()["totals"]["our_fault"] >= 1,
                  "our-fault errors are counted separately")

            r = await client.get("/dev/errors", headers=dev_auth,
                                 params={"dev_fault": "false"})
            check(all(e["is_dev_fault"] is False for e in r.json()["errors"]),
                  "filtering by fault works")

            # --- Refunding -------------------------------------------------------------
            before = (await billing.account_state(school_id))["balance"]

            r = await client.post(f"/dev/errors/{error_id}/refund", headers=auth)
            check(r.status_code == 403, "a school cannot refund itself",
                  f"got {r.status_code}")

            r = await client.post(f"/dev/errors/{error_id}/refund",
                                  headers=dev_auth)
            check(r.status_code == 200, "a dev refunds the failed run",
                  f"HTTP {r.status_code} {r.text[:120]}")
            check(r.json()["credits"] == 120, "the full charge is returned",
                  str(r.json().get("credits")))

            after = (await billing.account_state(school_id))["balance"]
            check(after - before == 120, "the balance moved by exactly that",
                  f"{before} -> {after}")

            txn = await db.fetchrow("""
                SELECT type, amount, created_by FROM credit_transactions
                WHERE timetable_id = $1 AND type = 'refund'
            """, tid)
            check(txn is not None and txn["amount"] == 120,
                  "a refund transaction records it")
            check(txn is not None and "@" in (txn["created_by"] or ""),
                  "against the dev who did it - refunds must be auditable",
                  str(txn and txn["created_by"]))

            resolved = await db.fetchval(
                "SELECT resolved FROM error_log WHERE id = $1", error_id)
            check(resolved is True, "and the error is closed")

            # The one that would hurt: paying twice.
            r = await client.post(f"/dev/errors/{error_id}/refund",
                                  headers=dev_auth)
            check(r.json().get("already_refunded") is True,
                  "a second refund is refused")
            check((await billing.account_state(school_id))["balance"] == after,
                  "and the balance is unchanged by it")

            # --- Refusing to refund what was never charged -------------------------------
            clean_tid = str(await db.fetchval("""
                INSERT INTO timetables (school_id, name, layout_id, status)
                VALUES ($1, $2, $3, 'failed') RETURNING id
            """, school_id, f"Never charged {RUN}", layout_id))
            timetables.append(clean_tid)
            clean_error = str(await db.fetchval("""
                INSERT INTO error_log
                    (school_id, timetable_id, error_type, is_dev_fault,
                     error_message)
                VALUES ($1,$2,'pipeline',true,'Failed before billing')
                RETURNING id
            """, school_id, clean_tid))
            errors.append(clean_error)

            r = await client.post(f"/dev/errors/{clean_error}/refund",
                                  headers=dev_auth)
            check(r.status_code == 422,
                  "a run that was never charged cannot be refunded",
                  f"got {r.status_code}")

            r = await client.post(f"/dev/errors/{clean_error}/resolve",
                                  headers=dev_auth)
            check(r.status_code == 200, "but it can be resolved without paying")

            # --- Logs ---------------------------------------------------------------------
            r = await client.get("/dev/logs", headers=dev_auth)
            check(r.status_code == 200, "GET logs", f"HTTP {r.status_code}")
            check("last_7_days_by_model" in r.json(),
                  "the token spend per model is reported - it exists nowhere else")

            # --- Users ---------------------------------------------------------------------
            r = await client.get("/dev/users", headers=dev_auth,
                                 params={"search": f"p16.school.{RUN}"})
            check(r.status_code == 200 and len(r.json()["users"]) == 1,
                  "searching users by email finds one",
                  str(len(r.json().get("users", []))))
            check(r.json()["users"][0]["school"] == f"Portal school {RUN}",
                  "with their school")

            r = await client.get("/dev/users", headers=dev_auth,
                                 params={"search": f"Portal school {RUN}"})
            check(len(r.json()["users"]) == 1, "and searching by school works")

            # --- Invoiced billing applications ------------------------------------------------
            r = await client.post("/billing/applications", headers=auth, json={
                "billing_contact_name": "Business Manager",
                "billing_contact_email": f"bursar.{RUN}@example.com",
                "organisation_abn": "12345678901",
                "reason": "We pay by purchase order."})
            check(r.status_code == 201, "a school applies for invoiced billing",
                  f"HTTP {r.status_code} {r.text[:120]}")
            app_id = r.json()["id"]
            applications.append(app_id)

            r = await client.post("/billing/applications", headers=auth, json={
                "billing_contact_name": "Again", "billing_contact_email": "x@y.com"})
            check(r.status_code == 409, "a second pending application is refused",
                  f"got {r.status_code}")

            r = await client.get("/dev/applications", headers=dev_auth)
            listed = next((a for a in r.json() if a["id"] == app_id), None)
            check(listed is not None, "it reaches the dev queue")
            check(listed and listed["status"] == "pending", "as pending")
            check(listed and listed["organisation_abn"] == "12345678901",
                  "with the details a business manager gave")

            # Invoiced billing must not be selectable before approval.
            r = await client.put("/billing/mode", headers=auth,
                                 json={"billing_mode": "invoiced"})
            check(r.status_code == 403,
                  "a school cannot switch to invoiced before approval",
                  f"got {r.status_code}")

            r = await client.post(f"/dev/applications/{app_id}/decide",
                                  headers=dev_auth, json={"approve": False})
            check(r.status_code == 422, "refusing without a reason is refused",
                  f"got {r.status_code}")

            r = await client.post(f"/dev/applications/{app_id}/decide",
                                  headers=auth, json={"approve": True})
            check(r.status_code == 403, "a school cannot approve its own",
                  f"got {r.status_code}")

            r = await client.post(f"/dev/applications/{app_id}/decide",
                                  headers=dev_auth, json={"approve": True})
            check(r.status_code == 200, "a dev approves it",
                  f"HTTP {r.status_code} {r.text[:120]}")

            approved = await db.fetchrow("""
                SELECT invoiced_approved, invoiced_approved_by
                FROM school_billing WHERE school_id = $1
            """, school_id)
            check(approved["invoiced_approved"] is True,
                  "the school is granted invoiced billing")
            check("@" in (approved["invoiced_approved_by"] or ""),
                  "recorded against the dev who approved it",
                  str(approved["invoiced_approved_by"]))

            r = await client.put("/billing/mode", headers=auth,
                                 json={"billing_mode": "invoiced"})
            check(r.status_code == 200,
                  "and only now can the school choose it",
                  f"got {r.status_code}")

            r = await client.post(f"/dev/applications/{app_id}/decide",
                                  headers=dev_auth, json={"approve": True})
            check(r.status_code == 409, "deciding it twice is refused",
                  f"got {r.status_code}")

            # --- Invoices and debts ------------------------------------------------------------
            r = await client.get("/dev/invoices", headers=dev_auth)
            check(r.status_code == 200 and "totals" in r.json(),
                  "GET invoices", f"HTTP {r.status_code}")

            charge_id = str(await db.fetchval("""
                INSERT INTO outstanding_charges
                    (school_id, charge_type, original_amount_cents,
                     interest_rate_daily, total_owed_cents, days_outstanding)
                VALUES ($1,'payg_failed',27600,0.20,33120,3) RETURNING id
            """, school_id))

            r = await client.get("/dev/outstanding", headers=dev_auth)
            debt = next((c for c in r.json()["charges"] if c["id"] == charge_id),
                        None)
            check(debt is not None, "an unpaid debt is listed")
            check(debt and debt["total_owed_cents"] == 33120,
                  "with the interest already on it",
                  str(debt and debt["total_owed_cents"]))
            check(r.json()["total_owed_cents"] >= 33120,
                  "and totalled across schools")

        finally:
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM schools WHERE name LIKE $1",
                    f"Portal%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 16 - dev portal\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

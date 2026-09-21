"""
Phase 12 - transactional email.

No Resend key is configured, so nothing leaves the building. That is the point
of the design being testable: every send still renders its template, honours
preferences, and records the attempt, so the whole path is exercised and only
the delivery is missing.

What this checks is everything except delivery - which is the part a test could
never check anyway.

    python test_email.py
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
from routers import notifications  # noqa: E402
from services import email  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.email").setLevel(logging.CRITICAL)
logging.getLogger("klasser.auth").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(e: str):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": e, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def cleanup() -> None:
    for school_id, user_id in schools:
        await db.execute("DELETE FROM email_log WHERE school_id = $1", school_id)
        await db.execute(
            "DELETE FROM notification_preferences WHERE user_id = $1", user_id)
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM email_log WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM onboarding WHERE school_id = $1",
                "DELETE FROM school_complexity WHERE school_id = $1",
                "DELETE FROM school_billing WHERE school_id = $1",
                "DELETE FROM school_credits WHERE school_id = $1",
                "DELETE FROM credit_transactions WHERE school_id = $1",
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
            # --- Every template renders ---------------------------------------
            problems = email.check_templates()
            check(not problems, "every template renders with sample context",
                  "; ".join(problems[:2]))
            check(len(email.SUBJECTS) >= 24,
                  "all the templates EMAIL.md lists exist",
                  f"{len(email.SUBJECTS)}")

            # A subject line built from context, not a constant.
            subject = email.subject_for("generation_complete",
                                        {"timetable_name": "Term 1 2026"})
            check("Term 1 2026" in subject,
                  "subjects are built from the email's own context", subject)

            # --- StrictUndefined catches a missing variable ----------------------
            outcome = await email.send("nobody@example.com", "welcome",
                                       {"first_name": "Sam"})
            check(outcome["sent"] is False and outcome["reason"] == "render_failed",
                  "a template missing a variable fails loudly, not silently",
                  str(outcome.get("reason")))
            check("school_name" in str(outcome.get("error", "")),
                  "and names the variable that was missing",
                  str(outcome.get("error"))[:80])

            outcome = await email.send("nobody@example.com", "not_a_template", {})
            check(outcome["reason"] == "unknown_template",
                  "an unknown template is refused")

            # --- Signup sends a welcome, and records it ---------------------------
            owner_email = f"mail.{RUN}@example.com"
            r = await client.post("/auth/signup", json={
                "school_name": f"Mail {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Sam", "surname": "Owner", "email": owner_email,
                "password": PASSWORD, "campuses": [{"name": "Main"}]})
            check(r.status_code == 201, "signup succeeds",
                  f"HTTP {r.status_code}")
            school_id, user_id = r.json()["school_id"], r.json()["user_id"]
            schools.append((school_id, user_id))
            auth = {"Authorization": f"Bearer {await sign_in(owner_email)}"}

            logged = await db.fetchrow("""
                SELECT template_name, subject, status, recipient
                FROM email_log WHERE school_id = $1 AND template_name = 'welcome'
            """, school_id)
            check(logged is not None, "a welcome email was attempted at signup")
            check(logged and logged["recipient"] == owner_email,
                  "to the right address")
            check(logged and logged["subject"] == "Welcome to Klasser",
                  "with the right subject", str(logged and logged["subject"]))
            check(logged and logged["status"] == "skipped",
                  "recorded as skipped rather than sent",
                  str(logged and logged["status"]))

            # Every suite in this project signs up @example.com addresses. RFC
            # 2606 reserves those so they can never receive mail, and the
            # provider rejects them outright - so a full test run would
            # otherwise fire a hundred real API calls and burn the daily quota.
            check(email.deliverable("someone@example.com") is False,
                  "a reserved domain is never handed to the provider")
            check(email.deliverable("head@westfield.edu.au") is True,
                  "but a real address is")
            check(email.deliverable("someone@sub.example.com") is False,
                  "including subdomains of reserved domains")

            # --- Preferences are honoured ------------------------------------------
            r = await client.put("/notifications", headers=auth, json={
                "preferences": {"notify_purchase_receipt": False}})
            check(r.status_code == 200, "turn off purchase receipts")

            before = await db.fetchval(
                "SELECT count(*) FROM email_log WHERE school_id = $1", school_id)

            outcome = await email.send_if_preferred(
                user_id, "notify_purchase_receipt", owner_email,
                "credit_purchase_receipt", {
                    "first_name": "Sam", "school_name": "Mail",
                    "package_name": "Standard", "credits": 800,
                    "price_dollars": "1,600.00", "new_balance": 800,
                    "billing_url": "https://klasser.ai/billing.html"},
                school_id=school_id)
            check(outcome["reason"] == "opted_out",
                  "an opted-out email is not sent", str(outcome))

            after = await db.fetchval(
                "SELECT count(*) FROM email_log WHERE school_id = $1", school_id)
            check(after == before,
                  "and nothing is written to the log for it", f"{before} -> {after}")

            # --- Account mail ignores preferences -----------------------------------
            r = await client.put("/notifications", headers=auth, json={
                "preferences": {"notify_account_blocked": False}})
            outcome = await email.send_if_preferred(
                user_id, "notify_account_blocked", owner_email,
                "account_blocked", {
                    "first_name": "Sam", "school_name": "Mail",
                    "blocked_reason": "Unpaid balance",
                    "outstanding_dollars": "450.80",
                    "resolve_url": "https://klasser.ai/billing.html",
                    "support_url": "https://klasser.ai/support.html"},
                school_id=school_id)
            check(outcome["reason"] != "opted_out",
                  "a school cannot opt out of being told their account is "
                  "blocked", str(outcome.get("reason")))
            check("account_blocked" in email.ALWAYS_SEND,
                  "because it is on the always-send list")

            # --- notify_school reaches the school ------------------------------------
            outcome = await email.notify_school(
                school_id, "notify_version_published", "version_published", {
                    "school_name": "Mail", "timetable_name": "Term 1",
                    "version_number": 2, "published_by": "Sam Owner",
                    "notes": "", "view_url": "https://klasser.ai/output.html"})
            check(outcome["recipients"] == 1,
                  "a school-wide email finds the school's users",
                  str(outcome["recipients"]))

            rendered = email.render("version_published", {
                "first_name": "Sam", "school_name": "Mail",
                "timetable_name": "Term 1", "version_number": 2,
                "published_by": "Sam Owner", "notes": "",
                "view_url": "https://klasser.ai/output.html",
                "app_url": "https://klasser.ai",
                "support_email": "support@klasser.ai"})
            check("Sam" in rendered and "Term 1" in rendered,
                  "the rendered email actually contains its content")
            check("<html" in rendered.lower() and "Klass" in rendered,
                  "and the shared layout")

            # Content is escaped: a school name is school-supplied text.
            escaped = email.render("welcome", {
                "first_name": "<script>alert(1)</script>",
                "school_name": "Mail", "login_url": "https://klasser.ai",
                "app_url": "https://klasser.ai",
                "support_email": "support@klasser.ai"})
            check("<script>" not in escaped,
                  "school-supplied text is escaped in the HTML",
                  escaped[escaped.find("alert") - 40:escaped.find("alert") + 10]
                  if "alert" in escaped else "")

            # --- A deactivated user stops receiving ------------------------------------
            await db.execute("UPDATE users SET is_active = false WHERE id = $1",
                             user_id)
            outcome = await email.notify_school(
                school_id, "notify_version_published", "version_published", {
                    "school_name": "Mail", "timetable_name": "Term 1",
                    "version_number": 3, "published_by": "Sam",
                    "notes": "", "view_url": "https://klasser.ai"})
            check(outcome["recipients"] == 0,
                  "a deactivated user is not emailed", str(outcome["recipients"]))
            await db.execute("UPDATE users SET is_active = true WHERE id = $1",
                             user_id)

            # --- The log answers "were they told?" -------------------------------------
            rows = await db.fetch("""
                SELECT template_name, status FROM email_log
                WHERE school_id = $1 ORDER BY created_at
            """, school_id)
            templates = [r["template_name"] for r in rows]
            check("welcome" in templates and "account_blocked" in templates,
                  "every attempt is on the record", str(sorted(set(templates))))
            check(all(r["status"] in ("sent", "skipped", "failed") for r in rows),
                  "with a status on each")

            # --- Nothing raises at the caller --------------------------------------------
            broken = await email.send(owner_email, "invoice_sent",
                                      {"first_name": "Sam"})
            check(broken["sent"] is False,
                  "a broken send reports failure rather than raising",
                  str(broken.get("reason")))

            failed = await db.fetchval("""
                SELECT count(*) FROM email_log
                WHERE school_id IS NULL AND status = 'failed'
                  AND template_name = 'invoice_sent'
            """)
            check(failed >= 1, "and the failure is recorded too", str(failed))
            await db.execute("""
                DELETE FROM email_log
                WHERE school_id IS NULL AND template_name = 'invoice_sent'
            """)

        finally:
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM schools WHERE name LIKE $1", f"Mail {RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 12 - transactional email\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

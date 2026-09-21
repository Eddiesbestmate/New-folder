"""
Phase 10 - the PAYG charge and the three scheduled billing jobs.

test_billing.py covers reserve/settle/release. This covers what happens after:
the card being charged, a decline blocking the account and opening a debt,
interest growing it daily, invoices being raised monthly, and enforcement
warning then blocking then revoking.

These are the paths that move money without anyone watching, so the cases that
matter most are the ones where a job runs twice.

    python test_payg_jobs.py
"""

import asyncio
import logging
import ssl
import sys
import uuid
from datetime import date, timedelta

import httpx

import config
import keys
from models import database as db

try:
    import truststore

    truststore.inject_into_ssl()
    SSL_CONTEXT: ssl.SSLContext | bool = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except ImportError:
    SSL_CONTEXT = True

import main  # noqa: E402
from services import billing, jobs  # noqa: E402
from services import settings as settings_service  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.billing").setLevel(logging.CRITICAL)
logging.getLogger("klasser.jobs").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

# The billing timezone, not the machine's. An earlier version used
# date.today() and failed whenever the local date and the date the code
# reckoned in disagreed - which was the bug, not the test.
TODAY = config.billing_date()

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []
timetables: list[str] = []
layouts: dict[str, str] = {}
job_dates: set[date] = set()


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(email: str):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def make_school(client, tag: str, role: str = "owner") -> tuple[dict, str]:
    email = f"p10.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Phase10 {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": "Owner", "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))

    if role != "owner":
        await db.execute("UPDATE users SET role = $2 WHERE id = $1",
                         user_id, role)

    layouts[school_id] = str(await db.fetchval("""
        INSERT INTO timetable_layouts (school_id, name, days_in_cycle, is_active)
        VALUES ($1, 'Phase 10 layout', 5, true) RETURNING id
    """, school_id))

    return {"Authorization": f"Bearer {await sign_in(email)}"}, school_id


async def make_timetable(school_id: str, name: str, *,
                         settled_on: date | None = None,
                         credits: int = 100) -> str:
    """A timetable with a settled cost, as a finished generation would leave."""
    tid = str(await db.fetchval("""
        INSERT INTO timetables (school_id, name, layout_id, status, completed_at)
        VALUES ($1, $2, $3, 'complete', now()) RETURNING id
    """, school_id, name, layouts[school_id]))
    timetables.append(tid)

    if settled_on is not None:
        await db.execute("""
            INSERT INTO generation_cost_breakdown
                (timetable_id, school_id, estimated_credits, actual_credits,
                 base_fee, student_cost, status, settled_at)
            VALUES ($1,$2,$3,$3,60,0,'charged',$4)
        """, tid, school_id, credits, settled_on)
    return tid


async def cleanup() -> None:
    for tid in timetables:
        for sql in (
            "DELETE FROM payg_charge_log WHERE timetable_id = $1",
            "DELETE FROM outstanding_charges WHERE timetable_id = $1",
            "DELETE FROM invoice_line_items WHERE timetable_id = $1",
            "DELETE FROM credit_transactions WHERE timetable_id = $1",
            "DELETE FROM generation_cost_breakdown WHERE timetable_id = $1",
            "DELETE FROM timetables WHERE id = $1",
        ):
            await db.execute(sql, tid)

    for school_id, user_id in schools:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM payg_charge_log WHERE school_id = $1",
                "DELETE FROM invoice_line_items WHERE invoice_id IN "
                "  (SELECT id FROM invoices WHERE school_id = $1)",
                "DELETE FROM outstanding_charges WHERE school_id = $1",
                "DELETE FROM invoices WHERE school_id = $1",
                "DELETE FROM credit_transactions WHERE school_id = $1",
                "DELETE FROM generation_cost_breakdown WHERE school_id = $1",
                "DELETE FROM timetables WHERE school_id = $1",
                "DELETE FROM notification_preferences WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM onboarding WHERE school_id = $1",
                "DELETE FROM school_complexity WHERE school_id = $1",
                "DELETE FROM school_billing WHERE school_id = $1",
                "DELETE FROM school_credits WHERE school_id = $1",
                "DELETE FROM layout_periods WHERE school_id = $1",
                "DELETE FROM timetable_layouts WHERE school_id = $1",
                "DELETE FROM campuses WHERE school_id = $1",
                "DELETE FROM users WHERE school_id = $1",
                "DELETE FROM schools WHERE id = $1",
            ):
                await conn.execute(sql, school_id)
        await sb.delete_auth_user(user_id)

    # The job-run rows are global, not per school, so they have to go by date.
    for when in job_dates:
        await db.execute("DELETE FROM scheduled_job_runs WHERE run_date = $1", when)


async def run_job(name: str, on_date: date, **kwargs) -> dict:
    job_dates.add(on_date)
    return await jobs.JOBS[name](on_date=on_date, **kwargs)


async def main_test() -> None:
    await db.connect()
    original_rate = await settings_service.get("fake_terminal_success_rate")
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            auth, school_id = await make_school(client, "payg")

            # --- PAYG: a successful charge ------------------------------------
            await client.put("/billing/card", headers=auth,
                             json={"raw_input": "4111 test card"})
            await client.put("/billing/mode", headers=auth,
                             json={"billing_mode": "payg"})
            await settings_service.set_value("fake_terminal_success_rate", "1.0")

            tid = await make_timetable(school_id, f"PAYG ok {RUN}")
            outcome = await billing.charge_payg(school_id, tid, 100)

            rate = await settings_service.get_int("credit_payg_rate_cents", 230)
            check(outcome["charged"] is True, "PAYG charge succeeds",
                  str(outcome))
            check(outcome["amount_cents"] == 100 * rate,
                  "charged credits x the PAYG rate",
                  f"{outcome['amount_cents']} vs {100 * rate}")

            logged = await db.fetchrow("""
                SELECT status, amount_charged_cents, fake_payment_id, charged_at
                FROM payg_charge_log WHERE timetable_id = $1
            """, tid)
            check(logged["status"] == "success", "charge logged as success")
            check(logged["fake_payment_id"] is not None,
                  "a payment id is recorded")
            check(logged["charged_at"] is not None, "charged_at is set")

            txn = await db.fetchrow("""
                SELECT type, amount, price_paid_cents FROM credit_transactions
                WHERE timetable_id = $1 AND type = 'payg_charge'
            """, tid)
            check(txn is not None, "a transaction row explains the charge")
            check(txn and txn["amount"] == 0,
                  "a PAYG charge moves money, not credits", str(txn and txn["amount"]))

            # Charging twice for one generation is the worst bug this file
            # could have - settle() sits directly in front of it and is retried.
            again = await billing.charge_payg(school_id, tid, 100)
            check(again.get("already_charged") is True,
                  "a second charge for the same generation is refused")
            count = await db.fetchval(
                "SELECT count(*) FROM payg_charge_log WHERE timetable_id = $1", tid)
            check(count == 1, "only one charge row exists", str(count))

            state = await billing.account_state(school_id)
            check(state["account_blocked"] is False,
                  "a paying school stays unblocked")

            # --- PAYG: a decline ----------------------------------------------
            await settings_service.set_value("fake_terminal_success_rate", "0.0")
            tid2 = await make_timetable(school_id, f"PAYG declined {RUN}")
            declined = await billing.charge_payg(school_id, tid2, 50)

            check(declined["charged"] is False, "declined charge reports failure")
            check(declined["blocked"] is True, "a decline blocks the account")

            state = await billing.account_state(school_id)
            check(state["account_blocked"] is True, "the school is blocked")
            check(state["outstanding_cents"] == 50 * rate,
                  "the unpaid amount becomes outstanding",
                  f"{state['outstanding_cents']}")

            debt = await db.fetchrow("""
                SELECT id, charge_type, total_owed_cents, interest_rate_daily,
                       last_interest_date, days_outstanding
                FROM outstanding_charges WHERE timetable_id = $1
            """, tid2)
            check(debt is not None, "an outstanding charge is opened")
            check(debt and debt["charge_type"] == "payg_failed",
                  "the debt says what caused it")
            check(debt and float(debt["interest_rate_daily"]) == 0.20,
                  "the PAYG interest rate is applied",
                  str(debt and debt["interest_rate_daily"]))
            # Postgres CURRENT_DATE is UTC. If the debt is stamped with that
            # while the job reckons days in the billing timezone, the school is
            # charged an extra day the first time interest runs.
            check(debt and debt["last_interest_date"] == TODAY,
                  "the debt is dated in the billing timezone, not UTC",
                  f"{debt and debt['last_interest_date']} vs {TODAY}")

            # The timetable is the school's whatever happened to the payment.
            still_there = await db.fetchval(
                "SELECT status FROM timetables WHERE id = $1", tid2)
            check(still_there == "complete",
                  "a declined charge does not take the timetable away",
                  str(still_there))

            # A blocked school cannot start another run.
            refusal = "not refused at all"
            try:
                estimate = await billing.estimate_cost(school_id)
                await billing.check_and_reserve(
                    school_id, await make_timetable(school_id, f"blocked {RUN}"),
                    estimate)
            except billing.BillingError as exc:
                refusal = exc.code
            check(refusal == "account_blocked",
                  "a blocked school cannot generate", refusal)

            # --- Daily interest -----------------------------------------------
            owed_before = debt["total_owed_cents"]
            # The charge was opened today, so interest is due from tomorrow.
            summary = await run_job("accrue_interest", TODAY + timedelta(days=1))
            check(summary["charges"] >= 1, "interest job touched the debt",
                  str(summary["charges"]))

            grown = await db.fetchrow("""
                SELECT total_owed_cents, interest_accrued_cents, days_outstanding,
                       last_interest_date
                FROM outstanding_charges WHERE id = $1
            """, debt["id"])
            expected = owed_before + round(owed_before * 0.20)
            check(grown["total_owed_cents"] == expected,
                  "one day of interest at 20%",
                  f"{grown['total_owed_cents']} vs {expected}")
            check(grown["days_outstanding"] == 1, "days outstanding counted",
                  str(grown["days_outstanding"]))

            state = await billing.account_state(school_id)
            check(state["outstanding_cents"] == grown["total_owed_cents"],
                  "the school's headline debt follows the charge",
                  f"{state['outstanding_cents']}")

            # Running twice in one day would charge a school twice for it.
            repeat = await run_job("accrue_interest", TODAY + timedelta(days=1))
            check(repeat.get("skipped") is True,
                  "the interest job refuses a second run the same day",
                  str(repeat))
            unchanged = await db.fetchval(
                "SELECT total_owed_cents FROM outstanding_charges WHERE id = $1",
                debt["id"])
            check(unchanged == grown["total_owed_cents"],
                  "the debt did not move on the second run")

            # A missed day must not be free: three days later, three days accrue.
            await run_job("accrue_interest", TODAY + timedelta(days=4))
            after_gap = await db.fetchval(
                "SELECT days_outstanding FROM outstanding_charges WHERE id = $1",
                debt["id"])
            check(after_gap == 4, "a missed day is caught up, not skipped",
                  f"{after_gap} days")

            # --- Resolving a debt ----------------------------------------------
            dev_auth = await dev_login(client)
            r = await client.post(
                f"/billing/dev/outstanding/{debt['id']}/resolve", headers=dev_auth)
            check(r.status_code == 200, "dev can resolve a debt",
                  f"HTTP {r.status_code}")
            check(r.json().get("unblocked") is True,
                  "clearing the last debt unblocks the school", str(r.json()))

            state = await billing.account_state(school_id)
            check(state["account_blocked"] is False, "the school can generate again")
            check(state["outstanding_cents"] == 0, "nothing is owed",
                  str(state["outstanding_cents"]))

            # --- Monthly invoices ----------------------------------------------
            inv_auth, inv_school = await make_school(client, "inv")
            await db.execute("""
                UPDATE school_billing
                SET billing_mode = 'invoiced', invoiced_approved = true,
                    invoiced_payment_days = NULL
                WHERE school_id = $1
            """, inv_school)

            last_month_day = TODAY.replace(day=1) - timedelta(days=5)
            await make_timetable(inv_school, f"Invoiced A {RUN}",
                                 settled_on=last_month_day, credits=120)
            await make_timetable(inv_school, f"Invoiced B {RUN}",
                                 settled_on=last_month_day, credits=80)
            # This month's run belongs on next month's invoice.
            await make_timetable(inv_school, f"This month {RUN}",
                                 settled_on=TODAY, credits=999)

            summary = await run_job("generate_invoices", TODAY)
            check(summary["invoices"] >= 1, "an invoice was raised",
                  str(summary["invoices"]))

            invoice = await db.fetchrow("""
                SELECT id, invoice_number, subtotal_credits, total_cents, status,
                       due_date, period_start, period_end
                FROM invoices WHERE school_id = $1
            """, inv_school)
            check(invoice is not None, "the invoiced school has an invoice")
            check(invoice and invoice["subtotal_credits"] == 200,
                  "only last month's generations are billed",
                  f"{invoice and invoice['subtotal_credits']} credits")

            inv_rate = await settings_service.get_int(
                "credit_invoiced_rate_cents", 230)
            check(invoice and invoice["total_cents"] == 200 * inv_rate,
                  "billed at the invoiced rate",
                  f"{invoice and invoice['total_cents']}")
            check(invoice and invoice["status"] == "sent",
                  "the invoice is issued, not left as a draft")

            lines = await db.fetchval(
                "SELECT count(*) FROM invoice_line_items WHERE invoice_id = $1",
                invoice["id"])
            check(lines == 2, "one line per generation", str(lines))

            check(invoice["invoice_number"].startswith("KL-"),
                  "the invoice has a readable number", invoice["invoice_number"])

            # Two invoices for one month bills the school twice.
            repeat = await run_job("generate_invoices", TODAY)
            check(repeat.get("skipped") is True,
                  "the invoice job refuses a second run the same day")
            total = await db.fetchval(
                "SELECT count(*) FROM invoices WHERE school_id = $1", inv_school)
            check(total == 1, "still only one invoice", str(total))

            # Even forced, the period is already invoiced.
            forced = await run_job("generate_invoices", TODAY, force=True)
            total = await db.fetchval(
                "SELECT count(*) FROM invoices WHERE school_id = $1", inv_school)
            check(total == 1,
                  "forcing a re-run does not double-bill the same period",
                  f"{total} invoices, job said {forced.get('invoices')}")

            # A school with no generations gets no invoice at all.
            quiet_auth, quiet_school = await make_school(client, "quiet")
            await db.execute("""
                UPDATE school_billing
                SET billing_mode = 'invoiced', invoiced_approved = true
                WHERE school_id = $1
            """, quiet_school)
            await run_job("generate_invoices", TODAY + timedelta(days=1))
            none = await db.fetchval(
                "SELECT count(*) FROM invoices WHERE school_id = $1", quiet_school)
            check(none == 0, "a school that generated nothing is not invoiced",
                  str(none))

            # --- Invoice enforcement --------------------------------------------
            due = invoice["due_date"]

            # Day 0: the due date itself is a warning, not a penalty.
            summary = await run_job("enforce_invoices", due)
            check(summary["warned"] >= 1, "due date sends a warning",
                  str(summary["warned"]))
            check(any(e["school_id"] == inv_school
                      # Renamed from "invoice_warning" in Phase 12: the
                      # template that actually exists is invoice_reminder.
                      and e["template"] == "invoice_reminder"
                      for e in summary["emails"]),
                  "this school is the one warned")
            blocked = await db.fetchval(
                "SELECT account_blocked FROM school_billing WHERE school_id = $1",
                inv_school)
            check(blocked is False,
                  "nothing is blocked on the due date itself")

            # Day 1: blocked, and the debt starts.
            summary = await run_job("enforce_invoices", due + timedelta(days=1))
            check(summary["blocked"] >= 1, "one day overdue blocks the account",
                  str(summary["blocked"]))
            state = await billing.account_state(inv_school)
            check(state["account_blocked"] is True, "the invoiced school is blocked")
            check(state["blocked_reason"] and "overdue" in state["blocked_reason"],
                  "the reason names the invoice", str(state["blocked_reason"]))

            charge = await db.fetchrow("""
                SELECT charge_type, total_owed_cents, interest_rate_daily
                FROM outstanding_charges WHERE invoice_id = $1
            """, invoice["id"])
            check(charge is not None, "an overdue invoice becomes a debt")
            check(charge and float(charge["interest_rate_daily"]) == 0.10,
                  "invoice interest is 10% a day, not the PAYG rate",
                  str(charge and charge["interest_rate_daily"]))
            check(charge and charge["total_owed_cents"] == invoice["total_cents"],
                  "the debt starts at the invoice total")

            # Running enforcement again must not open a second debt.
            await run_job("enforce_invoices", due + timedelta(days=2))
            debts = await db.fetchval(
                "SELECT count(*) FROM outstanding_charges WHERE invoice_id = $1",
                invoice["id"])
            check(debts == 1, "one debt per invoice however often it runs",
                  str(debts))

            # Day 11: invoiced billing is withdrawn.
            revoke_after = await settings_service.get_int("invoice_revoke_days", 10)
            block_days = await settings_service.get_int("invoice_block_days", 1)
            summary = await run_job(
                "enforce_invoices", due + timedelta(days=block_days + revoke_after))
            check(summary["revoked"] >= 1, "invoiced billing is revoked",
                  str(summary["revoked"]))

            after = await db.fetchrow("""
                SELECT billing_mode, invoiced_approved, invoiced_revoked,
                       invoiced_revoked_reason
                FROM school_billing WHERE school_id = $1
            """, inv_school)
            check(after["billing_mode"] == "credits",
                  "the school falls back to prepaid credits",
                  after["billing_mode"])
            check(after["invoiced_revoked"] is True, "the revocation is recorded")
            check(after["invoiced_approved"] is False,
                  "approval is withdrawn, so it cannot simply be re-selected")

            still_owed = await db.fetchval(
                "SELECT total_owed_cents FROM outstanding_charges WHERE invoice_id = $1",
                invoice["id"])
            check(still_owed > 0, "the debt survives losing invoiced billing",
                  str(still_owed))

            # A paid invoice is left alone.
            await db.execute("""
                UPDATE invoices SET status = 'paid', paid_at = now() WHERE id = $1
            """, invoice["id"])
            before_days = await db.fetchval(
                "SELECT days_overdue FROM invoices WHERE id = $1", invoice["id"])
            await run_job("enforce_invoices", due + timedelta(days=30))
            after_days = await db.fetchval(
                "SELECT days_overdue FROM invoices WHERE id = $1", invoice["id"])
            check(after_days == before_days, "a paid invoice is not chased",
                  f"{before_days} -> {after_days}")

            # --- Dev endpoints ---------------------------------------------------
            r = await client.get("/billing/dev/jobs", headers=dev_auth)
            check(r.status_code == 200, "GET dev job history",
                  f"HTTP {r.status_code}")
            check(set(r.json()["known"]) == set(jobs.JOBS),
                  "every job is listed", str(r.json()["known"]))
            check(len(r.json()["history"]) > 0, "runs are recorded in history")

            r = await client.post("/billing/dev/jobs/not_a_job", headers=dev_auth)
            check(r.status_code == 404, "an unknown job is refused",
                  f"got {r.status_code}")

            r = await client.post("/billing/dev/jobs/accrue_interest",
                                  headers=auth)
            check(r.status_code == 403, "a school owner cannot run a billing job",
                  f"got {r.status_code}")

            # --- School-facing endpoints ------------------------------------------
            r = await client.get("/billing/invoices", headers=inv_auth)
            check(r.status_code == 200 and len(r.json()) == 1,
                  "a school sees its own invoice", f"HTTP {r.status_code}")

            r = await client.get(f"/billing/invoices/{invoice['id']}",
                                 headers=inv_auth)
            check(r.status_code == 200, "GET invoice detail", f"HTTP {r.status_code}")
            check(len(r.json()["lines"]) == 2, "the detail lists each generation",
                  str(len(r.json()["lines"])))

            r = await client.get(f"/billing/invoices/{invoice['id']}",
                                 headers=quiet_auth)
            check(r.status_code == 404,
                  "another school cannot read the invoice", f"got {r.status_code}")

            r = await client.get("/billing/outstanding", headers=inv_auth)
            check(r.status_code == 200, "GET outstanding", f"HTTP {r.status_code}")
            check(r.json()["total_owed_cents"] > 0, "the debt is reported")
            check(r.json()["account_blocked"] is True, "and the block with it")

            r = await client.get("/billing/outstanding", headers=quiet_auth)
            check(r.json()["charges"] == [],
                  "a school with no debts sees none")

            # --- Mode guard --------------------------------------------------------
            # Credits schools are never charged after a run.
            cr_auth, cr_school = await make_school(client, "credits")
            cr_tid = await make_timetable(cr_school, f"Credits {RUN}")
            result = await billing.charge_payg(cr_school, cr_tid, 100)
            check(result["charged"] is False and "credits" in result["reason"],
                  "a credits school is not charged a card", str(result))
            rows = await db.fetchval(
                "SELECT count(*) FROM payg_charge_log WHERE timetable_id = $1",
                cr_tid)
            check(rows == 0, "and nothing is logged against it", str(rows))

        finally:
            if original_rate is not None:
                await settings_service.set_value("fake_terminal_success_rate",
                                                 original_rate)
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM schools WHERE name LIKE $1",
                    f"Phase10%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


async def dev_login(client) -> dict:
    """A dev user, needed for the job and debt-resolution endpoints."""
    email = f"p10.dev.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Phase10 dev {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Dev", "surname": "User", "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))
    await db.execute("UPDATE users SET role = 'dev' WHERE id = $1", user_id)
    return {"Authorization": f"Bearer {await sign_in(email)}"}


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 10 - pay as you go and the billing jobs\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

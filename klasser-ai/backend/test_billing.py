"""
Phase 10 billing tests.

The important cases are the ones that cost money if wrong: that a school cannot
generate without credits, that concurrent generations cannot spend the same
credits twice, that a failed run is not charged, and that the transaction log
always explains the balance.

    python test_billing.py
"""

import asyncio
import json
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
from services import billing, fake_terminal  # noqa: E402
from services import settings as settings_service  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.billing").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []
timetables: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


async def sign_in(email: str):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def cleanup() -> None:
    for tid in timetables:
        await db.execute(
            "DELETE FROM generation_cost_breakdown WHERE timetable_id = $1", tid)
        await db.execute(
            "DELETE FROM credit_transactions WHERE timetable_id = $1", tid)
        await db.execute("DELETE FROM timetables WHERE id = $1", tid)

    for school_id, user_id in schools:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM credit_transactions WHERE school_id = $1",
                "DELETE FROM generation_cost_breakdown WHERE school_id = $1",
                "DELETE FROM timetables WHERE school_id = $1",
                "DELETE FROM notification_preferences WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM student_subjects WHERE school_id = $1",
                "DELETE FROM students WHERE school_id = $1",
                "DELETE FROM teachers WHERE school_id = $1",
                "DELETE FROM rooms WHERE school_id = $1",
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


layouts: dict[str, str] = {}


async def make_school(client, tag: str) -> tuple[dict, str, str]:
    email = f"billing.{tag}.{RUN}@example.com"
    r = await client.post("/auth/signup", json={
        "school_name": f"Billing {tag} {RUN}", "timezone": "Australia/Sydney",
        "first_name": "Test", "surname": "Owner", "email": email,
        "password": PASSWORD, "campuses": [{"name": "Main"}]})
    school_id, user_id = r.json()["school_id"], r.json()["user_id"]
    schools.append((school_id, user_id))

    # timetables.layout_id is NOT NULL, so every school needs a layout before a
    # timetable row can exist.
    layouts[school_id] = str(await db.fetchval("""
        INSERT INTO timetable_layouts (school_id, name, days_in_cycle, is_active)
        VALUES ($1, 'Billing test layout', 5, true) RETURNING id
    """, school_id))

    token = await sign_in(email)
    return {"Authorization": f"Bearer {token}"}, school_id, user_id


async def seed_minimal(school_id: str) -> None:
    """
    The least data that clears pre-validation.

    Without this, /allocation/start refuses for missing teachers before it ever
    looks at the balance, and a billing test would be asserting the wrong
    refusal.
    """
    campus_id = await db.fetchval(
        "SELECT id FROM campuses WHERE school_id = $1 LIMIT 1", school_id)
    layout_id = layouts[school_id]

    await db.execute("""
        INSERT INTO layout_periods
            (layout_id, school_id, day_number, period_number, period_type,
             start_time, end_time, label)
        SELECT $1, $2, d, p, 'teaching',
               make_time(8 + p, 0, 0), make_time(8 + p, 50, 0), 'P' || p
        FROM generate_series(1, 5) AS d, generate_series(1, 4) AS p
        ON CONFLICT DO NOTHING
    """, layout_id, school_id)

    await db.execute("""
        INSERT INTO teachers (school_id, first_name, surname, subjects,
                              campus_id, max_blocks)
        VALUES ($1, 'Test', 'Teacher', ARRAY['English'], $2, 5)
    """, school_id, campus_id)

    await db.execute("""
        INSERT INTO rooms (school_id, campus_id, name, capacity, room_type)
        VALUES ($1, $2, 'R1', 30, 'classroom')
    """, school_id, campus_id)

    student_id = await db.fetchval("""
        INSERT INTO students (school_id, full_name, year_group, campus_id)
        VALUES ($1, 'Test Student', 'Year 11', $2) RETURNING id
    """, school_id, campus_id)
    await db.execute("""
        INSERT INTO student_subjects (student_id, school_id, subject)
        VALUES ($1, $2, 'English')
    """, student_id, school_id)


async def make_timetable(school_id: str, name: str) -> str:
    tid = str(await db.fetchval("""
        INSERT INTO timetables (school_id, name, layout_id, status)
        VALUES ($1, $2, $3, 'processing') RETURNING id
    """, school_id, name, layouts[school_id]))
    timetables.append(tid)
    return tid


async def main_test() -> None:
    await db.connect()
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            auth, school_id, user_id = await make_school(client, "a")

            # --- Fake terminal ------------------------------------------------
            card = fake_terminal.process_fake_card("anything at all")
            check(fake_terminal.luhn_valid(card["_number"]),
                  "generated card passes the Luhn check", card["_number"][-4:])
            check(card["brand"] in ("Visa", "Mastercard", "Amex"),
                  "card has a real-looking brand", card["brand"])
            same = fake_terminal.process_fake_card("anything at all")
            check(same["_number"] == card["_number"],
                  "same input gives the same card")
            other = fake_terminal.process_fake_card("something else")
            check(other["_number"] != card["_number"],
                  "different input gives a different card")
            check(card["masked"].endswith(card["last_four"])
                  and "*" in card["masked"], "masked number hides the body",
                  card["masked"])

            check(fake_terminal.charge("x", 100, 1.0)["success"],
                  "success rate 1.0 always succeeds")
            check(not fake_terminal.charge("x", 100, 0.0)["success"],
                  "success rate 0.0 always declines")

            # --- Account state -------------------------------------------------
            r = await client.get("/billing/account", headers=auth)
            check(r.status_code == 200, "GET account", f"HTTP {r.status_code}")
            acct = r.json()
            check(acct["balance"] == 0, "new school starts at zero")
            check(acct["billing_mode"] == "credits", "default mode is credits")
            check(acct["generations_remaining"] == 0, "no generations affordable")

            # --- Generation is blocked without credits ---------------------------
            # The school needs enough data to clear pre-validation first,
            # otherwise the request is refused for missing teachers and the
            # billing check is never reached - which is what an earlier version
            # of this test accidentally asserted.
            await seed_minimal(school_id)

            r = await client.post("/allocation/start", headers=auth,
                                  json={"name": "Should be refused"})
            check(r.status_code == 402,
                  "generation refused for want of credits, not data",
                  f"got {r.status_code}: {r.text[:120]}")
            if r.status_code == 402:
                check(r.json()["detail"]["code"] == "insufficient_credits",
                      "refusal names the reason",
                      r.json()["detail"].get("code"))
                check(r.json()["detail"]["shortfall"] > 0,
                      "refusal says how short they are",
                      str(r.json()["detail"].get("shortfall")))

            left = await db.fetchval(
                "SELECT count(*) FROM timetables WHERE school_id = $1", school_id)
            check(left == 0, "refused generation leaves no timetable row",
                  str(left))

            # --- Purchase --------------------------------------------------------
            r = await client.get("/billing/packages", headers=auth)
            check(r.status_code == 200 and len(r.json()["packages"]) == 5,
                  "five packages offered")
            trial = next(p for p in r.json()["packages"] if p["name"] == "trial")
            check(trial["available"], "trial available to a new school")

            r = await client.post("/billing/purchase", headers=auth,
                                  json={"package": "trial", "raw_input": "4242"})
            check(r.status_code == 200, "purchase trial", f"HTTP {r.status_code}")
            check(r.json()["credits_added"] == 300, "300 credits added",
                  str(r.json().get("credits_added")))
            check(r.json()["simulated"] is True, "purchase marked simulated")

            r = await client.post("/billing/purchase", headers=auth,
                                  json={"package": "trial", "raw_input": "4242"})
            check(r.status_code == 409, "trial cannot be bought twice",
                  f"got {r.status_code}")

            # --- Declined purchase -----------------------------------------------
            await settings_service.set_value("fake_terminal_success_rate", "0.0")
            settings_service.invalidate("fake_terminal_success_rate")
            before = (await client.get("/billing/account", headers=auth)).json()
            r = await client.post("/billing/purchase", headers=auth,
                                  json={"package": "standard", "raw_input": "1"})
            check(r.status_code == 402, "declined card returns 402",
                  f"got {r.status_code}")
            after = (await client.get("/billing/account", headers=auth)).json()
            check(after["balance"] == before["balance"],
                  "declined purchase adds no credits",
                  f"{before['balance']} -> {after['balance']}")
            await settings_service.set_value("fake_terminal_success_rate", "1.0")
            settings_service.invalidate("fake_terminal_success_rate")

            # --- Reserve and settle ------------------------------------------------
            estimate = await billing.estimate_cost(school_id)
            check(estimate.total > 0, "estimate calculated", str(estimate.total))

            tid = await make_timetable(school_id, f"Billing test {RUN}")

            await billing.check_and_reserve(school_id, tid, estimate)
            state = await billing.account_state(school_id)
            check(state["reserved"] == estimate.total, "credits held",
                  str(state["reserved"]))
            check(state["available"] == 300 - estimate.total,
                  "available reduced by the hold", str(state["available"]))
            check(state["balance"] == 300, "balance untouched until settled",
                  str(state["balance"]))

            await billing.settle(school_id, tid, estimate.total)
            state = await billing.account_state(school_id)
            check(state["balance"] == 300 - estimate.total, "balance charged",
                  str(state["balance"]))
            check(state["reserved"] == 0, "hold released", str(state["reserved"]))

            settled_twice = await billing.settle(school_id, tid, estimate.total)
            check(settled_twice.get("already_settled") is True,
                  "settling twice is refused")
            state2 = await billing.account_state(school_id)
            check(state2["balance"] == state["balance"],
                  "double settle does not double charge", str(state2["balance"]))

            # --- Retry surcharge ----------------------------------------------------
            tid2 = await make_timetable(school_id, f"Billing retry {RUN}")
            await billing.check_and_reserve(school_id, tid2, estimate)
            before_balance = (await billing.account_state(school_id))["balance"]
            outcome = await billing.settle(school_id, tid2, estimate.total,
                                           school_retries=2, dev_retries=3)
            rate = await settings_service.get_int("credit_school_retry", 16)
            check(outcome["surcharge"] == 2 * rate,
                  "school retries charged", str(outcome["surcharge"]))
            after_balance = (await billing.account_state(school_id))["balance"]
            check(before_balance - after_balance == estimate.total + 2 * rate,
                  "dev retries not charged",
                  f"{before_balance} -> {after_balance}")

            # --- Failed generation releases the hold ----------------------------------
            tid3 = await make_timetable(school_id, f"Billing fail {RUN}")
            await billing.check_and_reserve(school_id, tid3, estimate)
            balance_before = (await billing.account_state(school_id))["balance"]
            await billing.release(school_id, tid3, "validation failed")
            state = await billing.account_state(school_id)
            check(state["reserved"] == 0, "hold released on failure",
                  str(state["reserved"]))
            check(state["balance"] == balance_before,
                  "failed generation is not charged", str(state["balance"]))

            # --- Concurrency: two generations cannot spend the same credits ------------
            auth_b, school_b, _ = await make_school(client, "b")
            big = await billing.estimate_cost(school_b)

            # Fund exactly one run. Any more and both reservations legitimately
            # succeed and the race is never tested - which is what happened when
            # this was funded with a round 200 credits against a 60-credit run.
            await billing.adjust(school_b, big.total, "fund exactly one run", "test")

            t_a = await make_timetable(school_b, "Race A")
            t_b = await make_timetable(school_b, "Race B")

            outcomes = await asyncio.gather(
                billing.check_and_reserve(school_b, t_a, big),
                billing.check_and_reserve(school_b, t_b, big),
                return_exceptions=True)
            succeeded = [o for o in outcomes if not isinstance(o, Exception)]
            refused = [o for o in outcomes
                       if isinstance(o, billing.InsufficientCredits)]
            other = [o for o in outcomes
                     if isinstance(o, Exception)
                     and not isinstance(o, billing.InsufficientCredits)]

            check(not other, "no unexpected errors in the race",
                  "; ".join(f"{type(o).__name__}: {o}" for o in other))
            check(len(succeeded) == 1 and len(refused) == 1,
                  "two concurrent reservations, only one wins",
                  f"{len(succeeded)} succeeded, {len(refused)} refused")

            state = await billing.account_state(school_b)
            check(state["reserved"] == big.total,
                  "exactly one run's credits are held",
                  f"{state['reserved']} of {big.total}")
            check(state["available"] == 0, "nothing left available",
                  str(state["available"]))

            state = await billing.account_state(school_b)
            check(state["reserved"] <= state["balance"],
                  "reserved never exceeds balance",
                  f"{state['reserved']} of {state['balance']}")

            # --- Balance floor ----------------------------------------------------------
            outcome = await billing.adjust(school_b, -100000, "over-deduct", "test")
            check(outcome["balance"] == 0, "balance cannot go negative",
                  str(outcome["balance"]))
            check(outcome["applied"] > -100000, "over-deduction is clamped",
                  str(outcome["applied"]))

            # --- Transaction log ----------------------------------------------------------
            r = await client.get("/billing/transactions", headers=auth)
            check(r.status_code == 200, "GET transactions")
            kinds = [t["type"] for t in r.json()]
            check("purchase" in kinds, "purchase logged")
            check("generation" in kinds, "generation logged")
            check("hold_released" in kinds, "released hold logged")

            # The most recent transaction that actually moved credits must agree
            # with the live balance. Accepting "any of the last three" - as this
            # did - would pass even if the log had drifted.
            actual = await db.fetchval(
                "SELECT balance FROM school_credits WHERE school_id = $1", school_id)
            moved = [t for t in r.json() if t["amount"] != 0]
            check(bool(moved), "at least one balance-changing transaction logged")
            if moved:
                check(moved[0]["balance_after"] == actual,
                      "latest transaction matches the live balance",
                      f"log {moved[0]['balance_after']} vs actual {actual}")

            # --- Card and mode -------------------------------------------------------------
            r = await client.put("/billing/card", headers=auth,
                                 json={"raw_input": "whatever the user types"})
            check(r.status_code == 200, "save card", f"HTTP {r.status_code}")
            check("_number" not in r.json() and "number" not in r.json(),
                  "card number never returned", str(list(r.json())))

            r = await client.put("/billing/mode", headers=auth,
                                 json={"billing_mode": "payg"})
            check(r.status_code == 200, "switch to payg with a card saved")

            r = await client.delete("/billing/card", headers=auth)
            check(r.status_code == 409, "cannot remove the card while on payg",
                  f"got {r.status_code}")

            r = await client.put("/billing/mode", headers=auth,
                                 json={"billing_mode": "invoiced"})
            check(r.status_code == 403, "invoiced needs approval",
                  f"got {r.status_code}")

            # PAYG does not require a positive balance.
            await db.execute(
                "UPDATE school_credits SET balance = 0 WHERE school_id = $1",
                school_id)
            tid4 = await make_timetable(school_id, "PAYG run")
            reservation = await billing.check_and_reserve(school_id, tid4, estimate)
            check(reservation["reserved"] == 0,
                  "payg holds nothing up front", str(reservation["reserved"]))

            # --- Dev endpoints -----------------------------------------------------------------
            r = await client.post("/billing/dev/adjust", headers=auth,
                                  json={"school_id": school_id, "amount": 50,
                                        "note": "test"})
            check(r.status_code == 403, "owner cannot use dev adjust",
                  f"got {r.status_code}")

            await db.execute("UPDATE users SET role = 'dev' WHERE id = $1", user_id)
            sb.forget_token(auth["Authorization"].split()[1])
            r = await client.post("/billing/dev/adjust", headers=auth,
                                  json={"school_id": school_id, "amount": 500,
                                        "note": "manual top-up for testing"})
            check(r.status_code == 200 and r.json()["balance"] == 500,
                  "dev can adjust credits", str(r.json().get("balance")))

            logged = await db.fetchrow("""
                SELECT created_by, note FROM credit_transactions
                WHERE school_id = $1 AND type = 'manual_adjustment'
                ORDER BY created_at DESC LIMIT 1
            """, school_id)
            check(logged and "@" in (logged["created_by"] or ""),
                  "adjustment records who made it",
                  logged["created_by"] if logged else "")

        finally:
            # A cleanup failure must not swallow the results: an exception here
            # would propagate out of main_test and report() would never run, so
            # a passing suite would look like a crash.
            try:
                await settings_service.set_value("fake_terminal_success_rate", "1.0")
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM schools WHERE name LIKE $1",
                    f"Billing%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 10 - billing\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

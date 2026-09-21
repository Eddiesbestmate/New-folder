"""
Phase 11 - versioning and AI-assisted editing.

Generates two real versions against the sandbox school with a stubbed AI, then
exercises publish, rollback, discard, compare, and the whole edit cycle.

The cases that matter are the ones where the AI is wrong or hostile: a field it
should not touch, an entry from another version, an invented UUID, and a change
that is legal in isolation but creates a clash. None of those may reach the
database.

    python test_editing.py
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
from services import ai_cluster, billing, editing, pipeline  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.pipeline").setLevel(logging.CRITICAL)
logging.getLogger("klasser.deterministic").setLevel(logging.CRITICAL)
logging.getLogger("klasser.editing").setLevel(logging.CRITICAL)
logging.getLogger("klasser.billing").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
timetables: list[str] = []
schools: list[tuple[str, str]] = []

# The sandbox school is shared with other suites, so anything this one changes
# on it has to be put back. Credits are the one that bites: leaving a balance
# behind makes test_output's "cannot afford" check pass for the wrong reason.
sandbox_credits: dict[str, int] = {}


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


class StubAI:
    """The pipeline's AI. Deterministic paths do the real work."""

    # What the edit interpreter returns next. Set by each test.
    edit_reply: dict | str = {}

    @classmethod
    async def call(cls, task_key, prompt, **kwargs):
        if task_key == "task_edit_interpret":
            if isinstance(cls.edit_reply, str):
                return cls.edit_reply
            return json.dumps(cls.edit_reply)
        if task_key.startswith("validator_"):
            return json.dumps({"result": "pass", "reason": None,
                               "confidence": "high"})
        if "subject_map" in prompt:
            return json.dumps({"subject_map": []})
        if '"groups"' in prompt:
            return json.dumps({"groups": []})
        return json.dumps({"assignments": {}})


async def sign_in(email: str):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def cleanup() -> None:
    if sandbox_credits:
        sandbox = await db.fetchval(
            "SELECT value FROM settings WHERE key = 'sandbox_school_id'")
        await db.execute("""
            UPDATE school_credits
            SET balance = $2, reserved = $3, lifetime_purchased = $4,
                lifetime_spent = $5, updated_at = now()
            WHERE school_id = $1
        """, sandbox, sandbox_credits["balance"], sandbox_credits["reserved"],
            sandbox_credits["lifetime_purchased"],
            sandbox_credits["lifetime_spent"])
        await db.execute("""
            DELETE FROM credit_transactions
            WHERE school_id = $1 AND (note LIKE $2 OR type = 'edit_session')
        """, sandbox, f"%{RUN}%")

    for tid in timetables:
        for a in await db.fetch(
                "SELECT id FROM generation_attempts WHERE timetable_id = $1", tid):
            aid = a["id"]
            for sql in (
                "DELETE FROM timetable_entry_students WHERE timetable_entry_id IN "
                "  (SELECT id FROM timetable_entries WHERE attempt_id = $1)",
                "DELETE FROM timetable_entries WHERE attempt_id = $1",
                "DELETE FROM duty_assignments WHERE attempt_id = $1",
                "DELETE FROM class_group_slots WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_students WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_teachers WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_rooms WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_duties WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_duty_counts WHERE attempt_id = $1",
                "DELETE FROM class_consistency_registry WHERE attempt_id = $1",
                "DELETE FROM class_group_students WHERE class_group_id IN "
                "  (SELECT id FROM class_groups WHERE attempt_id = $1)",
                "DELETE FROM class_groups WHERE attempt_id = $1",
                "DELETE FROM generation_events WHERE attempt_id = $1",
                "DELETE FROM pipeline_state WHERE attempt_id = $1",
                "DELETE FROM generation_jobs WHERE attempt_id = $1",
                "DELETE FROM pipeline_log WHERE attempt_id = $1",
                "DELETE FROM error_log WHERE attempt_id = $1",
                "DELETE FROM validation_results WHERE attempt_id = $1",
                "DELETE FROM partial_solutions WHERE attempt_id = $1",
            ):
                await db.execute(sql, aid)
        for sql in (
            "DELETE FROM timetable_edits WHERE version_id IN "
            "  (SELECT id FROM timetable_versions WHERE timetable_id = $1)",
            "DELETE FROM transport_schedule WHERE timetable_id = $1",
            "DELETE FROM credit_transactions WHERE timetable_id = $1",
            "DELETE FROM generation_cost_breakdown WHERE timetable_id = $1",
            "DELETE FROM timetable_versions WHERE timetable_id = $1",
            "DELETE FROM generation_attempts WHERE timetable_id = $1",
            "DELETE FROM timetables WHERE id = $1",
        ):
            await db.execute(sql, tid)

    for school_id, user_id in schools:
        # By id, not by school. This suite moves its user onto the sandbox
        # school, so deleting "users of the throwaway school" misses them - and
        # because users.id references auth.users(id), the leftover row then
        # makes the Supabase auth delete fail with a 500 and the account is
        # stranded too.
        await db.execute(
            "DELETE FROM notification_preferences WHERE user_id = $1", user_id)
        await db.execute(
            "DELETE FROM timetable_edits WHERE requested_by = $1", user_id)
        await db.execute("DELETE FROM users WHERE id = $1", user_id)

        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM credit_transactions WHERE school_id = $1",
                "DELETE FROM notification_preferences WHERE user_id IN "
                "  (SELECT id FROM users WHERE school_id = $1)",
                "DELETE FROM timetable_edits WHERE school_id = $1",
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


async def generate(school_id: str, name: str,
                   timetable_id: str | None = None) -> tuple[str, str]:
    """
    One real generation. Returns (timetable_id, version_id).

    Passing an existing `timetable_id` adds another version to it. Publishing is
    scoped to one timetable - a school can run several (different terms, say),
    each with its own live version - so testing the one-published invariant
    means two versions of the *same* timetable, not two timetables.
    """
    if timetable_id is None:
        layout_id = await db.fetchval("""
            SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
            ORDER BY created_at DESC LIMIT 1
        """, school_id)
        timetable_id = str(await db.fetchval("""
            INSERT INTO timetables (school_id, name, layout_id, status)
            VALUES ($1, $2, $3, 'processing') RETURNING id
        """, school_id, name, layout_id))
        timetables.append(timetable_id)

    summary = await pipeline.run_with_retries(timetable_id, "edit@example.com")
    return timetable_id, summary["version_id"]


async def an_entry(version_id: str, offset: int = 0) -> dict:
    row = await db.fetchrow("""
        SELECT te.id, te.class_code, te.day_number, te.layout_period_id,
               te.teacher_id, te.room_id, lp.period_number
        FROM timetable_entries te
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        WHERE te.version_id = $1
        ORDER BY te.class_code, te.day_number, lp.period_number
        OFFSET $2 LIMIT 1
    """, version_id, offset)
    return dict(row)


async def main_test() -> None:
    await db.connect()
    original = ai_cluster.call
    ai_cluster.call = StubAI.call  # type: ignore[assignment]
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            school_id = await db.fetchval(
                "SELECT value FROM settings WHERE key = 'sandbox_school_id'")

            email = f"edit.{RUN}@example.com"
            r = await client.post("/auth/signup", json={
                "school_name": f"Edit Test {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Test", "surname": "Owner", "email": email,
                "password": PASSWORD, "campuses": [{"name": "Main"}]})
            throwaway, user_id = r.json()["school_id"], r.json()["user_id"]
            schools.append((throwaway, user_id))
            await db.execute("UPDATE users SET school_id = $2 WHERE id = $1",
                             user_id, school_id)
            auth = {"Authorization": f"Bearer {await sign_in(email)}"}

            # A separate school, to prove one cannot touch the other's edits.
            other_email = f"edit.other.{RUN}@example.com"
            r = await client.post("/auth/signup", json={
                "school_name": f"Edit Other {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Other", "surname": "Owner", "email": other_email,
                "password": PASSWORD, "campuses": [{"name": "Main"}]})
            schools.append((r.json()["school_id"], r.json()["user_id"]))
            other_auth = {"Authorization": f"Bearer {await sign_in(other_email)}"}

            # Credits, so applying an edit is affordable. The original balance
            # is put back in cleanup.
            sandbox_credits.update(dict(await db.fetchrow("""
                SELECT balance, reserved, lifetime_purchased, lifetime_spent
                FROM school_credits WHERE school_id = $1
            """, school_id)))
            await billing.adjust(school_id, 500, f"Edit test {RUN}",
                                 created_by="test")

            tid, v1 = await generate(school_id, f"Edit test A {RUN}")
            check(bool(v1), "first version generated")

            # --- Publishing ---------------------------------------------------
            r = await client.post(f"/timetables/version/{v1}/publish",
                                  headers=auth, params={"notes": "First"})
            check(r.status_code == 200, "publish a draft", f"HTTP {r.status_code}")
            check(r.json()["was_rollback"] is False, "a draft publish is not a rollback")

            status_now = await db.fetchval(
                "SELECT status FROM timetable_versions WHERE id = $1", v1)
            check(status_now == "published", "version is published", str(status_now))

            r = await client.post(f"/timetables/version/{v1}/publish", headers=auth)
            check(r.json().get("unchanged") is True,
                  "republishing the live version changes nothing")

            # --- A second version, and the one-published invariant -------------
            _, v2 = await generate(school_id, f"Edit test A {RUN}", tid)
            r = await client.post(f"/timetables/version/{v2}/publish", headers=auth)
            check(r.status_code == 200, "publish the second version")

            live = await db.fetch("""
                SELECT id FROM timetable_versions
                WHERE timetable_id = $1 AND status = 'published'
            """, tid)
            check(len(live) == 1, "only one version of a timetable is live",
                  f"{len(live)} published")
            check(str(live[0]["id"]) == v2, "and it is the newer one")
            check(await db.fetchval(
                "SELECT status FROM timetable_versions WHERE id = $1", v1)
                == "archived", "the previous version is archived")

            # --- Rollback -------------------------------------------------------
            r = await client.post(f"/timetables/version/{v1}/publish", headers=auth)
            check(r.status_code == 200, "an archived version can be republished")
            check(r.json()["was_rollback"] is True, "and it is reported as a rollback")
            check(await db.fetchval(
                "SELECT status FROM timetable_versions WHERE id = $1", v2)
                == "archived", "the rolled-back-over version is archived")

            # --- Compare ---------------------------------------------------------
            r = await client.get(f"/timetables/version/{v1}/compare/{v2}",
                                 headers=auth)
            check(r.status_code == 200, "GET compare", f"HTTP {r.status_code}")
            diff = r.json()
            check("changes" in diff and "counts" in diff, "diff has changes and counts")
            check(diff["entries"]["a"] > 0 and diff["entries"]["b"] > 0,
                  "both sides have entries",
                  f"{diff['entries']}")

            r = await client.get(f"/timetables/version/{v1}/compare/{v1}",
                                 headers=auth)
            check(r.json()["identical"] is True,
                  "a version compared with itself is identical",
                  str(r.json()["counts"]))

            r = await client.get(f"/timetables/version/{v1}/compare/{v2}",
                                 headers=other_auth)
            check(r.status_code == 404, "another school cannot compare",
                  f"got {r.status_code}")

            # --- Editing needs a draft -------------------------------------------
            StubAI.edit_reply = {"interpretation": "x", "change_type": "move_class",
                                 "changes": [], "confidence": "low"}
            r = await client.post("/editing/request", headers=auth,
                                  json={"version_id": v1,
                                        "request_text": "move something"})
            check(r.status_code == 409, "a published version cannot be edited",
                  f"got {r.status_code}")

            # A fresh draft to edit, on its own timetable so discarding it at
            # the end does not disturb the published one above.
            tid3, v3 = await generate(school_id, f"Edit test C {RUN}")
            entry = await an_entry(v3)
            other_entry = await an_entry(v3, offset=1)

            # --- A clean move ------------------------------------------------------
            free = await db.fetchrow("""
                SELECT lp.id, lp.day_number, lp.period_number
                FROM layout_periods lp
                WHERE lp.layout_id = (SELECT layout_id FROM timetables WHERE id = $1)
                  AND lp.period_type = 'teaching'
                  AND NOT EXISTS (
                      SELECT 1 FROM timetable_entries te
                      WHERE te.version_id = $2
                        AND te.layout_period_id = lp.id
                        AND (te.teacher_id = $3 OR te.room_id = $4))
                  AND NOT EXISTS (
                      SELECT 1 FROM timetable_entries te
                      JOIN timetable_entry_students a
                        ON a.timetable_entry_id = te.id
                      WHERE te.version_id = $2 AND te.layout_period_id = lp.id
                        AND a.student_id IN (
                            SELECT student_id FROM timetable_entry_students
                            WHERE timetable_entry_id = $5))
                LIMIT 1
            """, tid3, v3, entry["teacher_id"], entry["room_id"], entry["id"])
            check(free is not None, "the sandbox has a genuinely free period")

            StubAI.edit_reply = {
                "interpretation": f"Move {entry['class_code']} to another period",
                "change_type": "move_class",
                "changes": [{
                    "entry_id": str(entry["id"]),
                    "entity_name": entry["class_code"],
                    "field": "layout_period_id",
                    "old_value": str(entry["layout_period_id"]),
                    "new_value": str(free["id"]),
                    "day_number": free["day_number"],
                    "description": "Move to a free period",
                }],
                "confidence": "high", "clarification_needed": None,
            }

            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3,
                "request_text": f"Move {entry['class_code']} to another period"})
            check(r.status_code == 201, "POST an edit request",
                  f"HTTP {r.status_code} {r.text[:120]}")
            proposal = r.json()
            edit_id = proposal["id"]

            check(proposal["validator_result"] == "pass",
                  "a clean move validates", str(proposal["validator_detail"]))
            check(proposal["can_apply"] is True, "and can be applied")
            check(proposal["status"] == "pending", "it is pending, not applied")
            check(len(proposal["changes"]) == 1, "the diff has one change")

            # The after-slot must be the period actually proposed, not merely
            # different from the before-slot: "either the day or the period
            # changed" would pass on a diff that showed the wrong destination.
            target_label = await db.fetchval("""
                SELECT coalesce(label, 'P' || period_number)
                FROM layout_periods WHERE id = $1
            """, free["id"])
            shown = proposal["changes"][0]
            check(shown["after"]["day"] == free["day_number"]
                  and shown["after"]["period"] == target_label,
                  "the diff shows the exact slot proposed",
                  f"{shown['after']} vs day {free['day_number']} {target_label}")
            check(shown["before"]["day"] == entry["day_number"],
                  "and the before-slot is where it is now",
                  f"{shown['before']['day']} vs {entry['day_number']}")

            # Interpreting must not touch the timetable or the balance.
            still = await db.fetchval(
                "SELECT layout_period_id FROM timetable_entries WHERE id = $1",
                entry["id"])
            check(str(still) == str(entry["layout_period_id"]),
                  "asking does not move anything")
            before_balance = (await billing.account_state(school_id))["balance"]

            # --- Applying ------------------------------------------------------------
            r = await client.post(f"/editing/{edit_id}/apply", headers=auth)
            check(r.status_code == 200, "apply the edit",
                  f"HTTP {r.status_code} {r.text[:120]}")
            check(r.json()["changes"] == 1, "one entry changed")

            moved = await db.fetchrow("""
                SELECT layout_period_id, day_number FROM timetable_entries
                WHERE id = $1
            """, entry["id"])
            check(str(moved["layout_period_id"]) == str(free["id"]),
                  "the entry is in the new period")
            check(moved["day_number"] == free["day_number"],
                  "and day_number moved with it - a stale day breaks transport",
                  f"{moved['day_number']} vs {free['day_number']}")

            after_balance = (await billing.account_state(school_id))["balance"]
            cost = r.json()["credits_charged"]
            check(cost > 0, "credits were charged", str(cost))
            check(before_balance - after_balance == cost,
                  "the balance moved by exactly that much",
                  f"{before_balance} -> {after_balance}")

            txn = await db.fetchrow("""
                SELECT type, amount FROM credit_transactions
                WHERE school_id = $1 AND type = 'edit_session'
                ORDER BY created_at DESC LIMIT 1
            """, school_id)
            check(txn is not None and txn["amount"] == -cost,
                  "a transaction records the edit", str(txn and txn["amount"]))

            r = await client.post(f"/editing/{edit_id}/apply", headers=auth)
            check(r.json().get("already_applied") is True,
                  "applying twice does nothing the second time")
            check((await billing.account_state(school_id))["balance"]
                  == after_balance, "and charges nothing the second time")

            # --- A change that would clash -------------------------------------------
            # Put one class exactly where another already is.
            clashing = await db.fetchrow("""
                SELECT te.id, te.layout_period_id, te.teacher_id
                FROM timetable_entries te
                WHERE te.version_id = $1 AND te.teacher_id = $2
                  AND te.id <> $3
                LIMIT 1
            """, v3, other_entry["teacher_id"], other_entry["id"])

            if clashing:
                StubAI.edit_reply = {
                    "interpretation": "Move into an occupied period",
                    "change_type": "move_class",
                    "changes": [{
                        "entry_id": str(other_entry["id"]),
                        "field": "layout_period_id",
                        "old_value": str(other_entry["layout_period_id"]),
                        "new_value": str(clashing["layout_period_id"]),
                        "description": "Straight into a clash",
                    }],
                    "confidence": "high",
                }
                r = await client.post("/editing/request", headers=auth, json={
                    "version_id": v3, "request_text": "move it onto the other one"})
                bad = r.json()
                check(bad["validator_result"] == "fail",
                      "a change that double-books is refused",
                      str(bad["validator_detail"]))
                check(bad["can_apply"] is False, "and cannot be applied")

                r = await client.post(f"/editing/{bad['id']}/apply", headers=auth)
                check(r.status_code == 422,
                      "applying a failed proposal is refused",
                      f"got {r.status_code}")

                unchanged = await db.fetchval(
                    "SELECT layout_period_id FROM timetable_entries WHERE id = $1",
                    other_entry["id"])
                check(str(unchanged) == str(other_entry["layout_period_id"]),
                      "the rejected change left nothing behind - the dry run "
                      "rolled back")

            # --- Things the AI must not be allowed to do -------------------------------
            StubAI.edit_reply = {
                "interpretation": "Rewrite the class code",
                "change_type": "move_class",
                "changes": [{"entry_id": str(entry["id"]), "field": "class_code",
                             "old_value": "a", "new_value": "b"}],
                "confidence": "high",
            }
            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3, "request_text": "rename the class"})
            check(r.json()["validator_result"] == "fail",
                  "a field outside the allowlist is refused",
                  str(r.json()["validator_detail"]))

            StubAI.edit_reply = {
                "interpretation": "Move an entry from another version",
                "change_type": "move_class",
                "changes": [{"entry_id": str((await an_entry(v1))["id"]),
                             "field": "layout_period_id",
                             "old_value": str(entry["layout_period_id"]),
                             "new_value": str(free["id"])}],
                "confidence": "high",
            }
            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3, "request_text": "move the other version's class"})
            check(r.json()["validator_result"] == "fail",
                  "an entry from another version is refused",
                  str(r.json()["validator_detail"]))

            StubAI.edit_reply = {
                "interpretation": "Move to a room that does not exist",
                "change_type": "change_room",
                "changes": [{"entry_id": str(entry["id"]), "field": "room_id",
                             "old_value": str(entry["room_id"]),
                             "new_value": str(uuid.uuid4())}],
                "confidence": "high",
            }
            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3, "request_text": "put it in room Z"})
            check(r.json()["validator_result"] == "fail",
                  "an invented room id is refused",
                  str(r.json()["validator_detail"]))

            StubAI.edit_reply = {
                "interpretation": "Rewrite everything",
                "change_type": "move_class",
                "changes": [{"entry_id": str(entry["id"]),
                             "field": "layout_period_id",
                             "old_value": str(entry["layout_period_id"]),
                             "new_value": str(free["id"])}] * 25,
                "confidence": "high",
            }
            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3, "request_text": "move everything"})
            check(r.json()["validator_result"] == "fail",
                  "a change touching more than 20 entries is a regeneration",
                  str(r.json()["validator_detail"]))

            StubAI.edit_reply = "this is not json at all"
            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3, "request_text": "something unparseable"})
            check(r.status_code == 502, "an unparseable AI reply is reported",
                  f"got {r.status_code}")

            # --- Ambiguity ---------------------------------------------------------------
            StubAI.edit_reply = {
                "interpretation": "Which Tuesday did you mean?",
                "change_type": "move_class", "changes": [],
                "confidence": "low",
                "clarification_needed": "Which day did you mean?",
            }
            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3, "request_text": "move it to tuesday"})
            check(r.json()["validator_result"] == "clarify",
                  "an ambiguous request asks for clarification",
                  str(r.json()["validator_result"]))
            check(r.json()["can_apply"] is False, "and cannot be applied")

            # --- Rejecting -----------------------------------------------------------------
            StubAI.edit_reply = {
                "interpretation": "Move it again",
                "change_type": "move_class",
                "changes": [{"entry_id": str(entry["id"]),
                             "field": "layout_period_id",
                             "old_value": str(free["id"]),
                             "new_value": str(entry["layout_period_id"])}],
                "confidence": "high",
            }
            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3, "request_text": "put it back"})
            reject_id = r.json()["id"]

            r = await client.post(f"/editing/{reject_id}/reject", headers=auth)
            check(r.status_code == 200, "an edit can be rejected")
            r = await client.post(f"/editing/{reject_id}/apply", headers=auth)
            check(r.status_code == 409, "a rejected edit cannot be applied",
                  f"got {r.status_code}")

            # --- History and isolation ---------------------------------------------------
            r = await client.get(f"/editing/version/{v3}/history", headers=auth)
            check(r.status_code == 200, "GET edit history")
            check(len(r.json()) >= 6, "every request is recorded, valid or not",
                  f"{len(r.json())} rows")
            check(any(e["status"] == "applied" for e in r.json()),
                  "the applied one is marked as such")

            r = await client.get(f"/editing/{edit_id}", headers=other_auth)
            check(r.status_code == 404, "another school cannot read an edit",
                  f"got {r.status_code}")
            r = await client.post(f"/editing/{edit_id}/apply", headers=other_auth)
            check(r.status_code == 404, "another school cannot apply an edit",
                  f"got {r.status_code}")
            r = await client.get(f"/editing/version/{v3}/history", headers=other_auth)
            check(r.json() == [], "another school sees no history",
                  f"{len(r.json())} rows")

            # --- Staff cannot edit -----------------------------------------------------------
            await db.execute("UPDATE users SET role = 'staff' WHERE id = $1", user_id)
            sb.forget_token(auth["Authorization"].split()[1])
            r = await client.post("/editing/request", headers=auth, json={
                "version_id": v3, "request_text": "move something"})
            check(r.status_code == 403, "staff cannot request an edit",
                  f"got {r.status_code}")
            r = await client.get(f"/editing/version/{v3}/history", headers=auth)
            check(r.status_code == 200, "staff can still read the history")
            await db.execute("UPDATE users SET role = 'owner' WHERE id = $1", user_id)
            sb.forget_token(auth["Authorization"].split()[1])

            # --- Discarding ---------------------------------------------------------------------
            r = await client.delete(f"/timetables/version/{v1}", headers=auth)
            check(r.status_code == 409, "a published version cannot be discarded",
                  f"got {r.status_code}")
            r = await client.delete(f"/timetables/version/{v2}", headers=auth)
            check(r.status_code == 409, "an archived version cannot be discarded",
                  f"got {r.status_code}")

            entries_before = await db.fetchval(
                "SELECT count(*) FROM timetable_entries WHERE version_id = $1", v3)
            check(entries_before > 0, "the draft has entries to discard",
                  str(entries_before))

            r = await client.delete(f"/timetables/version/{v3}", headers=auth)
            check(r.status_code == 200, "a draft can be discarded",
                  f"HTTP {r.status_code} {r.text[:120]}")
            check(await db.fetchval(
                "SELECT count(*) FROM timetable_entries WHERE version_id = $1", v3)
                == 0, "its entries go with it")
            check(await db.fetchval(
                "SELECT count(*) FROM timetable_versions WHERE id = $1", v3) == 0,
                "and the version itself")

        finally:
            ai_cluster.call = original  # type: ignore[assignment]
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM timetables WHERE name LIKE $1",
                    f"Edit test%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 11 - versioning and AI-assisted editing\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

"""
Phase 14 - CSV export templates.

Generates a real timetable against the sandbox school, defines a Sentral-shaped
template, and checks the CSV byte for byte.

The cases that matter: a template is a presentation instruction and must never
become a query, the allowlist must hold against a hostile template, and a room
called "Hall, Main" must not shift every later column by one.

    python test_export.py
"""

import asyncio
import csv
import io
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
from services import ai_cluster, exporter, pipeline  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.pipeline").setLevel(logging.CRITICAL)
logging.getLogger("klasser.deterministic").setLevel(logging.CRITICAL)
logging.getLogger("klasser.export").setLevel(logging.CRITICAL)
logging.getLogger("klasser.exporter").setLevel(logging.CRITICAL)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
schools: list[tuple[str, str]] = []
timetables: list[str] = []
templates: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


class StubAI:
    """The pipeline's AI, plus a scripted reply for the export mapper."""

    export_reply: dict | str = {}

    @classmethod
    async def call(cls, task_key, prompt, **kwargs):
        if task_key == "task_export_map":
            if isinstance(cls.export_reply, str):
                return cls.export_reply
            return json.dumps(cls.export_reply)
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
    for template_id in templates:
        await db.execute("DELETE FROM csv_output_templates WHERE id = $1",
                         template_id)

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
            "DELETE FROM transport_schedule WHERE timetable_id = $1",
            "DELETE FROM timetable_versions WHERE timetable_id = $1",
            "DELETE FROM generation_attempts WHERE timetable_id = $1",
            "DELETE FROM timetables WHERE id = $1",
        ):
            await db.execute(sql, tid)

    for school_id, user_id in schools:
        # By id: this suite moves its user onto the sandbox school, so deleting
        # by school would miss them and strand the auth account too.
        await db.execute(
            "DELETE FROM notification_preferences WHERE user_id = $1", user_id)
        await db.execute("DELETE FROM users WHERE id = $1", user_id)
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM csv_output_templates WHERE school_id = $1",
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
    original = ai_cluster.call
    ai_cluster.call = StubAI.call  # type: ignore[assignment]
    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            school_id = await db.fetchval(
                "SELECT value FROM settings WHERE key = 'sandbox_school_id'")

            email = f"p14.{RUN}@example.com"
            r = await client.post("/auth/signup", json={
                "school_name": f"Export Test {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Test", "surname": "Owner", "email": email,
                "password": PASSWORD, "campuses": [{"name": "Main"}]})
            throwaway, user_id = r.json()["school_id"], r.json()["user_id"]
            schools.append((throwaway, user_id))
            await db.execute("UPDATE users SET school_id = $2 WHERE id = $1",
                             user_id, school_id)
            auth = {"Authorization": f"Bearer {await sign_in(email)}"}

            other_email = f"p14.other.{RUN}@example.com"
            r = await client.post("/auth/signup", json={
                "school_name": f"Export Other {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Other", "surname": "Owner", "email": other_email,
                "password": PASSWORD, "campuses": [{"name": "Main"}]})
            schools.append((r.json()["school_id"], r.json()["user_id"]))
            other_auth = {"Authorization": f"Bearer {await sign_in(other_email)}"}

            # --- A real timetable to export -----------------------------------
            layout_id = await db.fetchval("""
                SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
                ORDER BY created_at DESC LIMIT 1
            """, school_id)
            tid = str(await db.fetchval("""
                INSERT INTO timetables (school_id, name, layout_id, status)
                VALUES ($1, $2, $3, 'processing') RETURNING id
            """, school_id, f"Export test {RUN}", layout_id))
            timetables.append(tid)
            summary = await pipeline.run_with_retries(tid, "export@example.com")
            version_id = summary["version_id"]
            check(bool(version_id), "a timetable to export")

            # --- The vocabulary -------------------------------------------------
            r = await client.get("/export/fields", headers=auth)
            check(r.status_code == 200, "GET fields", f"HTTP {r.status_code}")
            sources = {f["source"] for f in r.json()["fields"]}
            check("class_code" in sources and "teacher.surname" in sources,
                  "timetable fields listed", str(len(sources)))

            r = await client.get("/export/fields", headers=auth,
                                 params={"output_type": "transport"})
            check({f["source"] for f in r.json()["fields"]} != sources,
                  "a different output type offers different fields")

            r = await client.get("/export/fields", headers=auth,
                                 params={"output_type": "nonsense"})
            check(r.status_code == 422, "an unknown output type is refused",
                  f"got {r.status_code}")

            # --- Interpreting a pasted header row (no AI) -------------------------
            StubAI.export_reply = {"columns": [], "unmatched": ["everything"],
                                   "note": "should not be called"}
            r = await client.post("/export/interpret", headers=auth, json={
                "description": "ClassCode,TeacherSurname,RoomName,PeriodStart,DayNumber",
                "output_type": "timetable_entries"})
            check(r.status_code == 200, "POST interpret", f"HTTP {r.status_code}")
            mapped = r.json()
            check(mapped["used_ai"] is False,
                  "a recognised header row costs no AI call")
            check([c["source"] for c in mapped["columns"]] ==
                  ["class_code", "teacher.surname", "room.name",
                   "period.start_time", "period.day_number"],
                  "every column mapped to the right field",
                  str([c["source"] for c in mapped["columns"]]))
            check([c["label"] for c in mapped["columns"]][0] == "ClassCode",
                  "the school's own heading is kept")

            # --- Interpreting prose (AI) -------------------------------------------
            StubAI.export_reply = {
                "columns": [
                    {"label": "Class", "source": "class_code", "format": None},
                    {"label": "Teacher", "source": "teacher.full_name",
                     "format": None},
                    {"label": "Starts", "source": "period.start_time",
                     "format": "HH:MM"},
                ],
                "unmatched": ["the colour of the room"],
                "note": "No field records room colour.",
            }
            r = await client.post("/export/interpret", headers=auth, json={
                "description": "I need the class, who teaches it, what time it "
                               "starts and the colour of the room",
                "output_type": "timetable_entries"})
            mapped = r.json()
            check(mapped["used_ai"] is True, "prose goes to the AI")
            check(len(mapped["columns"]) == 3, "three columns mapped",
                  str(len(mapped["columns"])))
            check(mapped["unmatched"] == ["the colour of the room"],
                  "what it could not map is reported", str(mapped["unmatched"]))

            # An invented field must never survive into a template.
            StubAI.export_reply = {
                "columns": [
                    {"label": "Class", "source": "class_code", "format": None},
                    {"label": "Secret", "source": "teacher.salary", "format": None},
                    {"label": "Also", "source": "'; DROP TABLE users; --",
                     "format": None},
                ],
                "unmatched": [],
            }
            r = await client.post("/export/interpret", headers=auth, json={
                "description": "class code and the teacher's salary please",
                "output_type": "timetable_entries"})
            mapped = r.json()
            check([c["source"] for c in mapped["columns"]] == ["class_code"],
                  "fields the AI invented are dropped",
                  str([c["source"] for c in mapped["columns"]]))
            check(len(mapped["unmatched"]) == 2,
                  "and reported as unmatched rather than silently lost",
                  str(mapped["unmatched"]))

            # --- Saving a template --------------------------------------------------
            sentral = {
                "name": f"Sentral {RUN}",
                "description": "Sentral timetable import",
                "output_type": "timetable_entries",
                "columns": [
                    {"label": "ClassCode", "source": "class_code", "format": None},
                    {"label": "TeacherSurname", "source": "teacher.surname",
                     "format": None},
                    {"label": "RoomName", "source": "room.name", "format": None},
                    {"label": "PeriodStart", "source": "period.start_time",
                     "format": "HH:MM"},
                    {"label": "DayNumber", "source": "period.day_number",
                     "format": "integer"},
                ],
                "delimiter": ",", "include_header": True,
            }
            r = await client.post("/export/templates", headers=auth, json=sentral)
            check(r.status_code == 201, "create a template",
                  f"HTTP {r.status_code} {r.text[:120]}")
            template_id = r.json()["id"]
            templates.append(template_id)

            r = await client.post("/export/templates", headers=auth, json=sentral)
            check(r.status_code == 409, "a duplicate name is refused",
                  f"got {r.status_code}")

            bad = dict(sentral, name=f"Bad {RUN}", columns=[
                {"label": "Salary", "source": "teacher.salary", "format": None}])
            r = await client.post("/export/templates", headers=auth, json=bad)
            check(r.status_code == 422, "a template with an unknown field is refused",
                  f"got {r.status_code}")
            check("teacher.salary" in r.text, "and says which field",
                  r.text[:100])

            bad = dict(sentral, name=f"Bad {RUN}", columns=[
                {"label": "X", "source": "class_code", "format": "sql"}])
            r = await client.post("/export/templates", headers=auth, json=bad)
            check(r.status_code == 422, "an unknown format is refused",
                  f"got {r.status_code}")

            bad = dict(sentral, name=f"Bad {RUN}", columns=[])
            r = await client.post("/export/templates", headers=auth, json=bad)
            check(r.status_code == 422, "a template with no columns is refused",
                  f"got {r.status_code}")

            bad = dict(sentral, name=f"Bad {RUN}", delimiter="DROP")
            r = await client.post("/export/templates", headers=auth, json=bad)
            check(r.status_code == 422, "an odd delimiter is refused",
                  f"got {r.status_code}")

            # --- The CSV itself -------------------------------------------------------
            r = await client.get(f"/export/preview/{version_id}/{template_id}",
                                 headers=auth, params={"rows": 5})
            check(r.status_code == 200, "GET preview", f"HTTP {r.status_code}")
            preview = r.json()
            check(preview["total_rows"] > 0, "the timetable has rows to export",
                  str(preview["total_rows"]))
            check(preview["showing"] == 5, "five rows shown",
                  str(preview["showing"]))
            check(preview["header"] ==
                  "ClassCode,TeacherSurname,RoomName,PeriodStart,DayNumber",
                  "the header is exactly what the school asked for",
                  str(preview["header"]))

            r = await client.get(f"/export/download/{version_id}/{template_id}",
                                 headers=auth)
            check(r.status_code == 200, "GET download", f"HTTP {r.status_code}")
            check(r.headers["content-type"].startswith("text/csv"),
                  "served as CSV", r.headers["content-type"])
            check("attachment" in r.headers.get("content-disposition", ""),
                  "as a download", r.headers.get("content-disposition", ""))
            check(".csv" in r.headers.get("content-disposition", ""),
                  "with a .csv filename")

            parsed = list(csv.reader(io.StringIO(r.text)))
            check(len(parsed) == preview["total_rows"] + 1,
                  "every row is present plus the header",
                  f"{len(parsed)} vs {preview['total_rows'] + 1}")
            check(all(len(row) == 5 for row in parsed),
                  "every row has five fields - nothing is shifted",
                  str({len(row) for row in parsed}))

            times = [row[3] for row in parsed[1:]]
            check(all(len(t) == 5 and t[2] == ":" for t in times),
                  "times are formatted HH:MM", str(times[:3]))
            days = [row[4] for row in parsed[1:]]
            check(all(d.isdigit() for d in days),
                  "day numbers are integers", str(days[:3]))

            # A real school has rooms and names with commas and apostrophes in
            # them. Joining strings instead of using the csv module puts every
            # later column one place out.
            room_id = await db.fetchval("""
                SELECT room_id FROM timetable_entries WHERE version_id = $1 LIMIT 1
            """, version_id)
            old_name = await db.fetchval(
                "SELECT name FROM rooms WHERE id = $1", room_id)
            await db.execute("UPDATE rooms SET name = $2 WHERE id = $1",
                             room_id, 'Hall, "Main" O\'Brien')
            try:
                r = await client.get(
                    f"/export/download/{version_id}/{template_id}", headers=auth)
                parsed = list(csv.reader(io.StringIO(r.text)))
                check(all(len(row) == 5 for row in parsed),
                      "a room name with a comma and quotes does not shift columns",
                      str({len(row) for row in parsed}))
                check(any('Hall, "Main" O\'Brien' in row for row in parsed),
                      "and survives the round trip intact")
                # A cell beginning = + - @ is a formula to Excel, Sheets and
                # LibreOffice. The whole point of an export is that someone
                # opens it, so a room named =HYPERLINK(...) would run on a
                # deputy principal's machine.
                await db.execute("UPDATE rooms SET name = $2 WHERE id = $1",
                                 room_id, '=HYPERLINK("http://evil","Payroll")')
                r = await client.get(
                    f"/export/download/{version_id}/{template_id}", headers=auth)
                cells = [c for row in csv.reader(io.StringIO(r.text))
                         for c in row]
                dangerous = [c for c in cells
                             if c[:1] in ("=", "+", "-", "@")]
                check(not dangerous,
                      "no exported cell can be read as a spreadsheet formula",
                      str(dangerous[:2]))
                check(any(c.startswith("'=HYPERLINK") for c in cells),
                      "the value is preserved, just neutralised",
                      str([c for c in cells if "HYPERLINK" in c][:1]))

                # The guard must not mangle ordinary data.
                check(all(not c.startswith("'") for c in cells
                          if "HYPERLINK" not in c),
                      "and ordinary values are untouched")
            finally:
                await db.execute("UPDATE rooms SET name = $2 WHERE id = $1",
                                 room_id, old_name)

            # --- Other output types -----------------------------------------------------
            duties = {
                "name": f"Duties {RUN}", "output_type": "duties",
                "columns": [
                    {"label": "Teacher", "source": "teacher.full_name",
                     "format": None},
                    {"label": "Duty", "source": "duty_type", "format": None},
                    {"label": "Day", "source": "day_number", "format": "integer"},
                ],
                "delimiter": "\t", "include_header": True,
            }
            r = await client.post("/export/templates", headers=auth, json=duties)
            check(r.status_code == 201, "a duties template",
                  f"HTTP {r.status_code} {r.text[:120]}")
            duties_id = r.json()["id"]
            templates.append(duties_id)

            r = await client.get(f"/export/preview/{version_id}/{duties_id}",
                                 headers=auth)
            check(r.status_code == 200, "preview a tab-separated export")
            check("\t" in (r.json()["header"] or ""),
                  "the tab delimiter is used", repr(r.json()["header"]))

            # Asserting on the header alone would pass against an empty result,
            # leaving the duties query itself unexercised.
            assigned = await db.fetchval(
                "SELECT count(*) FROM duty_assignments WHERE version_id = $1",
                version_id)
            check(r.json()["total_rows"] == assigned,
                  "the duties query returns every assigned duty",
                  f"{r.json()['total_rows']} vs {assigned} in the database")
            check(assigned > 0, "and the sandbox actually has duties to export",
                  str(assigned))

            if assigned:
                rows = list(csv.reader(io.StringIO(r.json()["csv"]),
                                       delimiter="\t"))
                check(all(len(row) == 3 for row in rows),
                      "every duty row has three fields",
                      str({len(row) for row in rows}))

            # A field from another output type cannot be borrowed.
            bad = dict(duties, name=f"Mixed {RUN}", columns=[
                {"label": "Class", "source": "class_code", "format": None}])
            r = await client.post("/export/templates", headers=auth, json=bad)
            check(r.status_code == 422,
                  "a timetable field cannot be used in a duties template",
                  f"got {r.status_code}")

            # --- Editing and deleting ------------------------------------------------------
            changed = dict(sentral, name=f"Sentral {RUN}", include_header=False)
            r = await client.put(f"/export/templates/{template_id}",
                                 headers=auth, json=changed)
            check(r.status_code == 200, "update a template")

            r = await client.get(f"/export/preview/{version_id}/{template_id}",
                                 headers=auth)
            check(r.json()["header"] is None, "the header row can be turned off")

            r = await client.get("/export/templates", headers=auth)
            check(len(r.json()) == 2, "both templates are listed",
                  str(len(r.json())))

            # --- Isolation --------------------------------------------------------------------
            r = await client.get("/export/templates", headers=other_auth)
            check(r.json() == [], "another school sees no templates",
                  f"{len(r.json())}")
            r = await client.get(f"/export/preview/{version_id}/{template_id}",
                                 headers=other_auth)
            check(r.status_code == 404, "and cannot preview the export",
                  f"got {r.status_code}")
            r = await client.get(f"/export/download/{version_id}/{template_id}",
                                 headers=other_auth)
            check(r.status_code == 404, "nor download it", f"got {r.status_code}")
            r = await client.put(f"/export/templates/{template_id}",
                                 headers=other_auth, json=changed)
            check(r.status_code == 404, "nor edit it", f"got {r.status_code}")
            r = await client.delete(f"/export/templates/{template_id}",
                                    headers=other_auth)
            check(r.status_code == 404, "nor delete it", f"got {r.status_code}")

            # --- Staff ----------------------------------------------------------------------------
            await db.execute("UPDATE users SET role = 'staff' WHERE id = $1",
                             user_id)
            sb.forget_token(auth["Authorization"].split()[1])
            r = await client.post("/export/templates", headers=auth,
                                  json=dict(sentral, name=f"Staff {RUN}"))
            check(r.status_code == 403, "staff cannot create a template",
                  f"got {r.status_code}")
            r = await client.get(f"/export/download/{version_id}/{template_id}",
                                 headers=auth)
            check(r.status_code == 200, "but staff can still export",
                  f"got {r.status_code}")
            await db.execute("UPDATE users SET role = 'owner' WHERE id = $1",
                             user_id)
            sb.forget_token(auth["Authorization"].split()[1])

            r = await client.delete(f"/export/templates/{template_id}",
                                    headers=auth)
            check(r.status_code == 204, "the owner deletes a template",
                  f"got {r.status_code}")
            templates.remove(template_id)

        finally:
            ai_cluster.call = original  # type: ignore[assignment]
            try:
                await cleanup()
                left = await db.fetchval(
                    "SELECT count(*) FROM timetables WHERE name LIKE $1",
                    f"Export test%{RUN}")
                check(left == 0, "test data cleaned up", f"{left} left")
            except Exception as exc:  # noqa: BLE001
                check(False, "test data cleaned up", f"{type(exc).__name__}: {exc}")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 14 - CSV export templates\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

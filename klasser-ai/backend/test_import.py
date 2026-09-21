"""
Import test against the live Supabase project.

The AI mapping call is stubbed, so parsing, mapping, validation, duplicate
matching and the actual writes are all verified without a provider key. One
case deliberately lets the AI call fail to prove the heuristic fallback works.

    python test_import.py
"""

import asyncio
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
from services import ai_cluster, importer  # noqa: E402
from services import supabase_client as sb  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.importer").setLevel(logging.ERROR)

RUN = uuid.uuid4().hex[:8]
PASSWORD = "TestPass123"

results: list[tuple[bool, str]] = []
created: list[tuple[str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


# --- Stubbed AI --------------------------------------------------------------

class FakeAI:
    """Returns a mapping built from the headers, or raises to force fallback."""
    fail = False
    last_prompt = ""

    @classmethod
    async def call(cls, task_key, prompt, **kwargs):
        cls.last_prompt = prompt
        if cls.fail:
            raise ai_cluster.AIError("simulated provider outage")

        # Echo a plausible mapping: exact header-name matches only, so the test
        # exercises the correction path for anything else.
        headers = json.loads(prompt.split("Column headers found in the file:")[1]
                             .split("\n\nFirst rows")[0].strip())
        known = {
            "First Name": "first_name", "Surname": "surname",
            "Teaches": "subjects", "Campus": "campus", "Max Blocks": "max_blocks",
            "Student Name": "full_name", "Year": "year_group",
            "Room": "name", "Seats": "capacity", "Type": "room_type",
        }
        return json.dumps({
            "detected_columns": [
                {"source_column": h, "klasser_field": known.get(h),
                 "confidence": "high", "sample_values": []}
                for h in headers
            ],
            "unrecognised_columns": [h for h in headers if h not in known],
            "warnings": [],
        })


async def sign_in(email: str):
    async with httpx.AsyncClient(base_url=keys.SUPABASE_URL, timeout=20.0,
                                 verify=SSL_CONTEXT) as c:
        r = await c.post("/auth/v1/token", params={"grant_type": "password"},
                         headers={"apikey": keys.SUPABASE_ANON_KEY},
                         json={"email": email, "password": PASSWORD})
    return r.json().get("access_token") if r.status_code == 200 else None


async def cleanup() -> None:
    for school_id, user_id in created:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM import_jobs WHERE school_id = $1",
                "DELETE FROM student_subjects WHERE school_id = $1",
                "DELETE FROM students WHERE school_id = $1",
                "DELETE FROM teachers WHERE school_id = $1",
                "DELETE FROM rooms WHERE school_id = $1",
                "DELETE FROM subject_settings WHERE school_id = $1",
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


def upload_files(text: str, name: str = "data.csv"):
    return {"file": (name, io.BytesIO(text.encode()), "text/csv")}


async def main_test() -> None:
    await db.connect()

    # Patch only the call function, not the module. Replacing the module would
    # break `except ai_cluster.AIError` inside interpret(), which then raises
    # AttributeError instead of taking the fallback path.
    original_call = ai_cluster.call
    ai_cluster.call = FakeAI.call  # type: ignore[assignment]

    transport = httpx.ASGITransport(app=main.app)

    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as client:
        try:
            email = f"import.{RUN}@example.com"
            r = await client.post("/auth/signup", json={
                "school_name": f"Import Test {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Test", "surname": "Owner", "email": email,
                "password": PASSWORD,
                "campuses": [{"name": "North Campus"}, {"name": "South Campus"}],
            })
            created.append((r.json()["school_id"], r.json()["user_id"]))
            school_id = r.json()["school_id"]
            auth = {"Authorization": f"Bearer {await sign_in(email)}"}

            # --- Parsing --------------------------------------------------
            csv_text = (
                "First Name,Surname,Teaches,Campus,Max Blocks\n"
                "Ada,Lovelace,\"Mathematics, Computing\",North Campus,5\n"
                "Grace,Hopper,Computing,South Campus,4\n"
                "Alan,Turing,Mathematics,North Campus,\n"
            )
            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(csv_text),
                                  data={"import_type": "teachers"})
            check(r.status_code == 201, "upload CSV", f"HTTP {r.status_code} {r.text[:120]}")
            job = r.json()
            check(job["rows_found"] == 3, "three data rows found", str(job["rows_found"]))
            check(job["format"] == "csv", "format detected as csv")
            check(len(job["headers"]) == 5, "five headers", str(len(job["headers"])))

            mapped = {c["source_column"]: c["klasser_field"]
                      for c in job["mapping"]["detected_columns"]}
            check(mapped.get("First Name") == "first_name", "AI mapping applied")
            check(job["mapping"]["source"] == "ai", "mapping came from the AI")

            # --- Confirm and import ---------------------------------------
            r = await client.post(f"/import/{job['job_id']}/confirm", headers=auth,
                                  json={"detected_columns":
                                        job["mapping"]["detected_columns"]})
            check(r.status_code == 200, "confirm import", f"HTTP {r.status_code} {r.text[:150]}")
            summary = r.json()
            check(summary["inserted"] == 3, "three teachers inserted",
                  str(summary["inserted"]))
            check(summary["skipped"] == 0, "nothing skipped", str(summary["skipped"]))

            row = await db.fetchrow("""
                SELECT full_name, subjects, max_blocks, campus_id FROM teachers
                WHERE school_id = $1 AND surname = 'Lovelace'
            """, school_id)
            check(row["full_name"] == "Ada Lovelace", "generated full_name correct")
            check(list(row["subjects"]) == ["Mathematics", "Computing"],
                  "comma-separated subjects split", str(row["subjects"]))
            check(row["max_blocks"] == 5, "numeric field converted", str(row["max_blocks"]))
            check(row["campus_id"] is not None, "campus matched by name")

            blank = await db.fetchval("""
                SELECT max_blocks FROM teachers WHERE school_id = $1 AND surname = 'Turing'
            """, school_id)
            check(blank is None, "empty numeric cell left null", str(blank))

            # --- Re-import updates rather than duplicating -------------------
            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(csv_text),
                                  data={"import_type": "teachers"})
            r = await client.post(f"/import/{r.json()['job_id']}/confirm", headers=auth,
                                  json={"detected_columns":
                                        r.json()["mapping"]["detected_columns"]})
            check(r.json()["updated"] == 3, "re-import updates all three",
                  str(r.json()["updated"]))
            n = await db.fetchval(
                "SELECT count(*) FROM teachers WHERE school_id = $1", school_id)
            check(n == 3, "no duplicate teachers created", str(n))

            # --- Bad rows are skipped with reasons ----------------------------
            bad = (
                "First Name,Surname,Campus\n"
                "Valid,Person,North Campus\n"
                ",,North Campus\n"
                "Onlyfirst,,North Campus\n"
                "Unknown,Campus,Nowhere Campus\n"
            )
            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(bad),
                                  data={"import_type": "teachers"})
            j = r.json()
            r = await client.post(f"/import/{j['job_id']}/confirm", headers=auth,
                                  json={"detected_columns":
                                        j["mapping"]["detected_columns"]})
            s = r.json()
            check(s["skipped"] == 2, "two invalid rows skipped", str(s["skipped"]))
            check(len(s["warnings"]) >= 2, "reasons recorded", str(len(s["warnings"])))
            check(any("campus" in w["reason"].lower() for w in s["warnings"]),
                  "unknown campus warned about")
            check(any(w["row"] == 3 for w in s["warnings"]),
                  "warning names the file row number")

            unknown = await db.fetchval("""
                SELECT campus_id FROM teachers
                WHERE school_id = $1 AND surname = 'Campus'
            """, school_id)
            check(unknown is None, "row with unknown campus still imported, campus null")

            # --- Full name splitting -------------------------------------------
            single = "Name,Campus\nAnna Maria Patel,North Campus\nPrince,North Campus\n"
            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(single),
                                  data={"import_type": "teachers"})
            j = r.json()
            cols = [{"source_column": "Name", "klasser_field": "full_name"},
                    {"source_column": "Campus", "klasser_field": "campus"}]
            r = await client.post(f"/import/{j['job_id']}/confirm", headers=auth,
                                  json={"detected_columns": cols})
            s = r.json()
            check(s["inserted"] == 1, "one of two name rows imported", str(s["inserted"]))
            check(s["skipped"] == 1, "single-word name skipped", str(s["skipped"]))
            row = await db.fetchrow("""
                SELECT first_name, surname FROM teachers
                WHERE school_id = $1 AND surname = 'Patel'
            """, school_id)
            check(row["first_name"] == "Anna Maria", "split on the last space",
                  f"{row['first_name']!r}")

            # --- User can correct the mapping -------------------------------------
            students = "Student Name,Year,House\nJo Smith,Year 11,Blue\n"
            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(students),
                                  data={"import_type": "students"})
            j = r.json()
            check(any(c["klasser_field"] is None
                      for c in j["mapping"]["detected_columns"]),
                  "unmapped column reported for review")

            # Deliberately override with a wrong-but-valid field, then a bogus one.
            cols = [{"source_column": "Student Name", "klasser_field": "full_name"},
                    {"source_column": "Year", "klasser_field": "year_group"},
                    {"source_column": "House", "klasser_field": "not_a_field"}]
            r = await client.post(f"/import/{j['job_id']}/confirm", headers=auth,
                                  json={"detected_columns": cols})
            check(r.status_code == 200, "bogus field ignored rather than rejected",
                  f"HTTP {r.status_code}")
            check(r.json()["inserted"] == 1, "student imported")

            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(students),
                                  data={"import_type": "students"})
            r = await client.post(f"/import/{r.json()['job_id']}/confirm", headers=auth,
                                  json={"detected_columns": [
                                      {"source_column": "House",
                                       "klasser_field": None}]})
            check(r.status_code == 422, "import with nothing mapped rejected",
                  f"got {r.status_code}")

            # --- Rooms need a real campus -------------------------------------------
            rooms = "Room,Seats,Type,Campus\nN-101,30,classroom,North Campus\nX-1,25,classroom,Nowhere\n"
            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(rooms),
                                  data={"import_type": "rooms"})
            j = r.json()
            cols = [{"source_column": "Room", "klasser_field": "name"},
                    {"source_column": "Seats", "klasser_field": "capacity"},
                    {"source_column": "Type", "klasser_field": "room_type"},
                    {"source_column": "Campus", "klasser_field": "campus"}]
            r = await client.post(f"/import/{j['job_id']}/confirm", headers=auth,
                                  json={"detected_columns": cols})
            s = r.json()
            check(s["inserted"] == 1, "room on a known campus imported", str(s["inserted"]))
            check(s["failed"] == 1, "room on an unknown campus failed", str(s["failed"]))

            # --- Heuristic fallback when the AI is unavailable -------------------------
            FakeAI.fail = True
            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(
                                      "first name,surname,campus\nZoe,Ng,North Campus\n"),
                                  data={"import_type": "teachers"})
            j = r.json()
            check(j["mapping"]["source"] == "heuristic",
                  "falls back to header matching when the AI fails",
                  j["mapping"].get("source"))
            m = {c["source_column"]: c["klasser_field"]
                 for c in j["mapping"]["detected_columns"]}
            check(m.get("first name") == "first_name",
                  "heuristic normalises header names", str(m))
            r = await client.post(f"/import/{j['job_id']}/confirm", headers=auth,
                                  json={"detected_columns":
                                        j["mapping"]["detected_columns"]})
            check(r.json()["inserted"] == 1, "import works without any AI")
            FakeAI.fail = False

            # --- Rejections -------------------------------------------------------------
            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files("a,b\n1,2\n"),
                                  data={"import_type": "wombats"})
            check(r.status_code == 422, "unknown import type rejected", f"got {r.status_code}")

            r = await client.post("/import/upload", headers=auth,
                                  files=upload_files(""),
                                  data={"import_type": "teachers"})
            check(r.status_code == 422, "empty file rejected", f"got {r.status_code}")

            r = await client.post(
                "/import/upload", headers=auth,
                files={"file": ("scan.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
                data={"import_type": "teachers"})
            check(r.status_code == 422, "PDF rejected clearly", f"got {r.status_code}")
            check("csv" in r.text.lower(), "PDF rejection suggests CSV")

            # --- Job history ---------------------------------------------------------------
            r = await client.get("/import/jobs", headers=auth)
            check(r.status_code == 200 and len(r.json()) >= 7,
                  "import history listed", f"{len(r.json())} jobs")

            # --- Tenant isolation ------------------------------------------------------------
            other_email = f"import.other.{RUN}@example.com"
            r2 = await client.post("/auth/signup", json={
                "school_name": f"Other {RUN}", "timezone": "Australia/Sydney",
                "first_name": "Other", "surname": "Owner", "email": other_email,
                "password": PASSWORD, "campuses": [{"name": "Main"}]})
            created.append((r2.json()["school_id"], r2.json()["user_id"]))
            other_auth = {"Authorization": f"Bearer {await sign_in(other_email)}"}

            r = await client.get(f"/import/{job['job_id']}", headers=other_auth)
            check(r.status_code == 404, "cannot read another school's import job",
                  f"got {r.status_code}")

            # --- Onboarding progress updated ---------------------------------------------------
            r = await client.get("/onboarding/progress", headers=auth)
            teachers_step = next(s for s in r.json()["steps"]
                                 if s["key"] == "step_teachers")
            check(teachers_step["complete"], "importing teachers ticked the setup step")

        finally:
            ai_cluster.call = original_call  # type: ignore[assignment]
            await cleanup()
            left = await db.fetchval(
                "SELECT count(*) FROM schools WHERE name LIKE $1", f"%{RUN}")
            check(left == 0, "test data cleaned up", f"{left} left")
            await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nData import\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

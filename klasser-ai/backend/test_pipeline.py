"""
Phase 6 pipeline test - a full generation against the seeded sandbox school.

The AI is stubbed, so this proves the orchestration, chunking, registry writes,
conflict handling and final solution are correct without provider keys. One case
disables the AI entirely to confirm the deterministic fallbacks still produce a
valid timetable.

    python test_pipeline.py
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
from services import ai_cluster, deterministic, pipeline  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("klasser.pipeline").setLevel(logging.ERROR)

results: list[tuple[bool, str]] = []
created_attempts: list[str] = []
created_timetables: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


class StubAI:
    """
    Returns shape-correct but deliberately unhelpful answers.

    The point is that the pipeline must produce a valid timetable anyway: the
    model's output is a suggestion, and deterministic code decides.
    """
    enabled = True
    calls = 0
    validator_verdict = "pass"     # or 'fail', to test the blocking path
    validator_prompts: list[str] = []

    @classmethod
    async def call(cls, task_key, prompt, **kwargs):
        cls.calls += 1
        if not cls.enabled:
            raise ai_cluster.AIError("AI disabled for this test")

        if task_key.startswith("validator_"):
            cls.validator_prompts.append(prompt)
            return json.dumps({
                "result": cls.validator_verdict,
                "reason": None if cls.validator_verdict == "pass"
                          else "stub validator was told to fail",
                "confidence": "high",
            })

        if "subject_map" in prompt:
            return json.dumps({"subject_map": []})
        if '"groups"' in prompt:
            return json.dumps({"groups": []})       # unusable, forces even split
        if '"assignments"' in prompt and "period slots" in prompt:
            return json.dumps({"assignments": {}})  # forces deterministic fill
        if '"assignments"' in prompt:
            return json.dumps({"assignments": {}})  # forces load balancing
        return "{}"


async def sandbox_school_id() -> str:
    return await db.fetchval(
        "SELECT value FROM settings WHERE key = 'sandbox_school_id'")


async def cleanup() -> None:
    for attempt_id in created_attempts:
        async with db.transaction() as conn:
            for sql in (
                "DELETE FROM timetable_entry_students WHERE timetable_entry_id IN "
                "  (SELECT id FROM timetable_entries WHERE attempt_id = $1)",
                "DELETE FROM timetable_entries WHERE attempt_id = $1",
                "DELETE FROM duty_assignments WHERE attempt_id = $1",
                "DELETE FROM class_group_slots WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_students WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_teachers WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_rooms WHERE attempt_id = $1",
                "DELETE FROM allocation_registry_buses WHERE attempt_id = $1",
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
            ):
                await conn.execute(sql, attempt_id)

    for attempt_id in created_attempts:
        await db.execute(
            "DELETE FROM validation_results WHERE attempt_id = $1", attempt_id)

    for timetable_id in created_timetables:
        async with db.transaction() as conn:
            await conn.execute(
                "DELETE FROM transport_schedule WHERE timetable_id = $1", timetable_id)
            await conn.execute(
                "DELETE FROM timetable_versions WHERE timetable_id = $1", timetable_id)
            await conn.execute(
                "DELETE FROM generation_attempts WHERE timetable_id = $1", timetable_id)
            await conn.execute("DELETE FROM timetables WHERE id = $1", timetable_id)


async def make_attempt(school_id: str, name: str) -> tuple[str, str]:
    layout_id = await db.fetchval("""
        SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
        ORDER BY created_at DESC LIMIT 1
    """, school_id)
    timetable_id = await db.fetchval("""
        INSERT INTO timetables (school_id, name, layout_id, status)
        VALUES ($1,$2,$3,'processing') RETURNING id
    """, school_id, name, layout_id)
    attempt_id = await db.fetchval("""
        INSERT INTO generation_attempts (timetable_id, attempt_number, locked_model)
        VALUES ($1, 1, 'magistral') RETURNING id
    """, timetable_id)
    await db.execute("""
        INSERT INTO generation_jobs (attempt_id, status, notify_email)
        VALUES ($1,'queued','test@example.com')
    """, attempt_id)

    created_timetables.append(str(timetable_id))
    created_attempts.append(str(attempt_id))
    return str(attempt_id), str(timetable_id)


async def sweep_stale() -> int:
    """
    Remove timetables left by an earlier run that crashed before cleanup.

    Without this the final "cleaned up" check counts other runs' debris and
    fails for a reason that has nothing to do with this run.
    """
    stale = await db.fetch(
        "SELECT id FROM timetables WHERE name LIKE 'Pipeline test%'")
    for row in stale:
        created_timetables.append(str(row["id"]))
        for attempt in await db.fetch(
                "SELECT id FROM generation_attempts WHERE timetable_id = $1",
                row["id"]):
            created_attempts.append(str(attempt["id"]))
    return len(stale)


async def main_test() -> None:
    await db.connect()
    original_call = ai_cluster.call
    ai_cluster.call = StubAI.call  # type: ignore[assignment]

    try:
        swept = await sweep_stale()
        if swept:
            print(f"(sweeping {swept} timetable(s) left by an earlier run)")

        school_id = await sandbox_school_id()
        check(bool(school_id), "sandbox school found")

        # --- Pre-validation on good data ------------------------------------
        layout_id = await db.fetchval("""
            SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
        """, school_id)
        pre = await deterministic.pre_validate(school_id, str(layout_id))
        check(pre.passed, "sandbox data passes pre-validation",
              "; ".join(pre.failures[:2]))

        # --- Full run --------------------------------------------------------
        attempt_id, timetable_id = await make_attempt(school_id, "Pipeline test")
        summary = await pipeline.run(attempt_id)

        check(summary["entries"] > 0, "timetable entries written",
              str(summary["entries"]))
        check(summary["class_groups"] > 0, "class groups formed",
              str(summary["class_groups"]))

        version_id = summary["version_id"]

        # --- Every stage recorded ---------------------------------------------
        stages = await db.fetch("""
            SELECT stage, status FROM pipeline_state WHERE attempt_id = $1
        """, attempt_id)
        done = {s["stage"] for s in stages if s["status"] == "complete"}
        check(len(done) == 11, "all eleven stages completed", f"{len(done)}")
        check(all(s["status"] == "complete" for s in stages),
              "no stage left in progress or failed")

        # --- The solution is legal ----------------------------------------------
        solution = await deterministic.check_solution(version_id)
        check(solution.passed, "generated timetable passes every hard check",
              "; ".join(solution.failures[:3]))

        # --- Periods per cycle respected -----------------------------------------
        # A subject_settings row with year_level NULL applies to every year, and
        # an exact year match takes precedence. Joining only on an exact match
        # silently falls back to the school default and compares against the
        # wrong number - which is what this check did before.
        settings_join = """
            LEFT JOIN LATERAL (
                SELECT ss.min_periods_per_cycle, ss.max_periods_per_cycle
                FROM subject_settings ss
                WHERE ss.school_id = cg.school_id
                  AND ss.subject = cg.subject
                  AND (ss.year_level = cg.year_level OR ss.year_level IS NULL)
                ORDER BY (ss.year_level IS NULL)
                LIMIT 1
            ) ss ON true
        """

        shortfall = await db.fetch(f"""
            SELECT cg.class_code, cg.subject, count(s.*) AS given,
                   coalesce(ss.min_periods_per_cycle, 4) AS needed
            FROM class_groups cg
            JOIN class_group_slots s ON s.class_group_id = cg.id
            {settings_join}
            WHERE cg.attempt_id = $1
            GROUP BY cg.class_code, cg.subject, ss.min_periods_per_cycle
            HAVING count(s.*) < coalesce(ss.min_periods_per_cycle, 4)
        """, attempt_id)
        check(not shortfall, "every class got at least its minimum periods",
              f"{len(shortfall)} short"
              + (f" e.g. {shortfall[0]['class_code']} got {shortfall[0]['given']}"
                 f" of {shortfall[0]['needed']}" if shortfall else ""))

        excess = await db.fetch(f"""
            SELECT cg.class_code, count(s.*) AS given,
                   coalesce(ss.max_periods_per_cycle, 5) AS allowed
            FROM class_groups cg
            JOIN class_group_slots s ON s.class_group_id = cg.id
            {settings_join}
            WHERE cg.attempt_id = $1
            GROUP BY cg.class_code, ss.max_periods_per_cycle
            HAVING count(s.*) > coalesce(ss.max_periods_per_cycle, 5)
        """, attempt_id)
        check(not excess, "no class exceeded its maximum periods",
              f"{len(excess)} over")

        # --- Registries populated -------------------------------------------------
        for table, label in (
            ("allocation_registry_students", "student registry"),
            ("allocation_registry_teachers", "teacher registry"),
            ("allocation_registry_rooms", "room registry"),
            ("class_consistency_registry", "consistency registry"),
        ):
            n = await db.fetchval(
                f"SELECT count(*) FROM {table} WHERE attempt_id = $1", attempt_id)
            check(n > 0, f"{label} written", str(n))

        # --- Class consistency: one teacher and room per class ---------------------
        inconsistent = await db.fetch("""
            SELECT class_code, count(DISTINCT teacher_id) AS teachers,
                   count(DISTINCT room_id) AS rooms
            FROM timetable_entries WHERE version_id = $1
            GROUP BY class_code
            HAVING count(DISTINCT teacher_id) > 1 OR count(DISTINCT room_id) > 1
        """, version_id)
        check(not inconsistent,
              "each class keeps the same teacher and room all cycle",
              f"{len(inconsistent)} inconsistent")

        # --- Every student in a class is in its entries -----------------------------
        missing = await db.fetchval("""
            SELECT count(*) FROM class_groups cg
            JOIN class_group_students cgs ON cgs.class_group_id = cg.id
            WHERE cg.attempt_id = $1
              AND NOT EXISTS (
                  SELECT 1 FROM timetable_entries te
                  JOIN timetable_entry_students tes ON tes.timetable_entry_id = te.id
                  WHERE te.class_group_id = cg.id AND tes.student_id = cgs.student_id)
        """, attempt_id)
        check(missing == 0, "every class member appears in the timetable",
              f"{missing} missing")

        # --- day_number stays in step with its period --------------------------------
        drift = await db.fetchval("""
            SELECT count(*) FROM timetable_entries te
            JOIN layout_periods lp ON lp.id = te.layout_period_id
            WHERE te.version_id = $1 AND te.day_number <> lp.day_number
        """, version_id)
        check(drift == 0, "denormalised day_number matches its period", str(drift))

        # --- Duties --------------------------------------------------------------------
        duties = await db.fetchval(
            "SELECT count(*) FROM duty_assignments WHERE attempt_id = $1", attempt_id)
        check(duties > 0, "duties assigned", str(duties))

        duty_check = await deterministic.check_duties(attempt_id)
        check(duty_check.passed, "duty rules respected",
              "; ".join(duty_check.failures[:2]))

        exempt_used = await db.fetchval("""
            SELECT count(*) FROM duty_assignments da
            JOIN teacher_duty_exemptions e ON e.teacher_id = da.teacher_id
            WHERE da.attempt_id = $1
        """, attempt_id)
        check(exempt_used == 0, "the duty-exempt teacher got no duties",
              str(exempt_used))

        # --- Transport ------------------------------------------------------------------
        # `>= 0` is true of every count, so it asserted nothing. The sandbox has
        # two campuses with students whose home campus differs from where their
        # classes are, so movements must exist and every passenger leg must
        # actually carry someone.
        transport = await db.fetch("""
            SELECT passenger_count, is_empty_leg, from_campus_id, to_campus_id
            FROM transport_schedule WHERE version_id = $1
        """, version_id)
        check(len(transport) > 0, "transport solved for the two-campus school",
              f"{len(transport)} movements")
        carrying = [t for t in transport if not t["is_empty_leg"]]
        check(all(t["passenger_count"] > 0 for t in carrying),
              "every passenger leg carries at least one student",
              f"{sum(1 for t in carrying if t['passenger_count'] == 0)} empty")
        check(all(t["from_campus_id"] != t["to_campus_id"] for t in transport),
              "no movement starts and ends at the same campus")

        # --- Version and events ----------------------------------------------------------
        version = await db.fetchrow("""
            SELECT version_number, status FROM timetable_versions WHERE id = $1
        """, version_id)
        check(version["status"] == "draft", "version created as a draft")
        check(version["version_number"] == 1, "first version numbered 1")

        events = await db.fetch("""
            SELECT event_type FROM generation_events WHERE attempt_id = $1
        """, attempt_id)
        kinds = {e["event_type"] for e in events}
        check("stage_start" in kinds and "complete" in kinds,
              "progress events emitted", f"{len(events)} events")

        job = await db.fetchrow(
            "SELECT status, progress_pct FROM generation_jobs WHERE attempt_id = $1",
            attempt_id)
        check(job["status"] == "complete" and job["progress_pct"] == 100,
              "job finished at 100%", f"{job['status']} {job['progress_pct']}%")

        # --- Validation: chunked by day, no silent truncation ---------------------------
        recorded = await db.fetch("""
            SELECT validator, result FROM validation_results WHERE attempt_id = $1
        """, attempt_id)
        by_name = {r["validator"]: r["result"] for r in recorded}
        check("deterministic" in by_name, "deterministic result recorded")
        check(by_name.get("deterministic") == "pass", "deterministic passed")
        check(len(recorded) == 5, "five validator results recorded",
              f"{len(recorded)}")

        cycle_days = await db.fetchval("""
            SELECT count(DISTINCT lp.day_number)
            FROM timetable_entries te
            JOIN layout_periods lp ON lp.id = te.layout_period_id
            WHERE te.version_id = $1
        """, version_id)
        check(len(StubAI.validator_prompts) == cycle_days * 4,
              "every validator saw every day",
              f"{len(StubAI.validator_prompts)} prompts, "
              f"expected {cycle_days} days x 4 validators")

        # The old bug: a slice of the cycle presented as if it were the whole.
        day_one = await deterministic.summarise_day(version_id, 1)
        actual_day_one = await db.fetchval("""
            SELECT count(*) FROM timetable_entries te
            JOIN layout_periods lp ON lp.id = te.layout_period_id
            WHERE te.version_id = $1 AND lp.day_number = 1
        """, version_id)
        check(len(day_one["entries"]) == actual_day_one,
              "a day chunk holds every entry for that day",
              f"{len(day_one['entries'])} of {actual_day_one}")
        check(day_one["complete"] is True, "chunk is marked complete")
        check(all("not a sample" in p for p in StubAI.validator_prompts),
              "prompt states the day is complete")

        totals = await deterministic.global_summary(version_id)
        check(totals["total_entries"] == summary["entries"],
              "cycle totals cover every entry",
              f"{totals['total_entries']} vs {summary['entries']}")
        check(all(str(cycle_days) in p or "cycle" in p
                  for p in StubAI.validator_prompts[:1]),
              "prompt carries cycle-wide totals")

        # --- A failing validator blocks the generation ------------------------------------
        StubAI.validator_verdict = "fail"
        StubAI.validator_prompts = []
        attempt_fail, _ = await make_attempt(school_id, "Pipeline test veto")
        try:
            await pipeline.run(attempt_fail)
            check(False, "a failing validator blocks the timetable")
        except pipeline.PipelineError as exc:
            check("stub validator" in str(exc).lower(),
                  "failure reason comes from the validator", str(exc)[:70])
            state = await db.fetchval("""
                SELECT status FROM pipeline_state
                WHERE attempt_id = $1 AND stage = 'layer10_validation'
            """, attempt_fail)
            check(state == "failed", "validation stage marked failed", str(state))
        StubAI.validator_verdict = "pass"

        # --- With no AI at all --------------------------------------------------------------
        # Allocation must still work without AI - every layer has a
        # deterministic path. Validation must NOT silently pass: with no
        # validator able to respond, VALIDATION.md requires the run to fail as
        # insufficient_validators rather than shipping an unreviewed timetable.
        StubAI.enabled = False
        attempt2, _ = await make_attempt(school_id, "Pipeline test no AI")
        try:
            await pipeline.run(attempt2)
            check(False, "generation without AI fails at validation")
        except pipeline.PipelineError as exc:
            check("validator" in str(exc).lower(),
                  "no-AI run fails for lack of validators", str(exc)[:70])

        # The allocation itself still happened and is legal - only the quorum
        # was unavailable.
        version2 = await db.fetchval("""
            SELECT id FROM timetable_versions WHERE generation_attempt_id = $1
        """, attempt2)
        check(version2 is not None, "a draft was still produced without AI")
        if version2:
            solution2 = await deterministic.check_solution(str(version2))
            check(solution2.passed,
                  "AI-free allocation is legal on every hard constraint",
                  "; ".join(solution2.failures[:3]))

        results2 = await db.fetch("""
            SELECT validator, result FROM validation_results WHERE attempt_id = $1
        """, attempt2)
        check(any(r["result"] == "error" for r in results2),
              "validator errors recorded rather than ignored",
              f"{len(results2)} rows")
        StubAI.enabled = True

        # --- Failure path -----------------------------------------------------------------
        bad_school = await db.fetchval("""
            INSERT INTO schools (name, timezone)
            VALUES ($1, 'Australia/Sydney') RETURNING id
        """, f"Pipeline Fail Test {uuid.uuid4().hex[:6]}")
        try:
            bad = await deterministic.pre_validate(str(bad_school), str(layout_id))
            check(not bad.passed, "empty school fails pre-validation")
            check(any("teacher" in f.lower() for f in bad.failures),
                  "pre-validation names what is missing",
                  bad.failures[0] if bad.failures else "")
        finally:
            await db.execute("DELETE FROM schools WHERE id = $1", bad_school)

    finally:
        ai_cluster.call = original_call  # type: ignore[assignment]
        await cleanup()
        left = await db.fetchval(
            "SELECT count(*) FROM timetables WHERE name LIKE 'Pipeline test%'")
        check(left == 0, "test data cleaned up", f"{left} left")
        await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nPhase 6 - generation pipeline\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

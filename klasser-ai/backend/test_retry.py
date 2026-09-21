"""
Retry loop tests (PIPELINE.md).

A failed generation must retry from the layer responsible, reuse the layers
before it, vary its choices so the retry is not a repeat, and give up after
allocation_max_loops with the fault correctly attributed.

    python test_retry.py
"""

import asyncio
import json
import logging
import sys
import uuid

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from models import database as db
from services import ai_cluster, pipeline
from services import settings as settings_service

logging.getLogger("klasser.pipeline").setLevel(logging.CRITICAL)
logging.getLogger("klasser.deterministic").setLevel(logging.CRITICAL)

# Names carry a per-run tag so the cleanup assertion is about this run only.
# Without it a crashed earlier run leaves a row behind and fails the next one,
# which reads as a cleanup bug when nothing is wrong.
RUN = uuid.uuid4().hex[:8]

results: list[tuple[bool, str]] = []
timetables: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))


class StubAI:
    """Passes validation unless told otherwise; forces deterministic paths."""
    fail_until_attempt = 0   # validators fail while attempt_number <= this
    attempt_seen = 0

    @classmethod
    async def call(cls, task_key, prompt, **kwargs):
        if task_key.startswith("validator_"):
            if cls.attempt_seen <= cls.fail_until_attempt:
                return json.dumps({
                    "result": "fail",
                    "reason": "Teacher double-booked on this day",
                    "confidence": "high"})
            return json.dumps({"result": "pass", "reason": None,
                               "confidence": "high"})
        if "subject_map" in prompt:
            return json.dumps({"subject_map": []})
        if '"groups"' in prompt:
            return json.dumps({"groups": []})
        return json.dumps({"assignments": {}})


async def sandbox() -> tuple[str, str]:
    school_id = await db.fetchval(
        "SELECT value FROM settings WHERE key = 'sandbox_school_id'")
    layout_id = await db.fetchval("""
        SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
        ORDER BY created_at DESC LIMIT 1
    """, school_id)
    return str(school_id), str(layout_id)


async def make_timetable(name: str) -> str:
    school_id, layout_id = await sandbox()
    tid = str(await db.fetchval("""
        INSERT INTO timetables (school_id, name, layout_id, status)
        VALUES ($1,$2,$3,'processing') RETURNING id
    """, school_id, f"{name} {RUN}", layout_id))
    timetables.append(tid)
    return tid


async def cleanup() -> None:
    for tid in timetables:
        attempts = await db.fetch(
            "SELECT id FROM generation_attempts WHERE timetable_id = $1", tid)
        for a in attempts:
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
        await db.execute(
            "DELETE FROM transport_schedule WHERE timetable_id = $1", tid)
        await db.execute(
            "DELETE FROM timetable_versions WHERE timetable_id = $1", tid)
        await db.execute(
            "DELETE FROM generation_attempts WHERE timetable_id = $1", tid)
        await db.execute("DELETE FROM timetables WHERE id = $1", tid)


async def main_test() -> None:
    await db.connect()
    original = ai_cluster.call
    ai_cluster.call = StubAI.call  # type: ignore[assignment]

    # Track which attempt the stub is answering for.
    original_run = pipeline.run

    async def counting_run(attempt_id, seed=1, **kw):
        StubAI.attempt_seen = seed
        return await original_run(attempt_id, seed=seed, **kw)

    pipeline.run = counting_run  # type: ignore[assignment]

    try:
        # --- Failure routing -------------------------------------------------
        cases = [
            ("Teacher double-booked: 11MAT1 and 11SCI1", "", "layer5_teachers"),
            ("Room double-booked: A and B", "", "layer4_rooms"),
            ("A student has 2 classes in the same period", "", "layer3_period_slots"),
            ("11ENG1 has 40 students in R1, which holds 24", "", "layer4_rooms"),
            ("11ENG1 has 40 students, over the hard maximum of 30", "",
             "layer2_class_groups"),
            ("something unrecognised", "", "layer2_class_groups"),
            ("anything at all", "layer3_period_slots", "layer3_period_slots"),
        ]
        for message, failed_stage, expected in cases:
            got = pipeline.restart_stage(message, failed_stage)
            check(got == expected, f"routes {message[:34]!r}",
                  f"got {got}, expected {expected}")

        # --- Retry succeeds on the second attempt ------------------------------
        StubAI.fail_until_attempt = 1
        tid = await make_timetable("Retry test recovers")
        summary = await pipeline.run_with_retries(tid, "test@example.com")

        check(summary["attempts"] == 2, "recovered on the second attempt",
              f"{summary['attempts']}")
        check(summary["entries"] > 0, "second attempt produced a timetable",
              str(summary["entries"]))

        attempts = await db.fetch("""
            SELECT attempt_number, status, failure_reason
            FROM generation_attempts WHERE timetable_id = $1
            ORDER BY attempt_number
        """, tid)
        check(len(attempts) == 2, "one attempt row per try", f"{len(attempts)}")
        check(attempts[0]["status"] == "failed", "first attempt recorded as failed")
        check(attempts[1]["status"] == "complete", "second recorded as complete")
        check("double-booked" in (attempts[0]["failure_reason"] or "").lower(),
              "failure reason kept on the failed attempt")

        retry_events = await db.fetch("""
            SELECT ge.message, ge.detail FROM generation_events ge
            JOIN generation_attempts ga ON ga.id = ge.attempt_id
            WHERE ga.timetable_id = $1 AND ge.event_type = 'retry'
        """, tid)
        check(len(retry_events) == 1, "a retry event was emitted",
              f"{len(retry_events)}")
        if retry_events:
            detail = json.loads(retry_events[0]["detail"])
            check(detail["from_stage"] == "layer5_teachers",
                  "retry event names the layer it restarted from",
                  detail.get("from_stage"))

        versions = await db.fetchval(
            "SELECT count(*) FROM timetable_versions WHERE timetable_id = $1", tid)
        check(versions == 1, "only the successful attempt left a version",
              f"{versions}")

        # --- Earlier layers are reused, not recomputed ---------------------------
        # This is the point of routing the retry. Without it the loop still
        # recovers, so recovery alone does not prove reuse - an earlier version
        # passed every other check here while silently re-running all nine
        # layers.
        second_id = attempts[1]["id"] if "id" in attempts[1] else None
        second_id = await db.fetchval("""
            SELECT id FROM generation_attempts
            WHERE timetable_id = $1 AND attempt_number = 2
        """, tid)
        ran = {r["stage"] for r in await db.fetch("""
            SELECT stage FROM pipeline_state WHERE attempt_id = $1
        """, second_id)}

        check("layer2_class_groups" not in ran,
              "retry did not re-run class group formation",
              "it ran again" if "layer2_class_groups" in ran else "")
        check("layer3_period_slots" not in ran,
              "retry did not re-run period assignment")
        check("layer5_teachers" in ran, "retry did re-run teacher assignment")
        check("layer10_validation" in ran, "retry re-validated")

        copied = await db.fetchval("""
            SELECT count(*) FROM class_groups WHERE attempt_id = $1
        """, second_id)
        check(copied > 0, "class groups were copied into the retry",
              f"{copied}")

        slots = await db.fetchval("""
            SELECT count(*) FROM class_group_slots WHERE attempt_id = $1
        """, second_id)
        check(slots > 0, "period slots were copied into the retry", f"{slots}")

        orphans = await db.fetchval("""
            SELECT count(*) FROM class_group_slots s
            JOIN class_groups cg ON cg.id = s.class_group_id
            WHERE s.attempt_id = $1 AND cg.attempt_id <> $1
        """, second_id)
        check(orphans == 0,
              "copied slots point at the retry's own class groups", f"{orphans}")

        # --- Retries vary their choices -----------------------------------------
        # Same inputs, different seed, must not make identical assignments.
        StubAI.fail_until_attempt = 0
        tid2 = await make_timetable("Retry test seed A")
        a1 = await pipeline.run_with_retries(tid2, "test@example.com")
        first = await db.fetch("""
            SELECT class_code, teacher_id FROM timetable_entries
            WHERE version_id = $1 ORDER BY class_code
        """, a1["version_id"])

        aid = str(await db.fetchval("""
            INSERT INTO generation_attempts (timetable_id, attempt_number, locked_model)
            VALUES ($1, 9, 'magistral') RETURNING id
        """, tid2))
        await db.execute("""
            INSERT INTO generation_jobs (attempt_id, status, notify_email)
            VALUES ($1,'queued','test@example.com')
        """, aid)
        a2 = await original_run(aid, seed=7)
        second = await db.fetch("""
            SELECT class_code, teacher_id FROM timetable_entries
            WHERE version_id = $1 ORDER BY class_code
        """, a2["version_id"])

        same = [f["teacher_id"] == s["teacher_id"]
                for f, s in zip(first, second)]
        check(not all(same) if len(same) > 4 else True,
              "a different seed makes different choices",
              f"{sum(same)}/{len(same)} assignments identical")

        # --- Gives up after the configured number of loops -------------------------
        StubAI.fail_until_attempt = 99          # never passes
        await settings_service.set_value("allocation_max_loops", "2")
        settings_service.invalidate("allocation_max_loops")

        tid3 = await make_timetable("Retry test exhausted")
        try:
            await pipeline.run_with_retries(tid3, "test@example.com")
            check(False, "gives up after max loops")
        except pipeline.PipelineError as exc:
            check("after 2 attempts" in str(exc),
                  "stops at allocation_max_loops", str(exc)[:60])

        tried = await db.fetchval(
            "SELECT count(*) FROM generation_attempts WHERE timetable_id = $1", tid3)
        check(tried == 2, "exactly max_loops attempts made", f"{tried}")

        status = await db.fetchval(
            "SELECT status FROM timetables WHERE id = $1", tid3)
        check(status == "failed", "timetable marked failed", str(status))

        left = await db.fetchval("""
            SELECT count(*) FROM timetable_versions WHERE timetable_id = $1
        """, tid3)
        check(left == 0, "no draft left behind by failed attempts", f"{left}")

    finally:
        await settings_service.set_value("allocation_max_loops", "5")
        ai_cluster.call = original  # type: ignore[assignment]
        pipeline.run = original_run  # type: ignore[assignment]
        await cleanup()
        remaining = await db.fetchval(
            "SELECT count(*) FROM timetables WHERE name LIKE $1",
            f"Retry test%{RUN}")
        check(remaining == 0, "test data cleaned up", f"{remaining} left")
        await db.disconnect()


def report() -> int:
    width = max(len(m) for _, m in results)
    failed = sum(1 for ok, _ in results if not ok)
    print("\nRetry loop\n")
    for ok, message in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {message.ljust(width)}")
    print()
    print(f"All {len(results)} checks passed." if not failed
          else f"{failed} of {len(results)} checks failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    asyncio.run(main_test())
    sys.exit(report())

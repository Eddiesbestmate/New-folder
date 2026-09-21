"""
Live end-to-end generation against real providers.

Everything else in the suite stubs the AI. This one does not: it runs a full
generation, including the four-validator quorum, against whatever providers are
actually configured, and reports real latency and real token usage.

Mistral is currently unusable (accounts pending verification), so the tasks
normally assigned to Magistral are pointed at their fallbacks for the duration
of the run and restored afterwards. validator_4 is deliberately left on
mistral_small: watching the quorum hold with one validator down is part of the
test.

This spends real API quota.

    python test_live.py [--school "Westfield College"] [--keep]
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import defaultdict

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from models import database as db
from services import ai_cluster, deterministic, pipeline
from services import settings as settings_service

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
                    datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Mistral is down, so its work goes to the models that do answer. Fallbacks are
# pointed at Groq so the fallback path is a real alternative rather than the
# same provider twice.
OVERRIDES = {
    "task_class_group_formation": "gemini",
    "task_block_campus_primary": "gemini",
    "task_block_campus_fallback": "groq",
    "task_teacher_primary": "gemini",
    "task_teacher_fallback": "groq",
}

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{f' - {detail}' if detail else ''}"))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f" - {detail}" if detail else ""))


async def apply_overrides() -> dict[str, str]:
    original = {}
    for key, value in OVERRIDES.items():
        original[key] = await db.fetchval(
            "SELECT value FROM settings WHERE key = $1", key)
        await db.execute(
            "UPDATE settings SET value = $2 WHERE key = $1", key, value)
    settings_service.invalidate()
    return original


async def restore(original: dict[str, str]) -> None:
    for key, value in original.items():
        if value is not None:
            await db.execute(
                "UPDATE settings SET value = $2 WHERE key = $1", key, value)
    settings_service.invalidate()


async def cleanup(timetable_id: str) -> None:
    attempts = await db.fetch(
        "SELECT id FROM generation_attempts WHERE timetable_id = $1", timetable_id)
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
        "DELETE FROM transport_schedule WHERE timetable_id = $1", timetable_id)
    await db.execute(
        "DELETE FROM timetable_versions WHERE timetable_id = $1", timetable_id)
    await db.execute(
        "DELETE FROM generation_attempts WHERE timetable_id = $1", timetable_id)
    await db.execute("DELETE FROM timetables WHERE id = $1", timetable_id)


async def report_usage(timetable_id: str) -> None:
    """Real token usage and latency, straight from pipeline_log."""
    rows = await db.fetch("""
        SELECT stage, model_used, status, tokens_used, latency_ms
        FROM pipeline_log WHERE timetable_id = $1
        ORDER BY created_at
    """, timetable_id)

    if not rows:
        print("\nNo pipeline_log rows - no AI calls were made.")
        return

    by_model: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "ok": 0, "tokens": 0, "ms": 0, "errors": 0,
                 "rate_limited": 0})
    for r in rows:
        m = by_model[r["model_used"] or "?"]
        m["calls"] += 1
        m["ms"] += r["latency_ms"] or 0
        if r["status"] == "success":
            m["ok"] += 1
            m["tokens"] += r["tokens_used"] or 0
        elif r["status"] == "rate_limited":
            m["rate_limited"] += 1
        else:
            m["errors"] += 1

    print(f"\n{'MODEL':<16} {'CALLS':>6} {'OK':>4} {'429':>4} {'ERR':>4} "
          f"{'TOKENS':>9} {'AVG ms':>8}")
    for model in sorted(by_model):
        s = by_model[model]
        avg = s["ms"] // max(1, s["calls"])
        print(f"{model:<16} {s['calls']:>6} {s['ok']:>4} {s['rate_limited']:>4} "
              f"{s['errors']:>4} {s['tokens']:>9,} {avg:>8,}")

    total = sum(s["tokens"] for s in by_model.values())
    print(f"\nTotal tokens actually billed: {total:,} "
          f"across {len(rows)} calls")

    by_stage: dict[str, int] = defaultdict(int)
    for r in rows:
        by_stage[r["stage"]] += r["tokens_used"] or 0
    print(f"\n{'STAGE':<34} {'TOKENS':>9}")
    for stage in sorted(by_stage, key=lambda s: -by_stage[s]):
        print(f"{stage:<34} {by_stage[stage]:>9,}")


async def main_test() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--school", default="Westfield College")
    parser.add_argument("--keep", action="store_true",
                        help="Leave the generated timetable in place")
    args = parser.parse_args()

    await db.connect()
    pools = await ai_cluster.load_key_pools()
    print(f"Provider pools: {pools}\n")

    original = await apply_overrides()
    print("Model assignments for this run:")
    for key in sorted(set(list(OVERRIDES) + ["task_interpret", "validator_1",
                                             "validator_2", "validator_3",
                                             "validator_4"])):
        print(f"  {key:<32} {await settings_service.get(key)}")
    print()

    timetable_id = None
    try:
        school = await db.fetchrow(
            "SELECT id, name FROM schools WHERE name = $1", args.school)
        if school is None:
            print(f"No school named {args.school!r}")
            return

        layout_id = await db.fetchval("""
            SELECT id FROM timetable_layouts WHERE school_id = $1 AND is_active
            ORDER BY created_at DESC LIMIT 1
        """, school["id"])

        pre = await deterministic.pre_validate(str(school["id"]), str(layout_id))
        check(pre.passed, "pre-validation", "; ".join(pre.failures[:2]))
        if not pre.passed:
            return

        timetable_id = str(await db.fetchval("""
            INSERT INTO timetables (school_id, name, layout_id, status)
            VALUES ($1, 'Live model test', $2, 'processing') RETURNING id
        """, school["id"], layout_id))

        print(f"\nGenerating for {school['name']}...\n")
        started = time.monotonic()
        summary = await pipeline.run_with_retries(timetable_id, "live@example.com")
        elapsed = time.monotonic() - started

        print()
        check(summary["entries"] > 0 and summary["class_groups"] > 0,
              "generation produced a timetable",
              f"{summary['entries']} entries, {summary['class_groups']} classes, "
              f"{summary['attempts']} attempt(s), {elapsed:.0f}s")

        version_id = summary["version_id"]

        solution = await deterministic.check_solution(version_id)
        check(solution.passed, "timetable passes every hard constraint",
              "; ".join(solution.failures[:3]))

        vres = await db.fetch("""
            SELECT vr.validator, vr.result, vr.detail
            FROM validation_results vr
            JOIN generation_attempts ga ON ga.id = vr.attempt_id
            WHERE ga.timetable_id = $1
            ORDER BY vr.responded_at
        """, timetable_id)

        print("\nValidators:")
        for r in vres:
            note = f" - {r['detail'][:80]}" if r["detail"] else ""
            print(f"  {r['validator']:<16} {r['result']}{note}")

        responded = [r for r in vres
                     if r["validator"] != "deterministic"
                     and r["result"] in ("pass", "fail")]
        check(len(responded) >= 2, "quorum reached with real models",
              f"{len(responded)} of 4 responded")

        check(any(r["validator"] == "deterministic" and r["result"] == "pass"
                  for r in vres), "deterministic validator passed")

        # Did any layer have to fall back?
        switches = await db.fetch("""
            SELECT ge.message FROM generation_events ge
            JOIN generation_attempts ga ON ga.id = ge.attempt_id
            WHERE ga.timetable_id = $1 AND ge.event_type = 'model_switch'
        """, timetable_id)
        print(f"\nModel switches: {len(switches)}")
        for s in switches:
            print(f"  {s['message']}")

        # Errors are expected while providers are half-configured, but every one
        # must have been survived - the run reaching here proves the fallbacks
        # held. What must not happen is an error with no recorded reason.
        errored = await db.fetch("""
            SELECT model_used, detail FROM pipeline_log
            WHERE timetable_id = $1 AND status = 'error'
        """, timetable_id)
        check(all(e["detail"] for e in errored),
              "every failed AI call recorded why",
              f"{len(errored)} errors, "
              f"{sum(1 for e in errored if not e['detail'])} without a reason")

        await report_usage(timetable_id)

    finally:
        await restore(original)
        if timetable_id and not args.keep:
            await cleanup(timetable_id)
            print("\nCleaned up.")
        elif timetable_id:
            print(f"\nKept timetable {timetable_id}")
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main_test())
    failed = sum(1 for ok, _ in results if not ok)
    print(f"\n{'All' if not failed else failed} "
          f"{'checks passed.' if not failed else f'of {len(results)} checks failed.'}")
    sys.exit(1 if failed else 0)

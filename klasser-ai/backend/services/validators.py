"""
AI quorum validation (VALIDATION.md, PIPELINE.md Layer 10).

Four AI validators plus the deterministic checker review the finished
timetable. The deterministic result is authoritative for hard constraints; the
AI validators judge what deterministic rules cannot - fairness, clustering,
whether anything simply looks like a mistake.

Chunked by day. Each validator sees every entry for one day plus cycle-wide
aggregates, and is told explicitly that the day is complete. Sending a silent
sample of the cycle produced confident "no clashes found" answers from
validators that had seen a fraction of the timetable, which is worse than not
asking at all.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from models import database as db
from services import ai_cluster, deterministic
from services import settings as settings_service

log = logging.getLogger("klasser.validators")

VALIDATOR_KEYS = ["validator_1", "validator_2", "validator_3", "validator_4"]


@dataclass
class ValidatorResult:
    name: str
    model: str
    result: str            # 'pass' | 'fail' | 'rate_limited' | 'error'
    detail: Optional[str] = None
    days_checked: int = 0
    days_total: int = 0
    # Advisory observations. Recorded and shown to the school, never blocking.
    concerns: list[str] = field(default_factory=list)

    @property
    def responded(self) -> bool:
        return self.result in ("pass", "fail")


def build_prompt(day_data: dict, totals: dict, day: int, of_days: int) -> str:
    return f"""You are reviewing one day of a school timetable for problems of
judgement. Clashes have already been ruled out.

This is day {day} of a {of_days}-day cycle. Every entry for this day is listed
below - it is not a sample. Times are given for both classes and duties.

Day {day} timetable (complete):
{json.dumps(day_data["entries"], indent=2, default=str)}

Duties on day {day}:
{json.dumps(day_data["duties"], indent=2, default=str)}

Cycle-wide totals (all {of_days} days):
{json.dumps(totals, indent=2, default=str)}

Teacher, room and student double-bookings have already been checked by exact
code against the full database, and there are none. Do not report them. If a
teacher appears twice, or two classes appear in one room, read the times again
- you are misreading the data, and saying so would be wrong.

Judge these, which code cannot:
- Class sizes that are unreasonable, or vary wildly within one subject
- Workload or duty load that falls unfairly on one teacher across the cycle
- Clustering that would make a bad day for students or staff
- Anything that looks like a mistake rather than a choice

Two different things, reported separately:

"concerns" - things a person should look at. Uneven duty counts, an awkward
cluster, a class that looks too big. These are recorded and shown to the school.
They do NOT stop the timetable being used. Most days have one or two, and that
is normal and fine.

"blocking" - only if the timetable is genuinely unusable as it stands, and you
can point at the exact rows in the data above that prove it. A teacher having
two duties in one day at different times is NOT blocking. Anything you are
inferring, assuming, or describing as "typical" or "potential" is NOT blocking.
If you are unsure, it is a concern, not blocking.

Return ONLY a JSON object:
{{"result": "pass" or "fail",
  "concerns": ["short description", ...],
  "reason": null unless result is "fail", else exactly what is unusable and
            which rows show it,
  "confidence": "high" or "medium"}}

Use "fail" only for blocking. Concerns alone are a "pass"."""


def parse(text: str) -> tuple[str, Optional[str], list[str]]:
    """
    A validator's verdict: result, blocking reason, and advisory concerns.

    Concerns are the point of the split. A validator that notices uneven duty
    loads has seen something real and worth showing a school, but it must not
    stop them using a timetable that breaks no rule.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(cleaned[start:end + 1])

    result = str(data.get("result", "")).lower()
    if result not in ("pass", "fail"):
        raise ValueError(f"validator returned result={result!r}")

    raw = data.get("concerns") or []
    concerns = [str(c)[:200] for c in raw if str(c).strip()][:5] \
        if isinstance(raw, list) else [str(raw)[:200]]

    return result, data.get("reason"), concerns


async def run_one(validator_key: str, version_id: str, days: list[int],
                  totals: dict, timetable_id: str,
                  attempt_id: str) -> ValidatorResult:
    """
    Run one validator across every day.

    Days run sequentially for a given validator so a single provider is not hit
    with the whole cycle at once; the four validators run in parallel, which is
    where the concurrency comes from.

    A failure on any day fails the validator - the first problem found is what
    the school needs to see.
    """
    alias = await settings_service.get(validator_key)
    if not alias:
        return ValidatorResult(validator_key, "?", "error",
                               f"{validator_key} is not assigned to a model")

    checked = 0
    concerns: list[str] = []
    for day in days:
        day_data = await deterministic.summarise_day(version_id, day)
        if not day_data["entries"]:
            continue

        prompt = build_prompt(day_data, totals, day, len(days))
        try:
            text = await ai_cluster.call(
                validator_key, prompt, json_mode=True, temperature=0,
                timetable_id=timetable_id, attempt_id=attempt_id,
                stage=f"validate_{validator_key}_day{day}",
                locked_model=alias)
        except ai_cluster.RateLimited:
            return ValidatorResult(validator_key, alias, "rate_limited",
                                   f"rate limited on day {day}", checked, len(days))
        except ai_cluster.AIError as exc:
            return ValidatorResult(validator_key, alias, "error",
                                   str(exc)[:300], checked, len(days))

        try:
            result, reason, day_concerns = parse(text)
        except (json.JSONDecodeError, ValueError) as exc:
            return ValidatorResult(validator_key, alias, "error",
                                   f"unparseable response on day {day}: {exc}",
                                   checked, len(days))

        checked += 1
        concerns.extend(f"Day {day}: {c}" for c in day_concerns)

        if result == "fail":
            return ValidatorResult(validator_key, alias, "fail",
                                   f"Day {day}: {reason}", checked, len(days),
                                   concerns)

    return ValidatorResult(validator_key, alias, "pass", None, checked,
                           len(days), concerns)


async def run_validation(version_id: str, attempt_id: str,
                         timetable_id: str) -> dict:
    """
    Full validation: the deterministic checker plus the AI quorum.

    Quorum rules (VALIDATION.md):
      - the deterministic check must pass, and can never be overridden
      - at least `quorum_minimum` AI validators must actually respond
      - any single AI fail blocks, regardless of how many passed
    """
    # These were hardcoded while the settings existed and did nothing. Turning
    # off `quorum_require_deterministic` in the dev portal had no effect, which
    # is worse than not offering the control at all.
    require_deterministic = await settings_service.get_bool(
        "quorum_require_deterministic", True)
    any_fail_blocks = await settings_service.get_bool(
        "quorum_any_fail_blocks", True)

    hard = await deterministic.check_solution(version_id)
    await record(attempt_id, "deterministic",
                 "pass" if hard.passed else "fail",
                 None if hard.passed else "; ".join(hard.failures[:5]))

    if not hard.passed and require_deterministic:
        # No point spending tokens on a solution already known to be invalid.
        return {
            "result": "fail",
            "reason": hard.failures[0],
            "source": "deterministic",
            "validators": [],
        }

    if not hard.passed:
        log.warning(
            "Deterministic check failed but quorum_require_deterministic is "
            "off - continuing to the AI validators: %s", hard.failures[0])

    days = await deterministic.validation_days(version_id)
    totals = await deterministic.global_summary(version_id)

    results: list[ValidatorResult] = list(await asyncio.gather(*[
        run_one(key, version_id, days, totals, timetable_id, attempt_id)
        for key in VALIDATOR_KEYS
    ]))

    for r in results:
        await record(attempt_id, r.model or r.name, r.result, r.detail)

    minimum = await settings_service.get_int("quorum_minimum", 2)
    responded = [r for r in results if r.responded]
    failed = [r for r in responded if r.result == "fail"]

    # Everything the validators noticed but did not consider blocking. These
    # are the observations that used to fail a valid timetable - uneven duty
    # loads, an awkward cluster. Worth showing a school; never worth refusing
    # to give them a timetable over.
    concerns = [c for r in results for c in r.concerns]

    summary = {
        "validators": [
            {"validator": r.name, "model": r.model, "result": r.result,
             "detail": r.detail, "days_checked": r.days_checked,
             "days_total": r.days_total, "concerns": r.concerns}
            for r in results
        ],
        "days": len(days),
        "responded": len(responded),
        "quorum_minimum": minimum,
        "deterministic_passed": hard.passed,
        "concerns": concerns,
    }

    if concerns:
        log.info("Validators raised %s advisory concern(s)", len(concerns))

    if failed and any_fail_blocks:
        return {**summary, "result": "fail", "reason": failed[0].detail,
                "source": failed[0].model}

    if failed:
        # Majority rules instead: a single dissenter no longer blocks.
        passing = [r for r in responded if r.result == "pass"]
        if len(failed) >= len(passing):
            return {**summary, "result": "fail", "reason": failed[0].detail,
                    "source": failed[0].model}
        log.warning("%s validator(s) failed but quorum_any_fail_blocks is off "
                    "and %s passed - accepting", len(failed), len(passing))

    if len(responded) < minimum:
        return {**summary, "result": "insufficient_validators",
                "reason": f"Only {len(responded)} of {len(results)} validators "
                          f"responded; {minimum} are required.",
                "source": "quorum"}

    return {**summary, "result": "pass", "reason": None, "source": "quorum"}


async def record(attempt_id: str, validator: str, result: str,
                 detail: Optional[str]) -> None:
    try:
        await db.execute("""
            INSERT INTO validation_results (attempt_id, validator, result, detail)
            VALUES ($1, $2, $3, $4)
        """, attempt_id, validator[:40], result, (detail or "")[:2000] or None)
    except Exception:
        log.exception("Could not record validation result")

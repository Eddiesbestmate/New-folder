"""
The free sample run.

A school that has just signed up has no teachers, no rooms and no layout, so
there is nothing to generate. This runs a real generation against the seeded
demonstration school instead - Westfield College, two campuses, 48 students,
buses and duties - and grants the new school read-only sight of that one
timetable.

Three rules, and they are the whole design:

**It runs on the sandbox school's data, not theirs.** Copying 48 sample students
into a real school would leave them deleting demo data before they could start,
and anyone who abandoned setup would be left with a fake roll.

**The grant is read-only and singular.** One timetable, recorded on their
onboarding row. It can never be published, edited or exported as their own -
those paths ask for write access, which only the owning school has.

**It is free and once.** No credits are reserved or settled. `sandbox_used`
stops a second one, so the cost of a curious visitor is bounded at one run.
"""

import logging
from typing import Optional

from models import database as db
from services import deterministic, queue
from services import settings as settings_service

log = logging.getLogger("klasser.sandbox")


class SandboxError(Exception):
    def __init__(self, message: str, *, code: str = "sandbox_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


async def sandbox_school_id() -> Optional[str]:
    return await settings_service.get("sandbox_school_id")


async def granted_timetable(school_id: str) -> Optional[str]:
    """
    The one sandbox timetable this school may read, if any.

    Called on every output request from a school viewing its sample, so it is a
    single indexed lookup and nothing more.
    """
    value = await db.fetchval("""
        SELECT sandbox_timetable_id FROM onboarding WHERE school_id = $1
    """, school_id)
    return str(value) if value else None


async def state(school_id: str) -> dict:
    """What the dashboard needs to decide what to offer."""
    row = await db.fetchrow("""
        SELECT sandbox_used, sandbox_used_at, sandbox_timetable_id
        FROM onboarding WHERE school_id = $1
    """, school_id)

    if row is None:
        return {"available": False, "used": False, "timetable_id": None,
                "reason": "This school has no onboarding record."}

    timetable_id = (str(row["sandbox_timetable_id"])
                    if row["sandbox_timetable_id"] else None)

    status = version_id = None
    if timetable_id:
        found = await db.fetchrow("""
            SELECT t.status,
                   (SELECT v.id FROM timetable_versions v
                     WHERE v.timetable_id = t.id
                     ORDER BY v.version_number DESC LIMIT 1) AS version_id
            FROM timetables t WHERE t.id = $1
        """, timetable_id)
        if found:
            status = found["status"]
            version_id = str(found["version_id"]) if found["version_id"] else None

    return {
        "available": not row["sandbox_used"],
        "used": bool(row["sandbox_used"]),
        "used_at": row["sandbox_used_at"],
        "timetable_id": timetable_id,
        "status": status,
        "version_id": version_id,
    }


async def start(school_id: str, user: dict) -> dict:
    """
    Queue the free run. Charges nothing.

    `sandbox_used` is set in the same transaction that creates the timetable, so
    two clicks - or two tabs - cannot both get a run.
    """
    sandbox_id = await sandbox_school_id()
    if not sandbox_id:
        raise SandboxError(
            "No demonstration school is configured on this instance.",
            code="no_sandbox")

    if str(sandbox_id) == str(school_id):
        raise SandboxError(
            "This is the demonstration school. Generate normally instead.",
            code="is_sandbox")

    layout_id = await db.fetchval("""
        SELECT id FROM timetable_layouts
        WHERE school_id = $1 AND is_active = true
        ORDER BY created_at DESC LIMIT 1
    """, sandbox_id)
    if layout_id is None:
        raise SandboxError(
            "The demonstration school has no timetable layout.",
            code="no_layout")

    # The same pre-validation a real run gets. If the seeded school cannot
    # produce a timetable, that is a fault on this instance and the school
    # should be told plainly rather than watching a run fail.
    check = await deterministic.pre_validate(str(sandbox_id), str(layout_id))
    if not check.passed:
        log.error("Sandbox school failed pre-validation: %s", check.failures)
        raise SandboxError(
            "The demonstration school is not ready on this instance. "
            "This is our fault, not yours - support has been notified.",
            code="sandbox_unhealthy")

    school_name = await db.fetchval(
        "SELECT name FROM schools WHERE id = $1", school_id)

    async with db.transaction() as conn:
        # Claim the single run first. If another request already has it, the
        # UPDATE matches nothing and we stop before creating anything.
        claimed = await conn.fetchval("""
            UPDATE onboarding
            SET sandbox_used = true, sandbox_used_at = now(), updated_at = now()
            WHERE school_id = $1 AND sandbox_used = false
            RETURNING school_id
        """, school_id)
        if not claimed:
            raise SandboxError(
                "This school has already had its free sample run.",
                code="already_used")

        timetable_id = await conn.fetchval("""
            INSERT INTO timetables (school_id, name, layout_id, status)
            VALUES ($1, $2, $3, 'processing') RETURNING id
        """, sandbox_id, f"Sample for {school_name}", layout_id)

        await conn.execute("""
            UPDATE onboarding SET sandbox_timetable_id = $2 WHERE school_id = $1
        """, school_id, timetable_id)

    # Queued as the sandbox school, because that is whose data it runs on - the
    # worker loads teachers, rooms and students by the timetable's school_id.
    # Urgent priority: someone is watching this one, deciding whether to buy.
    job_id = await queue.enqueue(
        "generate", str(sandbox_id),
        timetable_id=str(timetable_id),
        payload={"notify_email": user["email"], "sandbox_for": str(school_id)},
        priority=queue.PRIORITY_URGENT)

    if job_id is None:
        # Nothing was spent, so give the run back rather than burning it.
        async with db.transaction() as conn:
            await conn.execute("""
                UPDATE onboarding
                SET sandbox_used = false, sandbox_used_at = NULL,
                    sandbox_timetable_id = NULL
                WHERE school_id = $1
            """, school_id)
            await conn.execute("DELETE FROM timetables WHERE id = $1",
                               timetable_id)
        raise SandboxError("Could not queue the sample run. Try again.",
                           code="queue_failed")

    log.info("Sample run queued for %s on the sandbox school (timetable %s)",
             school_name, timetable_id)

    waiting = await queue.stats()
    return {
        "timetable_id": str(timetable_id),
        "job_id": job_id,
        "queued_ahead": max(0, waiting["queued"] - 1),
        "credits_charged": 0,
    }


async def is_sample(version_id: str) -> bool:
    """Whether a version came from a sample run, for the banner on the output."""
    owner = await db.fetchval("""
        SELECT v.school_id FROM timetable_versions v WHERE v.id = $1
    """, version_id)
    if owner is None:
        return False
    return str(owner) == str(await sandbox_school_id())


async def may_read(timetable_id: Optional[str], school_id: str) -> bool:
    """Whether this school was granted sight of this sandbox timetable."""
    if not timetable_id:
        return False
    return await granted_timetable(school_id) == str(timetable_id)

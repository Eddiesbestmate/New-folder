"""
Timetable versions - publish, roll back, discard and compare (VERSIONING.md).

A generation leaves a draft. Publishing makes it the live timetable and archives
whatever was live before. Rolling back is the same operation applied to an
archived version, so there is one code path for "make this the live one" rather
than two that can drift apart.

The invariant this module exists to hold: **at most one published version per
timetable, at any instant**. Archiving the old one and publishing the new one
happen in a single transaction, so there is never a moment when a school has two
live timetables or none.
"""

import logging
from typing import Optional

import config
from models import database as db

log = logging.getLogger("klasser.versioning")


class VersioningError(Exception):
    def __init__(self, message: str, *, code: str = "versioning_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --- Publishing --------------------------------------------------------------

async def publish(version_id: str, user: dict,
                  notes: Optional[str] = None) -> dict:
    """
    Make this version the live one, archiving the current published version.

    Works on a draft (normal publish) or an archived version (a rollback). A
    version that is already published is left alone rather than being
    republished, so the publish timestamp keeps meaning what it says.
    """
    version = await db.fetchrow("""
        SELECT id, timetable_id, version_number, status
        FROM timetable_versions WHERE id = $1 AND school_id = $2
    """, version_id, user["school_id"])
    if version is None:
        raise VersioningError("Timetable version not found", code="not_found")

    if version["status"] == "published":
        return {"status": "published", "unchanged": True,
                "version_number": version["version_number"]}

    entries = await db.fetchval(
        "SELECT count(*) FROM timetable_entries WHERE version_id = $1", version_id)
    if not entries:
        # An empty version would take the school's live timetable away.
        raise VersioningError(
            "This version has no entries, so publishing it would leave the "
            "school with an empty timetable.", code="empty_version")

    async with db.transaction() as conn:
        previous = await conn.fetchval("""
            UPDATE timetable_versions SET status = 'archived'
            WHERE timetable_id = $1 AND status = 'published' AND id <> $2
            RETURNING version_number
        """, version["timetable_id"], version_id)

        await conn.execute("""
            UPDATE timetable_versions
            SET status = 'published', published_at = now(), published_by = $2,
                notes = COALESCE($3, notes)
            WHERE id = $1
        """, version_id, user["id"], notes)

    rollback = version["status"] == "archived"
    log.info("Version %s of timetable %s %s by %s (replaced v%s)",
             version["version_number"], version["timetable_id"],
             "rolled back to" if rollback else "published", user["email"],
             previous)

    # Everyone at the school, not just whoever clicked: a published timetable
    # is the one people are about to teach from.
    try:
        from services import email

        row = await db.fetchrow("""
            SELECT t.name AS timetable, s.name AS school
            FROM timetables t JOIN schools s ON s.id = t.school_id
            WHERE t.id = $1
        """, version["timetable_id"])

        await email.notify_school(
            user["school_id"], "notify_version_published", "version_published",
            {
                "school_name": row["school"] if row else "your school",
                "timetable_name": row["timetable"] if row else "your timetable",
                "version_number": version["version_number"],
                "published_by": f"{user.get('first_name', '')} "
                                f"{user.get('surname', '')}".strip()
                                or user["email"],
                "notes": notes or "",
                "view_url": f"{config.APP_URL}/output.html?version={version_id}",
            })
    except Exception:  # noqa: BLE001
        log.exception("Could not send the published email for %s", version_id)

    return {"status": "published", "version_number": version["version_number"],
            "replaced_version": previous, "was_rollback": rollback}


async def discard(version_id: str, user: dict) -> dict:
    """
    Throw away a draft.

    Only a draft: an archived version is part of the school's history and a
    published one is their live timetable. Deleting either by mistake is not
    recoverable, so the status check is the guard rather than a confirmation
    dialog in the browser.
    """
    version = await db.fetchrow("""
        SELECT id, timetable_id, version_number, status
        FROM timetable_versions WHERE id = $1 AND school_id = $2
    """, version_id, user["school_id"])
    if version is None:
        raise VersioningError("Timetable version not found", code="not_found")

    if version["status"] != "draft":
        raise VersioningError(
            f"Only a draft can be discarded. This version is {version['status']}.",
            code="not_a_draft")

    async with db.transaction() as conn:
        await conn.execute("""
            DELETE FROM timetable_entry_students
            WHERE timetable_entry_id IN (
                SELECT id FROM timetable_entries WHERE version_id = $1)
        """, version_id)
        for sql in (
            "DELETE FROM timetable_entries WHERE version_id = $1",
            "DELETE FROM duty_assignments WHERE version_id = $1",
            "DELETE FROM transport_schedule WHERE version_id = $1",
            "DELETE FROM timetable_edits WHERE version_id = $1",
            "DELETE FROM timetable_versions WHERE id = $1",
        ):
            await conn.execute(sql, version_id)

    log.info("Draft version %s of timetable %s discarded by %s",
             version["version_number"], version["timetable_id"], user["email"])
    return {"discarded": True, "version_number": version["version_number"]}


# --- Comparison --------------------------------------------------------------

async def compare(version_a: str, version_b: str, school_id: str) -> dict:
    """
    What changed between two versions.

    Done here rather than in the browser (as VERSIONING.md sketches) because
    the comparison needs names, not ids - "Room C14 to Room B02" is useful and
    two UUIDs are not - and a large timetable is thousands of entries, which is
    a lot to ship twice and diff in JavaScript.

    Entries are matched on class code plus day, the pair that identifies one
    meeting of a class across versions. An entry present in one and not the
    other is reported as added or removed.
    """
    # Looked up one at a time rather than with ANY(): comparing a version with
    # itself is a reasonable thing to ask for, and a set-based lookup collapses
    # the duplicate into one row and reads as "not found".
    async def describe_version(version_id: str) -> dict:
        row = await db.fetchrow("""
            SELECT id, version_number, status, created_at
            FROM timetable_versions WHERE id = $1 AND school_id = $2
        """, version_id, school_id)
        if row is None:
            raise VersioningError(
                "Both versions must exist and belong to this school",
                code="not_found")
        return dict(row) | {"id": str(row["id"])}

    info_a = await describe_version(version_a)
    info_b = await describe_version(version_b)

    async def entries(version_id: str) -> dict[tuple, dict]:
        rows = await db.fetch("""
            SELECT te.id, te.class_code, te.subject, te.day_number,
                   te.layout_period_id, te.teacher_id, te.room_id,
                   lp.period_number, lp.label AS period_label,
                   t.full_name AS teacher, r.name AS room, c.name AS campus
            FROM timetable_entries te
            JOIN layout_periods lp ON lp.id = te.layout_period_id
            JOIN teachers t ON t.id = te.teacher_id
            JOIN rooms r ON r.id = te.room_id
            JOIN campuses c ON c.id = te.campus_id
            WHERE te.version_id = $1
            ORDER BY te.class_code, te.day_number, lp.period_number
        """, version_id)

        # A class can meet twice on one day, so the period number is part of
        # the key for the second and later meetings - otherwise one of them
        # would silently disappear from the diff.
        out: dict[tuple, dict] = {}
        seen: dict[tuple, int] = {}
        for row in rows:
            base = (row["class_code"], row["day_number"])
            n = seen.get(base, 0)
            seen[base] = n + 1
            out[base + (n,)] = dict(row) | {"id": str(row["id"])}
        return out

    left = await entries(version_a)
    right = await entries(version_b)

    changes: list[dict] = []

    for key, before in left.items():
        after = right.get(key)
        if after is None:
            changes.append({"type": "removed", "class_code": before["class_code"],
                            "day": before["day_number"], "before": _slot(before),
                            "after": None})
            continue

        for field, kind in (("period_number", "moved"),
                            ("teacher_id", "teacher_changed"),
                            ("room_id", "room_changed")):
            if before[field] != after[field]:
                changes.append({
                    "type": kind, "class_code": before["class_code"],
                    "day": before["day_number"],
                    "before": _slot(before), "after": _slot(after),
                })

    for key, after in right.items():
        if key not in left:
            changes.append({"type": "added", "class_code": after["class_code"],
                            "day": after["day_number"], "before": None,
                            "after": _slot(after)})

    counts: dict[str, int] = {}
    for change in changes:
        counts[change["type"]] = counts.get(change["type"], 0) + 1

    return {
        "a": info_a, "b": info_b,
        "entries": {"a": len(left), "b": len(right)},
        "identical": not changes,
        "counts": counts,
        "changes": sorted(changes, key=lambda c: (c["class_code"], c["day"])),
    }


def _slot(entry: dict) -> dict:
    """One entry, as a school would read it back."""
    return {
        "entry_id": entry["id"],
        "day": entry["day_number"],
        "period": entry["period_number"],
        "period_label": entry["period_label"] or f"P{entry['period_number']}",
        "teacher": entry["teacher"],
        "room": entry["room"],
        "campus": entry["campus"],
        "subject": entry["subject"],
    }

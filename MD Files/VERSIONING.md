# Timetable Versioning

## Overview

Each time a timetable is generated, a new version is created in draft status. Versions can be compared, published, and archived. Only one version is active (published) at a time.

---

## Version lifecycle

```
Generation completes → timetable_version created (status = 'draft')
User reviews draft
User publishes → status = 'published'
               → previous published version → status = 'archived'
               → send version_published email
User can view any version at any time
User can roll back by publishing an archived version
```

---

## Version numbering

```python
async def get_next_version_number(timetable_id: str) -> int:
    result = await db.fetchrow("""
        SELECT COALESCE(MAX(version_number), 0) + 1 as next
        FROM timetable_versions WHERE timetable_id = $1
    """, timetable_id)
    return result['next']
```

---

## Publishing a version

```python
async def publish_version(version_id: str, notes: str, current_user):
    async with db.transaction():
        # Archive current published version
        await db.execute("""
            UPDATE timetable_versions
            SET status = 'archived'
            WHERE timetable_id = (
                SELECT timetable_id FROM timetable_versions WHERE id = $1
            )
            AND status = 'published'
        """, version_id)

        # Publish new version
        await db.execute("""
            UPDATE timetable_versions
            SET status = 'published',
                published_at = now(),
                published_by = $1,
                notes = $2
            WHERE id = $3
        """, current_user['id'], notes, version_id)

    await send_if_preferred(current_user['id'], 'notify_version_published', ...)
```

---

## Version comparison

Frontend fetches two versions and diffs them:

```javascript
async function compareVersions(versionIdA, versionIdB) {
    const [a, b] = await Promise.all([
        apiCall('GET', `/timetables/version/${versionIdA}/entries`),
        apiCall('GET', `/timetables/version/${versionIdB}/entries`),
    ]);

    const changes = [];
    for (const entryA of a) {
        const entryB = b.find(e => e.class_code === entryA.class_code
                                && e.day_number === entryA.day_number);
        if (!entryB) {
            changes.push({ type: 'removed', entry: entryA });
        } else {
            if (entryA.layout_period_id !== entryB.layout_period_id) changes.push({ type: 'moved', from: entryA, to: entryB });
            if (entryA.teacher_id !== entryB.teacher_id) changes.push({ type: 'teacher_changed', from: entryA, to: entryB });
            if (entryA.room_id !== entryB.room_id) changes.push({ type: 'room_changed', from: entryA, to: entryB });
        }
    }
    return changes;
}
```

Displayed as a colour-coded diff table: added (green), removed (red), changed (amber).

---

## As built

`services/versioning.py`. Four differences from the sketches above:

**Comparison is computed server-side**, at
`GET /timetables/version/{a}/compare/{b}`, not in the browser as sketched. The
diff needs names to be readable — "Room C14 to Room B02", not two UUIDs — and a
large timetable is thousands of entries to ship twice and diff in JavaScript.

**Entries are matched on class code, day, *and* occurrence.** A class can meet
twice on the same day; keying on `(class_code, day_number)` alone silently drops
one of the two meetings from the diff.

**Publishing is scoped to one timetable, not one school.** A school can run
several timetables — different terms, say — each with its own live version. The
invariant is one published version *per timetable*.

**Rollback is publish.** Publishing an archived version archives whatever was
live and restores it; there is one code path for "make this the live one" rather
than two that can drift apart. The response says `was_rollback` so the UI can
word it correctly. Republishing the version that is already live is a no-op, so
the publish timestamp keeps meaning what it says.

Also here: `discard`, which deletes a draft and its entries. Only a draft —
an archived version is the school's history and a published one is their live
timetable, and neither deletion is recoverable. Publishing an empty version is
refused for the same reason: it would take the school's timetable away.

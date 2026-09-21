"""
Notification preferences (NOTIFICATIONS.md).

Which emails a user wants. Nothing here sends anything - Phase 12 owns that -
but `wants()` and `recipients()` are the functions it will call, and they are
written and tested now so the send path has something to ask.

Preference columns are real columns rather than a JSON blob, so a typo in a key
is a database error rather than a silently ignored setting. The cost is that
adding a preference needs a migration; the benefit is that
`notify_generation_complete` can never quietly become `notify_generation_completed`.
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from models import database as db
from routers.auth import CurrentUser

log = logging.getLogger("klasser.notifications")

router = APIRouter(tags=["notifications"])

# The allowlist. Every key is a column on notification_preferences, and nothing
# reaches SQL that is not on this list - the update statement builds column
# names, so this is a security boundary and not just documentation.
PREFERENCES: dict[str, tuple[str, str, str]] = {
    # key: (group, label, what triggers it)
    "notify_generation_complete": (
        "Generation", "Timetable complete",
        "A generation finishes successfully"),
    "notify_generation_failed": (
        "Generation", "Generation failed",
        "A generation fails after every retry"),
    "notify_generation_paused": (
        "Generation", "Generation paused",
        "A run is paused for low credits or a declined card"),
    "notify_low_balance": (
        "Billing", "Low balance warning",
        "The balance drops below your threshold"),
    "notify_purchase_receipt": (
        "Billing", "Credit purchase receipt",
        "A credit package is bought"),
    "notify_payg_charged": (
        "Billing", "Pay as you go receipt",
        "A card is charged after a generation"),
    "notify_payg_failed": (
        "Billing", "Pay as you go declined",
        "A card is declined"),
    "notify_invoice_sent": (
        "Billing", "Invoice sent",
        "A monthly invoice is raised"),
    "notify_invoice_overdue": (
        "Billing", "Invoice overdue",
        "An invoice passes its due date"),
    "notify_account_blocked": (
        "Billing", "Account blocked",
        "The account is blocked for an unpaid balance"),
    "notify_version_published": (
        "Timetable", "Version published",
        "Anyone at your school publishes a version"),
    "notify_edit_applied": (
        "Timetable", "Edit applied",
        "An AI-assisted edit is applied"),
    "notify_user_invited": (
        "Team", "User invited",
        "Someone is invited to your school"),
    "notify_user_joined": (
        "Team", "User joined",
        "An invited user accepts and joins"),
}

GROUP_ORDER = ["Generation", "Billing", "Timetable", "Team"]

# Built once from PREFERENCES so the statement and the allowlist cannot drift.
# The result is a constant by the time any request runs - no per-request string
# building, and nothing derived from input ever reaches SQL.
ORDERED_KEYS = list(PREFERENCES)
UPDATE_SQL = (
    "UPDATE notification_preferences SET "
    + ", ".join(f"{key} = coalesce(${i + 2}, {key})"
                for i, key in enumerate(ORDERED_KEYS))
    + ", updated_at = now() WHERE user_id = $1"
)


class PreferencesIn(BaseModel):
    """A partial update: only the keys present are changed."""
    preferences: dict[str, bool]


async def _row(user_id: str) -> dict:
    """
    This user's preferences, creating the row if it is missing.

    Signup provisions one, but a user created before that did, or by a direct
    insert, would otherwise have no row - and a missing row must mean "wants
    everything" rather than an error on a settings page.
    """
    row = await db.fetchrow(
        "SELECT * FROM notification_preferences WHERE user_id = $1", user_id)
    if row is None:
        await db.execute("""
            INSERT INTO notification_preferences (user_id) VALUES ($1)
            ON CONFLICT DO NOTHING
        """, user_id)
        row = await db.fetchrow(
            "SELECT * FROM notification_preferences WHERE user_id = $1", user_id)
    return dict(row) if row else {}


@router.get("")
async def get_preferences(user: CurrentUser) -> dict:
    """Current settings, grouped the way the page lays them out."""
    row = await _row(user["id"])

    groups = []
    for name in GROUP_ORDER:
        items = [
            {"key": key, "label": label, "detail": detail,
             "enabled": bool(row.get(key, True))}
            for key, (group, label, detail) in PREFERENCES.items()
            if group == name
        ]
        if items:
            groups.append({"group": name, "items": items})

    return {
        "preferences": {k: bool(row.get(k, True)) for k in PREFERENCES},
        "groups": groups,
        "email": user["email"],
    }


@router.put("")
async def set_preferences(payload: PreferencesIn, user: CurrentUser) -> dict:
    """
    Save changed toggles. The page sends only what moved.

    Unknown keys are refused rather than ignored: a typo in the frontend would
    otherwise look like it saved and silently do nothing.
    """
    unknown = sorted(set(payload.preferences) - set(PREFERENCES))
    if unknown:
        raise HTTPException(422, f"Unknown preference(s): {', '.join(unknown)}")
    if not payload.preferences:
        return {"updated": 0}

    await _row(user["id"])

    # Every column named, every value a placeholder, nothing interpolated. A
    # key the request did not send arrives as NULL and coalesce leaves it alone,
    # so a partial update needs no dynamic SQL. The statement is verbose, but
    # adding a preference then fails loudly here instead of silently not saving.
    values = [payload.preferences.get(key) for key in ORDERED_KEYS]
    await db.execute(UPDATE_SQL, user["id"], *values)

    keys = sorted(payload.preferences)
    log.info("%s updated %s notification preference(s)", user["email"], len(keys))
    return {"updated": len(keys), "keys": keys}


# --- What the email layer will ask -------------------------------------------

async def wants(user_id: str, preference_key: str) -> bool:
    """
    Whether one user wants this email. Defaults to yes.

    A missing row or an unknown key means send: failing to warn a school that
    their account is blocked is worse than one unwanted email.
    """
    if preference_key not in PREFERENCES:
        log.warning("Unknown notification preference %r - defaulting to send",
                    preference_key)
        return True

    # The whole row, then the key read in Python: no column name reaches SQL.
    row = await db.fetchrow(
        "SELECT * FROM notification_preferences WHERE user_id = $1", user_id)
    if row is None:
        return True
    value = dict(row).get(preference_key)
    return True if value is None else bool(value)


async def recipients(school_id: str, preference_key: str) -> list[dict]:
    """
    Everyone at a school who wants this email.

    Inactive users are excluded - a deactivated account should stop receiving
    mail without anyone having to remember to turn its preferences off.
    """
    known = preference_key in PREFERENCES
    if not known:
        log.warning("Unknown notification preference %r - notifying everyone",
                    preference_key)

    # Fixed SQL; the preference is applied in Python. Filtering in the query
    # would mean putting a column name into it, and this is not a hot path -
    # it runs once per notification, over one school's users.
    rows = await db.fetch("""
        SELECT u.id, u.email, u.first_name, u.surname, u.role, np.*
        FROM users u
        LEFT JOIN notification_preferences np ON np.user_id = u.id
        WHERE u.school_id = $1 AND u.is_active = true
        ORDER BY u.created_at
    """, school_id)

    people = []
    for row in rows:
        record = dict(row)
        # A missing preferences row means the user has never changed anything,
        # which means they want everything.
        if known and record.get(preference_key) is False:
            continue
        people.append({
            "id": str(record["id"]), "email": record["email"],
            "first_name": record["first_name"], "surname": record["surname"],
            "role": record["role"],
        })
    return people


async def owner_of(school_id: str) -> Optional[dict]:
    """The account owner, for the emails only they should get."""
    row = await db.fetchrow("""
        SELECT id, email, first_name, surname FROM users
        WHERE school_id = $1 AND role = 'owner' AND is_active = true
        ORDER BY created_at LIMIT 1
    """, school_id)
    return dict(row) | {"id": str(row["id"])} if row else None

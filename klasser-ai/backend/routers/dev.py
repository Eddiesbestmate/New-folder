"""
Dev portal - API key pools, model assignments and system settings.

Every endpoint requires role = 'dev'. These are not school-scoped: they
configure the platform itself.

A provider key, once stored, is never returned by any endpoint. Listing shows
a masked hint only. There is no "reveal" endpoint by design - if a key is lost,
rotate it at the provider.
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

import config
from models import database as db
from routers.auth import require_dev_role
from services import ai_cluster, billing, encryption
from services import settings as settings_service

log = logging.getLogger("klasser.dev")

router = APIRouter(tags=["dev"])

DevUser = Annotated[dict, Depends(require_dev_role)]


class KeyIn(BaseModel):
    provider: str
    key_label: str = Field(min_length=1, max_length=60)
    api_key: str = Field(min_length=8, max_length=500)
    account_label: Optional[str] = Field(default=None, max_length=120)

    @field_validator("provider")
    @classmethod
    def known_provider(cls, v: str) -> str:
        if v not in config.PROVIDER_URLS:
            raise ValueError(f"provider must be one of {sorted(config.PROVIDER_URLS)}")
        return v

    @field_validator("api_key")
    @classmethod
    def looks_like_a_key(cls, v: str) -> str:
        v = v.strip()
        if " " in v:
            raise ValueError("API key should not contain spaces")
        return v


class SettingIn(BaseModel):
    value: str = Field(max_length=500)


# --- API key pools -----------------------------------------------------------

@router.get("/keys")
async def list_keys(user: DevUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT id, provider, key_label, account_label, is_active, updated_at
        FROM api_keys ORDER BY provider, key_label
    """)

    loaded = ai_cluster.pool_status()
    return [
        {
            "id": str(r["id"]),
            "provider": r["provider"],
            "key_label": r["key_label"],
            "account_label": r["account_label"],
            "is_active": r["is_active"],
            "updated_at": r["updated_at"],
            "loaded_in_pool": loaded.get(r["provider"], 0),
        }
        for r in rows
    ]


@router.post("/keys", status_code=status.HTTP_201_CREATED)
async def add_key(payload: KeyIn, user: DevUser) -> dict:
    if not encryption.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ENCRYPTION_KEY is not configured - cannot store provider keys")

    dupe = await db.fetchval("""
        SELECT 1 FROM api_keys WHERE provider = $1 AND key_label = $2
    """, payload.provider, payload.key_label)
    if dupe:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{payload.provider} already has a key labelled '{payload.key_label}'")

    key_id = await db.fetchval("""
        INSERT INTO api_keys (provider, key_label, encrypted_key, account_label)
        VALUES ($1, $2, $3, $4) RETURNING id
    """, payload.provider, payload.key_label,
        encryption.encrypt_key(payload.api_key), payload.account_label)

    await ai_cluster.load_key_pools()
    log.info("Dev %s added %s key '%s'", user["email"], payload.provider,
             payload.key_label)

    # The masked hint is the only form of the key that ever leaves the server.
    return {
        "id": str(key_id),
        "hint": encryption.mask(payload.api_key),
        "pools": ai_cluster.pool_status(),
    }


@router.put("/keys/{key_id}/active")
async def set_key_active(key_id: str, active: bool, user: DevUser) -> dict:
    updated = await db.fetchval("""
        UPDATE api_keys SET is_active = $2, updated_at = now()
        WHERE id = $1 RETURNING id
    """, key_id, active)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")

    await ai_cluster.load_key_pools()
    return {"is_active": active, "pools": ai_cluster.pool_status()}


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(key_id: str, user: DevUser) -> None:
    """
    Remove a key.

    error_log and pipeline_log reference api_keys, so those references are
    cleared first - the history is kept, it just no longer names a key that
    does not exist.
    """
    row = await db.fetchrow(
        "SELECT provider, key_label FROM api_keys WHERE id = $1", key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")

    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE pipeline_log SET api_key_id = NULL WHERE api_key_id = $1", key_id)
        await conn.execute(
            "UPDATE error_log SET api_key_id = NULL WHERE api_key_id = $1", key_id)
        await conn.execute("DELETE FROM api_keys WHERE id = $1", key_id)

    await ai_cluster.load_key_pools()
    log.info("Dev %s deleted %s key '%s'", user["email"],
             row["provider"], row["key_label"])


@router.get("/pools")
async def pools(user: DevUser) -> dict:
    """What the running process currently holds in memory."""
    await ai_cluster.ensure_loaded()
    return {
        "encryption_configured": encryption.is_configured(),
        "pools": ai_cluster.pool_status(),
        "providers": sorted(config.PROVIDER_URLS),
    }


@router.post("/pools/reload")
async def reload_pools(user: DevUser) -> dict:
    return {"pools": await ai_cluster.load_key_pools()}


@router.post("/providers/{alias}/test")
async def test_provider(alias: str, user: DevUser) -> dict:
    """Send one trivial prompt to confirm the provider actually answers."""
    return await ai_cluster.test_provider(alias)


# --- Model assignments and settings ------------------------------------------

@router.get("/models")
async def model_assignments(user: DevUser) -> dict:
    tasks = await settings_service.get_many("task_")
    validators = await settings_service.get_many("validator_")
    models = await settings_service.get_many("model_")

    return {
        "known_aliases": sorted(ai_cluster.MODEL_REGISTRY),
        "alias_providers": {
            alias: provider
            for alias, (provider, _) in ai_cluster.MODEL_REGISTRY.items()
        },
        "tasks": tasks,
        "validators": validators,
        "model_strings": models,
    }


@router.put("/settings/{key}")
async def update_setting(key: str, payload: SettingIn, user: DevUser) -> dict:
    """
    Change one setting.

    Assignment keys are checked against the known model aliases, so a typo
    cannot silently break the pipeline at generation time.
    """
    existing = await db.fetchrow("SELECT category FROM settings WHERE key = $1", key)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No setting named '{key}'")

    if key.startswith(("task_", "validator_")):
        if payload.value not in ai_cluster.MODEL_REGISTRY:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT
                if hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT") else 422,
                f"'{payload.value}' is not a known model. "
                f"Known: {sorted(ai_cluster.MODEL_REGISTRY)}")

    await db.execute("""
        UPDATE settings SET value = $2, updated_at = now() WHERE key = $1
    """, key, payload.value)
    settings_service.invalidate(key)

    log.info("Dev %s set %s = %s", user["email"], key, payload.value)
    return {"key": key, "value": payload.value}


@router.get("/settings")
async def all_settings(user: DevUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT key, value, category, description, updated_at
        FROM settings ORDER BY category, key
    """)
    return [dict(r) for r in rows]


# --- Health ------------------------------------------------------------------

@router.get("/health")
async def system_health(user: DevUser) -> dict:
    counts = await db.fetchrow("""
        SELECT
            (SELECT count(*) FROM schools)             AS schools,
            (SELECT count(*) FROM users)               AS users,
            (SELECT count(*) FROM timetables)          AS timetables,
            (SELECT count(*) FROM api_keys
              WHERE is_active)                         AS active_keys,
            (SELECT count(*) FROM error_log
              WHERE resolved = false)                  AS open_errors
    """)
    return {
        "database": await db.is_healthy(),
        "encryption_configured": encryption.is_configured(),
        "pools": ai_cluster.pool_status(),
        "counts": dict(counts),
    }


# --- Operational views --------------------------------------------------------
# Cross-tenant by design: this is the platform looking at itself. Everything
# below is read-only apart from the two explicit actions at the end.

@router.get("/generations")
async def generations(user: DevUser,
                      state: Annotated[Optional[str], Query()] = None,
                      school_id: Annotated[Optional[str], Query()] = None,
                      limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
    """
    Every generation across every school, newest first.

    The columns are the ones asked when something has gone wrong: whose it was,
    how many attempts it took, what it cost, and why it failed.
    """
    if state is not None and state not in ("processing", "complete", "failed"):
        raise HTTPException(422, "state must be processing, complete or failed")

    rows = await db.fetch("""
        SELECT t.id, t.name, t.status, t.created_at, t.completed_at,
               s.name AS school, t.school_id,
               (SELECT count(*) FROM generation_attempts ga
                 WHERE ga.timetable_id = t.id) AS attempts,
               (SELECT ga.failure_reason FROM generation_attempts ga
                 WHERE ga.timetable_id = t.id
                 ORDER BY ga.attempt_number DESC LIMIT 1) AS last_failure,
               (SELECT ga.locked_model FROM generation_attempts ga
                 WHERE ga.timetable_id = t.id
                 ORDER BY ga.attempt_number DESC LIMIT 1) AS model,
               gcb.estimated_credits, gcb.actual_credits, gcb.status AS billing,
               (SELECT count(*) FROM timetable_entries te
                 WHERE te.timetable_id = t.id) AS entries
        FROM timetables t
        JOIN schools s ON s.id = t.school_id
        LEFT JOIN generation_cost_breakdown gcb ON gcb.timetable_id = t.id
        WHERE ($1::text IS NULL OR t.status = $1)
          AND ($2::uuid IS NULL OR t.school_id = $2)
        ORDER BY t.created_at DESC
        LIMIT $3
    """, state, school_id, limit)

    totals = await db.fetchrow("""
        SELECT count(*) FILTER (WHERE status = 'processing') AS processing,
               count(*) FILTER (WHERE status = 'complete')   AS complete,
               count(*) FILTER (WHERE status = 'failed')     AS failed
        FROM timetables
    """)

    return {
        "totals": dict(totals),
        "generations": [
            dict(r) | {"id": str(r["id"]), "school_id": str(r["school_id"])}
            for r in rows
        ],
    }


@router.get("/logs")
async def logs(user: DevUser,
               stage: Annotated[Optional[str], Query(max_length=60)] = None,
               log_status: Annotated[Optional[str], Query(alias="status",
                                                          max_length=30)] = None,
               attempt_id: Annotated[Optional[str], Query()] = None,
               limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict:
    """
    The pipeline log across every school: what ran, on what model, how long.

    Also the token bill. `tokens_used` per model is the number that decides
    whether a provider is affordable, and it exists nowhere else.
    """
    rows = await db.fetch("""
        SELECT pl.id, pl.stage, pl.model_used, pl.status, pl.detail,
               pl.tokens_used, pl.latency_ms, pl.created_at,
               t.name AS timetable, s.name AS school
        FROM pipeline_log pl
        LEFT JOIN timetables t ON t.id = pl.timetable_id
        LEFT JOIN schools s ON s.id = t.school_id
        WHERE ($1::text IS NULL OR pl.stage = $1)
          AND ($2::text IS NULL OR pl.status = $2)
          AND ($3::uuid IS NULL OR pl.attempt_id = $3)
        ORDER BY pl.created_at DESC
        LIMIT $4
    """, stage, log_status, attempt_id, limit)

    by_model = await db.fetch("""
        SELECT model_used,
               count(*) AS calls,
               coalesce(sum(tokens_used), 0) AS tokens,
               round(avg(latency_ms)) AS avg_ms,
               count(*) FILTER (WHERE status <> 'ok') AS failures
        FROM pipeline_log
        WHERE created_at > now() - interval '7 days' AND model_used IS NOT NULL
        GROUP BY model_used ORDER BY calls DESC
    """)

    return {
        "last_7_days_by_model": [dict(r) for r in by_model],
        "entries": [dict(r) | {"id": str(r["id"])} for r in rows],
    }


@router.get("/errors")
async def errors(user: DevUser,
                 resolved: Annotated[bool, Query()] = False,
                 dev_fault: Annotated[Optional[bool], Query()] = None,
                 limit: Annotated[int, Query(ge=1, le=200)] = 100) -> dict:
    """
    The error log, with what is needed to decide on a refund.

    `is_dev_fault` is the column that matters: a school is not charged for a
    run that failed because a provider rate-limited us, and this is where that
    judgement gets acted on.
    """
    rows = await db.fetch("""
        SELECT e.id, e.error_type, e.provider, e.stage, e.is_dev_fault,
               e.error_message, e.resolved, e.created_at,
               e.school_id, e.timetable_id,
               s.name AS school, t.name AS timetable,
               gcb.actual_credits, gcb.status AS billing_status
        FROM error_log e
        LEFT JOIN schools s ON s.id = e.school_id
        LEFT JOIN timetables t ON t.id = e.timetable_id
        LEFT JOIN generation_cost_breakdown gcb ON gcb.timetable_id = e.timetable_id
        WHERE e.resolved = $1
          AND ($2::boolean IS NULL OR e.is_dev_fault = $2)
        ORDER BY e.created_at DESC
        LIMIT $3
    """, resolved, dev_fault, limit)

    totals = await db.fetchrow("""
        SELECT count(*) FILTER (WHERE resolved = false) AS open,
               count(*) FILTER (WHERE resolved = false AND is_dev_fault) AS our_fault,
               count(*) FILTER (WHERE created_at > now() - interval '24 hours')
                 AS last_24h
        FROM error_log
    """)

    return {
        "totals": dict(totals),
        "errors": [
            dict(r) | {"id": str(r["id"]),
                       "school_id": str(r["school_id"]) if r["school_id"] else None,
                       "timetable_id": str(r["timetable_id"])
                       if r["timetable_id"] else None}
            for r in rows
        ],
    }


@router.post("/errors/{error_id}/resolve")
async def resolve_error(error_id: str, user: DevUser) -> dict:
    """Mark an error dealt with. Refunding is a separate, deliberate act."""
    updated = await db.fetchval("""
        UPDATE error_log SET resolved = true WHERE id = $1 RETURNING id
    """, error_id)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Error not found")
    log.info("Error %s marked resolved by %s", error_id, user["email"])
    return {"resolved": True, "id": error_id}


@router.post("/errors/{error_id}/refund")
async def refund_error(error_id: str, user: DevUser) -> dict:
    """
    Give a school back the credits a failed generation cost them.

    Deliberately separate from resolving: most errors need no refund, and a
    refund that happened automatically on every logged error would be
    impossible to audit. Idempotent - a second call returns what the first did
    rather than paying twice.
    """
    row = await db.fetchrow("""
        SELECT e.id, e.school_id, e.timetable_id, e.resolved,
               gcb.actual_credits, gcb.status AS billing_status
        FROM error_log e
        LEFT JOIN generation_cost_breakdown gcb ON gcb.timetable_id = e.timetable_id
        WHERE e.id = $1
    """, error_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Error not found")
    if not row["school_id"] or not row["timetable_id"]:
        raise HTTPException(422, "This error is not attached to a generation.")
    if row["billing_status"] != "charged" or not row["actual_credits"]:
        raise HTTPException(
            422, "That generation was never charged, so there is nothing to "
                 "refund.")

    already = await db.fetchval("""
        SELECT id FROM credit_transactions
        WHERE timetable_id = $1 AND type = 'refund'
    """, row["timetable_id"])
    if already:
        return {"refunded": False, "already_refunded": True}

    outcome = await billing.adjust(
        str(row["school_id"]), int(row["actual_credits"]),
        f"Refund for a failed generation (error {error_id})",
        created_by=user["email"], kind="refund",
        # Attaches the refund to the run. Without it the duplicate check above
        # never matches and a second click pays the school twice.
        timetable_id=str(row["timetable_id"]))

    await db.execute("UPDATE error_log SET resolved = true WHERE id = $1",
                     error_id)

    log.info("Refunded %s credits to %s for error %s by %s",
             row["actual_credits"], row["school_id"], error_id, user["email"])
    return {"refunded": True, "credits": int(row["actual_credits"]),
            "balance": outcome["balance"]}


@router.get("/users")
async def all_users(user: DevUser,
                    search: Annotated[Optional[str], Query(max_length=100)] = None,
                    limit: Annotated[int, Query(ge=1, le=500)] = 200) -> dict:
    """Every user on the platform, for answering "who is this person"."""
    rows = await db.fetch("""
        SELECT u.id, u.first_name, u.surname, u.email, u.role, u.is_active,
               u.login_method, u.created_at, u.deactivated_at,
               s.name AS school, u.school_id
        FROM users u
        JOIN schools s ON s.id = u.school_id
        WHERE ($1::text IS NULL
               OR u.email ILIKE '%' || $1 || '%'
               OR (u.first_name || ' ' || u.surname) ILIKE '%' || $1 || '%'
               OR s.name ILIKE '%' || $1 || '%')
        ORDER BY u.created_at DESC
        LIMIT $2
    """, search, limit)

    totals = await db.fetchrow("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE is_active) AS active,
               count(*) FILTER (WHERE role = 'owner') AS owners,
               count(*) FILTER (WHERE role = 'dev') AS devs
        FROM users
    """)

    return {
        "totals": dict(totals),
        "users": [dict(r) | {"id": str(r["id"]),
                             "school_id": str(r["school_id"])} for r in rows],
    }


@router.get("/invoices")
async def all_invoices(user: DevUser,
                       invoice_status: Annotated[Optional[str],
                                                 Query(alias="status")] = None,
                       limit: Annotated[int, Query(ge=1, le=200)] = 100) -> dict:
    """Invoices across every school, and what is owed."""
    rows = await db.fetch("""
        SELECT i.id, i.invoice_number, i.period_start, i.period_end,
               i.subtotal_credits, i.total_cents, i.status, i.due_date,
               i.days_overdue, i.sent_at, i.paid_at,
               s.name AS school, i.school_id
        FROM invoices i
        JOIN schools s ON s.id = i.school_id
        WHERE ($1::text IS NULL OR i.status = $1)
        ORDER BY i.period_start DESC, s.name
        LIMIT $2
    """, invoice_status, limit)

    totals = await db.fetchrow("""
        SELECT count(*) FILTER (WHERE status = 'sent') AS sent,
               count(*) FILTER (WHERE status = 'overdue') AS overdue,
               count(*) FILTER (WHERE status = 'paid') AS paid,
               coalesce(sum(total_cents) FILTER
                 (WHERE status IN ('sent', 'overdue')), 0) AS owed_cents
        FROM invoices
    """)

    return {
        "totals": dict(totals),
        "invoices": [
            dict(r) | {"id": str(r["id"]), "school_id": str(r["school_id"]),
                       "period_start": r["period_start"].isoformat(),
                       "period_end": r["period_end"].isoformat(),
                       "due_date": r["due_date"].isoformat()}
            for r in rows
        ],
    }


@router.get("/outstanding")
async def all_outstanding(user: DevUser,
                          limit: Annotated[int, Query(ge=1, le=200)] = 100) -> dict:
    """Every unpaid debt, oldest first, with the interest it is accruing."""
    rows = await db.fetch("""
        SELECT oc.id, oc.charge_type, oc.original_amount_cents,
               oc.interest_accrued_cents, oc.total_owed_cents,
               oc.days_outstanding, oc.interest_rate_daily, oc.created_at,
               s.name AS school, oc.school_id,
               sb.account_blocked, t.name AS timetable, i.invoice_number
        FROM outstanding_charges oc
        JOIN schools s ON s.id = oc.school_id
        LEFT JOIN school_billing sb ON sb.school_id = oc.school_id
        LEFT JOIN timetables t ON t.id = oc.timetable_id
        LEFT JOIN invoices i ON i.id = oc.invoice_id
        WHERE oc.status = 'outstanding'
        ORDER BY oc.created_at
        LIMIT $1
    """, limit)

    return {
        "total_owed_cents": sum(r["total_owed_cents"] for r in rows),
        "blocked_schools": len({str(r["school_id"]) for r in rows
                                if r["account_blocked"]}),
        "charges": [
            dict(r) | {"id": str(r["id"]), "school_id": str(r["school_id"]),
                       "interest_rate_daily": float(r["interest_rate_daily"])}
            for r in rows
        ],
    }


@router.get("/applications")
async def invoiced_applications(user: DevUser,
                                app_status: Annotated[Optional[str],
                                                      Query(alias="status")] = None
                                ) -> list[dict]:
    """
    Schools asking to be invoiced monthly rather than prepaying.

    The table has existed since migration 001 with nothing reading or writing
    it. This is the dev side; a school applies through billing.
    """
    rows = await db.fetch("""
        SELECT a.id, a.school_id, s.name AS school, a.billing_contact_name,
               a.billing_contact_email, a.billing_contact_phone,
               a.organisation_abn, a.reason, a.status, a.created_at,
               a.reviewed_at, a.reviewed_by, a.rejection_reason,
               u.email AS applied_by
        FROM invoiced_billing_applications a
        JOIN schools s ON s.id = a.school_id
        LEFT JOIN users u ON u.id = a.applied_by
        WHERE ($1::text IS NULL OR a.status = $1)
        ORDER BY a.created_at DESC
    """, app_status)
    return [dict(r) | {"id": str(r["id"]), "school_id": str(r["school_id"])}
            for r in rows]


class ApplicationDecisionIn(BaseModel):
    approve: bool
    rejection_reason: Optional[str] = Field(default=None, max_length=500)


@router.post("/applications/{application_id}/decide")
async def decide_application(application_id: str,
                             payload: ApplicationDecisionIn,
                             user: DevUser) -> dict:
    """
    Approve or refuse an invoiced-billing application.

    Approving only grants permission - it does not switch the school over.
    They choose invoiced billing themselves afterwards, which keeps the record
    of who decided what they are on.
    """
    row = await db.fetchrow("""
        SELECT id, school_id, status FROM invoiced_billing_applications
        WHERE id = $1
    """, application_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not found")
    if row["status"] != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"That application is already {row['status']}.")
    if not payload.approve and not payload.rejection_reason:
        raise HTTPException(422, "Give a reason when refusing an application.")

    async with db.transaction() as conn:
        await conn.execute("""
            UPDATE invoiced_billing_applications
            SET status = $2, reviewed_at = now(), reviewed_by = $3,
                rejection_reason = $4
            WHERE id = $1
        """, application_id, "approved" if payload.approve else "rejected",
            user["email"], payload.rejection_reason)

        if payload.approve:
            await conn.execute("""
                UPDATE school_billing
                SET invoiced_approved = true, invoiced_approved_at = now(),
                    invoiced_approved_by = $2, invoiced_revoked = false,
                    updated_at = now()
                WHERE school_id = $1
            """, row["school_id"], user["email"])

    log.info("Invoiced billing application %s %s by %s", application_id,
             "approved" if payload.approve else "rejected", user["email"])
    return {"decided": True,
            "status": "approved" if payload.approve else "rejected"}

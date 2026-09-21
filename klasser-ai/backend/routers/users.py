"""
School users and the invite flow (AUTH.md).

An owner invites a colleague by email; the invitee sets a name and password and
lands in the same school. Deactivation is reversible and takes effect on the
next request, because `get_current_user` checks `is_active` every time.

Two things shape this file:

**A school must always have a way in.** The last active owner cannot be
deactivated, demoted, or left as the only owner and then removed - a school
locked out of its own account has no self-service route back.

**Email is not a dependency.** Phase 12 owns sending. Supabase will email the
invite if the project has a mail provider, and when it cannot, the invite is
still created and the accept link handed back to the owner to pass on. The flow
works today and improves when email arrives, rather than being blocked on it.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field, field_validator

import config
from models import database as db
from routers.auth import CurrentUser, require_owner
from services import email as email_service
from services import supabase_client as sb

log = logging.getLogger("klasser.users")

router = APIRouter(tags=["users"])

ROLES = ("owner", "staff")
INVITE_DAYS = 7


class InviteIn(BaseModel):
    email: EmailStr
    role: str = "staff"

    @field_validator("role")
    @classmethod
    def known_role(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        return v


class AcceptIn(BaseModel):
    token: str = Field(min_length=10, max_length=100)
    first_name: str = Field(min_length=1, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=200)

    @field_validator("password")
    @classmethod
    def strength(cls, v: str) -> str:
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one letter and one number")
        return v


class RoleIn(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def known_role(cls, v: str) -> str:
        if v not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        return v


async def active_owners(school_id: str, *, excluding: Optional[str] = None) -> int:
    return await db.fetchval("""
        SELECT count(*) FROM users
        WHERE school_id = $1 AND role = 'owner' AND is_active = true
          AND ($2::uuid IS NULL OR id <> $2)
    """, school_id, excluding)


# --- Listing ------------------------------------------------------------------

@router.get("")
async def list_users(user: CurrentUser) -> dict:
    """Everyone at the school, plus invites nobody has accepted yet."""
    school_id = user["school_id"]

    people = await db.fetch("""
        SELECT id, first_name, surname, email, role, is_active, login_method,
               created_at, deactivated_at
        FROM users WHERE school_id = $1
        ORDER BY is_active DESC, created_at
    """, school_id)

    invites = await db.fetch("""
        SELECT i.id, i.email, i.role, i.status, i.created_at, i.expires_at,
               i.expires_at < now() AS expired,
               u.first_name || ' ' || u.surname AS invited_by
        FROM invites i
        LEFT JOIN users u ON u.id = i.invited_by
        WHERE i.school_id = $1 AND i.status = 'pending'
        ORDER BY i.created_at DESC
    """, school_id)

    return {
        "users": [dict(r) | {"id": str(r["id"]),
                             "is_you": str(r["id"]) == str(user["id"])}
                  for r in people],
        "invites": [dict(r) | {"id": str(r["id"])} for r in invites],
        "active_owners": await active_owners(school_id),
        "can_manage": user["role"] in ("owner", "dev"),
    }


# --- Inviting -----------------------------------------------------------------

@router.post("/invite", status_code=status.HTTP_201_CREATED)
async def invite(payload: InviteIn,
                 user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Invite someone to this school.

    Returns the accept link. That is deliberate: until a mail provider is
    configured the owner needs some way to pass it on, and even afterwards a
    link they can copy is useful when an email does not arrive.
    """
    school_id = user["school_id"]
    email = payload.email.strip().lower()

    existing = await db.fetchrow(
        "SELECT id, school_id, is_active FROM users WHERE lower(email) = $1", email)
    if existing:
        if str(existing["school_id"]) == str(school_id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That person is already on your school."
                + ("" if existing["is_active"]
                   else " Their account is deactivated - reactivate it instead."))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That email already belongs to another school's account.")

    pending = await db.fetchval("""
        SELECT id FROM invites
        WHERE lower(email) = $1 AND status = 'pending' AND expires_at > now()
    """, email)
    if pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "There is already a pending invite for that address. Revoke it "
            "first, or resend it.")

    # Our own token, not the Supabase user id. The accept page is reached
    # before the invitee has a session, so the token has to be unguessable on
    # its own - a user id is not a secret.
    token = secrets.token_urlsafe(32)
    accept_url = f"{config.APP_URL}/accept-invite.html?token={token}"

    try:
        result = await sb.invite_auth_user(
            email, redirect_to=accept_url,
            metadata={"school_id": str(school_id), "invited_role": payload.role})
    except sb.AuthUserError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Could not create the account: {exc.message}") from exc

    auth_id = result["user"]["id"]

    try:
        invite_id = await db.fetchval("""
            INSERT INTO invites (school_id, invited_by, email, role, token,
                                 expires_at)
            VALUES ($1,$2,$3,$4,$5, now() + ($6 || ' days')::interval)
            RETURNING id
        """, school_id, user["id"], email, payload.role, token, str(INVITE_DAYS))
    except Exception:
        # The auth user exists but the invite does not; without this it would be
        # stranded and the address unusable.
        log.exception("Invite row failed for %s - removing the auth user", email)
        await sb.delete_auth_user(auth_id)
        raise

    # Our own invite email, which carries our accept link rather than Supabase's
    # magic link. If it sends, the Supabase one being rate-limited stops
    # mattering - which is the normal case on the free tier.
    school_name = await db.fetchval(
        "SELECT name FROM schools WHERE id = $1", school_id)
    inviter = f"{user.get('first_name', '')} {user.get('surname', '')}".strip() \
        or user["email"]

    sent = await email_service.send(email, "invite", {
        "school_name": school_name,
        "invited_by_name": inviter,
        "role": payload.role,
        "accept_url": accept_url,
        "expires_days": INVITE_DAYS,
    }, school_id=str(school_id))

    # And tell the owner it went, if they want to know.
    await email_service.send_if_preferred(
        str(user["id"]), "notify_user_invited", user["email"], "user_invited", {
            "owner_first_name": user.get("first_name", ""),
            "school_name": school_name,
            "invitee_email": email,
            "role": payload.role,
            "invite_expires_days": INVITE_DAYS,
        }, school_id=str(school_id))

    log.info("%s invited %s as %s (supabase=%s, ours=%s)",
             user["email"], email, payload.role, result.get("emailed"),
             sent.get("sent"))

    return {
        "id": str(invite_id),
        "email": email,
        "role": payload.role,
        "accept_url": accept_url,
        # True if either path reached them. The page shows the copyable link
        # whenever this is false.
        "emailed": bool(result.get("emailed") or sent.get("sent")),
        "expires_in_days": INVITE_DAYS,
    }


@router.delete("/invites/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(invite_id: str,
                        user: Annotated[dict, Depends(require_owner)]) -> None:
    """
    Withdraw an invite nobody has accepted.

    The auth user Supabase created is removed too, so the address is free to be
    invited again later.
    """
    row = await db.fetchrow("""
        SELECT id, email, status FROM invites WHERE id = $1 AND school_id = $2
    """, invite_id, user["school_id"])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    if row["status"] != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"That invite is already {row['status']}.")

    await db.execute("DELETE FROM invites WHERE id = $1", invite_id)

    auth_user = await sb.find_auth_user(row["email"])
    if auth_user:
        linked = await db.fetchval(
            "SELECT 1 FROM users WHERE id = $1", auth_user["id"])
        # Only if they never finished accepting - otherwise this is a real user.
        if not linked:
            await sb.delete_auth_user(auth_user["id"])

    log.info("%s revoked the invite for %s", user["email"], row["email"])


@router.post("/invites/{invite_id}/resend")
async def resend_invite(invite_id: str,
                        user: Annotated[dict, Depends(require_owner)]) -> dict:
    """Extend an invite and hand back the link again."""
    row = await db.fetchrow("""
        SELECT id, email, role, token, status FROM invites
        WHERE id = $1 AND school_id = $2
    """, invite_id, user["school_id"])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    if row["status"] != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"That invite is already {row['status']}.")

    await db.execute("""
        UPDATE invites SET expires_at = now() + ($2 || ' days')::interval
        WHERE id = $1
    """, invite_id, str(INVITE_DAYS))

    return {
        "id": str(row["id"]),
        "email": row["email"],
        "accept_url": f"{config.APP_URL}/accept-invite.html?token={row['token']}",
        "expires_in_days": INVITE_DAYS,
    }


# --- Accepting ----------------------------------------------------------------

@router.get("/invite/{token}")
async def look_up_invite(token: str) -> dict:
    """
    What the accept page shows before anyone signs in.

    Public by necessity - the invitee has no session yet. It returns only the
    email already in the link's possession and the school's name, and an expired
    or unknown token is reported the same way as a used one, so the endpoint
    cannot be used to discover which addresses have been invited.
    """
    row = await db.fetchrow("""
        SELECT i.email, i.role, i.status, i.expires_at, s.name AS school
        FROM invites i JOIN schools s ON s.id = i.school_id
        WHERE i.token = $1
    """, token)

    if row is None or row["status"] != "pending" or row["expires_at"] < datetime.now(
            timezone.utc):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This invite link is not valid any more. Ask whoever invited you "
            "to send a new one.")

    return {"email": row["email"], "role": row["role"], "school": row["school"],
            "expires_at": row["expires_at"]}


@router.post("/accept")
async def accept(payload: AcceptIn) -> dict:
    """
    Finish an invite: set a name and password, and join the school.

    Public, because the invitee has no account until this succeeds. The token is
    the credential, and it is single-use - the invite is marked accepted in the
    same transaction that creates the user.
    """
    invite = await db.fetchrow("""
        SELECT i.id, i.school_id, i.email, i.role, i.status, i.expires_at,
               s.name AS school
        FROM invites i JOIN schools s ON s.id = i.school_id
        WHERE i.token = $1
    """, payload.token)

    if invite is None or invite["status"] != "pending" or \
            invite["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This invite link is not valid any more. Ask whoever invited you "
            "to send a new one.")

    auth_user = await sb.find_auth_user(invite["email"])
    if auth_user is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The account for this invite is missing. Ask for a new invite.")

    already = await db.fetchval("SELECT 1 FROM users WHERE id = $1", auth_user["id"])
    if already:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This invite has already been used.")

    try:
        await sb.set_password(auth_user["id"], payload.password)
    except sb.AuthUserError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Could not set the password: {exc.message}") from exc

    async with db.transaction() as conn:
        await conn.execute("""
            INSERT INTO users (id, school_id, first_name, surname, email, role,
                               login_method)
            VALUES ($1,$2,$3,$4,$5,$6,'password')
        """, auth_user["id"], invite["school_id"], payload.first_name.strip(),
            payload.surname.strip(), invite["email"], invite["role"])

        await conn.execute(
            "INSERT INTO notification_preferences (user_id) VALUES ($1) "
            "ON CONFLICT DO NOTHING", auth_user["id"])

        # Marked accepted inside the same transaction, so a token cannot be
        # used twice even by two requests arriving together.
        await conn.execute("""
            UPDATE invites SET status = 'accepted', accepted_at = now()
            WHERE id = $1 AND status = 'pending'
        """, invite["id"])

    log.info("%s accepted an invite to %s as %s",
             invite["email"], invite["school"], invite["role"])

    await email_service.send(invite["email"], "welcome", {
        "first_name": payload.first_name.strip(),
        "school_name": invite["school"],
        "login_url": f"{config.APP_URL}/dashboard.html",
    }, school_id=str(invite["school_id"]), user_id=str(auth_user["id"]))

    owner = await db.fetchrow("""
        SELECT id, email, first_name FROM users
        WHERE school_id = $1 AND role = 'owner' AND is_active = true
        ORDER BY created_at LIMIT 1
    """, invite["school_id"])
    if owner:
        await email_service.send_if_preferred(
            str(owner["id"]), "notify_user_joined", owner["email"],
            "user_invited", {
                "owner_first_name": owner["first_name"],
                "school_name": invite["school"],
                "invitee_email": invite["email"],
                "role": invite["role"],
                "invite_expires_days": INVITE_DAYS,
            }, school_id=str(invite["school_id"]))

    return {"joined": True, "school": invite["school"], "email": invite["email"],
            "role": invite["role"]}


# --- Managing -----------------------------------------------------------------

@router.patch("/{user_id}/role")
async def set_role(user_id: str, payload: RoleIn,
                   user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    target = await db.fetchrow("""
        SELECT id, role, is_active, email FROM users
        WHERE id = $1 AND school_id = $2
    """, user_id, school_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    # Demoting the last owner would leave the school with nobody who can invite,
    # buy credits or publish.
    if target["role"] == "owner" and payload.role != "owner":
        if await active_owners(school_id, excluding=user_id) == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This is the only active owner. Make someone else an owner "
                "first.")

    await db.execute(
        "UPDATE users SET role = $3 WHERE id = $1 AND school_id = $2",
        user_id, school_id, payload.role)

    log.info("%s changed %s to %s", user["email"], target["email"], payload.role)
    return {"updated": True, "role": payload.role}


@router.post("/{user_id}/deactivate")
async def deactivate(user_id: str,
                     user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Switch off an account. Reversible, and effective on the next request.

    Sessions are not revoked at Supabase: `get_current_user` checks `is_active`
    against our own database on every call, so a token that is still valid buys
    nothing. Deleting the auth user - as AUTH.md sketches - would be neither
    reversible nor possible, since `users.id` references it.
    """
    school_id = user["school_id"]
    target = await db.fetchrow("""
        SELECT id, role, is_active, email, first_name, surname FROM users
        WHERE id = $1 AND school_id = $2
    """, user_id, school_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if not target["is_active"]:
        return {"deactivated": True, "unchanged": True}

    if str(user_id) == str(user["id"]):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You cannot deactivate your own account.")

    if target["role"] == "owner" and await active_owners(
            school_id, excluding=user_id) == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This is the only active owner. The school would be locked out.")

    await db.execute("""
        UPDATE users SET is_active = false, deactivated_at = now()
        WHERE id = $1 AND school_id = $2
    """, user_id, school_id)

    log.info("%s deactivated %s", user["email"], target["email"])

    await email_service.send_if_preferred(
        str(user["id"]), "notify_user_joined", user["email"],
        "user_deactivated", {
            "owner_first_name": user.get("first_name", ""),
            "school_name": await db.fetchval(
                "SELECT name FROM schools WHERE id = $1", school_id),
            "user_name": f"{target['first_name']} {target['surname']}".strip()
                         if target.get("first_name") else target["email"],
            "user_email": target["email"],
            "deactivated_at": config.long_date(datetime.now(timezone.utc)),
        }, school_id=str(school_id))

    return {"deactivated": True, "email": target["email"]}


@router.post("/{user_id}/reactivate")
async def reactivate(user_id: str,
                     user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    target = await db.fetchrow("""
        SELECT id, is_active, email FROM users WHERE id = $1 AND school_id = $2
    """, user_id, school_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    await db.execute("""
        UPDATE users SET is_active = true, deactivated_at = NULL
        WHERE id = $1 AND school_id = $2
    """, user_id, school_id)

    log.info("%s reactivated %s", user["email"], target["email"])
    return {"reactivated": True, "email": target["email"]}

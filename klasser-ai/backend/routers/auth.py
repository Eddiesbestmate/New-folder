"""
Authentication - JWT verification, role guards and school signup.

Password login only for now. Google and Microsoft slot in without changing any
of this: Supabase issues the same JWT whichever method was used, and
`users.login_method` records which one. Signup is the only method-specific
path, and it branches on login_method already.
"""

import logging
import re
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator

import config
from models import database as db
from services import supabase_client as sb

log = logging.getLogger("klasser.auth")

router = APIRouter(tags=["auth"])
security = HTTPBearer(auto_error=False)

VALID_TIMEZONES = {tz for tz, _ in config.SCHOOL_TIMEZONES}


# --- Dependencies ------------------------------------------------------------

async def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
) -> dict:
    """
    Resolve the bearer token to a row in our users table.

    The is_active check runs on every request, so a deactivated account loses
    access immediately even if it still holds a valid Supabase token.
    """
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    auth_user = await sb.get_auth_user(credentials.credentials)
    if auth_user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    row = await db.fetchrow("SELECT * FROM users WHERE id = $1", auth_user["id"])
    if row is None:
        # Authenticated with Supabase but not provisioned in our schema. Happens
        # if signup failed partway. Distinct code so the frontend can react.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Account is not linked to a school. Contact support.",
        )
    if not row["is_active"]:
        sb.forget_token(credentials.credentials)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account deactivated")

    return dict(row)


CurrentUser = Annotated[dict, Depends(get_current_user)]


async def require_owner(user: CurrentUser) -> dict:
    if user["role"] not in ("owner", "dev"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner access required")
    return user


async def require_dev_role(user: CurrentUser) -> dict:
    if user["role"] != "dev":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Dev access required")
    return user


# --- Schemas -----------------------------------------------------------------

class CampusIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: Optional[str] = Field(default=None, max_length=300)


class SignupIn(BaseModel):
    school_name: str = Field(min_length=2, max_length=200)
    timezone: str
    first_name: str = Field(min_length=1, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    campuses: list[CampusIn] = Field(default_factory=list, max_length=20)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, v: str) -> str:
        if v not in VALID_TIMEZONES:
            raise ValueError(f"Unsupported timezone. Choose one of: {sorted(VALID_TIMEZONES)}")
        return v

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        # Supabase enforces a minimum too; this gives a clearer message earlier.
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("Password must contain at least one letter and one number")
        return v


class UserOut(BaseModel):
    id: str
    school_id: str
    school_name: str
    timezone: str
    first_name: str
    surname: str
    email: str
    role: str
    login_method: str
    onboarding_complete: bool


# --- Endpoints ---------------------------------------------------------------

@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(payload: SignupIn) -> dict:
    """
    Create a school and its owner account.

    The auth user is created first because it is the only step that cannot
    participate in our database transaction. If the provisioning transaction
    then fails, the auth user is deleted so a retry with the same email works
    rather than hitting "already registered".
    """
    existing = await db.fetchrow(
        "SELECT id FROM users WHERE lower(email) = lower($1)", payload.email
    )
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This email is already registered. Try logging in instead.",
        )

    try:
        auth_user = await sb.create_auth_user(payload.email, payload.password)
    except sb.AuthUserError as exc:
        if "already" in exc.message.lower():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This email is already registered. Try logging in instead.",
            ) from exc
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    user_id = auth_user["id"]

    try:
        async with db.transaction() as conn:
            school_id = await conn.fetchval(
                "INSERT INTO schools (name, timezone) VALUES ($1, $2) RETURNING id",
                payload.school_name, payload.timezone,
            )

            await conn.execute("""
                INSERT INTO users
                    (id, school_id, first_name, surname, email, role, login_method)
                VALUES ($1, $2, $3, $4, $5, 'owner', 'password')
            """, user_id, school_id, payload.first_name, payload.surname, payload.email)

            for campus in payload.campuses:
                await conn.execute(
                    "INSERT INTO campuses (school_id, name, address) VALUES ($1, $2, $3)",
                    school_id, campus.name, campus.address,
                )

            await conn.execute(
                "INSERT INTO school_credits (school_id, balance) VALUES ($1, 0)", school_id)
            await conn.execute(
                "INSERT INTO school_billing (school_id, billing_mode) VALUES ($1, 'credits')",
                school_id)
            await conn.execute("""
                INSERT INTO school_complexity (school_id, campus_count)
                VALUES ($1, $2)
            """, school_id, max(1, len(payload.campuses)))
            await conn.execute("""
                INSERT INTO onboarding (school_id, step_school_details, step_campuses)
                VALUES ($1, true, $2)
            """, school_id, bool(payload.campuses))
            await conn.execute(
                "INSERT INTO notification_preferences (user_id) VALUES ($1)", user_id)

    except Exception:
        log.exception("Signup provisioning failed for %s - removing auth user", payload.email)
        await sb.delete_auth_user(user_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Could not create the account. Nothing was saved - please try again.",
        )

    # After the transaction, and never raising: a school that has been created
    # successfully must not be reported as failed because a mail provider is
    # down. send() swallows its own errors and records the attempt.
    from services import email as email_service

    await email_service.send(payload.email, "welcome", {
        "first_name": payload.first_name,
        "school_name": payload.school_name,
        "login_url": f"{config.APP_URL}/dashboard.html",
    }, school_id=str(school_id), user_id=str(user_id))
    log.info("School created: %s (owner %s)", payload.school_name, payload.email)

    return {
        "school_id": str(school_id),
        "user_id": str(user_id),
        "message": "Account created. You can now log in.",
    }


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    school = await db.fetchrow(
        "SELECT name, timezone FROM schools WHERE id = $1", user["school_id"])
    complete = await db.fetchval(
        "SELECT completed FROM onboarding WHERE school_id = $1", user["school_id"])

    return UserOut(
        id=str(user["id"]),
        school_id=str(user["school_id"]),
        school_name=school["name"] if school else "",
        timezone=school["timezone"] if school else "Australia/Sydney",
        first_name=user["first_name"],
        surname=user["surname"],
        email=user["email"],
        role=user["role"],
        login_method=user["login_method"],
        onboarding_complete=bool(complete),
    )


@router.get("/timezones")
async def timezones() -> list[dict]:
    """Timezone options for the signup form. Public - no auth needed."""
    return [{"value": tz, "label": label} for tz, label in config.SCHOOL_TIMEZONES]


@router.get("/check-dev")
async def check_dev(user: Annotated[dict, Depends(require_dev_role)]) -> dict:
    """Dev portal calls this after login to confirm the role before rendering."""
    return {"role": user["role"], "email": user["email"]}

"""
KLASSER AI - Supabase Auth access.

Two clients, deliberately separate:

  auth_request()  - anon key. Verifies user tokens. Subject to RLS.
  admin_request() - service role key. BYPASSES RLS. Used only for creating,
                    inviting and deleting auth users.

The service role key is never returned to a caller, never logged, and never
reaches the frontend. Any new use of admin_request() should be treated as a
security-relevant change.
"""

import logging
import time
from typing import Any, Optional

import httpx

import keys

log = logging.getLogger("klasser.supabase")

# Token -> (user dict, expires_at). Verifying a JWT costs an HTTPS round trip to
# Supabase, and every authenticated request needs it, so successful lookups are
# cached briefly. Short enough that a deactivated user loses access promptly -
# the is_active check also runs against our own database on every request.
_token_cache: dict[str, tuple[dict, float]] = {}
TOKEN_CACHE_SECONDS = 30


def _client(api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=keys.SUPABASE_URL,
        headers={"apikey": api_key, "Content-Type": "application/json"},
        timeout=20.0,
    )


async def get_auth_user(access_token: str) -> Optional[dict]:
    """
    Resolve a Supabase access token to its auth user. Returns None if the token
    is invalid or expired.
    """
    cached = _token_cache.get(access_token)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    async with _client(keys.SUPABASE_ANON_KEY) as client:
        try:
            r = await client.get(
                "/auth/v1/user",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            log.error("Token verification request failed: %s", exc)
            return None

    if r.status_code != 200:
        return None

    user = r.json()
    _token_cache[access_token] = (user, time.monotonic() + TOKEN_CACHE_SECONDS)
    return user


def forget_token(access_token: str) -> None:
    _token_cache.pop(access_token, None)


async def admin_request(method: str, path: str, **kwargs) -> httpx.Response:
    """Call the Supabase Admin API with the service role key."""
    if not keys.SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_KEY is not configured")

    async with _client(keys.SUPABASE_SERVICE_KEY) as client:
        return await client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {keys.SUPABASE_SERVICE_KEY}"},
            **kwargs,
        )


async def create_auth_user(email: str, password: str,
                           email_confirmed: bool = True) -> dict[str, Any]:
    """
    Create a Supabase Auth user.

    email_confirmed defaults to True because no transactional email provider is
    configured yet (Phase 12). Once Resend is wired up, set this to False so new
    accounts must verify their address before signing in.
    """
    r = await admin_request("POST", "/auth/v1/admin/users", json={
        "email": email,
        "password": password,
        "email_confirm": email_confirmed,
    })
    if r.status_code not in (200, 201):
        detail = ""
        try:
            body = r.json()
            detail = body.get("msg") or body.get("message") or body.get("error_description", "")
        except ValueError:
            detail = r.text[:200]
        raise AuthUserError(detail or f"HTTP {r.status_code}", r.status_code)
    return r.json()


async def delete_auth_user(user_id: str) -> None:
    """
    Remove an auth user. Used to undo a half-finished signup.

    `public.users.id` references `auth.users(id)`, so this fails with a 500
    while our own row still exists - delete that first.
    """
    r = await admin_request("DELETE", f"/auth/v1/admin/users/{user_id}")
    if r.status_code not in (200, 204):
        log.error("Failed to delete auth user %s: HTTP %s", user_id, r.status_code)


# The admin list endpoint is paginated. An earlier version read only the first
# page, which worked perfectly with a handful of accounts and would have failed
# silently the moment the project passed 200 - an invitee whose auth user sat on
# page two would be told "the account for this invite is missing", and their
# invite could never be accepted. Exactly the failure that appears at launch.
AUTH_PAGE_SIZE = 200
AUTH_MAX_PAGES = 100


async def find_auth_user(email: str) -> Optional[dict]:
    """The auth user with this email, if one exists. Pages until found."""
    wanted = email.strip().lower()

    for page in range(1, AUTH_MAX_PAGES + 1):
        r = await admin_request("GET", "/auth/v1/admin/users",
                                params={"page": page,
                                        "per_page": AUTH_PAGE_SIZE})
        if r.status_code != 200:
            log.error("Could not list auth users (page %s): HTTP %s",
                      page, r.status_code)
            return None

        users = r.json().get("users", [])
        for user in users:
            if (user.get("email") or "").lower() == wanted:
                return user

        # A short page is the last page.
        if len(users) < AUTH_PAGE_SIZE:
            return None

    # Not "no such user" - we ran out of pages to look through, and saying the
    # account does not exist would be a guess.
    log.error("Gave up looking for %s after %s pages of auth users",
              email, AUTH_MAX_PAGES)
    return None


async def invite_auth_user(email: str, *, redirect_to: Optional[str] = None,
                           metadata: Optional[dict] = None) -> dict:
    """
    Create an auth user for an invitee and ask Supabase to email them a link.

    Supabase only sends that email if a mail provider is configured on the
    project; until Phase 12 wires one up there may be none. The invite is still
    created either way and the caller is told whether mail went out, so the
    owner can pass the link on by hand rather than the whole flow being blocked
    on an email provider.
    """
    body: dict[str, Any] = {"email": email}
    if metadata:
        body["data"] = metadata

    path = "/auth/v1/invite"
    if redirect_to:
        path += f"?redirect_to={redirect_to}"

    r = await admin_request("POST", path, json=body)
    if r.status_code in (200, 201):
        return {"user": r.json(), "emailed": True}

    # Supabase returns 4xx/5xx when it cannot send. Fall back to creating the
    # user directly so an invite still exists to accept.
    detail = ""
    try:
        body_json = r.json()
        detail = (body_json.get("msg") or body_json.get("message")
                  or body_json.get("error_description") or "")
    except ValueError:
        detail = r.text[:200]

    log.warning("Supabase could not email an invite to %s (HTTP %s: %s) - "
                "creating the account without sending", email, r.status_code,
                detail)

    created = await admin_request("POST", "/auth/v1/admin/users", json={
        "email": email,
        "email_confirm": True,
        **({"user_metadata": metadata} if metadata else {}),
    })
    if created.status_code not in (200, 201):
        raise AuthUserError(detail or f"HTTP {created.status_code}",
                            created.status_code)

    return {"user": created.json(), "emailed": False, "reason": detail}


async def set_password(user_id: str, password: str) -> None:
    """
    Set a password on an existing auth user.

    Used when an invitee accepts: they were created without one, and a password
    is how they sign in afterwards without another magic link.
    """
    r = await admin_request("PUT", f"/auth/v1/admin/users/{user_id}",
                            json={"password": password, "email_confirm": True})
    if r.status_code not in (200, 201):
        detail = ""
        try:
            detail = r.json().get("msg", "")
        except ValueError:
            detail = r.text[:200]
        raise AuthUserError(detail or f"HTTP {r.status_code}", r.status_code)


class AuthUserError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

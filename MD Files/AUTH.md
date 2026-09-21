# Authentication

## Supabase Auth

All authentication goes through Supabase Auth. Three methods supported for school users:
- Google OAuth
- Microsoft OAuth
- Email + password

Dev portal users: Google OAuth or email + password, with `role = 'dev'` in the users table.

---

## School user signup (new school)

```
User visits marketing site → clicks "Get started"
→ /signup page
→ Fills in:
    School name
    Timezone
    First name, Surname
    Email
→ Chooses login method (Google / Microsoft / password)
→ On submit:
    1. Create school record
    2. Supabase Auth creates user account
    3. Insert users row (school_id, role = 'owner', login_method)
    4. Insert school_credits row (balance = 0)
    5. Insert school_billing row (billing_mode = 'credits')
    6. Insert school_complexity row
    7. Insert onboarding row (all steps false)
    8. Insert notification_preferences row (all defaults)
    9. Send welcome email
    10. Redirect to onboarding wizard
```

One login method per account. If user tries to sign up with Google but the email already exists with password, show error: "This email is already registered with password login."

---

## Invite flow

```
Owner visits /users → enters invitee email and role
→ FastAPI checks email not already registered
→ FastAPI checks no pending invite for this email
→ Supabase Admin API: invite_user_by_email(email)
   ⚠ Requires SUPABASE_SERVICE_KEY, not the anon key — see "Service role key" below
→ Insert invites row (school_id, invited_by, email, role, token = supabase user id)
→ Supabase sends magic link email (or use custom invite template via Resend)
→ Send user_invited email to owner (if preference enabled)

User receives email → clicks link → /accept-invite?token=xxx
→ Form: First name, Surname, choose login method
→ On submit:
    1. Update users row: first_name, surname, login_method
    2. school_id comes from invites table via token
    3. Update invites row: status = 'accepted', accepted_at
    4. Insert notification_preferences row
    5. Send welcome email
    6. Redirect to dashboard
```

### As built

`routers/users.py`. Three differences, each forced by something real:

**The token is ours, not the Supabase user id.** The accept page is reached
before the invitee has any session, so the token in the link is the only thing
protecting it — and a user id is not a secret. It is 32 bytes from
`secrets.token_urlsafe`, and the invite is marked accepted inside the same
transaction that creates the user, so it cannot be redeemed twice even by two
requests arriving together.

**The invitee's row is created on accept, not on invite.** Until they accept
there is only an `invites` row and a passwordless Supabase account. So a pending
invite never appears as a half-real user in the school's list, and revoking one
removes the Supabase account too — otherwise the address could never be invited
again.

**Email is not a dependency.** Supabase emails the link when the project has a
mail provider; on the free tier it currently answers
`HTTP 429: email rate limit exceeded`, so the fallback runs: the account is
created anyway and the accept link is returned to the owner, who can copy it.
The UI says which happened. Phase 12 makes the email land; nothing here is
waiting on it.

---

## JWT verification on every endpoint

```python
# backend/routers/auth.py
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer
from supabase import create_client
from keys import SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY

# Normal client — respects RLS, used for token verification
supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# Admin client — BYPASSES RLS. Only for invite_user_by_email and delete_user.
supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

security = HTTPBearer()

async def get_current_user(token = Depends(security)):
    try:
        user = supabase.auth.get_user(token.credentials)
        db_user = await db.fetchrow(
            "SELECT * FROM users WHERE id = $1", user.user.id
        )
        if not db_user:
            raise HTTPException(401, "User not found")
        if not db_user['is_active']:
            raise HTTPException(403, "Account deactivated")
        return db_user
    except Exception:
        raise HTTPException(401, "Invalid token")

async def require_owner(user = Depends(get_current_user)):
    if user['role'] not in ('owner', 'dev'):
        raise HTTPException(403, "Owner access required")
    return user

async def require_dev_role(user = Depends(get_current_user)):
    if user['role'] != 'dev':
        raise HTTPException(403, "Dev access required")
    return user
```

---

## Account deactivation

```python
async def deactivate_user(user_id: str, deactivated_by: str):
    # 1. Set is_active = false in users table
    await db.execute("""
        UPDATE users SET is_active = false, deactivated_at = now()
        WHERE id = $1
    """, user_id)

    # 2. Revoke all Supabase sessions — requires the service role key
    await supabase_admin.auth.admin.delete_user(user_id)
```

**Step 2 is wrong and is not what was built.** Deleting the auth user is:

- **impossible** while our row exists — `public.users.id REFERENCES
  auth.users(id)`, so the delete returns a 500 (the same failure the Phase 11
  audit traced to thirteen stranded test accounts); and
- **irreversible**, whereas deactivation has to be undoable.

It is also unnecessary. `get_current_user` reads `is_active` from our own
database on *every* request, so a Supabase session that is still technically
valid grants nothing the moment the flag flips. `test_users.py` asserts exactly
that: the deactivated user's existing token is refused on the next call, not
when the JWT eventually expires.

As built (`routers/users.py`): set the flag, record `deactivated_at`, and stop.
Reactivation clears both. The last active owner cannot be deactivated or
demoted, so a school can never be locked out of its own account.

---

## Service role key

`SUPABASE_SERVICE_KEY` is required by exactly two operations:

| Operation | Where |
|---|---|
| `invite_user_by_email` | Invite flow — owner invites a staff member |
| `delete_user` / session revocation | Account deactivation |

**This key bypasses Row Level Security entirely.** Rules:

- Server-side only. Never sent to the frontend, never embedded in any HTML or JS file.
- Never logged, never included in an error response or a support ticket attachment.
- Only ever used through `supabase_admin`. Every other call uses the anon client so RLS
  stays in force.
- Stored in `.env` (git-ignored) and in the Render environment config. Listed with an
  empty value in `.env.example`.

Deactivated users get 403 on every API call even if they somehow have a valid token. The `is_active` check runs on every request via `get_current_user`.

---

## Dev portal access

Dev portal is a separate HTML page (`/dev-portal.html`). It uses the same Supabase Auth but checks `role = 'dev'` after login.

```javascript
// js/auth.js — dev portal login
async function devLogin(email, password) {
    const { data, error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw error;

    // Check dev role
    const user = await apiCall('GET', '/auth/me');
    if (user.role !== 'dev') {
        await supabase.auth.signOut();
        throw new Error('Access restricted to authorised team members');
    }

    window.location.href = '/dev-portal.html';
}
```

Dev users are created manually via Supabase dashboard or a one-time setup script. There is no self-signup for dev accounts.

---

## Session management

Supabase handles JWT refresh automatically in the JS client. Tokens expire after 1 hour by default. On the frontend:

```javascript
// js/api.js
supabase.auth.onAuthStateChange((event, session) => {
    if (event === 'SIGNED_OUT') {
        window.location.href = '/index.html';
    }
});
```

Server-side endpoints rely on the JWT being valid. No server-side session storage.

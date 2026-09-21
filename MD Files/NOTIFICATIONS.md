# Notification preferences

## Per-user preferences

Stored in `notification_preferences` table. All default to true. Users control them from /notifications.

---

## Preference keys and what triggers them

| Key | Default | Triggered by |
|---|---|---|
| notify_generation_complete | true | Generation pipeline completes successfully |
| notify_generation_failed | true | Pipeline fails after max retries |
| notify_generation_paused | true | Pipeline paused due to low credits or failed PAYG |
| notify_low_balance | true | Balance drops below school's low_balance_threshold |
| notify_purchase_receipt | true | Credit package purchased |
| notify_payg_charged | true | PAYG charge succeeds after generation |
| notify_payg_failed | true | PAYG charge fails (simulated decline) |
| notify_invoice_sent | true | Monthly invoice generated and emailed |
| notify_invoice_overdue | true | Invoice passes due date |
| notify_account_blocked | true | Account blocked due to outstanding charge |
| notify_version_published | true | A timetable version is published by anyone on the school |
| notify_edit_applied | true | An AI-assisted edit is applied to the timetable |
| notify_user_invited | true | Owner: when they invite someone. Staff: when they are invited |
| notify_user_joined | true | Owner receives notification when invited user accepts |

---

## Notifications UI

`/notifications.html` — simple list of toggles grouped by category:

```
Generation
  [ ] Timetable complete
  [ ] Generation failed
  [ ] Generation paused

Billing
  [ ] Low balance warning
  [ ] Credit purchase receipt
  [ ] PAYG charge receipt
  [ ] PAYG charge failed
  [ ] Invoice sent
  [ ] Invoice overdue
  [ ] Account blocked

Timetable
  [ ] Version published
  [ ] Edit applied

Team
  [ ] User invited / joined
```

Preferences saved on change (no save button — auto-save with debounce).

---

## Sending notifications

Always check preferences before sending:

```python
# services/email.py
async def send_if_preferred(user_id: str, preference_key: str, **email_kwargs):
    prefs = await db.fetchrow(
        "SELECT * FROM notification_preferences WHERE user_id = $1", user_id
    )
    if prefs is None or prefs.get(preference_key, True):
        await send_email(**email_kwargs)
```

As built, that lives in `routers/notifications.py` as `wants(user_id, key)`,
with `recipients(school_id, key)` and `owner_of(school_id)` alongside it. Phase
12 calls them; they are written and tested now so the send path has something to
ask. Three decisions:

**A missing row or an unknown key means send.** Failing to tell a school their
account is blocked is worse than one unwanted email.

**An inactive user is never a recipient**, so deactivating an account stops its
mail without anyone remembering to turn the preferences off.

**No column name is ever interpolated into SQL.** `wants()` reads the whole row
and picks the key in Python; `recipients()` filters in Python too; the update
statement names all fourteen columns and passes NULL for the ones a request did
not send. Adding a preference then fails loudly in that statement rather than
silently not saving.

For school-wide notifications (e.g. version published, account blocked), send to all active users of the school with that preference enabled:

```python
async def notify_school(school_id: str, preference_key: str, **email_kwargs):
    users = await db.fetch("""
        SELECT u.id, u.email, u.first_name,
               COALESCE(np.{preference_key}, true) as wants_email
        FROM users u
        LEFT JOIN notification_preferences np ON np.user_id = u.id
        WHERE u.school_id = $1 AND u.is_active = true
    """.format(preference_key=preference_key), school_id)

    for user in users:
        if user['wants_email']:
            await send_email(to=user['email'], **email_kwargs)
```

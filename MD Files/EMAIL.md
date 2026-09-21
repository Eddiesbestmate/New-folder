# Email

## Provider: Resend

Free tier: 3,000 emails/month, 100/day. No credit card required.

```python
# pip install resend
# Add RESEND_API_KEY to .env
```

```python
# services/email.py
import resend
from keys import RESEND_API_KEY
from config import FROM_EMAIL, APP_URL
from jinja2 import Environment, FileSystemLoader

resend.api_key = RESEND_API_KEY
jinja = Environment(loader=FileSystemLoader('templates/email'))

async def send_email(to: str, template_name: str, context: dict):
    template = jinja.get_template(f"{template_name}.html")
    html = template.render(**context, app_url=APP_URL)

    subject_map = {
        'welcome':                  'Welcome to Klasser',
        'email_verify':             'Verify your email address',
        'password_reset':           'Reset your password',
        'invite':                   f"You've been invited to join {context.get('school_name', 'a school')} on Klasser",
        'new_device_login':         'New login detected',
        'generation_complete':      f"Your timetable \"{context.get('timetable_name')}\" is ready",
        'generation_failed':        f"Timetable generation failed — {context.get('timetable_name')}",
        'generation_paused_credits':'Generation paused — low credits',
        'generation_paused_payg':   'Generation paused — payment required',
        'credit_purchase_receipt':  f"Receipt: {context.get('package_name')} — {context.get('credits')} credits",
        'payg_receipt':             f"Payment receipt: {context.get('timetable_name')}",
        'payg_failed':              'Payment failed — action required',
        'low_balance_warning':      'Low credit balance',
        'invoice_sent':             f"Invoice {context.get('invoice_number')} — {context.get('period')}",
        'invoice_reminder':         f"Invoice {context.get('invoice_number')} overdue — action required",
        'account_blocked':          'Your account has been blocked',
        'invoiced_billing_revoked': 'Invoiced billing has been removed from your account',
        'invoiced_billing_approved':'Invoiced billing approved',
        'invoiced_billing_received':'Invoiced billing application received',
        'version_published':        f"Timetable version {context.get('version_number')} published",
        'edit_applied':             'Timetable edit applied',
        'edit_rejected':            'Timetable edit could not be applied',
        'user_invited':             f"You've added {context.get('invitee_email')} to Klasser",
        'user_deactivated':         f"{context.get('user_name')}'s account has been deactivated",
    }

    resend.Emails.send({
        "from": FROM_EMAIL,
        "to": [to],
        "subject": subject_map.get(template_name, "Klasser notification"),
        "html": html,
    })
```

---

## Template variables — all 22 templates

### Authentication

**welcome**
```
first_name, school_name, login_url, app_url
```

**email_verify**
```
first_name, verify_url, expires_hours
```

**password_reset**
```
first_name, reset_url, expires_minutes
```

**invite**
```
school_name, invited_by_name, role, accept_url, expires_days
```

**new_device_login**
```
first_name, device, location, time, not_you_url
```

---

### Generation

**generation_complete**
```
first_name, timetable_name, school_name,
classes_formed, teachers_assigned, time_taken,
credits_estimated, credits_actual, credits_refunded,
attempt_count, view_url
```

**generation_failed**
```
first_name, timetable_name, school_name,
failure_reason, is_school_fault,
credits_charged, credits_refunded,
retry_url, support_url
```

**generation_paused_credits**
```
first_name, timetable_name, school_name,
credits_needed, credits_available,
paused_stage, top_up_url
```

**generation_paused_payg**
```
first_name, timetable_name, school_name,
amount_needed_cents, card_last_four,
update_payment_url
```

---

### Billing

**credit_purchase_receipt**
```
first_name, school_name,
package_name, credits, price_dollars,
new_balance, billing_url
```

**payg_receipt**
```
first_name, school_name, timetable_name,
credits_used, amount_charged_dollars,
card_brand, card_last_four, fake_payment_id,
billing_url
```

**payg_failed**
```
first_name, school_name,
amount_dollars, card_brand, card_last_four,
interest_rate_daily_pct,
update_payment_url, outstanding_url
```

**low_balance_warning**
```
first_name, school_name,
current_balance, estimated_generation_cost,
generations_remaining,
top_up_url
```

**invoice_sent**
```
first_name, school_name,
invoice_number, period,
subtotal_credits, total_dollars,
due_date, line_items,
pay_url, invoice_pdf_url
```

**invoice_reminder**
```
first_name, school_name,
invoice_number, total_dollars,
days_overdue, interest_accrued_dollars,
total_owed_dollars, block_date,
pay_url
```

**account_blocked**
```
first_name, school_name,
blocked_reason, outstanding_dollars,
resolve_url, support_url
```

**invoiced_billing_revoked**
```
first_name, school_name,
days_overdue, outstanding_dollars,
new_billing_mode, top_up_url, support_url
```

**invoiced_billing_approved**
```
first_name, school_name,
billing_contact_name, payment_days,
invoice_sent_day, due_day,
billing_url
```

**invoiced_billing_received**
```
first_name, school_name,
application_date, expected_response_days
```

---

### Timetable

**version_published**
```
first_name, school_name,
timetable_name, version_number,
published_by, notes,
view_url
```

**edit_applied**
```
first_name, school_name,
timetable_name, version_number,
request_text, ai_interpretation,
changes_summary, credits_charged,
view_url
```

**edit_rejected**
```
first_name, school_name,
timetable_name, request_text,
rejection_reason, credits_charged,
support_url
```

---

### Team

**user_invited**
```
owner_first_name, school_name,
invitee_email, role,
invite_expires_days
```

**user_deactivated**
```
owner_first_name, school_name,
user_name, user_email,
deactivated_at
```

---

## Email templates folder structure

```
backend/templates/email/
├── _base.html              # Base layout with header, footer, unsubscribe
├── welcome.html
├── email_verify.html
├── password_reset.html
├── invite.html
├── new_device_login.html
├── generation_complete.html
├── generation_failed.html
├── generation_paused_credits.html
├── generation_paused_payg.html
├── credit_purchase_receipt.html
├── payg_receipt.html
├── payg_failed.html
├── low_balance_warning.html
├── invoice_sent.html
├── invoice_reminder.html
├── account_blocked.html
├── invoiced_billing_revoked.html
├── invoiced_billing_approved.html
├── invoiced_billing_received.html
├── version_published.html
├── edit_applied.html
├── edit_rejected.html
├── user_invited.html
└── user_deactivated.html
```

---

## Base template structure

```html
<!-- templates/email/_base.html -->
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width">
  <title>Klasser</title>
</head>
<body style="background:#F7F7FB;font-family:Outfit,system-ui,sans-serif;padding:32px 16px">
  <div style="max-width:560px;margin:0 auto">

    <!-- Header -->
    <div style="background:#6366F1;border-radius:12px 12px 0 0;padding:20px 28px">
      <span style="font-size:18px;font-weight:700;color:#fff">Klass<span style="color:#F5A623">er</span></span>
    </div>

    <!-- Content -->
    <div style="background:#fff;border-radius:0 0 12px 12px;padding:28px;border:1px solid #E4E4EF;border-top:none">
      {% block content %}{% endblock %}
    </div>

    <!-- Footer -->
    <div style="text-align:center;padding:20px;font-size:11px;color:#9090B0">
      <p>Klasser &middot; AI-powered school timetabling</p>
      <p style="margin-top:6px">
        <a href="{{ app_url }}/notifications" style="color:#6366F1">Notification preferences</a>
        &middot;
        <a href="{{ app_url }}/support" style="color:#6366F1">Support</a>
      </p>
    </div>

  </div>
</body>
</html>
```

---

## Checking notification preferences before sending

```python
async def send_if_preferred(user_id: str, preference_key: str, *args, **kwargs):
    prefs = await db.fetchrow("""
        SELECT * FROM notification_preferences WHERE user_id = $1
    """, user_id)

    # If no preferences row, default is true for everything
    if prefs is None or prefs.get(preference_key, True):
        await send_email(*args, **kwargs)
```

Usage:
```python
await send_if_preferred(
    user_id,
    'notify_generation_complete',
    to=user.email,
    template_name='generation_complete',
    context={...}
)
```

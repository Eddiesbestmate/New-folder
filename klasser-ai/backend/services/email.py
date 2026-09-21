"""
Transactional email (EMAIL.md).

Renders a Jinja template, checks the recipient actually wants it, and hands it
to Resend. Twenty-four templates, all extending one base layout.

Four decisions shape this file:

**An email can never break the thing that triggered it.** Every send is wrapped:
a provider outage must not fail a generation that succeeded, or undo a refund
that was paid. Failures are logged and recorded, never raised at the caller.

**Preferences are checked here, not at each call site.** `send_if_preferred()`
is the only way school-facing mail is sent, so a new send point cannot forget.
Some mail ignores preferences by design - see ALWAYS_SEND.

**With no API key, mail is recorded rather than sent.** Every send still
renders its template and writes an `email_log` row marked `skipped`. So the flow
is exercised, the wiring is testable, and turning email on later is a matter of
adding the key - nothing else changes.

**Rendering is verified up front.** `check_templates()` renders all of them with
sample context at startup, so a broken template is found on deploy rather than
the first time a school's card declines.
"""

import asyncio
import json
import logging
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

import config
import keys
from models import database as db

log = logging.getLogger("klasser.email")

TEMPLATE_DIR = config.BACKEND_DIR / "templates" / "email" \
    if hasattr(config, "BACKEND_DIR") else None

# StrictUndefined: a template referring to a variable nobody passed raises here
# rather than silently rendering "Hello ," to a school.
_env = Environment(
    loader=FileSystemLoader(
        str((__import__("pathlib").Path(__file__).parent.parent
             / "templates" / "email").resolve())),
    autoescape=True,
    undefined=StrictUndefined,
)

SUBJECTS: dict[str, Any] = {
    "welcome": lambda c: "Welcome to Klasser",
    "email_verify": lambda c: "Verify your email address",
    "password_reset": lambda c: "Reset your password",
    "invite": lambda c: (
        f"You've been invited to join {c.get('school_name', 'a school')} "
        "on Klasser"),
    "new_device_login": lambda c: "New login detected",
    "generation_complete": lambda c: (
        f'Your timetable "{c.get("timetable_name")}" is ready'),
    "generation_failed": lambda c: (
        f"Timetable generation failed - {c.get('timetable_name')}"),
    "generation_paused_credits": lambda c: "Generation paused - low credits",
    "generation_paused_payg": lambda c: "Generation paused - payment required",
    "credit_purchase_receipt": lambda c: (
        f"Receipt: {c.get('package_name')} - {c.get('credits')} credits"),
    "payg_receipt": lambda c: f"Payment receipt: {c.get('timetable_name')}",
    "payg_failed": lambda c: "Payment failed - action required",
    "low_balance_warning": lambda c: "Low credit balance",
    "invoice_sent": lambda c: (
        f"Invoice {c.get('invoice_number')} - {c.get('period')}"),
    "invoice_reminder": lambda c: (
        f"Invoice {c.get('invoice_number')} overdue - action required"),
    "account_blocked": lambda c: "Your account has been blocked",
    "invoiced_billing_revoked": lambda c: (
        "Invoiced billing has been removed from your account"),
    "invoiced_billing_approved": lambda c: "Invoiced billing approved",
    "invoiced_billing_received": lambda c: (
        "Invoiced billing application received"),
    "version_published": lambda c: (
        f"Timetable version {c.get('version_number')} published"),
    "edit_applied": lambda c: "Timetable edit applied",
    "edit_rejected": lambda c: "Timetable edit could not be applied",
    "user_invited": lambda c: (
        f"You've added {c.get('invitee_email')} to Klasser"),
    "user_deactivated": lambda c: (
        f"{c.get('user_name')}'s account has been deactivated"),
    # Not in EMAIL.md's list of 22. The daily interest job needs somewhere to
    # tell a school their debt grew, and none of the others fit: invoice_reminder
    # is about one invoice, payg_failed is about the moment a card declines.
    "debt_reminder": lambda c: (
        f"Outstanding balance - ${c.get('total_owed_dollars')} owing"),
}

# Mail that goes regardless of preferences. A school that has switched off
# billing notifications still has to be told their account is blocked or their
# password was reset - those are not marketing, they are the account itself.
ALWAYS_SEND = frozenset({
    "welcome", "email_verify", "password_reset", "invite",
    "new_device_login", "account_blocked", "invoiced_billing_revoked",
})


# Reserved by RFC 2606 precisely so they can never receive mail. Every test
# suite in this project uses them, so without this guard a full run fires a
# hundred real API calls at the provider, burns the daily quota, and logs a
# wall of failures - the provider rejects them outright anyway.
UNROUTABLE_DOMAINS = ("example.com", "example.org", "example.net",
                      "test", "invalid", "localhost")


def configured() -> bool:
    return bool(keys.RESEND_API_KEY)


def deliverable(address: str) -> bool:
    """Whether it is worth handing this address to the provider at all."""
    domain = (address or "").rsplit("@", 1)[-1].strip().lower()
    return bool(domain) and not any(
        domain == d or domain.endswith("." + d) for d in UNROUTABLE_DOMAINS)


def render(template_name: str, context: dict) -> str:
    """
    The HTML for one email. Raises if the template or its variables are wrong.

    The base layout's own variables are defaults rather than overrides, so a
    caller that supplies one wins instead of causing a duplicate-argument
    TypeError - which is a crash in the send path over something harmless.
    """
    template = _env.get_template(f"{template_name}.html")
    return template.render({
        "app_url": config.APP_URL,
        "support_email": config.SUPPORT_EMAIL,
        **context,
    })


def subject_for(template_name: str, context: dict) -> str:
    builder = SUBJECTS.get(template_name)
    return builder(context) if builder else "Klasser notification"


async def _record(to: str, template_name: str, subject: str, status: str,
                  school_id: Optional[str], user_id: Optional[str],
                  error: Optional[str] = None) -> None:
    """
    Every attempt, sent or not.

    Without this there is no way to answer "did the school get told?", which is
    the first question asked whenever a school says they were charged without
    warning.
    """
    try:
        await db.execute("""
            INSERT INTO email_log
                (school_id, user_id, recipient, template_name, subject, status,
                 error)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
        """, school_id, user_id, to, template_name, subject, status, error)
    except Exception:  # noqa: BLE001
        # Logging the log failing must not take down the caller either.
        log.exception("Could not write email_log row for %s", template_name)


async def send(to: str, template_name: str, context: dict, *,
               school_id: Optional[str] = None,
               user_id: Optional[str] = None) -> dict:
    """
    Render and send one email. Never raises.

    Returns what happened so a caller that cares can look, and so tests can
    assert on it. Callers that do not care can ignore it entirely, which is the
    normal case.
    """
    if template_name not in SUBJECTS:
        log.error("Unknown email template %r", template_name)
        return {"sent": False, "reason": "unknown_template"}

    subject = subject_for(template_name, context)

    try:
        html = render(template_name, context)
    except TemplateError as exc:
        log.exception("Template %s failed to render", template_name)
        await _record(to, template_name, subject, "failed", school_id, user_id,
                      f"render: {exc}")
        return {"sent": False, "reason": "render_failed", "error": str(exc)}

    if not deliverable(to):
        log.info("Not sending to %s - reserved domain, would only bounce", to)
        await _record(to, template_name, subject, "skipped", school_id, user_id,
                      "reserved domain (RFC 2606) - cannot receive mail")
        return {"sent": False, "reason": "unroutable", "subject": subject,
                "html": html}

    if not configured():
        # Phase 12 is wired up but no provider key is set on this instance. The
        # template rendered, the preferences were honoured, and the intent is
        # recorded - only the delivery is missing.
        log.info("No RESEND_API_KEY - would have emailed %s: %s", to, subject)
        await _record(to, template_name, subject, "skipped", school_id, user_id,
                      "no provider key configured")
        return {"sent": False, "reason": "not_configured", "subject": subject,
                "html": html}

    try:
        import resend

        resend.api_key = keys.RESEND_API_KEY
        # The SDK is synchronous; off the event loop so one slow send does not
        # stall every other request this process is serving.
        result = await asyncio.to_thread(resend.Emails.send, {
            "from": keys.RESEND_FROM or config.FROM_EMAIL,
            "to": [to],
            "subject": subject,
            "html": html,
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("Sending %s to %s failed", template_name, to)
        await _record(to, template_name, subject, "failed", school_id, user_id,
                      str(exc)[:500])
        return {"sent": False, "reason": "provider_error", "error": str(exc)}

    await _record(to, template_name, subject, "sent", school_id, user_id)
    log.info("Emailed %s: %s", to, subject)
    return {"sent": True, "subject": subject,
            "id": (result or {}).get("id") if isinstance(result, dict) else None}


async def send_if_preferred(user_id: str, preference_key: str, to: str,
                            template_name: str, context: dict, *,
                            school_id: Optional[str] = None) -> dict:
    """
    Send only if this person wants this kind of email.

    The single entry point for school-facing mail, so a new send point cannot
    forget to check. Account mail in ALWAYS_SEND bypasses the check by design.
    """
    if template_name not in ALWAYS_SEND:
        from routers import notifications

        if not await notifications.wants(user_id, preference_key):
            log.debug("%s opted out of %s", user_id, preference_key)
            return {"sent": False, "reason": "opted_out"}

    return await send(to, template_name, context,
                      school_id=school_id, user_id=user_id)


async def notify_school(school_id: str, preference_key: str,
                        template_name: str, context: dict) -> dict:
    """
    Tell everyone at a school who wants to hear it.

    Each recipient gets their own first_name, so a school-wide email does not
    open "Hi None". Sent one at a time and never raising: one bad address must
    not stop the rest.
    """
    from routers import notifications

    people = await notifications.recipients(school_id, preference_key)
    sent = failed = 0

    for person in people:
        outcome = await send(
            person["email"], template_name,
            {**context, "first_name": person["first_name"]},
            school_id=school_id, user_id=person["id"])
        if outcome.get("sent"):
            sent += 1
        else:
            failed += 1

    return {"recipients": len(people), "sent": sent, "failed": failed}


# --- Start-up verification ----------------------------------------------------

SAMPLE_CONTEXT: dict[str, Any] = {
    "first_name": "Sam", "owner_first_name": "Sam",
    "school_name": "Westfield College", "login_url": "https://klasser.ai/login",
    "verify_url": "https://klasser.ai/verify", "expires_hours": 24,
    "reset_url": "https://klasser.ai/reset", "expires_minutes": 30,
    "invited_by_name": "Alex Reid", "role": "staff",
    "accept_url": "https://klasser.ai/accept", "expires_days": 7,
    "device": "Chrome on Windows", "location": "Melbourne, Australia",
    "time": "9:14am, 3 March", "not_you_url": "https://klasser.ai/support",
    "timetable_name": "Term 1 2026", "classes_formed": 21,
    "teachers_assigned": 14, "time_taken": "6 minutes",
    "credits_estimated": 200, "credits_actual": 196, "credits_refunded": 0,
    "attempt_count": 2, "view_url": "https://klasser.ai/output",
    "failure_reason": "Day 3: teacher double-booked", "is_school_fault": False,
    "credits_charged": 0, "retry_url": "https://klasser.ai/generate",
    "support_url": "https://klasser.ai/support",
    "credits_needed": 200, "credits_available": 40,
    "paused_stage": "layer5_teachers", "top_up_url": "https://klasser.ai/billing",
    "amount_needed_cents": 45080, "card_last_four": "4242",
    "update_payment_url": "https://klasser.ai/billing",
    "package_name": "Standard", "credits": 800, "price_dollars": "1,600.00",
    "new_balance": 840, "billing_url": "https://klasser.ai/billing",
    "credits_used": 196, "amount_charged_dollars": "450.80",
    "card_brand": "Visa", "fake_payment_id": "ch_3Nk2p8",
    "amount_dollars": "450.80", "interest_rate_daily_pct": "20",
    "outstanding_url": "https://klasser.ai/billing",
    "current_balance": 120, "estimated_generation_cost": 200,
    "generations_remaining": 0,
    "invoice_number": "KL-202603-0001", "period": "March 2026",
    "subtotal_credits": 392, "total_dollars": "901.60",
    "due_date": "14 April 2026",
    "line_items": [{"description": "Timetable generation - Term 1 2026",
                    "credits_used": 196, "amount_dollars": "450.80"}],
    "pay_url": "https://klasser.ai/billing",
    "invoice_pdf_url": "https://klasser.ai/billing",
    "days_overdue": 3, "interest_accrued_dollars": "54.10",
    "total_owed_dollars": "955.70", "block_date": "18 April 2026",
    "blocked_reason": "Pay as you go charge declined",
    "outstanding_dollars": "955.70",
    "resolve_url": "https://klasser.ai/billing",
    "new_billing_mode": "credits",
    "billing_contact_name": "Jo Baker", "payment_days": 30,
    "invoice_sent_day": 1, "due_day": 14,
    "application_date": "3 March 2026", "expected_response_days": 2,
    "version_number": 2, "published_by": "Alex Reid",
    "notes": "Swapped Period 3 and 5 on day 4",
    "request_text": "Move 11COM1 out of Period 3 on Day 4",
    "ai_interpretation": "Move 11COM1 from Period 3 to Period 5 on Day 4",
    "changes_summary": "1 class moved", "rejection_reason":
        "That would double-book M. Patel in Period 5",
    "invitee_email": "newteacher@westfield.edu.au", "invite_expires_days": 7,
    "user_name": "Jamie Lee", "user_email": "jamie@westfield.edu.au",
    "deactivated_at": "3 March 2026",
    "what": "a declined card payment", "days_outstanding": 4,
    "interest_today_dollars": "18.03",
}


def check_templates() -> list[str]:
    """
    Render every template with sample context. Returns the ones that failed.

    Run at start-up. A template with a typo in a variable name is otherwise
    discovered by a school not receiving the email that tells them their account
    is blocked.
    """
    problems = []
    for name in sorted(SUBJECTS):
        try:
            html = render(name, SAMPLE_CONTEXT)
            if len(html) < 200:
                problems.append(f"{name}: rendered only {len(html)} characters")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
    return problems

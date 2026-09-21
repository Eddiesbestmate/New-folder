# Timezone handling

## Core rule

**All timestamps stored in UTC in the database. Always. No exceptions.**

Convert to local school time only at display, in the frontend.

---

## Australian timezones

Schools will be in one of these IANA timezone names:

| State/Territory | IANA name | Notes |
|---|---|---|
| NSW, VIC, TAS, ACT | Australia/Sydney | Observes daylight saving |
| Queensland | Australia/Brisbane | No daylight saving |
| South Australia | Australia/Adelaide | Observes daylight saving |
| Northern Territory | Australia/Darwin | No daylight saving |
| Western Australia | Australia/Perth | No daylight saving |

Always use IANA names, never abbreviations like AEST or AEDT. The abbreviation changes with daylight saving. The IANA name does not.

---

## School timezone field

```sql
ALTER TABLE schools ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Australia/Sydney';
```

Collected in onboarding step 1. Presented as a dropdown of the five options above.

---

## Period times

Block start and end times (`TIME` columns) represent local school time, not UTC. A `09:00` start time means 09:00 wherever the school is.

When doing any calculation involving block times and UTC timestamps (e.g. "is this block happening right now?"), convert using the school's timezone:

```python
# services/timezone.py
from zoneinfo import ZoneInfo
from datetime import datetime, date, time

def school_local_now(school_timezone: str) -> datetime:
    return datetime.now(ZoneInfo(school_timezone))

def block_start_utc(block_start_local: time, day: date, school_timezone: str) -> datetime:
    tz = ZoneInfo(school_timezone)
    local_dt = datetime.combine(day, block_start_local, tzinfo=tz)
    return local_dt.astimezone(ZoneInfo('UTC'))

def utc_to_school_local(utc_dt: datetime, school_timezone: str) -> datetime:
    return utc_dt.astimezone(ZoneInfo(school_timezone))
```

---

## Scheduled jobs

Jobs that fire at a specific time of day must fire at the right local time for each school.

**Invoice generation (1st of month):**
Runs at midnight Sydney time (covers most schools). For WA schools (2-3 hours behind), this means they get their invoice slightly early, which is fine.

```python
# APScheduler cron: runs at midnight AEST/AEDT (Sydney time)
scheduler.add_job(
    generate_monthly_invoices,
    'cron',
    hour=0, minute=0,
    timezone='Australia/Sydney'
)
```

**Invoice enforcement (daily overdue check):**
Same — runs once daily at midnight Sydney time.

**Interest accrual:**
Runs once daily. Timing doesn't matter much since it's a daily calculation.

**Generation complete notifications:**
Sent immediately when job completes. No timezone adjustment needed — user receives it whenever they receive it.

---

## Frontend display

All API responses return UTC timestamps. Frontend converts to school local time before displaying.

```javascript
// js/api.js
function formatLocalTime(utcString, schoolTimezone) {
    const date = new Date(utcString);
    return date.toLocaleString('en-AU', {
        timeZone: schoolTimezone,
        dateStyle: 'medium',
        timeStyle: 'short'
    });
}

// Usage
const generated = formatLocalTime(timetable.created_at, school.timezone);
// → "14 Aug 2026, 3:24 pm"
```

School timezone is returned in the `/schools/me` endpoint and stored in JS after login.

---

## Invoice due dates

Invoice due dates are stored as `DATE` (no time component). "Due 14 September" means midnight at the start of 14 September in the school's local timezone.

```python
def invoice_due_datetime(due_date: date, school_timezone: str) -> datetime:
    """Returns the UTC datetime at which the invoice becomes overdue."""
    tz = ZoneInfo(school_timezone)
    # Overdue at midnight on the due date in school's timezone
    local_midnight = datetime(due_date.year, due_date.month, due_date.day,
                              0, 0, 0, tzinfo=tz)
    return local_midnight.astimezone(ZoneInfo('UTC'))
```

---

## Summary checklist for Claude Code

- [ ] All `TIMESTAMPTZ` columns in Supabase store UTC automatically
- [ ] `schools.timezone` is always set during onboarding
- [ ] `layout_periods.start_time` and `end_time` are `TIME` (local, no timezone)
- [ ] All `datetime.now()` calls in backend use `datetime.now(UTC)` not naive datetime
- [ ] Frontend receives UTC, converts using school timezone for display
- [ ] Scheduled jobs use IANA timezone names not offsets
- [ ] Never use abbreviations (AEST, AEDT, ACST) in code

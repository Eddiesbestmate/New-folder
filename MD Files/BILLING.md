# Billing

## Credit packages

| Package | Credits | Price | Per credit | Limit |
|---|---|---|---|---|
| Trial | 300 | $480 | $1.60 | Once per school, expires 12 months |
| Standard | 800 | $1,600 | $2.00 | Unlimited |
| Annual | 1,500 | $2,850 | $1.90 | Unlimited |
| Ultra | 3,000 | $5,400 | $1.80 | Unlimited |
| Enterprise | 8,000 | $12,800 | $1.60 | Unlimited |
| PAYG | Variable | — | $2.30 | Charged after each generation |

## Pricing rationale

Schools budget annually. A deputy principal in Australia earns $120,000–$150,000/year.
Three weeks of their time per term saved = ~$33,600/year in staff time.
At $2,000/year average Klasser spend, you are delivering 16x ROI.
Edval (primary incumbent) charges $6,000–$15,000/year for an inferior product.
Klasser at revised pricing is still 50–75% cheaper than incumbents.

---

## Generation cost formula

```python
def calculate_generation_cost(school_id, settings) -> int:
    complexity = get_school_complexity(school_id)

    base = int(settings['credit_base_fee'])                          # 60cr
    students = complexity.student_count * float(settings['credit_per_student'])           # 0.20cr each
    teachers_over = max(0, complexity.teacher_count - int(settings['credit_teacher_threshold']))
    teachers = teachers_over * float(settings['credit_per_teacher_over'])                 # 0.40cr each over 20
    campuses = max(0, complexity.campus_count - 1) * int(settings['credit_per_extra_campus'])  # 30cr each
    transport = int(settings['credit_transport_flat']) if complexity.transport_enabled else 0   # 40cr flat
    routes = complexity.bus_route_count * int(settings['credit_per_bus_route'])           # 10cr each
    duties = int(settings['credit_duties_flat']) if complexity.duties_enabled else 0      # 20cr flat
    days_over = max(0, complexity.cycle_days - int(settings['credit_day_threshold']))
    days = days_over * int(settings['credit_per_extra_day'])                              # 4cr each over 5
    accel = int(settings['credit_accelerated_flat']) if complexity.accelerated_enabled else 0   # 20cr flat
    doubles = int(settings['credit_double_period_flat']) if complexity.double_periods_enabled else 0  # 10cr flat

    total = base + students + teachers + campuses + transport + routes + duties + days + accel + doubles
    return round(total)
```

## Generation cost by school size

| School | Students | Campuses | Transport | Approx credits | Approx price |
|---|---|---|---|---|---|
| Small | 400 | 1 | No | ~120cr | ~$240 |
| Medium | 700 | 1 | No | ~200cr | ~$400 |
| Complex | 1,000 | 2 | Yes | ~400cr | ~$800 |
| Large multi-campus | 1,500 | 3 | Yes | ~600cr | ~$1,200 |

---

## Billing modes

### Credits mode

```
Before generation:
  check_and_reserve(school_id, estimated_cost)
  → if balance - reserved < estimated_cost → block with InsufficientCreditsError

After generation:
  settle_generation(school_id, actual_cost, estimated_cost)
  → deduct actual_cost from balance
  → release reserved amount
  → if actual < estimated: add refund transaction
```

### PAYG mode

```
Before generation:
  check_fake_card_valid(school_id)
  → if no card saved → block with NoPaymentMethodError

After generation:
  charge_fake_card(school_id, actual_credits)
  → if simulated fail (based on fake_terminal_success_rate setting):
      log payg_charge_log as 'failed'
      block school
      send payg_failed email
  → if success:
      log payg_charge_log as 'success'
      send payg_receipt email
```

### Invoiced mode

```
Each generation:
  → cost logged to invoice ledger (not charged immediately)

1st of each month:
  → generate_monthly_invoices() runs
  → invoices created for each invoiced school
  → invoice PDFs generated and emailed

14th of each month (due date):
  → overdue check job runs daily
  → day 0 overdue: warning email sent
  → day 1 overdue: generation blocked, interest begins at 10%/day
  → day 11 overdue (10 days after block): invoiced billing revoked, school moved to credits mode
```

---

## Fake card terminal

The fake terminal simulates Stripe card input for the VCE project. No real payments are ever processed.

### How it works

```python
# services/fake_terminal.py
import random
import string
import hashlib
from datetime import datetime

def process_fake_card(raw_input: str) -> dict:
    """
    Takes whatever the user typed and returns a realistic-looking card record.
    The input is used as a seed so the same input always produces the same card.
    """
    seed = hashlib.md5(raw_input.encode()).hexdigest()
    rng = random.Random(seed)

    # Generate card number (Luhn-valid format, not real)
    prefix = rng.choice(['4', '5', '34', '37'])  # Visa, MC, Amex
    length = 15 if prefix in ['34', '37'] else 16
    digits = prefix + ''.join([str(rng.randint(0, 9)) for _ in range(length - len(prefix) - 1)])
    check_digit = luhn_check_digit(digits)
    card_number = digits + str(check_digit)

    # Determine brand
    brand = 'Amex' if prefix in ['34', '37'] else ('Visa' if prefix == '4' else 'Mastercard')

    # Generate expiry (1-4 years from now)
    now = datetime.now()
    exp_year = now.year + rng.randint(1, 4)
    exp_month = rng.randint(1, 12)

    # Generate fake Stripe-format IDs
    pm_id = 'pm_' + ''.join(rng.choices(string.ascii_letters + string.digits, k=24))
    charge_id = 'ch_' + ''.join(rng.choices(string.ascii_letters + string.digits, k=24))

    return {
        'payment_method_id': pm_id,
        'last_four': card_number[-4:],
        'brand': brand,
        'expires': f"{exp_month:02d}/{str(exp_year)[-2:]}",
        'fake_charge_id': charge_id,
    }

def luhn_check_digit(partial: str) -> int:
    digits = [int(d) for d in partial]
    digits.reverse()
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - (total % 10)) % 10
```

### Terminal UI behaviour

```
User clicks "Add payment method"
→ Modal opens with card input fields
→ User types anything in any field (letters, numbers, anything)
→ Fields format themselves as they type:
    Card number: groups into 4-4-4-4 (or 4-6-5 for Amex)
    Expiry: auto-inserts slash, validates MM/YY format visually
    CVV: 3 or 4 digits depending on brand
→ User clicks "Add card"
→ Brief "Processing..." animation (1.5 seconds)
→ fake_terminal.process_fake_card() called with raw input
→ Result stored in school_billing:
    payg_card_last_four, payg_card_brand, payg_card_expires, payg_fake_method_id
→ Success screen shown with masked card number
```

### Simulating a declined charge

Dev portal has a toggle: Settings → fake_terminal_success_rate

```
1.0 → always succeeds (default)
0.0 → always fails (useful for testing blocked account flow)
0.8 → 80% success (useful for testing retry flow)
```

When a PAYG charge is "declined":
- payg_charge_log entry created with status = 'simulated_fail'
- School blocked immediately
- payg_failed email sent
- outstanding_charges record created with 20%/day interest

---

## Daily interest accrual job

```python
# Runs once per day via a scheduled task (e.g. APScheduler in FastAPI)
async def accrue_daily_interest():
    outstanding = await db.fetch("""
        SELECT * FROM outstanding_charges WHERE status = 'outstanding'
    """)

    for charge in outstanding:
        daily_interest = int(charge['total_owed_cents'] * charge['interest_rate_daily'])
        new_total = charge['total_owed_cents'] + daily_interest
        new_days = charge['days_outstanding'] + 1

        await db.execute("""
            UPDATE outstanding_charges SET
                interest_accrued_cents = interest_accrued_cents + $1,
                total_owed_cents = $2,
                days_outstanding = $3
            WHERE id = $4
        """, daily_interest, new_total, new_days, charge['id'])

        await update_school_outstanding_balance(charge['school_id'])
        await send_daily_reminder_if_needed(charge, new_days)
```

---

## Monthly invoice job

```python
# Runs on the 1st of each month
async def generate_monthly_invoices():
    invoiced_schools = await db.fetch("""
        SELECT school_id FROM school_billing
        WHERE billing_mode = 'invoiced' AND invoiced_approved = true
    """)

    for school in invoiced_schools:
        generations = await db.fetch("""
            SELECT gc.*, t.name as timetable_name
            FROM generation_cost_breakdown gc
            JOIN timetables t ON t.id = gc.timetable_id
            WHERE gc.school_id = $1
            AND gc.settled_at >= date_trunc('month', now() - interval '1 month')
            AND gc.settled_at < date_trunc('month', now())
            AND gc.status = 'charged'
        """, school['school_id'])

        if not generations:
            continue

        total_credits = sum(g['actual_credits'] for g in generations)
        # Invoiced billing charges at its own configurable rate. It currently matches
        # the PAYG rate ($2.30/credit) but is a separate setting so the two can diverge.
        invoiced_rate = int(await get_setting('credit_invoiced_rate_cents'))  # 230
        total_cents = total_credits * invoiced_rate

        invoice_id = await create_invoice(school['school_id'], generations, total_credits, total_cents)
        await send_invoice_email(school['school_id'], invoice_id)
```

---

## Invoice enforcement job

```python
# Runs daily
async def process_overdue_invoices():
    overdue = await db.fetch("""
        SELECT i.*, sb.school_id
        FROM invoices i
        JOIN school_billing sb ON sb.school_id = i.school_id
        WHERE i.status IN ('sent', 'overdue')
        AND i.due_date < CURRENT_DATE
    """)

    for invoice in overdue:
        days_overdue = (date.today() - invoice['due_date']).days

        await db.execute("""
            UPDATE invoices SET status = 'overdue', days_overdue = $1 WHERE id = $2
        """, days_overdue, invoice['id'])

        await accrue_invoice_interest(invoice['id'], rate=0.10)

        # Day 0 (due date) — warning
        if days_overdue == 0:
            await send_email(invoice['school_id'], 'invoice_warning', invoice)

        # Day 1 — block
        if days_overdue == 1:
            await block_school(invoice['school_id'], reason='invoice_overdue')
            await send_email(invoice['school_id'], 'account_blocked', invoice)

        # Day 11 — revoke invoiced billing
        if days_overdue == 11:
            await revoke_invoiced_billing(invoice['school_id'])
            await send_email(invoice['school_id'], 'invoiced_revoked', invoice)
```

---

---

## As built

The three jobs above live in `backend/services/jobs.py` and are fired by
`backend/services/scheduler.py` (APScheduler, started by the worker). Four
things differ from the sketches above, each for a reason:

**Every job claims a day before doing anything.** `scheduled_job_runs` holds one
row per `(job_name, run_date)`, taken with `INSERT ... ON CONFLICT DO NOTHING`.
The sketches assume the scheduler fires exactly once; in practice several
workers run, processes restart, and a dev can trigger a job by hand. Applying a
day's interest twice overcharges a school, so "once per day" is enforced in the
database rather than assumed from the clock.

**Interest is charged per elapsed day, not per run.** Each charge carries
`last_interest_date`. If a job misses three days, three days accrue on the next
run. `days_outstanding + 1` would silently forgive the gap.

**One clock.** `config.billing_date()` defines "today" for every job, and the
scheduler fires on the same timezone. Mixing Postgres `CURRENT_DATE` (UTC) with
the server's local date puts them a day apart for most of the Australian day —
and always in production, where servers run UTC — which charged a new debt two
days of interest the first time it accrued.

**A declined PAYG charge is enforcement, not clawback.** The timetable is
already written and stays with the school. The decline blocks *future*
generations and opens an `outstanding_charges` row at `payg_interest_rate`.
Revoking invoiced billing works the same way: the school drops to prepaid
credits and the debt survives.

**Emails are stubbed until Phase 12.** `jobs._notify()` records what would be
sent and returns it in the job summary, so the tests assert the right school
would be told the right thing. Wiring up Resend is a change to that one
function.

Dev portal endpoints: `POST /billing/dev/jobs/{name}` runs a job by hand (with
`?force=true` to repeat one already run today), `GET /billing/dev/jobs` shows
the schedule and run history, and `POST /billing/dev/outstanding/{id}/resolve`
records a payment — which unblocks the school if it was their last debt.

---

## Manual credit adjustment (dev portal)

Dev portal owners can add or remove credits from any school account with a note. This is the only payment mechanism for the VCE version — schools "pay" by having a dev portal user add credits manually.

```python
@router.post("/dev/credits/adjust")
async def manual_credit_adjustment(
    school_id: str,
    amount: int,           # positive to add, negative to remove
    note: str,
    current_user = Depends(require_dev_role)
):
    async with db.transaction():
        await db.execute("""
            UPDATE school_credits
            SET balance = balance + $1, updated_at = now()
            WHERE school_id = $2
        """, amount, school_id)

        new_balance = await get_balance(school_id)

        await db.execute("""
            INSERT INTO credit_transactions
            (school_id, type, amount, balance_after, note, created_by)
            VALUES ($1, 'manual_adjustment', $2, $3, $4, $5)
        """, school_id, amount, new_balance, note, current_user.email)

    return {"balance": new_balance}
```

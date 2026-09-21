# Support

## In-app support

School users can submit support tickets from any page via the Help button in the sidebar footer.

### Ticket categories
- Billing — credits, invoices, charges
- Generation — pipeline errors, unexpected results
- Data — import issues, data problems
- Account — users, access, login
- Other

### Priority levels
- Low — general questions
- Normal — default
- High — blocking issue
- Urgent — cannot generate, account blocked

### Ticket form fields
- Category (dropdown)
- Subject (text)
- Body (textarea)
- Attach to timetable? (optional — links pipeline log)

---

## Support page

`/support.html` shows:
- New ticket form
- Existing tickets with status
- Contact details

```
Need help faster?
Email: support@klasser.ai
Response time: within 1 business day

For urgent billing issues:
Email: billing@klasser.ai
```

---

## Dev portal — support view

Dev portal shows all tickets across all schools:
- Filter by status, category, priority, school
- Open ticket → see full body + pipeline log if attached
- Update status
- Add internal notes (not visible to school)
- Mark resolved

---

## As built

`routers/support.py`, plus migration 012 for the columns this file assumed but
the schema lacked: `internal_notes`, `resolved_by`, `first_response_at`.

**Internal notes cannot leak, because the query does not select them.** Every
school-facing query names its columns through one `SCHOOL_COLUMNS` constant that
omits `internal_notes` — rather than selecting `*` and deleting the key
afterwards, which fails the moment someone adds a column.

**Triage is deterministic.** The sketch's `'high' if category == 'billing' and
'blocked' in body` became `triage()`: a school saying "blocked", "cannot
generate" or "locked out" is urgent on a billing or generation ticket, high on
any other. No model call, so a stuck school never waits on one.

**Closing and resolving differ.** A school may withdraw its own ticket
(`closed`); only the dev portal may mark one `resolved`. Otherwise the queue's
resolution figures would measure nothing.

**An attached timetable brings its log.** The most recent attempt is recorded on
the ticket, so `GET /support/dev/tickets/{id}` returns the pipeline log and
error log alongside the body — the dev opening it does not have to go looking.

`GET /support/contact` is deliberately public: a user who cannot log in is
exactly the one who needs the support address.

---

## Ticket router

```python
@router.post("/support/tickets")
async def create_ticket(
    category: str,
    subject: str,
    body: str,
    timetable_id: str = None,
    current_user = Depends(get_current_user)
):
    # Auto-set priority based on category
    priority = 'high' if category == 'billing' and 'blocked' in body.lower() else 'normal'

    ticket_id = await db.fetchval("""
        INSERT INTO support_tickets
        (school_id, user_id, category, subject, body, timetable_id, priority)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
    """, current_user['school_id'], current_user['id'],
        category, subject, body, timetable_id, priority)

    return {"ticket_id": ticket_id}
```

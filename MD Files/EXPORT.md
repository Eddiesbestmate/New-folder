# CSV Output Templates

## Overview

Schools define custom export formats so they can import Klasser output directly into their SIS (Compass, Sentral, SEQTA, etc.). Each template is named, saved, and reusable.

The school describes what columns they need in plain English. Gemini maps their description to Klasser's internal fields. The school reviews and saves.

---

## Template definition flow

```
1. School visits /export-templates → New template
2. Fills in:
   - Template name (e.g. "Sentral timetable import")
   - Output type (timetable entries / transport / duties / students)
   - Delimiter (comma / tab / semicolon)
   - Include header row (yes/no)
3. Defines columns — two ways:
   Option A: Paste a sample header row from their SIS
             e.g. "ClassCode,TeacherSurname,RoomName,PeriodStart,DayNumber"
   Option B: Type what columns they need in plain English
             e.g. "I need the class code, the teacher's surname, the room number,
                   what time each period starts, and which day of the cycle it is"
4. Gemini interprets and maps:
   ClassCode       → class_code
   TeacherSurname  → teacher.surname
   RoomName        → room.name
   PeriodStart     → period.start_time
   DayNumber       → period.day_number
5. School reviews mapping — can change any field
6. School saves template
```

---

## Available source fields

```python
EXPORT_FIELDS = {
    'timetable_entries': {
        # Class
        'class_code':           'Class code e.g. 11COM1',
        'subject':              'Subject name',
        'year_level':           'Year level e.g. Year 11',
        # Teacher
        'teacher.full_name':    'Teacher full name',
        'teacher.surname':      'Teacher surname only',
        'teacher.first_name':   'Teacher first name only',
        # Room
        'room.name':            'Room name/number',
        'room.campus':          'Campus name',
        'room.room_type':       'Room type',
        'room.capacity':        'Room capacity',
        # Period (block)
        'period.label':         'Period label e.g. Period 1',
        'period.number':        'Period number within the day',
        'period.start_time':    'Period start time HH:MM',
        'period.end_time':      'Period end time HH:MM',
        'period.day_number':    'Day number in cycle (1-10)',
        # Students
        'student_count':        'Number of students in this class',
        'student.full_name':    'Student full name (one row per student)',
        'student.year_group':   'Student year group',
    },
    'transport': {
        'bus.name':             'Bus name/number',
        'from_campus':          'Departure campus',
        'to_campus':            'Arrival campus',
        'departure_time':       'Departure time HH:MM',
        'arrival_time':         'Arrival time HH:MM',
        'day_number':           'Day number in cycle',
        'passenger_count':      'Number of passengers',
        'is_empty_leg':         'True/False whether empty leg',
    },
    'duties': {
        'teacher.full_name':    'Teacher full name',
        'teacher.surname':      'Teacher surname',
        'duty_type':            'Type of duty',
        'duty_timing':          'When in day (before_school etc)',
        'duty_start_time':      'Duty start time HH:MM',
        'duty_end_time':        'Duty end time HH:MM',
        'campus':               'Campus for this duty',
        'day_number':           'Day number in cycle',
    },
    'students': {
        'student.full_name':    'Student full name',
        'student.year_group':   'Year group',
        'student.campus':       'Home campus',
        'class_code':           'Class code',
        'subject':              'Subject name',
        'period.label':         'Period label',
        'period.day_number':    'Day number',
        'teacher.full_name':    'Teacher name',
        'room.name':            'Room name',
    }
}
```

---

## Template storage

```json
// csv_output_templates.columns JSONB structure
[
  {
    "label": "ClassCode",
    "source": "class_code",
    "format": null
  },
  {
    "label": "TeacherSurname",
    "source": "teacher.surname",
    "format": null
  },
  {
    "label": "PeriodStart",
    "source": "period.start_time",
    "format": "HH:MM"
  },
  {
    "label": "DayNumber",
    "source": "period.day_number",
    "format": "integer"
  }
]
```

---

## Export generation

```python
# services/exporter.py
async def generate_csv(timetable_id: str, version_id: str, template_id: str) -> str:
    template = await db.fetchrow(
        "SELECT * FROM csv_output_templates WHERE id = $1", template_id
    )
    columns = json.loads(template['columns'])

    # Fetch data based on output_type
    if template['output_type'] == 'timetable_entries':
        rows = await db.fetch("""
            SELECT te.*,
                   t.full_name  as teacher_name,
                   t.first_name as teacher_first_name,
                   t.surname    as teacher_surname,
                   r.name as room_name, r.room_type, r.capacity,
                   lp.label as period_label, lp.period_number,
                   lp.start_time, lp.end_time, lp.day_number,
                   c.name as campus_name
            FROM timetable_entries te
            JOIN teachers t        ON t.id  = te.teacher_id
            JOIN rooms r           ON r.id  = te.room_id
            JOIN layout_periods lp ON lp.id = te.layout_period_id
            JOIN campuses c        ON c.id  = te.campus_id
            WHERE te.version_id = $1
            ORDER BY lp.day_number, lp.period_number
        """, version_id)

    delimiter = template['delimiter']
    lines = []

    if template['include_header']:
        lines.append(delimiter.join(col['label'] for col in columns))

    for row in rows:
        values = []
        for col in columns:
            value = resolve_field(row, col['source'], col.get('format'))
            values.append(str(value) if value is not None else '')
        lines.append(delimiter.join(values))

    return '\n'.join(lines)
```

---

## On the export page

User visits output page → clicks Export → sees:
- Dropdown of their saved templates
- Preview of first 5 rows using the selected template
- Download button
- "Create new template" link

Export is instant — no AI involved at export time. AI only runs once when the template is defined.

---

## As built

`services/exporter.py` and `routers/export.py`. Four differences from the sketch
above:

**A template never becomes SQL.** The sketch builds the query from the template;
as built there is one fixed statement per output type, and columns are resolved
from the fetched row in Python against the `EXPORT_FIELDS` allowlist. A template
is a presentation instruction, so a broken or hostile one can produce a
wrong-looking file but cannot reach data the school does not own.

**A pasted header row usually needs no AI.** `_header_row()` recognises a single
line of short delimited tokens, and `_guess()` maps the names SIS vendors
actually use (`ClassCode`, `TeacherSurname`, `PeriodStart`, `DayNumber`, and
around forty more). Only prose, or a header with an unrecognised column, reaches
the model. If the model then fails, whatever was matched by name is still
offered rather than an error.

**The AI's output is filtered, not trusted.** Any `source` it returns that is
not in `EXPORT_FIELDS` is moved to `unmatched` and shown to the school, never
saved to the template.

**Written with Python's `csv` module.** A room called `Hall, "Main" O'Brien`
must be quoted correctly, or every column after it shifts by one and the
school's SIS imports the wrong data without complaining.

**Cells are neutralised against formula injection.** Excel, Sheets and
LibreOffice evaluate a cell beginning `=`, `+`, `-` or `@`, so a teacher named
`=HYPERLINK("http://evil","Payroll")` would run on whoever opened the export —
and opening it is the entire point of the feature (CWE-1236). Those values get a
leading apostrophe, which spreadsheets strip on display. It fires almost only on
values that were suspicious to begin with; the test asserts both that no cell
survives as a formula and that ordinary values are untouched.

Delimiters are restricted to comma, tab, semicolon and pipe. `student.full_name`
from the sketch is not a field on `timetable_entries` — one row per student is
what the separate `students` output type is for.

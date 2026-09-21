"""
Template-driven CSV export (EXPORT.md).

A school describes the columns their SIS wants - in plain English, or by pasting
its header row - and the AI maps that description onto Klasser's fields once,
when the template is defined. Export itself is pure data: no AI, no cost, no
latency.

Two rules hold this together:

**A column's source must be on the allowlist.** `EXPORT_FIELDS` is the whole
vocabulary. The AI proposes, this module checks, and a source it does not
recognise is rejected rather than reaching a query.

**Export never builds SQL from a template.** One fixed query per output type
fetches everything, and columns are resolved from the resulting row in Python.
A template is a presentation instruction, not a query fragment - so a hostile or
broken template can produce a wrong-looking CSV but cannot reach data the school
does not own.
"""

import csv
import io
import json
import logging
import re
from datetime import date, datetime, time
from typing import Any, Optional

from models import database as db
from services import ai_cluster
from services import settings as settings_service

log = logging.getLogger("klasser.exporter")

OUTPUT_TYPES = ("timetable_entries", "transport", "duties", "students")

DELIMITERS = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}

# Source field -> (description, key on the fetched row).
EXPORT_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "timetable_entries": {
        "class_code": ("Class code e.g. 11COM1", "class_code"),
        "subject": ("Subject name", "subject"),
        "year_level": ("Year level e.g. Year 11", "year_level"),
        "teacher.full_name": ("Teacher full name", "teacher_full_name"),
        "teacher.surname": ("Teacher surname only", "teacher_surname"),
        "teacher.first_name": ("Teacher first name only", "teacher_first_name"),
        "room.name": ("Room name/number", "room_name"),
        "room.campus": ("Campus name", "campus_name"),
        "room.room_type": ("Room type", "room_type"),
        "room.capacity": ("Room capacity", "room_capacity"),
        "period.label": ("Period label e.g. Period 1", "period_label"),
        "period.number": ("Period number within the day", "period_number"),
        "period.start_time": ("Period start time HH:MM", "start_time"),
        "period.end_time": ("Period end time HH:MM", "end_time"),
        "period.day_number": ("Day number in cycle (1-10)", "day_number"),
        "student_count": ("Number of students in this class", "student_count"),
    },
    "transport": {
        "bus.name": ("Bus name/number", "bus_name"),
        "from_campus": ("Departure campus", "from_campus"),
        "to_campus": ("Arrival campus", "to_campus"),
        "departure_time": ("Departure time HH:MM", "departure_time"),
        "arrival_time": ("Arrival time HH:MM", "arrival_time"),
        "day_number": ("Day number in cycle", "day_number"),
        "passenger_count": ("Number of passengers", "passenger_count"),
        "is_empty_leg": ("True/False whether empty leg", "is_empty_leg"),
    },
    "duties": {
        "teacher.full_name": ("Teacher full name", "teacher_full_name"),
        "teacher.surname": ("Teacher surname", "teacher_surname"),
        "duty_type": ("Type of duty", "duty_type"),
        "duty_timing": ("When in day (before_school etc)", "duty_timing"),
        "duty_start_time": ("Duty start time HH:MM", "duty_start_time"),
        "duty_end_time": ("Duty end time HH:MM", "duty_end_time"),
        "campus": ("Campus for this duty", "campus_name"),
        "day_number": ("Day number in cycle", "day_number"),
    },
    "students": {
        "student.full_name": ("Student full name", "student_full_name"),
        "student.year_group": ("Year group", "student_year_group"),
        "student.campus": ("Home campus", "student_campus"),
        "class_code": ("Class code", "class_code"),
        "subject": ("Subject name", "subject"),
        "period.label": ("Period label", "period_label"),
        "period.day_number": ("Day number", "day_number"),
        "teacher.full_name": ("Teacher name", "teacher_full_name"),
        "room.name": ("Room name", "room_name"),
    },
}

FORMATS = ("HH:MM", "H:MM", "integer", "upper", "lower", "title",
           "yes_no", "true_false", "date")


class ExportError(Exception):
    def __init__(self, message: str, *, code: str = "export_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# --- Queries -----------------------------------------------------------------
# One per output type. Fixed SQL, scoped to the version; a template chooses
# among the columns these return and can never add to them.

QUERIES: dict[str, str] = {
    "timetable_entries": """
        SELECT te.class_code, te.subject, cg.year_level,
               t.full_name AS teacher_full_name, t.first_name AS teacher_first_name,
               t.surname AS teacher_surname,
               r.name AS room_name, r.room_type, r.capacity AS room_capacity,
               c.name AS campus_name,
               lp.label AS period_label, lp.period_number,
               lp.start_time, lp.end_time, lp.day_number,
               (SELECT count(*) FROM timetable_entry_students tes
                 WHERE tes.timetable_entry_id = te.id) AS student_count
        FROM timetable_entries te
        JOIN teachers t ON t.id = te.teacher_id
        JOIN rooms r ON r.id = te.room_id
        JOIN campuses c ON c.id = te.campus_id
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        LEFT JOIN class_groups cg ON cg.id = te.class_group_id
        WHERE te.version_id = $1
        ORDER BY lp.day_number, lp.period_number, te.class_code
    """,
    "transport": """
        SELECT b.name AS bus_name,
               f.name AS from_campus, tc.name AS to_campus,
               ts.departure_time, ts.arrival_time, ts.day_number,
               ts.passenger_count, ts.is_empty_leg
        FROM transport_schedule ts
        JOIN buses b ON b.id = ts.bus_id
        JOIN campuses f ON f.id = ts.from_campus_id
        JOIN campuses tc ON tc.id = ts.to_campus_id
        WHERE ts.version_id = $1
        ORDER BY ts.day_number, ts.departure_time
    """,
    "duties": """
        SELECT t.full_name AS teacher_full_name, t.surname AS teacher_surname,
               dt.name AS duty_type, lds.timing AS duty_timing,
               lds.start_time AS duty_start_time, lds.end_time AS duty_end_time,
               c.name AS campus_name, lds.day_number
        FROM duty_assignments da
        JOIN teachers t ON t.id = da.teacher_id
        JOIN layout_duty_slots lds ON lds.id = da.duty_slot_id
        JOIN duty_types dt ON dt.id = lds.duty_type_id
        LEFT JOIN campuses c ON c.id = lds.campus_id
        WHERE da.version_id = $1
        ORDER BY lds.day_number, lds.start_time
    """,
    "students": """
        SELECT s.full_name AS student_full_name, s.year_group AS student_year_group,
               sc.name AS student_campus,
               te.class_code, te.subject,
               lp.label AS period_label, lp.day_number,
               t.full_name AS teacher_full_name, r.name AS room_name
        FROM timetable_entry_students tes
        JOIN timetable_entries te ON te.id = tes.timetable_entry_id
        JOIN students s ON s.id = tes.student_id
        LEFT JOIN campuses sc ON sc.id = s.campus_id
        JOIN teachers t ON t.id = te.teacher_id
        JOIN rooms r ON r.id = te.room_id
        JOIN layout_periods lp ON lp.id = te.layout_period_id
        WHERE te.version_id = $1
        ORDER BY s.year_group, s.full_name, lp.day_number, lp.period_number
    """,
}


# --- Formatting ---------------------------------------------------------------

def resolve_field(row: Any, source: str, output_type: str,
                  fmt: Optional[str] = None) -> str:
    """One cell. Never raises: a broken column produces an empty cell."""
    field = EXPORT_FIELDS.get(output_type, {}).get(source)
    if field is None:
        return ""

    value = row.get(field[1]) if isinstance(row, dict) else row[field[1]]
    if value is None:
        return ""

    if fmt in ("HH:MM", "H:MM") and isinstance(value, (time, datetime)):
        return (f"{value.hour}:{value.minute:02d}" if fmt == "H:MM"
                else f"{value.hour:02d}:{value.minute:02d}")
    if isinstance(value, (time, datetime)) and fmt is None:
        return f"{value.hour:02d}:{value.minute:02d}"
    if isinstance(value, date) or fmt == "date":
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    if fmt == "integer":
        try:
            return str(int(float(value)))
        except (TypeError, ValueError):
            return ""
    if isinstance(value, bool) or fmt in ("yes_no", "true_false"):
        truthy = bool(value)
        if fmt == "yes_no":
            return "Yes" if truthy else "No"
        return "true" if truthy else "false"

    text = str(value)
    if fmt == "upper":
        return text.upper()
    if fmt == "lower":
        return text.lower()
    if fmt == "title":
        return text.title()
    return text


# Excel, Google Sheets and LibreOffice evaluate a cell beginning with any of
# these as a formula. A teacher named `=HYPERLINK("http://...","Payroll")` or a
# subject starting with `=` would therefore execute when the school opens the
# file - and opening the file is the entire point of the feature (CWE-1236).
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def neutralise(value: str) -> str:
    """
    Stop a spreadsheet treating a cell as a formula.

    A leading apostrophe makes the cell literal text in every spreadsheet
    program and is stripped on display. Real timetable data essentially never
    begins with one of these characters, so this fires almost only on values
    that were suspicious to begin with - and when it does fire, the alternative
    is running someone else's formula on a deputy principal's machine.
    """
    if value and value[0] in FORMULA_TRIGGERS:
        return "'" + value
    return value


def validate_columns(columns: list, output_type: str) -> list[dict]:
    """
    Check a template's columns against the allowlist.

    Returns the cleaned list. Raises if a source is unknown - silently dropping
    a column would hand the school a CSV their SIS rejects, with no clue why.
    """
    if output_type not in EXPORT_FIELDS:
        raise ExportError(
            f"output_type must be one of {', '.join(OUTPUT_TYPES)}",
            code="bad_output_type")
    if not columns:
        raise ExportError("A template needs at least one column.",
                          code="no_columns")
    if len(columns) > 60:
        raise ExportError("A template may have at most 60 columns.",
                          code="too_many_columns")

    allowed = EXPORT_FIELDS[output_type]
    cleaned = []
    for i, col in enumerate(columns, 1):
        if not isinstance(col, dict):
            raise ExportError(f"Column {i} is not an object.", code="bad_column")

        source = col.get("source")
        if source not in allowed:
            raise ExportError(
                f"Column {i} ('{col.get('label', source)}') uses unknown field "
                f"'{source}'. Available: {', '.join(sorted(allowed))}",
                code="unknown_field")

        fmt = col.get("format")
        if fmt is not None and fmt not in FORMATS:
            raise ExportError(
                f"Column {i} uses unknown format '{fmt}'. "
                f"Available: {', '.join(FORMATS)}", code="unknown_format")

        label = str(col.get("label") or source)[:100]
        cleaned.append({"label": label, "source": source, "format": fmt})

    return cleaned


# --- Generating ---------------------------------------------------------------

async def generate(version_id: str, template: dict,
                   limit: Optional[int] = None) -> str:
    """
    The CSV. `limit` produces the preview shown before downloading.

    Written with the csv module rather than by joining strings: a room called
    "Hall, Main" or a teacher named O'Brien has to be quoted correctly or the
    school's SIS reads the file one column out of step.
    """
    output_type = template["output_type"]
    if output_type not in QUERIES:
        raise ExportError(f"Unknown output type {output_type}",
                          code="bad_output_type")

    columns = template["columns"]
    if isinstance(columns, str):
        columns = json.loads(columns)
    columns = validate_columns(columns, output_type)

    rows = await db.fetch(QUERIES[output_type], version_id)
    if limit is not None:
        rows = rows[:limit]

    delimiter = template.get("delimiter") or ","
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n",
                        quoting=csv.QUOTE_MINIMAL)

    # Applied to the header too: a column label is school-supplied text, and a
    # template called "=cmd" would otherwise be a formula in row one.
    if template.get("include_header", True):
        writer.writerow([neutralise(col["label"]) for col in columns])

    # Neutralised here rather than in resolve_field, so a field type added later
    # cannot miss the guard by being formatted somewhere else.
    for row in rows:
        writer.writerow([
            neutralise(
                resolve_field(row, col["source"], output_type, col.get("format")))
            for col in columns
        ])

    return buffer.getvalue()


async def row_count(version_id: str, output_type: str) -> int:
    """How many rows an export would produce, for the preview header."""
    if output_type not in QUERIES:
        return 0
    return len(await db.fetch(QUERIES[output_type], version_id))


# --- Interpreting a description -----------------------------------------------

def _header_row(text: str) -> Optional[list[str]]:
    """
    Treat the input as a pasted header row if it looks like one.

    A single line of short comma or tab separated tokens is a header, not a
    sentence. Spotting that avoids an AI call for the common case - and a
    pasted header is exactly what the school's SIS documentation gives them.
    """
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line or len(text.strip().splitlines()) > 1:
        return None

    for sep in ("\t", ",", ";", "|"):
        if sep in line:
            parts = [p.strip() for p in line.split(sep)]
            if len(parts) >= 2 and all(0 < len(p) <= 40 for p in parts):
                # A sentence with commas is not a header row.
                if not any(" " in p and len(p.split()) > 3 for p in parts):
                    return parts
    return None


def _guess(label: str, output_type: str) -> Optional[str]:
    """
    Match a column name to a field without asking the AI.

    Covers the names SIS vendors actually use. Anything this misses falls
    through to the model.
    """
    key = re.sub(r"[^a-z]", "", label.lower())
    allowed = EXPORT_FIELDS[output_type]

    direct = {
        "classcode": "class_code", "class": "class_code", "code": "class_code",
        "subject": "subject", "subjectname": "subject",
        "yearlevel": "year_level", "year": "year_level",
        "teacher": "teacher.full_name", "teachername": "teacher.full_name",
        "teacherfullname": "teacher.full_name",
        "teachersurname": "teacher.surname", "surname": "teacher.surname",
        "lastname": "teacher.surname",
        "teacherfirstname": "teacher.first_name", "firstname": "teacher.first_name",
        "room": "room.name", "roomname": "room.name", "roomnumber": "room.name",
        "campus": "room.campus", "campusname": "room.campus",
        "roomtype": "room.room_type", "capacity": "room.capacity",
        "period": "period.label", "periodlabel": "period.label",
        "periodnumber": "period.number", "periodno": "period.number",
        "periodstart": "period.start_time", "starttime": "period.start_time",
        "start": "period.start_time",
        "periodend": "period.end_time", "endtime": "period.end_time",
        "end": "period.end_time",
        "day": "period.day_number", "daynumber": "period.day_number",
        "dayno": "period.day_number", "cycleday": "period.day_number",
        "students": "student_count", "studentcount": "student_count",
        "size": "student_count", "classsize": "student_count",
        "bus": "bus.name", "busname": "bus.name",
        "from": "from_campus", "fromcampus": "from_campus",
        "to": "to_campus", "tocampus": "to_campus",
        "departure": "departure_time", "departuretime": "departure_time",
        "arrival": "arrival_time", "arrivaltime": "arrival_time",
        "passengers": "passenger_count", "passengercount": "passenger_count",
        "duty": "duty_type", "dutytype": "duty_type",
        "studentname": "student.full_name", "student": "student.full_name",
        "yeargroup": "student.year_group",
    }

    candidate = direct.get(key)
    if candidate and candidate in allowed:
        return candidate

    # Fall back to the field whose own name matches.
    for source in allowed:
        if re.sub(r"[^a-z]", "", source.lower()).endswith(key):
            return source
    return None


def build_prompt(description: str, output_type: str) -> str:
    fields = "\n".join(
        f"  {source:<22} {desc}"
        for source, (desc, _) in sorted(EXPORT_FIELDS[output_type].items()))

    return f"""
You are mapping a school's requested CSV columns onto a timetabling system's
fields.

What the school asked for:
"{description}"

Available fields for output type "{output_type}":
{fields}

Available formats: {', '.join(FORMATS)}

Return ONLY a JSON object:
{{
  "columns": [
    {{"label": "ClassCode", "source": "class_code", "format": null}},
    {{"label": "PeriodStart", "source": "period.start_time", "format": "HH:MM"}}
  ],
  "unmatched": ["anything they asked for that has no field"],
  "note": "one sentence on anything you were unsure about, or null"
}}

Rules:
- "source" must be exactly one of the field names listed above. Never invent one.
- "label" is the column heading their system expects - keep their spelling and
  capitalisation.
- Keep the order they asked for.
- If something they asked for has no matching field, leave it out of columns and
  name it in unmatched.
Do not include any text before or after the JSON.
""".strip()


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


async def interpret(description: str, output_type: str,
                    school_id: Optional[str] = None) -> dict:
    """
    Turn a description or a pasted header row into proposed columns.

    Tries the deterministic path first. A pasted header whose every name is
    recognised needs no AI at all - which is most of them, and it makes the
    common case free and instant.
    """
    if output_type not in EXPORT_FIELDS:
        raise ExportError(f"output_type must be one of {', '.join(OUTPUT_TYPES)}",
                          code="bad_output_type")

    header = _header_row(description)
    if header:
        guesses = [(label, _guess(label, output_type)) for label in header]
        if all(source for _, source in guesses):
            return {
                "columns": [{"label": label, "source": source, "format": None}
                            for label, source in guesses],
                "unmatched": [],
                "note": "Matched from the pasted header row without using AI.",
                "used_ai": False,
            }

    try:
        raw = await ai_cluster.call(
            "task_export_map", build_prompt(description, output_type),
            stage="export_map")
        proposal = _extract_json(raw)
    except (ai_cluster.AIError, json.JSONDecodeError, ValueError) as exc:
        # Fall back to whatever the deterministic matcher managed. A partial
        # template the school can finish by hand beats an error page.
        if header:
            matched = [{"label": label, "source": source, "format": None}
                       for label, source in guesses if source]
            if matched:
                return {
                    "columns": matched,
                    "unmatched": [label for label, source in guesses if not source],
                    "note": "The AI was unavailable; these were matched by name. "
                            "Check them and add anything missing.",
                    "used_ai": False,
                }
        raise ExportError(
            "Could not interpret that description. Try pasting the header row "
            "from your SIS, or naming the columns one per line.",
            code="interpretation_failed") from exc

    columns = proposal.get("columns") or []
    allowed = EXPORT_FIELDS[output_type]

    # The model is not trusted with the allowlist: anything it invented is moved
    # to unmatched rather than saved.
    kept, invented = [], []
    for col in columns:
        if isinstance(col, dict) and col.get("source") in allowed:
            fmt = col.get("format")
            kept.append({"label": str(col.get("label") or col["source"])[:100],
                         "source": col["source"],
                         "format": fmt if fmt in FORMATS else None})
        elif isinstance(col, dict):
            invented.append(str(col.get("label") or col.get("source")))

    unmatched = [str(u) for u in (proposal.get("unmatched") or [])] + invented

    return {
        "columns": kept,
        "unmatched": unmatched,
        "note": proposal.get("note"),
        "used_ai": True,
    }


def available_fields(output_type: str) -> list[dict]:
    """The vocabulary, for the column picker."""
    return [
        {"source": source, "description": desc}
        for source, (desc, _) in sorted(EXPORT_FIELDS.get(output_type, {}).items())
    ]

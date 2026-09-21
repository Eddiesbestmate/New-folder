"""
AI-assisted data import.

Schools upload whatever format they already have. The AI reads a sample and
proposes a column mapping; the school confirms or corrects it; then
deterministic code reads every row and writes it.

The division matters: the AI only ever maps column headers. It never sees the
full file, never decides what to insert, and never touches the database. Row
validation, duplicate matching and writing are all deterministic, so a bad AI
response can produce a wrong mapping - which the user reviews - but never a
corrupt import.
"""

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from models import database as db
from services import ai_cluster

log = logging.getLogger("klasser.importer")

MAX_SAMPLE_ROWS = 12
MAX_ROWS = 20000

# Fields the importer understands, per import type. Mirrors IMPORT.md, with the
# teacher name split applied (first_name + surname, full_name split on import).
IMPORT_FIELD_DEFINITIONS: dict[str, dict[str, dict]] = {
    "teachers": {
        "first_name": {"required": True, "description": "Teacher given name"},
        "surname": {"required": True, "description": "Teacher family name"},
        "full_name": {"required": False,
                      "description": "Whole name in one column - split into "
                                     "first_name and surname on import"},
        "email": {"required": False, "description": "Email address"},
        "subjects": {"required": False,
                     "description": "Comma-separated subjects they teach"},
        "campus": {"required": False, "description": "Home campus name"},
        "max_blocks": {"required": False, "description": "Maximum blocks per day"},
        "max_duties_per_cycle": {"required": False,
                                 "description": "Maximum duties per cycle"},
        "gender": {"required": False, "description": "Gender, for duty balancing"},
    },
    "students": {
        "full_name": {"required": True, "description": "Full name of the student"},
        "year_group": {"required": True, "description": "Year level e.g. Year 11"},
        "campus": {"required": False, "description": "Home campus name"},
        "subjects": {"required": False,
                     "description": "Comma-separated subject enrolments"},
        "gender": {"required": False, "description": "Gender, for class balancing"},
        "ability_band": {"required": False,
                         "description": "Ability grouping (high/mid/low)"},
    },
    "rooms": {
        "name": {"required": True, "description": "Room name or number"},
        "campus": {"required": True, "description": "Campus the room is on"},
        "capacity": {"required": True, "description": "Maximum student capacity"},
        "room_type": {"required": False,
                      "description": "Type e.g. classroom, computer_lab, science_lab"},
    },
    "subjects": {
        "subject": {"required": True, "description": "Subject name"},
        "year_level": {"required": False,
                       "description": "Year level, blank means all years"},
        "hard_max_size": {"required": False, "description": "Hard maximum class size"},
        "soft_max_size": {"required": False, "description": "Preferred maximum size"},
        "min_size": {"required": False, "description": "Minimum class size"},
        "room_type": {"required": False, "description": "Required room type"},
        "campus_lock": {"required": False,
                        "description": "Campus name if the subject is campus-locked"},
        "code_prefix": {"required": False,
                        "description": "Class code prefix e.g. COM for Computing"},
        "min_periods_per_cycle": {
            "required": False,
            "description": "Fewest times a class meets per cycle"},
        "max_periods_per_cycle": {
            "required": False,
            "description": "Most times a class meets per cycle"},
    },
}


class ImportError_(Exception):
    """Raised for a problem the user can act on."""


@dataclass
class ParsedFile:
    headers: list[str]
    rows: list[dict[str, str]]
    warnings: list[str] = field(default_factory=list)


# --- Parsing -----------------------------------------------------------------

def detect_format(filename: str, content: bytes) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".csv") or lower.endswith(".txt"):
        return "csv"
    if lower.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    if lower.endswith(".pdf") or content[:5] == b"%PDF-":
        return "pdf"
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    # Excel files are ZIP archives.
    if content[:2] == b"PK":
        return "xlsx"
    return "csv"


def parse_csv(content: bytes) -> ParsedFile:
    text = content.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise ImportError_("The file is empty")

    # Sniff the delimiter so tab- and semicolon-separated exports work too.
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ImportError_("Could not find a header row")

    headers = [h.strip() for h in reader.fieldnames if h and h.strip()]
    if not headers:
        raise ImportError_("The header row is blank")

    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    for i, raw in enumerate(reader):
        if i >= MAX_ROWS:
            warnings.append(f"File has more than {MAX_ROWS} rows - the rest were ignored")
            break
        row = {
            (k.strip() if k else ""): (v.strip() if isinstance(v, str) else "")
            for k, v in raw.items() if k
        }
        if any(row.values()):
            rows.append(row)

    if not rows:
        raise ImportError_("No data rows found under the header")
    return ParsedFile(headers=headers, rows=rows, warnings=warnings)


def parse_xlsx(content: bytes) -> ParsedFile:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ImportError_("openpyxl is not installed - cannot read .xlsx files") from exc

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    sheet = wb.active
    rows_iter = sheet.iter_rows(values_only=True)

    try:
        header_row = next(rows_iter)
    except StopIteration as exc:
        raise ImportError_("The spreadsheet is empty") from exc

    headers = [str(h).strip() if h is not None else "" for h in header_row]
    if not any(headers):
        raise ImportError_("The first row is blank - it should hold column headings")

    rows: list[dict[str, str]] = []
    warnings: list[str] = []
    for i, values in enumerate(rows_iter):
        if i >= MAX_ROWS:
            warnings.append(f"File has more than {MAX_ROWS} rows - the rest were ignored")
            break
        row = {
            headers[j]: ("" if v is None else str(v).strip())
            for j, v in enumerate(values) if j < len(headers) and headers[j]
        }
        if any(row.values()):
            rows.append(row)

    wb.close()
    if not rows:
        raise ImportError_("No data rows found under the header")
    return ParsedFile(headers=[h for h in headers if h], rows=rows, warnings=warnings)


def parse_file(filename: str, content: bytes) -> tuple[str, ParsedFile]:
    fmt = detect_format(filename, content)

    if fmt == "csv":
        return fmt, parse_csv(content)
    if fmt == "xlsx":
        return fmt, parse_xlsx(content)

    # PDF and image import needs the AI to extract the rows themselves, not just
    # map columns - a different and much larger job than this pipeline does.
    # Rejecting clearly beats importing something silently wrong.
    raise ImportError_(
        f"{fmt.upper()} files are not supported yet. Export the data as CSV or "
        "Excel and upload that instead."
    )


# --- AI mapping --------------------------------------------------------------

def build_import_prompt(import_type: str, headers: list[str],
                        sample_rows: list[dict[str, str]]) -> str:
    fields = IMPORT_FIELD_DEFINITIONS[import_type]
    sample = json.dumps(sample_rows[:MAX_SAMPLE_ROWS], indent=2)

    return f"""You are interpreting a school data file for import into a timetabling system.

Import type: {import_type}

Column headers found in the file:
{json.dumps(headers, indent=2)}

First rows of the file:
{sample}

Available fields for {import_type}:
{json.dumps(fields, indent=2)}

Map each column in the file to one of the available fields, or null if it does
not correspond to any of them.

Return ONLY a JSON object with this structure:
{{
  "detected_columns": [
    {{
      "source_column": "exact column header from the file",
      "klasser_field": "matching field name, or null",
      "confidence": "high | medium | low",
      "sample_values": ["value1", "value2"]
    }}
  ],
  "unrecognised_columns": ["headers with no match"],
  "warnings": ["any data quality issues you noticed"]
}}

Do not include any text before or after the JSON."""


def guess_mapping(import_type: str, headers: list[str]) -> dict:
    """
    Deterministic fallback used when no AI provider is reachable.

    Matches on normalised header names and common aliases. Worse than the AI at
    unusual headers, but it means an import is never blocked by a rate limit -
    and the user confirms the mapping either way.
    """
    fields = IMPORT_FIELD_DEFINITIONS[import_type]
    aliases = {
        "first_name": {"firstname", "first", "givenname", "given", "forename"},
        "surname": {"lastname", "last", "familyname", "family"},
        "full_name": {"name", "fullname", "teachername", "studentname"},
        "year_group": {"year", "yearlevel", "yeargroup", "grade", "form"},
        "campus": {"campus", "site", "location", "campusname"},
        "subjects": {"subject", "subjects", "classes", "teaches", "enrolments"},
        "capacity": {"capacity", "seats", "size", "maxsize"},
        "room_type": {"type", "roomtype", "category"},
        "name": {"room", "roomname", "roomnumber", "number"},
        "gender": {"gender", "sex"},
        "ability_band": {"ability", "band", "abilityband", "stream"},
        "email": {"email", "emailaddress", "mail"},
        "subject": {"subject", "subjectname", "course"},
        "year_level": {"year", "yearlevel", "grade"},
        "code_prefix": {"prefix", "code", "codeprefix", "subjectcode"},
    }

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    detected = []
    unmatched = []
    used: set[str] = set()

    for header in headers:
        h = norm(header)
        match = None
        for fname in fields:
            if fname in used:
                continue
            if h == norm(fname) or h in aliases.get(fname, set()):
                match = fname
                break
        if match:
            used.add(match)
            detected.append({"source_column": header, "klasser_field": match,
                             "confidence": "medium", "sample_values": []})
        else:
            detected.append({"source_column": header, "klasser_field": None,
                             "confidence": "low", "sample_values": []})
            unmatched.append(header)

    return {
        "detected_columns": detected,
        "unrecognised_columns": unmatched,
        "warnings": ["Mapping was matched by column name - no AI provider was "
                     "available. Check it carefully before confirming."],
        "source": "heuristic",
    }


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response that may have prose around it."""
    text = text.strip()
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


async def interpret(import_type: str, parsed: ParsedFile) -> dict:
    """Ask the AI for a mapping, falling back to header matching if it cannot."""
    prompt = build_import_prompt(import_type, parsed.headers, parsed.rows)
    try:
        raw = await ai_cluster.call("task_import_interpret", prompt,
                                    stage="import_interpret", json_mode=True)
        mapping = extract_json(raw)
        mapping["source"] = "ai"
    except (ai_cluster.AIError, json.JSONDecodeError, KeyError) as exc:
        log.warning("Import interpretation fell back to heuristics: %s", exc)
        return guess_mapping(import_type, parsed.headers)

    # Never trust the model to have returned the right shape.
    if not isinstance(mapping.get("detected_columns"), list):
        log.warning("AI mapping had no detected_columns - using heuristics")
        return guess_mapping(import_type, parsed.headers)

    valid = set(IMPORT_FIELD_DEFINITIONS[import_type])
    for col in mapping["detected_columns"]:
        if col.get("klasser_field") not in valid:
            col["klasser_field"] = None

    # Fill in real sample values rather than whatever the model echoed back.
    for col in mapping["detected_columns"]:
        source = col.get("source_column")
        col["sample_values"] = [
            r.get(source, "") for r in parsed.rows[:3] if r.get(source)
        ]

    return mapping


# --- Applying the mapping ----------------------------------------------------

def split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def apply_mapping(rows: list[dict[str, str]], mapping: dict) -> list[dict[str, Any]]:
    """Turn source rows into field-keyed rows using the confirmed mapping."""
    pairs = [
        (c["source_column"], c["klasser_field"])
        for c in mapping.get("detected_columns", [])
        if c.get("klasser_field")
    ]

    out = []
    for row in rows:
        mapped: dict[str, Any] = {}
        for source, field_name in pairs:
            value = (row.get(source) or "").strip()
            if value:
                mapped[field_name] = value
        out.append(mapped)
    return out


def to_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def split_list(value: Any) -> list[str]:
    if not value:
        return []
    return [s.strip() for s in re.split(r"[,;/|]", str(value)) if s.strip()]


def validate_row(row: dict, import_type: str) -> tuple[bool, Optional[str]]:
    if import_type == "teachers":
        if not row.get("first_name") and not row.get("surname"):
            if not row.get("full_name"):
                return False, "Missing teacher name"
            first, last = split_name(row["full_name"])
            if not last:
                return False, f"Could not split '{row['full_name']}' into first name and surname"
            row["first_name"], row["surname"] = first, last
        if not row.get("first_name") or not row.get("surname"):
            return False, "Missing first name or surname"

    elif import_type == "students":
        if not row.get("full_name"):
            return False, "Missing student name"
        if not row.get("year_group"):
            return False, "Missing year group"

    elif import_type == "rooms":
        if not row.get("name"):
            return False, "Missing room name"
        if not row.get("campus"):
            return False, "Missing campus"
        if to_int(row.get("capacity")) is None:
            return False, f"Capacity is not a number: {row.get('capacity')!r}"
        if to_int(row.get("capacity")) < 1:
            return False, "Capacity must be at least 1"

    elif import_type == "subjects":
        if not row.get("subject"):
            return False, "Missing subject name"

    return True, None


# --- Writing -----------------------------------------------------------------

async def campus_lookup(school_id: str) -> dict[str, str]:
    rows = await db.fetch(
        "SELECT id, name FROM campuses WHERE school_id = $1", school_id)
    return {r["name"].strip().lower(): str(r["id"]) for r in rows}


async def run_import(job_id: str) -> dict:
    """
    Import the rows for a confirmed job.

    Existing rows are updated rather than duplicated, matched on the natural key
    for each type (IMPORT.md). Every skipped row is recorded with its reason so
    the school can see exactly what did not come through.
    """
    job = await db.fetchrow("SELECT * FROM import_jobs WHERE id = $1", job_id)
    if job is None:
        raise ImportError_("Import job not found")

    school_id = job["school_id"]
    import_type = job["import_type"]
    payload = json.loads(job["confirmed_mapping"])
    mapping = payload["mapping"]
    source_rows = payload["rows"]

    await db.execute(
        "UPDATE import_jobs SET status = 'importing' WHERE id = $1", job_id)

    campuses = await campus_lookup(school_id)
    mapped = apply_mapping(source_rows, mapping)

    imported = updated = skipped = failed = 0
    warnings: list[dict] = []

    def warn(line: int, reason: str) -> None:
        if len(warnings) < 200:
            warnings.append({"row": line, "reason": reason})

    for i, row in enumerate(mapped, start=2):  # row 1 is the header
        ok, reason = validate_row(row, import_type)
        if not ok:
            skipped += 1
            warn(i, reason or "Invalid row")
            continue

        campus_id = None
        campus_name = row.get("campus") or row.get("campus_lock")
        if campus_name:
            campus_id = campuses.get(campus_name.strip().lower())
            if campus_id is None:
                warn(i, f"Unknown campus '{campus_name}' - imported without a campus")

        try:
            created = await _write_row(import_type, school_id, row, campus_id)
            if created:
                imported += 1
            else:
                updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            warn(i, f"Could not save: {str(exc)[:150]}")
            log.exception("Import row %s failed", i)

    await db.execute("""
        UPDATE import_jobs
        SET status = 'complete', rows_total = $2, rows_imported = $3,
            rows_skipped = $4, rows_failed = $5, warnings = $6,
            completed_at = now()
        WHERE id = $1
    """, job_id, len(mapped), imported + updated, skipped, failed,
        json.dumps(warnings))

    summary = {
        "rows_total": len(mapped),
        "inserted": imported,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "warnings": warnings,
    }
    log.info("Import %s finished: %s", job_id, summary)
    return summary


async def _write_row(import_type: str, school_id: str, row: dict,
                     campus_id: Optional[str]) -> bool:
    """Insert or update one row. Returns True if inserted."""

    if import_type == "teachers":
        existing = await db.fetchval("""
            SELECT id FROM teachers
            WHERE school_id = $1 AND lower(first_name) = lower($2)
              AND lower(surname) = lower($3)
        """, school_id, row["first_name"], row["surname"])

        subjects = split_list(row.get("subjects"))
        if existing:
            await db.execute("""
                UPDATE teachers SET subjects = $2, campus_id = COALESCE($3, campus_id),
                                    max_blocks = COALESCE($4, max_blocks),
                                    max_duties_per_cycle = COALESCE($5, max_duties_per_cycle),
                                    gender = COALESCE($6, gender)
                WHERE id = $1
            """, existing, subjects, campus_id, to_int(row.get("max_blocks")),
                to_int(row.get("max_duties_per_cycle")), row.get("gender"))
            return False

        await db.execute("""
            INSERT INTO teachers (school_id, first_name, surname, subjects,
                                  campus_id, max_blocks, max_duties_per_cycle, gender)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """, school_id, row["first_name"], row["surname"], subjects, campus_id,
            to_int(row.get("max_blocks")), to_int(row.get("max_duties_per_cycle")),
            row.get("gender"))
        return True

    if import_type == "students":
        existing = await db.fetchval("""
            SELECT id FROM students
            WHERE school_id = $1 AND lower(full_name) = lower($2) AND year_group = $3
        """, school_id, row["full_name"], row["year_group"])

        if existing:
            student_id = existing
            await db.execute("""
                UPDATE students SET campus_id = COALESCE($2, campus_id),
                                    gender = COALESCE($3, gender),
                                    ability_band = COALESCE($4, ability_band)
                WHERE id = $1
            """, student_id, campus_id, row.get("gender"), row.get("ability_band"))
            created = False
        else:
            student_id = await db.fetchval("""
                INSERT INTO students (school_id, full_name, year_group, campus_id,
                                      gender, ability_band)
                VALUES ($1,$2,$3,$4,$5,$6) RETURNING id
            """, school_id, row["full_name"], row["year_group"], campus_id,
                row.get("gender"), row.get("ability_band"))
            created = True

        for subject in split_list(row.get("subjects")):
            await db.execute("""
                INSERT INTO student_subjects (student_id, school_id, subject)
                VALUES ($1,$2,$3) ON CONFLICT DO NOTHING
            """, student_id, school_id, subject)
        return created

    if import_type == "rooms":
        if campus_id is None:
            raise ImportError_(f"Campus '{row.get('campus')}' does not exist")

        existing = await db.fetchval("""
            SELECT id FROM rooms
            WHERE school_id = $1 AND campus_id = $2 AND lower(name) = lower($3)
        """, school_id, campus_id, row["name"])

        if existing:
            await db.execute("""
                UPDATE rooms SET capacity = $2, room_type = COALESCE($3, room_type)
                WHERE id = $1
            """, existing, to_int(row["capacity"]), row.get("room_type"))
            return False

        await db.execute("""
            INSERT INTO rooms (school_id, campus_id, name, capacity, room_type)
            VALUES ($1,$2,$3,$4,$5)
        """, school_id, campus_id, row["name"], to_int(row["capacity"]),
            row.get("room_type"))
        return True

    if import_type == "subjects":
        year_level = row.get("year_level")
        existing = await db.fetchval("""
            SELECT id FROM subject_settings
            WHERE school_id = $1 AND lower(subject) = lower($2)
              AND year_level IS NOT DISTINCT FROM $3
        """, school_id, row["subject"], year_level)

        if existing:
            await db.execute("""
                UPDATE subject_settings
                SET hard_max_size = COALESCE($2, hard_max_size),
                    soft_max_size = COALESCE($3, soft_max_size),
                    min_size = COALESCE($4, min_size),
                    default_room_type = COALESCE($5, default_room_type),
                    campus_locked_id = COALESCE($6, campus_locked_id),
                    code_prefix = COALESCE($7, code_prefix),
                    min_periods_per_cycle = COALESCE($8, min_periods_per_cycle),
                    max_periods_per_cycle = COALESCE($9, max_periods_per_cycle)
                WHERE id = $1
            """, existing, to_int(row.get("hard_max_size")),
                to_int(row.get("soft_max_size")), to_int(row.get("min_size")),
                row.get("room_type"), campus_id, row.get("code_prefix"),
                to_int(row.get("min_periods_per_cycle")),
                to_int(row.get("max_periods_per_cycle")))
            return False

        await db.execute("""
            INSERT INTO subject_settings
                (school_id, subject, year_level, hard_max_size, soft_max_size,
                 min_size, default_room_type, campus_locked_id, code_prefix,
                 min_periods_per_cycle, max_periods_per_cycle)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """, school_id, row["subject"], year_level,
            to_int(row.get("hard_max_size")), to_int(row.get("soft_max_size")),
            to_int(row.get("min_size")), row.get("room_type"), campus_id,
            row.get("code_prefix"),
            to_int(row.get("min_periods_per_cycle")),
            to_int(row.get("max_periods_per_cycle")))
        return True

    raise ImportError_(f"Unknown import type '{import_type}'")

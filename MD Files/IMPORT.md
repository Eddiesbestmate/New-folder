# AI Data Import

## Overview

Schools upload their existing data in whatever format they have. Gemini interprets the structure, maps columns to Klasser fields, and shows the school what it understood before importing.

No fixed template required. Supported input formats: CSV, XLSX, PDF, images (photos of spreadsheets), plain text.

---

## Import flow

```
1. User visits /import
2. Selects import type (teachers / students / rooms / subjects / mixed)
3. Uploads file (any format)
4. Backend:
   a. Stores file temporarily
   b. Converts to base64 if image/PDF
   c. Sends to Gemini with interpretation prompt
   d. Gemini returns structured column mapping + sample rows
5. Frontend shows mapping confirmation screen:
   - Left column: field names Gemini found in the file
   - Right column: Klasser field they map to (editable dropdown)
   - Sample rows shown for each mapped field
   - Unrecognised columns flagged as warnings
   - User can remap any column
6. User confirms mapping
7. Import job created (status = 'confirmed')
8. Background job imports rows:
   - Validates each row
   - Skips rows with validation errors (logged to import_job.warnings)
   - Inserts valid rows
9. User sees import summary:
   - Rows imported
   - Rows skipped (with reasons)
   - Rows failed
```

---

## Gemini interpretation prompt

```python
def build_import_prompt(import_type: str, file_content: str, file_format: str) -> str:
    field_definitions = IMPORT_FIELD_DEFINITIONS[import_type]

    return f"""
You are interpreting a school data file for import into a timetabling system.

File format: {file_format}
Import type: {import_type}

File content:
{file_content}

Available Klasser fields for {import_type}:
{json.dumps(field_definitions, indent=2)}

Return ONLY a JSON object with this structure:
{{
  "detected_columns": [
    {{
      "source_column": "exact column header from file",
      "klasser_field": "matching Klasser field name or null if no match",
      "confidence": "high | medium | low",
      "sample_values": ["value1", "value2", "value3"]
    }}
  ],
  "unrecognised_columns": ["column names with no Klasser match"],
  "warnings": ["any data quality issues noticed"],
  "estimated_rows": 150
}}

Do not include any text before or after the JSON.
"""
```

---

## Field definitions per import type

```python
IMPORT_FIELD_DEFINITIONS = {
    'teachers': {
        'first_name':     {'required': True,  'description': 'Teacher given name'},
        'surname':        {'required': True,  'description': 'Teacher family name'},
        'full_name':      {'required': False, 'description': 'Whole name in one column — split into first_name and surname on import'},
        'email':          {'required': False, 'description': 'Email address'},
        'subjects':       {'required': False, 'description': 'Comma-separated subjects they teach'},
        'campus':         {'required': False, 'description': 'Home campus name'},
        'max_blocks':     {'required': False, 'description': 'Maximum blocks per day'},
        'gender':         {'required': False, 'description': 'Gender (for duty balancing)'},
    },
    'students': {
        'full_name':      {'required': True,  'description': 'Full name of the student'},
        'year_group':     {'required': True,  'description': 'Year level e.g. Year 11'},
        'campus':         {'required': False, 'description': 'Home campus name'},
        'subjects':       {'required': False, 'description': 'Comma-separated subject enrollments'},
        'gender':         {'required': False, 'description': 'Gender (for class balancing)'},
        'ability_band':   {'required': False, 'description': 'Ability grouping (high/mid/low)'},
    },
    'rooms': {
        'name':           {'required': True,  'description': 'Room name or number'},
        'campus':         {'required': True,  'description': 'Campus the room is on'},
        'capacity':       {'required': True,  'description': 'Maximum student capacity'},
        'room_type':      {'required': False, 'description': 'Type e.g. classroom, computer_lab, science_lab'},
    },
    'subjects': {
        'subject':        {'required': True,  'description': 'Subject name'},
        'year_level':     {'required': False, 'description': 'Year level (blank = all years)'},
        'hard_max_size':  {'required': False, 'description': 'Hard maximum class size'},
        'soft_max_size':  {'required': False, 'description': 'Preferred maximum class size'},
        'room_type':      {'required': False, 'description': 'Required room type'},
        'campus_lock':    {'required': False, 'description': 'Campus name if subject is campus-locked'},
        'code_prefix':    {'required': False, 'description': 'Class code prefix e.g. COM for Computing'},
    }
}
```

---

## Import router

```python
# routers/import_router.py
@router.post("/import/upload")
async def upload_import_file(
    file: UploadFile,
    import_type: str,
    current_user = Depends(get_current_user)
):
    content = await file.read()
    file_format = detect_format(file.filename, content)

    # Convert to text/base64 as appropriate
    if file_format == 'csv':
        text_content = content.decode('utf-8', errors='replace')
    elif file_format == 'xlsx':
        text_content = xlsx_to_csv_string(content)
    elif file_format in ('pdf', 'image'):
        # Send as base64 to Gemini vision
        base64_content = base64.b64encode(content).decode()
        text_content = f"[{file_format.upper()} FILE — base64 encoded]\n{base64_content}"

    # Call Gemini
    prompt = build_import_prompt(import_type, text_content, file_format)
    ai_mapping = await ai_cluster.call('task_import_interpret', prompt)
    parsed = json.loads(ai_mapping)

    # Create import job
    job_id = await db.fetchval("""
        INSERT INTO import_jobs
        (school_id, imported_by, import_type, original_filename, original_format, ai_mapping, status)
        VALUES ($1, $2, $3, $4, $5, $6, 'mapping')
        RETURNING id
    """, current_user['school_id'], current_user['id'],
        import_type, file.filename, file_format,
        json.dumps(parsed))

    return {"job_id": job_id, "mapping": parsed}


@router.post("/import/{job_id}/confirm")
async def confirm_import(
    job_id: str,
    confirmed_mapping: dict,
    current_user = Depends(get_current_user)
):
    await db.execute("""
        UPDATE import_jobs
        SET confirmed_mapping = $1, status = 'confirmed'
        WHERE id = $2
    """, json.dumps(confirmed_mapping), job_id)

    # Queue background import job
    background_tasks.add_task(run_import, job_id)
    return {"status": "importing"}
```

---

## Row validation during import

```python
async def validate_import_row(row: dict, import_type: str, school_id: str) -> tuple[bool, str]:
    if import_type == 'teachers':
        # Source files often carry one name column. Split it before validating.
        if not row.get('first_name') and not row.get('surname'):
            if not row.get('full_name'):
                return False, "Missing teacher name"
            row['first_name'], row['surname'] = split_name(row['full_name'])

        if not row.get('first_name') or not row.get('surname'):
            return False, "Missing first_name or surname"

        # Check for duplicate — teachers.full_name is a generated column
        exists = await db.fetchrow("""
            SELECT id FROM teachers
            WHERE school_id = $1 AND first_name = $2 AND surname = $3
        """, school_id, row['first_name'], row['surname'])
        if exists:
            return False, f"Teacher '{row['first_name']} {row['surname']}' already exists — skipped"

    elif import_type == 'students':
        if not row.get('full_name') or not row.get('year_group'):
            return False, "Missing full_name or year_group"

    elif import_type == 'rooms':
        if not row.get('name') or not row.get('capacity'):
            return False, "Missing name or capacity"
        try:
            int(row['capacity'])
        except ValueError:
            return False, f"Invalid capacity value: {row['capacity']}"

    return True, None
```

---

## Splitting a single name column

`teachers` stores `first_name` and `surname` separately; `full_name` is a generated
column. When the source file has only one name column, split on the **last** space —
everything before it is the given name(s), everything after is the surname.

```python
def split_name(full: str) -> tuple[str, str]:
    """'Anna Maria Patel' -> ('Anna Maria', 'Patel'). Single word -> surname empty."""
    parts = full.strip().split()
    if len(parts) == 1:
        return parts[0], ''
    return ' '.join(parts[:-1]), parts[-1]
```

Single-word names and names where the split is wrong (e.g. 'van der Berg') are flagged
in `import_job.warnings` so the school can correct them on the data page. The mapping
confirmation screen shows the split result for the first few rows, so a wrong split is
visible before any row is written.

---

## Update vs insert

If a row already exists (matched by name/email), the importer updates it rather than creating a duplicate. The matching logic:

- Teachers: match on first_name + surname within school
- Students: match on full_name + year_group within school
- Rooms: match on name + campus within school
- Subjects: match on subject + year_level within school

Users are shown how many rows were inserted vs updated on the completion screen.

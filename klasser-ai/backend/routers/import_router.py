"""
Data import - upload, confirm the mapping, then write the rows.

Two steps by design. Nothing is written until the school has seen what the
importer understood and confirmed it, so a wrong mapping costs a click rather
than a corrupted teacher list.
"""

import json
import logging
from typing import Annotated, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, UploadFile,
                     status)
from pydantic import BaseModel, Field

from models import database as db
from routers.auth import CurrentUser, require_owner
from routers.onboarding import recompute
from services import importer

log = logging.getLogger("klasser.import")

router = APIRouter(tags=["import"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


class ConfirmIn(BaseModel):
    """The mapping as the user approved it, which may differ from the AI's."""
    detected_columns: list[dict] = Field(min_length=1, max_length=200)


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload(
    user: Annotated[dict, Depends(require_owner)],
    file: UploadFile = File(...),
    import_type: str = Form(...),
) -> dict:
    if import_type not in importer.IMPORT_FIELD_DEFINITIONS:
        raise HTTPException(
            422, f"import_type must be one of "
                 f"{sorted(importer.IMPORT_FIELD_DEFINITIONS)}")

    content = await file.read()
    if not content:
        raise HTTPException(422, "The file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413, f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")

    try:
        fmt, parsed = importer.parse_file(file.filename or "upload", content)
    except importer.ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc

    mapping = await importer.interpret(import_type, parsed)

    # The parsed rows are stashed on the job so the confirm step does not need
    # the file re-uploaded. Kept as JSON rather than in memory so a restart
    # between the two steps does not lose the upload.
    job_id = await db.fetchval("""
        INSERT INTO import_jobs
            (school_id, imported_by, import_type, original_filename,
             original_format, ai_mapping, rows_total, status)
        VALUES ($1,$2,$3,$4,$5,$6,$7,'mapping')
        RETURNING id
    """, user["school_id"], user["id"], import_type,
        file.filename or "upload", fmt,
        json.dumps({"mapping": mapping, "rows": parsed.rows}),
        len(parsed.rows))

    return {
        "job_id": str(job_id),
        "original_filename": file.filename or "upload",
        "format": fmt,
        "rows_found": len(parsed.rows),
        "headers": parsed.headers,
        "mapping": mapping,
        "sample": parsed.rows[:5],
        "fields": importer.IMPORT_FIELD_DEFINITIONS[import_type],
        "warnings": parsed.warnings,
    }


@router.post("/{job_id}/confirm")
async def confirm(job_id: str, payload: ConfirmIn,
                  user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Accept the user's mapping and import immediately.

    This runs inline rather than as a background task: imports are bounded
    (20k rows) and the user is watching the screen. Background execution
    arrives with the job queue in Phase 9.5.
    """
    job = await db.fetchrow("""
        SELECT * FROM import_jobs WHERE id = $1 AND school_id = $2
    """, job_id, user["school_id"])
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import job not found")
    if job["status"] not in ("mapping", "confirmed"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This import is already {job['status']}")

    stored = json.loads(job["ai_mapping"])
    valid_fields = set(importer.IMPORT_FIELD_DEFINITIONS[job["import_type"]])

    # Trust the user's choice of field, but only if it is a real field.
    columns = []
    for col in payload.detected_columns:
        field_name = col.get("klasser_field")
        columns.append({
            "source_column": col.get("source_column"),
            "klasser_field": field_name if field_name in valid_fields else None,
        })

    if not any(c["klasser_field"] for c in columns):
        raise HTTPException(422, "Map at least one column before importing")

    await db.execute("""
        UPDATE import_jobs SET confirmed_mapping = $2, status = 'confirmed'
        WHERE id = $1
    """, job_id, json.dumps({
        "mapping": {"detected_columns": columns},
        "rows": stored["rows"],
    }))

    try:
        summary = await importer.run_import(job_id)
    except importer.ImportError_ as exc:
        await db.execute(
            "UPDATE import_jobs SET status = 'failed' WHERE id = $1", job_id)
        raise HTTPException(422, str(exc)) from exc

    await recompute(user["school_id"])
    return {"job_id": job_id, **summary}


@router.get("/jobs")
async def list_jobs(user: CurrentUser,
                    limit: int = 20) -> list[dict]:
    rows = await db.fetch("""
        SELECT id, import_type, original_filename, original_format, status,
               rows_total, rows_imported, rows_skipped, rows_failed,
               created_at, completed_at
        FROM import_jobs
        WHERE school_id = $1
        ORDER BY created_at DESC
        LIMIT $2
    """, user["school_id"], min(limit, 100))
    return [dict(r) | {"id": str(r["id"])} for r in rows]


@router.get("/{job_id}")
async def job_detail(job_id: str, user: CurrentUser) -> dict:
    row = await db.fetchrow("""
        SELECT id, import_type, original_filename, original_format, status,
               rows_total, rows_imported, rows_skipped, rows_failed,
               warnings, created_at, completed_at
        FROM import_jobs WHERE id = $1 AND school_id = $2
    """, job_id, user["school_id"])
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import job not found")

    result = dict(row) | {"id": str(row["id"])}
    result["warnings"] = json.loads(row["warnings"]) if row["warnings"] else []
    return result

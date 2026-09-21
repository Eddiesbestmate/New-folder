"""
CSV export (EXPORT.md).

Templates are defined once, with AI help, and reused. Export itself is
deterministic and free - the AI runs when a template is created, never when a
file is downloaded.
"""

import json
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from models import database as db
from routers.auth import CurrentUser, require_owner
from services import exporter

log = logging.getLogger("klasser.export")

router = APIRouter(tags=["export"])


class InterpretIn(BaseModel):
    description: str = Field(min_length=2, max_length=2000)
    output_type: str = "timetable_entries"


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=300)
    output_type: str = "timetable_entries"
    columns: list[dict]
    delimiter: str = ","
    include_header: bool = True


def _http(exc: exporter.ExportError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if exc.code == "not_found" else 422
    if exc.code == "interpretation_failed":
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(code, exc.message)


async def owned_template(template_id: str, school_id: str) -> dict:
    row = await db.fetchrow("""
        SELECT id, name, description, columns, delimiter, include_header,
               output_type, created_at, updated_at
        FROM csv_output_templates WHERE id = $1 AND school_id = $2
    """, template_id, school_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    template = dict(row)
    template["columns"] = json.loads(template["columns"]) \
        if isinstance(template["columns"], str) else template["columns"]
    return template


async def owned_version(version_id: str, school_id: str) -> dict:
    row = await db.fetchrow("""
        SELECT v.id, v.version_number, v.status, t.name
        FROM timetable_versions v
        JOIN timetables t ON t.id = v.timetable_id
        WHERE v.id = $1 AND v.school_id = $2
    """, version_id, school_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Timetable version not found")
    return dict(row)


# --- Vocabulary ---------------------------------------------------------------

@router.get("/fields")
async def fields(user: CurrentUser,
                 output_type: Annotated[str, Query()] = "timetable_entries") -> dict:
    """What a template can contain. Drives the column picker."""
    if output_type not in exporter.OUTPUT_TYPES:
        raise HTTPException(
            422, f"output_type must be one of {', '.join(exporter.OUTPUT_TYPES)}")
    return {
        "output_type": output_type,
        "output_types": list(exporter.OUTPUT_TYPES),
        "fields": exporter.available_fields(output_type),
        "formats": list(exporter.FORMATS),
        "delimiters": [{"value": k, "label": v}
                       for k, v in exporter.DELIMITERS.items()],
    }


@router.post("/interpret")
async def interpret(payload: InterpretIn,
                    user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Map a description, or a pasted SIS header row, onto Klasser fields.

    Free and instant when the header row is recognised outright; only an
    unrecognised description costs an AI call.
    """
    try:
        return await exporter.interpret(payload.description, payload.output_type,
                                        user["school_id"])
    except exporter.ExportError as exc:
        raise _http(exc) from exc


# --- Templates ----------------------------------------------------------------

@router.get("/templates")
async def list_templates(user: CurrentUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT id, name, description, output_type, delimiter, include_header,
               columns, created_at, updated_at
        FROM csv_output_templates WHERE school_id = $1 ORDER BY name
    """, user["school_id"])
    return [
        dict(r) | {"id": str(r["id"]),
                   "columns": json.loads(r["columns"])
                   if isinstance(r["columns"], str) else r["columns"]}
        for r in rows
    ]


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(payload: TemplateIn,
                          user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]

    if payload.delimiter not in exporter.DELIMITERS:
        raise HTTPException(422, "delimiter must be one of , \\t ; |")

    try:
        columns = exporter.validate_columns(payload.columns, payload.output_type)
    except exporter.ExportError as exc:
        raise _http(exc) from exc

    dupe = await db.fetchval("""
        SELECT 1 FROM csv_output_templates
        WHERE school_id = $1 AND lower(name) = lower($2)
    """, school_id, payload.name)
    if dupe:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"A template called {payload.name} already exists")

    template_id = await db.fetchval("""
        INSERT INTO csv_output_templates
            (school_id, name, description, columns, delimiter, include_header,
             output_type)
        VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id
    """, school_id, payload.name, payload.description, json.dumps(columns),
        payload.delimiter, payload.include_header, payload.output_type)

    log.info("Export template %s created for %s", payload.name, school_id)
    return {"id": str(template_id), "columns": columns}


@router.put("/templates/{template_id}")
async def update_template(template_id: str, payload: TemplateIn,
                          user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_template(template_id, school_id)

    if payload.delimiter not in exporter.DELIMITERS:
        raise HTTPException(422, "delimiter must be one of , \\t ; |")

    try:
        columns = exporter.validate_columns(payload.columns, payload.output_type)
    except exporter.ExportError as exc:
        raise _http(exc) from exc

    clash = await db.fetchval("""
        SELECT 1 FROM csv_output_templates
        WHERE school_id = $1 AND lower(name) = lower($2) AND id <> $3
    """, school_id, payload.name, template_id)
    if clash:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"A template called {payload.name} already exists")

    await db.execute("""
        UPDATE csv_output_templates
        SET name = $3, description = $4, columns = $5, delimiter = $6,
            include_header = $7, output_type = $8, updated_at = now()
        WHERE id = $1 AND school_id = $2
    """, template_id, school_id, payload.name, payload.description,
        json.dumps(columns), payload.delimiter, payload.include_header,
        payload.output_type)
    return {"updated": True, "columns": columns}


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(template_id: str,
                          user: Annotated[dict, Depends(require_owner)]) -> None:
    await owned_template(template_id, user["school_id"])
    await db.execute(
        "DELETE FROM csv_output_templates WHERE id = $1 AND school_id = $2",
        template_id, user["school_id"])


# --- Exporting ----------------------------------------------------------------

@router.get("/preview/{version_id}/{template_id}")
async def preview(version_id: str, template_id: str, user: CurrentUser,
                  rows: Annotated[int, Query(ge=1, le=50)] = 5) -> dict:
    """The first few rows, so a school can check a template before downloading."""
    school_id = user["school_id"]
    await owned_version(version_id, school_id)
    template = await owned_template(template_id, school_id)

    try:
        csv_text = await exporter.generate(version_id, template, limit=rows)
        total = await exporter.row_count(version_id, template["output_type"])
    except exporter.ExportError as exc:
        raise _http(exc) from exc

    lines = csv_text.splitlines()
    return {
        "template": template["name"],
        "output_type": template["output_type"],
        "total_rows": total,
        "showing": max(0, len(lines) - (1 if template["include_header"] else 0)),
        "header": lines[0] if template["include_header"] and lines else None,
        "lines": lines,
        "csv": csv_text,
    }


@router.get("/download/{version_id}/{template_id}")
async def download(version_id: str, template_id: str, user: CurrentUser) -> Response:
    """
    The whole file.

    Served as text/csv with a filename built from the timetable and template, so
    a school downloading three exports can tell them apart afterwards.
    """
    school_id = user["school_id"]
    version = await owned_version(version_id, school_id)
    template = await owned_template(template_id, school_id)

    try:
        csv_text = await exporter.generate(version_id, template)
    except exporter.ExportError as exc:
        raise _http(exc) from exc

    def safe(text: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "-"
                       for c in text).strip("-")[:60] or "export"

    filename = (f"{safe(version['name'])}-v{version['version_number']}"
                f"-{safe(template['name'])}.csv")

    log.info("Exported %s rows for %s using template %s",
             csv_text.count("\n"), school_id, template["name"])

    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

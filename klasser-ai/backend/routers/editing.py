"""
AI-assisted timetable editing (EDITING.md).

Asking is free; applying costs credits. The split matters: a school can try
wording a request several ways without paying for the ones that turned out to
mean something else.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from routers.auth import CurrentUser, require_owner
from services import billing as billing_service
from services import editing as editing_service

log = logging.getLogger("klasser.editing")

router = APIRouter(tags=["editing"])

CODE_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "not_a_draft": status.HTTP_409_CONFLICT,
    "rejected": status.HTTP_409_CONFLICT,
    "validation_failed": 422,
    "interpretation_failed": status.HTTP_502_BAD_GATEWAY,
    "bad_field": 422,
}


class RequestIn(BaseModel):
    version_id: str
    request_text: str = Field(min_length=3, max_length=500)


def _http(exc: editing_service.EditError) -> HTTPException:
    return HTTPException(
        CODE_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST), exc.message)


@router.post("/request", status_code=status.HTTP_201_CREATED)
async def request_edit(payload: RequestIn,
                       user: Annotated[dict, Depends(require_owner)]) -> dict:
    """
    Interpret a plain-English change and check whether it is allowed.

    Nothing is written to the timetable and nothing is charged. The response
    carries the proposed before/after and a verdict; applying it is a separate
    call the school makes after seeing the diff.
    """
    try:
        return await editing_service.interpret(
            payload.version_id, payload.request_text, user)
    except editing_service.EditError as exc:
        raise _http(exc) from exc


@router.get("/{edit_id}")
async def get_edit(edit_id: str, user: CurrentUser) -> dict:
    try:
        return await editing_service.describe(edit_id, user["school_id"])
    except editing_service.EditError as exc:
        raise _http(exc) from exc


@router.post("/{edit_id}/apply")
async def apply_edit(edit_id: str,
                     user: Annotated[dict, Depends(require_owner)]) -> dict:
    """Commit an approved change and charge for the session."""
    try:
        return await editing_service.apply_edit(edit_id, user)
    except editing_service.EditError as exc:
        raise _http(exc) from exc
    except billing_service.BillingError as exc:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED,
                            exc.message) from exc


@router.post("/{edit_id}/reject")
async def reject_edit(edit_id: str,
                      user: Annotated[dict, Depends(require_owner)]) -> dict:
    try:
        return await editing_service.reject(edit_id, user)
    except editing_service.EditError as exc:
        raise _http(exc) from exc


@router.get("/version/{version_id}/history")
async def edit_history(version_id: str, user: CurrentUser,
                       limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    rows = await editing_service.history(version_id, user["school_id"])
    return rows[:limit]

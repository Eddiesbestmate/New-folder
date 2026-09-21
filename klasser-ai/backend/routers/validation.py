"""Validation results and quorum reporting (Phase 7)

Stub. Endpoints are added in the phase noted above; this file exists so main.py
can register the router from Phase 1 onward.
"""

from fastapi import APIRouter

from routers.auth import CurrentUser

router = APIRouter(tags=["validation"])


@router.get("/_stub")
async def stub(user: CurrentUser) -> dict:
    # Authenticated even though it returns nothing. An unauthenticated
    # endpoint here is a hole waiting for the first real handler.
    return {"router": "validation", "status": "not_implemented"}

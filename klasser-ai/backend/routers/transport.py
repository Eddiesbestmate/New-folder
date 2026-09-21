"""
Transport - bus fleet and inter-campus routes.

The solver itself lives in the pipeline (Layer 7, deterministic per
TRANSPORT.md). This router manages the inputs it needs - which buses exist,
where they are based, and how long it takes to get between campuses - plus a
read-only view of what the solver produced.

Transport only matters for a multi-campus school. A single-campus school never
has a student who needs to be somewhere else.
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from models import database as db
from routers.auth import CurrentUser, require_owner
from routers.onboarding import recompute

log = logging.getLogger("klasser.transport")

router = APIRouter(tags=["transport"])


class BusIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    capacity: int = Field(ge=1, le=200)
    home_campus_id: str


class RouteIn(BaseModel):
    from_campus_id: str
    to_campus_id: str
    travel_minutes: int = Field(ge=1, le=600)

    @field_validator("to_campus_id")
    @classmethod
    def not_the_same_campus(cls, v, info):
        if v == info.data.get("from_campus_id"):
            raise ValueError("A route must go between two different campuses")
        return v


async def owned_campus(school_id: str, campus_id: str) -> str:
    ok = await db.fetchval(
        "SELECT 1 FROM campuses WHERE id = $1 AND school_id = $2",
        campus_id, school_id)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Unknown campus for this school")
    return campus_id


# --- Overview ----------------------------------------------------------------

@router.get("/overview")
async def overview(user: CurrentUser) -> dict:
    """
    Whether transport applies, and whether it is set up well enough to solve.

    A school with two campuses but no routes will generate a timetable in which
    cross-campus students simply cannot travel, so the gaps are reported here
    rather than discovered at generation time.
    """
    school_id = user["school_id"]

    campuses = await db.fetch("""
        SELECT id, name FROM campuses WHERE school_id = $1 ORDER BY created_at
    """, school_id)
    buses = await db.fetchval(
        "SELECT count(*) FROM buses WHERE school_id = $1", school_id)
    routes = await db.fetch("""
        SELECT from_campus_id, to_campus_id FROM routes WHERE school_id = $1
    """, school_id)

    needed = {
        (str(a["id"]), str(b["id"]))
        for a in campuses for b in campuses if a["id"] != b["id"]
    }
    have = {(str(r["from_campus_id"]), str(r["to_campus_id"])) for r in routes}
    missing = needed - have

    by_id = {str(c["id"]): c["name"] for c in campuses}

    return {
        "applies": len(campuses) > 1,
        "campuses": len(campuses),
        "buses": buses,
        "routes": len(routes),
        "seats": await db.fetchval(
            "SELECT coalesce(sum(capacity), 0) FROM buses WHERE school_id = $1",
            school_id),
        "missing_routes": [
            {"from": by_id.get(a, "?"), "to": by_id.get(b, "?"),
             "from_id": a, "to_id": b}
            for a, b in sorted(missing)
        ],
        "ready": len(campuses) <= 1 or (buses > 0 and not missing),
    }


# --- Buses -------------------------------------------------------------------

@router.get("/buses")
async def list_buses(user: CurrentUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT b.id, b.name, b.capacity, b.home_campus_id, c.name AS home_campus
        FROM buses b
        JOIN campuses c ON c.id = b.home_campus_id
        WHERE b.school_id = $1
        ORDER BY b.name
    """, user["school_id"])
    return [dict(r) | {"id": str(r["id"]),
                       "home_campus_id": str(r["home_campus_id"])} for r in rows]


@router.post("/buses", status_code=status.HTTP_201_CREATED)
async def create_bus(payload: BusIn,
                     user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    await owned_campus(school_id, payload.home_campus_id)

    dupe = await db.fetchval("""
        SELECT 1 FROM buses WHERE school_id = $1 AND lower(name) = lower($2)
    """, school_id, payload.name)
    if dupe:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"A bus called {payload.name} already exists")

    bus_id = await db.fetchval("""
        INSERT INTO buses (school_id, name, capacity, home_campus_id)
        VALUES ($1, $2, $3, $4) RETURNING id
    """, school_id, payload.name, payload.capacity, payload.home_campus_id)

    await recompute(school_id)
    await _sync_complexity(school_id)
    return {"id": str(bus_id)}


@router.patch("/buses/{bus_id}")
async def update_bus(bus_id: str, payload: BusIn,
                     user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    exists = await db.fetchval(
        "SELECT 1 FROM buses WHERE id = $1 AND school_id = $2", bus_id, school_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bus not found")
    await owned_campus(school_id, payload.home_campus_id)

    await db.execute("""
        UPDATE buses SET name = $3, capacity = $4, home_campus_id = $5
        WHERE id = $1 AND school_id = $2
    """, bus_id, school_id, payload.name, payload.capacity,
        payload.home_campus_id)
    return {"updated": True}


@router.delete("/buses/{bus_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bus(bus_id: str,
                     user: Annotated[dict, Depends(require_owner)]) -> None:
    school_id = user["school_id"]
    exists = await db.fetchval(
        "SELECT 1 FROM buses WHERE id = $1 AND school_id = $2", bus_id, school_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bus not found")

    # A bus that has carried students in a published timetable cannot be
    # removed without leaving that schedule referring to nothing.
    used = await db.fetchval(
        "SELECT count(*) FROM transport_schedule WHERE bus_id = $1", bus_id)
    if used:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This bus appears in {used} scheduled movements. Archive the "
            "timetable before removing it.")

    await db.execute("DELETE FROM buses WHERE id = $1 AND school_id = $2",
                     bus_id, school_id)
    await recompute(school_id)
    await _sync_complexity(school_id)


# --- Routes ------------------------------------------------------------------

@router.get("/routes")
async def list_routes(user: CurrentUser) -> list[dict]:
    rows = await db.fetch("""
        SELECT r.id, r.from_campus_id, r.to_campus_id, r.travel_minutes,
               f.name AS from_campus, t.name AS to_campus
        FROM routes r
        JOIN campuses f ON f.id = r.from_campus_id
        JOIN campuses t ON t.id = r.to_campus_id
        WHERE r.school_id = $1
        ORDER BY f.name, t.name
    """, user["school_id"])
    return [
        dict(r) | {"id": str(r["id"]),
                   "from_campus_id": str(r["from_campus_id"]),
                   "to_campus_id": str(r["to_campus_id"])}
        for r in rows
    ]


@router.post("/routes", status_code=status.HTTP_201_CREATED)
async def create_route(payload: RouteIn,
                       user: Annotated[dict, Depends(require_owner)],
                       both_ways: Annotated[bool, Query()] = True) -> dict:
    """
    Add a route between two campuses.

    Routes are directional in the schema because a return leg can take a
    different time, but a school almost always wants both. `both_ways` creates
    the reverse at the same travel time unless it already exists.
    """
    school_id = user["school_id"]
    await owned_campus(school_id, payload.from_campus_id)
    await owned_campus(school_id, payload.to_campus_id)

    created = []
    pairs = [(payload.from_campus_id, payload.to_campus_id)]
    if both_ways:
        pairs.append((payload.to_campus_id, payload.from_campus_id))

    for origin, destination in pairs:
        exists = await db.fetchval("""
            SELECT 1 FROM routes
            WHERE school_id = $1 AND from_campus_id = $2 AND to_campus_id = $3
        """, school_id, origin, destination)
        if exists:
            continue
        route_id = await db.fetchval("""
            INSERT INTO routes (school_id, from_campus_id, to_campus_id,
                                travel_minutes)
            VALUES ($1, $2, $3, $4) RETURNING id
        """, school_id, origin, destination, payload.travel_minutes)
        created.append(str(route_id))

    if not created:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "That route already exists")

    await _sync_complexity(school_id)
    return {"created": created}


@router.patch("/routes/{route_id}")
async def update_route(route_id: str, payload: RouteIn,
                       user: Annotated[dict, Depends(require_owner)]) -> dict:
    school_id = user["school_id"]
    exists = await db.fetchval(
        "SELECT 1 FROM routes WHERE id = $1 AND school_id = $2",
        route_id, school_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Route not found")

    await db.execute("""
        UPDATE routes SET travel_minutes = $3 WHERE id = $1 AND school_id = $2
    """, route_id, school_id, payload.travel_minutes)
    return {"updated": True}


@router.delete("/routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_route(route_id: str,
                       user: Annotated[dict, Depends(require_owner)]) -> None:
    school_id = user["school_id"]
    exists = await db.fetchval(
        "SELECT 1 FROM routes WHERE id = $1 AND school_id = $2",
        route_id, school_id)
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Route not found")

    await db.execute("DELETE FROM routes WHERE id = $1 AND school_id = $2",
                     route_id, school_id)
    await _sync_complexity(school_id)


# --- Schedule ----------------------------------------------------------------

@router.get("/schedule/{version_id}")
async def schedule(version_id: str, user: CurrentUser,
                   day: Annotated[Optional[int], Query(ge=1, le=20)] = None) -> dict:
    """
    What the solver produced, as a day chain per bus (TRANSPORT.md).

    Empty legs are included: every repositioning movement is a real record, so
    a school can see why a bus is where it is rather than inferring it.
    """
    owned = await db.fetchval("""
        SELECT 1 FROM timetable_versions WHERE id = $1 AND school_id = $2
    """, version_id, user["school_id"])
    if not owned:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Timetable version not found")

    rows = await db.fetch("""
        SELECT ts.id, ts.day_number, ts.bus_id, b.name AS bus, b.capacity,
               to_char(ts.departure_time, 'HH24:MI') AS departure,
               to_char(ts.arrival_time, 'HH24:MI') AS arrival,
               f.name AS from_campus, t.name AS to_campus,
               ts.passenger_count, ts.is_empty_leg
        FROM transport_schedule ts
        JOIN buses b ON b.id = ts.bus_id
        JOIN campuses f ON f.id = ts.from_campus_id
        JOIN campuses t ON t.id = ts.to_campus_id
        WHERE ts.version_id = $1 AND ($2::int IS NULL OR ts.day_number = $2)
        ORDER BY ts.day_number, b.name, ts.departure_time
    """, version_id, day)

    chains: dict[str, dict] = {}
    for row in rows:
        key = f"{row['day_number']}-{row['bus']}"
        chain = chains.setdefault(key, {
            "day": row["day_number"], "bus": row["bus"],
            "capacity": row["capacity"], "movements": [],
            "passengers": 0, "empty_legs": 0,
        })
        chain["movements"].append(dict(row) | {"id": str(row["id"]),
                                               "bus_id": str(row["bus_id"])})
        chain["passengers"] += row["passenger_count"]
        chain["empty_legs"] += 1 if row["is_empty_leg"] else 0

    total = len(rows)
    empty = sum(1 for r in rows if r["is_empty_leg"])

    return {
        "movements": total,
        "empty_legs": empty,
        # A high proportion of empty running means buses are repositioning more
        # than they are carrying anyone, which is worth surfacing.
        "empty_leg_pct": round(100 * empty / total) if total else 0,
        "passengers": sum(r["passenger_count"] for r in rows),
        "days": sorted({r["day_number"] for r in rows}),
        "chains": sorted(chains.values(),
                         key=lambda c: (c["day"], c["bus"])),
    }


async def _sync_complexity(school_id: str) -> None:
    """Keep the cost inputs in step with the fleet, since both affect pricing."""
    await db.execute("""
        UPDATE school_complexity SET
            bus_route_count = (SELECT count(*) FROM routes WHERE school_id = $1),
            transport_enabled = (
                SELECT count(*) > 0 FROM buses WHERE school_id = $1),
            last_calculated_at = now()
        WHERE school_id = $1
    """, school_id)

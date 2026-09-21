"""
KLASSER AI - FastAPI application entry point.

    uvicorn main:app --reload --port 8000
"""

import logging
from contextlib import asynccontextmanager

# Must run before any TLS connection is made. This machine's network intercepts
# TLS with a private root CA that certifi does not carry, so verification is
# pointed at the OS trust store instead of being weakened.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - optional on clean networks
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
import keys
from models import database as db
from routers import (
    allocation,
    auth,
    billing,
    dev,
    editing,
    export,
    import_router,
    intake,
    notifications,
    onboarding,
    schools,
    support,
    timetables,
    transport,
    users,
    validation,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("klasser")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting %s", config.APP_NAME)

    # Report missing configuration by name at startup, rather than letting it
    # surface later as an opaque failure inside a request.
    for label, names in (
        ("database", keys.REQUIRED_FOR_DB),
        ("auth", keys.REQUIRED_FOR_AUTH),
        ("admin operations", keys.REQUIRED_FOR_ADMIN),
        ("email", keys.REQUIRED_FOR_EMAIL),
        ("AI key storage", keys.REQUIRED_FOR_AI_KEYS),
    ):
        absent = keys.missing(names)
        if absent:
            log.warning("Not configured for %s - missing: %s", label, ", ".join(absent))

    await db.connect()

    # Decrypt provider keys into memory once, rather than per call. Never fatal:
    # the app must still serve everything that does not need AI.
    try:
        from services import ai_cluster

        await ai_cluster.load_key_pools()
    except Exception:
        log.exception("Could not load AI key pools - AI calls will fail until fixed")

    yield
    await db.disconnect()
    log.info("Shutdown complete")


app = FastAPI(
    title=config.APP_NAME,
    description="AI-powered school timetabling",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["system"])
async def root() -> dict:
    return {"app": config.APP_NAME, "version": app.version, "docs": "/docs"}


@app.get("/health", tags=["system"])
async def health() -> JSONResponse:
    """Liveness plus a real database round-trip. Used by Render and the dev portal."""
    db_ok = await db.is_healthy()
    body = {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "unavailable",
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)


app.include_router(auth.router, prefix="/auth")
app.include_router(onboarding.router, prefix="/onboarding")
app.include_router(schools.router, prefix="/schools")
app.include_router(intake.router, prefix="/intake")
app.include_router(allocation.router, prefix="/allocation")
app.include_router(transport.router, prefix="/transport")
app.include_router(validation.router, prefix="/validation")
app.include_router(editing.router, prefix="/editing")
app.include_router(timetables.router, prefix="/timetables")
app.include_router(import_router.router, prefix="/import")
app.include_router(export.router, prefix="/export")
app.include_router(billing.router, prefix="/billing")
app.include_router(support.router, prefix="/support")
app.include_router(notifications.router, prefix="/notifications")
app.include_router(users.router, prefix="/users")
app.include_router(dev.router, prefix="/dev")

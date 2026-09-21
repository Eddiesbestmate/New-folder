# =============================================================================
# KLASSER AI - DATABASE ACCESS
# Thin asyncpg wrapper. One pool for the process, opened on startup and closed
# on shutdown. Every query in the app goes through the helpers here.
# =============================================================================

import logging
from typing import Any, Optional

import asyncpg

import keys

log = logging.getLogger("klasser.db")

_pool: Optional[asyncpg.Pool] = None


def normalise_dsn(url: str) -> str:
    """
    asyncpg takes a plain libpq DSN. Supabase and most tutorials hand out a
    SQLAlchemy-style URL ('postgresql+asyncpg://...'), which asyncpg rejects
    with an unhelpful error, so strip the driver suffix.
    """
    if not url:
        return url
    for prefix, replacement in (
        ("postgresql+asyncpg://", "postgresql://"),
        ("postgres+asyncpg://", "postgresql://"),
        ("postgres://", "postgresql://"),
    ):
        if url.startswith(prefix):
            return replacement + url[len(prefix):]
    return url


def ssl_setting():
    """
    TLS mode for the Postgres connection.

    Returns False (no TLS) only when DATABASE_SSL=disable is set explicitly.
    That exists for one situation: a local network that blocks TLS on port 5432,
    where plaintext is the only way to reach the database. It sends credentials
    and rows in the clear, so it warns every time and must never be set in a
    deployed environment.
    """
    if keys.DATABASE_SSL == "disable":
        log.warning(
            "DATABASE_SSL=disable - connecting to Postgres WITHOUT TLS. "
            "The password and all data cross the network in cleartext. "
            "Local development only; never deploy with this set."
        )
        return False
    return True


async def connect() -> Optional[asyncpg.Pool]:
    """
    Open the connection pool. Returns None if DATABASE_URL is unset, so the API
    still boots for frontend work before Supabase exists. Any endpoint that
    touches the database will then fail loudly via require_pool().
    """
    global _pool

    if _pool is not None:
        return _pool

    if keys.missing(keys.REQUIRED_FOR_DB):
        log.warning(
            "DATABASE_URL is not set - starting without a database. "
            "Endpoints that read or write data will return 503."
        )
        return None

    dsn = normalise_dsn(keys.DATABASE_URL)
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=10,
        command_timeout=60,
        # Supabase's pooler does not support prepared statement caching.
        statement_cache_size=0,
        ssl=ssl_setting(),
    )

    version = await _pool.fetchval("SELECT version()")
    log.info("Database connected: %s", version.split(",")[0])
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("Database pool closed")


def pool() -> Optional[asyncpg.Pool]:
    return _pool


def require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database is not configured (DATABASE_URL is unset)")
    return _pool


async def is_healthy() -> bool:
    if _pool is None:
        return False
    try:
        return await _pool.fetchval("SELECT 1") == 1
    except Exception:
        log.exception("Database health check failed")
        return False


# --- Query helpers -----------------------------------------------------------
# Thin passthroughs so callers never touch the pool object directly. Always pass
# school_id in the WHERE clause of school-scoped queries - see BUILD_ORDER.md.

async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    return await require_pool().fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> Optional[asyncpg.Record]:
    return await require_pool().fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    return await require_pool().fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    return await require_pool().execute(query, *args)


async def executemany(query: str, args: list[Any]) -> None:
    """
    Run one statement over many argument tuples in a single round trip.

    Anything writing more than a handful of rows should use this: a statement
    per row exceeds the command timeout on realistic data volumes.
    """
    if not args:
        return
    await require_pool().executemany(query, args)


def transaction():
    """
    Usage:
        async with db.transaction() as conn:
            await conn.execute(...)
    """
    return _TransactionContext()


class _TransactionContext:
    def __init__(self) -> None:
        self._conn_ctx = None
        self._tx = None

    async def __aenter__(self) -> asyncpg.Connection:
        self._conn_ctx = require_pool().acquire()
        conn = await self._conn_ctx.__aenter__()
        self._tx = conn.transaction()
        await self._tx.__aenter__()
        return conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._tx.__aexit__(exc_type, exc, tb)
        await self._conn_ctx.__aexit__(exc_type, exc, tb)

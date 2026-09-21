"""
Runtime settings, read from the `settings` table.

Model assignments, thresholds and pricing all live in the database so the dev
portal can change them without a redeploy. Values are cached in process; the
dev portal calls invalidate() after any write.
"""

import logging
from typing import Any, Optional

from models import database as db

log = logging.getLogger("klasser.settings")

_cache: dict[str, str] = {}


async def get(key: str, default: Optional[str] = None) -> Optional[str]:
    if key in _cache:
        return _cache[key]

    row = await db.fetchrow("SELECT value FROM settings WHERE key = $1", key)
    if row is None:
        return default

    _cache[key] = row["value"]
    return row["value"]


async def get_int(key: str, default: int) -> int:
    raw = await get(key)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        log.warning("Setting %s=%r is not an integer, using %s", key, raw, default)
        return default


async def get_float(key: str, default: float) -> float:
    raw = await get(key)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        log.warning("Setting %s=%r is not a number, using %s", key, raw, default)
        return default


async def get_bool(key: str, default: bool = False) -> bool:
    raw = await get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


async def get_many(prefix: str) -> dict[str, str]:
    """Every setting whose key starts with prefix. Not cached - used by the dev portal."""
    rows = await db.fetch(
        "SELECT key, value FROM settings WHERE key LIKE $1 || '%' ORDER BY key", prefix)
    return {r["key"]: r["value"] for r in rows}


async def set_value(key: str, value: str) -> None:
    await db.execute("""
        INSERT INTO settings (key, value, category, description)
        VALUES ($1, $2, 'custom', NULL)
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = now()
    """, key, value)
    _cache.pop(key, None)


def invalidate(key: Optional[str] = None) -> None:
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def cached() -> dict[str, Any]:
    return dict(_cache)

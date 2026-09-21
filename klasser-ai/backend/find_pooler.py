"""
Find the working Supabase pooler endpoint for this project.

The direct host (db.<ref>.supabase.co) is IPv6-only. On an IPv4-only network it
cannot be resolved at all, so the connection pooler must be used instead. The
pooler hostname includes the AWS region, which is not discoverable via DNS -
every region's hostname resolves. This tries each one until one authenticates.

    python find_pooler.py

Prints a DATABASE_URL line to paste into .env.
"""

import asyncio
import re
import sys
from urllib.parse import quote, urlparse

import asyncpg

import keys

REGIONS = [
    "ap-southeast-2", "ap-southeast-1", "ap-northeast-1", "ap-south-1",
    "us-east-1", "us-east-2", "us-west-1", "eu-west-1", "eu-west-2",
    "eu-central-1", "ca-central-1", "sa-east-1",
]


def parse_current() -> tuple[str, str]:
    """Pull project ref and password out of whatever DATABASE_URL holds."""
    url = keys.DATABASE_URL or ""
    m = re.search(r"db\.([a-z0-9]+)\.supabase\.co", url)
    ref = m.group(1) if m else ""

    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    password = parsed.password or ""

    if not ref:
        m = re.search(r"postgres\.([a-z0-9]+)[:@]", url)
        ref = m.group(1) if m else ""

    return ref, password


async def try_region(ref: str, password: str, region: str, port: int) -> tuple[str, str | None, str]:
    """Returns (region:port label, working dsn or None, short note)."""
    host = f"aws-0-{region}.pooler.supabase.com"
    dsn = f"postgresql://postgres.{ref}:{quote(password, safe='')}@{host}:{port}/postgres"
    label = f"{region}:{port}"
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn, statement_cache_size=0), timeout=12
        )
    except asyncio.TimeoutError:
        return label, None, "timeout"
    except Exception as exc:
        msg = str(exc).strip().splitlines()[0]
        # "Tenant or user not found" = right pooler, wrong region for this project.
        if "Tenant or user not found" in msg:
            return label, None, "wrong region"
        return label, None, msg[:70]
    try:
        await conn.fetchval("SELECT 1")
        return label, dsn, "connected"
    finally:
        await conn.close()


async def main() -> int:
    ref, password = parse_current()
    if not ref or not password:
        print("Could not read project ref / password from DATABASE_URL in .env")
        return 1

    print(f"Project ref: {ref}")
    print("Probing all pooler regions in parallel\n")

    # All regions at once - one timeout window total, not one per region.
    for port in (5432, 6543):
        print(f"  port {port}:")
        outcomes = await asyncio.gather(
            *(try_region(ref, password, r, port) for r in REGIONS)
        )
        for label, dsn, note in outcomes:
            print(f"    {label:<24} {note}")
            if dsn:
                print(f"\nCONNECTED via {label}\n")
                print("Put this in .env as DATABASE_URL:")
                print(dsn)
                return 0
        print()

    print("No pooler region accepted the connection.")
    print("Copy the exact string from the dashboard's Connect button")
    print("(Session pooler tab) - its password may differ from the direct one.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

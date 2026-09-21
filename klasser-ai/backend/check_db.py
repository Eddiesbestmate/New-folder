"""Quick database reachability check, for when migrations start failing."""

import asyncio
import socket
import struct
import sys
from urllib.parse import urlparse

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import keys
from models import database as db

SSL_REQUEST = struct.pack("!ii", 8, 80877103)


def wire_probe(host: str, port: int) -> str:
    """Does anything at that address speak the Postgres protocol?"""
    try:
        with socket.create_connection((host, port), timeout=10) as s:
            s.settimeout(10)
            s.sendall(SSL_REQUEST)
            data = s.recv(1)
            if not data:
                return "connected, then closed without replying"
            return {b"S": "speaks Postgres, TLS offered",
                    b"N": "speaks Postgres, TLS refused"}.get(
                        data, f"unexpected byte {data!r}")
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


async def main() -> int:
    dsn = db.normalise_dsn(keys.DATABASE_URL or "")
    if not dsn:
        print("DATABASE_URL is not set.")
        return 1

    parsed = urlparse(dsn)
    host, port = parsed.hostname, parsed.port or 5432
    print(f"Target: {host}:{port}")
    print(f"DATABASE_SSL: {keys.DATABASE_SSL}\n")

    try:
        addresses = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        print(f"DNS: {sorted({a[4][0] for a in addresses})}")
    except OSError as exc:
        print(f"DNS FAILED: {exc}")
        return 1

    print(f"Wire: {wire_probe(host, port)}\n")

    for attempt in range(1, 4):
        try:
            await db.connect()
            version = await db.fetchval("SELECT version()")
            tables = await db.fetchval("""
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """)
            print(f"CONNECTED on attempt {attempt}")
            print(f"  {version[:70]}")
            print(f"  {tables} tables")
            await db.disconnect()
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"attempt {attempt}: {type(exc).__name__}: {str(exc)[:120]}")
            await asyncio.sleep(3)

    print("\nCould not connect. The pooler reset the connection three times.")
    print("This is usually the network rather than the database - the same")
    print("symptom appeared when TLS was being intercepted on port 5432.")
    return 1


sys.exit(asyncio.run(main()))

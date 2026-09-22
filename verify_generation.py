#!/usr/bin/env python3
"""Start a generation and follow it to a verdict."""

import asyncio
import sys

import aiohttp

API_BASE = "http://localhost:8000"
EMAIL = "test@test.com"
PASSWORD = "A@2024Lu"
SUPABASE_URL = "https://zglgkwhocldlvrfhikfd.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpnbGdrd2hvY2xkbHZyZmhpa2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzOTk2NzIsImV4cCI6MjEwMzk3NTY3Mn0.-PKTIaaotkt63P7BW-oMdp_jMTlr1wTbVSXpsITHMy8"


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                          json={"email": EMAIL, "password": PASSWORD},
                          headers={"apikey": SUPABASE_KEY}) as r:
            token = (await r.json())["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        async with s.post(f"{API_BASE}/allocation/start",
                          json={"name": "Curriculum verification run"},
                          headers=auth) as r:
            body = await r.json()
            if r.status != 202:
                print(f"Refused ({r.status}):")
                print(body)
                sys.exit(1)
        timetable_id = body["timetable_id"]
        print(f"Queued {timetable_id}, {body['estimated_credits']} credits, "
              f"{body['queued_ahead']} ahead")
        for warning in body.get("warnings") or []:
            print(f"  warning: {warning}")

        attempt = None
        for _ in range(150):
            await asyncio.sleep(4)
            async with s.get(
                    f"{API_BASE}/allocation/timetable/{timetable_id}/attempts",
                    headers=auth) as r:
                # Checked, because an error body is a dict and indexing it
                # with -1 raises KeyError instead of saying what went wrong.
                if r.status != 200:
                    print(f"  attempts: {r.status} {(await r.text())[:200]}")
                    continue
                attempts = await r.json()
            if not attempts:
                print("  waiting for a worker to claim it...")
                continue
            attempt = attempts[-1]["id"]

            async with s.get(f"{API_BASE}/allocation/{attempt}/status",
                             headers=auth) as r:
                status = await r.json()
            print(f"  {status.get('progress_pct', 0)}% "
                  f"{status.get('current_stage') or status.get('job_status')} "
                  f"{status.get('current_chunk') or ''}")

            if status.get("status") in ("complete", "failed") \
                    or status.get("job_status") in ("complete", "failed"):
                print(f"\nFINAL: {status.get('status')} / "
                      f"{status.get('job_status')}")
                if status.get("failure_reason"):
                    print(f"reason: {status['failure_reason']}")
                if status.get("version_id"):
                    print(f"version: {status['version_id']}")
                return
        print("timed out watching")


asyncio.run(main())

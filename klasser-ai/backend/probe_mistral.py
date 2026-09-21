"""Show the full, untruncated error Mistral returns for each magistral model."""

import asyncio

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import config
from models import database as db
from services import ai_cluster

MODELS = ["magistral-medium-latest", "magistral-small-latest",
          "mistral-small-latest"]


async def go() -> None:
    await db.connect()
    await ai_cluster.load_key_pools()

    pool = ai_cluster._pools.get("mistral")
    if not pool:
        print("No mistral keys stored.")
        return

    from openai import AsyncOpenAI
    from services import encryption

    # One request per key, well spaced. If every key fails on its first call,
    # the accounts are not activated - that is not a throughput problem.
    for entry in pool.entries:
        client = AsyncOpenAI(api_key=entry.key,
                             base_url=config.PROVIDER_URLS["mistral"],
                             max_retries=0)
        try:
            r = await client.chat.completions.create(
                model="magistral-small-latest",
                messages=[{"role": "user",
                           "content": 'Reply with the JSON object {"ok": true}.'}],
                max_tokens=200,
                temperature=0,
            )
            print(f"OK    {entry.label} ({encryption.mask(entry.key)}): "
                  f"{r.choices[0].message.content[:60]}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {entry.label} ({encryption.mask(entry.key)}): "
                  f"{type(exc).__name__} {str(exc)[:120]}")
        await asyncio.sleep(20)

    await db.disconnect()


asyncio.run(go())

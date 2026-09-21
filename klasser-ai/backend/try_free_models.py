"""
Try candidate free OpenRouter models on a prompt shaped like the pipeline's.

Connectivity alone is not enough: these layers need a model that returns strict
JSON matching a schema. This sends a miniature class-splitting task and checks
the response actually parses into the expected shape.
"""

import asyncio
import json

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import config
from models import database as db
from services import ai_cluster

CANDIDATES = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "thinkingmachines/inkling:free",
    "openai/gpt-oss-120b",          # currently working, for comparison
]

PROMPT = """Split these 6 students into exactly 2 balanced classes.

Students (i is the index you must use):
[{"i": 0, "name": "A"}, {"i": 1, "name": "B"}, {"i": 2, "name": "C"},
 {"i": 3, "name": "D"}, {"i": 4, "name": "E"}, {"i": 5, "name": "F"}]

Every student appears exactly once across all groups.

Return ONLY JSON: {"groups": [[0, 1, 2], [3, 4, 5]]}"""


def valid(text: str) -> tuple[bool, str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return False, "no JSON found"
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            return False, f"unparseable: {exc}"

    groups = data.get("groups")
    if not isinstance(groups, list) or len(groups) != 2:
        return False, f"expected 2 groups, got {groups}"
    seen = sorted(n for g in groups for n in g)
    if seen != [0, 1, 2, 3, 4, 5]:
        return False, f"not a partition: {seen}"
    return True, f"{[len(g) for g in groups]}"


async def go() -> None:
    await db.connect()
    await ai_cluster.load_key_pools()

    from openai import AsyncOpenAI

    entry = ai_cluster._pools["openrouter"].entries[0]
    client = AsyncOpenAI(api_key=entry.key,
                         base_url=config.PROVIDER_URLS["openrouter"],
                         max_retries=0)

    print(f"{'MODEL':<46} {'RESULT':<10} DETAIL")
    for model in CANDIDATES:
        try:
            r = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT}],
                temperature=0,
                max_tokens=2000,
                response_format={"type": "json_object"},
            )
            text = r.choices[0].message.content or ""
            ok, detail = valid(text)
            tokens = getattr(getattr(r, "usage", None), "total_tokens", 0)
            print(f"{model:<46} {'OK' if ok else 'BAD':<10} "
                  f"{detail} ({tokens} tokens)")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).replace("\n", " ")
            print(f"{model:<46} {'ERROR':<10} {msg[:90]}")
        await asyncio.sleep(3)

    await db.disconnect()


asyncio.run(go())

"""
Fetch live pricing for the models we would actually use, from OpenRouter.

OpenRouter's /models endpoint returns per-token prices, so this is measured
rather than recalled. Costs are then projected onto the measured workload of
one 700-student generation.
"""

import asyncio

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import httpx

# Measured on the 700-student profile: 121k input / 24k output for allocation,
# 110k input per validator (day-chunked), ~2k output per validator.
ALLOC_IN, ALLOC_OUT = 121_000, 24_000
VAL_IN, VAL_OUT = 110_000, 2_000

CANDIDATES = [
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
    "google/gemini-3-flash-preview",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-5",
    "mistralai/magistral-medium-2506",
    "mistralai/mistral-small-latest",
    "meta-llama/llama-3.3-70b-instruct",
    "qwen/qwen3-32b",
    "deepseek/deepseek-chat",
]


def dollars(value: float) -> str:
    return f"${value:,.4f}" if value < 1 else f"${value:,.2f}"


async def go() -> None:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get("https://openrouter.ai/api/v1/models")
    if r.status_code != 200:
        print(f"OpenRouter returned HTTP {r.status_code}")
        return

    catalogue = {m["id"]: m for m in r.json().get("data", [])}

    print(f"{'MODEL':<38} {'IN $/M':>9} {'OUT $/M':>9} "
          f"{'ALLOC RUN':>11} {'VALIDATOR':>11}")
    print("-" * 82)

    for model_id in CANDIDATES:
        model = catalogue.get(model_id)
        if model is None:
            print(f"{model_id:<38} {'not on OpenRouter':>43}")
            continue

        pricing = model.get("pricing", {})
        try:
            in_rate = float(pricing.get("prompt", 0)) * 1_000_000
            out_rate = float(pricing.get("completion", 0)) * 1_000_000
        except (TypeError, ValueError):
            print(f"{model_id:<38} {'unpriced':>43}")
            continue

        alloc = (ALLOC_IN * in_rate + ALLOC_OUT * out_rate) / 1_000_000
        val = (VAL_IN * in_rate + VAL_OUT * out_rate) / 1_000_000

        print(f"{model_id:<38} {in_rate:>9,.2f} {out_rate:>9,.2f} "
              f"{dollars(alloc):>11} {dollars(val):>11}")

    print("\nALLOC RUN  = one 700-student generation's allocation layers")
    print("VALIDATOR  = one validator reviewing that timetable, all days")
    print("OpenRouter adds its fee on top of these list prices.")


asyncio.run(go())

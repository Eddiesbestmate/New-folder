"""Show which model every task and validator is currently assigned to."""

import asyncio

from models import database as db
from services import ai_cluster

# Tasks the pipeline actually calls today, from services/pipeline.py.
CALLED_NOW = {
    "task_interpret",
    "task_class_group_formation",
    "task_block_campus_primary",
    "task_block_campus_fallback",
    "task_teacher_primary",
    "task_teacher_fallback",
}

# Assigned in settings but not reached by any code path yet, and why.
NOT_REACHED = {
    "task_divide": "no routing layer built",
    "task_room_allocation": "Layer 4 is deterministic - ranking leaves no choice",
    "task_duty_assignment": "Layers 6 and 8 are deterministic - all rules are hard",
    "task_transport_format": "Layer 7 is deterministic (TRANSPORT.md)",
    "task_import_interpret": "used by the importer, not the pipeline",
    "task_edit_interpret": "Phase 11",
    "task_export_map": "Phase 14",
    "validator_1": "Phase 7",
    "validator_2": "Phase 7",
    "validator_3": "Phase 7",
    "validator_4": "Phase 7",
}


async def go() -> None:
    await db.connect()

    rows = await db.fetch("""
        SELECT key, value FROM settings
        WHERE key LIKE 'validator_%' OR key LIKE 'task_%'
        ORDER BY key
    """)

    print(f"{'SETTING':<30} {'MODEL':<14} {'PROVIDER':<12} STATUS")
    for r in rows:
        alias = r["value"]
        provider = ai_cluster.MODEL_REGISTRY.get(alias, ("?",))[0]
        if r["key"] in CALLED_NOW:
            status = "called"
        else:
            status = "not reached - " + NOT_REACHED.get(r["key"], "unknown")
        print(f"{r['key']:<30} {alias:<14} {provider:<12} {status}")

    print()
    used = {r["value"] for r in rows if r["key"] in CALLED_NOW}
    for alias in sorted(ai_cluster.MODEL_REGISTRY):
        provider, setting = ai_cluster.MODEL_REGISTRY[alias]
        model = await db.fetchval(
            "SELECT value FROM settings WHERE key = $1", setting)
        mark = "in use now" if alias in used else "configured, not yet called"
        print(f"{alias:<14} {provider:<12} {model:<28} {mark}")

    await db.disconnect()


asyncio.run(go())

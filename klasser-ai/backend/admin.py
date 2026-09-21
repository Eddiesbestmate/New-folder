"""
Klasser admin CLI - manage AI provider keys and dev accounts from the terminal.

The dev portal UI arrives in Phase 16; this covers the same ground until then.
Keys are Fernet-encrypted with ENCRYPTION_KEY before being stored, exactly as
the dev portal endpoints do, and are never printed back.

    python admin.py list-keys
    python admin.py add-key mistral "Key 1"
    python admin.py add-key groq "Key 1" --key gsk_xxx
    python admin.py delete-key <id>
    python admin.py test mistral
    python admin.py test-all
    python admin.py grant-dev you@example.com
"""

import argparse
import asyncio
import getpass
import sys

# Point TLS at the OS trust store before anything opens a connection.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import config
from models import database as db
from services import ai_cluster, encryption


async def list_keys() -> int:
    rows = await db.fetch("""
        SELECT id, provider, key_label, account_label, is_active, updated_at
        FROM api_keys ORDER BY provider, key_label
    """)
    if not rows:
        print("No provider keys stored.\n")
        print("Add one with:  python admin.py add-key mistral \"Key 1\"")
        return 0

    print(f"{'PROVIDER':<12} {'LABEL':<14} {'ACTIVE':<7} {'ACCOUNT':<16} ID")
    for r in rows:
        print(f"{r['provider']:<12} {r['key_label']:<14} "
              f"{'yes' if r['is_active'] else 'no':<7} "
              f"{(r['account_label'] or '-'):<16} {r['id']}")

    counts = await ai_cluster.load_key_pools()
    print(f"\nPools loaded: {counts or 'none'}")
    return 0


async def add_key(provider: str, label: str, key: str | None,
                  account: str | None) -> int:
    if provider not in config.PROVIDER_URLS:
        print(f"Unknown provider '{provider}'. "
              f"Choose from: {', '.join(sorted(config.PROVIDER_URLS))}")
        return 1

    if not encryption.is_configured():
        print("ENCRYPTION_KEY is not set in .env - cannot store keys.")
        return 1

    if not key:
        # Hidden input, so the key does not end up in shell history.
        key = getpass.getpass(f"Paste the {provider} API key (input hidden): ").strip()
    if not key:
        print("No key entered.")
        return 1

    dupe = await db.fetchval(
        "SELECT 1 FROM api_keys WHERE provider = $1 AND key_label = $2",
        provider, label)
    if dupe:
        print(f"{provider} already has a key labelled '{label}'. "
              "Use a different label or delete the old one.")
        return 1

    key_id = await db.fetchval("""
        INSERT INTO api_keys (provider, key_label, encrypted_key, account_label)
        VALUES ($1, $2, $3, $4) RETURNING id
    """, provider, label, encryption.encrypt_key(key), account)

    counts = await ai_cluster.load_key_pools()
    print(f"Stored {provider} '{label}' as {encryption.mask(key)}")
    print(f"  id: {key_id}")
    print(f"  pools now: {counts}")
    return 0


async def delete_key(key_id: str) -> int:
    row = await db.fetchrow(
        "SELECT provider, key_label FROM api_keys WHERE id = $1", key_id)
    if row is None:
        print("No key with that id.")
        return 1

    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE pipeline_log SET api_key_id = NULL WHERE api_key_id = $1", key_id)
        await conn.execute(
            "UPDATE error_log SET api_key_id = NULL WHERE api_key_id = $1", key_id)
        await conn.execute("DELETE FROM api_keys WHERE id = $1", key_id)

    print(f"Deleted {row['provider']} '{row['key_label']}'")
    print(f"  pools now: {await ai_cluster.load_key_pools()}")
    return 0


async def test_one(alias: str) -> bool:
    result = await ai_cluster.test_provider(alias)
    if result.get("ok"):
        print(f"  OK    {alias:<14} {result['latency_ms']}ms  "
              f"reply: {result['reply'][:60]}")
        return True
    print(f"  FAIL  {alias:<14} {result.get('error', 'unknown error')[:100]}")
    return False


async def test(alias: str) -> int:
    await ai_cluster.load_key_pools()
    print(f"Testing {alias}\n")
    return 0 if await test_one(alias) else 1


async def test_all() -> int:
    await ai_cluster.load_key_pools()
    print("Sending one trivial prompt to each configured model\n")

    failed = 0
    for alias in sorted(ai_cluster.MODEL_REGISTRY):
        provider, _ = ai_cluster.MODEL_REGISTRY[alias]
        if not ai_cluster.pool_status().get(provider):
            print(f"  skip  {alias:<14} no {provider} key stored")
            continue
        if not await test_one(alias):
            failed += 1

    print()
    print("All configured providers answered." if not failed
          else f"{failed} provider(s) failed.")
    return 1 if failed else 0


async def list_models(provider: str, filter_text: str | None) -> int:
    """
    Ask a provider which models it actually serves for our key.

    Model names change: providers retire and rename them, and a stale string in
    the settings table shows up as a 404 at generation time. This is also the
    cleanest test of whether a key is valid at all - a 401 here is the key, not
    the model.
    """
    import httpx

    await ai_cluster.load_key_pools()
    pool = ai_cluster.pool_status().get(provider, 0)
    if not pool:
        print(f"No {provider} key stored. Add one first.")
        return 1

    entry = ai_cluster._pools[provider].entries[0]
    base = config.PROVIDER_URLS[provider]

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {entry.key}"})

    if r.status_code == 401:
        print(f"401 Unauthorized from {provider}.")
        print(f"  The stored key ({encryption.mask(entry.key)}) was rejected.")
        print("  The key itself is wrong, expired, or from a different provider.")
        print(f"  Delete it and re-add:  python admin.py list-keys")
        return 1
    if r.status_code != 200:
        print(f"{provider} returned HTTP {r.status_code}: {r.text[:300]}")
        return 1

    try:
        models = r.json().get("data", [])
    except ValueError:
        print(f"{provider} returned a non-JSON body: {r.text[:200]}")
        return 1

    names = sorted(m.get("id", "") for m in models if m.get("id"))
    if filter_text:
        names = [n for n in names if filter_text.lower() in n.lower()]

    # OpenRouter serves /models without authentication, so a 200 here says
    # nothing about the key. Only providers that actually reject an anonymous
    # request prove anything, and even then only that the key was accepted for
    # a listing - `admin.py test <model>` is the real check.
    PUBLIC_MODEL_LISTS = {"openrouter"}
    verdict = ("key not checked - this provider lists models without auth"
               if provider in PUBLIC_MODEL_LISTS
               else f"key {encryption.mask(entry.key)} was accepted")

    print(f"{provider}: {verdict}, {len(names)} model(s)"
          + (f" matching '{filter_text}'" if filter_text else "") + "\n")
    for name in names:
        print(f"  {name}")

    print("\nSet one with:  python admin.py set-model <setting> <model string>")
    print("Current model settings:")
    for row in await db.fetch(
            "SELECT key, value FROM settings WHERE key LIKE 'model_%' ORDER BY key"):
        print(f"  {row['key']:<24} {row['value']}")
    return 0


async def set_model(setting: str, value: str) -> int:
    if not setting.startswith("model_"):
        setting = f"model_{setting}"

    existing = await db.fetchval(
        "SELECT value FROM settings WHERE key = $1", setting)
    if existing is None:
        rows = await db.fetch(
            "SELECT key FROM settings WHERE key LIKE 'model_%' ORDER BY key")
        print(f"No setting named '{setting}'. Known:")
        for r in rows:
            print(f"  {r['key']}")
        return 1

    await db.execute(
        "UPDATE settings SET value = $2, updated_at = now() WHERE key = $1",
        setting, value)

    from services import settings as settings_service

    settings_service.invalidate(setting)
    print(f"{setting}: {existing}  ->  {value}")
    return 0


async def grant_dev(email: str) -> int:
    row = await db.fetchrow(
        "SELECT id, first_name, surname, role FROM users WHERE lower(email) = lower($1)",
        email)
    if row is None:
        print(f"No user with email {email}. Sign up in the browser first.")
        return 1
    if row["role"] == "dev":
        print(f"{email} is already a dev.")
        return 0

    await db.execute("UPDATE users SET role = 'dev' WHERE id = $1", row["id"])
    print(f"{row['first_name']} {row['surname']} ({email}) is now a dev.")
    print("Log out and back in for it to take effect.")
    return 0


COMMANDS = {
    "list-keys": lambda a: list_keys(),
    "add-key": lambda a: add_key(a.provider, a.label, a.key, a.account),
    "delete-key": lambda a: delete_key(a.id),
    "test": lambda a: test(a.model),
    "test-all": lambda a: test_all(),
    "models": lambda a: list_models(a.provider, a.filter),
    "set-model": lambda a: set_model(a.setting, a.value),
    "grant-dev": lambda a: grant_dev(a.email),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Klasser admin")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-keys", help="List stored provider keys")

    add = sub.add_parser("add-key", help="Store a provider API key")
    add.add_argument("provider", choices=sorted(config.PROVIDER_URLS))
    add.add_argument("label", help="A name for this key, e.g. 'Key 1'")
    add.add_argument("--key", help="The API key. Omit to be prompted (hidden).")
    add.add_argument("--account", help="Which account it belongs to")

    rm = sub.add_parser("delete-key", help="Remove a stored key")
    rm.add_argument("id")

    t = sub.add_parser("test", help="Send one real prompt to a model")
    t.add_argument("model", choices=sorted(ai_cluster.MODEL_REGISTRY))

    sub.add_parser("test-all", help="Test every model that has a key")

    m = sub.add_parser("models", help="List the models a provider actually serves")
    m.add_argument("provider", choices=sorted(config.PROVIDER_URLS))
    m.add_argument("--filter", help="Only show names containing this text")

    sm = sub.add_parser("set-model", help="Point a model setting at a new string")
    sm.add_argument("setting", help="e.g. model_groq or just groq")
    sm.add_argument("value", help="The provider's model id")

    dev = sub.add_parser("grant-dev", help="Give an existing user the dev role")
    dev.add_argument("email")

    return parser


async def main() -> int:
    args = build_parser().parse_args()
    await db.connect()
    try:
        return await COMMANDS[args.command](args)
    finally:
        await db.disconnect()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)

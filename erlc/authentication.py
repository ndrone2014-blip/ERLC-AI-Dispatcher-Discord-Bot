"""Resolve which ER:LC credentials a guild uses (encrypted DB secret first, .env fallback)."""
from __future__ import annotations

from typing import Optional

from security.secrets import SecretStore

SERVER_KEY_NAME = "erlc_server_key"
GLOBAL_KEY_NAME = "erlc_global_key"


async def resolve_server_key(secrets: SecretStore, guild_id: int, env_key: str, env_guild_id: int = 0) -> Optional[str]:
    key = await secrets.get(guild_id, SERVER_KEY_NAME)
    if key:
        return key
    # The .env key is only applied to the single configured development guild (or any
    # guild if no DEV_GUILD_ID is set) so one key is never silently shared across servers.
    if env_key and (env_guild_id == 0 or env_guild_id == guild_id):
        return env_key
    return None


async def resolve_global_key(secrets: SecretStore, guild_id: int, env_key: str) -> str:
    return (await secrets.get(guild_id, GLOBAL_KEY_NAME)) or env_key or ""


def looks_like_server_key(value: str) -> bool:
    v = (value or "").strip()
    return 10 <= len(v) <= 200 and " " not in v

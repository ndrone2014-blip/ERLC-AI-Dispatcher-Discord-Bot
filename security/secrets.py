"""Encrypted credential storage (Fernet / AES-128-CBC + HMAC)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from core.logging_setup import register_secret
from database.connection import Database

log = logging.getLogger(__name__)


class SecretStore:
    def __init__(self, db: Database, secret_key: str, key_file: Path):
        self.db = db
        self._fernet = Fernet(self._load_key(secret_key, key_file))
        self._cache: dict[tuple[int, str], Optional[str]] = {}  # decrypted values; this process is the only writer

    @staticmethod
    def _load_key(secret_key: str, key_file: Path) -> bytes:
        if secret_key:
            return secret_key.encode()
        if key_file.exists():
            return key_file.read_bytes().strip()
        key = Fernet.generate_key()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(key)
        log.warning(
            "No SECRET_KEY in .env - generated %s. Back this file up: without it stored credentials cannot be decrypted.",
            key_file,
        )
        return key

    async def set(self, guild_id: int, name: str, value: str, actor_id: Optional[int] = None) -> None:
        token = self._fernet.encrypt(value.encode()).decode()
        register_secret(value)
        self._cache[(guild_id, name)] = value
        await self.db.execute(
            "INSERT INTO secrets(guild_id, name, ciphertext, updated_by, updated_at) VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(guild_id, name) DO UPDATE SET ciphertext = excluded.ciphertext, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (guild_id, name, token, actor_id),
        )

    async def get(self, guild_id: int, name: str) -> Optional[str]:
        if (guild_id, name) in self._cache:
            return self._cache[(guild_id, name)]
        row = await self.db.fetchone("SELECT ciphertext FROM secrets WHERE guild_id = ? AND name = ?", (guild_id, name))
        if row is None:
            self._cache[(guild_id, name)] = None
            return None
        try:
            value = self._fernet.decrypt(row["ciphertext"].encode()).decode()
        except InvalidToken:
            log.error("Stored secret %s for guild %s could not be decrypted (SECRET_KEY changed?)", name, guild_id)
            return None
        register_secret(value)
        self._cache[(guild_id, name)] = value
        return value

    async def delete(self, guild_id: int, name: str) -> None:
        self._cache.pop((guild_id, name), None)
        await self.db.execute("DELETE FROM secrets WHERE guild_id = ? AND name = ?", (guild_id, name))
        self._cache.pop((guild_id, name), None)

    async def delete_prefix(self, guild_id: int, prefix: str) -> int:
        rows = await self.db.fetchall("SELECT name FROM secrets WHERE guild_id = ? AND name LIKE ?", (guild_id, prefix + "%"))
        for r in rows:
            await self.delete(guild_id, r["name"])
        return len(rows)

    async def exists(self, guild_id: int, name: str) -> bool:
        return await self.db.fetchone("SELECT 1 FROM secrets WHERE guild_id = ? AND name = ?", (guild_id, name)) is not None


def mask(value: Optional[str]) -> str:
    if not value:
        return "not set"
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}••••{value[-3:]}"

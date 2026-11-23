"""Async SQLite access layer with a simple, ordered migration runner."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class Database:
    """Thin wrapper around a single aiosqlite connection.

    SQLite serialises writes anyway; a single connection plus an asyncio lock for
    multi-statement transactions keeps things simple and safe.
    """

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._tx_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._conn.commit()
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ migrations
    async def migrate(self) -> list[str]:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
        )
        await self.conn.commit()
        applied = {r["name"] for r in await self.fetchall("SELECT name FROM schema_migrations")}
        ran: list[str] = []
        for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if file.name in applied:
                continue
            log.info("Applying database migration %s", file.name)
            sql = file.read_text(encoding="utf-8")
            await self.conn.executescript(sql)
            await self.conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (file.name,))
            await self.conn.commit()
            ran.append(file.name)
        return ran

    # ------------------------------------------------------------------ helpers
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Execute a single statement and commit. Returns lastrowid."""
        if self._tx_lock.locked() and asyncio.current_task() is getattr(self, "_tx_owner", None):
            cur = await self.conn.execute(sql, tuple(params))
            return cur.lastrowid
        async with self._tx_lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur.lastrowid

    async def execute_rowcount(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._tx_lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur.rowcount

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def fetchval(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        return row[0] if row is not None and row[0] is not None else default

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run several statements atomically: `async with db.transaction() as c: await c.execute(...)`."""
        async with self._tx_lock:
            self._tx_owner = asyncio.current_task()
            try:
                await self.conn.execute("BEGIN")
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
            finally:
                self._tx_owner = None

    # ------------------------------------------------------------------ counters
    async def next_number(self, guild_id: int, name: str) -> int:
        async with self._tx_lock:
            await self.conn.execute(
                "INSERT INTO counters(guild_id, name, value) VALUES (?, ?, 0) ON CONFLICT(guild_id, name) DO NOTHING",
                (guild_id, name),
            )
            await self.conn.execute(
                "UPDATE counters SET value = value + 1 WHERE guild_id = ? AND name = ?", (guild_id, name)
            )
            cur = await self.conn.execute(
                "SELECT value FROM counters WHERE guild_id = ? AND name = ?", (guild_id, name)
            )
            row = await cur.fetchone()
            await self.conn.commit()
            return int(row[0])

    # ------------------------------------------------------------------ guild config
    async def get_config(self, guild_id: int, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM guild_config WHERE guild_id = ? AND key = ?", (guild_id, key))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    async def set_config(self, guild_id: int, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO guild_config(guild_id, key, value, updated_at) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (guild_id, key, json.dumps(value)),
        )

    async def get_all_config(self, guild_id: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for row in await self.fetchall("SELECT key, value FROM guild_config WHERE guild_id = ?", (guild_id,)):
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
        return out

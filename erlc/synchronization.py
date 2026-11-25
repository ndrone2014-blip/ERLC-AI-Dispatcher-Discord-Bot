"""Per-guild ER:LC polling loop with backoff, rate-limit handling and status tracking.

The documented API has no push feed for 911 calls besides the (payload-undocumented)
event webhook, so the authoritative path is polling GET /v2/server?EmergencyCalls=true.
A verified webhook simply wakes the loop for an immediate poll.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

import aiohttp

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import iso_now
from security.secrets import SecretStore

from .authentication import resolve_global_key, resolve_server_key
from .client import ERLCClient, ERLCError
from .models import ServerSnapshot

log = logging.getLogger(__name__)


class GuildSync:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.task: Optional[asyncio.Task] = None
        self.wake = asyncio.Event()
        self.client: Optional[ERLCClient] = None
        self.client_key: Optional[str] = None
        self.last_snapshot: Optional[ServerSnapshot] = None
        self.status: str = "starting"      # starting | connected | error | not_configured | paused | disabled | rate_limited
        self.detail: str = ""
        self.recent_calls: list[dict] = []


class SyncManager:
    def __init__(self, db: Database, bus: EventBus, secrets: SecretStore, session_factory: Callable[[], aiohttp.ClientSession],
                 intake_factory, *, base_url: str, env_server_key: str, env_global_key: str, env_guild_id: int,
                 poll_seconds: int):
        self.db = db
        self.bus = bus
        self.secrets = secrets
        self.session_factory = session_factory
        self.intake_factory = intake_factory  # -> CallIntake
        self.base_url = base_url
        self.env_server_key = env_server_key
        self.env_global_key = env_global_key
        self.env_guild_id = env_guild_id
        self.poll_seconds = poll_seconds
        self.guilds: dict[int, GuildSync] = {}
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle
    def start_guild(self, guild_id: int) -> None:
        gs = self.guilds.get(guild_id)
        if gs and gs.task and not gs.task.done():
            return
        gs = gs or GuildSync(guild_id)
        self.guilds[guild_id] = gs
        gs.task = asyncio.create_task(self._loop(gs), name=f"erlc-sync-{guild_id}")

    async def stop(self) -> None:
        self._stopping = True
        for gs in self.guilds.values():
            if gs.task:
                gs.task.cancel()
        await asyncio.gather(*(gs.task for gs in self.guilds.values() if gs.task), return_exceptions=True)

    def trigger(self, guild_id: int) -> None:
        gs = self.guilds.get(guild_id)
        if gs:
            gs.wake.set()

    def state(self, guild_id: int) -> GuildSync:
        return self.guilds.get(guild_id) or GuildSync(guild_id)

    async def reset_client(self, guild_id: int) -> None:
        gs = self.guilds.get(guild_id)
        if gs:
            gs.client = None
            gs.client_key = None
            gs.wake.set()

    # ------------------------------------------------------------------ helpers
    async def _client_for(self, gs: GuildSync) -> Optional[ERLCClient]:
        key = await resolve_server_key(self.secrets, gs.guild_id, self.env_server_key, self.env_guild_id)
        if not key:
            return None
        if gs.client is None or gs.client_key != key:
            gk = await resolve_global_key(self.secrets, gs.guild_id, self.env_global_key)
            gs.client = ERLCClient(key, self.session_factory(), base_url=self.base_url, global_key=gk)
            gs.client_key = key
        return gs.client

    async def _set_status(self, gs: GuildSync, status: str, detail: str = "") -> None:
        changed = (status, detail) != (gs.status, gs.detail)
        gs.status, gs.detail = status, detail
        if changed:
            await self.bus.publish(ev.ERLC_STATUS, gs.guild_id, status=status, detail=detail)

    async def _ensure_state_row(self, guild_id: int) -> None:
        await self.db.execute("INSERT OR IGNORE INTO api_sync_state(guild_id) VALUES (?)", (guild_id,))

    async def _sleep(self, gs: GuildSync, seconds: float) -> None:
        gs.wake.clear()
        try:
            await asyncio.wait_for(gs.wake.wait(), timeout=max(0.5, seconds))
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------ test
    async def test_connection(self, guild_id: int, key: Optional[str] = None) -> tuple[bool, str, Optional[ServerSnapshot]]:
        if key is None:
            key = await resolve_server_key(self.secrets, guild_id, self.env_server_key, self.env_guild_id)
        if not key:
            return False, "No ER:LC server key configured.", None
        gk = await resolve_global_key(self.secrets, guild_id, self.env_global_key)
        client = ERLCClient(key, self.session_factory(), base_url=self.base_url, global_key=gk)
        try:
            snap = await client.get_server(players=True, emergency_calls=True)
        except ERLCError as e:
            return False, e.friendly(), None
        calls = "EmergencyCalls field returned" if snap.emergency_calls_present else "EmergencyCalls field NOT returned"
        return True, (f"Connected to '{snap.name}' — {snap.current_players}/{snap.max_players} players, "
                      f"{len(snap.emergency_calls)} active 911 call(s), {calls}. Latency {client.last_latency_ms} ms."), snap

    # ------------------------------------------------------------------ main loop
    async def _loop(self, gs: GuildSync) -> None:
        await self._ensure_state_row(gs.guild_id)
        failures = 0
        while not self._stopping:
            try:
                enabled = await self.db.get_config(gs.guild_id, "erlc_monitoring_enabled", True)
                if not enabled:
                    await self._set_status(gs, "disabled", "Monitoring disabled by an administrator")
                    await self._sleep(gs, 30)
                    continue
                client = await self._client_for(gs)
                if client is None:
                    await self._set_status(gs, "not_configured", "No ER:LC server key configured")
                    await self._sleep(gs, 30)
                    continue
                state = await self.db.fetchone("SELECT last_success_at FROM api_sync_state WHERE guild_id = ?", (gs.guild_id,))
                bootstrap = state is None or state["last_success_at"] is None
                await self.db.execute("UPDATE api_sync_state SET last_attempt_at = ? WHERE guild_id = ?", (iso_now(), gs.guild_id))
                snapshot = await client.get_server(players=True, emergency_calls=True)
                gs.last_snapshot = snapshot
                await self.db.execute(
                    "UPDATE api_sync_state SET connected = 1, last_success_at = ?, consecutive_failures = 0, server_name = ?, "
                    "current_players = ?, max_players = ?, join_key = ?, rate_limit_remaining = ?, rate_limit_reset = ? WHERE guild_id = ?",
                    (iso_now(), snapshot.name, snapshot.current_players, snapshot.max_players, snapshot.join_key,
                     client.rate.remaining, int(client.rate.reset) if client.rate.reset else None, gs.guild_id))
                failures = 0
                detail = "" if snapshot.emergency_calls_present else "API response did not include EmergencyCalls"
                await self._set_status(gs, "connected", detail)
                stale = int((await self.db.get_config(gs.guild_id, "dispatch_settings", {}) or {}).get("stale_call_s", 900))
                result = await self.intake_factory().process_snapshot(gs.guild_id, snapshot, bootstrap=bootstrap, stale_after_s=stale)
                gs.recent_calls = [
                    {"number": c.call_number, "started_at": c.started_at, "team": c.team,
                     "location": c.position_descriptor, "description": c.description}
                    for c in snapshot.emergency_calls[-5:]
                ]
                if result.new_incidents or result.updated_incidents or result.ended_calls:
                    log.info("Guild %s: %s new, %s updated, %s ended in-game calls", gs.guild_id, len(result.new_incidents),
                             len(result.updated_incidents), result.ended_calls)
                await self.bus.publish(ev.ERLC_STATUS, gs.guild_id, status="connected", poll=True)
                await self._sleep(gs, self._interval(client))
            except asyncio.CancelledError:
                raise
            except ERLCError as e:
                failures += 1
                await self._record_error(gs, e, failures)
                if e.rate_limited:
                    await self._set_status(gs, "rate_limited", f"Rate limited, retrying in {int(e.retry_after or 5)}s")
                    await self._sleep(gs, (e.retry_after or 5) + 0.5)
                elif e.fatal:
                    await self._set_status(gs, "error", e.friendly() + " — set a new key with /settings erlc-key (or the ER:LC panel)")
                    await self._sleep(gs, 300)  # do not hammer the API with a bad key; config change wakes us
                else:
                    await self._set_status(gs, "error", e.friendly())
                    await self._sleep(gs, min(120, self.poll_seconds * (2 ** min(failures, 5))))
            except Exception as e:  # pragma: no cover - defensive
                failures += 1
                log.exception("Unexpected ER:LC sync error for guild %s", gs.guild_id)
                await self._set_status(gs, "error", f"Internal error: {e.__class__.__name__}")
                await self._sleep(gs, min(120, self.poll_seconds * (2 ** min(failures, 5))))

    def _interval(self, client: ERLCClient) -> float:
        interval = float(self.poll_seconds)
        r = client.rate
        if r.remaining is not None and r.reset:
            left = max(1.0, r.reset - time.time())
            if r.remaining <= 1:
                interval = max(interval, left + 0.5)
            elif r.remaining < 3:
                interval = max(interval, left / max(1, r.remaining))
        return interval

    async def _record_error(self, gs: GuildSync, e: ERLCError, failures: int) -> None:
        log.warning("ER:LC sync failed for guild %s (%s consecutive): %s", gs.guild_id, failures, e.friendly())
        await self.db.execute(
            "UPDATE api_sync_state SET connected = 0, last_error_at = ?, last_error = ?, consecutive_failures = ? WHERE guild_id = ?",
            (iso_now(), e.friendly()[:300], failures, gs.guild_id))

    # ------------------------------------------------------------------ webhook bridge
    async def resolve_webhook_token(self, token: str) -> Optional[int]:
        if not token:
            return None
        rows = await self.db.fetchall("SELECT guild_id, value FROM guild_config WHERE key = 'erlc_webhook_token'")
        import json
        for r in rows:
            try:
                if json.loads(r["value"]) == token:
                    return int(r["guild_id"])
            except ValueError:
                continue
        return None

    async def on_webhook(self, guild_id: int, raw: bytes) -> None:
        await self.db.execute("INSERT OR IGNORE INTO api_sync_state(guild_id) VALUES (?)", (guild_id,))
        await self.db.execute("UPDATE api_sync_state SET webhook_last_at = ?, webhook_last_status = 'verified' WHERE guild_id = ?",
                              (iso_now(), guild_id))
        log.info("Verified ER:LC webhook for guild %s (%d bytes) - triggering immediate poll", guild_id, len(raw))
        self.trigger(guild_id)

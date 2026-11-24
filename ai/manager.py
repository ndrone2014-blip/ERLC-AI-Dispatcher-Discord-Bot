"""AI provider manager: ordering, fallback, cooldowns, quotas and usage tracking.

Rules:
  * Providers are tried in the guild's configured order (primary, secondary, ...).
  * A provider that fails with quota/auth/outage errors is put on a cooldown that
    is persisted in the database, so it is never hammered with retries.
  * If every provider is unavailable an AIUnavailable error is raised and callers
    fall back to deterministic templates/rules - dispatch keeps working.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections import defaultdict, deque
from typing import Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import iso_now

from .providers.base import AIProvider, AIResponse, ProviderError

log = logging.getLogger(__name__)

COOLDOWNS = {"quota": 60.0, "auth": 3600.0, "unavailable": 60.0, "timeout": 20.0, "bad_response": 0.0, "error": 15.0}


class AIUnavailable(Exception):
    pass


class AIManager:
    def __init__(self, db: Database, bus: EventBus, providers: list[AIProvider], *,
                 daily_limits: Optional[dict[str, int]] = None, rpm_limits: Optional[dict[str, int]] = None):
        self.db = db
        self.bus = bus
        self.providers: dict[str, AIProvider] = {p.name: p for p in providers}
        self.daily_limits = daily_limits or {}
        self.rpm_limits = rpm_limits or {}
        self._recent: dict[str, deque] = defaultdict(deque)

    # ------------------------------------------------------------------ configuration
    async def ensure_guild(self, guild_id: int) -> None:
        for i, name in enumerate(self.providers):
            await self.db.execute(
                "INSERT OR IGNORE INTO ai_providers(guild_id, provider, enabled, model, priority_order) VALUES (?, ?, 1, NULL, ?)",
                (guild_id, name, (i + 1) * 10),
            )

    async def guild_providers(self, guild_id: int) -> list[dict]:
        await self.ensure_guild(guild_id)
        rows = await self.db.fetchall(
            "SELECT * FROM ai_providers WHERE guild_id = ? ORDER BY priority_order, provider", (guild_id,))
        out = []
        for r in rows:
            p = self.providers.get(r["provider"])
            if p is None:
                continue
            d = dict(r)
            d["label"] = p.label
            d["configured"] = p.configured
            d["effective_model"] = r["model"] or p.default_model
            d["cooling_down"] = bool(r["cooldown_until"] and r["cooldown_until"] > time.time())
            d["requests_today"] = await self.requests_today(r["provider"])
            d["daily_limit"] = self.daily_limits.get(r["provider"])
            out.append(d)
        return out

    async def set_order(self, guild_id: int, primary: str, secondary: Optional[str] = None) -> None:
        await self.ensure_guild(guild_id)
        names = list(self.providers)
        order = [primary] + ([secondary] if secondary and secondary != primary else [])
        order += [n for n in names if n not in order]
        for i, name in enumerate(order):
            await self.db.execute(
                "UPDATE ai_providers SET priority_order = ? WHERE guild_id = ? AND provider = ?", ((i + 1) * 10, guild_id, name))
        await self.bus.publish(ev.AI_STATUS, guild_id)

    async def set_enabled(self, guild_id: int, provider: str, enabled: bool) -> None:
        await self.db.execute("UPDATE ai_providers SET enabled = ? WHERE guild_id = ? AND provider = ?",
                              (1 if enabled else 0, guild_id, provider))
        await self.bus.publish(ev.AI_STATUS, guild_id)

    async def set_model(self, guild_id: int, provider: str, model: Optional[str]) -> None:
        await self.db.execute("UPDATE ai_providers SET model = ? WHERE guild_id = ? AND provider = ?", (model, guild_id, provider))
        await self.bus.publish(ev.AI_STATUS, guild_id)

    async def clear_cooldown(self, guild_id: int, provider: str) -> None:
        await self.db.execute("UPDATE ai_providers SET cooldown_until = NULL WHERE guild_id = ? AND provider = ?", (guild_id, provider))

    async def primary_name(self, guild_id: int) -> Optional[str]:
        for p in await self.guild_providers(guild_id):
            if p["enabled"] and p["configured"]:
                return p["provider"]
        return None

    # ------------------------------------------------------------------ quotas
    async def requests_today(self, provider: str) -> int:
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        return int(await self.db.fetchval(
            "SELECT COUNT(*) FROM ai_usage WHERE provider = ? AND at >= ?", (provider, day + "T00:00:00"), 0))

    def _rpm_ok(self, provider: str) -> bool:
        limit = self.rpm_limits.get(provider)
        if not limit:
            return True
        q = self._recent[provider]
        now = time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        return len(q) < limit

    async def quota_warnings(self, guild_id: int) -> list[str]:
        warnings = []
        for p in await self.guild_providers(guild_id):
            limit = p.get("daily_limit")
            if limit and p["requests_today"] >= limit * 0.8:
                warnings.append(f"{p['label']}: {p['requests_today']}/{limit} requests used today (local soft limit).")
            if p["cooling_down"]:
                left = int(p["cooldown_until"] - time.time())
                warnings.append(f"{p['label']}: paused for {left}s after error: {(p.get('last_error') or '')[:120]}")
        return warnings

    # ------------------------------------------------------------------ generation
    async def generate(self, guild_id: Optional[int], *, purpose: str, system: str, prompt: str, json_mode: bool = False,
                       max_tokens: int = 600, temperature: float = 0.3, only: Optional[str] = None) -> AIResponse:
        candidates: list[tuple[AIProvider, Optional[str], dict]] = []
        if guild_id is not None:
            for row in await self.guild_providers(guild_id):
                if only and row["provider"] != only:
                    continue
                if not row["enabled"] or not row["configured"]:
                    continue
                candidates.append((self.providers[row["provider"]], row["model"], row))
        else:
            candidates = [(p, None, {"cooldown_until": None}) for p in self.providers.values() if p.configured]
        errors: list[str] = []
        for provider, model, row in candidates:
            if row.get("cooldown_until") and row["cooldown_until"] > time.time() and not only:
                errors.append(f"{provider.name}: cooling down")
                continue
            limit = self.daily_limits.get(provider.name)
            if limit and await self.requests_today(provider.name) >= limit and not only:
                errors.append(f"{provider.name}: local daily limit reached")
                continue
            if not self._rpm_ok(provider.name) and not only:
                errors.append(f"{provider.name}: per-minute limit reached")
                continue
            self._recent[provider.name].append(time.monotonic())
            try:
                try:
                    resp = await provider.generate(system=system, prompt=prompt, model=model, json_mode=json_mode,
                                                   max_tokens=max_tokens, temperature=temperature)
                except ProviderError as e:
                    if e.kind != "model":
                        raise
                    # The configured model was retired or renamed: switch to an available one and retry once.
                    replacement = await self._replacement_model(provider, model or provider.default_model)
                    if not replacement:
                        raise
                    log.warning("AI model %s on %s unavailable (%s); switching to %s", model or provider.default_model,
                                provider.name, e.message[:120], replacement)
                    if guild_id is not None:
                        await self.set_model(guild_id, provider.name, replacement)
                    provider.default_model = replacement
                    model = replacement
                    resp = await provider.generate(system=system, prompt=prompt, model=model, json_mode=json_mode,
                                                   max_tokens=max_tokens, temperature=temperature)
            except ProviderError as e:
                await self._record_failure(guild_id, provider.name, model or provider.default_model, purpose, e)
                errors.append(f"{provider.name}: {e}")
                continue
            await self._record_success(guild_id, resp, purpose)
            return resp
        raise AIUnavailable("; ".join(errors) or "No AI provider is configured")

    @staticmethod
    async def _replacement_model(provider: AIProvider, current: str) -> Optional[str]:
        try:
            models = [m for m in await provider.list_models() if m != current]
        except ProviderError:
            return None
        if not models:
            return None

        def score(name: str) -> tuple:
            import re
            n = name.lower()
            version = tuple(int(x) for x in re.findall(r"\d+", n)[:2]) or (0,)
            stable = "preview" not in n and "exp" not in n
            return ("flash" in n and "lite" not in n, stable, "latest" not in n, version)

        return max(models, key=score)

    async def test_provider(self, guild_id: int, provider: str) -> tuple[bool, str]:
        try:
            resp = await self.generate(guild_id, purpose="test", system="You are a connectivity test.",
                                       prompt="Reply with exactly: DISPATCH ONLINE", max_tokens=20, only=provider)
            await self.clear_cooldown(guild_id, provider)
            return True, f"{resp.provider} / {resp.model} replied in {resp.latency_ms} ms: {resp.text.strip()[:60]}"
        except AIUnavailable as e:
            return False, str(e)

    async def _record_success(self, guild_id: Optional[int], resp: AIResponse, purpose: str) -> None:
        await self.db.execute(
            "INSERT INTO ai_usage(guild_id, provider, model, purpose, ok, latency_ms, tokens_in, tokens_out) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (guild_id, resp.provider, resp.model, purpose, resp.latency_ms, resp.tokens_in, resp.tokens_out),
        )
        if guild_id is not None:
            await self.db.execute(
                "UPDATE ai_providers SET last_ok_at = ?, cooldown_until = NULL WHERE guild_id = ? AND provider = ?",
                (iso_now(), guild_id, resp.provider))

    async def _record_failure(self, guild_id: Optional[int], provider: str, model: str, purpose: str, err: ProviderError) -> None:
        log.warning("AI provider %s failed (%s): %s", provider, purpose, err)
        await self.db.execute(
            "INSERT INTO ai_usage(guild_id, provider, model, purpose, ok, error) VALUES (?, ?, ?, ?, 0, ?)",
            (guild_id, provider, model, purpose, str(err)[:300]),
        )
        cooldown = err.retry_after if err.retry_after is not None else COOLDOWNS.get(err.kind, 15.0)
        until = time.time() + cooldown if cooldown > 0 else None
        if guild_id is not None:
            await self.db.execute(
                "UPDATE ai_providers SET last_error_at = ?, last_error = ?, cooldown_until = COALESCE(?, cooldown_until) "
                "WHERE guild_id = ? AND provider = ?",
                (iso_now(), str(err)[:300], until, guild_id, provider))
            await self.bus.publish(ev.AI_STATUS, guild_id)

    async def usage_summary(self, guild_id: int) -> dict:
        rows = await self.db.fetchall(
            "SELECT provider, SUM(ok) AS ok, COUNT(*) - SUM(ok) AS failed, AVG(CASE WHEN ok THEN latency_ms END) AS avg_ms, "
            "SUM(COALESCE(tokens_in,0)) AS tin, SUM(COALESCE(tokens_out,0)) AS tout "
            "FROM ai_usage WHERE (guild_id = ? OR guild_id IS NULL) AND at >= ? GROUP BY provider",
            (guild_id, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d") + "T00:00:00"),
        )
        return {r["provider"]: dict(r) for r in rows}

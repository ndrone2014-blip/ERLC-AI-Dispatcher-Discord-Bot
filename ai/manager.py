"""AI provider manager: ordering, fallback, cooldowns, quotas, caching and usage tracking.

Rules:
  * Each guild picks a mode: "manual" (providers tried in the configured order) or "auto"
    (the healthiest free provider first). An optional preferred provider always goes first.
  * Automatic failover (on by default) tries the next provider when one fails; with failover off only
    the first usable provider is tried.
  * API keys can come from .env (bot owner) or be set per guild by its administrators (encrypted).
  * A provider that fails with quota/auth/outage errors is put on a cooldown that
    is persisted in the database, so it is never hammered with retries.
  * If every provider is unavailable an AIUnavailable error is raised and callers
    fall back to deterministic templates/rules - dispatch keeps working.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
from collections import OrderedDict, defaultdict, deque
from typing import Awaitable, Callable, Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import iso_now

from .providers.base import AIProvider, AIResponse, ProviderError
from .providers.catalog import SPEC

Credentials = Callable[[int, str], Awaitable[dict]]

log = logging.getLogger(__name__)

# Per-purpose time limits (seconds). Voice and dispatch must never wait long for an AI that is slow or down.
PURPOSE_TIMEOUTS = {"intent": 4.0, "question": 6.0, "assessment": 6.0, "announcement": 3.0, "report_summary": 20.0, "test": 15.0}

COOLDOWNS = {"quota": 60.0, "auth": 3600.0, "unavailable": 60.0, "timeout": 20.0, "bad_response": 0.0, "error": 15.0}

# Identical requests within this time reuse the previous answer (no extra API call, instant reply).
CACHE_TTL = {"intent": 300.0, "announcement": 600.0, "assessment": 45.0}
COST_RANK = {"free": 0, "local": 1, "trial": 2, "credits": 3, "paid": 4}
DEFAULT_AI_SETTINGS = {"mode": "manual", "failover": True, "preferred": None}


class AIUnavailable(Exception):
    pass


class AIManager:
    def __init__(self, db: Database, bus: EventBus, providers: list[AIProvider], *,
                 daily_limits: Optional[dict[str, int]] = None, rpm_limits: Optional[dict[str, int]] = None,
                 credentials: Optional[Credentials] = None):
        self.db = db
        self.bus = bus
        self.providers: dict[str, AIProvider] = {p.name: p for p in providers}
        self.daily_limits = daily_limits or {}
        self.rpm_limits = rpm_limits or {}
        self.credentials = credentials
        self._recent: dict[str, deque] = defaultdict(deque)
        self._today: dict[str, tuple[str, int]] = {}            # provider -> (day, requests) in-memory counter
        self._cache: "OrderedDict[str, tuple[float, AIResponse]]" = OrderedDict()
        self._active: dict[int, dict] = {}                       # guild -> last provider that answered
        self._failure_notified: dict[tuple[int, str], float] = {}
        self.cache_hits = 0
        self._known_guilds: dict[int, set] = {}

    # ------------------------------------------------------------------ guild AI settings
    async def ai_settings(self, guild_id: int) -> dict:
        s = await self.db.get_config(guild_id, "ai_settings", {}) or {}
        return {**DEFAULT_AI_SETTINGS, **s}

    async def update_ai_settings(self, guild_id: int, **changes) -> dict:
        s = await self.ai_settings(guild_id)
        s.update({k: v for k, v in changes.items() if k in DEFAULT_AI_SETTINGS})
        await self.db.set_config(guild_id, "ai_settings", s)
        await self.bus.publish(ev.AI_STATUS, guild_id)
        return s

    def active_provider(self, guild_id: int) -> Optional[dict]:
        return self._active.get(guild_id)

    async def _creds(self, guild_id: Optional[int], provider: str) -> dict:
        if guild_id is None or self.credentials is None:
            return {}
        try:
            return {k: v for k, v in (await self.credentials(guild_id, provider) or {}).items() if v}
        except Exception:
            log.exception("Could not load stored credentials for %s", provider)
            return {}

    # ------------------------------------------------------------------ configuration
    async def ensure_guild(self, guild_id: int) -> None:
        """Add rows for providers this guild has not seen yet, after the existing ones (keeps the admin's order)."""
        known = self._known_guilds.get(guild_id)
        if known is not None and known >= set(self.providers):
            return
        rows = await self.db.fetchall("SELECT provider, priority_order FROM ai_providers WHERE guild_id = ?", (guild_id,))
        have = {r["provider"] for r in rows}
        top = max([r["priority_order"] for r in rows] or [0])
        for name in self.providers:
            if name in have:
                continue
            top += 10
            await self.db.execute(
                "INSERT OR IGNORE INTO ai_providers(guild_id, provider, enabled, model, priority_order) VALUES (?, ?, 1, NULL, ?)",
                (guild_id, name, top))
        self._known_guilds[guild_id] = set(self.providers)

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
            creds = await self._creds(guild_id, r["provider"])
            d["label"] = p.label
            d["server_key"] = bool(creds.get("api_key") or creds.get("base_url"))
            d["configured"] = p.ready_with(**creds) if creds else p.configured
            d["key_source"] = "server" if d["server_key"] and d["configured"] else ("env" if p.configured else None)
            d["effective_model"] = r["model"] or p.default_model
            d["cooling_down"] = bool(r["cooldown_until"] and r["cooldown_until"] > time.time())
            d["requests_today"] = await self.requests_today(r["provider"])
            d["daily_limit"] = self.daily_limits.get(r["provider"])
            sp = SPEC.get(r["provider"])
            d["cost"] = sp.cost if sp else "free"
            d["signup_url"] = sp.signup_url if sp else ""
            out.append(d)
        return out

    async def ordered_providers(self, guild_id: int) -> list[dict]:
        """Providers in the order they will be tried for this guild (mode + preferred applied)."""
        rows = await self.guild_providers(guild_id)
        settings = await self.ai_settings(guild_id)
        if settings["mode"] == "auto":
            usage = await self.usage_summary(guild_id)

            def health(p: dict) -> tuple:
                u = usage.get(p["provider"], {})
                failed = int(u.get("failed") or 0)
                ok = int(u.get("ok") or 0)
                return (not (p["enabled"] and p["configured"]), p["cooling_down"], COST_RANK.get(p["cost"], 5),
                        failed > ok and failed > 2, float(u.get("avg_ms") or 1500) // 500)

            rows.sort(key=health)
        pref = settings.get("preferred")
        if pref:
            rows.sort(key=lambda p: p["provider"] != pref)
        return rows

    async def set_order(self, guild_id: int, primary: str, secondary: Optional[str] = None) -> None:
        await self.ensure_guild(guild_id)
        current = [r["provider"] for r in await self.guild_providers(guild_id)] or list(self.providers)
        order = [primary] + ([secondary] if secondary and secondary != primary else [])
        order += [n for n in current if n not in order]
        order += [n for n in self.providers if n not in order]
        settings = await self.ai_settings(guild_id)
        if settings["mode"] != "manual" or settings.get("preferred"):
            await self.update_ai_settings(guild_id, mode="manual", preferred=None)
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
        for p in await self.ordered_providers(guild_id):
            if p["enabled"] and p["configured"]:
                return p["provider"]
        return None

    # ------------------------------------------------------------------ quotas
    async def requests_today(self, provider: str) -> int:
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        cached = self._today.get(provider)
        if cached and cached[0] == day:
            return cached[1]
        n = int(await self.db.fetchval(
            "SELECT COUNT(*) FROM ai_usage WHERE provider = ? AND at >= ?", (provider, day + "T00:00:00"), 0))
        self._today[provider] = (day, n)
        return n

    def _count_request(self, provider: str) -> None:
        day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        cur = self._today.get(provider)
        self._today[provider] = (day, (cur[1] if cur and cur[0] == day else 0) + 1)

    def _rpm_ok(self, provider: str, bucket: Optional[str] = None) -> bool:
        limit = self.rpm_limits.get(provider)
        if not limit:
            return True
        q = self._recent[bucket or provider]
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
    def _cache_key(self, guild_id, purpose, system, prompt, json_mode, max_tokens, only) -> str:
        raw = f"{guild_id}|{purpose}|{only}|{json_mode}|{max_tokens}|{system}|{prompt}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def clear_cache(self) -> None:
        self._cache.clear()

    async def generate(self, guild_id: Optional[int], *, purpose: str, system: str, prompt: str, json_mode: bool = False,
                       max_tokens: int = 600, temperature: float = 0.3, only: Optional[str] = None,
                       timeout: Optional[float] = None, use_cache: bool = True) -> AIResponse:
        timeout = timeout or PURPOSE_TIMEOUTS.get(purpose, 10.0)
        ttl = CACHE_TTL.get(purpose) if use_cache and not only else None
        ckey = self._cache_key(guild_id, purpose, system, prompt, json_mode, max_tokens, only) if ttl else None
        if ckey:
            hit = self._cache.get(ckey)
            if hit and time.monotonic() - hit[0] < ttl:
                self._cache.move_to_end(ckey)
                self.cache_hits += 1
                return hit[1]
        candidates: list[tuple[AIProvider, Optional[str], dict, dict]] = []
        failover = True
        if guild_id is not None:
            failover = bool((await self.ai_settings(guild_id)).get("failover", True)) or bool(only)
            for row in await self.ordered_providers(guild_id):
                if only and row["provider"] != only:
                    continue
                if not row["enabled"] or not row["configured"]:
                    continue
                creds = await self._creds(guild_id, row["provider"])
                candidates.append((self.providers[row["provider"]], row["model"], row, creds))
        else:
            candidates = [(p, None, {"cooldown_until": None}, {}) for p in self.providers.values() if p.configured]
        errors: list[str] = []
        tried = 0
        for provider, model, row, creds in candidates:
            if tried and not failover:
                errors.append(f"{provider.name}: skipped (automatic failover is off)")
                break
            if row.get("cooldown_until") and row["cooldown_until"] > time.time() and not only:
                errors.append(f"{provider.name}: cooling down")
                continue
            limit = self.daily_limits.get(provider.name)
            if limit and await self.requests_today(provider.name) >= limit and not only:
                errors.append(f"{provider.name}: local daily limit reached")
                continue
            # A server using its own key gets its own per-minute budget (keys are rate limited separately).
            bucket = f"{provider.name}:{guild_id}" if creds else provider.name
            if not self._rpm_ok(provider.name, bucket) and not only:
                errors.append(f"{provider.name}: per-minute limit reached")
                continue
            tried += 1
            self._recent[bucket].append(time.monotonic())
            self._count_request(provider.name)
            kwargs = dict(system=system, prompt=prompt, model=model, json_mode=json_mode, max_tokens=max_tokens,
                          temperature=temperature, timeout=timeout)
            if creds:
                kwargs.update(creds)
            try:
                try:
                    resp = await provider.generate(**kwargs)
                except ProviderError as e:
                    if e.kind != "model":
                        raise
                    # The configured model was retired or renamed: switch to an available one and retry once.
                    replacement = await self._replacement_model(provider, model or provider.default_model, creds)
                    if not replacement:
                        raise
                    log.warning("AI model %s on %s unavailable (%s); switching to %s", model or provider.default_model,
                                provider.name, e.message[:120], replacement)
                    if guild_id is not None:
                        await self.set_model(guild_id, provider.name, replacement)
                    if not creds:
                        provider.default_model = replacement
                    model = replacement
                    kwargs["model"] = model
                    resp = await provider.generate(**kwargs)
            except ProviderError as e:
                await self._record_failure(guild_id, provider.name, model or provider.default_model, purpose, e)
                errors.append(f"{provider.name}: {e}")
                continue
            except Exception as e:  # e.g. an HTML error page that is not JSON: never let one provider break dispatch
                log.exception("Unexpected error from AI provider %s", provider.name)
                err = ProviderError(f"Unexpected {e.__class__.__name__}", kind="error")
                await self._record_failure(guild_id, provider.name, model or provider.default_model, purpose, err)
                errors.append(f"{provider.name}: {err}")
                continue
            await self._record_success(guild_id, resp, purpose)
            if guild_id is not None:
                self._active[guild_id] = {"provider": resp.provider, "label": provider.label, "model": resp.model,
                                          "latency_ms": resp.latency_ms, "at": time.time(), "purpose": purpose}
            if ckey:
                self._cache[ckey] = (time.monotonic(), resp)
                while len(self._cache) > 500:
                    self._cache.popitem(last=False)
            return resp
        raise AIUnavailable("; ".join(errors) or "No AI provider is configured")

    @staticmethod
    async def _replacement_model(provider: AIProvider, current: str, creds: Optional[dict] = None) -> Optional[str]:
        try:
            models = [m for m in await provider.list_models(**(creds or {})) if m != current]
        except ProviderError:
            return None
        if not models:
            return None

        def score(name: str) -> tuple:
            import re
            n = name.lower()
            version = tuple(int(x) for x in re.findall(r"\d+", n)[:2]) or (0,)
            stable = "preview" not in n and "exp" not in n
            free = n.endswith(":free") or provider.name != "openrouter"
            return (free, "flash" in n and "lite" not in n, stable, "latest" not in n, version)

        return max(models, key=score)

    async def test_provider(self, guild_id: int, provider: str) -> tuple[bool, str]:
        try:
            resp = await self.generate(guild_id, purpose="test", system="You are a connectivity test.",
                                       prompt="Reply with exactly: DISPATCH ONLINE", max_tokens=40, only=provider,
                                       use_cache=False)
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
        from core.logging_setup import redact
        log.warning("AI provider %s failed (%s): %s", provider, purpose, err)
        if guild_id is not None:
            # At most one failure notice per provider per guild every 10 minutes (no log spam during an outage).
            key = (guild_id, provider)
            if time.monotonic() - self._failure_notified.get(key, -1e9) > 600:
                self._failure_notified[key] = time.monotonic()
                await self.bus.publish("ai.failure", guild_id, provider=provider, model=model, purpose=purpose, kind=err.kind,
                                       message=redact(err.message)[:300])
        await self.db.execute(
            "INSERT INTO ai_usage(guild_id, provider, model, purpose, ok, error) VALUES (?, ?, ?, ?, 0, ?)",
            (guild_id, provider, model, purpose, redact(str(err))[:300]),
        )
        cooldown = err.retry_after if err.retry_after is not None else COOLDOWNS.get(err.kind, 15.0)
        until = time.time() + cooldown if cooldown > 0 else None
        if guild_id is not None:
            await self.db.execute(
                "UPDATE ai_providers SET last_error_at = ?, last_error = ?, cooldown_until = COALESCE(?, cooldown_until) "
                "WHERE guild_id = ? AND provider = ?",
                (iso_now(), redact(str(err))[:300], until, guild_id, provider))
            await self.bus.publish(ev.AI_STATUS, guild_id)

    async def usage_summary(self, guild_id: int) -> dict:
        rows = await self.db.fetchall(
            "SELECT provider, SUM(ok) AS ok, COUNT(*) - SUM(ok) AS failed, AVG(CASE WHEN ok THEN latency_ms END) AS avg_ms, "
            "SUM(COALESCE(tokens_in,0)) AS tin, SUM(COALESCE(tokens_out,0)) AS tout "
            "FROM ai_usage WHERE (guild_id = ? OR guild_id IS NULL) AND at >= ? GROUP BY provider",
            (guild_id, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d") + "T00:00:00"),
        )
        return {r["provider"]: dict(r) for r in rows}

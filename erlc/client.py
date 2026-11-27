"""HTTP client for the official ER:LC Private Server API (https://apidocs.erlc.gg/).

Documented facts this client relies on:
  * Base URL https://api.erlc.gg, v2 data via GET /v2/server with boolean query flags.
  * Auth header `server-key` (required); optional `Authorization` global key for public apps.
  * Rate limit headers X-RateLimit-Bucket/Limit/Remaining/Reset; HTTP 429 with Retry-After.
  * Error bodies carry a numeric `code` (see ERROR_CODES).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from .models import ServerSnapshot

log = logging.getLogger(__name__)

ERROR_CODES = {
    0: "Unknown error",
    1001: "Error communicating with Roblox / the in-game private server",
    1002: "Internal ER:LC API system error",
    2000: "No server-key provided",
    2001: "Incorrectly formatted server-key",
    2002: "Invalid or expired server-key",
    2003: "Invalid global API key",
    2004: "Server-key is banned from the API",
    2005: "Unauthenticated requests on behalf of multiple servers (a global API key is required)",
    3001: "No valid command in request body",
    3002: "Private server is offline (no players)",
    4000: "Not authorized for this action on this server",
    4001: "Rate limited or temporarily blocked",
    4002: "Command restricted",
    4003: "Message prohibited",
    9998: "Resource restricted",
    9999: "In-game module out of date (kick all players and retry)",
}

# Codes that will not fix themselves by retrying with the same key.
FATAL_CODES = {2000, 2001, 2002, 2003, 2004, 2005}


class ERLCError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES or self.status == 403 and self.code is None

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 or self.code == 4001

    def friendly(self) -> str:
        base = ERROR_CODES.get(self.code or -1)
        parts = [p for p in [f"HTTP {self.status}" if self.status else None, f"code {self.code}" if self.code is not None else None] if p]
        detail = f" ({', '.join(parts)})" if parts else ""
        return f"{base or str(self)}{detail}"


@dataclass
class RateLimitState:
    bucket: Optional[str] = None
    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset: Optional[float] = None
    blocked_until: float = 0.0


class ERLCClient:
    def __init__(
        self,
        server_key: str,
        session: aiohttp.ClientSession,
        base_url: str = "https://api.erlc.gg",
        global_key: str = "",
        timeout: float = 10.0,
    ):
        self.server_key = server_key
        self.global_key = global_key
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.rate = RateLimitState()
        self._lock = asyncio.Lock()
        self.last_latency_ms: Optional[int] = None

    def _headers(self) -> dict[str, str]:
        headers = {"server-key": self.server_key, "Accept": "application/json", "User-Agent": "ERLC-AI-Dispatch/1.0"}
        if self.global_key:
            headers["Authorization"] = self.global_key
        return headers

    def _update_rate(self, resp: aiohttp.ClientResponse) -> None:
        h = resp.headers
        try:
            if "X-RateLimit-Bucket" in h:
                self.rate.bucket = h["X-RateLimit-Bucket"]
            if "X-RateLimit-Limit" in h:
                self.rate.limit = int(h["X-RateLimit-Limit"])
            if "X-RateLimit-Remaining" in h:
                self.rate.remaining = int(h["X-RateLimit-Remaining"])
            if "X-RateLimit-Reset" in h:
                self.rate.reset = float(h["X-RateLimit-Reset"])
        except ValueError:
            pass

    async def _wait_for_rate_limit(self) -> None:
        now = time.time()
        if self.rate.blocked_until > now:
            await asyncio.sleep(self.rate.blocked_until - now)
        elif self.rate.remaining == 0 and self.rate.reset and self.rate.reset > now:
            await asyncio.sleep(min(self.rate.reset - now, 30))

    async def _request(self, method: str, path: str, *, params: Optional[dict] = None, json: Any = None) -> Any:
        async with self._lock:
            await self._wait_for_rate_limit()
            url = f"{self.base_url}{path}"
            started = time.monotonic()
            try:
                async with self.session.request(
                    method, url, params=params, json=json, headers=self._headers(), timeout=self.timeout
                ) as resp:
                    self.last_latency_ms = int((time.monotonic() - started) * 1000)
                    self._update_rate(resp)
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = None
                    if resp.status == 429:
                        retry = _retry_after(resp, body)
                        self.rate.blocked_until = time.time() + retry
                        raise ERLCError("Rate limited by ER:LC API", 429, _code(body) or 4001, retry)
                    if resp.status >= 400:
                        msg = (body.get("message") or body.get("error")) if isinstance(body, dict) else None
                        raise ERLCError(msg or f"ER:LC API returned HTTP {resp.status}", resp.status, _code(body))
                    return body
            except asyncio.TimeoutError as e:
                raise ERLCError("ER:LC API request timed out") from e
            except aiohttp.ClientError as e:
                raise ERLCError(f"Network error contacting ER:LC API: {e.__class__.__name__}") from e

    async def get_server(self, *, players: bool = True, emergency_calls: bool = True, **flags: bool) -> ServerSnapshot:
        params: dict[str, str] = {}
        if players:
            params["Players"] = "true"
        if emergency_calls:
            params["EmergencyCalls"] = "true"
        for k, v in flags.items():
            if v:
                params[k] = "true"
        data = await self._request("GET", "/v2/server", params=params)
        if not isinstance(data, dict):
            raise ERLCError("Unexpected response shape from /v2/server")
        return ServerSnapshot.parse(data)

    async def run_command(self, command: str) -> dict:
        return await self._request("POST", "/v2/server/command", json={"command": command}) or {}

    async def get_bans(self) -> dict[str, str]:
        """Documented v1 endpoint: {"RobloxId": "Username", ...}. Raises ERLCError if unavailable."""
        data = await self._request("GET", "/v1/server/bans")
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _code(body: Any) -> Optional[int]:
    if isinstance(body, dict):
        try:
            return int(body.get("code")) if body.get("code") is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _retry_after(resp: aiohttp.ClientResponse, body: Any) -> float:
    for candidate in (resp.headers.get("Retry-After"), (body or {}).get("retry_after") if isinstance(body, dict) else None):
        try:
            if candidate is not None:
                return max(1.0, float(candidate))
        except (TypeError, ValueError):
            continue
    return 5.0

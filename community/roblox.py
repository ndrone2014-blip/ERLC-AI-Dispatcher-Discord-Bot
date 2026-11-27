"""Roblox Web API client (public, documented endpoints only).

Profile-code verification uses:
  POST https://users.roblox.com/v1/usernames/users       username -> user id
  GET  https://users.roblox.com/v1/users/{id}             profile (About text, created, isBanned)
  GET  https://thumbnails.roblox.com/v1/users/avatar-headshot

Official OAuth 2.0 sign-in (optional, needs an OAuth app from create.roblox.com):
  https://apis.roblox.com/oauth/v1/authorize | /token | /userinfo
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

USERS = "https://users.roblox.com"
THUMBS = "https://thumbnails.roblox.com"
OAUTH = "https://apis.roblox.com/oauth/v1"


class RobloxError(Exception):
    pass


@dataclass
class RobloxUser:
    id: int
    name: str
    display_name: str = ""
    description: str = ""
    created: str = ""
    is_banned: bool = False

    def as_dict(self) -> dict:
        return {"id": str(self.id), "name": self.name, "display_name": self.display_name, "created": self.created,
                "is_banned": self.is_banned}


class RobloxAPI:
    def __init__(self, session_factory: Optional[Callable[[], Any]], *, client_id: str = "", client_secret: str = "",
                 timeout: float = 10.0):
        self.session_factory = session_factory
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout

    @property
    def oauth_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _json(self, method: str, url: str, **kw) -> Any:
        if self.session_factory is None:
            raise RobloxError("Roblox lookups are not available (no HTTP session).")
        import aiohttp
        try:
            async with self.session_factory().request(method, url, timeout=aiohttp.ClientTimeout(total=self.timeout), **kw) as r:
                if r.status == 429:
                    raise RobloxError("Roblox is rate limiting requests right now. Try again in a minute.")
                if r.status == 404:
                    return None
                if r.status >= 400:
                    body = (await r.text())[:200]
                    raise RobloxError(f"Roblox returned HTTP {r.status}. {body}")
                return await r.json(content_type=None)
        except RobloxError:
            raise
        except Exception as e:  # network errors, timeouts
            raise RobloxError(f"Could not reach Roblox ({type(e).__name__}).") from e

    async def user_by_username(self, username: str) -> Optional[RobloxUser]:
        data = await self._json("POST", f"{USERS}/v1/usernames/users",
                                json={"usernames": [username], "excludeBannedUsers": False})
        items = (data or {}).get("data") or []
        if not items:
            return None
        u = items[0]
        return RobloxUser(id=int(u["id"]), name=u.get("name") or username, display_name=u.get("displayName") or "")

    async def user(self, user_id: int) -> Optional[RobloxUser]:
        u = await self._json("GET", f"{USERS}/v1/users/{int(user_id)}")
        if not u:
            return None
        return RobloxUser(id=int(u["id"]), name=u.get("name", ""), display_name=u.get("displayName") or "",
                          description=u.get("description") or "", created=u.get("created") or "",
                          is_banned=bool(u.get("isBanned")))

    async def headshot(self, user_id: int) -> Optional[str]:
        try:
            data = await self._json("GET", f"{THUMBS}/v1/users/avatar-headshot",
                                    params={"userIds": str(int(user_id)), "size": "150x150", "format": "Png", "isCircular": "false"})
        except RobloxError:
            return None
        items = (data or {}).get("data") or []
        return items[0].get("imageUrl") if items and items[0].get("state") == "Completed" else None

    # ------------------------------------------------------------------ existing verification services
    async def _linked_lookup(self, url: str, headers: dict) -> Optional[int]:
        """Returns the Roblox user id another verification service has on file, or None."""
        if self.session_factory is None:
            raise RobloxError("Lookups are not available (no HTTP session).")
        import aiohttp
        try:
            async with self.session_factory().get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)) as r:
                if r.status in (401, 403):
                    raise RobloxError("The API key was rejected. Check it on the dashboard (Verification → Settings).")
                if r.status == 404:
                    return None
                if r.status == 429:
                    raise RobloxError("The service is rate limiting requests. Try again in a minute.")
                if r.status >= 400:
                    raise RobloxError(f"The service returned HTTP {r.status}.")
                data = await r.json(content_type=None)
        except RobloxError:
            raise
        except Exception as e:
            raise RobloxError(f"Could not reach the service ({type(e).__name__}).") from e
        if not isinstance(data, dict):
            return None
        for key in ("robloxID", "robloxId", "roblox_id", "robloxUserId"):
            v = data.get(key)
            if v not in (None, "", 0, "0"):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
        return None

    async def bloxlink_lookup(self, guild_id: int, discord_id: int, api_key: str) -> Optional[int]:
        # Bloxlink server ("guild") API key from blox.link → your server → API.
        return await self._linked_lookup(f"https://api.blox.link/v4/public/guilds/{int(guild_id)}/discord-to-roblox/{int(discord_id)}",
                                         {"Authorization": api_key})

    async def rover_lookup(self, guild_id: int, discord_id: int, api_key: str) -> Optional[int]:
        # RoVer server API key from rover.link → dashboard → your server → API.
        return await self._linked_lookup(f"https://registry.rover.link/api/guilds/{int(guild_id)}/discord-to-roblox/{int(discord_id)}",
                                         {"Authorization": f"Bearer {api_key}"})

    # ------------------------------------------------------------------ OAuth 2.0 (authorization code + PKCE)
    @staticmethod
    def new_pkce() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return verifier, challenge

    def authorize_url(self, redirect_uri: str, state: str, challenge: str) -> str:
        q = {"client_id": self.client_id, "redirect_uri": redirect_uri, "scope": "openid profile", "response_type": "code",
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{OAUTH}/authorize?{urllib.parse.urlencode(q)}"

    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> dict:
        data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "client_id": self.client_id,
                "client_secret": self.client_secret, "redirect_uri": redirect_uri}
        tok = await self._json("POST", f"{OAUTH}/token", data=data)
        if not tok or not tok.get("access_token"):
            raise RobloxError("Roblox did not return an access token.")
        info = await self._json("GET", f"{OAUTH}/userinfo", headers={"Authorization": f"Bearer {tok['access_token']}"})
        if not info or not info.get("sub"):
            raise RobloxError("Roblox did not return the account details.")
        return info

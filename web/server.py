"""Hive Dispatch web dashboard server (aiohttp, runs inside the bot process).

Security model
* Sign-in with Discord OAuth2 (scope `identify` only). We learn who you are; which servers you may
  manage is decided by the bot from your *real* membership and roles in each server, checked on every
  request. Nothing the browser sends can widen that.
* Session cookie: 256-bit random, HttpOnly, SameSite=Lax, Secure on HTTPS. Only its SHA-256 hash is
  stored, so a database copy cannot be used to sign in.
* Every state-changing request needs the per-session CSRF token header and a same-origin Origin.
* Strict Content-Security-Policy (scripts only from this server), no framing, rate limits per session
  and per IP, 1 MB request limit. Credentials (API keys, ER:LC key) are write-only.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from aiohttp import ClientTimeout, web

from community.common import ServiceError
from core.version import VERSION
from database.models import iso_now, parse_iso, utcnow
from security.permissions import Actor
from security.validation import RateLimiter

from .api import Api, ApiError, CsvResponse, Ctx

if TYPE_CHECKING:
    from bot.client import DispatchBot

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "pd_session"
STATE_COOKIE = "pd_oauth_state"
SESSION_DAYS = 7
DISCORD_API = "https://discord.com/api/v10"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net https://*.rbxcdn.com; "
       "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; "
       "form-action 'self'")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _json(data, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, default=str), status=status, content_type="application/json",
                        headers={"Cache-Control": "no-store"})


class DashboardServer:
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot
        self.s = bot.settings
        self.api = Api(bot)
        self.runner: Optional[web.AppRunner] = None
        self.running = False
        self.api_limiter = RateLimiter(max_calls=240, per_seconds=60)
        self.auth_limiter = RateLimiter(max_calls=12, per_seconds=60)
        self.write_limiter = RateLimiter(max_calls=60, per_seconds=60)
        self._member_cache: dict[tuple[int, int], tuple[float, object]] = {}
        self._streams: dict[int, set[asyncio.Queue]] = {}
        self._denied_logged: dict[tuple[int, int], float] = {}
        bot.bus.subscribe("*", self._on_event)

    # ------------------------------------------------------------------ lifecycle
    @property
    def base_url(self) -> str:
        return self.s.dashboard_url

    @property
    def secure(self) -> bool:
        return self.base_url.startswith("https://")

    def client_id(self) -> str:
        return self.s.discord_client_id or str(self.bot.application_id or "")

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 1024, middlewares=[self._errors, self._headers, self._proxy_guard])
        r = app.router
        r.add_get("/", self._index)
        r.add_get("/healthz", self._health)
        r.add_get("/auth/login", self._login)
        r.add_get("/auth/callback", self._callback)
        r.add_post("/auth/logout", self._logout)
        r.add_get("/verify/roblox/{token}", self._roblox_start)
        r.add_get("/auth/roblox/callback", self._roblox_callback)
        r.add_get("/api/me", self._me)
        r.add_post("/api/me/logout-all", self._logout_all)
        r.add_get("/api/me/sessions", self._my_sessions)
        r.add_get("/api/g/{gid}/events", self._events)
        r.add_route("*", "/api/g/{gid}/{path:.*}", self._api)
        r.add_static("/static/", STATIC, show_index=False)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.s.dashboard_host, self.s.dashboard_port)
        await site.start()
        self.running = True
        if not self.s.discord_client_secret:
            log.warning("Web dashboard is running at %s but DISCORD_CLIENT_SECRET is not set, so nobody can sign in yet. "
                        "See docs/DASHBOARD.md.", self.base_url)
        else:
            log.info("Web dashboard running at %s (listening on %s:%s)", self.base_url, self.s.dashboard_host, self.s.dashboard_port)

    async def stop(self) -> None:
        self.running = False
        for qs in self._streams.values():
            for q in qs:
                q.put_nowait(None)
        if self.runner:
            await self.runner.cleanup()

    # ------------------------------------------------------------------ middleware
    @web.middleware
    async def _headers(self, request: web.Request, handler):
        resp = await handler(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @web.middleware
    async def _errors(self, request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except ApiError as e:
            return _json({"error": e.message}, e.status)
        except (ServiceError, ValueError) as e:
            return _json({"error": str(e)}, 400)
        except Exception:
            log.exception("Dashboard error on %s %s", request.method, request.path)
            return _json({"error": "Something went wrong on the server. The error was logged."}, 500)

    def via_proxy(self, request: web.Request) -> bool:
        """True when the request came through the Netlify edge function (it sends the shared secret)."""
        secret = self.s.dashboard_proxy_secret
        # X-Pulse-* is the header name used before the rename to Hive Dispatch; accepted so an older site still works.
        got = request.headers.get("X-Hive-Proxy-Secret") or request.headers.get("X-Pulse-Proxy-Secret", "")
        return bool(secret) and bool(got) and secrets.compare_digest(got, secret)

    @web.middleware
    async def _proxy_guard(self, request: web.Request, handler):
        # With Netlify hosting, everything must arrive through the Netlify site. Requests that hit the tunnel
        # address directly are refused, so the bot is never exposed without the dashboard's protections.
        if self.s.dashboard_proxy_secret and request.path != "/healthz" and not self.via_proxy(request):
            if request.path.startswith("/api/"):
                return _json({"error": "Open the dashboard through its Netlify address."}, 403)
            return self._page("Use the dashboard address", f"Open the dashboard at {self.base_url} instead.", ok=False, status=403)
        return await handler(request)

    def ip(self, request: web.Request) -> str:
        client_ip = request.headers.get("X-Hive-Client-IP") or request.headers.get("X-Pulse-Client-IP")
        if self.via_proxy(request) and client_ip:
            return client_ip.strip()[:64]
        if self.s.dashboard_trust_proxy:
            for h in ("CF-Connecting-IP", "X-Real-IP"):
                if request.headers.get(h):
                    return request.headers[h].strip()
            if request.headers.get("X-Forwarded-For"):
                return request.headers["X-Forwarded-For"].split(",")[0].strip()
        return request.remote or "?"

    def _check_origin(self, request: web.Request) -> None:
        origin = request.headers.get("Origin")
        if not origin:
            return  # same-origin fetch from older browsers; CSRF token still required
        allowed = {self.base_url.rstrip("/"), f"http://localhost:{self.s.dashboard_port}", f"http://127.0.0.1:{self.s.dashboard_port}"}
        if origin.rstrip("/") not in allowed:
            raise ApiError(403, "Request blocked: it did not come from the dashboard.")

    # ------------------------------------------------------------------ sessions
    async def session(self, request: web.Request) -> Optional[dict]:
        token = request.cookies.get(COOKIE)
        if not token or len(token) > 200:
            return None
        row = await self.bot.db.fetchone("SELECT * FROM web_sessions WHERE sid_hash = ?", (_hash(token),))
        if not row:
            return None
        sess = dict(row)
        if (parse_iso(sess["expires_at"]) or utcnow()) <= utcnow():
            await self.bot.db.execute("DELETE FROM web_sessions WHERE sid_hash = ?", (sess["sid_hash"],))
            return None
        last = parse_iso(sess["last_seen"])
        if last is None or (utcnow() - last).total_seconds() > 300:
            await self.bot.db.execute("UPDATE web_sessions SET last_seen = ? WHERE sid_hash = ?", (iso_now(), sess["sid_hash"]))
        return sess

    async def require_session(self, request: web.Request, *, write: bool = False) -> dict:
        sess = await self.session(request)
        if sess is None:
            raise ApiError(401, "Please sign in again.")
        if self.api_limiter.check("api", sess["sid_hash"]) is not None:
            raise ApiError(429, "Too many requests — slow down for a moment.")
        if write:
            self._check_origin(request)
            token = request.headers.get("X-CSRF-Token", "")
            if not token or not secrets.compare_digest(token, sess["csrf"]):
                raise ApiError(403, "Your session token is out of date. Reload the page.")
            if self.write_limiter.check("write", sess["sid_hash"]) is not None:
                raise ApiError(429, "Too many changes in a short time — wait a moment.")
        return sess

    def _set_cookie(self, resp: web.Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="Lax", secure=self.secure, path="/")

    # ------------------------------------------------------------------ pages
    async def _index(self, request: web.Request) -> web.Response:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def _health(self, request: web.Request) -> web.Response:
        ok = True
        try:
            await self.bot.db.fetchval("SELECT 1")
        except Exception:
            ok = False
        return _json({"ok": ok, "version": VERSION, "discord": self.bot.is_ready()}, 200 if ok else 503)

    def _page(self, title: str, body: str, ok: bool = True, status: int = 200) -> web.Response:
        import html as h
        page = (STATIC / "result.html").read_text(encoding="utf-8")
        page = page.replace("{{title}}", h.escape(title)).replace("{{body}}", h.escape(body)).replace(
            "{{state}}", "ok" if ok else "bad").replace("{{v}}", VERSION)
        return web.Response(text=page, content_type="text/html", status=status, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ Discord OAuth2
    async def _login(self, request: web.Request) -> web.Response:
        if self.auth_limiter.check("login", self.ip(request)) is not None:
            return self._page("Slow down", "Too many sign-in attempts. Wait a minute and try again.", ok=False, status=429)
        if not self.s.discord_client_secret or not self.client_id():
            return self._page("Sign-in is not set up", "The bot owner needs to add DISCORD_CLIENT_SECRET to the .env file "
                                                       "(see docs/DASHBOARD.md) and restart the bot.", ok=False, status=503)
        state = secrets.token_urlsafe(24)
        q = {"client_id": self.client_id(), "redirect_uri": f"{self.base_url}/auth/callback", "response_type": "code",
             "scope": "identify", "state": state, "prompt": "none"}
        resp = web.Response(status=302, headers={"Location": f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(q)}"})
        self._set_cookie(resp, STATE_COOKIE, state, 600)
        return resp

    async def _callback(self, request: web.Request) -> web.Response:
        if self.auth_limiter.check("callback", self.ip(request)) is not None:
            return self._page("Slow down", "Too many sign-in attempts. Wait a minute and try again.", ok=False, status=429)
        state, expected = request.query.get("state", ""), request.cookies.get(STATE_COOKIE, "")
        if request.query.get("error"):
            return self._page("Sign-in cancelled", "Discord sign-in was cancelled. You can close this page or try again.", ok=False)
        if not state or not expected or not secrets.compare_digest(state, expected):
            return self._page("Sign-in expired", "That sign-in link expired or was opened in another browser. Please try again.", ok=False, status=400)
        code = request.query.get("code", "")
        try:
            sess = self.bot.session()
            async with sess.post(f"{DISCORD_API}/oauth2/token", data={
                    "client_id": self.client_id(), "client_secret": self.s.discord_client_secret, "grant_type": "authorization_code",
                    "code": code, "redirect_uri": f"{self.base_url}/auth/callback"}, timeout=ClientTimeout(total=15)) as r:
                tok = await r.json(content_type=None)
                if r.status != 200 or "access_token" not in tok:
                    log.warning("Discord token exchange failed (%s): %s", r.status, str(tok)[:200])
                    return self._page("Sign-in failed", "Discord did not accept the sign-in. Check that the redirect URL in the "
                                                        "Developer Portal matches DASHBOARD_PUBLIC_URL/auth/callback exactly.", ok=False, status=400)
            async with sess.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {tok['access_token']}"},
                                timeout=ClientTimeout(total=15)) as r:
                me = await r.json(content_type=None)
                if r.status != 200 or "id" not in me:
                    return self._page("Sign-in failed", "Could not read your Discord account.", ok=False, status=400)
        except Exception:
            log.exception("Discord OAuth failed")
            return self._page("Sign-in failed", "Could not reach Discord. Try again in a moment.", ok=False, status=502)
        token = secrets.token_urlsafe(32)
        avatar = (f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64" if me.get("avatar")
                  else "https://cdn.discordapp.com/embed/avatars/0.png")
        expires = (utcnow() + dt.timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        await self.bot.db.execute(
            "INSERT INTO web_sessions(sid_hash, user_id, username, avatar_url, csrf, ip, user_agent, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_hash(token), int(me["id"]), me.get("global_name") or me.get("username"), avatar, secrets.token_urlsafe(24), self.ip(request),
             (request.headers.get("User-Agent") or "")[:200], expires))
        await self.bot.db.execute("DELETE FROM web_sessions WHERE expires_at < ?", (iso_now(),))
        await self.bot.audit.record(None, int(me["id"]), "dashboard.login", None, {"ip": self.ip(request)})
        resp = web.Response(status=302, headers={"Location": "/"})
        self._set_cookie(resp, COOKIE, token, SESSION_DAYS * 86400)
        resp.del_cookie(STATE_COOKIE, path="/")
        return resp

    async def _logout(self, request: web.Request) -> web.Response:
        sess = await self.require_session(request, write=True)
        await self.bot.db.execute("DELETE FROM web_sessions WHERE sid_hash = ?", (sess["sid_hash"],))
        resp = _json({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def _logout_all(self, request: web.Request) -> web.Response:
        sess = await self.require_session(request, write=True)
        n = await self.bot.db.execute_rowcount("DELETE FROM web_sessions WHERE user_id = ?", (sess["user_id"],))
        resp = _json({"ok": True, "ended": n})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def _my_sessions(self, request: web.Request) -> web.Response:
        sess = await self.require_session(request)
        rows = await self.bot.db.fetchall("SELECT sid_hash, ip, user_agent, created_at, last_seen FROM web_sessions WHERE user_id = ? "
                                          "ORDER BY last_seen DESC", (sess["user_id"],))
        return _json({"items": [{"current": r["sid_hash"] == sess["sid_hash"], "ip": r["ip"], "user_agent": r["user_agent"],
                                 "created_at": r["created_at"], "last_seen": r["last_seen"]} for r in rows]})

    # ------------------------------------------------------------------ guild access
    async def member(self, guild, user_id: int):
        key = (guild.id, user_id)
        hit = self._member_cache.get(key)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        m = guild.get_member(user_id)
        if m is None:
            try:
                m = await guild.fetch_member(user_id)
            except Exception:
                m = None
        self._member_cache[key] = (time.monotonic(), m)
        if len(self._member_cache) > 5000:
            self._member_cache.clear()
        return m

    async def access(self, sess: dict, guild_id: int) -> tuple[object, set[str]]:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise ApiError(404, "Hive Dispatch is not in that server.")
        m = await self.member(guild, int(sess["user_id"]))
        if m is None:
            raise ApiError(403, "You are not a member of that server.")
        perms = await self.bot.permissions.all_for(guild_id, Actor.from_member(m))
        if "dashboard.view" not in perms:
            await self._log_denied(guild_id, sess)
            raise ApiError(403, "Your roles don't give you access to this server's dashboard. Ask an administrator.")
        return m, perms

    async def _log_denied(self, guild_id: int, sess: dict) -> None:
        key = (guild_id, int(sess["user_id"]))
        if time.monotonic() - self._denied_logged.get(key, 0) < 600:
            return
        self._denied_logged[key] = time.monotonic()
        await self.bot.logbook.write(guild_id, "security", "Dashboard access denied",
                                     fields=[("User", f"<@{sess['user_id']}> ({sess.get('username') or '?'})", True)])

    async def _me(self, request: web.Request) -> web.Response:
        sess = await self.session(request)
        if sess is None:
            return _json({"signed_in": False, "login_ready": bool(self.s.discord_client_secret and self.client_id())})
        guilds = []
        for g in self.bot.guilds:
            m = g.get_member(int(sess["user_id"]))
            if m is None and not g.chunked and len(guilds) < 25:
                m = await self.member(g, int(sess["user_id"]))
            if m is None:
                continue
            perms = await self.bot.permissions.all_for(g.id, Actor.from_member(m))
            guilds.append({"id": str(g.id), "name": g.name, "icon": g.icon.url if g.icon else None, "can_open": "dashboard.view" in perms})
        return _json({"signed_in": True, "csrf": sess["csrf"], "version": VERSION,
                      "user": {"id": str(sess["user_id"]), "name": sess["username"], "avatar": sess["avatar_url"]},
                      "guilds": sorted(guilds, key=lambda g: (not g["can_open"], g["name"].lower()))})

    async def _api(self, request: web.Request) -> web.StreamResponse:
        write = request.method not in {"GET", "HEAD"}
        sess = await self.require_session(request, write=write)
        try:
            gid = int(request.match_info["gid"])
        except ValueError:
            raise ApiError(400, "Bad server id.") from None
        member, perms = await self.access(sess, gid)
        body = {}
        if write and request.can_read_body:
            try:
                body = await request.json()
            except Exception:
                raise ApiError(400, "The request body must be JSON.") from None
            if not isinstance(body, dict):
                raise ApiError(400, "The request body must be a JSON object.")
        ctx = Ctx(guild_id=gid, user_id=int(sess["user_id"]), perms=perms, role_ids={r.id for r in getattr(member, "roles", [])},
                  query=dict(request.query), body=body, username=getattr(member, "display_name", None) or sess["username"] or "")
        result = await self.api.dispatch(request.method, "/" + request.match_info["path"].strip("/"), ctx)
        if write and request.method != "GET":
            path = request.match_info["path"]
            if not path.startswith(("notifications/read", "assistant", "prefs", "embeds/preview")):
                await self.bot.logbook.write(gid, "dashboard", f"Dashboard: {request.method} /{path}"[:120],
                                             fields=[("User", f"<@{sess['user_id']}>", True)])
        if isinstance(result, CsvResponse):
            return web.Response(text=result.text, content_type="text/csv", headers={
                "Content-Disposition": f'attachment; filename="{result.filename}"', "Cache-Control": "no-store"})
        return _json(result)

    # ------------------------------------------------------------------ live updates (Server-Sent Events)
    async def _on_event(self, e) -> None:
        qs = self._streams.get(e.guild_id)
        if not qs:
            return
        # v5.0: the ER:LC loop reports "still connected" after every poll (every few seconds). That heartbeat used to
        # make every open page reload. Only real changes are forwarded now (status changes, player joins/leaves).
        if e.name == "erlc.status" and e.data.get("poll"):
            return
        # Only the event *name* is sent; the page then re-fetches what it shows through the normal,
        # permission-checked API, so nothing private leaks through the stream.
        msg = e.name
        for q in list(qs):
            if q.qsize() < 100:
                q.put_nowait(msg)

    async def _events(self, request: web.Request) -> web.StreamResponse:
        sess = await self.require_session(request)
        gid = int(request.match_info["gid"])
        await self.access(sess, gid)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        self._streams.setdefault(gid, set()).add(q)
        try:
            await resp.write(b"retry: 5000\n\n")
            started = time.monotonic()
            while self.running and time.monotonic() - started < 3600:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    await resp.write(b": keep-alive\n\n")
                    continue
                if msg is None:
                    break
                await resp.write(f"data: {msg}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._streams.get(gid, set()).discard(q)
        return resp

    # ------------------------------------------------------------------ Roblox OAuth (verification)
    def _roblox_redirect(self) -> str:
        return f"{self.s.dashboard_public_url}/auth/roblox/callback"

    async def _roblox_start(self, request: web.Request) -> web.Response:
        v = self.bot.community.verification
        if not (v.oauth_available() and self.s.dashboard_public_url):
            return self._page("Not available", "Roblox sign-in is not set up on this bot. Use the profile-code method in Discord instead.",
                              ok=False, status=503)
        if self.auth_limiter.check("roblox", self.ip(request)) is not None:
            return self._page("Slow down", "Too many attempts. Wait a minute and try again.", ok=False, status=429)
        req = await v.oauth_request(request.match_info["token"][:100])
        if req is None:
            return self._page("Link expired", "This verification link expired or was already used. Run /verify in Discord again.",
                              ok=False, status=410)
        raise web.HTTPFound(self.bot.roblox.authorize_url(self._roblox_redirect(), req["token"], v.oauth_challenge(req)))

    async def _roblox_callback(self, request: web.Request) -> web.Response:
        if request.query.get("error"):
            return self._page("Verification cancelled", "You cancelled the Roblox sign-in. Run /verify in Discord to try again.", ok=False)
        state, code = request.query.get("state", "")[:100], request.query.get("code", "")[:2000]
        if not state or not code:
            return self._page("Verification failed", "Roblox did not send the expected details. Run /verify again.", ok=False, status=400)
        try:
            res = await self.bot.community.verification.complete_oauth(state, code, self._roblox_redirect())
        except ServiceError as e:
            return self._page("Verification failed", str(e), ok=False, status=400)
        g = self.bot.get_guild(int(res["guild_id"]))
        extra = " Some roles could not be updated — staff have been told in the log." if res["problems"] else ""
        return self._page("You're verified!", f"Your Discord account is now linked to {res['roblox']['name']} on Roblox"
                                              f"{' in ' + g.name if g else ''}. You can close this page and go back to Discord.{extra}")


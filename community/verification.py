"""Roblox account verification.

Two legitimate methods - an account is only ever linked after one of them really succeeds:

1. **Profile code** (works out of the box). The member gets a random phrase of plain words, puts it in
   the *About* section of their Roblox profile, and presses *Done*. The bot reads the public profile
   through the Roblox Users API and links the account only if the phrase is there.
2. **Roblox OAuth 2.0** (optional). The member signs in on roblox.com through the official
   authorization page; Roblox tells us which account signed in. Needs ROBLOX_CLIENT_ID /
   ROBLOX_CLIENT_SECRET and the web dashboard reachable at DASHBOARD_PUBLIC_URL.

Staff can also link manually (recorded as method "manual" with who did it).
"""
from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Optional

from .common import ServiceError, ValidationError, clean_text, iso_in, iso_now, paginate_sql, parse_iso, row_dicts, utcnow
from .roblox import RobloxAPI, RobloxError, RobloxUser

if TYPE_CHECKING:
    from .platform import CommunityPlatform

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
CODE_TTL_SECONDS = 20 * 60
OAUTH_TTL_SECONDS = 10 * 60
MAX_CHECKS = 10

# Plain words that survive Roblox's text filter (no numbers - they are often hashed out in profiles).
WORDS = ("amber", "anchor", "apple", "arrow", "autumn", "badge", "beacon", "birch", "blue", "bright", "brook", "cabin",
         "canyon", "cedar", "cloud", "clover", "cobalt", "comet", "coral", "crystal", "delta", "desert", "eagle", "ember",
         "falcon", "fern", "forest", "frost", "garden", "glacier", "golden", "granite", "harbor", "hawk", "island", "ivory",
         "jade", "lantern", "lemon", "maple", "meadow", "mint", "moon", "mountain", "ocean", "olive", "orbit", "pebble",
         "pine", "planet", "prairie", "quartz", "rain", "river", "rocket", "sage", "silver", "sky", "spring", "star",
         "summit", "thunder", "tiger", "valley")

NICKNAME_FORMATS = {"": "Do not change nicknames", "{roblox}": "Roblox username", "{display}": "Roblox display name",
                    "{display} (@{roblox})": "Display name (@username)", "{discord} | {roblox}": "Discord name | Roblox username"}

DEFAULTS = {
    "enabled": True,
    "verified_role_id": None,        # given after a successful verification
    "unverified_role_id": None,      # removed after a successful verification (given back when unlinked)
    "nickname_format": "",
    "min_account_age_days": 0,
    "block_banned_accounts": True,
    "allow_profile_code": True,
    "allow_oauth": True,
    "dm_on_verify": True,
    "panel_channel_id": None,
    "use_bloxlink": True,         # check Bloxlink first (needs the server's Bloxlink API key)
    "use_rover": True,            # check RoVer first (needs the server's RoVer API key)
    "auto_verify_on_join": True,  # new members already linked in Bloxlink/RoVer are verified automatically
}
EXTERNAL = {"bloxlink": "Bloxlink", "rover": "RoVer"}
METHOD_LABEL = {"profile_code": "Profile code", "oauth": "Roblox sign-in (OAuth)", "manual": "Linked by staff",
                "bloxlink": "Imported from Bloxlink", "rover": "Imported from RoVer"}


def new_code() -> str:
    return "pulse " + " ".join(secrets.choice(WORDS) for _ in range(6))


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z]+", " ", (text or "").lower()).split())


def code_in_text(code: str, text: str) -> bool:
    return _norm(code) in _norm(text)


class VerificationService:
    def __init__(self, p: "CommunityPlatform", roblox: RobloxAPI):
        self.p = p
        self.db = p.db
        self.roblox = roblox

    async def settings(self, guild_id: int) -> dict:
        return await self.p.settings(guild_id, "verification_settings", DEFAULTS)

    async def update_settings(self, guild_id: int, changes: dict, actor_id: int) -> dict:
        clean = {}
        for k, v in changes.items():
            if k in {"verified_role_id", "unverified_role_id", "panel_channel_id"}:
                clean[k] = int(v) if v not in (None, "", 0, "0") else None
            elif k == "nickname_format":
                if v not in NICKNAME_FORMATS:
                    raise ValidationError("Unknown nickname format.")
                clean[k] = v
            elif k == "min_account_age_days":
                clean[k] = max(0, min(3650, int(v or 0)))
            else:
                clean[k] = bool(v)
        return await self.p.update_settings(guild_id, "verification_settings", DEFAULTS, clean, actor_id)

    def oauth_available(self) -> bool:
        return self.roblox.oauth_configured

    # ------------------------------------------------------------------ queries
    async def get_link(self, guild_id: int, discord_id: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM roblox_links WHERE guild_id = ? AND discord_id = ?", (guild_id, int(discord_id)))
        if not r:
            return None
        d = dict(r)
        d["roblox_id"] = str(d["roblox_id"])
        d["discord_id"] = str(d["discord_id"])
        d["profile_url"] = f"https://www.roblox.com/users/{d['roblox_id']}/profile"
        return d

    async def link_by_roblox(self, guild_id: int, roblox_id: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT discord_id FROM roblox_links WHERE guild_id = ? AND roblox_id = ?", (guild_id, int(roblox_id)))
        return await self.get_link(guild_id, r["discord_id"]) if r else None

    async def pending_request(self, guild_id: int, discord_id: int) -> Optional[dict]:
        r = await self.db.fetchone(
            "SELECT * FROM verification_requests WHERE guild_id = ? AND discord_id = ? AND status = 'pending' "
            "AND method = 'profile_code' ORDER BY id DESC LIMIT 1", (guild_id, discord_id))
        if not r:
            return None
        d = dict(r)
        if (parse_iso(d["expires_at"]) or utcnow()) <= utcnow():
            await self.db.execute("UPDATE verification_requests SET status = 'expired' WHERE id = ?", (d["id"],))
            return None
        return d

    async def list_links(self, guild_id: int, q: str = "", page: int = 1, per_page: int = 25) -> dict:
        where, params = "WHERE rl.guild_id = ?", [guild_id]
        if q:
            where += " AND (rl.roblox_username LIKE ? OR CAST(rl.discord_id AS TEXT) = ? OR CAST(rl.roblox_id AS TEXT) = ? OR m.username LIKE ?)"
            params += [f"%{q}%", q, q, f"%{q}%"]
        base = f" FROM roblox_links rl LEFT JOIN members m ON m.guild_id = rl.guild_id AND m.user_id = rl.discord_id {where}"
        pg = await paginate_sql(self.db, "SELECT rl.*, m.username, m.display_name, m.avatar_url" + base + " ORDER BY rl.verified_at DESC",
                                "SELECT COUNT(*)" + base, params, page, per_page)
        for it in pg.items:
            it["discord_id"], it["roblox_id"] = str(it["discord_id"]), str(it["roblox_id"])
        return pg.as_dict()

    async def list_pending(self, guild_id: int) -> list[dict]:
        rows = row_dicts(await self.db.fetchall(
            "SELECT r.id, r.discord_id, r.roblox_username, r.method, r.attempts, r.created_at, r.expires_at, m.username "
            "FROM verification_requests r LEFT JOIN members m ON m.guild_id = r.guild_id AND m.user_id = r.discord_id "
            "WHERE r.guild_id = ? AND r.status = 'pending' AND r.expires_at > ? ORDER BY r.id DESC LIMIT 100", (guild_id, iso_now())))
        for r in rows:
            r["discord_id"] = str(r["discord_id"])
        return rows

    async def history(self, guild_id: int, discord_id: Optional[int] = None, page: int = 1, per_page: int = 25) -> dict:
        where, params = "WHERE guild_id = ?", [guild_id]
        if discord_id:
            where += " AND discord_id = ?"
            params.append(int(discord_id))
        pg = await paginate_sql(self.db, f"SELECT * FROM verification_history {where} ORDER BY id DESC",
                                f"SELECT COUNT(*) FROM verification_history {where}", params, page, per_page)
        for it in pg.items:
            it["discord_id"] = str(it["discord_id"])
            it["roblox_id"] = str(it["roblox_id"]) if it.get("roblox_id") else None
        return pg.as_dict()

    # ------------------------------------------------------------------ requirements
    async def _check_requirements(self, guild_id: int, user: RobloxUser, discord_id: int) -> None:
        s = await self.settings(guild_id)
        if s["block_banned_accounts"] and user.is_banned:
            raise ServiceError("That Roblox account is banned and cannot be verified.")
        min_age = int(s.get("min_account_age_days") or 0)
        if min_age and user.created:
            created = parse_iso(user.created)
            if created and (utcnow() - created).days < min_age:
                raise ServiceError(f"This server requires Roblox accounts to be at least {min_age} day(s) old.")
        other = await self.link_by_roblox(guild_id, user.id)
        if other and int(other["discord_id"]) != int(discord_id):
            raise ServiceError("That Roblox account is already linked to another Discord member in this server. "
                               "Ask staff to remove the old link first.")

    # ------------------------------------------------------------------ method 1: profile code
    async def start_profile(self, guild_id: int, discord_id: int, username: str) -> dict:
        s = await self.settings(guild_id)
        if not s["enabled"] or not s["allow_profile_code"]:
            raise ServiceError("Profile-code verification is turned off on this server.")
        username = (username or "").strip().lstrip("@")
        if not USERNAME_RE.match(username):
            raise ValidationError("Roblox usernames are 3-20 letters, numbers or underscores.")
        try:
            found = await self.roblox.user_by_username(username)
        except RobloxError as e:
            raise ServiceError(str(e)) from e
        if found is None:
            raise ServiceError(f"No Roblox account named **{username}** was found. Check the spelling.")
        await self._check_requirements(guild_id, found, discord_id)
        await self.db.execute("UPDATE verification_requests SET status = 'cancelled' WHERE guild_id = ? AND discord_id = ? "
                              "AND status = 'pending'", (guild_id, discord_id))
        code = new_code()
        rid = await self.db.execute(
            "INSERT INTO verification_requests(guild_id, discord_id, roblox_id, roblox_username, method, code, expires_at) "
            "VALUES (?, ?, ?, ?, 'profile_code', ?, ?)", (guild_id, discord_id, found.id, found.name, code, iso_in(CODE_TTL_SECONDS)))
        return {"request_id": rid, "code": code, "roblox": found.as_dict(), "expires_in_minutes": CODE_TTL_SECONDS // 60,
                "profile_url": f"https://www.roblox.com/users/{found.id}/profile"}

    async def check_profile(self, guild_id: int, discord_id: int) -> dict:
        req = await self.pending_request(guild_id, discord_id)
        if req is None:
            raise ServiceError("You have no verification in progress (or it expired). Start again with `/verify`.")
        if req["attempts"] >= MAX_CHECKS:
            await self.db.execute("UPDATE verification_requests SET status = 'failed', failure = 'too many checks' WHERE id = ?", (req["id"],))
            raise ServiceError("Too many checks. Start a new verification with `/verify`.")
        await self.db.execute("UPDATE verification_requests SET attempts = attempts + 1 WHERE id = ?", (req["id"],))
        try:
            user = await self.roblox.user(req["roblox_id"])
        except RobloxError as e:
            raise ServiceError(str(e)) from e
        if user is None:
            raise ServiceError("That Roblox account no longer exists.")
        if not code_in_text(req["code"], user.description):
            raise ServiceError("The code was not found in your Roblox profile's **About** section yet. Save your profile, "
                               "wait a few seconds and press **Done** again. (Roblox can take a moment to update.)")
        await self._check_requirements(guild_id, user, discord_id)
        await self.db.execute("UPDATE verification_requests SET status = 'verified', completed_at = ? WHERE id = ?", (iso_now(), req["id"]))
        return await self._link(guild_id, discord_id, user, method="profile_code", actor_id=discord_id)

    # ------------------------------------------------------------------ method 2: OAuth
    async def start_oauth(self, guild_id: int, discord_id: int) -> dict:
        s = await self.settings(guild_id)
        if not s["enabled"] or not s["allow_oauth"]:
            raise ServiceError("Roblox sign-in verification is turned off on this server.")
        if not self.roblox.oauth_configured:
            raise ServiceError("Roblox sign-in is not set up by the bot owner (ROBLOX_CLIENT_ID / ROBLOX_CLIENT_SECRET).")
        token = secrets.token_urlsafe(32)
        verifier, challenge = self.roblox.new_pkce()
        await self.db.execute(
            "INSERT INTO verification_requests(guild_id, discord_id, method, code, token, expires_at) VALUES (?, ?, 'oauth', ?, ?, ?)",
            (guild_id, discord_id, verifier, token, iso_in(OAUTH_TTL_SECONDS)))
        return {"token": token, "challenge": challenge, "expires_in_minutes": OAUTH_TTL_SECONDS // 60}

    async def oauth_request(self, token: str) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM verification_requests WHERE token = ? AND method = 'oauth'", (token,))
        if not r or r["status"] != "pending" or (parse_iso(r["expires_at"]) or utcnow()) <= utcnow():
            return None
        return dict(r)

    def oauth_challenge(self, req: dict) -> str:
        import base64
        import hashlib
        return base64.urlsafe_b64encode(hashlib.sha256(req["code"].encode()).digest()).decode().rstrip("=")

    async def complete_oauth(self, token: str, code: str, redirect_uri: str) -> dict:
        req = await self.oauth_request(token)
        if req is None:
            raise ServiceError("This verification link expired or was already used. Run `/verify` again.")
        # single use: claim it before talking to Roblox
        n = await self.db.execute_rowcount("UPDATE verification_requests SET status = 'failed', failure = 'in progress' "
                                           "WHERE id = ? AND status = 'pending'", (req["id"],))
        if not n:
            raise ServiceError("This verification link was already used.")
        try:
            info = await self.roblox.exchange_code(code, req["code"], redirect_uri)
            user = await self.roblox.user(int(info["sub"])) or RobloxUser(
                id=int(info["sub"]), name=info.get("preferred_username") or info.get("name") or "", display_name=info.get("nickname") or "")
            await self._check_requirements(req["guild_id"], user, req["discord_id"])
        except (RobloxError, ServiceError) as e:
            await self.db.execute("UPDATE verification_requests SET failure = ? WHERE id = ?", (str(e)[:300], req["id"]))
            await self._history(req["guild_id"], req["discord_id"], None, None, "failed", "oauth", req["discord_id"], str(e)[:300])
            raise ServiceError(str(e)) from e
        await self.db.execute("UPDATE verification_requests SET status = 'verified', failure = NULL, roblox_id = ?, "
                              "roblox_username = ?, completed_at = ? WHERE id = ?", (user.id, user.name, iso_now(), req["id"]))
        result = await self._link(req["guild_id"], req["discord_id"], user, method="oauth", actor_id=req["discord_id"])
        result["guild_id"] = req["guild_id"]
        return result

    # ------------------------------------------------------------------ staff tools
    async def manual_link(self, guild_id: int, discord_id: int, username: str, actor_id: int, reason: str = "") -> dict:
        username = (username or "").strip().lstrip("@")
        if not USERNAME_RE.match(username):
            raise ValidationError("Roblox usernames are 3-20 letters, numbers or underscores.")
        try:
            found = await self.roblox.user_by_username(username)
            user = (await self.roblox.user(found.id)) if found else None
        except RobloxError as e:
            raise ServiceError(str(e)) from e
        if user is None:
            raise ServiceError(f"No Roblox account named **{username}** was found.")
        other = await self.link_by_roblox(guild_id, user.id)
        if other and int(other["discord_id"]) != int(discord_id):
            raise ServiceError(f"That Roblox account is already linked to <@{other['discord_id']}>. Remove that link first.")
        return await self._link(guild_id, discord_id, user, method="manual", actor_id=actor_id, reason=reason)

    async def unlink(self, guild_id: int, discord_id: int, actor_id: int, reason: str = "") -> bool:
        link = await self.get_link(guild_id, discord_id)
        if not link:
            return False
        await self.db.execute("DELETE FROM roblox_links WHERE guild_id = ? AND discord_id = ?", (guild_id, int(discord_id)))
        await self._history(guild_id, discord_id, int(link["roblox_id"]), link["roblox_username"], "removed", link["method"],
                            actor_id, clean_text(reason, 300))
        s = await self.settings(guild_id)
        problems = []
        if s.get("verified_role_id"):
            if (err := await self.p.actions.remove_role(guild_id, int(discord_id), int(s["verified_role_id"]), "Roblox link removed")):
                problems.append(f"verified role: {err}")
        if s.get("unverified_role_id"):
            if (err := await self.p.actions.add_role(guild_id, int(discord_id), int(s["unverified_role_id"]), "Roblox link removed")):
                problems.append(f"unverified role: {err}")
        await self.p.audit.record(guild_id, actor_id, "verification.remove", str(discord_id), {"roblox": link["roblox_username"]})
        await self.p.log(guild_id, "verification", "Roblox Link Removed",
                         fields=[("Member", f"<@{discord_id}>", True), ("Roblox", link["roblox_username"], True)]
                         + ([("Reason", clean_text(reason, 300), False)] if reason else [])
                         + ([("Problems", "; ".join(problems), False)] if problems else []), actor_id=actor_id)
        await self.p.publish("verification.removed", guild_id, user_id=int(discord_id), roblox=link["roblox_username"],
                             actor_id=actor_id)
        return True

    async def sync_roles(self, guild_id: int, discord_id: int) -> list[str]:
        link = await self.get_link(guild_id, discord_id)
        if not link:
            raise ServiceError("That member is not verified.")
        return await self._apply_discord(guild_id, int(discord_id), link["roblox_username"], link.get("display_name") or "")

    # ------------------------------------------------------------------ internals
    async def _link(self, guild_id: int, discord_id: int, user: RobloxUser, *, method: str, actor_id: int,
                    reason: str = "") -> dict:
        async with self.db.transaction() as c:
            await c.execute("DELETE FROM roblox_links WHERE guild_id = ? AND discord_id = ?", (guild_id, int(discord_id)))
            await c.execute("DELETE FROM roblox_links WHERE guild_id = ? AND roblox_id = ?", (guild_id, user.id))
            await c.execute("INSERT INTO roblox_links(guild_id, discord_id, roblox_id, roblox_username, display_name, method, verified_by) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", (guild_id, int(discord_id), user.id, user.name, user.display_name, method,
                                                             actor_id if method == "manual" else None))
            # Keep the dispatch officer record in step (used for ER:LC player matching).
            await c.execute("UPDATE officers SET roblox_username = ?, updated_at = ? WHERE guild_id = ? AND discord_id = ?",
                            (user.name, iso_now(), guild_id, int(discord_id)))
        await self._history(guild_id, discord_id, user.id, user.name, "manual_link" if method == "manual" else "verified", method,
                            actor_id, clean_text(reason, 300) or None)
        problems = await self._apply_discord(guild_id, int(discord_id), user.name, user.display_name)
        await self.p.audit.record(guild_id, actor_id, "verification.link", str(discord_id), {"roblox": user.name, "method": method})
        label = METHOD_LABEL.get(method, method)
        await self.p.log(guild_id, "verification", "Roblox Account Verified",
                         fields=[("Member", f"<@{discord_id}>", True), ("Roblox", f"[{user.name}](https://www.roblox.com/users/{user.id}/profile)", True),
                                 ("Method", label, True)] + ([("Problems", "; ".join(problems)[:500], False)] if problems else []),
                         actor_id=actor_id, colour=0x2EA043)
        await self.p.publish("verification.completed", guild_id, user_id=int(discord_id), roblox=user.name, roblox_id=user.id,
                             method=method, actor_id=actor_id)
        s = await self.settings(guild_id)
        if s.get("dm_on_verify") and method in {"profile_code", "oauth"}:
            await self.p.actions.dm(int(discord_id), embed={
                "title": "✅ Roblox account verified", "color": 0x2EA043,
                "description": f"Your Discord account is now linked to **{user.name}** on Roblox.",
                "footer": {"text": "Pulse Dispatch | ERLC"}})
        return {"roblox": user.as_dict(), "method": method, "problems": problems}

    async def _apply_discord(self, guild_id: int, discord_id: int, roblox_name: str, display: str) -> list[str]:
        s = await self.settings(guild_id)
        problems = []
        if s.get("verified_role_id"):
            if (err := await self.p.actions.add_role(guild_id, discord_id, int(s["verified_role_id"]), "Roblox verified")):
                problems.append(f"Could not give the verified role: {err}")
        if s.get("unverified_role_id"):
            if (err := await self.p.actions.remove_role(guild_id, discord_id, int(s["unverified_role_id"]), "Roblox verified")):
                problems.append(f"Could not remove the unverified role: {err}")
        fmt = s.get("nickname_format") or ""
        if fmt:
            m = self.p.actions.member(guild_id, discord_id) or {}
            nick = fmt.replace("{roblox}", roblox_name).replace("{display}", display or roblox_name) \
                      .replace("{discord}", (m.get("display_name") or m.get("name") or "")[:16])
            if (err := await self.p.actions.set_nickname(guild_id, discord_id, nick[:32], "Roblox verified")):
                problems.append(f"Could not set the nickname: {err}")
        return problems

    async def _history(self, guild_id, discord_id, roblox_id, roblox_username, action, method, actor_id, detail=None) -> None:
        await self.db.execute(
            "INSERT INTO verification_history(guild_id, discord_id, roblox_id, roblox_username, action, method, actor_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (guild_id, int(discord_id), roblox_id, roblox_username, action, method, actor_id, detail))

    async def stats(self, guild_id: int, since_iso: str) -> dict:
        row = await self.db.fetchone(
            "SELECT SUM(action IN ('verified','manual_link')) AS verified, SUM(action = 'removed') AS removed, "
            "SUM(action = 'failed') AS failed FROM verification_history WHERE guild_id = ? AND at >= ?", (guild_id, since_iso))
        total = await self.db.fetchval("SELECT COUNT(*) FROM roblox_links WHERE guild_id = ?", (guild_id,), 0)
        return {"linked": total, "verified": row["verified"] or 0, "removed": row["removed"] or 0, "failed": row["failed"] or 0}

    # ------------------------------------------------------------------ method 3: existing Bloxlink / RoVer links
    async def external_key(self, guild_id: int, provider: str) -> Optional[str]:
        if provider not in EXTERNAL:
            return None
        saved = await self.p.secrets.get(guild_id, f"verify_key:{provider}") if self.p.secrets is not None else None
        return saved or getattr(self.p, "env_keys", {}).get(provider)

    async def external_status(self, guild_id: int) -> dict:
        out = {}
        for prov in EXTERNAL:
            out[prov] = bool(getattr(self.p, "env_keys", {}).get(prov)) or bool(
                self.p.secrets is not None and await self.p.secrets.exists(guild_id, f"verify_key:{prov}"))
        return out

    async def set_external_key(self, guild_id: int, provider: str, key: Optional[str], actor_id: int) -> None:
        if provider not in EXTERNAL:
            raise ValidationError("Unknown service.")
        if self.p.secrets is None:
            raise ServiceError("Encrypted key storage is not available.")
        if key:
            key = key.strip()
            if not 8 <= len(key) <= 400 or any(c.isspace() for c in key):
                raise ValidationError("That doesn't look like an API key.")
            await self.p.secrets.set(guild_id, f"verify_key:{provider}", key, actor_id)
        else:
            await self.p.secrets.delete(guild_id, f"verify_key:{provider}")
        await self.p.audit.record(guild_id, actor_id, f"verification.{provider}_key", None, {"key": "***" if key else None})

    async def _lookup_external(self, guild_id: int, discord_id: int, provider: str) -> Optional[int]:
        key = await self.external_key(guild_id, provider)
        if not key:
            return None
        if provider == "bloxlink":
            return await self.roblox.bloxlink_lookup(guild_id, discord_id, key)
        return await self.roblox.rover_lookup(guild_id, discord_id, key)

    async def test_external(self, guild_id: int, provider: str, discord_id: int) -> dict:
        """Checks the stored key by looking up the person pressing Test."""
        if not await self.external_key(guild_id, provider):
            raise ServiceError(f"No {EXTERNAL.get(provider, provider)} API key is saved.")
        try:
            rid = await self._lookup_external(guild_id, discord_id, provider)
        except RobloxError as e:
            raise ServiceError(str(e)) from e
        if rid is None:
            return {"ok": True, "message": f"The key works. You aren't linked in {EXTERNAL[provider]} for this server, so nothing was found."}
        user = None
        try:
            user = await self.roblox.user(rid)
        except RobloxError:
            pass
        return {"ok": True, "message": f"The key works. {EXTERNAL[provider]} has you linked to {user.name if user else f'Roblox user {rid}'}."}

    async def try_external(self, guild_id: int, discord_id: int, *, quiet: bool = False) -> Optional[dict]:
        """Verify instantly if Bloxlink or RoVer already has this member linked. Returns the result, or None
        if no configured service knows them. Errors from one service never block the others."""
        s = await self.settings(guild_id)
        if not s["enabled"]:
            return None
        for prov in ("bloxlink", "rover"):
            if not s.get(f"use_{prov}"):
                continue
            try:
                rid = await self._lookup_external(guild_id, discord_id, prov)
                if rid is None:
                    continue
                user = await self.roblox.user(rid)
                if user is None:
                    continue
                await self._check_requirements(guild_id, user, discord_id)
            except RobloxError as e:
                if not quiet:
                    await self.p.log(guild_id, "verification", f"{EXTERNAL[prov]} lookup failed", description=str(e)[:300])
                continue
            except ServiceError:
                if quiet:
                    return None
                raise
            return await self._link(guild_id, discord_id, user, method=prov, actor_id=discord_id)
        return None

    async def expire_old(self) -> int:
        return await self.db.execute_rowcount("UPDATE verification_requests SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
                                              (iso_now(),))

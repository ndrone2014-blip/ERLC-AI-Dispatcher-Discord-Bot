"""discord.py implementation of community.actions.DiscordActions.

Every method returns a short, human-readable error (or None) instead of raising, so services
can record an honest outcome ("Missing Permissions", "DMs are closed") with each action.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Optional

import discord

from community.actions import DiscordActions

if TYPE_CHECKING:
    from bot.client import DispatchBot

log = logging.getLogger(__name__)


def _err(e: Exception) -> str:
    if isinstance(e, discord.Forbidden):
        return "Missing Permissions (check the bot's role position and channel permissions)"
    if isinstance(e, discord.NotFound):
        return "Not found (it may have been deleted)"
    if isinstance(e, discord.HTTPException):
        return f"Discord error {e.status}: {e.text[:120] if e.text else ''}".strip()
    return type(e).__name__


def build_embed(data: Optional[dict]) -> tuple[Optional[discord.Embed], Optional[discord.ui.View]]:
    if not data:
        return None, None
    d = dict(data)
    buttons = d.pop("buttons", None) or []
    session_number = d.pop("_session", None)  # session announcements get Join / Leave buttons
    if d.get("timestamp") is True:
        d["timestamp"] = dt.datetime.now(dt.timezone.utc).isoformat()
    if "colour" in d and "color" not in d:
        d["color"] = d.pop("colour")
    embed = discord.Embed.from_dict({k: v for k, v in d.items() if v not in (None, "", [])})
    view = None
    if session_number is not None:
        from bot.commands.community_ui import session_view
        view = session_view(int(session_number))
    if buttons:
        view = view or discord.ui.View(timeout=None)
        for b in buttons[:5]:
            view.add_item(discord.ui.Button(label=b["label"][:80], url=b["url"]))
    return embed, view


class BotActions(DiscordActions):
    available = True

    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    def _guild(self, guild_id: int) -> Optional[discord.Guild]:
        return self.bot.get_guild(int(guild_id))

    async def _member(self, guild_id: int, user_id: int) -> Optional[discord.Member]:
        g = self._guild(guild_id)
        if g is None:
            return None
        m = g.get_member(int(user_id))
        if m is None:
            try:
                m = await g.fetch_member(int(user_id))
            except discord.HTTPException:
                return None
        return m

    async def _role_change(self, guild_id: int, user_id: int, role_id: int, add: bool, reason: str) -> Optional[str]:
        g = self._guild(guild_id)
        if g is None:
            return "The bot is not in that server."
        role = g.get_role(int(role_id))
        if role is None:
            return f"Role {role_id} no longer exists."
        if role >= g.me.top_role:
            return f"The @{role.name} role is above the bot's highest role. Move the bot's role higher in Server Settings → Roles."
        m = await self._member(guild_id, user_id)
        if m is None:
            return "That member is not in the server."
        try:
            if add and role not in m.roles:
                await m.add_roles(role, reason=reason[:500] or None)
            elif not add and role in m.roles:
                await m.remove_roles(role, reason=reason[:500] or None)
        except discord.HTTPException as e:
            return _err(e)
        return None

    async def add_role(self, guild_id, user_id, role_id, reason=""):
        return await self._role_change(guild_id, user_id, role_id, True, reason)

    async def remove_role(self, guild_id, user_id, role_id, reason=""):
        return await self._role_change(guild_id, user_id, role_id, False, reason)

    async def set_nickname(self, guild_id, user_id, nickname, reason=""):
        g = self._guild(guild_id)
        m = await self._member(guild_id, user_id)
        if g is None or m is None:
            return "That member is not in the server."
        if m.id == g.owner_id:
            return "Discord does not allow bots to change the server owner's nickname."
        if m.top_role >= g.me.top_role:
            return "Their highest role is above the bot's role."
        try:
            await m.edit(nick=(nickname or None) and nickname[:32], reason=reason[:500] or None)
        except discord.HTTPException as e:
            return _err(e)
        return None

    async def timeout(self, guild_id, user_id, seconds, reason=""):
        m = await self._member(guild_id, user_id)
        if m is None:
            return "That member is not in the server."
        try:
            await m.timeout(dt.timedelta(seconds=int(seconds)), reason=reason[:500] or None)
        except discord.HTTPException as e:
            return _err(e)
        return None

    async def remove_timeout(self, guild_id, user_id, reason=""):
        m = await self._member(guild_id, user_id)
        if m is None:
            return "That member is not in the server."
        try:
            await m.timeout(None, reason=reason[:500] or None)
        except discord.HTTPException as e:
            return _err(e)
        return None

    async def kick(self, guild_id, user_id, reason=""):
        m = await self._member(guild_id, user_id)
        if m is None:
            return "That member is not in the server."
        try:
            await m.kick(reason=reason[:500] or None)
        except discord.HTTPException as e:
            return _err(e)
        return None

    async def ban(self, guild_id, user_id, reason="", delete_message_days=0):
        g = self._guild(guild_id)
        if g is None:
            return "The bot is not in that server."
        try:
            await g.ban(discord.Object(id=int(user_id)), reason=reason[:500] or None,
                        delete_message_seconds=int(delete_message_days) * 86400)
        except discord.HTTPException as e:
            return _err(e)
        return None

    async def unban(self, guild_id, user_id, reason=""):
        g = self._guild(guild_id)
        if g is None:
            return "The bot is not in that server."
        try:
            await g.unban(discord.Object(id=int(user_id)), reason=reason[:500] or None)
        except discord.NotFound:
            return "That user is not banned."
        except discord.HTTPException as e:
            return _err(e)
        return None

    async def dm(self, user_id, content=None, embed=None):
        try:
            user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
            e, view = build_embed(embed)
            kwargs = {"content": content, "embed": e}
            if view:
                kwargs["view"] = view
            await user.send(**kwargs)
        except discord.Forbidden:
            return "Their DMs are closed."
        except discord.HTTPException as e:
            return _err(e)
        return None

    async def send(self, guild_id, channel_id, content=None, embed=None, *, mention_roles=None):
        ch = self.bot.get_channel(int(channel_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)) or getattr(ch, "guild", None) is None or ch.guild.id != int(guild_id):
            return None, "That channel no longer exists (or belongs to another server)."
        e, view = build_embed(embed)
        allowed = discord.AllowedMentions(everyone=False, users=True, replied_user=False,
                                          roles=[discord.Object(id=r) for r in (mention_roles or [])] or False)
        try:
            kwargs = {"content": content, "embed": e, "allowed_mentions": allowed}
            if view:
                kwargs["view"] = view
            msg = await ch.send(**kwargs)
            return msg.id, None
        except discord.HTTPException as ex:
            return None, _err(ex)

    async def edit_message(self, guild_id, channel_id, message_id, content=None, embed=None):
        ch = self.bot.get_channel(int(channel_id))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)) or ch.guild.id != int(guild_id):
            return "That channel no longer exists."
        e, _ = build_embed(embed)
        try:
            await ch.get_partial_message(int(message_id)).edit(content=content, embed=e)
        except discord.HTTPException as ex:
            return _err(ex)
        return None

    def can_moderate(self, guild_id, moderator_id, target_id):
        g = self._guild(guild_id)
        if g is None:
            return "The bot is not in that server."
        target = g.get_member(int(target_id))
        if target is None:
            return None  # not in the server: bans by ID are allowed
        if target.id == g.owner_id:
            return "You can't moderate the server owner."
        if target.bot and target.id == g.me.id:
            return "I can't moderate myself."
        mod = g.get_member(int(moderator_id))
        if mod is not None and mod.id != g.owner_id and target.top_role >= mod.top_role:
            return "Their highest role is the same as or above yours."
        if target.top_role >= g.me.top_role:
            return "Their highest role is above the bot's role. Move the bot's role higher in Server Settings → Roles."
        return None

    def member(self, guild_id, user_id):
        g = self._guild(guild_id)
        m = g.get_member(int(user_id)) if g else None
        if m is None:
            return None
        return member_dict(m)

    def guild_info(self, guild_id):
        g = self._guild(guild_id)
        if g is None:
            return None
        return {"id": str(g.id), "name": g.name, "member_count": g.member_count, "icon_url": g.icon.url if g.icon else None,
                "owner_id": str(g.owner_id)}


def member_dict(m: discord.Member) -> dict:
    return {"id": m.id, "name": m.name, "display_name": m.display_name, "avatar_url": m.display_avatar.url, "bot": m.bot,
            "roles": [r.id for r in m.roles if not r.is_default()],
            "joined_at": m.joined_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if m.joined_at else None}

"""The boundary between community services and Discord.

`DiscordActions` is the interface; `bot/actions.py` implements it with discord.py. The
base class here does nothing and reports that Discord is unavailable, which is also what
the tests use (through `RecordingActions`).

Every method returns an error string (or None on success) instead of raising, so a
failed role change never breaks the database record of what happened: the record keeps
an honest outcome instead.
"""
from __future__ import annotations

from typing import Any, Optional


class DiscordActions:
    available = False

    async def add_role(self, guild_id: int, user_id: int, role_id: int, reason: str = "") -> Optional[str]:
        return "Discord is not connected."

    async def remove_role(self, guild_id: int, user_id: int, role_id: int, reason: str = "") -> Optional[str]:
        return "Discord is not connected."

    async def set_nickname(self, guild_id: int, user_id: int, nickname: Optional[str], reason: str = "") -> Optional[str]:
        return "Discord is not connected."

    async def timeout(self, guild_id: int, user_id: int, seconds: int, reason: str = "") -> Optional[str]:
        return "Discord is not connected."

    async def remove_timeout(self, guild_id: int, user_id: int, reason: str = "") -> Optional[str]:
        return "Discord is not connected."

    async def kick(self, guild_id: int, user_id: int, reason: str = "") -> Optional[str]:
        return "Discord is not connected."

    async def ban(self, guild_id: int, user_id: int, reason: str = "", delete_message_days: int = 0) -> Optional[str]:
        return "Discord is not connected."

    async def unban(self, guild_id: int, user_id: int, reason: str = "") -> Optional[str]:
        return "Discord is not connected."

    async def dm(self, user_id: int, content: Optional[str] = None, embed: Optional[dict] = None) -> Optional[str]:
        return "Discord is not connected."

    async def send(self, guild_id: int, channel_id: int, content: Optional[str] = None, embed: Optional[dict] = None,
                   *, mention_roles: Optional[list[int]] = None) -> tuple[Optional[int], Optional[str]]:
        return None, "Discord is not connected."

    async def edit_message(self, guild_id: int, channel_id: int, message_id: int, content: Optional[str] = None,
                           embed: Optional[dict] = None) -> Optional[str]:
        return "Discord is not connected."

    def can_moderate(self, guild_id: int, moderator_id: int, target_id: int) -> Optional[str]:
        """Role-hierarchy check. Returns why the action is not allowed, or None."""
        return None

    def member(self, guild_id: int, user_id: int) -> Optional[dict]:
        return None

    def guild_info(self, guild_id: int) -> Optional[dict]:
        return None


class RecordingActions(DiscordActions):
    """Test double: records every call and succeeds (or fails for listed method names)."""

    available = True

    def __init__(self, fail: Optional[dict[str, str]] = None, members: Optional[dict] = None,
                 hierarchy_errors: Optional[dict[int, str]] = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail = fail or {}
        self.members = members or {}
        self.hierarchy_errors = hierarchy_errors or {}
        self._next_msg = 1000

    def _rec(self, name: str, *args: Any, **kw: Any) -> Optional[str]:
        self.calls.append((name, args, kw))
        return self.fail.get(name)

    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    async def add_role(self, guild_id, user_id, role_id, reason=""):
        return self._rec("add_role", guild_id, user_id, role_id)

    async def remove_role(self, guild_id, user_id, role_id, reason=""):
        return self._rec("remove_role", guild_id, user_id, role_id)

    async def set_nickname(self, guild_id, user_id, nickname, reason=""):
        return self._rec("set_nickname", guild_id, user_id, nickname)

    async def timeout(self, guild_id, user_id, seconds, reason=""):
        return self._rec("timeout", guild_id, user_id, seconds)

    async def remove_timeout(self, guild_id, user_id, reason=""):
        return self._rec("remove_timeout", guild_id, user_id)

    async def kick(self, guild_id, user_id, reason=""):
        return self._rec("kick", guild_id, user_id)

    async def ban(self, guild_id, user_id, reason="", delete_message_days=0):
        return self._rec("ban", guild_id, user_id)

    async def unban(self, guild_id, user_id, reason=""):
        return self._rec("unban", guild_id, user_id)

    async def dm(self, user_id, content=None, embed=None):
        return self._rec("dm", user_id, content=content, embed=embed)

    async def send(self, guild_id, channel_id, content=None, embed=None, *, mention_roles=None):
        err = self._rec("send", guild_id, channel_id, content=content, embed=embed)
        if err:
            return None, err
        self._next_msg += 1
        return self._next_msg, None

    async def edit_message(self, guild_id, channel_id, message_id, content=None, embed=None):
        return self._rec("edit_message", guild_id, channel_id, message_id)

    def can_moderate(self, guild_id, moderator_id, target_id):
        return self.hierarchy_errors.get(target_id)

    def member(self, guild_id, user_id):
        return self.members.get(user_id)

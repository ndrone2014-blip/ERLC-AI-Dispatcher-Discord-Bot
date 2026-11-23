"""Configurable, role-based permission system.

Permission groups are mapped to Discord roles by administrators (Administration
panel -> Permissions). Each permission lists the groups allowed to use it; those
lists can be overridden per guild.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from database.connection import Database

GROUPS = ["owner", "admin", "supervisor", "dispatcher", "officer", "fire_ems", "citizen"]
GROUP_LABEL = {
    "owner": "Server Owner",
    "admin": "Administrator",
    "supervisor": "Dispatch Supervisor",
    "dispatcher": "Dispatcher",
    "officer": "Officer",
    "fire_ems": "Fire/EMS Personnel",
    "citizen": "Citizen",
}

STAFF = {"owner", "admin", "supervisor", "dispatcher"}
RESPONDERS = STAFF | {"officer", "fire_ems"}
SUPERVISORS = {"owner", "admin", "supervisor"}
ADMINS = {"owner", "admin"}
EVERYONE = set(GROUPS)

DEFAULT_PERMISSIONS: dict[str, set[str]] = {
    # citizen services
    "call.submit": EVERYONE,
    "report.submit": EVERYONE,
    "report.view_own": EVERYONE,
    # responders
    "unit.self_status": RESPONDERS,
    "unit.register": RESPONDERS,
    "incident.view": RESPONDERS,
    "incident.attach_self": RESPONDERS,
    "incident.note": RESPONDERS,
    "incident.backup": RESPONDERS,
    "incident.manual_create": RESPONDERS,
    "bolo.view": RESPONDERS,
    "voice.command": RESPONDERS,
    # dispatchers
    "incident.attach_other": STAFF,
    "incident.update": STAFF,
    "incident.close": STAFF,
    "unit.manage_other": STAFF,
    "bolo.manage": STAFF | {"officer"},
    "report.view_all": STAFF,
    "report.manage": STAFF,
    "voice.control": STAFF,
    "announcement.skip_routine": SUPERVISORS,
    # supervisors
    "incident.priority_approve": SUPERVISORS,
    "incident.review_unverified": SUPERVISORS,
    "report.supervisor": SUPERVISORS,
    "stats.view": RESPONDERS,
    # administrators
    "admin.config": ADMINS,
    "admin.erlc": ADMINS,
    "admin.ai": ADMINS,
    "admin.permissions": {"owner", "admin"},
    "admin.backup": ADMINS,
    "admin.test": ADMINS | {"supervisor"},
}

PERMISSION_DESCRIPTIONS = {
    "call.submit": "Submit 911 calls in Discord",
    "report.submit": "File police reports",
    "unit.self_status": "Change own unit status",
    "incident.attach_self": "Attach self to incidents",
    "incident.attach_other": "Attach/detach other units",
    "incident.update": "Update incident details",
    "incident.close": "Close incidents",
    "incident.priority_approve": "Approve/override priorities",
    "report.view_all": "View private police reports",
    "report.manage": "Manage police reports",
    "bolo.manage": "Create/update/clear BOLOs",
    "voice.control": "Control the dispatcher voice",
    "admin.config": "Configure the system",
    "admin.erlc": "Configure the ER:LC API",
    "admin.ai": "Configure AI providers",
}


@dataclass
class Actor:
    """Platform-independent description of who is acting."""

    user_id: int
    role_ids: set[int]
    is_owner: bool = False
    is_discord_admin: bool = False

    @classmethod
    def from_member(cls, member) -> "Actor":
        guild = getattr(member, "guild", None)
        roles = {r.id for r in getattr(member, "roles", [])}
        perms = getattr(member, "guild_permissions", None)
        return cls(
            user_id=member.id,
            role_ids=roles,
            is_owner=bool(guild and guild.owner_id == member.id),
            is_discord_admin=bool(perms and (perms.administrator or perms.manage_guild)),
        )


class PermissionManager:
    def __init__(self, db: Database):
        self.db = db

    async def role_map(self, guild_id: int) -> dict[str, list[int]]:
        return await self.db.get_config(guild_id, "perm_roles", {}) or {}

    async def set_group_roles(self, guild_id: int, group: str, role_ids: Iterable[int]) -> None:
        if group not in GROUPS:
            raise ValueError(f"Unknown permission group {group}")
        mapping = await self.role_map(guild_id)
        mapping[group] = sorted({int(r) for r in role_ids})
        await self.db.set_config(guild_id, "perm_roles", mapping)

    async def overrides(self, guild_id: int) -> dict[str, list[str]]:
        return await self.db.get_config(guild_id, "perm_overrides", {}) or {}

    async def set_override(self, guild_id: int, permission: str, groups: Optional[Iterable[str]]) -> None:
        if permission not in DEFAULT_PERMISSIONS:
            raise ValueError(f"Unknown permission {permission}")
        ov = await self.overrides(guild_id)
        if groups is None:
            ov.pop(permission, None)
        else:
            ov[permission] = [g for g in groups if g in GROUPS]
        await self.db.set_config(guild_id, "perm_overrides", ov)

    async def groups_for(self, guild_id: int, actor: Actor) -> set[str]:
        groups = {"citizen"}
        if actor.is_owner:
            groups |= {"owner", "admin"}
        if actor.is_discord_admin:
            groups.add("admin")
        mapping = await self.role_map(guild_id)
        for group, roles in mapping.items():
            if actor.role_ids & set(roles):
                groups.add(group)
        return groups

    async def allowed_groups(self, guild_id: int, permission: str) -> set[str]:
        ov = await self.overrides(guild_id)
        if permission in ov:
            return set(ov[permission]) | {"owner"}
        return set(DEFAULT_PERMISSIONS.get(permission, ADMINS)) | {"owner"}

    async def has(self, guild_id: int, actor: Actor, permission: str) -> bool:
        groups = await self.groups_for(guild_id, actor)
        if "owner" in groups:
            return True
        allowed = await self.allowed_groups(guild_id, permission)
        # Administrators may always manage permissions so they cannot lock themselves out.
        if permission == "admin.permissions" and "admin" in groups:
            return True
        return bool(groups & allowed)

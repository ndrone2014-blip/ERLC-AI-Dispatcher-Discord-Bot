"""Embed builder: validated, reusable embed templates with safe {placeholders}.

Templates are stored as Discord embed JSON (the same shape discord.Embed.from_dict accepts)
plus an optional list of link buttons. Discord's limits are enforced on save, so a template
that saves will always send.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional

from database.models import dumps

from .common import ServiceError, ValidationError, clean_text, iso_now, loads, safe_format

if TYPE_CHECKING:
    from .platform import CommunityPlatform

_URL = re.compile(r"^https?://[^\s<>\"']{3,500}$", re.I)
_HEX = re.compile(r"^#?([0-9a-f]{6})$", re.I)

PLACEHOLDERS = {
    "server": "Server name", "member_count": "Member count", "user": "Member mention", "user_name": "Member username",
    "user_id": "Member ID", "roblox": "Verified Roblox username", "date": "Today's date (UTC)",
}

PRESETS: dict[str, dict] = {
    "Welcome": {"title": "Welcome to {server}!", "color": 0x3CC8F0,
                "description": "Hey {user}, welcome aboard! You are member **#{member_count}**.\n\n"
                               "1. Read the rules\n2. Verify your Roblox account with `/verify`\n3. Apply to a department with `/apply`",
                "footer": {"text": "Hive Dispatch | ERLC"}},
    "Announcement": {"title": "📣 Announcement", "color": 0x3CC8F0, "description": "Write your announcement here.",
                     "footer": {"text": "{server} • Management"}, "timestamp": True},
    "Verification": {"title": "✅ Verify your Roblox account", "color": 0x2EA043,
                     "description": "Link your Roblox account to unlock the server.\n\nClick **Verify** below or run `/verify` "
                                    "and follow the steps. It takes about a minute.",
                     "footer": {"text": "Hive Dispatch | ERLC • Roblox Verification"}},
    "Applications": {"title": "📝 Applications are open", "color": 0x8957E5,
                     "description": "Want to join a department? Run `/apply` and pick a form.\n"
                                    "Reviewers will contact you once your application has been reviewed.",
                     "footer": {"text": "Hive Dispatch | ERLC • Applications"}},
    "Dispatch Incident": {"title": "🚨 Incident Notice", "color": 0xDA3633,
                          "description": "Describe the incident here.",
                          "fields": [{"name": "Location", "value": "—", "inline": True},
                                     {"name": "Priority", "value": "—", "inline": True}],
                          "footer": {"text": "Hive Dispatch | ERLC • Dispatch"}, "timestamp": True},
    "Moderation Log": {"title": "🛡️ Moderation Notice", "color": 0xD29922,
                       "description": "Describe the moderation action here.",
                       "footer": {"text": "Hive Dispatch | ERLC • Moderation"}, "timestamp": True},
    "Staff Notification": {"title": "👥 Staff Notice", "color": 0x6E7681, "description": "Message for the staff team.",
                           "footer": {"text": "Hive Dispatch | ERLC • Staff"}, "timestamp": True},
    "Session Announcement": {"title": "🎮 Session Starting", "color": 0x2EA043,
                             "description": "A roleplay session is starting soon. Join the server and report for duty!",
                             "fields": [{"name": "Host", "value": "—", "inline": True},
                                        {"name": "Start", "value": "—", "inline": True}],
                             "footer": {"text": "Hive Dispatch | ERLC • Sessions"}, "timestamp": True},
}


def parse_colour(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        if 0 <= value <= 0xFFFFFF:
            return value
        raise ValidationError("Colour must be between #000000 and #FFFFFF.")
    m = _HEX.match(str(value).strip())
    if not m:
        raise ValidationError("Colour must be a hex value like #3CC8F0.")
    return int(m.group(1), 16)


def _url(value: Any, field: str) -> Optional[str]:
    v = str(value or "").strip()
    if not v:
        return None
    if not _URL.match(v):
        raise ValidationError(f"{field} must be a full http(s) URL.")
    return v


def validate_embed(data: dict) -> dict:
    """Return a clean embed dict (Discord JSON) or raise ValidationError."""
    if not isinstance(data, dict):
        raise ValidationError("Embed data must be an object.")
    out: dict[str, Any] = {}
    title = clean_text(data.get("title"), 256)
    desc = clean_text(data.get("description"), 4096)
    if title:
        out["title"] = title
    if desc:
        out["description"] = desc
    if (u := _url(data.get("url"), "Title link")):
        out["url"] = u
    colour = parse_colour(data.get("color", data.get("colour")))
    if colour is not None:
        out["color"] = colour
    footer = data.get("footer") or {}
    if isinstance(footer, str):
        footer = {"text": footer}
    if clean_text(footer.get("text"), 2048):
        out["footer"] = {"text": clean_text(footer.get("text"), 2048)}
    author = data.get("author") or {}
    if isinstance(author, str):
        author = {"name": author}
    if clean_text(author.get("name"), 256):
        out["author"] = {"name": clean_text(author.get("name"), 256)}
        if (u := _url(author.get("icon_url"), "Author icon")):
            out["author"]["icon_url"] = u
    for key, label in (("image", "Image"), ("thumbnail", "Thumbnail")):
        v = data.get(key)
        v = v.get("url") if isinstance(v, dict) else v
        if (u := _url(v, label)):
            out[key] = {"url": u}
    fields = data.get("fields") or []
    if not isinstance(fields, list) or len(fields) > 25:
        raise ValidationError("An embed can have at most 25 fields.")
    clean_fields = []
    for f in fields:
        name, value = clean_text((f or {}).get("name"), 256), clean_text((f or {}).get("value"), 1024)
        if not name or not value:
            raise ValidationError("Every field needs a name and a value.")
        clean_fields.append({"name": name, "value": value, "inline": bool(f.get("inline"))})
    if clean_fields:
        out["fields"] = clean_fields
    if data.get("timestamp"):
        out["timestamp"] = True
    buttons = data.get("buttons") or []
    if not isinstance(buttons, list) or len(buttons) > 5:
        raise ValidationError("At most 5 link buttons.")
    clean_buttons = []
    for b in buttons:
        label = clean_text((b or {}).get("label"), 80)
        url = _url((b or {}).get("url"), "Button link")
        if not label or not url:
            raise ValidationError("Every button needs a label and a full http(s) link.")
        clean_buttons.append({"label": label, "url": url})
    if clean_buttons:
        out["buttons"] = clean_buttons
    if not any(k in out for k in ("title", "description", "fields", "image", "author")):
        raise ValidationError("The embed is empty: add at least a title or a description.")
    total = sum(len(str(out.get(k, ""))) for k in ("title", "description")) + len(out.get("footer", {}).get("text", "")) \
        + len(out.get("author", {}).get("name", "")) + sum(len(f["name"]) + len(f["value"]) for f in clean_fields)
    if total > 6000:
        raise ValidationError("The embed is longer than Discord's 6000 character limit.")
    return out


def render(data: dict, values: dict[str, Any]) -> dict:
    """Fill placeholders in every text part of a validated embed. Returns a new dict."""
    def f(s: Optional[str], n: int) -> Optional[str]:
        return safe_format(s, values, n) if s else s
    out = dict(data)
    for k, n in (("title", 256), ("description", 4096)):
        if out.get(k):
            out[k] = f(out[k], n)
    if out.get("footer"):
        out["footer"] = {**out["footer"], "text": f(out["footer"].get("text"), 2048)}
    if out.get("author"):
        out["author"] = {**out["author"], "name": f(out["author"].get("name"), 256)}
    if out.get("fields"):
        out["fields"] = [{**x, "name": f(x["name"], 256), "value": f(x["value"], 1024)} for x in out["fields"]]
    if out.pop("timestamp", None):
        out["timestamp"] = iso_now()
    return out


class EmbedService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    async def list(self, guild_id: int) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM embed_templates WHERE guild_id = ? ORDER BY name", (guild_id,))
        return [{**dict(r), "data": loads(r["data"], {})} for r in rows]

    async def get(self, guild_id: int, template_id: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM embed_templates WHERE guild_id = ? AND id = ?", (guild_id, int(template_id)))
        return {**dict(r), "data": loads(r["data"], {})} if r else None

    async def by_name(self, guild_id: int, name: str) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM embed_templates WHERE guild_id = ? AND lower(name) = lower(?)", (guild_id, name.strip()))
        return {**dict(r), "data": loads(r["data"], {})} if r else None

    async def save(self, guild_id: int, name: str, data: dict, actor_id: int, template_id: Optional[int] = None) -> dict:
        name = clean_text(name, 60, required=True, field="Template name")
        clean = validate_embed(data)
        existing = await self.by_name(guild_id, name)
        if existing and existing["id"] != template_id:
            raise ServiceError(f"A template named '{name}' already exists.")
        if template_id:
            if not await self.get(guild_id, template_id):
                raise ServiceError("Template not found.")
            await self.db.execute("UPDATE embed_templates SET name = ?, data = ?, updated_at = ? WHERE id = ? AND guild_id = ?",
                                  (name, dumps(clean), iso_now(), int(template_id), guild_id))
            tid = int(template_id)
        else:
            tid = await self.db.execute("INSERT INTO embed_templates(guild_id, name, data, created_by) VALUES (?, ?, ?, ?)",
                                        (guild_id, name, dumps(clean), actor_id))
        await self.p.audit.record(guild_id, actor_id, "embed.save", name)
        return await self.get(guild_id, tid)

    async def delete(self, guild_id: int, template_id: int, actor_id: int) -> bool:
        t = await self.get(guild_id, template_id)
        if not t:
            return False
        await self.db.execute("DELETE FROM embed_templates WHERE id = ? AND guild_id = ?", (int(template_id), guild_id))
        await self.p.audit.record(guild_id, actor_id, "embed.delete", t["name"])
        return True

    async def create_from_preset(self, guild_id: int, preset: str, actor_id: int) -> dict:
        if preset not in PRESETS:
            raise ServiceError("Unknown preset.")
        name, n = preset, 2
        while await self.by_name(guild_id, name):
            name, n = f"{preset} {n}", n + 1
        return await self.save(guild_id, name, PRESETS[preset], actor_id)

    async def values(self, guild_id: int, user_id: Optional[int] = None) -> dict[str, Any]:
        info = self.p.actions.guild_info(guild_id) or {}
        vals: dict[str, Any] = {"server": info.get("name") or "the server", "member_count": info.get("member_count") or "—",
                                "date": iso_now()[:10]}
        if user_id:
            m = self.p.actions.member(guild_id, user_id) or {}
            vals.update(user=f"<@{user_id}>", user_id=user_id, user_name=m.get("name") or str(user_id))
            link = await self.db.fetchone("SELECT roblox_username FROM roblox_links WHERE guild_id = ? AND discord_id = ?",
                                          (guild_id, user_id))
            vals["roblox"] = link["roblox_username"] if link else "not verified"
        return vals

    async def send(self, guild_id: int, channel_id: int, *, actor_id: Optional[int], template_id: Optional[int] = None,
                   data: Optional[dict] = None, content: Optional[str] = None, user_id: Optional[int] = None) -> int:
        if template_id:
            t = await self.get(guild_id, template_id)
            if not t:
                raise ServiceError("Template not found.")
            data = t["data"]
        embed = render(validate_embed(data), await self.values(guild_id, user_id)) if data else None
        text = safe_format(clean_text(content, 2000), await self.values(guild_id, user_id)) if content else None
        if not embed and not text:
            raise ValidationError("Nothing to send: add a message or choose a template.")
        msg_id, err = await self.p.actions.send(guild_id, int(channel_id), content=text, embed=embed)
        if err:
            raise ServiceError(f"Could not send the message: {err}")
        await self.p.audit.record(guild_id, actor_id, "announcement.send", f"channel:{channel_id}",
                                  {"template_id": template_id} if template_id else None)
        return msg_id

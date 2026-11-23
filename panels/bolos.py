"""Panel 7: BOLO Management."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

import discord

from bot.ui import FOOTER, WARN, bot_of, reply, require, truncate
from database.models import discord_ts
from security.validation import ValidationError

if TYPE_CHECKING:
    from bot.client import DispatchBot

KIND_EMOJI = {"person": "🧍", "vehicle": "🚗", "other": "📌"}


async def build(bot: "DispatchBot", guild_id: int):
    bolos = await bot.core.bolos.active(guild_id)
    e = discord.Embed(title="📢 Active BOLOs", colour=WARN)
    for b in bolos[:12]:
        value = truncate(b.get("description") or "No description", 300)
        if b.get("plate"):
            value = f"Plate **{b['plate']}** · " + value
        if b.get("last_seen"):
            value += f"\nLast seen: {truncate(b['last_seen'], 100)}"
        if b.get("reason"):
            value += f"\nReason: {truncate(b['reason'], 150)}"
        value += f"\nIssued {discord_ts(b['created_at'])} by <@{b['created_by']}>"
        e.add_field(name=f"{KIND_EMOJI.get(b['kind'], '📌')} BOLO #{b['number']} — {truncate(b['subject'], 80)}", value=value, inline=False)
    if not bolos:
        e.description = "No active BOLOs."
    e.set_footer(text=FOOTER)
    return [e], BoloPanelView(bolos)


class BoloPanelView(discord.ui.View):
    def __init__(self, bolos: Optional[list[dict]] = None):
        super().__init__(timeout=None)
        opts = [discord.SelectOption(label=truncate(f"#{b['number']} {b['subject']}", 95), value=str(b["id"]),
                                     emoji=KIND_EMOJI.get(b["kind"])) for b in (bolos or [])[:25]]
        self.select.options = opts or [discord.SelectOption(label="No active BOLOs", value="none")]
        self.select.disabled = not opts

    @discord.ui.select(custom_id="cad:bolo:select", placeholder="Select a BOLO to update or clear…", row=0)
    async def select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await require(interaction, "bolo.view"):
            return
        if select.values[0] == "none":
            return await reply(interaction, "No active BOLOs.")
        b = await bot_of(interaction).core.bolos.get(int(select.values[0]))
        if b is None or b["guild_id"] != interaction.guild_id:
            return await reply(interaction, "BOLO not found.", error=True)
        view = discord.ui.View(timeout=None)
        for a in ("update", "clear", "history"):
            view.add_item(BoloAction(a, b["id"]))
        await reply(interaction, f"**BOLO #{b['number']}** — {b['subject']}", view=view)

    @discord.ui.button(label="Create BOLO", emoji="➕", style=discord.ButtonStyle.primary, custom_id="cad:bolo:create", row=1)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await require(interaction, "bolo.manage"):
            await interaction.response.send_modal(BoloModal())

    @discord.ui.button(label="BOLO History", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="cad:bolo:history", row=1)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "bolo.view"):
            return
        rows = await bot_of(interaction).core.bolos.recent_all(interaction.guild_id, 20)
        lines = [f"#{b['number']} {KIND_EMOJI.get(b['kind'], '')} {truncate(b['subject'], 60)} · {b['status']} · {discord_ts(b['created_at'])}"
                 for b in rows]
        await reply(interaction, embed=discord.Embed(title="📜 BOLO history", description="\n".join(lines) or "None", colour=WARN))


class BoloAction(discord.ui.DynamicItem[discord.ui.Button], template=r"cad:b:(?P<action>[a-z]+):(?P<id>\d+)"):
    LABELS = {"update": ("Update", "✏️", discord.ButtonStyle.secondary), "clear": ("Clear BOLO", "✅", discord.ButtonStyle.success),
              "history": ("History", "📜", discord.ButtonStyle.secondary)}

    def __init__(self, action: str, bolo_id: int):
        label, emoji, style = self.LABELS[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"cad:b:{action}:{bolo_id}"))
        self.action = action
        self.bolo_id = bolo_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        b = await bot.core.bolos.get(self.bolo_id)
        if b is None or b["guild_id"] != interaction.guild_id:
            return await reply(interaction, "BOLO not found.", error=True)
        if self.action == "history":
            if not await require(interaction, "bolo.view"):
                return
            hist = await bot.core.bolos.history(b["id"])
            return await reply(interaction, "\n".join(f"{discord_ts(h['at'], 't')} **{h['event_type']}** by <@{h['actor_id']}>"
                                                      for h in hist) or "No history.")
        if not await require(interaction, "bolo.manage"):
            return
        if self.action == "update":
            return await interaction.response.send_modal(BoloModal(b))
        if b["status"] != "active":
            return await reply(interaction, "This BOLO is already cleared.")
        await bot.core.bolos.clear(b["id"], interaction.user.id)
        await reply(interaction, f"✅ BOLO #{b['number']} cleared.")


class BoloModal(discord.ui.Modal):
    def __init__(self, bolo: Optional[dict] = None):
        super().__init__(title="Update BOLO" if bolo else "Create BOLO")
        self.bolo = bolo
        self.kind = discord.ui.TextInput(label="Type: person / vehicle / other", max_length=10,
                                         default=bolo["kind"] if bolo else "vehicle")
        self.subject = discord.ui.TextInput(label="Subject (who / what)", max_length=100, default=bolo["subject"] if bolo else None)
        self.description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, max_length=800, required=False,
                                                default=bolo.get("description") if bolo else None)
        self.plate_seen = discord.ui.TextInput(label="Plate | last seen (e.g. ABC123 | Park St)", max_length=180, required=False,
                                               default=(f"{bolo.get('plate') or ''} | {bolo.get('last_seen') or ''}" if bolo else None))
        self.reason = discord.ui.TextInput(label="Reason", max_length=300, required=False, default=bolo.get("reason") if bolo else None)
        for i in (self.kind, self.subject, self.description, self.plate_seen, self.reason):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        plate, _, seen = (self.plate_seen.value or "").partition("|")
        try:
            if self.bolo:
                b = await bot.core.bolos.update(self.bolo["id"], interaction.user.id, subject=self.subject.value,
                                                description=self.description.value, plate=plate.strip(), last_seen=seen.strip(),
                                                reason=self.reason.value)
                return await reply(interaction, f"✏️ BOLO #{b['number']} updated.")
            b = await bot.core.bolos.create(interaction.guild_id, interaction.user.id, kind=self.kind.value.strip().lower(),
                                            subject=self.subject.value, description=self.description.value, plate=plate.strip(),
                                            last_seen=seen.strip(), reason=self.reason.value)
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        await reply(interaction, f"📢 BOLO #{b['number']} issued.")

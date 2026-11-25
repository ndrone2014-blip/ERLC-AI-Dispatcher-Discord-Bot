"""/setup-guide — step-by-step setup guide: optional dashboards, ER:LC API key, AI keys, roles, units, logging."""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.ui import has_perm, reply

if TYPE_CHECKING:
    from bot.client import DispatchBot

# page key -> (menu label, emoji, title, [(field name, field text)])
PAGES: dict[str, tuple[str, str, str, list[tuple[str, str]]]] = {
    "start": ("Start here", "🧭", "Pulse Dispatch Setup Guide", [
        ("What you'll do", "1. Choose your dashboards (optional)\n2. Connect your ER:LC server (API key)\n"
                           "3. Map your Discord roles\n4. Add a free AI key (optional)\n5. Register units\n6. Choose what is logged"),
        ("How to use this guide", "Pick a step from the menu below. The buttons open the right form for you "
                                  "(administrators only)."),
        ("Good to know", "Dispatch works without any dashboards and without AI. In-game 911 calls need the ER:LC API key."),
    ]),
    "dashboards": ("Dashboards (optional)", "🖥️", "Step 1 · Dashboards (optional)", [
        ("Choose a preset", "Run `/setup run` or press **Choose Setup Preset** below:\n"
                            "📡 **Dispatch Only** — `#dispatch` channel + 📻 radio, no panels\n"
                            "📋 **Dispatch + Logs** — adds `#dispatch-log` with live incident cards\n"
                            "🖥️ **Full Dispatch System** — all 11 panels and channels\n"
                            "🧩 **Custom** — pick exactly which panels you want"),
        ("Change it later", "`/dashboard status` — what's on\n`/dashboard enable` — add one panel (posts in a channel you choose)\n"
                            "`/dashboard disable` — remove one panel\n`/dashboard preset` — switch preset"),
        ("Nothing breaks without panels", "Every panel's job can also be done with commands, and logging always works "
                                          "(`/logs view`)."),
    ]),
    "erlc": ("ER:LC API key", "🎮", "Step 2 · Connect your ER:LC server", [
        ("1. Get the API pack (once)", "In ER:LC: **Menu** (top right) → **Servers** → **Owned Servers** → your private server → "
                                       "**Upgrade Packs** → **API Pack**. Only the server owner can buy it."),
        ("2. Copy your server key", "Join your private server → **Server Info** → **Edit Server Settings** → scroll to "
                                    "**ER:LC API** → **Edit** → copy the **Server Key**. Treat it like a password."),
        ("3. Give it to Pulse Dispatch", "Press **Set ER:LC API Key** below (or run `/settings erlc-key`) and paste it. "
                                         "The key is tested before it is saved and stored encrypted.\n"
                                         "Bot owners can also put `ERLC_SERVER_KEY=` in `.env`."),
        ("4. Check it", "`/erlc` should show 🟢. Make an in-game 911 call — it appears as an **API-VERIFIED** incident."),
        ("Errors", "`2002` wrong/expired key → copy it again · `3002` server offline (no players) · "
                   "`4001` rate limited → the bot waits automatically.\nKey leaked? Regenerate it in-game and set it again."),
    ]),
    "roles": ("Roles & permissions", "🔐", "Step 3 · Roles & permissions", [
        ("Map your roles", "`/permissions map group:<group> role:<your role>` for each group:\n"
                           "**Administrator** · **Dispatch Supervisor** · **Dispatcher** · **Officer** · **Fire/EMS**"),
        ("Who is what", "Members with Discord *Administrator* or *Manage Server* are always admins. Everyone else is a "
                        "*Citizen* (can report emergencies)."),
        ("Fine-tune", "`/permissions view` — current mapping\n`/permissions set` — change who can use one permission"),
    ]),
    "ai": ("AI keys (optional)", "🧠", "Step 4 · Free AI (optional)", [
        ("Do I need AI?", "No. Pulse Dispatch understands plain language on its own. AI adds deeper understanding and "
                          "assessments."),
        ("Free providers", "Groq · Cerebras · Google Gemini · OpenRouter · Mistral · SambaNova · NVIDIA · GitHub Models · "
                           "Cloudflare\n`/ai-provider list` — links and limits\n`/ai-provider guide` — step-by-step for one"),
        ("Add a key", "`/ai-provider key` (private form, stored encrypted for this server) — or the bot owner puts "
                      "`GROQ_API_KEY=` etc. in `.env` and restarts."),
        ("Check it", "`/ai-test` → 🟢 / 🔴 per provider · `/ai-status` → which one is active · `/ai-failover` → try order"),
    ]),
    "units": ("Units & dispatch", "🚓", "Step 5 · Units & dispatch", [
        ("Officers", "`/unit-register` (name, callsign, department) → `/unit-status 10-8` to go on duty."),
        ("Talking to dispatch", "Type in `#dispatch` or use `/dispatch ask`: *“crash at the bank, need EMS”*, "
                                "*“show me 10-8”*, *“what calls are active?”*. In 📻 radio say *“Dispatch, …”* "
                                "(`/voice join`)."),
        ("Customize", "`/departments add` · `/departments status-add` · `/settings response-style` · "
                      "`/settings agency-name` · `/settings category-add`"),
        ("Try it", "`/test call` creates a clearly labelled **TEST DATA** call through the whole pipeline."),
    ]),
    "logs": ("Logging", "📋", "Step 6 · Logging", [
        ("Where logs go", "`/logs channel` — use an existing channel\n`/logs create` — make a private `#pulse-logs`\n"
                          "`/logs route` — send one event type to its own channel"),
        ("What is logged", "`/logs settings` — tick the events you want (911 calls, assignments, status changes, "
                           "priority changes, BOLOs, closures, AI issues, ER:LC connection, admin changes…)"),
        ("History", "`/logs view` shows past records even if no log channel is set."),
    ]),
}
ORDER = list(PAGES)


def page_embed(key: str, look: E.Look) -> discord.Embed:
    label, emoji, title, fields = PAGES[key]
    e = E.base(f"{emoji} {title}", look=look,
               description=f"Step {ORDER.index(key)} of {len(ORDER) - 1}" if key != "start" else None)
    for name, value in fields:
        e.add_field(name=name, value=value, inline=False)
    return e


class GuideView(discord.ui.View):
    def __init__(self, user_id: int, look: E.Look, page: str = "start"):
        super().__init__(timeout=600)
        self.user_id, self.look, self.page = user_id, look, page
        sel = discord.ui.Select(placeholder="Jump to a step…", options=[
            discord.SelectOption(label=label, value=k, emoji=emoji, default=(k == page))
            for k, (label, emoji, _, _) in PAGES.items()])
        sel.callback = self._jump
        self.sel = sel
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Run `/setup-guide` to open your own guide.", ephemeral=True)
            return False
        return True

    async def _show(self, interaction: discord.Interaction, page: str):
        await interaction.response.edit_message(embed=page_embed(page, self.look), view=GuideView(self.user_id, self.look, page))

    async def _jump(self, interaction: discord.Interaction):
        await self._show(interaction, self.sel.values[0])

    @discord.ui.button(label="Back", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, ORDER[max(0, ORDER.index(self.page) - 1)])

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.primary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, ORDER[min(len(ORDER) - 1, ORDER.index(self.page) + 1)])

    @discord.ui.button(label="Set ER:LC API Key", emoji="🔑", style=discord.ButtonStyle.success, row=2)
    async def erlc_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await has_perm(interaction, "admin.erlc"):
            return await reply(interaction, "⛔ Only administrators can set the ER:LC API key.", error=True)
        from panels.erlc import KeyModal
        await interaction.response.send_modal(KeyModal())

    @discord.ui.button(label="Choose Setup Preset", emoji="🖥️", style=discord.ButtonStyle.secondary, row=2)
    async def preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await has_perm(interaction, "admin.config"):
            return await reply(interaction, "⛔ Only administrators can change the setup.", error=True)
        from bot.commands.setup import PresetPicker, _preset_embed
        await reply(interaction, embed=_preset_embed(), view=PresetPicker(interaction.client.get_cog("SetupCog"), True))


class PulseGuideCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    @app_commands.command(name="setup-guide", description="Step-by-step setup: dashboards (optional), ER:LC API key, AI, roles, logs")
    @app_commands.guild_only()
    @app_commands.choices(step=[app_commands.Choice(name=label, value=k) for k, (label, _, _, _) in PAGES.items()])
    async def setup_guide(self, interaction: discord.Interaction, step: Optional[app_commands.Choice[str]] = None):
        look = await E.look_for(self.bot, interaction.guild_id)
        page = step.value if step else "start"
        await reply(interaction, embed=page_embed(page, look), view=GuideView(interaction.user.id, look, page))


async def setup(bot: "DispatchBot"):
    await bot.add_cog(PulseGuideCog(bot))

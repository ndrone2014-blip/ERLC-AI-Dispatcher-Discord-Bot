"""Panel 5: Citizen Services (public)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.ui import BRAND, FOOTER, bot_of, rate_limited, reply, require
from dispatch.reports import REPORT_STATUSES
from security.validation import ValidationError, clean_text, digest, is_duplicate_submission

if TYPE_CHECKING:
    from bot.client import DispatchBot

DISCLAIMER = ("⚠️ **Roleplay only.** This is part of an ER:LC roleplay community on Discord. "
              "It is **not** a real emergency service. In a real emergency, contact your local emergency number.")


async def build(bot: "DispatchBot", guild_id: int):
    e = discord.Embed(title="🏛️ Citizen Services", colour=BRAND, description=DISCLAIMER)
    e.add_field(name="🚨 Submit 911 call", value="Report an in-character emergency to dispatch.", inline=False)
    e.add_field(name="📄 File police report", value="Reports are private and only visible to authorised personnel.", inline=False)
    e.add_field(name="🏢 Contact department", value="Send a message to a department.", inline=False)
    e.add_field(name="📨 Non-emergency request", value="Non-urgent requests (no voice broadcast).", inline=False)
    e.add_field(name="🔎 Check report status", value="Look up a report you filed by its number.", inline=False)
    e.set_footer(text=FOOTER)
    return [e], CitizenView()


class CitizenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Submit 911 Call", emoji="🚨", style=discord.ButtonStyle.danger, custom_id="cad:cit:911")
    async def call(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await require(interaction, "call.submit") and not await rate_limited(interaction, "911"):
            await interaction.response.send_modal(Call911Modal())

    @discord.ui.button(label="File Police Report", emoji="📄", style=discord.ButtonStyle.primary, custom_id="cad:cit:report")
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await require(interaction, "report.submit"):
            await interaction.response.send_modal(ReportModal())

    @discord.ui.button(label="Contact Department", emoji="🏢", style=discord.ButtonStyle.secondary, custom_id="cad:cit:contact")
    async def contact(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await require(interaction, "call.submit"):
            deps = await bot_of(interaction).core.units.departments(interaction.guild_id)
            await interaction.response.send_modal(ContactModal(deps, non_emergency=False))

    @discord.ui.button(label="Non-Emergency Request", emoji="📨", style=discord.ButtonStyle.secondary, custom_id="cad:cit:nonemerg")
    async def nonemerg(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await require(interaction, "call.submit"):
            deps = await bot_of(interaction).core.units.departments(interaction.guild_id)
            await interaction.response.send_modal(ContactModal(deps, non_emergency=True))

    @discord.ui.button(label="Check Report Status", emoji="🔎", style=discord.ButtonStyle.secondary, custom_id="cad:cit:status")
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReportStatusModal())


class Call911Modal(discord.ui.Modal, title="911 Call (roleplay)"):
    caller = discord.ui.TextInput(label="Your RP name (leave blank = anonymous)", required=False, max_length=50)
    location = discord.ui.TextInput(label="Location (street / postal / landmark)", max_length=150)
    category = discord.ui.TextInput(label="Emergency type (e.g. crash, robbery, fire)", max_length=60)
    description = discord.ui.TextInput(label="What is happening?", style=discord.TextStyle.paragraph, max_length=1000)
    extra = discord.ui.TextInput(label="Department needed / additional info", required=False, max_length=300,
                                 style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        try:
            loc = clean_text(self.location.value, 150, required=True, field="Location")
            desc = clean_text(self.description.value, 1000, required=True, field="Description")
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        if await is_duplicate_submission(bot.db, interaction.guild_id, interaction.user.id, "911", digest(loc, desc)):
            return await reply(interaction, "This call was already submitted.", error=True)
        extra = clean_text(self.extra.value, 300)
        requested = ""
        for d in await bot.core.units.departments(interaction.guild_id):
            if d["short_code"].lower() in extra.lower().split() or d["name"].lower() in extra.lower():
                requested = d["short_code"]
                break
        await interaction.response.defer(ephemeral=True, thinking=True)
        inc = await bot.core.create_discord_call(
            interaction.guild_id, interaction.user.id, caller=clean_text(self.caller.value, 50) or "Anonymous", location=loc,
            category=clean_text(self.category.value, 60), description=desc, requested_department=requested, extra=extra)
        await interaction.followup.send(f"🚨 Your 911 call was received as incident **#{inc['number']}**. "
                                        f"It is marked *unverified* because it was submitted through Discord, not in-game.",
                                        ephemeral=True)


class ReportModal(discord.ui.Modal, title="Police report (private)"):
    rp_name = discord.ui.TextInput(label="Your name / RP name", max_length=60)
    location = discord.ui.TextInput(label="Location", max_length=150)
    when = discord.ui.TextInput(label="Category + date/time (e.g. Theft, today 8pm)", max_length=100)
    description = discord.ui.TextInput(label="What happened?", style=discord.TextStyle.paragraph, max_length=2000)
    people = discord.ui.TextInput(label="Suspects / witnesses (if any)", style=discord.TextStyle.paragraph, required=False,
                                  max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        cat, _, when = self.when.value.partition(",")
        try:
            desc = clean_text(self.description.value, 2000, required=True, field="Description")
        except ValidationError as e:
            return await reply(interaction, f"⚠️ {e}", error=True)
        if await is_duplicate_submission(bot.db, interaction.guild_id, interaction.user.id, "report", digest(desc)):
            return await reply(interaction, "This report was already submitted.", error=True)
        people = clean_text(self.people.value, 1000)
        suspects, witnesses = people, ""
        low = people.lower()
        if "witness" in low:
            idx = low.index("witness")
            suspects, witnesses = people[:idx].strip(" ,;"), people[idx:].strip()
        rep = await bot.core.reports.create(
            interaction.guild_id, interaction.user.id, rp_name=self.rp_name.value, location=self.location.value,
            category=cat.strip(), occurred_at=when.strip(), description=desc, suspects=suspects, witnesses=witnesses)
        await reply(interaction, f"📄 Report **#{rep['number']}** filed. Add screenshots with "
                                 f"`/report attach number:{rep['number']}`. Check its status with **Check Report Status**.")


class ContactModal(discord.ui.Modal):
    def __init__(self, departments: list[dict], non_emergency: bool):
        super().__init__(title="Non-emergency request" if non_emergency else "Contact a department")
        self.non_emergency = non_emergency
        codes = ", ".join(d["short_code"] for d in departments)
        self.department = discord.ui.TextInput(label=f"Department ({codes})"[:45], max_length=8)
        self.message = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.department)
        self.add_item(self.message)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        dep = await bot.core.units.department_by_code(interaction.guild_id, self.department.value.strip())
        if dep is None or not dep["active"]:
            return await reply(interaction, "That department is not configured.", error=True)
        msg = clean_text(self.message.value, 1000, required=True, field="Message")
        kind = "Non-emergency request" if self.non_emergency else "Department contact"
        await bot.audit.record(interaction.guild_id, interaction.user.id, "citizen.contact", dep["short_code"], {"kind": kind})
        sent = await bot.post_department_message(interaction.guild_id, dep, interaction.user, kind, msg)
        await reply(interaction, f"📨 Sent to **{dep['name']}**." if sent else
                    "⚠️ No department-notification channel is configured. Ask an administrator to run `/setup`.")


class ReportStatusModal(discord.ui.Modal, title="Check report status"):
    number = discord.ui.TextInput(label="Report number", max_length=8)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        try:
            n = int(self.number.value.strip().lstrip("#"))
        except ValueError:
            return await reply(interaction, "Enter a number.", error=True)
        rep = await bot.core.reports.get_by_number(interaction.guild_id, n)
        from bot.ui import has_perm
        if rep is None or (rep["reporter_id"] != interaction.user.id and not await has_perm(interaction, "report.view_all")):
            return await reply(interaction, "No report with that number was filed by you.", error=True)
        await reply(interaction, f"📄 Report #{rep['number']}: **{REPORT_STATUSES.get(rep['status'], rep['status'])}**.")

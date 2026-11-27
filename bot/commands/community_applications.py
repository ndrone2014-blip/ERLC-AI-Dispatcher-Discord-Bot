"""v4.0 applications: /apply (a step-by-step wizard) and /application (review tools).

Discord modals hold at most 5 text boxes with 45-character labels, so the wizard shows every
question in full in an embed first, then collects text answers in modals (up to 5 at a time)
and choice / yes-no / confirmation answers with menus and buttons.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot import embeds as E
from bot.ui import bot_of, reply, require
from community.applications import PRESETS, STATUSES
from community.common import ServiceError
from database.models import discord_ts
from security.validation import ValidationError

from .community_ui import perms_of, review_view, role_ids

if TYPE_CHECKING:
    from bot.client import DispatchBot

TEXT = {"short", "long"}


async def _form_ac(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    forms = await interaction.client.community.applications.forms(interaction.guild_id, enabled_only=True)
    return [app_commands.Choice(name=f["name"][:100], value=f["id"]) for f in forms if current.lower() in f["name"].lower()][:25]


async def _any_form_ac(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    forms = await interaction.client.community.applications.forms(interaction.guild_id)
    return [app_commands.Choice(name=f"{f['name']} ({'open' if f['enabled'] else 'closed'})"[:100], value=f["id"]) for f in forms
            if current.lower() in f["name"].lower()][:25]


class Wizard:
    """Per-user, in-memory progress through one form (answers are only saved on submit)."""

    def __init__(self, form: dict, user_id: int):
        self.form = form
        self.user_id = user_id
        self.q = form["questions"]
        self.i = 0
        self.answers: dict[str, object] = {}
        self.stack: list[int] = []  # where each step started, for the Back button

    def advance(self, n: int = 1) -> None:
        self.stack.append(self.i)
        self.i += n

    def back(self) -> None:
        self.i = self.stack.pop() if self.stack else 0

    def text_batch(self) -> list[dict]:
        out = []
        j = self.i
        while j < len(self.q) and self.q[j]["type"] in TEXT and len(out) < 5:
            out.append(self.q[j])
            j += 1
        return out

    def step_embed(self, look: E.Look) -> discord.Embed:
        e = E.base(f"📝 {self.form['name']}", look=look, colour=E.INFO)
        e.set_footer(text=f"Question {min(self.i + 1, len(self.q))} of {len(self.q)} • {look.footer()}")
        if self.i >= len(self.q):
            e.description = "All questions answered. Press **Submit** to send your application."
            return e
        cur = self.q[self.i]
        if cur["type"] in TEXT:
            batch = self.text_batch()
            e.description = "Press **Answer** and fill in:\n\n" + "\n".join(
                f"**{self.i + k + 1}.** {E.safe(q['label'], 300)}{'' if q['required'] else ' *(optional)*'}" for k, q in enumerate(batch))
        else:
            e.description = f"**{self.i + 1}.** {E.safe(cur['label'], 300)}"
        return e


class AnswerModal(discord.ui.Modal):
    def __init__(self, wiz: Wizard, view: "WizardView"):
        super().__init__(title=f"{wiz.form['name']}"[:45], timeout=900)
        self.wiz, self.wview = wiz, view
        self.batch = wiz.text_batch()
        self.inputs = []
        for k, q in enumerate(self.batch):
            num = wiz.i + k + 1
            label = f"{num}. {q['label']}"
            ti = discord.ui.TextInput(label=(label[:42] + "…") if len(label) > 45 else label,
                                      style=discord.TextStyle.short if q["type"] == "short" else discord.TextStyle.paragraph,
                                      required=q["required"], max_length=q["max_length"],
                                      placeholder=q["label"][:100] if len(label) > 45 else None,
                                      default=str(wiz.answers.get(q["id"], "") or "") or None)
            self.inputs.append((q, ti))
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        for q, ti in self.inputs:
            self.wiz.answers[q["id"]] = str(ti.value or "")
        self.wiz.advance(len(self.batch))
        await self.wview.render(interaction, edit=True)


class WizardView(discord.ui.View):
    def __init__(self, wiz: Wizard, look: E.Look):
        super().__init__(timeout=900)
        self.wiz, self.look = wiz, look

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.wiz.user_id

    def build(self) -> None:
        self.clear_items()
        w = self.wiz
        if w.i >= len(w.q):
            b = discord.ui.Button(label="Submit application", emoji="📨", style=discord.ButtonStyle.success)
            b.callback = self._submit
            self.add_item(b)
        else:
            q = w.q[w.i]
            if q["type"] in TEXT:
                b = discord.ui.Button(label="Answer", emoji="✍️", style=discord.ButtonStyle.primary)
                b.callback = self._open_modal
                self.add_item(b)
            elif q["type"] in {"choice", "dropdown", "yesno"}:
                opts = ["Yes", "No"] if q["type"] == "yesno" else q["options"]
                sel = discord.ui.Select(placeholder="Choose an answer…", options=[discord.SelectOption(label=o[:100], value=o[:100]) for o in opts][:25])

                async def picked(interaction: discord.Interaction, sel=sel, q=q):
                    w.answers[q["id"]] = sel.values[0]
                    w.advance()
                    await self.render(interaction, edit=True)
                sel.callback = picked
                self.add_item(sel)
                if not q["required"]:
                    skip = discord.ui.Button(label="Skip", style=discord.ButtonStyle.secondary)

                    async def skipped(interaction: discord.Interaction, q=q):
                        w.answers[q["id"]] = ""
                        w.advance()
                        await self.render(interaction, edit=True)
                    skip.callback = skipped
                    self.add_item(skip)
            elif q["type"] == "confirm":
                b = discord.ui.Button(label="I confirm", emoji="✅", style=discord.ButtonStyle.success)

                async def confirmed(interaction: discord.Interaction, q=q):
                    w.answers[q["id"]] = True
                    w.advance()
                    await self.render(interaction, edit=True)
                b.callback = confirmed
                self.add_item(b)
        if w.i > 0:
            back = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)
            back.callback = self._back
            self.add_item(back)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def render(self, interaction: discord.Interaction, *, edit: bool) -> None:
        self.build()
        embed = self.wiz.step_embed(self.look)
        if edit:
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def _open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AnswerModal(self.wiz, self))

    async def _back(self, interaction: discord.Interaction):
        self.wiz.back()
        await self.render(interaction, edit=True)

    async def _cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Application cancelled — nothing was sent.", embed=None, view=None)

    async def _submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        try:
            app = await bot.community.applications.submit(interaction.guild_id, self.wiz.form["id"], interaction.user.id,
                                                          self.wiz.answers, role_ids(interaction))
        except (ServiceError, ValidationError) as e:
            return await interaction.response.edit_message(content=f"⚠️ {e}", view=self)
        self.stop()
        await interaction.response.edit_message(
            content=None, view=None,
            embed=E.base("📨 Application submitted", look=self.look, colour=E.OK,
                         description=f"Your **{E.safe(app['form_name'], 60)}** application (#{app['number']}) was sent. "
                                     "You'll get a DM when it's reviewed."))


def application_embed(app: dict, look: E.Look) -> discord.Embed:
    colour = {"approved": E.OK, "denied": E.DANGER, "awaiting_response": E.WARN}.get(app["status"], E.INFO)
    e = E.base(f"📝 Application #{app['number']} · {app['form_name']}", look=look, colour=colour,
               description=f"Applicant: <@{app['user_id']}> · Status: **{STATUSES.get(app['status'], app['status'])}** · "
                           f"{discord_ts(app['created_at'], 'R')}")
    for a in app["answers"][:20]:
        e.add_field(name=E.safe(a["question"], 250) or "—", value=E.safe(a["answer"] or "—", 1000), inline=False)
    if app.get("decision_reason"):
        e.add_field(name="Reviewer note", value=E.safe(app["decision_reason"], 500), inline=False)
    if app.get("notes"):
        e.add_field(name="Internal notes", value="\n".join(f"• {E.safe(n['note'], 150)} — <@{n['author_id']}>" for n in app["notes"][-4:]),
                    inline=False)
    return e


class CommunityApplicationsCog(commands.Cog):
    def __init__(self, bot: "DispatchBot"):
        self.bot = bot

    @app_commands.command(name="apply", description="Apply to a department or the staff team")
    @app_commands.autocomplete(form=_form_ac)
    @app_commands.guild_only()
    async def apply(self, interaction: discord.Interaction, form: Optional[int] = None):
        if not await require(interaction, "application.submit"):
            return
        apps = self.bot.community.applications
        look = await E.look_for(self.bot, interaction.guild_id)
        if form is None:
            forms = await apps.forms(interaction.guild_id, enabled_only=True)
            if not forms:
                return await reply(interaction, "There are no open applications right now.")
            e = E.base("📝 Open Applications", look=look, colour=E.INFO, description="Run `/apply form:<name>` to start one:\n\n" +
                       "\n".join(f"• **{E.safe(f['name'], 60)}** — {E.safe(f.get('description') or '', 120)}" for f in forms))
            return await reply(interaction, embed=e)
        f = await apps.form(interaction.guild_id, form)
        if not f:
            return await reply(interaction, "That form doesn't exist.", error=True)
        problem = await apps.can_submit(interaction.guild_id, f, interaction.user.id, role_ids(interaction))
        if problem:
            return await reply(interaction, f"⚠️ {problem}", error=True)
        view = WizardView(Wizard(f, interaction.user.id), look)
        await view.render(interaction, edit=False)

    application = app_commands.Group(name="application", description="Application review tools", guild_only=True)

    @application.command(name="list", description="Applications waiting for review")
    @app_commands.choices(status=[app_commands.Choice(name="Open (needs review)", value="open")] +
                          [app_commands.Choice(name=v, value=k) for k, v in STATUSES.items()])
    async def app_list(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None):
        if not await require(interaction, "application.review"):
            return
        data = await self.bot.community.applications.list(interaction.guild_id, status=status.value if status else "open", per_page=20)
        look = await E.look_for(self.bot, interaction.guild_id)
        lines = [f"**#{a['number']}** {E.safe(a['form_name'], 40)} · <@{a['user_id']}> · {STATUSES.get(a['status'], a['status'])} · "
                 f"{discord_ts(a['created_at'], 'R')}" for a in data["items"]]
        await reply(interaction, embed=E.base(f"📝 Applications ({data['total']})", look=look,
                                              description="\n".join(lines) or "Nothing here. 🎉"))

    @application.command(name="review", description="Open an application with Approve / Deny / Request info buttons")
    async def app_review(self, interaction: discord.Interaction, number: int):
        apps = self.bot.community.applications
        app = await apps.get(interaction.guild_id, number)
        if not app:
            return await reply(interaction, "Application not found.", error=True)
        if not await apps.can_review(interaction.guild_id, app, role_ids(interaction), "application.review" in await perms_of(interaction)):
            return await reply(interaction, "⛔ You can't review applications.", error=True)
        look = await E.look_for(self.bot, interaction.guild_id)
        done = app["status"] in {"approved", "denied", "archived"}
        await reply(interaction, embed=application_embed(app, look), view=None if done else review_view(number))

    @application.command(name="note", description="Add an internal reviewer note")
    async def app_note(self, interaction: discord.Interaction, number: int, note: app_commands.Range[str, 1, 1000]):
        if not await require(interaction, "application.review"):
            return
        await self.bot.community.applications.add_note(interaction.guild_id, number, interaction.user.id, note)
        await reply(interaction, f"📝 Note added to application #{number}.")

    @application.command(name="respond", description="Answer a reviewer's question about your application")
    async def app_respond(self, interaction: discord.Interaction, number: int, answer: app_commands.Range[str, 1, 1500]):
        await self.bot.community.applications.respond(interaction.guild_id, number, interaction.user.id, answer)
        await reply(interaction, "✅ Your answer was sent to the reviewers.")

    @application.command(name="mine", description="Your applications and their status")
    async def app_mine(self, interaction: discord.Interaction):
        data = await self.bot.community.applications.list(interaction.guild_id, status="all", user_id=interaction.user.id, per_page=15)
        look = await E.look_for(self.bot, interaction.guild_id)
        lines = [f"**#{a['number']}** {E.safe(a['form_name'], 40)} · {STATUSES.get(a['status'], a['status'])} · {discord_ts(a['created_at'], 'R')}"
                 for a in data["items"]]
        await reply(interaction, embed=E.base("📝 Your Applications", look=look, description="\n".join(lines) or "You haven't applied yet."))

    @application.command(name="preset", description="Create a ready-made form (starts closed so you can review it first)")
    @app_commands.choices(preset=[app_commands.Choice(name=p, value=p) for p in PRESETS])
    async def app_preset(self, interaction: discord.Interaction, preset: app_commands.Choice[str]):
        if not await require(interaction, "application.manage"):
            return
        f = await self.bot.community.applications.create_from_preset(interaction.guild_id, preset.value, interaction.user.id)
        await reply(interaction, f"✅ Form **{E.safe(f['name'], 60)}** created with {len(f['questions'])} questions. It is **closed** until you "
                                 f"open it with `/application toggle` (edit questions on the dashboard: {self.bot.settings.dashboard_url}/#/applications/forms).")

    @application.command(name="toggle", description="Open or close a form")
    @app_commands.autocomplete(form=_any_form_ac)
    async def app_toggle(self, interaction: discord.Interaction, form: int, open: bool):
        if not await require(interaction, "application.manage"):
            return
        apps = self.bot.community.applications
        f = await apps.form(interaction.guild_id, form)
        if not f:
            return await reply(interaction, "Form not found.", error=True)
        await apps.save_form(interaction.guild_id, {**f, "enabled": open}, interaction.user.id, form_id=f["id"])
        await reply(interaction, f"{'🟢 Opened' if open else '⚪ Closed'} **{E.safe(f['name'], 60)}**.")


async def setup(bot: "DispatchBot"):
    await bot.add_cog(CommunityApplicationsCog(bot))


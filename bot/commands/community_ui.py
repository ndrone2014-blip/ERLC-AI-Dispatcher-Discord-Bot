"""Shared Discord UI for the v4.0 community features: confirmations, the Roblox verification flow,
and persistent buttons (they keep working after the bot restarts)."""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

import discord

from bot import embeds as E
from bot.ui import bot_of, rate_limited, reply
from community.common import ServiceError
from security.permissions import Actor
from security.validation import ValidationError


async def perms_of(interaction: discord.Interaction) -> set[str]:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return set()
    return await bot_of(interaction).permissions.all_for(interaction.guild.id, Actor.from_member(interaction.user))


def role_ids(interaction: discord.Interaction) -> set[int]:
    return {r.id for r in getattr(interaction.user, "roles", [])}


async def guarded(interaction: discord.Interaction, fn: Callable[[], Awaitable[None]]) -> None:
    """Run a UI callback and turn expected errors into a friendly ephemeral reply."""
    try:
        await fn()
    except (ServiceError, ValidationError) as e:
        await reply(interaction, f"⚠️ {e}", error=True)


class ConfirmView(discord.ui.View):
    """Two-button confirmation that only the original user can press."""

    def __init__(self, user_id: int, on_confirm: Callable[[discord.Interaction], Awaitable[None]], *, label: str = "Confirm",
                 danger: bool = True, timeout: float = 90):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.on_confirm = on_confirm
        self.confirm.label = label
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await reply(interaction, "This confirmation isn't for you.", error=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await guarded(interaction, lambda: self.on_confirm(interaction))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


# ---------------------------------------------------------------------------------------------- verification
def verify_code_embed(info: dict, look: E.Look) -> discord.Embed:
    e = E.base("✅ Verify your Roblox account", look=look, colour=E.OK, description=(
        f"You're verifying as **{info['roblox']['name']}**.\n\n"
        "**1.** Open your Roblox profile (button below) and edit the **About** section.\n"
        "**2.** Paste this code anywhere in it and **save**:\n"
        f"```\n{info['code']}\n```"
        "**3.** Come back and press **Done**.\n\n"
        f"The code expires in {info['expires_in_minutes']} minutes. You can delete it from your profile afterwards."))
    return e


class UsernameModal(discord.ui.Modal, title="Verify your Roblox account"):
    username = discord.ui.TextInput(label="Roblox username", placeholder="e.g. Builderman", min_length=3, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        await start_verify(interaction, str(self.username.value))


class ProfileCodeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Use a profile code", emoji="🔑", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(UsernameModal())


async def start_verify(interaction: discord.Interaction, username: Optional[str], recheck: bool = False) -> None:
    bot = bot_of(interaction)
    if interaction.guild is None:
        return await reply(interaction, "Verification works inside a server.", error=True)
    if await rate_limited(interaction, "verify"):
        return
    v = bot.community.verification
    s = await v.settings(interaction.guild.id)
    look = await E.look_for(bot, interaction.guild_id)
    if not s["enabled"]:
        return await reply(interaction, "Roblox verification is turned off on this server.", error=True)
    link = await v.get_link(interaction.guild.id, interaction.user.id)

    async def go():
        if username:
            info = await v.start_profile(interaction.guild.id, interaction.user.id, username)
            view = discord.ui.View(timeout=None)
            view.add_item(VerifyCheckItem(interaction.user.id))
            view.add_item(discord.ui.Button(label="Open my Roblox profile", url=info["profile_url"]))
            return await reply(interaction, embed=verify_code_embed(info, look), view=view)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        # 1) Already verified with Bloxlink or RoVer? Then there's nothing to do.
        res = await v.try_external(interaction.guild.id, interaction.user.id)
        if res:
            from community.verification import METHOD_LABEL
            e = E.base("🎉 You're verified!", look=look, colour=E.OK,
                       description=f"Linked to **{res['roblox']['name']}** on Roblox ({METHOD_LABEL.get(res['method'], res['method'])}). "
                                   "Nothing else to do.")
            if res["problems"]:
                e.add_field(name="Heads-up for staff", value="\n".join(res["problems"])[:1000], inline=False)
            return await reply(interaction, embed=e)
        # 2) Sign in with Roblox (official login page), with the profile code as an optional fallback.
        oauth_ready = s["allow_oauth"] and v.oauth_available() and bool(bot.settings.dashboard_public_url)
        keys = await v.external_status(interaction.guild.id)
        sites = [(name, url) for prov, name, url in EXTERNAL_SITES if keys.get(prov) and s.get(f"use_{prov}", True)]
        view = discord.ui.View(timeout=600)
        e = E.base("✅ Verify your Roblox account", look=look, colour=E.OK)
        intro = f"You're currently linked to **{link['roblox_username']}**. Verifying again replaces that link.\n\n" if link else ""
        if recheck:
            intro = ("⚠️ We couldn't find your account yet. Make sure you finished on the site, using the **same Discord "
                     "account** you're on now, then press **I'm done** again.\n\n") + intro
        if oauth_ready:
            req = await v.start_oauth(interaction.guild.id, interaction.user.id)
            view.add_item(discord.ui.Button(label="Sign in with Roblox", emoji="🔐",
                                            url=f"{bot.settings.dashboard_public_url}/verify/roblox/{req['token']}"))
            e.description = (intro + "**1.** Press **Sign in with Roblox**.\n**2.** Log in on roblox.com and press **Confirm**.\n"
                             "**3.** Done — come back to Discord.\n\n"
                             f"Roblox never shares your password with us. This link works once, for {req['expires_in_minutes']} minutes.")
        elif sites:
            # No Roblox app of our own: send members to Bloxlink/RoVer, which use the official Roblox sign-in,
            # then read the result back with the server's API key.
            for name, url in sites:
                view.add_item(discord.ui.Button(label=f"Verify on {name}", emoji="🔐", url=url))
            view.add_item(VerifyRecheckItem())
            names = " or ".join(n for n, _ in sites)
            e.description = (intro + f"**1.** Press **Verify on {names}** and sign in with Discord.\n"
                             "**2.** Choose **Sign in with Roblox** there and log in on roblox.com.\n"
                             "**3.** Come back and press **I'm done**.\n\n"
                             "Never type your Roblox password anywhere except roblox.com.")
        elif not s["allow_profile_code"]:
            return await reply(interaction, "No verification method is set up on this server yet. Ask an administrator.", error=True)
        if s["allow_profile_code"]:
            btn = ProfileCodeButton()
            if oauth_ready or sites:
                btn.label, btn.style = "Can't sign in? Use a code instead", discord.ButtonStyle.secondary
            else:
                e.description = intro + "Press **Use a profile code** and enter your Roblox username. You'll get a short code to put in your " \
                                        "Roblox profile's About section for a moment."
            view.add_item(btn)
        await reply(interaction, embed=e, view=view)

    await guarded(interaction, go)


EXTERNAL_SITES = (("bloxlink", "Bloxlink", "https://blox.link/verify"),
                  ("rover", "RoVer", "https://rover.link/verify"))


class VerifyRecheckItem(discord.ui.DynamicItem[discord.ui.Button], template=r"pd:verify:recheck"):
    """After verifying on Bloxlink/RoVer: look the member up again."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="I'm done", emoji="✔️", style=discord.ButtonStyle.success,
                                           custom_id="pd:verify:recheck"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await start_verify(interaction, None, recheck=True)


class VerifyStartItem(discord.ui.DynamicItem[discord.ui.Button], template=r"pd:verify:start"):
    """The button on the public verification panel."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Verify", emoji="✅", style=discord.ButtonStyle.success, custom_id="pd:verify:start"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await start_verify(interaction, None)


class VerifyCheckItem(discord.ui.DynamicItem[discord.ui.Button], template=r"pd:verify:check:(?P<uid>\d+)"):
    def __init__(self, user_id: int):
        super().__init__(discord.ui.Button(label="Done", emoji="✔️", style=discord.ButtonStyle.success,
                                           custom_id=f"pd:verify:check:{user_id}"))
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await reply(interaction, "This verification belongs to someone else. Run `/verify` yourself.", error=True)
        if await rate_limited(interaction, "verify-check"):
            return
        bot = bot_of(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)

        async def go():
            res = await bot.community.verification.check_profile(interaction.guild_id, interaction.user.id)
            look = await E.look_for(bot, interaction.guild_id)
            e = E.base("🎉 Verified!", look=look, colour=E.OK,
                       description=f"Your Discord account is now linked to **{res['roblox']['name']}** on Roblox. "
                                   "You can remove the code from your profile now.")
            if res["problems"]:
                e.add_field(name="Heads-up for staff", value="\n".join(res["problems"])[:1000], inline=False)
            await interaction.followup.send(embed=e, ephemeral=True)

        await guarded(interaction, go)


def verification_panel_embed(look: E.Look, guild_name: str) -> discord.Embed:
    return E.base("✅ Verify your Roblox account", look=look, colour=E.OK, description=(
        f"Welcome to **{guild_name}**! Link your Roblox account to get access.\n\n"
        "Press **Verify** and follow the steps — it takes about a minute. Your Roblox password is never needed."))


# ---------------------------------------------------------------------------------------------- sessions
class SessionItem(discord.ui.DynamicItem[discord.ui.Button], template=r"pd:sess:(?P<action>join|leave):(?P<n>\d+)"):
    def __init__(self, action: str, number: int):
        super().__init__(discord.ui.Button(label="Join" if action == "join" else "Leave",
                                           emoji="🙋" if action == "join" else None,
                                           style=discord.ButtonStyle.success if action == "join" else discord.ButtonStyle.secondary,
                                           custom_id=f"pd:sess:{action}:{number}"))
        self.action, self.number = action, number

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["n"]))

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)

        async def go():
            svc = bot.community.sessions
            s = await (svc.join if self.action == "join" else svc.leave)(interaction.guild_id, self.number, interaction.user.id)
            await reply(interaction, f"{'🙋 You joined' if self.action == 'join' else 'You left'} **{s['name']}** "
                                     f"({s['participant_count']} participant{'s' if s['participant_count'] != 1 else ''}).")

        await guarded(interaction, go)


def session_view(number: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(SessionItem("join", number))
    v.add_item(SessionItem("leave", number))
    return v


# ---------------------------------------------------------------------------------------------- applications
class ReasonModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, on_submit: Callable[[discord.Interaction, str], Awaitable[None]], required: bool):
        super().__init__(title=title[:45])
        self.reason = discord.ui.TextInput(label=label[:45], style=discord.TextStyle.paragraph, max_length=1000, required=required)
        self.add_item(self.reason)
        self._cb = on_submit

    async def on_submit(self, interaction: discord.Interaction):
        await guarded(interaction, lambda: self._cb(interaction, str(self.reason.value or "")))


class AppReviewItem(discord.ui.DynamicItem[discord.ui.Button], template=r"pd:app:(?P<action>approve|deny|info|review):(?P<n>\d+)"):
    STYLE = {"approve": ("Approve", "✅", discord.ButtonStyle.success), "deny": ("Deny", "❌", discord.ButtonStyle.danger),
             "info": ("Request info", "✉️", discord.ButtonStyle.secondary), "review": ("Mark under review", "👀", discord.ButtonStyle.secondary)}

    def __init__(self, action: str, number: int):
        label, emoji, style = self.STYLE[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"pd:app:{action}:{number}"))
        self.action, self.number = action, number

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["n"]))

    async def callback(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        apps = bot.community.applications
        app = await apps.get(interaction.guild_id, self.number)
        if not app:
            return await reply(interaction, "Application not found.", error=True)
        if not await apps.can_review(interaction.guild_id, app, role_ids(interaction), "application.review" in await perms_of(interaction)):
            return await reply(interaction, "⛔ You can't review applications.", error=True)
        status = {"approve": "approved", "deny": "denied", "info": "awaiting_response", "review": "under_review"}[self.action]

        async def apply(inter: discord.Interaction, reason: str):
            res = await apps.set_status(inter.guild_id, self.number, status, inter.user.id, reason)
            extra = ("\n⚠️ " + "\n⚠️ ".join(res["problems"])) if res.get("problems") else ""
            await reply(inter, f"Application #{self.number} → **{status.replace('_', ' ').title()}**.{extra}")

        if self.action == "review":
            return await guarded(interaction, lambda: apply(interaction, ""))
        label = {"approve": "Note for the applicant (optional)", "deny": "Reason (sent to the applicant)",
                 "info": "What do you need from them?"}[self.action]
        await interaction.response.send_modal(ReasonModal(f"Application #{self.number}", label, apply, required=self.action == "info"))


def review_view(number: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for a in ("approve", "deny", "info", "review"):
        v.add_item(AppReviewItem(a, number))
    return v


DYNAMIC_ITEMS = (VerifyStartItem, VerifyCheckItem, VerifyRecheckItem, SessionItem, AppReviewItem)

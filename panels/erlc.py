"""Panel 9: ER:LC Integration."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot.ui import BRAND, FOOTER, bot_of, reply, require, status_dot, truncate
from database.models import discord_ts
from erlc.authentication import GLOBAL_KEY_NAME, SERVER_KEY_NAME, looks_like_server_key
from erlc.webhooks import new_webhook_token

if TYPE_CHECKING:
    from bot.client import DispatchBot

LIMITATIONS = (
    "• Only **in-game 911 calls** (`EmergencyCalls`) are exposed by the documented API. Robberies, pursuits and other "
    "events are **not** API-verifiable — incidents reported in Discord are labelled UNVERIFIED.\n"
    "• Calls are detected by polling `GET /v2/server`; the event webhook (optional) only triggers an instant poll "
    "because its payload format is not documented.\n"
    "• Caller postal/street shown is the caller's *current* in-game position from the player list."
)


async def build(bot: "DispatchBot", guild_id: int):
    sync = bot.sync.state(guild_id)
    st = await bot.db.fetchone("SELECT * FROM api_sync_state WHERE guild_id = ?", (guild_id,))
    has_key = await bot.secrets.exists(guild_id, SERVER_KEY_NAME) or bool(bot.settings.erlc_server_key)
    monitoring = await bot.db.get_config(guild_id, "erlc_monitoring_enabled", True)
    e = discord.Embed(title="🎮 ER:LC Integration", colour=BRAND)
    e.add_field(name="API status", value=f"{status_dot(sync.status == 'connected')} {sync.status}" +
                (f"\n{truncate(sync.detail, 200)}" if sync.detail else ""), inline=True)
    e.add_field(name="Server key", value="🔐 Stored (encrypted)" if await bot.secrets.exists(guild_id, SERVER_KEY_NAME)
                else ("From .env" if has_key else "❌ Not set"), inline=True)
    e.add_field(name="911 monitoring", value="On" if monitoring else "Off", inline=True)
    if st:
        e.add_field(name="Server", value=f"{st['server_name'] or '—'}" + (f"\nJoin code `{st['join_key']}`" if st["join_key"] else ""),
                    inline=True)
        e.add_field(name="Players", value=f"{st['current_players']}/{st['max_players']}" if st["current_players"] is not None else "—",
                    inline=True)
        e.add_field(name="Last successful sync", value=discord_ts(st["last_success_at"]), inline=True)
        if st["last_error"]:
            e.add_field(name=f"Last API error ({st['consecutive_failures']} consecutive)",
                        value=f"{discord_ts(st['last_error_at'])} {truncate(st['last_error'], 200)}", inline=False)
        if st["rate_limit_remaining"] is not None:
            e.add_field(name="Rate limit", value=f"{st['rate_limit_remaining']} remaining in window", inline=True)
        wh = "Disabled in .env" if not bot.settings.webhook_enabled else (
            f"Last verified webhook {discord_ts(st['webhook_last_at'])}" if st["webhook_last_at"] else "Enabled — no webhook received yet")
        if bot.webhook_server and bot.webhook_server.last_error:
            wh += f"\n⚠️ {bot.webhook_server.last_error}"
        e.add_field(name="Event webhook", value=wh, inline=True)
    calls = sync.recent_calls
    if calls:
        e.add_field(name="Latest in-game 911 calls (live)", value="\n".join(
            f"#{c['number']} {truncate(c.get('description') or '—', 50)} · {truncate(c.get('location') or 'no location', 40)} · "
            f"<t:{c['started_at']}:R>" for c in calls[-5:]), inline=False)
    e.add_field(name="Monitored events & limitations", value=LIMITATIONS, inline=False)
    e.set_footer(text=FOOTER)
    return [e], ERLCView()


class ERLCView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Set API Key", emoji="🔑", style=discord.ButtonStyle.primary, custom_id="cad:erlc:key")
    async def key(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await require(interaction, "admin.erlc"):
            await interaction.response.send_modal(KeyModal())

    @discord.ui.button(label="Test Connection", emoji="🧪", style=discord.ButtonStyle.secondary, custom_id="cad:erlc:test")
    async def test(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.erlc"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg, _ = await bot_of(interaction).sync.test_connection(interaction.guild_id)
        await interaction.followup.send(("🟢 " if ok else "🔴 ") + msg, ephemeral=True)

    @discord.ui.button(label="Retry / Sync Now", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="cad:erlc:retry")
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.erlc"):
            return
        bot = bot_of(interaction)
        bot.sync.start_guild(interaction.guild_id)
        await bot.sync.reset_client(interaction.guild_id)
        await reply(interaction, "🔄 Reconnecting and polling now.")

    @discord.ui.button(label="Toggle 911 Monitoring", emoji="📡", style=discord.ButtonStyle.secondary, custom_id="cad:erlc:monitor")
    async def monitor(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.erlc"):
            return
        bot = bot_of(interaction)
        cur = await bot.db.get_config(interaction.guild_id, "erlc_monitoring_enabled", True)
        await bot.db.set_config(interaction.guild_id, "erlc_monitoring_enabled", not cur)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "erlc.monitoring", str(not cur))
        bot.sync.trigger(interaction.guild_id)
        await reply(interaction, f"In-game 911 monitoring {'enabled' if not cur else 'disabled'}.")

    @discord.ui.button(label="Webhook Setup", emoji="🪝", style=discord.ButtonStyle.secondary, custom_id="cad:erlc:webhook")
    async def webhook(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.erlc"):
            return
        bot = bot_of(interaction)
        token = await bot.db.get_config(interaction.guild_id, "erlc_webhook_token")
        if not token:
            token = new_webhook_token()
            await bot.db.set_config(interaction.guild_id, "erlc_webhook_token", token)
        status = "enabled" if bot.settings.webhook_enabled else "**disabled** — set `ERLC_WEBHOOK_ENABLED=true` in .env and restart"
        await reply(interaction, (
            f"The webhook receiver is {status}. It listens on port `{bot.settings.webhook_port}`.\n"
            "ER:LC needs a public **HTTPS** URL (e.g. a Cloudflare Tunnel to this PC). In private server settings → *Event Webhook*, set:\n"
            f"`https://<your-public-host>/erlc/webhook/{token}`\n"
            "Signatures are verified with the official Ed25519 key. Polling keeps working without it.\n"
            "⚠️ Treat this URL as a secret."))

    @discord.ui.button(label="Remove Key", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="cad:erlc:remove")
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require(interaction, "admin.erlc"):
            return
        bot = bot_of(interaction)
        await bot.secrets.delete(interaction.guild_id, SERVER_KEY_NAME)
        await bot.secrets.delete(interaction.guild_id, GLOBAL_KEY_NAME)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "erlc.key_removed")
        await bot.sync.reset_client(interaction.guild_id)
        await reply(interaction, "🗑️ Stored ER:LC keys removed.")


class KeyModal(discord.ui.Modal, title="ER:LC API key (stored encrypted)"):
    server_key = discord.ui.TextInput(label="Server key (in-game: Settings → API)", max_length=200)
    global_key = discord.ui.TextInput(label="Global API key (optional, public apps)", max_length=200, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        bot = bot_of(interaction)
        key = self.server_key.value.strip()
        if not looks_like_server_key(key):
            return await reply(interaction, "That doesn't look like a valid server key.", error=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg, _ = await bot.sync.test_connection(interaction.guild_id, key)
        if not ok:
            return await interaction.followup.send(f"🔴 Key not saved — test failed: {msg}", ephemeral=True)
        await bot.secrets.set(interaction.guild_id, SERVER_KEY_NAME, key, interaction.user.id)
        if self.global_key.value.strip():
            await bot.secrets.set(interaction.guild_id, GLOBAL_KEY_NAME, self.global_key.value.strip(), interaction.user.id)
        await bot.audit.record(interaction.guild_id, interaction.user.id, "erlc.key_set")
        bot.sync.start_guild(interaction.guild_id)
        await bot.sync.reset_client(interaction.guild_id)
        await interaction.followup.send(f"🔐 Key saved (encrypted). 🟢 {msg}", ephemeral=True)

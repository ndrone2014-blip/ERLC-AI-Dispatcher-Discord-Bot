"""The Discord bot: wires every service together and owns background tasks."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ai.gemini import GeminiProvider
from ai.manager import AIManager
from ai.providers.catalog import CATALOG, SPEC
from ai.providers.ollama import OllamaProvider
from ai.providers.openai_compat import OpenAICompatProvider
from bot import text_dispatch
from bot.actions import BotActions, member_dict
from bot.logbook import Logbook
from community.platform import CommunityPlatform
from community.roblox import RobloxAPI
from config import Settings
from core.version import NAME, RELEASE, VERSION
from core import events as ev
from core.events import EventBus
from core.logging_setup import register_secret
from database.backups import create_backup
from database.connection import Database
from database.models import PRIORITY_LABEL, iso_now
from dispatch.announcements import ANNOUNCEMENT_UPDATED
from dispatch.core import DispatchCore
from erlc.synchronization import SyncManager
from erlc.webhooks import WebhookServer
from panels import (administration, ai_config, bolos, citizen_services, dashboard, erlc as erlc_panel, incidents as incidents_panel,
                    reports as reports_panel, statistics, units as units_panel, voice as voice_panel)
from panels.base import PanelManager
from security.audit import AuditLog
from security.permissions import PermissionManager
from security.secrets import SecretStore
from security.validation import RateLimiter
from voice.audio_queue import AnnouncementPlayer
from voice.commands import VoiceCommandProcessor
from voice.connection import VoiceManager
from voice.speech_to_text import STTEngine
from voice.text_to_speech import TTSEngine

log = logging.getLogger(__name__)

PANEL_MODULES = {
    "dashboard": dashboard, "incidents": incidents_panel, "units": units_panel, "voice": voice_panel,
    "citizen": citizen_services, "reports": reports_panel, "bolos": bolos, "ai": ai_config, "erlc": erlc_panel,
    "admin": administration, "stats": statistics,
}

COGS = ["bot.commands.setup", "bot.commands.dispatch", "bot.commands.units", "bot.commands.records", "bot.commands.system",
        "bot.commands.pulse_dispatch", "bot.commands.pulse_units", "bot.commands.pulse_admin", "bot.commands.pulse_ai",
        "bot.commands.pulse_info", "bot.commands.pulse_guide",
        # v4.0 Command Center
        "bot.commands.community_members", "bot.commands.community_moderation", "bot.commands.community_staff",
        "bot.commands.community_applications", "bot.commands.community_assistant"]


class DispatchBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.members = True        # map voice speakers / roles to officers
        intents.message_content = True
        intents.voice_states = True
        # Safe default: the bot never pings anyone unless a message explicitly allows specific role mentions.
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                         allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=False, replied_user=False))
        self.settings = settings
        self.started_at_iso = iso_now()
        self.bus = EventBus()
        self.db = Database(settings.database_path)
        self.audit = AuditLog(self.db)
        self.permissions = PermissionManager(self.db)
        self.ratelimiter = RateLimiter(max_calls=8, per_seconds=10)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.secrets: SecretStore = None  # type: ignore[assignment]
        self.ai: AIManager = None  # type: ignore[assignment]
        self.core: DispatchCore = None  # type: ignore[assignment]
        self.sync: SyncManager = None  # type: ignore[assignment]
        self.voice = VoiceManager(self, self.db, self.bus, settings.ffmpeg_path)
        self.tts = TTSEngine(settings.piper_voice_dir, settings.piper_default_voice, settings.data_dir / "tts_tmp")
        self.stt = STTEngine(settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
        self.player: AnnouncementPlayer = None  # type: ignore[assignment]
        self.voice_commands: VoiceCommandProcessor = None  # type: ignore[assignment]
        self.logbook: Logbook = None  # type: ignore[assignment]
        self.panels = PanelManager(self)
        self.webhook_server: Optional[WebhookServer] = None
        self.community: CommunityPlatform = None  # type: ignore[assignment]
        self.web = None  # web.server.DashboardServer when the dashboard is enabled
        self._tasks: list[asyncio.Task] = []
        self._card_locks: dict[int, asyncio.Lock] = {}
        self._card_pending: set[int] = set()
        for secret in (settings.discord_token, settings.gemini_api_key, settings.erlc_server_key, settings.erlc_global_key,
                       settings.openai_compat_api_key, settings.secret_key, settings.discord_client_secret,
                       settings.roblox_client_secret, settings.dashboard_proxy_secret,
                       *[v.get("api_key", "") for v in settings.provider_keys.values()]):
            register_secret(secret)

    def _build_providers(self) -> list:
        s = self.settings
        out = []
        for spec in CATALOG:
            if spec.name == "gemini":
                out.append(GeminiProvider(s.gemini_api_key, s.gemini_model, self.session))
            elif spec.name == "ollama":
                out.append(OllamaProvider(s.ollama_base_url, s.ollama_model, self.session))
            elif spec.name == "openai_compat":
                out.append(OpenAICompatProvider(s.openai_compat_base_url, s.openai_compat_api_key, s.openai_compat_model,
                                                self.session))
            else:
                env = s.provider_keys.get(spec.name, {})
                out.append(OpenAICompatProvider(spec.base_url, env.get("api_key", ""), env.get("model") or spec.default_model,
                                                self.session, name=spec.name, label=spec.label,
                                                account_id=env.get("account_id", ""), needs_key=True,
                                                extra_headers=spec.extra_headers, suggested_models=spec.models))
        return out

    async def _ai_credentials(self, guild_id: int, provider: str) -> dict:
        """Per-server AI keys set by that server's administrators (encrypted at rest). Never shared between servers."""
        sp = SPEC.get(provider)
        if sp is None or not sp.needs_key or self.secrets is None:
            return {}
        key = await self.secrets.get(guild_id, f"ai_key:{provider}")
        if not key:
            return {}
        creds = {"api_key": key}
        if sp.needs_account_id:
            acct = await self.secrets.get(guild_id, f"ai_account:{provider}")
            if acct:
                creds["account_id"] = acct
        return creds

    def session(self) -> aiohttp.ClientSession:
        if self.http_session is None or self.http_session.closed:
            self.http_session = aiohttp.ClientSession()
        return self.http_session

    # ------------------------------------------------------------------ startup
    async def setup_hook(self) -> None:
        s = self.settings
        s.ensure_dirs()
        await self.db.connect()
        self.secrets = SecretStore(self.db, s.secret_key, s.data_dir / "secret.key")
        providers = self._build_providers()
        # Ollama / custom endpoints are only "configured" when their URL is set in .env (never from Discord: no SSRF).
        self.ai = AIManager(self.db, self.bus, providers, daily_limits={"gemini": s.gemini_daily_request_limit},
                            rpm_limits={"gemini": 8, "openrouter": 18, "github": 10, "cohere": 18, "nvidia": 35},
                            credentials=self._ai_credentials)
        self.core = DispatchCore(self.db, self.bus, self.audit, self.ai)
        self.logbook = Logbook(self)
        # v4.0 Command Center: community platform shared by slash commands and the web dashboard.
        self.roblox = RobloxAPI(self.session, client_id=s.roblox_client_id, client_secret=s.roblox_client_secret)
        self.community = CommunityPlatform(self.db, self.bus, self.audit, self.permissions, actions=BotActions(self),
                                           roblox=self.roblox, ai=self.ai, dispatch=self.core, secrets=self.secrets,
                                           env_keys={"rover": self.settings.rover_api_key, "bloxlink": self.settings.bloxlink_api_key})
        self.community.set_log_writer(self.logbook.write)
        self._wire_notifications()
        self.sync = SyncManager(self.db, self.bus, self.secrets, self.session, lambda: self.core.intake,
                                base_url=s.erlc_base_url, env_server_key=s.erlc_server_key, env_global_key=s.erlc_global_key,
                                env_guild_id=s.dev_guild_id, poll_seconds=s.erlc_poll_seconds)
        self.player = AnnouncementPlayer(self.voice, self.tts, self.core.announcements, self.bus)
        self.voice_commands = VoiceCommandProcessor(self.core, self.voice, self.stt, self.permissions, self._voice_log,
                                                    self._member_lookup)
        self.voice.on_utterance = self.voice_commands.submit
        asyncio.create_task(self.tts.warmup())   # load the dispatcher voice now, not on the first reply
        recovered = await self.core.announcements.recover_interrupted()
        if recovered:
            log.info("Recovered %s interrupted announcement(s) back into the queue", recovered)

        for name, module in PANEL_MODULES.items():
            self.panels.register(name, module.build)
        # Persistent views: panel buttons keep working after a restart.
        for view in (dashboard.DashboardView(), incidents_panel.IncidentsPanelView(), units_panel.UnitsView(), voice_panel.VoiceView(),
                     citizen_services.CitizenView(), reports_panel.ReportsPanelView(), bolos.BoloPanelView(), ai_config.AIView(),
                     erlc_panel.ERLCView(), administration.AdminView(), statistics.StatsView()):
            self.add_view(view)
        self.add_dynamic_items(incidents_panel.IncidentAction, reports_panel.ReportAction, bolos.BoloAction)
        from bot.commands import community_ui
        self.add_dynamic_items(*community_ui.DYNAMIC_ITEMS)

        self.bus.subscribe(ev.ACTIVITY, self._on_activity)
        self.bus.subscribe(ev.INCIDENT_CREATED, self._on_incident_change)
        self.bus.subscribe(ev.INCIDENT_UPDATED, self._on_incident_change)
        self.bus.subscribe(ev.ASSIGNMENT_CHANGED, self._on_incident_change)
        self.bus.subscribe(ev.INCIDENT_CLOSED, self._on_incident_change)
        self.bus.subscribe(ev.INCIDENT_ASSESSED, self._on_assessed)
        self.bus.subscribe(ANNOUNCEMENT_UPDATED, self._on_announcement)
        self.bus.subscribe(ev.BACKUP_REQUESTED, self._on_backup_requested)

        for cog in COGS:
            await self.load_extension(cog)
        self.tree.on_error = self._on_app_command_error
        if s.dev_guild_id:
            guild = discord.Object(id=s.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to development guild %s", len(synced), s.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands (can take up to an hour to appear)", len(synced))

        if s.dashboard_enabled:
            try:
                from web.server import DashboardServer
                self.web = DashboardServer(self)
                await self.web.start()
            except OSError as e:
                log.error("Web dashboard could not start on %s:%s (%s). Is another program using that port? "
                          "Change DASHBOARD_PORT in .env.", s.dashboard_host, s.dashboard_port, e)
                self.web = None
            except Exception:
                log.exception("Web dashboard failed to start - the bot keeps running without it")
                self.web = None

        if s.webhook_enabled:
            self.webhook_server = WebhookServer(s.webhook_host, s.webhook_port, self.sync.resolve_webhook_token, self.sync.on_webhook)
            await self.webhook_server.start()

        from core.loop_monitor import LoopMonitor
        self.loop_monitor = LoopMonitor()
        self.loop_monitor.start()
        self.voice.start_watchdog()
        self.voice_commands.start()
        self._tasks += [asyncio.create_task(self._repeat_loop(), name="repeat-announcements"),
                        asyncio.create_task(self._backup_loop(), name="daily-backup"),
                        asyncio.create_task(self._health_loop(), name="health"),
                        asyncio.create_task(self._community_loop(), name="community")]

    async def on_ready(self) -> None:
        log.info("%s v%s (%s) logged in as %s (%s) in %d guild(s)", NAME, VERSION, RELEASE, self.user,
                 self.user.id if self.user else "?", len(self.guilds))
        await self._update_presence()
        for guild in self.guilds:
            await self._init_guild(guild)

    async def on_message(self, message: discord.Message) -> None:
        await text_dispatch.on_message(self, message)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._update_presence()
        await self._init_guild(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self._update_presence()

    # ------------------------------------------------------------------ v4.0 member events
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            await self.community.members.on_join(member.guild.id, member_dict(member))
        except Exception:
            log.exception("Member join handling failed")
        if not member.bot:
            try:  # already linked in Bloxlink / RoVer? verify them right away
                s = await self.community.verification.settings(member.guild.id)
                if s["auto_verify_on_join"]:
                    await self.community.verification.try_external(member.guild.id, member.id, quiet=True)
            except Exception:
                log.debug("Automatic verification on join failed", exc_info=True)

    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            await self.community.members.on_leave(member.guild.id, member.id, member.name)
        except Exception:
            log.exception("Member leave handling failed")

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.roles == after.roles and before.display_name == after.display_name and before.display_avatar == after.display_avatar:
            return
        try:
            await self.community.members.upsert(after.guild.id, member_dict(after))
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            if added or removed:
                await self.logbook.write(after.guild.id, "roles", "Member Roles Changed",
                                         fields=[("Member", f"{after.mention} ({after.name})", True)]
                                         + ([("Added", " ".join(r.mention for r in added)[:1000], False)] if added else [])
                                         + ([("Removed", " ".join(r.mention for r in removed)[:1000], False)] if removed else []))
        except Exception:
            log.exception("Member update handling failed")

    async def on_app_command_completion(self, interaction: discord.Interaction, command) -> None:
        try:
            name = getattr(command, "qualified_name", str(command))
            await self.community.analytics.record_command(interaction.guild_id, interaction.user.id, name, True)
            if interaction.guild_id:
                await self.logbook.write(interaction.guild_id, "commands", f"/{name}",
                                         fields=[("User", f"{interaction.user.mention}", True),
                                                 ("Channel", f"<#{interaction.channel_id}>" if interaction.channel_id else "—", True)],
                                         actor_id=None)
        except Exception:
            log.debug("Could not record command usage", exc_info=True)

    async def _sync_members(self, guild: discord.Guild) -> None:
        try:
            if not guild.chunked:
                await guild.chunk(cache=True)
            n = await self.community.members.bulk_sync(guild.id, [member_dict(m) for m in guild.members])
            log.info("Member directory for %s synced (%d members)", guild.name, n)
        except Exception:
            log.exception("Member directory sync failed for %s", guild.id)

    def _wire_notifications(self) -> None:
        """Turn existing dispatch / integration events into dashboard notifications."""
        notes = self.community.notifications

        async def ai_failure(e: ev.Event) -> None:
            if e.data.get("kind") in {"auth", "unavailable"}:
                await notes.create(e.guild_id, "integration", f"AI provider problem: {e.data.get('provider')}",
                                   body=str(e.data.get("message") or "")[:300], severity="warning", link="#/ai/providers",
                                   permission="admin.ai", dedupe_key=f"ai:{e.data.get('provider')}:{e.data.get('kind')}",
                                   window_seconds=3600)

        async def erlc_status(e: ev.Event) -> None:
            if e.data.get("status") == "error":
                await notes.create(e.guild_id, "integration", "ER:LC API error", body=str(e.data.get("detail") or "")[:300],
                                   severity="warning", link="#/settings/integrations", permission="admin.erlc",
                                   dedupe_key="erlc:error", window_seconds=3600)

        async def assessed(e: ev.Event) -> None:
            inc = await self.core.incidents.get(e.data.get("incident_id"))
            if inc and (inc.get("priority") or inc.get("ai_priority")) == "critical" and inc["status"] != "closed":
                await notes.create(e.guild_id, "dispatch", f"Critical incident #{inc['number']}",
                                   body=f"{inc.get('nature') or 'Unknown'} at {inc.get('location') or 'unknown location'}",
                                   severity="critical", link="#/dispatch", permission="incident.view",
                                   dedupe_key=f"critical:{inc['id']}", window_seconds=86400)

        self.bus.subscribe("ai.failure", ai_failure)
        self.bus.subscribe(ev.ERLC_STATUS, erlc_status)
        self.bus.subscribe(ev.INCIDENT_ASSESSED, assessed)

    def _presence_text(self) -> str:
        count = len(self.guilds)
        return f"/help | Dispatching {count} Server{'' if count == 1 else 's'}"

    async def _update_presence(self) -> None:
        """'Watching' status on the bot's profile; the same text shows as the title and the line under it."""
        text = self._presence_text()
        activity = discord.Activity(type=discord.ActivityType.watching, name=text, state=text)
        try:
            await self.change_presence(status=discord.Status.online, activity=activity)
        except Exception:
            log.exception("Could not update the bot's status")

    async def _init_guild(self, guild: discord.Guild) -> None:
        await self.core.units.ensure_defaults(guild.id, guild.name)
        await self.ai.ensure_guild(guild.id)
        self.sync.start_guild(guild.id)
        self.player.start_guild(guild.id)
        asyncio.create_task(self.panels.refresh_all(guild.id, force=True))
        asyncio.create_task(self._sync_members(guild))

    async def close(self) -> None:
        log.info("Shutting down gracefully…")
        for t in self._tasks:
            t.cancel()
        if getattr(self, "loop_monitor", None):
            self.loop_monitor.stop()
        try:
            if self.sync:
                await self.sync.stop()
            if self.player:
                await self.player.stop()
            await self.voice.shutdown()
            if self.webhook_server:
                await self.webhook_server.stop()
            if self.web:
                await self.web.stop()
        except Exception:
            log.exception("Error during shutdown")
        await super().close()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await self.db.close()
        log.info("Shutdown complete.")

    # ------------------------------------------------------------------ background tasks
    async def _repeat_loop(self) -> None:
        await self.wait_until_ready()
        while True:
            for g in list(self.guilds):
                try:
                    await self.core.repeat_tick(g.id)
                except Exception:
                    log.exception("Repeat tick failed for %s", g.id)
            await asyncio.sleep(15)

    async def _backup_loop(self) -> None:
        await self.wait_until_ready()
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                await create_backup(self.db, self.settings.backup_dir)
            except Exception:
                log.exception("Automatic backup failed")

    async def _community_loop(self) -> None:
        """Reminders, scheduled messages, leave dates, overdue shifts and auto-archiving (v4.0)."""
        await self.wait_until_ready()
        tick = 0
        while True:
            await asyncio.sleep(60)
            tick += 1
            try:
                await self.community.automation.run_due_schedules()
                await self.community.verification.expire_old()
            except Exception:
                log.exception("Scheduled messages failed")
            for g in list(self.guilds):
                try:
                    await self.community.sessions.tick(g.id)
                    await self.community.shifts.close_overdue(g.id)
                    if tick % 10 == 1:
                        await self.community.staff.tick(g.id)
                    if tick % 60 == 1:
                        await self.community.applications.tick(g.id)
                except Exception:
                    log.exception("Community tick failed for %s", g.id)

    async def _health_loop(self) -> None:
        await self.wait_until_ready()
        while True:
            await asyncio.sleep(60)
            try:
                await self.db.fetchval("SELECT 1")
            except Exception:
                log.exception("Database health check failed")
            for g in list(self.guilds):
                gs = self.sync.guilds.get(g.id)
                if gs is None or gs.task is None or gs.task.done():
                    log.warning("ER:LC sync task for %s not running - restarting", g.id)
                    self.sync.start_guild(g.id)
                self.player.start_guild(g.id)
                self.panels.schedule(g.id, "dashboard")
                self.panels.schedule(g.id, "ai")

    # ------------------------------------------------------------------ channels & notifications
    async def channel_for(self, guild_id: int, key: str) -> Optional[discord.TextChannel]:
        cid = await self.db.get_config(guild_id, key)
        ch = self.get_channel(int(cid)) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def log_channel(self, guild_id: int) -> Optional[discord.TextChannel]:
        return await self.channel_for(guild_id, "log_channel_id")

    async def notify_admins(self, guild_id: int, text: str) -> None:
        ch = await self.channel_for(guild_id, "admin_channel_id") or await self.log_channel(guild_id)
        if ch:
            try:
                await ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    async def supervisor_mentions(self, guild_id: int) -> str:
        roles = (await self.permissions.role_map(guild_id)).get("supervisor", [])
        return " ".join(f"<@&{r}>" for r in roles)

    async def notify_supervisors(self, guild_id: int, text: str, view: Optional[discord.ui.View] = None,
                                 embed: Optional[discord.Embed] = None) -> Optional[discord.Message]:
        ch = await self.channel_for(guild_id, "department_channel_id") or await self.log_channel(guild_id)
        if ch is None:
            return None
        mention = await self.supervisor_mentions(guild_id)
        try:
            return await ch.send(f"{mention} {text}".strip(), view=view, embed=embed,
                                 allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
        except discord.HTTPException:
            return None

    async def post_department_message(self, guild_id: int, dep: dict, user: discord.abc.User, kind: str, text: str) -> bool:
        ch = await self.channel_for(guild_id, "department_channel_id")
        if ch is None:
            return False
        e = discord.Embed(title=f"📨 {kind} — {dep['name']}", description=text, colour=0x1F6FEB)
        e.set_author(name=str(user), icon_url=user.display_avatar.url if hasattr(user, "display_avatar") else None)
        mention = f"<@&{dep['role_id']}>" if dep.get("role_id") else ""
        await ch.send(mention or None, embed=e, allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
        return True

    async def activity_feed(self, guild_id: int) -> list[dict]:
        return await self.db.get_config(guild_id, "activity_feed", []) or []

    async def _on_activity(self, e: ev.Event) -> None:
        feed = await self.activity_feed(e.guild_id)
        feed.append({"at": iso_now(), "text": str(e.data.get("text", ""))[:200]})
        await self.db.set_config(e.guild_id, "activity_feed", feed[-15:])

    # ------------------------------------------------------------------ dispatch log cards
    async def _on_incident_change(self, e: ev.Event) -> None:
        incident_id = e.data.get("incident_id")
        if not incident_id:
            return
        if incident_id in self._card_pending:
            return  # an update is already scheduled; it will read the latest state
        self._card_pending.add(incident_id)
        await asyncio.sleep(1.5)  # coalesce bursts of updates into one edit
        self._card_pending.discard(incident_id)
        lock = self._card_locks.setdefault(incident_id, asyncio.Lock())
        async with lock:
            await self._upsert_card(e.guild_id, incident_id)

    async def _upsert_card(self, guild_id: int, incident_id: int) -> None:
        from bot.ui import incident_embed
        if not await self.logbook.enabled(guild_id, "incidents"):
            return
        ch = await self.logbook.channel_for(guild_id, "incidents")
        inc = await self.core.incidents.get(incident_id)
        if ch is None or inc is None:
            return
        units = await self.core.incidents.assigned_units(incident_id)
        embed = incident_embed(inc, units, detailed=True)
        view = discord.ui.View(timeout=None)
        if inc["status"] != "closed":
            view.add_item(incidents_panel.IncidentAction("view", incident_id, label="Open / Manage"))
            view.add_item(incidents_panel.IncidentAction("attachme", incident_id))
        row = await self.db.fetchone("SELECT * FROM incident_messages WHERE incident_id = ? AND kind = 'log_card'", (incident_id,))
        try:
            if row:
                await ch.get_partial_message(row["message_id"]).edit(embed=embed, view=view)
                return
        except discord.NotFound:
            pass
        except discord.HTTPException as err:
            log.warning("Could not edit incident card: %s", err)
            return
        try:
            msg = await ch.send(embed=embed, view=view)
            await self.db.execute(
                "INSERT INTO incident_messages(incident_id, kind, channel_id, message_id) VALUES (?, 'log_card', ?, ?) "
                "ON CONFLICT(incident_id, kind) DO UPDATE SET channel_id = excluded.channel_id, message_id = excluded.message_id",
                (incident_id, ch.id, msg.id))
        except discord.HTTPException as err:
            log.warning("Could not post incident card: %s", err)

    async def _on_assessed(self, e: ev.Event) -> None:
        inc = await self.core.incidents.get(e.data["incident_id"])
        if inc is None or inc["status"] == "closed":
            return
        needs_approval = inc["priority_status"] == "recommended" and inc.get("ai_priority")
        if not needs_approval and not inc.get("supervisor_recommended"):
            return
        marker = f"{inc.get('ai_priority')}:{inc.get('supervisor_recommended')}"
        row = await self.db.fetchone("SELECT marker FROM incident_messages WHERE incident_id = ? AND kind = 'supervisor_alert'", (inc["id"],))
        if row and row["marker"] == marker:
            return
        view = discord.ui.View(timeout=None)
        view.add_item(incidents_panel.IncidentAction("view", inc["id"], label="Open Incident"))
        if needs_approval:
            view.add_item(incidents_panel.IncidentAction("approve", inc["id"]))
            view.add_item(incidents_panel.IncidentAction("priority", inc["id"]))
        text = (f"🛡️ **Incident #{inc['number']}** — {inc.get('nature') or 'Unknown'} at {inc.get('location') or 'location unavailable'}.\n"
                f"AI recommends **{PRIORITY_LABEL.get(inc.get('ai_priority') or '', '?')}** priority, {inc.get('recommended_units')} unit(s)."
                + (" **Supervisor approval required.**" if needs_approval else " Supervisor notification recommended."))
        msg = await self.notify_supervisors(e.guild_id, text, view=view)
        if msg:
            await self.db.execute(
                "INSERT INTO incident_messages(incident_id, kind, channel_id, message_id, marker) VALUES (?, 'supervisor_alert', ?, ?, ?) "
                "ON CONFLICT(incident_id, kind) DO UPDATE SET message_id = excluded.message_id, marker = excluded.marker",
                (inc["id"], msg.channel.id, msg.id, marker))

    async def _on_backup_requested(self, e: ev.Event) -> None:
        if e.data.get("urgency") == "emergency":
            unit = await self.core.units.unit(e.data["unit_id"]) if e.data.get("unit_id") else None
            await self.notify_supervisors(e.guild_id, f"🆘 **EMERGENCY backup requested** by `{unit['callsign'] if unit else 'unknown unit'}`.")

    async def _on_announcement(self, e: ev.Event) -> None:
        status = e.data.get("status")
        if status not in {"played", "muted", "failed"}:
            return
        row = await self.db.fetchone("SELECT * FROM announcements WHERE id = ?", (e.data["announcement_id"],))
        if row is None or row["kind"] == "voice_reply":
            return
        await self.logbook.radio(e.guild_id, row["text"], status=status, error=row["last_error"])

    # ------------------------------------------------------------------ voice helpers
    def _member_lookup(self, guild_id: int, user_id: int) -> Optional[discord.Member]:
        guild = self.get_guild(guild_id)
        return guild.get_member(user_id) if guild else None

    async def _voice_log(self, guild_id: int, speaker: str, transcript: str, reply_text: str) -> None:
        await self.logbook.radio(guild_id, reply_text, speaker=speaker, transcript=transcript)

    # ------------------------------------------------------------------ setup helpers
    async def apply_private_channel_permissions(self, guild: Optional[discord.Guild]) -> None:
        if guild is None:
            return
        cat_id = await self.db.get_config(guild.id, "private_category_id")
        cat = guild.get_channel(int(cat_id)) if cat_id else None
        if not isinstance(cat, discord.CategoryChannel):
            return
        roles = await self.permissions.role_map(guild.id)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_messages=True)}
        for group in ("admin", "supervisor", "dispatcher"):
            for rid in roles.get(group, []):
                role = guild.get_role(rid)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        try:
            await cat.edit(overwrites=overwrites)
            for ch in cat.channels:
                await ch.edit(sync_permissions=True)
        except discord.HTTPException as e:
            log.warning("Could not update private channel permissions: %s", e)

    async def repair_panels(self, guild: discord.Guild) -> list[str]:
        restored = []
        locations = await self.db.get_config(guild.id, "panel_channels", {}) or {}
        fallback = await self.channel_for(guild.id, "admin_channel_id")
        for panel in await self.panels.missing(guild.id):
            ch = self.get_channel(int(locations[panel])) if panel in locations else fallback
            if ch is None:
                continue
            await self.panels.post(guild.id, panel, ch)
            restored.append(panel)
            await asyncio.sleep(0.5)
        return restored

    async def _on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        from bot.ui import reply
        original = getattr(error, "original", error)
        if isinstance(error, app_commands.CheckFailure):
            return await reply(interaction, "⛔ You can't use this command.", error=True)
        if isinstance(error, app_commands.CommandOnCooldown):
            return await reply(interaction, f"⏳ Slow down — try again in {error.retry_after:.0f}s.", error=True)
        from community.common import ServiceError
        from security.validation import ValidationError
        if isinstance(original, (ValidationError, ServiceError)):  # expected, user-facing problems
            return await reply(interaction, f"⚠️ {original}", error=True)
        log.error("Command error in /%s", interaction.command.qualified_name if interaction.command else "?", exc_info=original)
        try:
            if self.community and interaction.command:
                await self.community.analytics.record_command(interaction.guild_id, interaction.user.id,
                                                              interaction.command.qualified_name, False)
                if interaction.guild_id:
                    await self.logbook.write(interaction.guild_id, "errors", f"Command error: /{interaction.command.qualified_name}",
                                             description=f"`{type(original).__name__}` — details are in the bot's log file.")
        except Exception:
            pass
        await reply(interaction, "⚠️ Something went wrong while processing that. The error was logged for administrators.", error=True)

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        log.exception("Unhandled error in %s", event_method)

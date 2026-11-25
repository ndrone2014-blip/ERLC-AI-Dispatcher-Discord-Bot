# 🚨 Pulse Dispatch | ERLC

An intelligent, realistic dispatch operation / Computer-Aided Dispatch system for **Emergency Response: Liberty County**
roleplay communities that runs **entirely inside Discord** — no website, no web dashboard.

> 🦃 **Thanksgiving Update (v3.0.0)** — faster dispatch, natural-language understanding, 15 AI providers with
> automatic failover, professional embeds & logging, optional dashboards, expanded units and 30+ new commands.
> See **[CHANGELOG.md](CHANGELOG.md)**.

* Detects **in-game 911 calls automatically** through the official ER:LC API and announces them **out loud** in a Discord
  voice channel with a radio-style dispatcher voice.
* Assesses every call (Google Gemini free tier, with automatic fallback to other providers or deterministic rules) and
  recommends **priority, unit count and departments** — supervisors approve or override.
* Reassesses when new information arrives and announces significant escalations.
* **Understands plain language** in any word order, with typos, slang, abbreviations and 10-codes:
  *“We have a vehicle crash at the bank”*, *“Vehicle accident, bank location”* and *“Need EMS at the bank for a crash”*
  all open (or join) the same incident. Missing details are asked for. Works with **no AI at all**.
* Officers manage themselves by **voice** ("Dispatch, I'm 10-8", "attach me to incident 104"), text or commands.
* **Optional** dashboards (11 panels), professional logging (14 event types), police reports, BOLOs, permissions,
  statistics, backups — every server configured independently.

> ⚠️ Roleplay system only. It is **not** a real emergency service.

---

## 1. Requirements (Windows)

| Item | Notes |
|---|---|
| Windows 10/11 PC that stays on | The bot only runs while this PC and its internet connection are on. Free 24/7 hosting is not provided. |
| **Python 3.12** | `winget install Python.Python.3.12` |
| **FFmpeg** | `winget install Gyan.FFmpeg` |
| A Discord bot | [Discord Developer Portal](https://discord.com/developers/applications) |
| ER:LC private server API key | In-game: Settings → search "API" |
| Google Gemini API key (free) | <https://aistudio.google.com/apikey> |

**Put the project in a short folder path**, for example `C:\ERLC-Dispatch`. Very long paths break Windows DLL loading for
the local voice engine (onnxruntime: *"The filename or extension is too long"*).

## 2. Create the Discord bot

1. Developer Portal → **New Application** → **Bot** → **Reset Token** → copy the token.
2. On the Bot page enable the privileged intents **Server Members Intent** and **Message Content Intent**
   (needed for the plain-language `#dispatch` channel).
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions: *Manage Channels, View Channels,
   Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages, Connect, Speak, Use Voice Activity*.
4. Open the generated URL and add the bot to your server.

## 3. Install

```bat
git clone https://github.com/ndrone2014-blip/ERLC-AI-Dispatcher-Discord-Bot.git C:\ERLC-Dispatch
cd C:\ERLC-Dispatch
setup.bat
```

`setup.bat` creates `.venv`, installs `requirements.txt`, downloads the default Piper voice (~60 MB) and creates `.env`.

## 4. Configure `.env`

Open `.env` in Notepad and fill in at least:

```ini
DISCORD_TOKEN=your bot token
GEMINI_API_KEY=your gemini key
DEV_GUILD_ID=your server id   (optional: makes slash commands appear instantly)
FFMPEG_PATH=C:\Users\<you>\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe   (only if "ffmpeg" on PATH is an old copy)
```

The **ER:LC server key is best entered from Discord** (step 6). It is then stored encrypted in the database.
`SECRET_KEY` may be left empty: a key is generated in `data/secret.key` — **back that file up** together with
`data/dispatch.db`. Never share `.env`.

## 5. Start / stop

* **Start:** double-click `start.bat` (or `.venv\Scripts\python.exe main.py`).
* **Stop:** press **Ctrl+C** in the window (graceful shutdown: voice disconnects, queues are kept) or close the window.
* Logs: `logs\dispatch.log` (secrets are redacted).

## 6. First-time setup in Discord

Run **`/setup-guide`** in Discord for an interactive version of these steps (with buttons that open the ER:LC key form
and the dashboard picker).

1. Run **`/setup run`** (needs *Manage Server*) and pick what your server needs — **dashboards are optional**:

   | Preset | What you get |
   |---|---|
   | **Dispatch Only** | `#dispatch` (talk to dispatch in plain language) + 📻 Dispatch Radio. No panels. |
   | **Dispatch + Logs** | Dispatch Only + `#dispatch-log` with live incident cards and log embeds. |
   | **Full Dispatch System** | The complete layout with all 11 panels (`#active-calls`, `#unit-board`, `#bolo-board`, `#citizen-services`, private `#dispatch-control`, `#police-reports`, `#department-notices`, `#dispatch-stats`). |
   | **Custom** | Choose exactly which panels you want. |

   Change later with `/dashboard enable|disable|preset`. **Logging always works**: every event is stored and can be
   viewed with `/logs view`, even with no log channel and every dashboard off.
2. **`/settings erlc-key`** → paste your ER:LC server key (tested before it is saved, stored encrypted).
3. **`/permissions map`** → map your Discord roles to *Administrator, Dispatch Supervisor, Dispatcher, Officer, Fire/EMS*.
4. *(Optional)* **`/ai-provider list`** → add a free AI key (OpenRouter, Groq, Gemini, Cerebras…). Dispatch works without one.
5. Officers: **`/unit-register`**, then **`/unit-status 10-8`**.
6. **`/voice join`** (or the Voice panel) for spoken radio traffic; **`/test call`** to try the pipeline with **TEST DATA**.
7. **`/logs settings`** → choose which events are logged and where (`/logs channel`, `/logs create`, `/logs route`).

Deleted a panel by accident? `/setup repair`.

## 7. How the automatic 911 pipeline works

```
ER:LC  GET /v2/server?EmergencyCalls=true&Players=true   (every ERLC_POLL_SECONDS, rate-limit aware)
   │   (optional verified Event Webhook just triggers an immediate poll)
   ▼
dedupe by (CallNumber, StartedAt) ─► new API-VERIFIED incident (real location + description only)
   ▼
instant local assessment (natural-language engine) ─► spoken at once; AI reassesses in the background
   (your AI providers in order / Automatic mode, with failover; validated against configured departments/units)
   ▼
announcement text (AI wording accepted only if it keeps every real fact; otherwise a template)
   ▼
DB-backed priority queue ─► Piper TTS ─► radio beeps/filter ─► Discord voice
   ▼
#active-calls panel, #dispatch-log card, supervisor alert (if approval needed), history
```

* If voice is disconnected, calls are still recorded, shown and logged; announcements wait in the queue.
* If AI is unavailable, the local language engine + varied radio phrasing keep dispatch working at full speed.
* Reports typed in Discord are always labelled **UNVERIFIED**; only calls detected through the ER:LC API are verified.
* A report that matches an active incident (same kind of call, same place, within 30 min) is **merged** into it
  instead of creating a duplicate; the extra caller's details are added and the call is reassessed.
* Unanswered calls are repeated (configurable interval/max/min priority) until a unit attaches or the call ends.
* Calls found on the very first sync that are older than 15 minutes are recorded and shown but not spoken (no backlog flood).

## 8. Voice commands

Say the wake word **"Dispatch"** first (configurable). Examples: *I'm 10-8* · *show me 10-7* · *I'm 10-23* ·
*attach me to incident 104* · *I'm clear from that incident* · *request backup* · *what's the highest priority call?* ·
*what calls are active?* · *how many units are on the current incident?* · *show me available units*.
Speakers are identified **only by their Discord account** (never by voice); unregistered speakers are asked to register.
Every action is confirmed only after the database records it. The same works in text: in `#dispatch` or with
`/dispatch ask` — and citizens can report emergencies there in their own words (*“there's a fire at the old warehouse
on 5th street”*). If the location or nature is missing, dispatch asks for it.

See **[docs/VOICE.md](docs/VOICE.md)** for an important note about Discord's end-to-end voice encryption (DAVE).

## 9. Slash commands

| Category | Commands |
|---|---|
| **Dispatch** | `/dispatch ask\|notice\|skip` · `/911` · `/calls` · `/incident view\|create\|note\|priority\|close\|history` · `/incident-view` · `/incident-update` · `/incident-close` · `/priority` · `/bolo create\|list\|clear` · `/broadcast` · `/report file\|status\|view\|attach` · `/test call` |
| **Units** | `/unit-register` · `/unit-status` · `/unit-assign` · `/unit-unassign` · `/unit-list` · `/available-units` · `/backup` · `/attach` · `/detach` · `/unit register\|status\|remove` · `/status` |
| **Administration** | `/setup-guide` · `/setup run\|repair\|panel` · `/settings view\|response-style\|agency-name\|dispatch-channel\|follow-up-questions\|duplicates\|approval-threshold\|embed\|callsign-format\|category-add\|category-remove\|erlc-key` · `/dashboard status\|enable\|disable\|preset` · `/logs settings\|channel\|create\|toggle\|route\|view` · `/departments list\|add\|remove\|status-add\|status-remove` · `/permissions view\|map\|set` · `/reset-settings` |
| **AI** | `/ai-status` · `/ai-provider list\|guide\|key\|remove-key\|prefer\|mode\|enable` · `/ai-test` · `/ai-model` · `/ai-failover` · `/ai status\|test\|primary` |
| **Information** | `/help` · `/about` · `/statistics` · `/stats` · `/ping` · `/version` · `/erlc` · `/voice join\|leave\|listen\|mute\|test\|diagnose` |

Destructive actions (`/incident-close`, `/reset-settings`) ask for confirmation. Permissions are enforced on every
command and button; regular members can never change administrator settings.

## 10. AI providers (free-first)

Dispatch never *needs* AI: the local language engine handles reports and officer traffic instantly. AI adds deeper
interpretation and assessments. Add keys **per server from Discord** with `/ai-provider key` (encrypted, never shown
again, never shared with other servers) or for every server in `.env`. Switch providers, models, Automatic mode and
failover at any time — no restart needed.

| Provider | Cost | Get a key |
|---|---|---|
| Groq | 🟢 Free tier (per-model limits, very fast) | <https://console.groq.com/keys> |
| Cerebras | 🟢 Free tier (~1M tokens/day) | <https://cloud.cerebras.ai/> |
| Google Gemini | 🟢 Free tier (AI Studio) | <https://aistudio.google.com/apikey> |
| **OpenRouter** | 🟢 Free `:free` models — 20 req/min, 50 req/day (1,000/day after a one-time $10 credit purchase) | <https://openrouter.ai/settings/keys> |
| Mistral AI | 🟢 Free Experiment plan (phone verification) | <https://console.mistral.ai/api-keys> |
| SambaNova Cloud | 🟢 Free tier | <https://cloud.sambanova.ai/apis> |
| NVIDIA NIM | 🟢 Free for prototyping (Developer Program, ~40 req/min) | <https://build.nvidia.com/> |
| GitHub Models | 🟢 Free with any GitHub account (low daily limits) | <https://github.com/settings/personal-access-tokens/new> |
| Cloudflare Workers AI | 🟢 10,000 free Neurons/day (needs Account ID) | <https://dash.cloudflare.com/profile/api-tokens> |
| Cohere | 🟡 Free **trial** key (rate limited, non-commercial) | <https://dashboard.cohere.com/api-keys> |
| Hugging Face | 🟡 Tiny monthly credit only (~$0.10), then paid | <https://huggingface.co/settings/tokens> |
| Ollama | 🖥️ Local, free, needs a capable PC | <https://ollama.com/download> |
| Together AI | 🔴 Paid (credits required) | <https://api.together.ai/settings/api-keys> |
| DeepSeek | 🔴 Paid (balance top-up required) | <https://platform.deepseek.com/api_keys> |
| Custom OpenAI-compatible | depends | set `OPENAI_COMPAT_*` in `.env` |

`/ai-provider guide name:<provider>` shows step-by-step instructions in Discord. Free-tier limits change often —
always confirm on the provider's page.

* **Automatic mode** (`/ai-provider mode`) tries the healthiest free provider first; **Manual** uses your order (`/ai primary`).
* **Failover** (`/ai-failover`, on by default): a provider that hits its rate limit, runs out of free credit, rejects its key
  or goes down is paused for the time it asks for and the next provider answers instantly. Retired models are replaced
  automatically. Failures are logged once (no spam) in the *AI provider failures* log.
* Identical requests are answered from a short-lived cache; per-minute limits are tracked per key.
* Keys never appear in Discord messages, logs or error text (redacted everywhere).

## 11. Testing

```bat
run_tests.bat
```

The suite (180+ tests) covers natural-language recognition (word order, spelling, slang, locations, follow-ups),
duplicate merging, server isolation, AI failover/modes/caching, key redaction, the ER:LC client against a local mock server, webhook Ed25519 verification, duplicate
prevention, crash recovery, incident creation/escalation, priority approval, AI fallback/cooldowns/quotas, intent
parsing, permissions, encryption, persistence across restarts, every panel/modal/command against Discord limits, the
radio audio chain through real FFmpeg, Opus decoding, echo prevention and the announcement queue.
Optional: `set PIPER_TEST_VOICE=data\voices\en_US-ryan-medium.onnx` and `set RUN_STT_TEST=1` (downloads Whisper).

## 12. Troubleshooting

| Problem | Fix |
|---|---|
| Slash commands don't appear | Set `DEV_GUILD_ID` and restart, or wait up to 1 h for global sync. |
| "DISCORD_TOKEN is missing" | Fill in `.env` next to `main.py`. |
| `ImportError: DLL load failed … filename or extension is too long` | Move the folder to a short path such as `C:\ERLC-Dispatch` and re-run `setup.bat`. |
| Bot joins voice but is silent | Check `FFMPEG_PATH` (an old ImageMagick `ffmpeg` on PATH can shadow the real one); run **Test Voice**. |
| "no Piper voice found" | Re-run `setup.bat` or `python -m piper.download_voices en_US-ryan-medium --data-dir data\voices`. Windows' built-in voice is used meanwhile. |
| ER:LC panel shows `error … code 2002` | Invalid/expired server key — set it again. Code `3002` = server offline (no players). |
| Rate limited (`4001`) | The bot backs off automatically; increase `ERLC_POLL_SECONDS`. |
| Gemini "quota" errors | Free tier exhausted — the bot switches to the secondary provider/rules; it resets daily. |
| Officer speech not recognised | See docs/VOICE.md; check *Receive diagnostics* in the Voice panel. |
| Bot ignores messages in `#dispatch` | Enable **Message Content Intent** in the Developer Portal; check `/settings view` → dispatch channel. |
| Gemini "high demand" (503) | Google is overloaded on that model. The bot now tries other free Gemini models automatically, then your other providers. Gemini 2.5 models were retired on 16 Oct 2026 — use a 3.x model in `GEMINI_MODEL`. |
| "Payment required / free credits used up" | That provider's free allowance is used up (e.g. Hugging Face). Failover moves on; add another free key. |
| An OpenRouter model costs money | Only model IDs ending in `:free` (or `openrouter/free`) are free. `/ai-model` warns you. |
| Want fewer log messages | `/logs settings` → untick event types, or `/logs route` busy types to their own channel. |

## 13. Project layout

```
main.py  config.py  requirements.txt  .env.example  setup.bat  start.bat  run_tests.bat
bot/        Discord client, embeds (Pulse design system), logbook, text dispatch, UI helpers, slash-command cogs
panels/     the 11 persistent panels (dashboard, incidents, units, voice, citizen, reports, bolos, ai, erlc, admin, stats)
dispatch/   incidents, calls intake, natural-language engine (nlu), radio phrasing, units, assignments,
            recommendations, escalation, announcements, BOLOs, reports
erlc/       official API client, models, auth, webhook receiver, synchronization loop
ai/         provider manager (modes, failover, cache), provider catalogue, Gemini, Ollama, OpenAI-compatible,
            assessment, conversation/intents
voice/      connection, receive sink (DAVE), speech-to-text, text-to-speech, radio processing, queue player, voice commands
database/   SQLite connection, migrations, backups
security/   permissions, validation, audit log, encrypted secrets
core/       event bus, logging (secret redaction), version
docs/       implementation plan & checklist, voice notes
tests/
```

---

Developed by Drone Hive Studios | Developer @ndrone40

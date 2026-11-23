# ER:LC AI Dispatcher (Discord CAD)

An AI-powered police dispatch / Computer-Aided Dispatch system for **Emergency Response: Liberty County** roleplay
communities that runs **entirely inside Discord** — no website, no web dashboard.

* Detects **in-game 911 calls automatically** through the official ER:LC API and announces them **out loud** in a Discord
  voice channel with a radio-style dispatcher voice.
* Assesses every call (Google Gemini free tier, with automatic fallback to other providers or deterministic rules) and
  recommends **priority, unit count and departments** — supervisors approve or override.
* Reassesses when new information arrives and announces significant escalations.
* Officers manage themselves by **voice** ("Dispatch, I'm 10-8", "attach me to incident 104") or with buttons/commands.
* 11 persistent, auto-updating Discord panels; police reports, BOLOs, permissions, audit log, statistics, backups.

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
2. On the Bot page enable the privileged intent **Server Members Intent**.
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

1. Run **`/setup run`** (needs *Manage Server*). It creates:
   * **📡 DISPATCH CENTER** – `#dispatch-dashboard`, `#active-calls`, `#unit-board`, `#bolo-board`, `#dispatch-log`,
     `#citizen-services`, voice **📻 Dispatch Radio**
   * **🔒 DISPATCH ADMIN** (private) – `#dispatch-control` (Voice, ER:LC, AI, Administration panels), `#police-reports`,
     `#department-notices`, `#dispatch-stats`
2. `#dispatch-control` → **ER:LC Integration → Set API Key** (the key is tested before it is saved).
3. **Administration → Roles & permissions**: map your Discord roles to *Administrator, Dispatch Supervisor, Dispatcher,
   Officer, Fire/EMS*. Mapped staff roles can see the private category.
4. **Voice Control** → choose *📻 Dispatch Radio* (the bot joins and reconnects automatically) → **Start Listening**.
5. Officers: `#unit-board` → **Register / Update My Unit**, then **10-8**.
6. **Administration → Test mode → Simulate 911 Call** to see the whole pipeline with clearly labelled **TEST DATA**.

Deleted a panel by accident? `/setup repair`.

## 7. How the automatic 911 pipeline works

```
ER:LC  GET /v2/server?EmergencyCalls=true&Players=true   (every ERLC_POLL_SECONDS, rate-limit aware)
   │   (optional verified Event Webhook just triggers an immediate poll)
   ▼
dedupe by (CallNumber, StartedAt) ─► new API-VERIFIED incident (real location + description only)
   ▼
assessment: Gemini → fallback provider → rule engine   (validated against configured departments/units)
   ▼
announcement text (AI wording accepted only if it keeps every real fact; otherwise a template)
   ▼
DB-backed priority queue ─► Piper TTS ─► radio beeps/filter ─► Discord voice
   ▼
#active-calls panel, #dispatch-log card, supervisor alert (if approval needed), history
```

* If voice is disconnected, calls are still recorded, shown and logged; announcements wait in the queue.
* If AI is unavailable, rule-based assessment + templates keep dispatch working.
* Unanswered calls are repeated (configurable interval/max/min priority) until a unit attaches or the call ends.
* Calls found on the very first sync that are older than 15 minutes are recorded and shown but not spoken (no backlog flood).

## 8. Voice commands

Say the wake word **"Dispatch"** first (configurable). Examples: *I'm 10-8* · *show me 10-7* · *I'm 10-23* ·
*attach me to incident 104* · *I'm clear from that incident* · *request backup* · *what's the highest priority call?* ·
*what calls are active?* · *how many units are on the current incident?* · *show me available units*.
Speakers are identified **only by their Discord account** (never by voice); unregistered speakers are asked to register.
Every action is confirmed only after the database records it. The same works in text: `/dispatch ask`.

See **[docs/VOICE.md](docs/VOICE.md)** for an important note about Discord's end-to-end voice encryption (DAVE).

## 9. Slash commands

`/setup run|repair|panel` · `/911` · `/calls` · `/incident view|create|note|priority|close|history` · `/attach` ·
`/detach` · `/backup` · `/unit register|status|remove` · `/status` · `/bolo create|list|clear` ·
`/report file|status|view|attach` · `/dispatch ask|notice|skip` · `/voice join|leave|listen|mute|test` ·
`/ai status|test|primary` · `/stats` · `/erlc` · `/test call` · `/help`

## 10. AI providers

Gemini is primary (free tier). Add fallbacks in `.env` (`OLLAMA_BASE_URL` for free local models, or any OpenAI-compatible
endpoint) and pick primary/secondary/model in the **AI Configuration** panel. A provider that hits its quota or errors is
paused for the time the API asks (or a safe default) and is never retried in a loop. `GEMINI_DAILY_REQUEST_LIMIT` is a
local safety cap — set it at or below your actual free-tier quota.

## 11. Testing

```bat
run_tests.bat
```

The suite (65+ tests) covers the ER:LC client against a local mock server, webhook Ed25519 verification, duplicate
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

## 13. Project layout

```
main.py  config.py  requirements.txt  .env.example  setup.bat  start.bat  run_tests.bat
bot/        Discord client, UI helpers, slash-command cogs
panels/     the 11 persistent panels (dashboard, incidents, units, voice, citizen, reports, bolos, ai, erlc, admin, stats)
dispatch/   incidents, calls intake, units, assignments, recommendations, escalation, announcements, BOLOs, reports
erlc/       official API client, models, auth, webhook receiver, synchronization loop
ai/         provider manager, Gemini, Ollama, OpenAI-compatible, assessment, conversation/intents
voice/      connection, receive sink (DAVE), speech-to-text, text-to-speech, radio processing, queue player, voice commands
database/   SQLite connection, migrations, backups
security/   permissions, validation, audit log, encrypted secrets
core/       event bus, logging
docs/       implementation plan & checklist, voice notes
tests/
```

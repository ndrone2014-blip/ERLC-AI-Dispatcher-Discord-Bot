# Changelog

## 🌐 Dashboard on Netlify (2026-11-27)
- The web dashboard can now be hosted on **Netlify** (`netlify.toml` + an edge function that forwards sign-in and data
  requests to the bot through a tunnel). One address for staff, same sign-in, permissions and live updates.
- New `DASHBOARD_PROXY_SECRET`: when set, the bot only answers dashboard requests that came through your Netlify site.
- Live updates fall back to refreshing every 20 seconds if a host cuts the connection.
- Guide: **docs/NETLIFY.md**.

## 🖥️ Pulse Dispatch | ERLC — Command Center Update · v4.0.0 (2026-11-27)

The biggest update yet: Pulse Dispatch grows from a dispatch bot into a full ER:LC community platform with a web
dashboard. **Everything from v3 keeps working** (settings, panels, incidents, units, keys and commands). New database
tables are added automatically on first start.

> Setup: add `DISCORD_CLIENT_SECRET` (and a redirect) for the dashboard — see **docs/DASHBOARD.md**.
> Roblox verification works with no setup — see **docs/ROBLOX_VERIFICATION.md**.
> Give the bot **Manage Roles, Manage Nicknames, Moderate Members, Kick Members, Ban Members** for the new features.

### 🖥️ Web dashboard (served by the bot itself)
- Sign in with Discord; you only see servers and sections your roles allow, checked on the server for every request.
- **Dashboard** with a status band and panels you can choose and reorder; **Analytics** (today → 90 days or custom,
  12 metrics from recorded data only); live **Activity feed**.
- **Dispatch center**: live incidents (priority-coloured), create/edit incidents, assign/clear units, notes,
  priorities, custom incident statuses, unit status changes, backups, BOLOs. Updates arrive live.
- Incident history, units, dispatch settings (dispatcher behaviour, departments, unit statuses, incident statuses).
- Members, verification, applications (review + drag-free form builder), sessions, staff, shifts, leave requests,
  moderation, automations, scheduled messages, log viewer with CSV export, audit log, log routing, embed builder with
  live preview, welcome/leave messages, announcements, slash command list, appearance, AI providers (write-only keys),
  dispatcher personality, AI usage, channels, ER:LC key, permissions matrix, notifications, integration status, sessions.
- Notifications bell, **Ask Pulse**, light/dark mode, searchable collapsible navigation, mobile layout
  (tables become cards), keyboard accessible, reduced-motion aware. A red/blue light bar pulses during critical incidents.
- Security: HttpOnly hashed sessions, CSRF tokens + origin checks, strict CSP, rate limits, write-only secrets,
  per-server isolation, audit logging.

### ✅ Roblox verification
- **Profile code** method (no setup): random word phrase in the Roblox *About* section, checked through the public
  Roblox Users API. **Sign in with Roblox** (official OAuth 2.0 + PKCE) optional.
- Verified/unverified roles, nickname formats, minimum account age, banned-account check, DM on success, persistent
  **Verify** panel, staff link/unlink/sync/lookup, full verification history and logs. One Roblox account per member per server.

### 👥 Staff, shifts & sessions
- Staff directory and profiles, rank ladder with automatic rank roles, promote/demote, departments, confidential notes,
  history, leave requests with approval (leave starts/ends on its dates), inactivity alerts.
- Shifts with types, notes, on-duty role, auto-close after N hours, leaderboard.
- Sessions: schedule, announce (with Join/Leave buttons), reminders with role ping and DMs, participants.

### 📝 Applications
- Custom forms with 6 answer types (short, long, multiple choice, dropdown, yes/no, required confirmation), up to
  25 questions, role restrictions, cooldowns, require-verification, approval role, review channel.
- 7 ready-made templates (Staff, Police, Sheriff, Fire, EMS, Dispatch, Civilian) that start closed.
- Review workflow: under review, request more information (applicant answers with `/application respond`), approve,
  deny, archive, internal notes, DM results. `/apply` is a step-by-step wizard in Discord.

### 🛡️ Moderation
- `/warn`, `/timeout`, `/kick`, `/ban` (even users who left), `/unban`, `/history`, `/case view|revoke|edit`,
  `/report-member`. Numbered cases with evidence, outcome and source.
- Role-hierarchy checks, confirmation for kicks/bans, DM before the action, appeal info, automatic escalation after N warnings.
- A failed Discord action is stored as a **failed** case with the reason — never shown as a success.

### ⚙️ Automation, notifications, embeds
- Event automations (18 triggers, 7 actions, conditions, placeholders, templates) with duplicate protection,
  loop protection and visible failures. Scheduled (repeating) messages.
- Central notifications with per-permission visibility, read/unread state, dedupe and optional Discord channels.
- Embed templates with Discord's limits enforced, link buttons, 8 presets; welcome/leave messages with auto-roles.

### 🧠 Pulse Assistant (`/ask` and "Ask Pulse")
- Recognises intents in any phrasing ("show active calls", "are there any incidents right now?", "who's on shift?",
  "start my shift", "am I verified?") and answers from live data in under a millisecond — no AI request.
- Respects the asker's permissions; actions (close incident, start/end shift) always ask for confirmation.
- Only conversational questions go to the AI, grounded in the real situation and told never to invent facts.

### 📋 Logging
- 13 new log categories: member joins/leaves, role changes, verification, moderation, applications, staff, shifts,
  sessions, automations, commands, security, dashboard, system errors (noisy ones start off).
- `/logs toggle|route|view` use autocomplete now (there are more than 25 categories); `/logs settings` has two menus.

### 🧩 Other
- New permission groups: Management, Moderator, Department Leader, Staff Member (plus ~35 new permissions).
- New commands: `/verify`, `/verification`, `/profile`, `/member`, `/staff`, `/shift`, `/session`, `/apply`,
  `/application`, `/warn`, `/timeout`, `/kick`, `/ban`, `/unban`, `/history`, `/case`, `/report-member`, `/ask`,
  `/server-info`, `/bot-info`, `/announcement`, `/dashboard web` (67 top-level commands in total).
- Command usage tracking, member directory mirror, incident dispatcher + custom status fields.
- Tests: 46 new tests for the platform services and dashboard API (permissions, server isolation, honest failures).

## 🔒 Close all incidents (2026-11-26)
- **New `/incident-close-all`.** Closes every active incident at once (asks for confirmation, needs the same
  permission as `/incident-close`). Assigned units are cleared and all queued radio traffic for those calls
  (repeats, 911 reads, escalations) is cancelled, so the dispatcher stops repeating old calls. Calls still listed
  in-game are not re-opened. An optional `reason` is saved in each incident's history.

## 🎙️ Busy voice channel fix (2026-11-25)
- **Dispatch keeps answering in crowded calls.** Speech-to-text used to handle every clip from everyone in one line,
  so with many people talking, officers' "Dispatch, …" clips waited too long and were silently dropped.
  - Registered officers are now transcribed before everyone else.
  - One person talking non-stop can only have 3 clips waiting, so they can't block the channel.
  - Chatter is screened on its first 2.5 s only (the wake word is at the start), so the backlog clears faster.
  - Bigger queue (60 clips) and a slightly longer wait before a clip counts as outdated (10 s).
- **Talking over the dispatcher works.** Listening no longer switches off while the dispatcher speaks (in a busy
  channel it is speaking a lot). The *Half-duplex (echo guard)* setting now only ignores clips that repeat what the
  dispatcher just said.
- **Faster replies.** Repeated dispatcher lines are cached and start playing instantly; the dispatcher voice is
  loaded at startup instead of on the first reply; end of a transmission is detected sooner (0.5 s of silence);
  the fast speech pass skips an extra voice-detection step; a quiet channel never needs two passes per command.
- **Fastest replies yet.** Replies are rendered with the radio effect in memory and played directly, so no FFmpeg
  program has to start for every reply; finished transmissions are cached, so repeated lines play instantly.
  End of a transmission is detected after 0.4 s of silence, and speech-to-text uses more CPU cores.
- **Talk like a real radio.** Say "Dispatch" and pause - the next thing you say is taken as the command (no second
  "Dispatch" needed). If you say nothing more, dispatch answers "*callsign*, go ahead."
- **Clearer diagnostics.** `/voice diagnose` and the Voice panel show the speech queue: heard, answered, ignored
  (no wake word), dropped (too busy), echo ignored and the last thing heard.
- **Listening can't silently die** from one bad voice packet any more (busy calls produce more of them).

## 🦃 Pulse Dispatch | ERLC — Thanksgiving Update · v3.0.0 (2026-11-26)

A major upgrade focused on speed, understanding, free AI, professional logging and a realistic dispatch experience.
Everything that worked before still works: existing settings, panels, incidents, units, keys and commands are kept.

### ⚡ Speed
- New **local natural-language engine**: reports and officer traffic are understood in well under a millisecond, with
  no AI request at all. AI is only consulted when the local engine cannot interpret a message.
- New calls are announced immediately from the instant local assessment; the AI assessment runs in the background.
- Response cache for repeated AI requests; per-guild settings and encrypted keys are cached in memory.
- New database indexes (incidents, assignments, AI usage, logs).
- Each request is handled independently: 40 simultaneous callers are processed in a few hundred milliseconds.
- Fixed a slow memory leak: secret redaction patterns were added again every time a stored key was read.

### 🧠 Smarter dispatch recognition
- Word-order independent: *“We have a vehicle crash at the bank”*, *“At the bank there's a crash involving a vehicle”*,
  *“Vehicle accident, bank location”*, *“Send police to the bank, there's been an accident”* and
  *“Need EMS at the bank for a crash”* are all understood as the same incident.
- Synonyms, informal language, common misspellings (*acident, colision, ambulence, jewlery…*), abbreviations
  (*mva, gsw, pd, ems, veh, @, p214*) and incident 10-codes (*10-50, 10-71, 10-80…*).
- Location extraction (*“at the bank”*, *“bank location”*, *“the old warehouse on 5th street”*, *“postal 214”*).
- Department requests (*“send police”*, *“need EMS”*) and injuries automatically add EMS and raise the priority.
- **Follow-up questions**: *“there's a fire!”* → *“What is the location?”* → the next answer completes the call.
- **Duplicate protection**: a report matching an active incident (same kind of call, same place) is merged into it.
- Officer requests in any order: *“available show me”*, *“me attach incident 104 to”*.
- Server-specific incident categories (`/settings category-add`).
- New plain-language **`#dispatch` channel**; `/dispatch ask` now also accepts emergency reports.

### 🤖 Free AI provider system (15 providers)
- Groq, Cerebras, Google Gemini, **OpenRouter**, Mistral, SambaNova, NVIDIA NIM, GitHub Models, Cloudflare Workers AI,
  Cohere, Hugging Face, Ollama, Together AI, DeepSeek and any custom OpenAI-compatible endpoint.
- Honest cost labels: 🟢 free tier · 🟡 small credits / trial · 🔴 paid · 🖥️ local. Paid services are never called free.
- Per-server API keys entered privately in Discord (`/ai-provider key`), encrypted, never displayed or logged.
- **Automatic mode**, preferred provider, manual order, per-provider models (`/ai-model`), automatic failover
  (`/ai-failover`), live connection status and the currently active provider (`/ai-status`). No restart needed.
- Retired models are replaced automatically; HTML/odd provider errors can no longer break a request.

### 🖥️ Optional dashboards
- `/setup run` presets: **Dispatch Only**, **Dispatch + Logs**, **Full Dispatch System**, **Custom**.
- `/dashboard status|enable|disable|preset`. Servers set up before this update keep all their panels.

### 📋 Professional logging
- 14 event types: 911 calls, incidents, AI responses, unit assignments, status changes, backup requests, priority changes,
  BOLOs, closures, failed dispatch requests, administrative changes, AI provider failures, radio traffic, ER:LC connection.
- Choose an existing channel or auto-create a private `#pulse-logs`; enable/disable each type; route types to their own channels.
- Every record is stored, so `/logs view` works even without a log channel.
- All dispatch messages and logs are clean, colour-coded embeds with timestamps and the *Pulse Dispatch | ERLC* footer.

### 🎙️ Realistic dispatch
- Varied, professional radio phrasing that never repeats the same sentence back-to-back, built only from real incident facts.
- Response styles: professional, concise, detailed. Optional agency name on the radio.
- *“911 CALL RECEIVED”* cards with incident, location, priority, status and the dispatcher's broadcast.

### 🚓 Units
- New default statuses **Busy (10-6)** and **Returning to Station (10-19)**, added once to existing servers (if you
  delete them they stay deleted). New *specialized* department type (SWAT, K9, air…).
- `/unit-register`, `/unit-status`, `/unit-assign` (with reassignment), `/unit-unassign`, `/unit-list`, `/available-units`.
- Optional callsign rules (`/settings callsign-format`).

### ⚙️ Commands & customization
- New: `/incident-view` `/incident-update` `/incident-close` `/priority` `/broadcast` `/unit-*` `/available-units`
  `/settings …` `/dashboard …` `/logs …` `/departments …` `/permissions …` `/reset-settings` `/ai-status`
  `/ai-provider …` `/ai-test` `/ai-model` `/ai-failover` `/about` `/statistics` `/ping` `/version`, and a new `/help`.
- Autocomplete, dropdowns, buttons and confirmation prompts; permissions enforced everywhere.
- Per-server: response style, agency name, dispatch channel, follow-ups, duplicate window, approval threshold, embed
  accent/compact/footer note, callsign rule, incident categories, departments, statuses, permissions, logging, dashboards.

### 📊 Statistics
- `/statistics`: 911 calls, incidents, open/closed, average time to first unit (24 h and all-time), units available/assigned,
  incidents by priority and by department. Category details are only shown to authorised staff.

### 🎮 ER:LC & 🔐 security
- ER:LC connection changes are logged; error messages point to `/settings erlc-key`; status shown in `/settings view`.
- The bot never pings anyone unless a message explicitly allows a role mention.
- Anything that looks like an API key is redacted from Discord messages, logs and stored errors.

### Limitations (and what was done instead)
- **In-game robberies, pursuits, shootings** are not exposed by the official ER:LC API, so they can never be marked
  verified. Reports from Discord are labelled **UNVERIFIED**; only API calls are verified.
- **Under one second**: local recognition answers in milliseconds. When a message genuinely needs AI, the reply time is
  the provider's network latency (usually 0.3–2 s), so the AI step runs in the background wherever possible.
- **In-game 911 calls are not merged** with each other: each one is a separate record in the ER:LC API. Duplicate merging
  applies to reports typed in Discord.
- **Priority levels** stay the five standard levels (Low → Critical) so every server's data stays comparable. Servers can
  set the supervisor-approval threshold and per-category priorities instead of renaming levels.
- **Ollama / custom endpoint URLs** can only be set in `.env` by the bot owner. Allowing any Discord admin to point the bot
  at an arbitrary URL would let them make the host PC send requests into its own network.
- The `#dispatch` channel needs Discord's **Message Content Intent** enabled in the Developer Portal.
- Free-tier limits are set by each provider and change often; the numbers shown were checked in November 2026.

Developed by Drone Hive Studios | Developer @ndrone40

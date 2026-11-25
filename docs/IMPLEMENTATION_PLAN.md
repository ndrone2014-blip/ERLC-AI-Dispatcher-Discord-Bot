# Implementation plan & development checklist

Status legend: ✅ implemented + automated test · 🟢 implemented, verified offline (needs a live Discord/ER:LC check) ·
⚠️ implemented with a documented limitation · ❌ not possible with the official API (closest alternative described)

## Environment decisions (inspected 2026-11-23)

| Decision | Reason |
|---|---|
| Python 3.12, discord.py 2.7.1 | Spec prefers Python; 2.7.1 implements Discord's mandatory DAVE voice encryption for sending. |
| discord-ext-voice-recv 0.5.2a179 + own DAVE bridge | Only maintained receive extension; it lacks DAVE decryption, which `voice/listener.py` adds using discord.py's session. |
| faster-whisper, Piper (+ Windows SAPI fallback), FFmpeg | Free, local, no paid subscriptions. |
| SQLite via aiosqlite, ordered SQL migrations | Persistent, zero-setup on Windows; per-guild scoping for multi-server. |
| Gemini REST (no SDK) + provider interface | Fewer dependencies; Ollama & OpenAI-compatible fallbacks plug into the same interface. |
| ER:LC v2 `GET /v2/server?EmergencyCalls=true` polling | The only documented source of 911 calls. Webhook payload is undocumented → webhook only triggers a poll. |

## Phase 1 — Foundation
- ✅ Config from `.env`, secrets never logged (redacting log filter), encrypted credential store (Fernet)
- ✅ Database schema, migrations, counters, backups (SQLite online backup API), daily auto-backup task
- ✅ Configurable role-based permissions (7 groups, per-permission overrides), validation, rate limits, duplicate guard, audit log
- ✅ Slash commands (all spec commands + `/test`, `/erlc`, `/help`, `/report`) — validated against Discord limits
- ✅ Persistent panels with stored message IDs, restored after restart, debounced change-only edits, deleted-panel detection + `/setup repair`

## Phase 2 — ER:LC integration
- ✅ Official client: `server-key` / optional `Authorization`, rate-limit headers, 429 `Retry-After`, documented error codes
- ✅ Connection test, status, server info, player count, last sync, errors, monitoring toggle, retry
- ✅ Duplicate-event detection `(guild, CallNumber, StartedAt)`; invalid calls counted and surfaced
- ✅ Backoff on failures; bad keys are not hammered; status shown everywhere ("live verification unavailable")
- ✅ Webhook receiver with Ed25519 verification (official key, `timestamp + raw body`, hex signature) + replay window
- 🟢 Live call against your real server — run **ER:LC → Test Connection** after adding the key

## Phase 3 — Automatic 911 dispatch (highest priority)
- ✅ Detect new in-game calls automatically, extract real location (`PositionDescriptor`, caller postal/street) and description
- ✅ Never invent data ("Location unavailable"), API-verified label only for ER:LC source
- ✅ Incident creation → assessment → announcement queued → panels/log updated → history recorded
- ✅ Crash recovery (call stored but incident missing is re-created), backlog not spoken on first sync
- ✅ Announcement queue in DB: priority ordering, dedupe, survives restarts/voice loss, played time recorded, critical pre-empts routine
- ✅ Configurable repeats; stop when a unit attaches, the in-game call has responders, it ends, or the incident closes
- 🟢 Spoken announcement in your voice channel — use **Test mode** then a real in-game 911 call

## Phase 4 — AI intelligence
- ✅ Gemini provider (free tier, thinking disabled on Flash for speed/quota), Ollama, OpenAI-compatible
- ✅ Primary/secondary selection, manual switching, automatic fallback, persisted cooldowns, local daily + per-minute caps, usage stats, tests, model list
- ✅ Assessment: category, priority, unit count, departments (only configured ones), supervisor flag, reasoning, availability notes, units already assigned
- ✅ Supervisor approval threshold, override with reason, original recommendation preserved, auto-approval below threshold
- ✅ Reassessment on new information; significant-change detection; escalation announcement; previous assessment kept in history
- ✅ AI announcement wording accepted only if all real facts are preserved; template otherwise
- ✅ Rule-based fallback keeps dispatch working with no AI

## Phase 5 — Voice
- ✅ TTS (Windows SAPI verified; Piper code path verified against the installed API, needs a downloaded voice)
- ✅ Radio effects through real FFmpeg; beeps/pauses/static toggles; speed & volume
- ✅ Join/leave/select channel/reconnect watchdog, mute (speech only), listen on/off, test, skip
- ✅ Receive sink: Opus decode, segmentation, bot filtering, half-duplex echo guard, wake word, Discord-ID speaker identity
- ✅ Natural-language intents (rules first, AI second; AI can't invent incident numbers); DB-confirmed replies
- ⚠️ Live speech recognition depends on DAVE decryption of received audio (see docs/VOICE.md) — diagnostics built in

## Phase 6 — Panels
- ✅ 1 Dashboard · 2 Active Incidents · 3 Units · 4 Voice · 5 Citizen Services · 6 Reports · 7 BOLOs · 8 AI · 9 ER:LC · 10 Administration · 11 Statistics
- ✅ Per-incident actions (attach/detach self & others, note/update, backup, edit, reassess, priority approve/override, review unverified, close, history)
- ✅ Reports private to authorised staff, AI summaries labelled as unverified allegations, attachments via `/report attach`
- ✅ BOLO create/update/clear/history + voice announcement

## Phase 7 — Reliability
- ✅ Graceful shutdown, health loop restarts dead sync/announcement tasks, DB error isolation, event-handler isolation
- ✅ Restart persistence test, simultaneous incidents, multiple officers/units, AI failure, API failure/timeout
- 🟢 Real voice disconnect / Discord reconnect behaviour — watch the Voice panel while testing

## Things the official API cannot do (not faked)
- ❌ Bank robberies, pursuits, shootings etc. are **not exposed** as events. Officers can report them; they are labelled **UNVERIFIED** and supervisors can review them. They can never be marked API-verified.
- ❌ The event-webhook payload format is undocumented, so incidents are never built from it.

## Phase 8 — 🦃 Thanksgiving Update (Pulse Dispatch v3.0.0)
- ✅ Local natural-language engine (`dispatch/nlu.py`): word order, synonyms, typos, slang, 10-codes, locations, follow-ups
- ✅ Report intake with duplicate merging (`DispatchCore.report_incident`), plain-language `#dispatch` channel
- ✅ 15 AI providers (`ai/providers/catalog.py`), per-server encrypted keys, Automatic mode, failover toggle, response cache
- ✅ Optional dashboards (setup presets, `/dashboard`), professional logging (`bot/logbook.py`, 14 categories)
- ✅ Pulse embed design system (`bot/embeds.py`), varied radio phrasing (`dispatch/phrases.py`)
- ✅ New statuses / specialized departments, 30+ new commands, per-server customization, statistics
- ✅ Tests: `tests/test_thanksgiving.py`
- 🟢 Live check after restart: `/setup run`, `/ai-provider key`, talk in `#dispatch`, `/logs view`

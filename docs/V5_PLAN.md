# Hive Dispatch v5.0 — Platform Overhaul: audit & implementation plan

## 1. Audit of v4.0 (what exists)

| Area | State before v5 |
|---|---|
| Stack | Python 3 · discord.py 2.7 · aiohttp web server · SQLite (aiosqlite) with ordered migrations (001–004) · vanilla JS dashboard (no build step) · optional Netlify hosting through an edge-function proxy |
| Backend API | `web/api.py`: ~190 routes, each declares a permission; guild id from the URL is checked against real Discord membership (`web/server.py → access()`); CSRF + Origin checks, rate limits, HttpOnly hashed sessions |
| Permissions | `security/permissions.py`: 11 groups mapped to Discord roles, ~60 permission keys, per-permission overrides |
| Live updates | SSE stream (`/api/g/{id}/events`) sends event *names*; the page re-fetches through the permission-checked API |
| Moderation | Discord-side only (warn / timeout / kick / ban / unban / note, cases, reports, escalation). **No in-game (ER:LC) moderation records** |
| Staff | Directory, ranks, notes, leave requests, shifts with on-duty role, leaderboard, overdue auto-close |
| BOLOs | Dispatch-oriented (person / vehicle / other), create / update / clear, history. **No priority, notes, search, pagination, player link or dashboard page** |
| ER:LC | Polls `GET /v2/server?Players&EmergencyCalls` for 911 calls only. **Player list, staff, queue, join logs, mod calls and command logs are not used** |
| Activity | `dispatch_log` (only stores categories that are switched on) + a 15-item config list |
| Dashboard | Light + dark theme, ~45 pages, basic tables/cards |

### Bugs and performance problems found

1. **Full-page refresh (critical).** Every ER:LC poll publishes `erlc.status` with `poll=True` (every few seconds). The SSE stream forwards it, and almost every page sets `S.refresh = () => renderPage(...)`, which clears the page to "Loading" and rebuilds it — scroll position, tabs, open menus and half-typed forms are lost several times a minute.
2. The lightbar re-fetched the whole dispatch board on every incident event.
3. A 401 during any background refresh re-booted the whole app.
4. Activity feed only showed categories that were switched on for Discord logging.
5. On Netlify the `{{v}}` cache-busting placeholder is not replaced (static hosting), so browsers could keep old scripts.
6. Leave requests could be reviewed but not *requested* from the dashboard.

## 2. Plan

**Preserve everything**: no table is dropped or renamed; migration `005_platform.sql` only adds tables/columns. Existing routes keep their paths and payloads; existing pages keep working inside the new shell.

### Backend
- **Migration 005**: `erlc_players`, `erlc_player_events`, `erlc_mod_records`, `player_notes`, `bolo_notes`, `activity_events`; new columns on `bolos` (priority, Roblox player, close reason).
- **ER:LC live** (`erlc/live.py`): the same single poll now also asks for `Staff, JoinLogs, Queue, ModCalls, CommandLogs, KillLogs`. Joins/leaves are detected by comparing the real player list between polls (first poll after start-up is a baseline, never reported as joins). Unsupported extras are switched off automatically. Player directory + lookup, in-game staff from the API's `Permission` field, run commands (`erlc.command` permission), ban list via the documented v1 endpoint when available. Nothing is invented: unavailable data is reported as unavailable.
- **In-game moderation** (`community/erlc_moderation.py`): warn / kick / ban / unban / note against Roblox players, optional execution in-game (`:pm`, `:kick`, `:ban`, `:unban`) with the result recorded, revoke, edit, player profiles (records, notes, BOLOs, sessions, Discord link), statistics, staff moderation records, active punishments.
- **BOLOs**: priority, linked Roblox player, notes, search/filter/pagination, close/reopen, notifications to `bolo.view`, and a *notification only* when a player with an active BOLO joins (never an automatic ban).
- **Staff**: overview with availability (on shift / on leave / available / inactive), hours 7 d / 30 d, performance (shifts, hours, moderation actions), request LOA from the dashboard.
- **Activity**: `activity_events` records every logbook entry (independent of the Discord log toggles) plus player joins/leaves; filters, search, pagination; pruned after 60 days.
- **Overview**: adds BOLOs, players in-game, in-game staff, unread notifications, 14-day activity trend, Discord/ER:LC/API/database health.
- **Global search** across members, staff, players, incidents, BOLOs, cases, applications (permission-gated).
- **SSE**: routine poll heartbeats are no longer forwarded; player changes are sent as one `erlc.players` event.

### Frontend
- New dark-only design system (`app.css` rewritten), brand mark, SVG icon set, sidebar with grouped navigation, top bar with global search (⌘K / Ctrl K command palette), notification center, skeleton loading, empty states, toasts, confirm dialogs, responsive tables → cards on phones, bottom-safe mobile drawer.
- **No full reloads**: pages subscribe to event topics; refreshes run in the background and *morph* the existing DOM (scroll, focus, typed input, open `<details>`, selected tabs and open modals are preserved). Refreshes pause while the tab is hidden and while you are typing.
- New/redesigned pages: Overview (customisable widgets), Activity, ER:LC Live, Players, Moderation center (overview, in-game records, player profiles, Discord cases, reports, staff records), Staff overview, Shifts (start/end), LOA, BOLOs.

### Testing
Backend tests for every new service and route (permissions, unauthorized access, persistence, join/leave diffing, BOLO match, duplicate shifts). Frontend checked in a headless browser at desktop, laptop, tablet and phone widths against a mock API.

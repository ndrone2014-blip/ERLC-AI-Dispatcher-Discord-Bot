# 🖥️ Hive Dispatch Web Dashboard (v4.0)

The dashboard is a website that the bot serves **itself**. There is nothing extra to install or host: when the
bot is running, the dashboard is running. Because the website and the bot share one database and one event bus,
anything changed on the dashboard is live in Discord immediately, and anything that happens in Discord appears on
the dashboard within a second.

---

## 1. Quick start: this PC only (about 5 minutes)

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → your Hive Dispatch
   application → **OAuth2**.
2. Under **Client Secret**, press **Reset Secret** and copy it.
3. Under **Redirects**, press **Add Redirect** and enter exactly:
   ```
   http://localhost:8080/auth/callback
   ```
   then **Save Changes**.
4. Open your `.env` file and set:
   ```
   DISCORD_CLIENT_SECRET=paste-the-secret-here
   ```
5. Restart the bot (`start.bat`). The console shows `Web dashboard running at http://localhost:8080`.
6. Open **http://localhost:8080** on the bot's PC and press **Sign in with Discord**.

> The client secret is like a password. Never share it or put it in a screenshot. If it leaks, press Reset Secret.

---

## 2. Who can sign in and what they see

Anyone can press "Sign in with Discord", but the dashboard only opens a server for people whose **roles in that
server** give them the **Open the web dashboard** permission. Everything else is decided the same way, checked
by the bot on every single request (nothing the browser sends can widen it):

| Group | Typical access |
|---|---|
| Server Owner | Everything, always |
| Administrator (Discord Administrator / Manage Server, or mapped roles) | Everything, incl. AI keys, ER:LC key, automations |
| Management | Staff, applications, embeds, announcements, bans, analytics, log export |
| Dispatch Supervisor | Dispatch center, priorities, shifts of others, sessions |
| Moderator | Moderation cases, reports, verification tools, member notes |
| Dispatcher | Dispatch center, units |
| Department Leader | Staff directory, leave approval, application review, sessions |
| Staff Member | Dashboard overview, staff directory, own shifts |
| Officer / Fire-EMS | Dashboard overview, dispatch board (read), own shifts |

Map your Discord roles to these groups on **Settings → Permissions** (or with `/permissions map` in Discord).
You can change what any group may do on the same page: tick the boxes for a permission and press **Save**.

---

## 3. Let staff open it from anywhere

> **Hosting on Netlify?** Follow **[NETLIFY.md](NETLIFY.md)** instead of this section. Netlify serves the pages at your
> `*.netlify.app` address and forwards requests to the bot.

By default the dashboard only listens on the bot's own PC (`127.0.0.1`). To share it, put it behind a **tunnel**:
a free service that gives your PC a public HTTPS address without opening ports on your router.

### Option A: Cloudflare Tunnel (free, stable address, needs a domain on Cloudflare)

```bat
winget install --id Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create hive
cloudflared tunnel route dns hive dashboard.yourdomain.com
cloudflared tunnel run --url http://localhost:8080 hive
```

Keep the last command running while the bot runs (or install it as a service: `cloudflared service install`).

### Option B: ngrok (no domain needed)

Create a free account at ngrok.com, then in the ngrok dashboard copy your authtoken and your free static domain
(at the time of writing the free plan includes one; check ngrok's current terms):

```bat
winget install ngrok.ngrok
ngrok config add-authtoken YOUR_TOKEN
ngrok http --url=your-name.ngrok-free.app 8080
```

> Avoid "quick" tunnels that give a different random address on every start: you would have to update the
> Discord redirect and `.env` each time.

### Then, for either option

1. In `.env`:
   ```
   DASHBOARD_PUBLIC_URL=https://dashboard.yourdomain.com
   DASHBOARD_TRUST_PROXY=true
   ```
   (no slash at the end)
2. Developer Portal → OAuth2 → **Redirects** → add `https://dashboard.yourdomain.com/auth/callback`.
   You can keep the localhost one too.
3. Restart the bot. Share the address with your staff; `/dashboard web` in Discord shows it too.

---

## 4. What's in the dashboard

| Area | Sections |
|---|---|
| Overview | Customizable dashboard (choose numbers and panels, reorder them), activity feed with filters/search, analytics |
| ER:LC | Live server (players, in-game staff, joins & leaves, join/kill/command logs, mod calls, server staff, ban list, server console), player directory |
| Moderation | Moderation center (stats, chart, top moderators, player lookup), in-game records, player profiles, Discord cases, active punishments, member reports, staff moderation records, settings |
| BOLOs | BOLO board: priorities, linked players, notes, close/reopen, history |
| Staff | Staff overview (availability, hours, performance), shifts (live timer), leave (LOA) requests and reviews, staff directory, settings |
| Dispatch | Dispatch center, incident history, units, dispatch settings |
| Community | Members, Roblox verification, applications, sessions, announcements |
| Automation · Logging · Bot · AI · Settings | As before (automations, schedules, log viewer, audit log, embed builder, AI providers, permissions, …) |

Top bar: breadcrumbs, **search / command palette** (Ctrl K or ⌘K, or press `/`), live-update indicator, **Ask Hive**
and the **notification center**. The dashboard is dark mode only. The red/blue light bar at the very top pulses while a
**critical** incident is open.

**Live updates never reload the page.** Each page listens only for the events it shows and updates those parts in
place, keeping your scroll position, tab, open dialogs and anything you're typing. If a host cuts the live connection
(some proxies do), the dashboard quietly checks every 20 seconds instead — the indicator in the top bar shows which.

### In-game moderation and the ER:LC API
In-game actions are always **recorded**. Tick *Carry it out in-game* to also run the matching ER:LC command (`:pm` for
warnings, `:kick`, `:ban`, `:unban`) through the official API; the result is stored on the record. ER:LC needs the
player to be in the server for `:kick` and `:pm`. Temporary bans are lifted automatically when they end.
The live sections (staff list, logs, mod calls, queue) come straight from ER:LC; if your server's API access doesn't
include one, the page says so instead of showing made-up data. Admins can switch the extra sections off with
`PUT /erlc/settings {"extras": false}` (they share the 911 poll, so they cost no extra requests).

---

## 5. Security

* Sign-in uses Discord OAuth2 with the `identify` scope only (your username and avatar). The bot never sees your
  Discord password, and the dashboard never asks for one.
* Session cookies are random, HttpOnly, SameSite=Lax and Secure on HTTPS, and expire after 7 days. Only a hash of
  the cookie is stored. **Settings → Security → Sign out everywhere** ends every session.
* Every change needs a per-session CSRF token and must come from the dashboard's own address.
* A strict Content-Security-Policy allows scripts only from the bot itself; the page can't be framed.
* Requests are rate-limited per session and per IP; request bodies are limited to 1 MB.
* API keys and the ER:LC key are encrypted at rest and **write-only**: they can be replaced but never read back.
* Every change is written to the audit log (and the `dashboard`/`security` log categories if you enable them).
* Servers are isolated: every query is scoped to the server in the address, and your membership in that server is
  verified by the bot.

---

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| "Sign-in isn't set up yet" | `DISCORD_CLIENT_SECRET` is missing in `.env`. Add it and restart. |
| "Discord did not accept the sign-in" | The redirect in the Developer Portal must match `DASHBOARD_PUBLIC_URL/auth/callback` **exactly** (http vs https, no trailing slash). |
| "Your roles don't give you access" | Map your role to a group on Settings → Permissions, or ask the owner/an administrator. |
| Console: "could not start … port" | Another program uses port 8080. Set `DASHBOARD_PORT=8090` (and update the redirect). |
| Works on the PC but not for staff | Set up a tunnel (section 3) and `DASHBOARD_PUBLIC_URL`. |
| Roles/nicknames don't change | Give the bot **Manage Roles** / **Manage Nicknames** and drag its role above the roles it manages. |
| Turn the dashboard off | `DASHBOARD_ENABLED=false`. Every feature still works through slash commands. |

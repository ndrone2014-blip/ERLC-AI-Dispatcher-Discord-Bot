# ✅ Roblox Verification (v4.0)

Pulse Dispatch links a member's Discord account to their Roblox account. An account is **only** linked after a
verification really succeeds. A member typing a name is never enough.

There are two methods. You can offer one or both.

| | 🔑 Profile code | 🔐 Sign in with Roblox (OAuth 2.0) |
|---|---|---|
| Setup | **None** | A Roblox OAuth app + a public HTTPS dashboard |
| How it proves ownership | Member puts a random phrase in their Roblox profile's **About**; the bot reads the public profile | Member signs in on roblox.com; Roblox tells the bot which account signed in |
| Members can use it | Everyone | Up to 10 people until Roblox approves your app (see below) |

---

## 1. Basic setup (profile code, about 3 minutes)

1. **Bot permissions.** In **Server Settings → Roles → Pulse Dispatch**, turn on **Manage Roles** and
   **Manage Nicknames**, then drag the Pulse Dispatch role **above** the roles it should give
   (e.g. *Verified*). Discord doesn't let a bot hand out roles above its own.
2. **Roles.** In Discord:
   ```
   /verification roles verified_role:@Verified unverified_role:@Unverified
   ```
   Both are optional. The verified role is given on success; the unverified role is removed (and given back if a
   link is removed). Or set them on the dashboard: **Verification → Settings**.
3. **Panel.** Post a public verification panel with a **Verify** button in your verification channel:
   ```
   /verification panel channel:#verify
   ```
4. Optional, on **Verification → Settings**: nickname format (e.g. `Display (@username)`), minimum Roblox account
   age, refuse banned accounts, DM on success.

### What members do

1. Press **Verify** on the panel (or run `/verify`) and pick **Use a profile code**, then type their Roblox username.
2. Copy the code they're shown (six plain words, e.g. `pulse amber falcon river cobalt meadow lantern`).
3. Open their Roblox profile → edit **About** → paste the code anywhere → **Save**.
4. Back in Discord press **Done**. They're verified, get the role and nickname, and can remove the code.

The code is valid for 20 minutes and can be checked 10 times. Plain words are used because Roblox's text filter
often hides numbers in profiles.

---

## 2. Optional: "Sign in with Roblox" (OAuth 2.0)

Requirements: the dashboard must be reachable at a public **HTTPS** address. Set up a tunnel and
`DASHBOARD_PUBLIC_URL` first (see [DASHBOARD.md](DASHBOARD.md), section 3).

1. Go to the Roblox Creator Dashboard → [OAuth 2.0 Apps](https://create.roblox.com/dashboard/credentials?activeTab=OAuthTab)
   → **Create App**, give it a unique name, agree to the terms → **Create**.
2. Copy the **Client ID** and **Secret** somewhere safe.
3. In the app's settings:
   * **Permissions / scopes:** add **openid** and **profile** (nothing else is needed).
   * **Redirect URL:** `https://YOUR-DASHBOARD-ADDRESS/auth/roblox/callback`
     (exactly your `DASHBOARD_PUBLIC_URL` + `/auth/roblox/callback`).
4. In `.env`:
   ```
   ROBLOX_CLIENT_ID=...
   ROBLOX_CLIENT_SECRET=...
   ```
5. Restart the bot. `/verify` now also shows a **Sign in with Roblox** button (each link works once, for 10 minutes).

**Important: Roblox's review.** New OAuth apps start in *private mode*, limited to **10 unique users**, which is
meant for testing. To let your whole community use it, submit the app for review in the Creator Dashboard. Roblox
asks for a demo video of up to 1 minute showing the full sign-in flow, plus Privacy Policy and Terms of Service
URLs (HTTPS). Until it's approved, keep profile-code verification switched on so everyone can verify.

---

## 3. Staff tools

| Command | What it does |
|---|---|
| `/verification lookup member:` or `roblox_username:` | Who is linked to what |
| `/verification link member: roblox_username:` | Link manually (recorded as a staff link with your name) |
| `/verification unlink member:` | Remove a link (roles are reverted) |
| `/verification sync member:` | Re-apply the verified role and nickname |
| `/verification history` | Recent verifications, removals and failures |
| `/verification status` | Settings and numbers at a glance |

The dashboard's **Verification** page has the same tools, plus searching all linked members and seeing
verifications in progress. Every verification event is written to the **verification** log category.

The same Roblox account can't be linked to two Discord members in one server. Each server keeps its own links.
Verified Roblox usernames are also written to the member's dispatch officer record.

---

## 4. Automations

Use the trigger **"A member verifies their Roblox account"** on the **Automation** page to give extra roles, send a
DM, post in a channel and so on. Placeholders: `{user}`, `{roblox}`, `{server}`.

---

## 5. Troubleshooting

| Problem | Fix |
|---|---|
| "The code was not found" | Make sure the profile was **saved**, wait ~10 seconds, press Done again. The code must be in **About**, not the display name. |
| "No Roblox account named …" | Check the spelling. It's the **username**, not the display name. |
| "already linked to another Discord member" | Staff can check with `/verification lookup` and remove the old link. |
| Verified but no role | The bot's role must be above the verified role, and the bot needs **Manage Roles**. The log entry says exactly what failed. |
| Nickname not changed | Discord never lets bots rename the **server owner**. Otherwise check **Manage Nicknames** and role order. |
| "Roblox is rate limiting" | Wait a minute and try again. |
| Sign in with Roblox missing | Needs `ROBLOX_CLIENT_ID`, `ROBLOX_CLIENT_SECRET` **and** `DASHBOARD_PUBLIC_URL`, plus "Allow Sign in with Roblox" in settings. |

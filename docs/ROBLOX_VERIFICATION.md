# ✅ Roblox Verification (v4.0)

Pulse Dispatch links a member's Discord account to their Roblox account. An account is **only** linked after a
verification really succeeds. A member typing a name is never enough.

Members never have to change their Roblox bio. When someone presses **Verify**, the bot tries these in order:

| Order | Method | What the member does | Setup |
|---|---|---|---|
| 1 | ⚡ **Instant (Bloxlink / RoVer)** | Nothing. If they're already verified with Bloxlink or RoVer, they're done in one click (and automatically when they join). | A free server API key, pasted on the dashboard |
| 2 | 🔐 **Sign in with Roblox** | Press the button, sign in on roblox.com, press **Confirm**. Done. | A Roblox OAuth app (about 5 minutes) |
| 3 | 🔑 Profile code (backup, can be turned off) | Paste a code in their Roblox About section | None |

---

## 1. Basics (2 minutes)

1. **Bot permissions.** In **Server Settings → Roles → Pulse Dispatch**, turn on **Manage Roles** and
   **Manage Nicknames**, then drag the Pulse Dispatch role **above** the roles it should give (e.g. *Verified*).
2. **Roles.** On the dashboard, **Verification → Settings**, pick the verified role (and an unverified role if you
   use one). Or: `/verification roles verified_role:@Verified`
3. **Panel.** `/verification panel` and pick the channel **from the list Discord shows** (or leave it out to post
   in the current channel). Typing a channel name by hand gives "A channel id specified is invalid".

---

## 2. Sign in with Roblox (the main method)

1. Open the Roblox Creator Dashboard → [OAuth 2.0 Apps](https://create.roblox.com/dashboard/credentials?activeTab=OAuthTab)
   → **Create App**, give it a name, agree to the terms → **Create**.
2. Copy the **Client ID** and **Secret**.
3. In the app's settings:
   * **Scopes:** **openid** and **profile**.
   * **Redirect URL:** `https://YOUR-SITE.netlify.app/auth/roblox/callback`
     (your `DASHBOARD_PUBLIC_URL` + `/auth/roblox/callback`).
4. In `.env`:
   ```
   ROBLOX_CLIENT_ID=...
   ROBLOX_CLIENT_SECRET=...
   ```
5. Restart the bot (keep ngrok running). **Verify** now shows a big **Sign in with Roblox** button.

**Roblox's review.** New OAuth apps start in *private mode*, limited to **10 people**. To open it to everyone,
submit the app for review in the Creator Dashboard (Roblox asks for a short demo video and Privacy Policy / Terms
links). Until then, members already on Bloxlink or RoVer still verify instantly (section 3), and you can leave the
profile code on as a backup.

---

## 3. Instant verification with Bloxlink or RoVer

If your members already verified with Bloxlink or RoVer anywhere, Pulse can ask those services who they are, so
they're verified the moment they press **Verify** or join the server. You need one or both keys:

* **Bloxlink:** [blox.link/dashboard](https://blox.link/dashboard) → your server → **Developer / API keys** →
  create a **Server API key**. Bloxlink must be in your server.
* **RoVer:** [rover.link](https://rover.link) dashboard → your server → **API keys** → create a key. RoVer must be in
  your server.

**No Roblox OAuth app?** (Roblox requires ID verification to create one.) Then **Verify** shows a
**Verify on Bloxlink / RoVer** button instead. Members sign in there with the official Roblox login, come back, press
**I'm done**, and Pulse picks the link up with your key.

Paste the key on the Pulse dashboard: **Verification → Settings → Instant verification**, press **Save**, then
**Test**. Test looks *you* up, so it says whether the key works. Keys are stored encrypted and never shown again.

Toggles on the same page: use Bloxlink, use RoVer, verify automatically when someone joins.

---

## 4. Turning off the profile code

On **Verification → Settings**, untick **Profile code**. Members then only see Sign in with Roblox (and instant
verification). Keep it on while your OAuth app is still limited to 10 people if not everyone uses Bloxlink/RoVer.

---

## 5. Staff tools

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

## 6. Automations

Use the trigger **"A member verifies their Roblox account"** on the **Automation** page to give extra roles, send a
DM, post in a channel and so on. Placeholders: `{user}`, `{roblox}`, `{server}`.

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| "The code was not found" | Make sure the profile was **saved**, wait ~10 seconds, press Done again. The code must be in **About**, not the display name. |
| "No Roblox account named …" | Check the spelling. It's the **username**, not the display name. |
| "already linked to another Discord member" | Staff can check with `/verification lookup` and remove the old link. |
| Verified but no role | The bot's role must be above the verified role, and the bot needs **Manage Roles**. The log entry says exactly what failed. |
| Nickname not changed | Discord never lets bots rename the **server owner**. Otherwise check **Manage Nicknames** and role order. |
| "Roblox is rate limiting" | Wait a minute and try again. |
| Sign in with Roblox missing | Needs `ROBLOX_CLIENT_ID`, `ROBLOX_CLIENT_SECRET` **and** `DASHBOARD_PUBLIC_URL`, plus "Allow Sign in with Roblox" in settings. |

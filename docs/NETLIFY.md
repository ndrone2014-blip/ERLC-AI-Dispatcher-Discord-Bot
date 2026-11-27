# 🌐 Hosting the Pulse Dispatch dashboard on Netlify

Netlify hosts the dashboard's **pages** at an address like `https://pulse-dispatch.netlify.app`. The bot and its
database still run on your PC, because that's where your dispatch data lives. A tiny Netlify edge function
(`web/netlify/edge-functions/pulse-proxy.js`) forwards sign-in and data requests to the bot through a free
**tunnel**, so for your staff it's all one address.

```
Staff browser ──► https://pulse-dispatch.netlify.app ──(edge function)──► tunnel ──► bot on your PC (port 8080)
                    pages served by Netlify               adds secret + visitor IP       database, Discord
```

If the bot (or the tunnel) is off, the site still loads but shows "Can't reach the bot".

You need: a free [Netlify](https://www.netlify.com) account connected to GitHub, and a free [ngrok](https://ngrok.com)
account (or a Cloudflare Tunnel, see the end).

---

## Step 1: Give the bot a stable tunnel address (ngrok)

1. Sign up at ngrok.com. In the ngrok dashboard, copy your **authtoken**, then open **Domains** and claim your free
   static domain (it looks like `something.ngrok-free.app`; at the time of writing the free plan includes one).
2. On the bot's PC, in a terminal:
   ```bat
   winget install ngrok.ngrok
   ngrok config add-authtoken YOUR_AUTHTOKEN
   ngrok http --url=something.ngrok-free.app 8080
   ```
   (Older ngrok versions use `--domain=` instead of `--url=`.) Leave this window open whenever the bot runs.

Your **tunnel address** is `https://something.ngrok-free.app`.

## Step 2: Make a shared secret

On the bot's PC:
```bat
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
Copy the result. This proves to the bot that requests really come from your Netlify site.

## Step 3: Create the Netlify site

1. Netlify → **Add new project** → **Import an existing project** → **GitHub** → pick
   `ERLC-AI-Dispatcher-Discord-Bot`.
2. Leave the build settings as they are; the repository's `netlify.toml` fills them in (base `web`, publish
   `static`, no build step).
3. Before deploying (or afterwards in **Project configuration → Environment variables**), add:

   | Key | Value |
   |---|---|
   | `PULSE_BOT_URL` | your tunnel address, e.g. `https://something.ngrok-free.app` |
   | `PULSE_PROXY_SECRET` | the secret from step 2 |

4. **Deploy**. Optionally rename the site (**Project configuration → Change project name**) to get
   `https://pulse-dispatch.netlify.app` or similar. Your **dashboard address** is that Netlify URL.

> Changed an environment variable later? Netlify only picks it up after a new deploy: **Deploys → Trigger deploy**.

## Step 4: Tell the bot its address

In the bot's `.env`:
```
DASHBOARD_PUBLIC_URL=https://pulse-dispatch.netlify.app
DASHBOARD_PROXY_SECRET=the-same-secret-as-PULSE_PROXY_SECRET
DISCORD_CLIENT_SECRET=your-discord-oauth-client-secret
```
(No slash at the end of the URL. `DASHBOARD_HOST` stays `127.0.0.1`: the tunnel connects on the same PC.)

## Step 5: Tell Discord the sign-in address

Discord Developer Portal → your app → **OAuth2**:
* **Client Secret**: Reset Secret if you haven't yet; it goes in `.env` as `DISCORD_CLIENT_SECRET`.
* **Redirects** → **Add Redirect** → `https://pulse-dispatch.netlify.app/auth/callback` → **Save Changes**.

## Step 6: Restart the bot

Restart with `start.bat`, then open your Netlify address and press **Sign in with Discord**. `/dashboard web` in
Discord now shows the Netlify address to your staff.

---

## Everyday use

Start the bot (`start.bat`) **and** the tunnel (`ngrok http --url=something.ngrok-free.app 8080`). Netlify itself is
always on. Pushing to GitHub redeploys the site automatically.

**Tip:** put `NGROK_URL=something.ngrok-free.app` (no `https://`) in `.env` and `start.bat` opens the tunnel for you in
its own window every time you start the bot. Keep that window open.

## Roblox "Sign in with Roblox" on Netlify

Use the Netlify address for the Roblox redirect URL: `https://pulse-dispatch.netlify.app/auth/roblox/callback`
(see [ROBLOX_VERIFICATION.md](ROBLOX_VERIFICATION.md)).

## Security notes

* With `DASHBOARD_PROXY_SECRET` set, the bot refuses every dashboard request that didn't come through your Netlify
  site, so someone who finds your tunnel address can't bypass the dashboard. Visitors can't fake the secret: the
  edge function always overwrites it.
* Sessions, CSRF protection, permissions and server isolation work exactly as described in
  [DASHBOARD.md](DASHBOARD.md). The secret and API keys never reach the browser.
* Netlify serves the pages with the same strict security headers the bot uses (see `netlify.toml`).

## Troubleshooting

| Problem | Fix |
|---|---|
| "The dashboard isn't connected to the bot yet" | Add `PULSE_BOT_URL` in Netlify and redeploy. |
| "The tunnel (ngrok) isn't running" | Start ngrok again on the bot's PC (or set `NGROK_URL` in `.env` so `start.bat` does it). |
| "The tunnel is running but the bot's dashboard isn't answering" | The bot isn't running, or its dashboard failed to start — the bot's console says why. Check the port matches (default 8080). |
| "Can't reach the bot" | Start the bot **and** ngrok; check `PULSE_BOT_URL` matches your ngrok domain exactly. |
| "Open the dashboard through its Netlify address" | `PULSE_PROXY_SECRET` (Netlify) and `DASHBOARD_PROXY_SECRET` (`.env`) don't match. Fix, redeploy, restart. |
| "Discord did not accept the sign-in" | The Discord redirect must be exactly `https://YOUR-SITE.netlify.app/auth/callback`, and `DASHBOARD_PUBLIC_URL` must be your Netlify URL. |
| Sign-in loops back to the sign-in page | `DASHBOARD_PUBLIC_URL` must start with `https://`. Clear the site's cookies and try again. |
| Live updates seem slow | If Netlify cuts the live-update connection, the dashboard automatically refreshes every 20 seconds instead. |

## Using a Cloudflare Tunnel instead of ngrok

If you have a domain on Cloudflare, a named tunnel works the same way:
```bat
winget install --id Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create pulse
cloudflared tunnel route dns pulse bot.yourdomain.com
cloudflared tunnel run --url http://localhost:8080 pulse
```
Then use `https://bot.yourdomain.com` as `PULSE_BOT_URL`. Avoid "quick" tunnels with a new random address on every
start: you'd have to change `PULSE_BOT_URL` and redeploy each time.

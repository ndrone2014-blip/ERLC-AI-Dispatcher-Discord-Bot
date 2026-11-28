// Hive Dispatch | ERLC — forwards dashboard sign-in and data requests from Netlify to the bot.
//
// The bot (and its database) runs on your PC, reachable through a tunnel at HIVE_BOT_URL. Visitors only ever
// talk to your Netlify address, so sign-in cookies, CSRF checks and live updates work exactly as if the bot
// served the site itself. HIVE_PROXY_SECRET proves to the bot that a request really came through Netlify.

const JSON_HEADERS = { "content-type": "application/json", "cache-control": "no-store" };

export default async (request, context) => {
  // The old PULSE_* names (from before the rename to Hive Dispatch) still work, so existing sites keep running.
  const env = (name) => Netlify.env.get("HIVE_" + name) || Netlify.env.get("PULSE_" + name) || "";
  const target = env("BOT_URL").trim().replace(/\/+$/, "");
  const secret = env("PROXY_SECRET");
  if (!target || !/^https?:\/\//.test(target)) {
    return new Response(JSON.stringify({ error: "The dashboard isn't connected to the bot yet. Set HIVE_BOT_URL in Netlify (see docs/NETLIFY.md)." }),
      { status: 503, headers: JSON_HEADERS });
  }
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  for (const h of ["host", "x-hive-proxy-secret", "x-hive-client-ip", "x-pulse-proxy-secret", "x-pulse-client-ip", "content-length", "accept-encoding"]) headers.delete(h);
  headers.set("X-Hive-Proxy-Secret", secret);
  if (context && context.ip) headers.set("X-Hive-Client-IP", context.ip);
  headers.set("ngrok-skip-browser-warning", "true"); // ngrok's free plan shows a warning page to browsers otherwise
  headers.set("accept-encoding", "identity");

  const init = { method: request.method, headers, redirect: "manual" };
  if (!["GET", "HEAD"].includes(request.method)) init.body = await request.arrayBuffer();

  let upstream;
  try {
    upstream = await fetch(target + url.pathname + url.search, init);
  } catch (_e) {
    return new Response(JSON.stringify({ error: "Can't reach the bot. Make sure the bot and its tunnel are running." }),
      { status: 502, headers: JSON_HEADERS });
  }
  // Tunnel problems come back as ngrok's own HTML pages: turn them into a clear message for the dashboard.
  const ngrokCode = upstream.headers.get("ngrok-error-code") || "";
  const isHtml = (upstream.headers.get("content-type") || "").includes("text/html");
  if (ngrokCode || (url.pathname.startsWith("/api/") && isHtml && upstream.status >= 400)) {
    const msg = {
      ERR_NGROK_3200: "The tunnel (ngrok) isn't running, so Netlify can't reach the bot. On the bot's PC, start ngrok again (start.bat does it for you when NGROK_URL is set in .env) — see docs/NETLIFY.md.",
      ERR_NGROK_8012: "The tunnel is running but the bot's dashboard isn't answering on the PC. Check that the bot is running, DASHBOARD_ENABLED is true and the port matches ngrok (default 8080). The bot's console/log says why if the dashboard failed to start.",
    }[ngrokCode] || `The tunnel answered with an error (${ngrokCode || "HTTP " + upstream.status}) instead of the bot. Make sure the bot and its tunnel are running and HIVE_BOT_URL is your current tunnel address.`;
    return new Response(JSON.stringify({ error: msg, tunnel_error: ngrokCode || upstream.status }), { status: 502, headers: JSON_HEADERS });
  }
  const out = new Headers(upstream.headers);
  out.delete("content-encoding"); // the body is already decoded by fetch
  out.delete("content-length");
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
};

export const config = { path: ["/api/*", "/auth/*", "/verify/*", "/healthz"] };

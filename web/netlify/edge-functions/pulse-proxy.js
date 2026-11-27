// Pulse Dispatch | ERLC — forwards dashboard sign-in and data requests from Netlify to the bot.
//
// The bot (and its database) runs on your PC, reachable through a tunnel at PULSE_BOT_URL. Visitors only ever
// talk to your Netlify address, so sign-in cookies, CSRF checks and live updates work exactly as if the bot
// served the site itself. PULSE_PROXY_SECRET proves to the bot that a request really came through Netlify.

const JSON_HEADERS = { "content-type": "application/json", "cache-control": "no-store" };

export default async (request, context) => {
  const target = (Netlify.env.get("PULSE_BOT_URL") || "").trim().replace(/\/+$/, "");
  const secret = Netlify.env.get("PULSE_PROXY_SECRET") || "";
  if (!target || !/^https?:\/\//.test(target)) {
    return new Response(JSON.stringify({ error: "The dashboard isn't connected to the bot yet. Set PULSE_BOT_URL in Netlify (see docs/NETLIFY.md)." }),
      { status: 503, headers: JSON_HEADERS });
  }
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  for (const h of ["host", "x-pulse-proxy-secret", "x-pulse-client-ip", "content-length", "accept-encoding"]) headers.delete(h);
  headers.set("X-Pulse-Proxy-Secret", secret);
  if (context && context.ip) headers.set("X-Pulse-Client-IP", context.ip);
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
  const out = new Headers(upstream.headers);
  out.delete("content-encoding"); // the body is already decoded by fetch
  out.delete("content-length");
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
};

export const config = { path: ["/api/*", "/auth/*", "/verify/*", "/healthz"] };

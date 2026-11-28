/* Hive Dispatch | ER:LC — web dashboard core (v5.0, no build step, no dependencies).
 *
 * Security: server data is always inserted as text (textContent / text nodes), never as HTML, so names, reasons
 * and messages typed by members can't inject markup. Only the static icon strings below are parsed as SVG.
 * Permissions are enforced by the server on every request; the UI only hides what the server would refuse.
 *
 * Live updates never reload the page: pages subscribe to event topics and update their own regions in place.
 * Scroll position, focus, typed input, open menus and modals are preserved.
 */
"use strict";

// ============================================================================ tiny DOM toolkit
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected" || k === "multiple" || k === "required") el[k] = !!v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
function clear(el, ...kids) { el.replaceChildren(); return add(el, kids); }
/** Replace a region's content without moving the page: keeps the region's own scroll and the window scroll. */
function swap(el, ...kids) {
  if (!el) return;
  const st = el.scrollTop, y = window.scrollY;
  el.replaceChildren(); add(el, kids);
  el.scrollTop = st;
  if (Math.abs(window.scrollY - y) > 1) window.scrollTo(0, y);
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const store = { get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
                set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage off: fine */ } } };

// ============================================================================ icons (static, trusted strings)
const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  chart: '<path d="M4 20V11"/><path d="M10 20V4"/><path d="M16 20v-6"/><path d="M21 20H3"/>',
  gamepad: '<rect x="2" y="7" width="20" height="11" rx="5"/><path d="M7 10.5v4M5 12.5h4"/><circle cx="15.5" cy="11.5" r=".9"/><circle cx="18" cy="14" r=".9"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.8c1.9.7 3.1 2.4 3.5 5.2"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6"/>',
  shield: '<path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z"/>',
  gavel: '<path d="m14 13-8.5 8.5a2.1 2.1 0 0 1-3-3L11 10"/><path d="m16 16 6-6"/><path d="m8 8 6-6"/><path d="m9 7 8 8"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  badge: '<path d="M12 2.5l2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.6-4.8 2.6.9-5.4-3.9-3.8 5.4-.8z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  siren: '<path d="M7 18v-6a5 5 0 0 1 10 0v6"/><path d="M5 21h14v-3H5z"/><path d="M12 3v2M4.2 6.2l1.4 1.4M19.8 6.2l-1.4 1.4"/>',
  car: '<path d="M3 16v-3l2-5.5A2 2 0 0 1 6.9 6h10.2a2 2 0 0 1 1.9 1.5L21 13v3"/><path d="M3 13h18M3 16h18v2H3z"/><circle cx="7" cy="18" r="1.6"/><circle cx="17" cy="18" r="1.6"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
  play: '<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/>',
  megaphone: '<path d="M3 11v2a1 1 0 0 0 1 1h3l6 4V6L7 10H4a1 1 0 0 0-1 1z"/><path d="M17 8a5 5 0 0 1 0 8"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  logs: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  bot: '<rect x="4" y="8" width="16" height="11" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M2 13v2M22 13v2"/>',
  sparkles: '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  sliders: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  bell: '<path d="M6 16v-5a6 6 0 1 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronLeft: '<path d="m15 6-6 6 6 6"/>',
  logout: '<path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4"/><path d="M10 17l5-5-5-5M15 12H3"/>',
  login: '<path d="M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4"/><path d="M14 17l5-5-5-5M19 12H8"/>',
  alert: '<path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5h.01"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.9-3.9L4 9M4 4v5h5"/><path d="M4 13a8 8 0 0 0 14.9 3.9L20 15M20 20v-5h-5"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  message: '<path d="M4 5h16v11H9l-5 4z"/>',
  signal: '<path d="M2 9a14 14 0 0 1 20 0M5.5 12.5a9 9 0 0 1 13 0M9 16a4 4 0 0 1 6 0M12 19.5h.01"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  note: '<path d="M5 3h10l4 4v14H5z"/><path d="M9 12h6M9 16h4"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  command: '<path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3z"/>',
  boot: '<path d="M7 3v8.5L4 15v4h16v-1.5l-6.5-3L11 10V3z"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
  unlock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
  download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3"/>',
  pin: '<path d="M12 21s-7-6-7-11a7 7 0 0 1 14 0c0 5-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  dots: '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  warn: '<path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  coffee: '<path d="M4 9h13v5a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z"/><path d="M17 11h1.5a2.5 2.5 0 0 1 0 5H17M8 3v3M12 3v3"/>',
  palette: '<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.6-.7 1.6-1.5 0-1.2-1-1.6-1-2.6 0-.8.6-1.4 1.5-1.4H17a4 4 0 0 0 4-4c0-4.7-4-8.5-9-8.5z"/><circle cx="7.5" cy="11" r="1"/><circle cx="10" cy="7" r="1"/><circle cx="15" cy="7.5" r="1"/>',
};
function icon(name, cls = "") {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.setAttribute("class", "ico " + cls);
  s.innerHTML = ICONS[name] || ICONS.dots; // static strings above only
  return s;
}
const NAV_ICONS = {
  dashboard: "home", activity: "activity", analytics: "chart", erlc: "server", "erlc/players": "gamepad", moderation: "shield",
  "moderation/records": "gavel", "moderation/discord": "message", "moderation/active": "ban", "moderation/reports": "alert",
  "moderation/staff": "users", "moderation/settings": "sliders", bolos: "eye", "staff/overview": "badge", shifts: "clock",
  "staff/leave": "calendar", staff: "users", "staff/settings": "sliders", dispatch: "siren", incidents: "file", units: "car",
  "dispatch/settings": "sliders", members: "users", verification: "checkCircle", applications: "file", sessions: "play",
  announcements: "megaphone", automation: "zap", "automation/schedules": "calendar", logs: "logs", "logs/audit": "list",
  "logs/settings": "sliders", embeds: "palette", welcome: "message", commands: "command", "bot/appearance": "palette", ai: "sparkles",
  "ai/personality": "bot", "ai/usage": "chart", settings: "sliders", "settings/permissions": "key", "settings/notifications": "bell",
  "settings/integrations": "layers", "settings/security": "shield",
};

// ============================================================================ state & API
const S = { me: null, gid: null, meta: null, perms: new Set(), refresh: null, es: null, poll: null, path: "", key: "", critical: 0,
            inflight: new Map(), sessionLost: false, prefs: null };
const can = (p) => S.perms.has(p);
const canAny = (...p) => p.some(can);

async function api(path, { method = "GET", body, quiet = false } = {}) {
  const url = path.startsWith("/api/") ? path : `/api/g/${S.gid}${path}`;
  // Identical GETs that are already on their way share one request (no duplicate network traffic).
  if (method === "GET" && S.inflight.has(url)) return S.inflight.get(url);
  const run = (async () => {
    const headers = { Accept: "application/json" };
    if (method !== "GET") { headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = S.me?.csrf || ""; }
    let r;
    try { r = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" }); }
    catch { throw new Error("Can't reach the bot. Is it running?"); }
    if (r.status === 401) { sessionLost(); throw new Error("Your session ended. Please sign in again."); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status}).`);
    return data;
  })();
  if (method === "GET") { S.inflight.set(url, run); run.finally(() => S.inflight.delete(url)).catch(() => {}); }
  return run;
}
function sessionLost() {
  // Never yank the page away mid-task: show a banner and let the user sign in when ready.
  if (S.sessionLost) return;
  S.sessionLost = true; disconnect();
  document.body.append(h("div", { class: "session-banner", role: "alert" }, icon("alert"), h("span", null, "Your session ended. Your unsaved changes are still on screen."),
    h("a", { class: "btn sm primary", href: "/auth/login" }, "Sign in again")));
}
async function act(fn, ok) {
  try { const out = await fn(); if (ok) toast(typeof ok === "function" ? ok(out) : ok); if (out && out.warning) toast(out.warning, true); return out; }
  catch (e) { toast(e.message, true); throw e; }
}
function toast(text, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : " ok"), role: bad ? "alert" : "status" }, icon(bad ? "alert" : "checkCircle"), h("span", null, text),
    h("button", { class: "toast-x", "aria-label": "Dismiss", onclick: () => t.remove() }, icon("x")));
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 250); }, bad ? 7000 : 3500);
}

// ============================================================================ formatting
const fmtNum = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString());
function when(iso) {
  if (!iso) return h("span", { class: "muted" }, "—");
  const d = new Date(iso), s = (Date.now() - d) / 1000;
  let txt;
  if (Math.abs(s) < 45) txt = s >= 0 ? "just now" : "in a moment";
  else if (Math.abs(s) < 3600) txt = rel(Math.round(s / 60), "min");
  else if (Math.abs(s) < 86400) txt = rel(Math.round(s / 3600), "h");
  else if (Math.abs(s) < 86400 * 14) txt = rel(Math.round(s / 86400), "d");
  else txt = d.toLocaleDateString();
  return h("time", { datetime: iso, title: d.toLocaleString(), dataset: { rel: iso } }, txt);
}
const rel = (n, u) => (n >= 0 ? `${n} ${u} ago` : `in ${-n} ${u}`);
function dur(sec) {
  if (sec === null || sec === undefined) return "—";
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return [d && `${d}d`, hh && `${hh}h`, (m && !d) && `${m}m`].filter(Boolean).join(" ") || `${sec}s`;
}
function clock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const hh = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return `${hh}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
/** A timer that ticks on the client (no network): <span data-since=iso>. */
const ticker = (iso, fmt = "clock") => h("span", { class: "tick num", dataset: { since: iso, fmt } }, fmt === "clock" ? clock((Date.now() - new Date(iso)) / 1000) : dur((Date.now() - new Date(iso)) / 1000));
setInterval(() => {
  for (const el of $$("[data-since]")) { const s = (Date.now() - new Date(el.dataset.since)) / 1000; el.textContent = el.dataset.fmt === "clock" ? clock(s) : dur(s); }
}, 1000);
setInterval(() => { for (const el of $$("time[data-rel]")) { const n = when(el.dataset.rel); if (n.textContent !== el.textContent) el.textContent = n.textContent; } }, 30000);
function initials(name) { return String(name || "?").replace(/[^A-Za-z0-9 _]/g, "").split(/[ _]+/).filter(Boolean).slice(0, 2).map((s) => s[0]).join("").toUpperCase() || "?"; }
function avatar(name, src, size = "") {
  return src ? h("img", { class: `avatar ${size}`, src, alt: "", loading: "lazy" }) : h("span", { class: `avatar initials ${size}`, "aria-hidden": "true" }, initials(name));
}
function who(id, name, avatarUrl) {
  if (!id && !name) return h("span", { class: "muted" }, "—");
  return h("span", { class: "user", title: id ? `ID ${id}` : null }, avatar(name || id, avatarUrl, "xs"), h("span", { class: "user-name" }, name || `User ${id}`));
}
const PR = { critical: "Critical", high: "High", medium: "Medium", routine: "Routine", low: "Low" };
const prPill = (p) => p ? h("span", { class: `pill p-${p}` }, PR[p] || p) : h("span", { class: "pill s-off" }, "Assessing");
const pill = (text, kind = "info") => h("span", { class: `pill s-${kind}` }, text);
const yes = (v, a = "On", b = "Off") => v ? pill(a, "ok") : pill(b, "off");
const dot = (kind) => h("span", { class: `dot d-${kind}`, "aria-hidden": "true" });
const localToUtc = (v) => (v ? new Date(v).toISOString() : null);
const utcToLocal = (iso) => { if (!iso) return ""; const d = new Date(iso); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
const idOf = (txt) => { const m = String(txt || "").match(/(\d{15,22})/); return m ? m[1] : null; };
const tip = (el, text) => { el.setAttribute("data-tip", text); return el; };

// ============================================================================ components
function modal({ title, body, actions = [], wide = false, onClose, size }) {
  const back = h("div", { class: "modal-back" });
  const prevFocus = document.activeElement;
  const close = () => { back.classList.add("out"); setTimeout(() => back.remove(), 160); document.removeEventListener("keydown", esc); onClose && onClose(); prevFocus && prevFocus.focus && prevFocus.focus({ preventScroll: true }); };
  const esc = (e) => { if (e.key === "Escape" && back === $$(".modal-back").pop()) { e.stopPropagation(); close(); } };
  const foot = h("footer");
  for (const a of actions) {
    const b = h("button", { class: "btn" + (a.primary ? " primary" : "") + (a.danger ? " danger" : "") + (a.ghost ? " ghost" : ""), type: "button" }, a.icon ? icon(a.icon) : null, a.label);
    b.addEventListener("click", async () => {
      if (!a.onClick) return close();
      b.disabled = true; b.classList.add("busy");
      try { const keep = await a.onClick(close); if (keep !== true && a.closes !== false) close(); }
      catch (e) { if (e && e.message) toast(e.message, true); }
      finally { b.disabled = false; b.classList.remove("busy"); }
    });
    foot.append(b);
  }
  const box = h("div", { class: "modal" + (wide ? " wide" : "") + (size ? ` ${size}` : ""), role: "dialog", "aria-modal": "true", "aria-label": title },
    h("header", null, h("h2", null, title), h("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: close }, icon("x"))),
    h("div", { class: "body" }, body), actions.length ? foot : null);
  back.append(box);
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  document.addEventListener("keydown", esc);
  document.body.append(back);
  setTimeout(() => (box.querySelector(".body input:not([type=checkbox]),.body select,.body textarea") || box.querySelector("footer .primary") || box.querySelector("button"))?.focus(), 40);
  return { close, box };
}
function confirmDialog(title, text, label = "Confirm", danger = true) {
  return new Promise((resolve) => {
    let done = false;
    modal({ title, size: "narrow", body: h("div", { class: "confirm" }, h("div", { class: `confirm-ico ${danger ? "bad" : "info"}` }, icon(danger ? "alert" : "info")), h("p", null, text)),
            onClose: () => { if (!done) resolve(false); },
            actions: [{ label: "Cancel", ghost: true }, { label, danger, primary: !danger, onClick: () => { done = true; resolve(true); } }] });
  });
}
function emptyState(title, text, action, ico = "layers") {
  return h("div", { class: "empty" }, h("div", { class: "empty-ico" }, icon(ico)), h("b", null, title), text ? h("p", null, text) : null,
    action ? h("div", { class: "empty-act" }, action) : null);
}
function table(cols, rows, { onRow, empty, rowClass, dense } = {}) {
  if (!rows || !rows.length) return empty || emptyState("Nothing here yet", null);
  const tb = h("tbody");
  for (const r of rows) {
    const tr = h("tr", { class: [onRow ? "click" : "", rowClass ? rowClass(r) : ""].join(" ").trim() || null, tabindex: onRow ? 0 : null });
    for (const c of cols) {
      const v = c.render ? c.render(r) : r[c.key];
      tr.append(h("td", { "data-label": c.label, class: c.cls || null }, v === null || v === undefined || v === "" ? h("span", { class: "muted" }, "—") : v));
    }
    if (onRow) {
      tr.addEventListener("click", (e) => { if (!e.target.closest("button,a,select,input,label")) onRow(r); });
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === tr) onRow(r); });
    }
    tb.append(tr);
  }
  return h("div", { class: "tbl-wrap" }, h("table", { class: "stack" + (dense ? " dense" : "") }, h("thead", null, h("tr", null, cols.map((c) => h("th", { class: c.cls || null }, c.label)))), tb));
}
function pager(data, goTo) {
  if (!data) return null;
  if (data.pages <= 1) return data.total ? h("div", { class: "pager" }, h("span", { class: "muted" }, `${fmtNum(data.total)} ${data.total === 1 ? "result" : "results"}`)) : null;
  const nums = [];
  const p = data.page, n = data.pages;
  const set = new Set([1, n, p - 1, p, p + 1].filter((x) => x >= 1 && x <= n));
  let last = 0;
  for (const x of [...set].sort((a, b) => a - b)) {
    if (x - last > 1) nums.push(h("span", { class: "gap" }, "…"));
    nums.push(h("button", { class: "pg" + (x === p ? " on" : ""), "aria-current": x === p ? "page" : null, onclick: () => goTo(x) }, x));
    last = x;
  }
  return h("div", { class: "pager" }, h("span", { class: "muted" }, `${fmtNum((p - 1) * data.per_page + 1)}–${fmtNum(Math.min(p * data.per_page, data.total))} of ${fmtNum(data.total)}`),
    h("span", { class: "pg-row" }, h("button", { class: "pg", "aria-label": "Previous page", disabled: p <= 1, onclick: () => goTo(p - 1) }, icon("chevronLeft")), nums,
      h("button", { class: "pg", "aria-label": "Next page", disabled: p >= n, onclick: () => goTo(p + 1) }, icon("chevronRight"))));
}
function tabs(items, current, onPick) {
  const bar = h("div", { class: "tabs", role: "tablist" });
  for (const [k, label, count] of items) {
    bar.append(h("button", { class: k === current ? "on" : null, role: "tab", "aria-selected": k === current ? "true" : "false", onclick: () => {
      for (const b of bar.children) { b.classList.remove("on"); b.setAttribute("aria-selected", "false"); }
      bar.children[items.findIndex((i) => i[0] === k)].classList.add("on");
      bar.children[items.findIndex((i) => i[0] === k)].setAttribute("aria-selected", "true");
      onPick(k);
    } }, label, count !== undefined && count !== null ? h("span", { class: "tab-count" }, fmtNum(count)) : null));
  }
  return bar;
}
function panel(title, body, ...headerExtra) {
  return h("section", { class: "panel" }, title ? h("header", null, h("h2", null, title), h("span", { class: "grow" }), headerExtra) : null, h("div", { class: "panel-body" }, body));
}
function card(opts) {
  // opts: { title, icon, actions:[], body, footer, cls }
  return h("section", { class: "panel " + (opts.cls || "") },
    opts.title ? h("header", null, opts.icon ? h("span", { class: "panel-ico" }, icon(opts.icon)) : null, h("h2", null, opts.title), opts.sub ? h("span", { class: "panel-sub" }, opts.sub) : null,
      h("span", { class: "grow" }), opts.actions || null) : null,
    h("div", { class: "panel-body" + (opts.flush ? " flush" : "") }, opts.body), opts.footer ? h("footer", { class: "panel-foot" }, opts.footer) : null);
}
function stat({ label, value, sub, ico, tone = "", onClick, title }) {
  const tag = onClick ? "button" : "div";
  return h(tag, { class: `stat ${tone}`, onclick: onClick || null, type: onClick ? "button" : null, title: title || null },
    h("div", { class: "stat-top" }, h("span", { class: "stat-label" }, label), ico ? h("span", { class: "stat-ico" }, icon(ico)) : null),
    h("div", { class: "stat-value num" }, value), sub ? h("div", { class: "stat-sub" }, sub) : h("div", { class: "stat-sub muted" }, " "));
}
function skeleton(kind = "page") {
  if (kind === "rows") return h("div", { class: "sk-wrap" }, Array.from({ length: 6 }, () => h("div", { class: "sk sk-row" })));
  if (kind === "stats") return h("div", { class: "stats" }, Array.from({ length: 4 }, () => h("div", { class: "sk sk-stat" })));
  return h("div", { class: "sk-page", "aria-busy": "true", "aria-label": "Loading" },
    h("div", { class: "sk sk-title" }), h("div", { class: "sk sk-sub" }),
    h("div", { class: "stats" }, Array.from({ length: 4 }, () => h("div", { class: "sk sk-stat" }))),
    h("div", { class: "grid" }, h("div", { class: "col-8" }, h("div", { class: "sk sk-panel" })), h("div", { class: "col-4" }, h("div", { class: "sk sk-panel" }))));
}
function loading() { return h("div", { class: "loading", role: "status" }, h("span", { class: "spinner" }), "Loading"); }
function errorBox(e) { return h("div", { class: "error-box", role: "alert" }, icon("alert"), h("div", null, h("b", null, "Couldn't load this. "), e.message || String(e))); }
function callout(text, kind = "info", ico) { return h("div", { class: `callout c-${kind}` }, icon(ico || (kind === "warn" || kind === "bad" ? "alert" : "info")), h("div", null, text)); }
function searchBox(placeholder, onInput, value = "") {
  const inp = h("input", { type: "search", placeholder, value, "aria-label": placeholder });
  inp.addEventListener("input", debounce(() => onInput(inp.value), 280));
  return h("label", { class: "search-in" }, icon("search"), inp);
}
function chips(items, current, onPick) {
  const row = h("div", { class: "chips", role: "group" });
  for (const [k, label] of items) {
    row.append(h("button", { class: "chip" + (k === current ? " on" : ""), "aria-pressed": k === current ? "true" : "false", onclick: (e) => {
      for (const b of row.children) { b.classList.remove("on"); b.setAttribute("aria-pressed", "false"); }
      e.currentTarget.classList.add("on"); e.currentTarget.setAttribute("aria-pressed", "true"); onPick(k);
    } }, label));
  }
  return row;
}
function menuButton(label, items, ico = "dots") {
  const btn = h("button", { class: label ? "btn" : "icon-btn", "aria-haspopup": "menu", "aria-label": label || "More actions" }, icon(ico), label);
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    $(".menu-pop")?.remove();
    const r = btn.getBoundingClientRect();
    const pop = h("div", { class: "menu-pop", role: "menu" }, items.filter(Boolean).map((it) => h("button", { role: "menuitem", class: it.danger ? "danger" : null,
      onclick: () => { pop.remove(); it.onClick(); } }, it.icon ? icon(it.icon) : null, it.label)));
    document.body.append(pop);
    const w = pop.offsetWidth;
    pop.style.top = `${r.bottom + window.scrollY + 6}px`;
    pop.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w))}px`;
    setTimeout(() => document.addEventListener("click", () => pop.remove(), { once: true }), 0);
    pop.querySelector("button")?.focus();
  });
  return btn;
}

// ---------------------------------------------------------------------------- form fields
function channelSelect(value, { allowNone = true, label = "No channel" } = {}) {
  const s = h("select", null, allowNone ? h("option", { value: "" }, label) : null);
  const groups = {};
  for (const c of S.meta.channels) (groups[c.category || "No category"] ||= []).push(c);
  for (const [cat, chs] of Object.entries(groups)) s.append(h("optgroup", { label: cat }, chs.map((c) => h("option", { value: c.id, selected: c.id === String(value || "") }, `#${c.name}`))));
  return s;
}
function roleSelect(value, { allowNone = true, multiple = false } = {}) {
  const vals = new Set((Array.isArray(value) ? value : [value]).filter(Boolean).map(String));
  const s = h("select", { multiple, size: multiple ? 6 : null }, !multiple && allowNone ? h("option", { value: "" }, "No role") : null);
  for (const r of S.meta.roles) if (!r.managed) s.append(h("option", { value: r.id, selected: vals.has(r.id) }, `@${r.name}${r.assignable ? "" : " (above the bot)"}`));
  return s;
}
function memberPicker(value, placeholder = "Search members or paste a user ID") {
  const list = h("datalist", { id: "dl" + Math.random().toString(36).slice(2) });
  const inp = h("input", { type: "text", list: list.id, placeholder, value: value || "", autocomplete: "off" });
  const search = debounce(async () => {
    if (!can("members.view") || inp.value.length < 2 || idOf(inp.value)) return;
    try {
      const d = await api(`/members?q=${encodeURIComponent(inp.value)}`);
      clear(list, d.items.slice(0, 15).map((m) => h("option", { value: `${m.display_name || m.username} (${m.user_id})` })));
    } catch { /* search is a convenience */ }
  }, 250);
  inp.addEventListener("input", search);
  const wrap = h("span", { class: "picker" }, inp, list);
  wrap.getValue = () => idOf(inp.value);
  return wrap;
}
function playerPicker(value = "", placeholder = "Roblox username or user ID") {
  const list = h("datalist", { id: "pl" + Math.random().toString(36).slice(2) });
  const inp = h("input", { type: "text", list: list.id, placeholder, value, autocomplete: "off", maxlength: 40 });
  inp.addEventListener("input", debounce(async () => {
    if (!can("erlc.view") || inp.value.length < 2) return;
    try { const d = await api(`/erlc/players?q=${encodeURIComponent(inp.value)}`); clear(list, d.items.slice(0, 12).map((p) => h("option", { value: p.username }, p.online ? "In-game now" : `Roblox ${p.roblox_id}`))); }
    catch { /* convenience only */ }
  }, 250));
  const wrap = h("span", { class: "picker" }, inp, list);
  wrap.getValue = () => inp.value.trim();
  return wrap;
}
const sel = (opts, value) => h("select", null, Object.entries(opts).map(([k, v]) => h("option", { value: k, selected: String(value) === String(k) }, v)));
/** form([{key,label,type,help,options,...}], values) -> { el, get() } */
function form(fields, values = {}) {
  const el = h("div", { class: "form" }), getters = {};
  for (const f of fields) {
    if (f.when && !f.when()) continue;
    const v = values[f.key] ?? f.default ?? "";
    let input, get;
    switch (f.type) {
      case "check": {
        input = h("input", { type: "checkbox", checked: !!v });
        el.append(h("label", { class: "check" }, input, h("span", { class: "switch", "aria-hidden": "true" }), h("span", null, f.label, f.help ? h("small", null, f.help) : null)));
        getters[f.key] = () => input.checked; continue;
      }
      case "textarea": input = h("textarea", { placeholder: f.placeholder, maxlength: f.max || null, rows: f.rows || 3 }, v); get = () => input.value; break;
      case "number": input = h("input", { type: "number", value: v, min: f.min ?? null, max: f.max ?? null }); get = () => (input.value === "" ? null : Number(input.value)); break;
      case "select": input = sel(f.options, v); get = () => input.value; break;
      case "channel": input = channelSelect(v); get = () => input.value || null; break;
      case "role": input = roleSelect(v); get = () => input.value || null; break;
      case "roles": input = roleSelect(v, { multiple: true }); get = () => [...input.selectedOptions].map((o) => o.value); break;
      case "member": input = memberPicker(v); get = () => input.getValue(); break;
      case "player": input = playerPicker(v); get = () => input.getValue(); break;
      case "datetime": input = h("input", { type: "datetime-local", value: utcToLocal(v) }); get = () => localToUtc(input.value); break;
      case "date": input = h("input", { type: "date", value: v, min: f.min || null }); get = () => input.value; break;
      case "color": input = h("input", { type: "color", value: v || "#3D8BFD" }); get = () => input.value; break;
      case "password": input = h("input", { type: "password", autocomplete: "off", placeholder: f.placeholder }); get = () => input.value; break;
      case "lines": input = h("textarea", { placeholder: f.placeholder, rows: 4 }, (v || []).join("\n")); get = () => input.value.split("\n").map((s) => s.trim()).filter(Boolean); break;
      default: input = h("input", { type: "text", value: v, placeholder: f.placeholder, maxlength: f.max || null }); get = () => input.value;
    }
    const help = f.help || (f.type === "roles" ? "Hold Ctrl (or ⌘ on Mac) to select several." : null);
    el.append(h("label", { class: "field" + (f.half ? " half" : "") }, h("span", null, f.label, f.optional ? h("em", null, " optional") : null), input, help ? h("small", null, help) : null));
    getters[f.key] = get;
  }
  return { el, get: () => Object.fromEntries(Object.entries(getters).map(([k, g]) => [k, g()])) };
}
function settingsPanel(title, fields, values, save, { note } = {}) {
  const f = form(fields, values);
  const btn = h("button", { class: "btn primary", onclick: async () => {
    btn.disabled = true;
    try { await act(() => save(f.get()), "Saved."); } finally { btn.disabled = false; }
  } }, icon("check"), "Save changes");
  return panel(title, h("div", null, note ? callout(note) : null, f.el, h("div", { class: "form-actions" }, btn)));
}

// ---------------------------------------------------------------------------- charts (SVG, no library)
const COLORS = ["var(--accent)", "var(--amber)", "var(--green)", "var(--red)", "var(--violet)", "var(--cyan)"];
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  for (const k of kids.flat()) if (k) el.append(k instanceof Node ? k : document.createTextNode(k));
  return el;
}
function chart(days, series, { type, height = 200 } = {}) {
  const W = 640, H = height, L = 34, B = 22, T = 10, R = 12;
  const max = Math.max(1, ...series.flatMap((s) => s.values));
  const nice = max <= 5 ? 5 : Math.ceil(max / 4) * 4;
  const x = (i) => L + (days.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (days.length - 1));
  const y = (v) => T + (H - T - B) * (1 - v / nice);
  const gid = "g" + Math.random().toString(36).slice(2, 8);
  const root = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
                            "aria-label": series.map((s) => `${s.label}: ${s.values.reduce((a, b) => a + b, 0)}`).join(", ") });
  const defs = svg("defs");
  series.forEach((s, si) => defs.append(svg("linearGradient", { id: `${gid}-${si}`, x1: 0, x2: 0, y1: 0, y2: 1 },
    svg("stop", { offset: "0%", "stop-color": COLORS[si % COLORS.length], "stop-opacity": si === 0 ? .28 : .12 }),
    svg("stop", { offset: "100%", "stop-color": COLORS[si % COLORS.length], "stop-opacity": 0 }))));
  root.append(defs);
  for (let g = 0; g <= 4; g++) {
    const v = (nice / 4) * g;
    root.append(svg("line", { class: "grid", x1: L, x2: W - R, y1: y(v), y2: y(v) }), svg("text", { class: "axis", x: L - 8, y: y(v) + 4, "text-anchor": "end" }, String(Math.round(v * 10) / 10)));
  }
  const step = Math.max(1, Math.ceil(days.length / 7));
  days.forEach((d, i) => { if (i % step === 0 || i === days.length - 1) root.append(svg("text", { class: "axis", x: x(i), y: H - 5, "text-anchor": "middle" }, String(d).slice(5))); });
  if ((type || (series.length === 1 ? "bar" : "line")) === "bar") {
    const bw = Math.max(3, Math.min(26, ((W - L - R) / days.length) * 0.62));
    series[0].values.forEach((v, i) => root.append(svg("rect", { class: "bar", x: x(i) - bw / 2, y: y(v), width: bw, height: Math.max(0, H - B - y(v)), rx: 3 },
      svg("title", null, `${days[i]}: ${v} ${String(series[0].label).toLowerCase()}`))));
  } else {
    series.forEach((s, si) => {
      const pts = s.values.map((v, i) => `${x(i)},${y(v)}`);
      root.append(svg("polygon", { fill: `url(#${gid}-${si})`, points: `${x(0)},${y(0)} ${pts.join(" ")} ${x(s.values.length - 1)},${y(0)}` }));
      root.append(svg("polyline", { class: "line", stroke: COLORS[si % COLORS.length], points: pts.join(" ") }));
      s.values.forEach((v, i) => root.append(svg("circle", { class: "pt", cx: x(i), cy: y(v), r: 3, fill: COLORS[si % COLORS.length] }, svg("title", null, `${days[i]} · ${s.label}: ${v}`))));
    });
  }
  const legend = series.length > 1 ? h("div", { class: "legend" }, series.map((s, i) => h("span", null, h("i", { style: `background:${COLORS[i % COLORS.length]}` }), `${s.label}`, h("b", null, fmtNum(s.values.reduce((a, b) => a + b, 0)))))) : null;
  return h("div", { class: "chart-wrap" }, root, legend);
}
function bars(items) { // horizontal bars: [{label, value, tone}]
  const max = Math.max(1, ...items.map((i) => i.value));
  return h("div", { class: "hbars" }, items.map((i) => h("div", { class: "hbar" }, h("span", { class: "hbar-label" }, i.label), h("span", { class: "hbar-track" },
    h("span", { class: `hbar-fill ${i.tone || ""}`, style: `width:${Math.max(2, (i.value / max) * 100)}%` })), h("b", { class: "num" }, fmtNum(i.value)))));
}

// ============================================================================ router & shell
const PAGES = {};
function page(path, title, fn, opts = {}) { PAGES[path] = { title, fn, ...opts }; }
function go(path) { location.hash = `#/g/${S.gid}/${path}`; }
function link(path, text) { return h("a", { href: `#/g/${S.gid}/${path}` }, text); }
const navPaths = () => new Set(S.meta.nav.flatMap((g) => g.items.map((i) => i.path)));

async function boot() {
  const app = $("#app");
  if (!S.me) {
    try {
      const r = await fetch("/api/me", { credentials: "same-origin" });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `The bot didn't answer properly (HTTP ${r.status}). Make sure the bot and its tunnel are running.`);
      S.me = data;
    } catch (e) { return clear(app, gate(errorBox(e), h("button", { class: "btn", onclick: () => boot() }, icon("refresh"), "Try again"))); }
  }
  if (!S.me.signed_in) return renderLogin(app);
  route();
}
function gate(...kids) {
  return h("main", { class: "gate" }, h("div", { class: "gate-glow", "aria-hidden": "true" }), h("div", { class: "gate-box" },
    h("img", { class: "gate-banner", src: "/static/brand/banner.png?v=5.0.1", alt: "Hive Dispatch | ER:LC" }), kids));
}
function renderLogin(app) {
  clear(app, gate(h("h1", null, "Sign in to your command center"),
    h("p", { class: "muted" }, "Use your Discord account. You'll only see the servers and sections your roles allow."),
    S.me.login_ready ? h("a", { class: "btn primary lg", href: "/auth/login" }, icon("login"), "Sign in with Discord")
      : callout("Sign-in isn't set up yet. The bot owner needs to add DISCORD_CLIENT_SECRET to the .env file and restart the bot (see docs/DASHBOARD.md or docs/NETLIFY.md).", "warn"),
    h("p", { class: "gate-foot" }, "100% free · Developed by Drone Hive Studios")));
}
function renderPicker(app) {
  disconnect();
  $("#lightbar").classList.remove("alert");
  const open = S.me.guilds.filter((g) => g.can_open), closed = S.me.guilds.filter((g) => !g.can_open);
  clear(app, gate(h("h1", null, `Welcome, ${S.me.user.name}`),
    h("p", { class: "muted" }, open.length ? "Choose a server to manage." : "None of your servers have given your roles dashboard access yet."),
    h("div", { class: "guilds" },
      open.map((g) => h("a", { href: `#/g/${g.id}/dashboard` }, g.icon ? h("img", { src: g.icon, alt: "" }) : h("span", { class: "gi" }, initials(g.name)), h("b", null, g.name), icon("chevronRight"))),
      closed.map((g) => h("div", { class: "off", title: "Your roles don't include dashboard access here" }, g.icon ? h("img", { src: g.icon, alt: "" }) : h("span", { class: "gi" }, initials(g.name)), h("span", null, g.name, h("small", null, "No access"))))),
    h("button", { class: "btn ghost sm", onclick: logout }, icon("logout"), "Sign out")));
}
async function route() {
  const m = location.hash.match(/^#\/g\/(\d+)\/?(.*)$/);
  if (!m) {
    // Short links from notifications ("#/bolos/3") open in the current server.
    const short = location.hash.match(/^#\/([a-z].*)$/);
    if (short && S.gid) { location.replace(`#/g/${S.gid}/${short[1]}`); return; }
    if (short && S.me.guilds.filter((g) => g.can_open).length === 1) { location.replace(`#/g/${S.me.guilds.find((g) => g.can_open).id}/${short[1]}`); return; }
    return renderPicker($("#app"));
  }
  const [, gid, rest] = m;
  if (gid !== S.gid || !S.meta) {
    S.gid = gid; S.meta = null;
    clear($("#app"), h("div", { class: "boot" }, h("img", { src: "/static/brand/mark-192.png", alt: "" }), h("span", { class: "spinner" })));
    try { S.meta = await api("/meta"); }
    catch (e) { S.gid = null; return clear($("#app"), gate(errorBox(e), h("a", { class: "btn", href: "#/" }, "Back to your servers"))); }
    S.perms = new Set(S.meta.permissions);
    S.prefs = await api("/prefs").catch(() => ({}));
    renderShell();
    connect();
    maybeOnboard();
  }
  renderPage(rest || "dashboard");
}
function renderShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Sections" });
  const closedGroups = new Set(store.get("pd-nav-closed", []));
  for (const g of S.meta.nav) {
    const grp = h("div", { class: "nav-group" + (closedGroups.has(g.group) ? " closed" : "") });
    grp.append(h("button", { type: "button", class: "nav-head", "aria-expanded": closedGroups.has(g.group) ? "false" : "true", onclick: (e) => {
      grp.classList.toggle("closed");
      e.currentTarget.setAttribute("aria-expanded", grp.classList.contains("closed") ? "false" : "true");
      store.set("pd-nav-closed", $$(".nav-group.closed > .nav-head", nav).map((b) => b.dataset.group));
    }, dataset: { group: g.group } }, h("span", null, g.group), icon("chevronDown", "caret")),
    h("ul", null, g.items.map((i) => h("li", null, h("a", { href: `#/g/${S.gid}/${i.path}`, dataset: { path: i.path } }, icon(NAV_ICONS[i.path] || "dots"), h("span", null, i.label), h("span", { class: "nav-badge hidden", dataset: { badge: i.path } }))))));
    nav.append(grp);
  }
  const guildSel = h("select", { "aria-label": "Server", onchange: (e) => { location.hash = `#/g/${e.target.value}/dashboard`; } },
    S.me.guilds.filter((g) => g.can_open).map((g) => h("option", { value: g.id, selected: g.id === S.gid }, g.name)));
  const bell = h("button", { class: "icon-btn bell", "aria-label": "Notifications", onclick: () => openNotifications() }, icon("bell"), h("span", { class: "count hidden", id: "bell-count" }));
  clear($("#app"), h("div", { class: "shell" },
    h("aside", { class: "side", "aria-label": "Main menu" },
      h("a", { class: "side-head", href: `#/g/${S.gid}/dashboard` }, h("img", { class: "brand-mark", src: "/static/brand/mark-192.png", alt: "" }),
        h("div", { class: "brand-name" }, h("b", null, "HIVE DISPATCH"), h("span", null, "ER:LC COMMAND CENTER"))),
      h("div", { class: "side-guild" }, S.meta.guild.icon ? h("img", { src: S.meta.guild.icon, alt: "" }) : h("span", { class: "gi" }, initials(S.meta.guild.name)), guildSel, icon("chevronDown", "caret")),
      nav,
      h("div", { class: "side-foot" }, avatar(S.me.user.name, S.me.user.avatar, "sm"),
        h("div", { class: "side-user" }, h("b", null, S.me.user.name), h("small", null, `v${S.me.version} · free forever`)),
        tip(h("button", { class: "icon-btn", "aria-label": "Sign out", onclick: logout }, icon("logout")), "Sign out"))),
    h("div", { class: "scrim", onclick: () => document.body.classList.remove("nav-open") }),
    h("div", { class: "main" },
      h("header", { class: "top" },
        h("button", { class: "icon-btn burger", "aria-label": "Open menu", onclick: () => document.body.classList.toggle("nav-open") }, icon("menu")),
        h("div", { class: "crumbs", id: "crumbs" }),
        h("button", { class: "top-search", onclick: () => openPalette(), "aria-label": "Search and commands" }, icon("search"), h("span", null, "Search players, members, cases…"), h("kbd", null, navigator.platform.includes("Mac") ? "⌘K" : "Ctrl K")),
        h("span", { class: "grow" }),
        h("span", { class: "live-ind", id: "live-ind", title: "Live updates" }, dot("off"), h("span", null, "Connecting")),
        can("assistant.use") ? tip(h("button", { class: "icon-btn", "aria-label": "Ask Hive", onclick: openAssistant }, icon("sparkles")), "Ask Hive") : null,
        bell),
      h("main", { class: "content", id: "content", tabindex: "-1" }))));
  document.addEventListener("click", (e) => { if (document.body.classList.contains("nav-open") && e.target.closest(".nav a")) document.body.classList.remove("nav-open"); });
  refreshBell();
}
function resolvePage(path) {
  const parts = path.split("/").filter(Boolean);
  for (let n = parts.length; n > 0; n--) { const k = parts.slice(0, n).join("/"); if (PAGES[k]) return [k, parts.slice(n)]; }
  return [null, []];
}
/** Navigate (full render with skeleton) or, when the same page is asked again, refresh it softly in place. */
async function renderPage(path) {
  const el = $("#content");
  if (!el) return;
  const [key, args] = resolvePage(path);
  if (key && key === S.key && path === S.path && el.childNodes.length) return softRender(key, args);
  S.refresh = null; S.path = path; S.key = key;
  const navKey = key && !navPaths().has(key) ? (S.meta.nav.flatMap((g) => g.items.map((i) => i.path)).find((p) => key.startsWith(p + "/")) || key) : key;
  for (const a of $$(".nav a")) { const on = a.dataset.path === navKey; a.classList.toggle("on", on); if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); }
  if (!key) return clear(el, emptyState("Page not found", "Pick a section from the menu.", h("a", { class: "btn", href: `#/g/${S.gid}/dashboard` }, "Go to dashboard"), "search"));
  const p = PAGES[key];
  const grp = S.meta.nav.find((g) => g.items.some((i) => i.path === navKey));
  clear($("#crumbs"), grp ? h("span", { class: "muted" }, grp.group) : null, grp ? icon("chevronRight") : null, h("b", null, p.title));
  document.title = `${p.title} · ${S.meta.guild.name} — Hive Dispatch`;
  remember(key, p.title);
  clear(el, skeleton());
  window.scrollTo(0, 0);
  try { await p.fn(el, args); }
  catch (e) { clear(el, errorBox(e)); }
  el.focus({ preventScroll: true });
}
let softBusy = false;
/** Legacy pages re-render into an off-screen copy that replaces the old one in one step, keeping scroll, focus and typed input. */
async function softRender(key, args) {
  if (softBusy) return;
  const el = $("#content");
  // Don't touch the page while someone is typing or has a menu open: try again shortly.
  const ae = document.activeElement;
  if (ae && el.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) return void setTimeout(() => { if (S.key === key) softRender(key, args); }, 2500);
  softBusy = true;
  const tmp = h("div");
  const keepRefresh = S.refresh;
  try {
    await PAGES[key].fn(tmp, args);
    if (S.key !== key) return;
    const y = window.scrollY;
    const oldFields = $$("input,select,textarea", el), newFields = $$("input,select,textarea", tmp);
    if (oldFields.length === newFields.length) oldFields.forEach((o, i) => {
      const n = newFields[i];
      if (o.dataset.dirty && o.type === n.type && o.tagName === n.tagName) { if (o.type === "checkbox") n.checked = o.checked; else n.value = o.value; n.dataset.dirty = "1"; }
    });
    const openDetails = $$("details", el).map((d) => d.open);
    $$("details", tmp).forEach((d, i) => { if (openDetails[i]) d.open = true; });
    el.replaceChildren(...tmp.childNodes);
    window.scrollTo(0, y);
  } catch { S.refresh = keepRefresh; /* keep showing the last good version */ }
  finally { softBusy = false; }
}
document.addEventListener("input", (e) => { if (e.target.matches?.("input,select,textarea")) e.target.dataset.dirty = "1"; }, true);
function head(title, desc, ...actions) {
  return h("div", { class: "page-head" }, h("div", { class: "grow" }, h("h1", null, title), desc ? h("p", null, desc) : null), h("div", { class: "page-actions" }, actions));
}
function remember(path, title) {
  if (path === "dashboard") return;
  const key = `pd-recent-${S.gid}`;
  const list = store.get(key, []).filter((r) => r.path !== path);
  list.unshift({ path, title });
  store.set(key, list.slice(0, 6));
}
async function logout() {
  try { await fetch("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": S.me?.csrf || "" }, credentials: "same-origin" }); } catch { /* signing out anyway */ }
  S.me = null; S.gid = null; S.meta = null; disconnect(); location.hash = "#/"; boot();
}
async function savePrefs(changes) {
  S.prefs = { ...(S.prefs || {}), ...changes };
  try { await api("/prefs", { method: "PUT", body: S.prefs, quiet: true }); } catch { /* preferences are a convenience */ }
}

// ---------------------------------------------------------------------------- live updates
/* Which events matter to which page. A page only refreshes (in place) when one of its topics fires. */
const TOPICS = [
  [/^dashboard$/, /^(incident|assignment|unit|shift|mod|erlc\.(players|record)|bolo|application|session|verification|member|staff|notification)/],
  [/^activity$/, /./],
  [/^erlc/, /^(erlc\.(players|status|record)|bolo)/],
  [/^moderation/, /^(mod\.|erlc\.record|report)/],
  [/^bolos/, /^bolo/],
  [/^(staff|shifts)/, /^(shift|staff)/],
  [/^(dispatch|units|incidents)/, /^(incident|assignment|unit|backup|bolo)/],
  [/^members/, /^(member|verification)/], [/^verification/, /^verification/], [/^applications/, /^application/], [/^sessions/, /^session/],
];
let pendingRefresh = false;
const runRefresh = debounce(() => {
  if (document.hidden) { pendingRefresh = true; return; }
  pendingRefresh = false;
  if (S.refresh && !S.sessionLost) Promise.resolve(S.refresh()).catch(() => {});
}, 1200);
function wants(name) {
  const key = S.key || "";
  for (const [page_, topic] of TOPICS) if (page_.test(key)) return topic.test(name);
  return false;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && pendingRefresh) runRefresh(); });
function setLive(state) {
  const el = $("#live-ind"); if (!el) return;
  const map = { live: ["ok", "Live"], poll: ["warn", "Auto-refresh"], off: ["off", "Offline"], connecting: ["off", "Connecting"] };
  const [kind, text] = map[state];
  clear(el, dot(kind), h("span", null, text));
  el.title = state === "live" ? "Live updates are on" : state === "poll" ? "Live connection unavailable here — checking every 20 seconds" : "Not connected";
}
function connect() {
  disconnect();
  if (!window.EventSource) return startPolling();
  const lights = debounce(updateLightbar, 2500);
  S.es = new EventSource(`/api/g/${S.gid}/events`);
  let failures = 0;
  setLive("connecting");
  S.es.onopen = () => { failures = 0; setLive("live"); };
  S.es.onmessage = (e) => {
    failures = 0; setLive("live");
    const name = e.data || "";
    if (name.startsWith("notification")) refreshBell();
    if (name.startsWith("incident") || name.startsWith("assignment")) lights();
    if (wants(name)) runRefresh();
  };
  // Some hosts/proxies cut long requests: fall back to refreshing every 20 s.
  S.es.onerror = () => {
    if (++failures < 4 || S.poll) return;
    S.es.close(); S.es = null; startPolling();
  };
  updateLightbar();
}
function startPolling() {
  setLive("poll");
  S.poll = setInterval(() => { if (document.hidden || S.sessionLost) return; refreshBell(); updateLightbar(); if (S.refresh) Promise.resolve(S.refresh()).catch(() => {}); }, 20000);
}
function disconnect() { if (S.es) { S.es.close(); S.es = null; } if (S.poll) { clearInterval(S.poll); S.poll = null; } }
async function updateLightbar() {
  if (!can("incident.view")) return;
  try {
    const d = await api("/dispatch");
    S.critical = d.incidents.filter((i) => i.effective_priority === "critical").length;
    $("#lightbar").classList.toggle("alert", S.critical > 0);
    $("#lightbar").title = S.critical ? `${S.critical} critical incident(s) open` : "";
  } catch { /* the lightbar is decoration on top of real data */ }
}

// ---------------------------------------------------------------------------- notifications center
async function refreshBell() {
  try {
    const d = await api("/notifications?unread=1");
    const c = $("#bell-count");
    if (c) { c.textContent = d.unread > 9 ? "9+" : String(d.unread); c.classList.toggle("hidden", !d.unread); }
    S.unread = d.unread;
  } catch { /* bell is optional */ }
}
function openNotifications() {
  if ($(".drawer")) return $(".drawer-back")?.click();
  const body = h("div", { class: "drawer-body" }, skeleton("rows"));
  let filter = "all";
  const back = h("div", { class: "drawer-back", onclick: () => close() });
  const close = () => { back.remove(); dr.classList.add("out"); setTimeout(() => dr.remove(), 200); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  const load = async () => {
    try {
      const d = await api("/notifications");
      const items = filter === "unread" ? d.items.filter((n) => !n.read) : d.items;
      swap(body, items.length ? items.map((n) => h("a", { class: "note" + (n.read ? "" : " unread"), href: n.link ? `#/g/${S.gid}/${n.link.replace(/^#\//, "")}` : "#",
        onclick: async () => { close(); if (!n.read) { await api("/notifications/read", { method: "POST", body: { ids: [n.id] } }).catch(() => {}); refreshBell(); } } },
        h("span", { class: `note-ico sev-${n.severity}` }, icon(n.severity === "critical" || n.severity === "warning" ? "alert" : n.kind === "staff" ? "badge" : n.kind === "moderation" ? "shield" : "bell")),
        h("span", { class: "note-main" }, h("b", null, n.title), n.body ? h("small", null, n.body) : null, h("small", { class: "muted" }, when(n.created_at))),
        n.read ? null : dot("accent")))
        : emptyState(filter === "unread" ? "You're all caught up" : "No notifications yet", "New applications, BOLO alerts, failures and critical incidents show up here.", null, "bell"));
      markAll.disabled = !d.unread;
    } catch (e) { swap(body, errorBox(e)); }
  };
  const markAll = h("button", { class: "btn sm", onclick: async () => { await act(() => api("/notifications/read", { method: "POST", body: { ids: "all" } }), "All marked as read."); refreshBell(); load(); } }, icon("check"), "Mark all read");
  const dr = h("aside", { class: "drawer", role: "dialog", "aria-label": "Notifications" },
    h("header", null, h("h2", null, "Notifications"), h("span", { class: "grow" }), markAll, h("button", { class: "icon-btn", "aria-label": "Close", onclick: () => close() }, icon("x"))),
    h("div", { class: "drawer-tabs" }, chips([["all", "All"], ["unread", "Unread"]], filter, (k) => { filter = k; load(); })), body);
  document.body.append(back, dr);
  document.addEventListener("keydown", esc);
  load();
}

// ---------------------------------------------------------------------------- command palette (⌘K / Ctrl K)
function paletteActions() {
  const a = [];
  if (can("shift.self")) a.push({ title: "Start or end my shift", icon: "clock", run: () => go("shifts") });
  if (can("erlc.moderate")) a.push({ title: "New in-game moderation action", icon: "gavel", run: () => window.erlcActionModal && window.erlcActionModal() });
  if (can("bolo.manage")) a.push({ title: "Create a BOLO", icon: "eye", run: () => window.boloCreate && window.boloCreate() });
  if (can("mod.warn")) a.push({ title: "New Discord moderation case", icon: "shield", run: () => typeof caseCreate === "function" && caseCreate() });
  if (can("incident.manual_create")) a.push({ title: "New incident", icon: "siren", run: () => typeof incidentCreate === "function" && incidentCreate(() => go("dispatch")) });
  if (can("shift.self")) a.push({ title: "Request leave (LOA)", icon: "calendar", run: () => go("staff/leave") });
  if (can("assistant.use")) a.push({ title: "Ask Hive", icon: "sparkles", run: openAssistant });
  a.push({ title: "Notifications", icon: "bell", run: openNotifications });
  a.push({ title: "Sign out", icon: "logout", run: logout });
  return a;
}
function openPalette() {
  if ($(".palette-back")) return;
  const inp = h("input", { type: "text", placeholder: "Search pages, players, members, cases, BOLOs… or type a command", "aria-label": "Search", autocomplete: "off" });
  const list = h("div", { class: "pal-list", role: "listbox" });
  let items = [], idx = 0, seq = 0;
  const back = h("div", { class: "palette-back", onmousedown: (e) => { if (e.target === back) close(); } });
  const close = () => { back.remove(); document.removeEventListener("keydown", key, true); };
  const pages = S.meta.nav.flatMap((g) => g.items.map((i) => ({ title: i.label, sub: g.group, icon: NAV_ICONS[i.path] || "dots", run: () => go(i.path) })));
  const draw = (groups) => {
    items = groups.flatMap((g) => g.items);
    idx = Math.min(idx, Math.max(0, items.length - 1));
    let n = 0;
    clear(list, items.length ? groups.filter((g) => g.items.length).map((g) => h("div", { class: "pal-group" }, h("div", { class: "pal-head" }, g.label),
      g.items.map((it) => { const i = n++; return h("button", { class: "pal-item" + (i === idx ? " on" : ""), role: "option", "aria-selected": i === idx ? "true" : "false",
        onmouseenter: () => { idx = i; mark(); }, onclick: () => { close(); it.run(); } }, h("span", { class: "pal-ico" }, icon(it.icon)), h("span", { class: "pal-text" }, h("b", null, it.title), it.sub ? h("small", null, it.sub) : null), i === idx ? h("kbd", null, "↵") : null); })))
      : h("div", { class: "pal-empty" }, "No matches."));
  };
  const mark = () => $$(".pal-item", list).forEach((b, i) => { b.classList.toggle("on", i === idx); b.setAttribute("aria-selected", i === idx ? "true" : "false"); if (i === idx) b.scrollIntoView({ block: "nearest" }); });
  const recent = store.get(`pd-recent-${S.gid}`, []).map((r) => ({ title: r.title, sub: "Recently visited", icon: NAV_ICONS[r.path] || "clock", run: () => go(r.path) }));
  const update = async () => {
    const q = inp.value.trim().toLowerCase();
    const fp = (arr) => arr.filter((x) => !q || x.title.toLowerCase().includes(q) || (x.sub || "").toLowerCase().includes(q));
    const base = [{ label: "Quick actions", items: fp(paletteActions()).slice(0, q ? 4 : 6) }, { label: q ? "Pages" : "Recently visited", items: q ? fp(pages).slice(0, 6) : recent.slice(0, 5) }];
    draw(base);
    if (q.length < 2) return;
    const my = ++seq;
    try {
      const r = await api(`/search?q=${encodeURIComponent(q)}`);
      if (my !== seq) return;
      draw([...base, { label: "Results", items: r.results.map((x) => ({ title: x.title, sub: `${x.kind}${x.subtitle ? " · " + x.subtitle : ""}`, icon: x.icon, run: () => go(x.link) })) }]);
    } catch { /* keep page results */ }
  };
  const key = (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); idx = Math.min(items.length - 1, idx + 1); mark(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); idx = Math.max(0, idx - 1); mark(); }
    else if (e.key === "Enter" && items[idx]) { e.preventDefault(); const it = items[idx]; close(); it.run(); }
  };
  inp.addEventListener("input", debounce(update, 160));
  document.addEventListener("keydown", key, true);
  back.append(h("div", { class: "palette", role: "dialog", "aria-label": "Search and commands" }, h("div", { class: "pal-in" }, icon("search"), inp, h("kbd", null, "Esc")), list,
    h("div", { class: "pal-foot" }, h("span", null, h("kbd", null, "↑"), h("kbd", null, "↓"), " navigate"), h("span", null, h("kbd", null, "↵"), " open"), h("span", null, h("kbd", null, "Esc"), " close"))));
  document.body.append(back);
  update();
  setTimeout(() => inp.focus(), 20);
}
document.addEventListener("keydown", (e) => {
  if (!S.meta) return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  else if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName) && !$(".modal-back")) { e.preventDefault(); openPalette(); }
});

// ---------------------------------------------------------------------------- onboarding
function maybeOnboard() {
  if (S.prefs?.onboarded || store.get(`pd-onboarded-${S.gid}`, false)) return;
  const steps = [
    ["Welcome to Hive Dispatch", "Your free ER:LC command center: live server, moderation, staff, BOLOs and dispatch in one place. Everything you see comes from real data.", "home"],
    ["Find anything fast", "Press Ctrl K (⌘K on Mac) or / to search players, members, cases and BOLOs, or to run quick actions like starting your shift.", "command"],
    ["It updates itself", "Joins, moderation, shifts and notifications arrive live. The page never reloads, so you never lose what you were typing.", "zap"],
  ];
  let i = 0;
  const body = h("div");
  const draw = () => clear(body, h("div", { class: "onboard" }, h("div", { class: "onboard-ico" }, icon(steps[i][2])), h("h3", null, steps[i][0]), h("p", null, steps[i][1]),
    h("div", { class: "onboard-dots" }, steps.map((_, j) => h("span", { class: j === i ? "on" : null })))));
  draw();
  const done = () => { store.set(`pd-onboarded-${S.gid}`, true); savePrefs({ onboarded: true }); };
  const m = modal({ title: "Getting started", size: "narrow", body, onClose: done, actions: [{ label: "Skip", ghost: true },
    { label: "Next", primary: true, onClick: () => { if (++i >= steps.length) return; draw(); if (i === steps.length - 1) m.box.querySelector("footer .primary").textContent = "Get started"; return true; } }] });
}

// ---------------------------------------------------------------------------- Ask Hive
function assistantBox() {
  const inp = h("input", { type: "text", placeholder: "Ask: any active calls? who's on shift? start my shift", maxlength: 500, "aria-label": "Ask Hive" });
  const out = h("div", { class: "ask-out", "aria-live": "polite" });
  const send = async () => {
    const q = inp.value.trim(); if (!q) return;
    clear(out, loading());
    try { showReply(out, await api("/assistant", { method: "POST", body: { question: q } })); }
    catch (e) { clear(out, errorBox(e)); }
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  return h("div", null, h("div", { class: "ask" }, inp, h("button", { class: "btn primary", onclick: send }, icon("sparkles"), "Ask")), out);
}
function showReply(out, r) {
  const src = { local: "Instant answer from live data", ai: "AI answer — may be imperfect", none: "No match" }[r.source];
  clear(out, h("div", { class: r.ok ? "ask-reply" : "callout c-info" }, h("h3", null, r.title), r.text ? h("p", null, r.text.replace(/\*\*/g, "")) : null,
    r.lines?.length ? h("ul", null, r.lines.map((l) => h("li", null, l))) : null,
    r.confirm ? h("button", { class: "btn primary", onclick: async () => {
      const done = await act(() => api("/assistant/confirm", { method: "POST", body: { action: r.confirm.action, args: r.confirm.args } }));
      showReply(out, done); if (S.refresh) S.refresh();
    } }, r.confirm.prompt) : null,
    h("div", { class: "small muted" }, `${src} · ${Math.round(r.latency_ms)} ms`)));
}
function openAssistant() { modal({ title: "Ask Hive", body: assistantBox() }); }

window.addEventListener("hashchange", () => { if (S.me?.signed_in) route(); });
document.documentElement.dataset.theme = "dark"; // v5.0: dark mode only

/* Pulse Dispatch | ER:LC — web dashboard (no build step, no dependencies).
 * Security: every piece of server data is inserted with textContent (never innerHTML), so names,
 * reasons and messages typed by members can't inject markup. Permission checks happen on the server;
 * the UI only hides what the server would refuse anyway. */
"use strict";

// ============================================================================ tiny DOM toolkit
const $ = (s, el = document) => el.querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected" || k === "multiple" || k === "required") el[k] = !!v;
    else if (k === "dataset") Object.assign(el.dataset, v);
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
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const store = { get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
                set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage off: fine */ } } };

// ============================================================================ state & API
const S = { me: null, gid: null, meta: null, perms: new Set(), refresh: null, es: null, path: "", critical: 0 };
const can = (p) => S.perms.has(p);

async function api(path, { method = "GET", body } = {}) {
  const url = path.startsWith("/api/") ? path : `/api/g/${S.gid}${path}`;
  const headers = { Accept: "application/json" };
  if (method !== "GET") { headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = S.me?.csrf || ""; }
  let r;
  try { r = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" }); }
  catch { throw new Error("Can't reach the bot. Is it running?"); }
  if (r.status === 401) { S.me = null; boot(); throw new Error("Your session ended. Please sign in again."); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status}).`);
  return data;
}
async function act(fn, ok) {
  try { const out = await fn(); if (ok) toast(typeof ok === "function" ? ok(out) : ok); return out; }
  catch (e) { toast(e.message, true); throw e; }
}
function toast(text, bad = false) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, text);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
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
  return h("time", { datetime: iso, title: d.toLocaleString() }, txt);
}
const rel = (n, u) => (n >= 0 ? `${n} ${u} ago` : `in ${-n} ${u}`);
function dur(sec) {
  if (sec === null || sec === undefined) return "—";
  sec = Math.max(0, Math.round(sec));
  const d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return [d && `${d}d`, hh && `${hh}h`, (m && !d) && `${m}m`].filter(Boolean).join(" ") || `${sec}s`;
}
function who(id, name, avatar) {
  if (!id && !name) return h("span", { class: "muted" }, "—");
  return h("span", { class: "user", title: id ? `ID ${id}` : null }, avatar ? h("img", { src: avatar, alt: "" }) : null, name || `User ${id}`);
}
const PR = { critical: "Critical", high: "High", medium: "Medium", routine: "Routine", low: "Low" };
const prPill = (p) => p ? h("span", { class: `pill p-${p}` }, PR[p] || p) : h("span", { class: "pill s-off" }, "Assessing");
const pill = (text, kind = "info") => h("span", { class: `pill s-${kind}` }, text);
const yes = (v, a = "On", b = "Off") => v ? pill(a, "ok") : pill(b, "off");
const localToUtc = (v) => (v ? new Date(v).toISOString() : null);
const utcToLocal = (iso) => { if (!iso) return ""; const d = new Date(iso); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
const idOf = (txt) => { const m = String(txt || "").match(/(\d{15,22})/); return m ? m[1] : null; };

// ============================================================================ components
function modal({ title, body, actions = [], wide = false, onClose }) {
  const back = h("div", { class: "modal-back" });
  const close = () => { back.remove(); document.removeEventListener("keydown", esc); onClose && onClose(); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  const foot = h("footer");
  for (const a of actions) {
    const b = h("button", { class: "btn" + (a.primary ? " primary" : "") + (a.danger ? " danger solid" : ""), type: "button" }, a.label);
    b.addEventListener("click", async () => {
      if (!a.onClick) return close();
      b.disabled = true;
      try { const keep = await a.onClick(close); if (keep !== true && a.closes !== false) close(); }
      catch (e) { if (e && e.message) toast(e.message, true); }
      finally { b.disabled = false; }
    });
    foot.append(b);
  }
  const box = h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": title },
    h("header", null, h("h2", null, title), h("button", { class: "btn ghost sm", type: "button", "aria-label": "Close", onclick: close }, "✕")),
    h("div", { class: "body" }, body), actions.length ? foot : null);
  back.append(box);
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
  document.addEventListener("keydown", esc);
  document.body.append(back);
  setTimeout(() => (box.querySelector("input,select,textarea") || box.querySelector("button"))?.focus(), 30);
  return { close, box };
}
function confirmDialog(title, text, label = "Confirm", danger = true) {
  return new Promise((resolve) => {
    let done = false;
    modal({ title, body: h("p", null, text), onClose: () => { if (!done) resolve(false); },
            actions: [{ label: "Cancel" }, { label, danger, primary: !danger, onClick: () => { done = true; resolve(true); } }] });
  });
}
function emptyState(title, text, action) {
  return h("div", { class: "empty" }, h("b", null, title), text ? h("div", null, text) : null,
    action ? h("div", { style: "margin-top:10px" }, action) : null);
}
function table(cols, rows, { onRow, empty } = {}) {
  if (!rows || !rows.length) return empty || emptyState("Nothing here yet", null);
  const tb = h("tbody");
  for (const r of rows) {
    const tr = h("tr", { class: onRow ? "click" : null, tabindex: onRow ? 0 : null });
    for (const c of cols) {
      const v = c.render ? c.render(r) : r[c.key];
      tr.append(h("td", { "data-label": c.label, class: c.cls || null }, v === null || v === undefined || v === "" ? h("span", { class: "muted" }, "—") : v));
    }
    if (onRow) {
      tr.addEventListener("click", (e) => { if (!e.target.closest("button,a,select,input")) onRow(r); });
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") onRow(r); });
    }
    tb.append(tr);
  }
  return h("div", { class: "tbl-wrap" }, h("table", { class: "stack" }, h("thead", null, h("tr", null, cols.map((c) => h("th", null, c.label)))), tb));
}
function pager(data, go) {
  if (!data || data.pages <= 1) return data && data.total ? h("div", { class: "pager" }, h("span", null, `${fmtNum(data.total)} total`)) : null;
  return h("div", { class: "pager" }, h("span", null, `Page ${data.page} of ${data.pages} · ${fmtNum(data.total)} total`),
    h("span", { class: "row" },
      h("button", { class: "btn sm", disabled: data.page <= 1, onclick: () => go(data.page - 1) }, "Previous"),
      h("button", { class: "btn sm", disabled: data.page >= data.pages, onclick: () => go(data.page + 1) }, "Next")));
}
function tabs(items, current, onPick) {
  return h("div", { class: "tabs", role: "tablist" }, items.map(([k, label]) =>
    h("button", { class: k === current ? "on" : null, role: "tab", "aria-selected": k === current ? "true" : "false", onclick: () => onPick(k) }, label)));
}
function panel(title, body, ...headerExtra) {
  return h("section", { class: "panel" }, title ? h("header", null, h("h2", null, title), h("span", { class: "grow" }), headerExtra) : null, body);
}
function loading() { return h("div", { class: "loading" }, "Loading"); }
function errorBox(e) { return h("div", { class: "error-box" }, h("b", null, "Couldn't load this. "), e.message || String(e)); }

// ---------------------------------------------------------------------------- form fields
function channelSelect(value, { allowNone = true, label = "No channel" } = {}) {
  const sel = h("select", null, allowNone ? h("option", { value: "" }, label) : null);
  const groups = {};
  for (const c of S.meta.channels) (groups[c.category || "No category"] ||= []).push(c);
  for (const [cat, chs] of Object.entries(groups)) sel.append(h("optgroup", { label: cat }, chs.map((c) => h("option", { value: c.id, selected: c.id === String(value || "") }, `#${c.name}`))));
  return sel;
}
function roleSelect(value, { allowNone = true, multiple = false } = {}) {
  const vals = new Set((Array.isArray(value) ? value : [value]).filter(Boolean).map(String));
  const sel = h("select", { multiple, size: multiple ? 6 : null }, !multiple && allowNone ? h("option", { value: "" }, "No role") : null);
  for (const r of S.meta.roles) if (!r.managed) sel.append(h("option", { value: r.id, selected: vals.has(r.id) }, `@${r.name}${r.assignable ? "" : " (above the bot)"}`));
  return sel;
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
  const wrap = h("span", null, inp, list);
  wrap.getValue = () => idOf(inp.value);
  return wrap;
}
const sel = (opts, value) => h("select", null, Object.entries(opts).map(([k, v]) => h("option", { value: k, selected: String(value) === String(k) }, v)));
/** form([{key,label,type,help,options,...}], values) -> { el, get() } */
function form(fields, values = {}) {
  const el = h("div"), getters = {};
  for (const f of fields) {
    if (f.when && !f.when()) continue;
    const v = values[f.key] ?? f.default ?? "";
    let input, get;
    switch (f.type) {
      case "check": {
        input = h("input", { type: "checkbox", checked: !!v });
        el.append(h("label", { class: "check" }, input, h("span", null, f.label, f.help ? h("small", null, f.help) : null)));
        getters[f.key] = () => input.checked; continue;
      }
      case "textarea": input = h("textarea", { placeholder: f.placeholder, maxlength: f.max || null }, v); get = () => input.value; break;
      case "number": input = h("input", { type: "number", value: v, min: f.min ?? null, max: f.max ?? null }); get = () => (input.value === "" ? null : Number(input.value)); break;
      case "select": input = sel(f.options, v); get = () => input.value; break;
      case "channel": input = channelSelect(v); get = () => input.value || null; break;
      case "role": input = roleSelect(v); get = () => input.value || null; break;
      case "roles": input = roleSelect(v, { multiple: true }); get = () => [...input.selectedOptions].map((o) => o.value); break;
      case "member": input = memberPicker(v); get = () => input.getValue(); break;
      case "datetime": input = h("input", { type: "datetime-local", value: utcToLocal(v) }); get = () => localToUtc(input.value); break;
      case "date": input = h("input", { type: "date", value: v }); get = () => input.value; break;
      case "color": input = h("input", { type: "color", value: v || "#3CC8F0" }); get = () => input.value; break;
      case "password": input = h("input", { type: "password", autocomplete: "off", placeholder: f.placeholder }); get = () => input.value; break;
      case "lines": input = h("textarea", { placeholder: f.placeholder }, (v || []).join("\n")); get = () => input.value.split("\n").map((s) => s.trim()).filter(Boolean); break;
      default: input = h("input", { type: "text", value: v, placeholder: f.placeholder, maxlength: f.max || null }); get = () => input.value;
    }
    const help = f.help || (f.type === "roles" ? "Hold Ctrl (or ⌘ on Mac) to select several." : null);
    el.append(h("label", { class: "field" }, h("span", null, f.label), input, help ? h("small", null, help) : null));
    getters[f.key] = get;
  }
  return { el, get: () => Object.fromEntries(Object.entries(getters).map(([k, g]) => [k, g()])) };
}
function settingsPanel(title, fields, values, save, { note } = {}) {
  const f = form(fields, values);
  const btn = h("button", { class: "btn primary", onclick: async () => {
    btn.disabled = true;
    try { await act(() => save(f.get()), "Saved."); } finally { btn.disabled = false; }
  } }, "Save changes");
  return panel(title, h("div", null, note ? h("div", { class: "callout" }, note) : null, f.el, btn));
}

// ---------------------------------------------------------------------------- charts (SVG, no library)
const COLORS = ["var(--blue)", "var(--amber)", "var(--green)", "var(--red)", "#A78BFA"];
function svg(tag, attrs, ...kids) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  for (const k of kids.flat()) if (k) el.append(k instanceof Node ? k : document.createTextNode(k));
  return el;
}
function chart(days, series, { type } = {}) {
  const W = 640, H = 190, L = 34, B = 22, T = 8, R = 22;
  const max = Math.max(1, ...series.flatMap((s) => s.values));
  const nice = max <= 5 ? 5 : Math.ceil(max / 4) * 4;
  const x = (i) => L + (days.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (days.length - 1));
  const y = (v) => T + (H - T - B) * (1 - v / nice);
  const root = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
                            "aria-label": series.map((s) => `${s.label}: ${s.values.reduce((a, b) => a + b, 0)}`).join(", ") });
  for (let g = 0; g <= 4; g++) {
    const v = (nice / 4) * g;
    root.append(svg("line", { class: "grid", x1: L, x2: W - R, y1: y(v), y2: y(v) }), svg("text", { class: "axis", x: L - 6, y: y(v) + 4, "text-anchor": "end" }, String(Math.round(v * 10) / 10)));
  }
  const step = Math.max(1, Math.ceil(days.length / 8));
  days.forEach((d, i) => { if (i % step === 0 || i === days.length - 1) root.append(svg("text", { class: "axis", x: x(i), y: H - 6, "text-anchor": "middle" }, d.slice(5))); });
  if ((type || (series.length === 1 ? "bar" : "line")) === "bar") {
    const bw = Math.max(2, Math.min(28, ((W - L - R) / days.length) * 0.6));
    series[0].values.forEach((v, i) => root.append(svg("rect", { class: "bar", x: x(i) - bw / 2, y: y(v), width: bw, height: Math.max(0, H - B - y(v)), rx: 2 },
      svg("title", null, `${days[i]}: ${v} ${series[0].label.toLowerCase()}`))));
  } else {
    series.forEach((s, si) => {
      root.append(svg("polyline", { class: "line", stroke: COLORS[si % COLORS.length], points: s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ") }));
      s.values.forEach((v, i) => root.append(svg("circle", { cx: x(i), cy: y(v), r: 3, fill: COLORS[si % COLORS.length] }, svg("title", null, `${days[i]} · ${s.label}: ${v}`))));
    });
  }
  const legend = series.length > 1 ? h("div", { class: "legend" }, series.map((s, i) => h("span", null, h("i", { style: `background:${COLORS[i % COLORS.length]}` }), `${s.label} (${fmtNum(s.values.reduce((a, b) => a + b, 0))})`))) : null;
  return h("div", null, root, legend);
}

// ============================================================================ router & shell
const PAGES = {};
function page(path, title, fn) { PAGES[path] = { title, fn }; }
function go(path) { location.hash = `#/g/${S.gid}/${path}`; }
function link(path, text) { return h("a", { href: `#/g/${S.gid}/${path}` }, text); }

async function boot() {
  const app = $("#app");
  if (!S.me) {
    try { S.me = await fetch("/api/me", { credentials: "same-origin" }).then((r) => r.json()); }
    catch { return clear(app, h("main", { class: "gate" }, h("div", { class: "gate-box" }, errorBox(new Error("Can't reach the bot. Is it running?"))))); }
  }
  if (!S.me.signed_in) return renderLogin(app);
  route();
}
function renderLogin(app) {
  clear(app, h("main", { class: "gate" }, h("div", { class: "gate-box" },
    h("img", { class: "login-banner", src: "/static/brand/banner.png", alt: "Pulse Dispatch | ER:LC" }),
    h("h1", null, "Sign in to your command center"),
    h("p", null, "Use your Discord account. You'll see the servers and sections your roles allow."),
    S.me.login_ready ? h("a", { class: "btn primary", href: "/auth/login" }, "Sign in with Discord")
      : h("div", { class: "callout", style: "text-align:left" }, "Sign-in isn't set up yet. The bot owner needs to add DISCORD_CLIENT_SECRET to the .env file and restart the bot (see docs/DASHBOARD.md)."))));
}
function renderPicker(app) {
  disconnect();
  $("#lightbar").classList.remove("alert");
  const open = S.me.guilds.filter((g) => g.can_open), closed = S.me.guilds.filter((g) => !g.can_open);
  clear(app, h("main", { class: "gate" }, h("div", { class: "gate-box" },
    h("img", { class: "login-banner", src: "/static/brand/banner.png", alt: "Pulse Dispatch | ER:LC" }),
    h("h1", null, `Welcome, ${S.me.user.name}`),
    h("p", null, open.length ? "Choose a server to manage." : "None of your servers have given your roles dashboard access yet."),
    h("div", { class: "guilds" },
      open.map((g) => h("a", { href: `#/g/${g.id}/dashboard` }, g.icon ? h("img", { src: g.icon, alt: "" }) : h("span", { class: "gi" }, g.name.slice(0, 1)), h("b", null, g.name))),
      closed.map((g) => h("div", { title: "Your roles don't include dashboard access here" }, g.icon ? h("img", { src: g.icon, alt: "" }) : h("span", { class: "gi" }, g.name.slice(0, 1)), h("span", null, g.name, h("br"), h("small", { class: "muted" }, "No access"))))),
    h("p", { class: "small", style: "margin-top:24px" }, h("button", { class: "btn ghost sm", onclick: logout }, "Sign out")))));
}
async function route() {
  const m = location.hash.match(/^#\/g\/(\d+)\/?(.*)$/);
  if (!m) return renderPicker($("#app"));
  const [, gid, rest] = m;
  if (gid !== S.gid || !S.meta) {
    S.gid = gid; S.meta = null;
    clear($("#app"), loading());
    try { S.meta = await api("/meta"); }
    catch (e) { S.gid = null; return clear($("#app"), h("main", { class: "gate" }, h("div", { class: "gate-box" }, errorBox(e), h("p", null, h("a", { href: "#/" }, "Back to your servers"))))); }
    S.perms = new Set(S.meta.permissions);
    renderShell();
    connect();
  }
  renderPage(rest || "dashboard");
}
function renderShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Sections" });
  const closedGroups = new Set(store.get("pd-nav-closed", []));
  for (const g of S.meta.nav) {
    const grp = h("div", { class: "nav-group" + (closedGroups.has(g.group) ? " closed" : "") });
    grp.append(h("button", { type: "button", "aria-expanded": closedGroups.has(g.group) ? "false" : "true", onclick: (e) => {
      grp.classList.toggle("closed");
      e.currentTarget.setAttribute("aria-expanded", grp.classList.contains("closed") ? "false" : "true");
      const c = [...document.querySelectorAll(".nav-group.closed > button")].map((b) => b.textContent);
      store.set("pd-nav-closed", c);
    } }, g.group), h("ul", null, g.items.map((i) => h("li", null, h("a", { href: `#/g/${S.gid}/${i.path}`, dataset: { path: i.path } }, i.label)))));
    nav.append(grp);
  }
  const search = h("input", { type: "search", placeholder: "Find a section…", "aria-label": "Find a section", class: "nav-search-in" });
  search.addEventListener("input", () => {
    const q = search.value.toLowerCase();
    for (const a of nav.querySelectorAll("li")) a.classList.toggle("hidden", !!q && !a.textContent.toLowerCase().includes(q));
    for (const g of nav.querySelectorAll(".nav-group")) { if (q) g.classList.remove("closed"); g.classList.toggle("hidden", !!q && !g.querySelector("li:not(.hidden)")); }
  });
  const guildSel = h("select", { "aria-label": "Server", onchange: (e) => { location.hash = `#/g/${e.target.value}/dashboard`; } },
    S.me.guilds.filter((g) => g.can_open).map((g) => h("option", { value: g.id, selected: g.id === S.gid }, g.name)));
  const bell = h("button", { class: "btn ghost", "aria-label": "Notifications", onclick: () => toggleNotifications(bell) }, "🔔", h("span", { class: "count hidden", id: "bell-count" }));
  const themeLabel = () => (document.documentElement.dataset.theme === "dark" ? ["Light mode", "☀"] : ["Dark mode", "☾"]);
  const themeInner = () => { const [l, i] = themeLabel(); return [h("span", { class: "lbl" }, l), h("span", { class: "ico", "aria-hidden": "true" }, i)]; };
  const theme = h("button", { class: "btn ghost theme-btn", title: "Switch theme", "aria-label": "Switch light or dark theme", onclick: () => {
    const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = t; store.set("pd-theme", t); clear(theme, themeInner());
  } }, themeInner());
  clear($("#app"), h("div", { class: "shell" },
    h("aside", { class: "side" },
      h("div", { class: "side-head" }, h("img", { class: "brand-mark", src: "/static/brand/mark-192.png", alt: "" }),
        h("div", { class: "brand-name" }, "PULSE DISPATCH", h("span", null, "ER:LC command center"))),
      h("div", { class: "nav-search" }, search), nav,
      h("div", { class: "side-foot" }, h("img", { class: "avatar", src: S.me.user.avatar, alt: "" }),
        h("div", { style: "min-width:0;flex:1" }, h("div", { style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, S.me.user.name), h("small", { class: "muted" }, `v${S.me.version}`)),
        h("button", { class: "btn ghost sm", onclick: logout }, "Sign out"))),
    h("div", { class: "main" },
      h("header", { class: "top" },
        h("button", { class: "btn ghost burger", "aria-label": "Menu", onclick: () => document.body.classList.toggle("nav-open") }, "☰"),
        h("div", { class: "guild-pick" }, S.meta.guild.icon ? h("img", { src: S.meta.guild.icon, alt: "" }) : null, guildSel),
        h("span", { class: "grow" }),
        can("assistant.use") ? h("button", { class: "btn ask-btn", onclick: openAssistant }, "Ask Pulse") : null,
        bell, theme),
      h("main", { class: "content", id: "content", tabindex: "-1" }))));
  document.addEventListener("click", (e) => { if (document.body.classList.contains("nav-open") && e.target.closest(".nav a")) document.body.classList.remove("nav-open"); });
  refreshBell();
}
async function renderPage(path) {
  const parts = path.split("/").filter(Boolean);
  let key = null, args = [];
  for (let n = parts.length; n > 0; n--) { const k = parts.slice(0, n).join("/"); if (PAGES[k]) { key = k; args = parts.slice(n); break; } }
  const el = $("#content");
  if (!el) return;
  S.refresh = null; S.path = path;
  for (const a of document.querySelectorAll(".nav a")) a.classList.toggle("on", a.dataset.path === key);
  if (!key) return clear(el, emptyState("Page not found", "Pick a section from the menu."));
  const p = PAGES[key];
  document.title = `${p.title} · ${S.meta.guild.name} — Pulse Dispatch`;
  remember(key, p.title);
  clear(el, loading());
  try { await p.fn(el, args); }
  catch (e) { clear(el, errorBox(e)); }
  el.focus({ preventScroll: true });
}
function head(title, desc, ...actions) {
  return h("div", { class: "page-head" }, h("div", { class: "grow" }, h("h1", null, title), desc ? h("p", null, desc) : null), actions);
}
function remember(path, title) {
  if (path === "dashboard") return;
  const key = `pd-recent-${S.gid}`;
  const list = store.get(key, []).filter((r) => r.path !== path);
  list.unshift({ path, title });
  store.set(key, list.slice(0, 5));
}
async function logout() {
  try { await fetch("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": S.me?.csrf || "" }, credentials: "same-origin" }); } catch { /* signing out anyway */ }
  S.me = null; S.gid = null; S.meta = null; disconnect(); location.hash = "#/"; boot();
}

// ---------------------------------------------------------------------------- live updates
function connect() {
  disconnect();
  if (!window.EventSource) return;
  const refresh = debounce(() => { if (S.refresh) S.refresh(); }, 900);
  const lights = debounce(updateLightbar, 1200);
  S.es = new EventSource(`/api/g/${S.gid}/events`);
  S.es.onmessage = (e) => {
    const name = e.data || "";
    if (name.startsWith("notification")) refreshBell();
    if (name.startsWith("incident") || name.startsWith("assignment")) lights();
    refresh();
  };
  updateLightbar();
}
function disconnect() { if (S.es) { S.es.close(); S.es = null; } }
async function updateLightbar() {
  if (!can("incident.view")) return;
  try {
    const d = await api("/dispatch");
    S.critical = d.incidents.filter((i) => i.effective_priority === "critical").length;
    $("#lightbar").classList.toggle("alert", S.critical > 0);
    $("#lightbar").title = S.critical ? `${S.critical} critical incident(s) open` : "";
  } catch { /* the lightbar is decoration on top of real data */ }
}

// ---------------------------------------------------------------------------- notifications & assistant
async function refreshBell() {
  try {
    const d = await api("/notifications?unread=1");
    const c = $("#bell-count");
    if (c) { c.textContent = d.unread > 9 ? "9+" : String(d.unread); c.classList.toggle("hidden", !d.unread); }
  } catch { /* bell is optional */ }
}
async function toggleNotifications(anchor) {
  const old = $(".drop");
  if (old) return old.remove();
  const box = h("div", { class: "drop", role: "dialog", "aria-label": "Notifications" }, loading());
  document.body.append(box);
  const close = (e) => { if (!box.contains(e.target) && e.target !== anchor && !anchor.contains(e.target)) { box.remove(); document.removeEventListener("mousedown", close); } };
  document.addEventListener("mousedown", close);
  try {
    const d = await api("/notifications");
    clear(box, h("header", null, h("b", null, "Notifications"), d.unread ? h("button", { class: "btn sm", onclick: async () => {
      await act(() => api("/notifications/read", { method: "POST", body: { ids: "all" } })); box.remove(); refreshBell(); } }, "Mark all read") : null),
      d.items.length ? d.items.map((n) => h("a", { class: "note" + (n.read ? "" : " unread"), href: n.link ? `#/g/${S.gid}/${n.link.replace(/^#\//, "")}` : "#",
        onclick: async () => { box.remove(); if (!n.read) { await api("/notifications/read", { method: "POST", body: { ids: [n.id] } }).catch(() => {}); refreshBell(); } } },
        h("b", null, n.title), n.body ? h("small", null, n.body) : null, h("small", null, when(n.created_at))))
        : emptyState("You're all caught up", "New applications, failures and critical incidents show up here."));
  } catch (e) { clear(box, errorBox(e)); }
}
function assistantBox() {
  const inp = h("input", { type: "text", placeholder: "Ask: any active calls? who's on shift? start my shift", maxlength: 500, "aria-label": "Ask Pulse" });
  const out = h("div", { class: "ask-out", "aria-live": "polite" });
  const send = async () => {
    const q = inp.value.trim(); if (!q) return;
    clear(out, loading());
    try {
      const r = await api("/assistant", { method: "POST", body: { question: q } });
      showReply(out, r);
    } catch (e) { clear(out, errorBox(e)); }
  };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  return h("div", null, h("div", { class: "ask" }, inp, h("button", { class: "btn primary", onclick: send }, "Ask")), out);
}
function showReply(out, r) {
  const src = { local: "Instant answer from live data", ai: "AI answer — may be imperfect", none: "No match" }[r.source];
  clear(out, h("div", { class: r.ok ? "" : "callout" }, h("h3", null, r.title), r.text ? h("p", { style: "margin:4px 0" }, r.text.replace(/\*\*/g, "")) : null,
    r.lines?.length ? h("ul", null, r.lines.map((l) => h("li", null, l))) : null,
    r.confirm ? h("button", { class: "btn primary", style: "margin-top:8px", onclick: async () => {
      const done = await act(() => api("/assistant/confirm", { method: "POST", body: { action: r.confirm.action, args: r.confirm.args } }));
      showReply(out, done); if (S.refresh) S.refresh();
    } }, r.confirm.prompt) : null,
    h("div", { class: "small muted", style: "margin-top:6px" }, `${src} · ${Math.round(r.latency_ms)} ms`)));
}
function openAssistant() { modal({ title: "Ask Pulse", body: assistantBox() }); }

window.addEventListener("hashchange", () => { if (S.me?.signed_in) route(); });
document.documentElement.dataset.theme = store.get("pd-theme", window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");

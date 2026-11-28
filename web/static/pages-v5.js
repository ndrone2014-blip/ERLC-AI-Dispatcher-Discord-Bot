/* Hive Dispatch v5.0 pages: overview, activity, ER:LC live, players, moderation center, BOLOs, staff.
 * Every page builds its layout once and then updates its data regions in place (S.refresh), so live updates never
 * reset filters, scroll position, tabs or forms. */
"use strict";

const nameOf = (o) => (o && (o.name || o.display_name || o.username)) || null;
const GAME = {
  warning: { label: "Warning", icon: "warn", tone: "amber" }, kick: { label: "Kick", icon: "boot", tone: "orange" },
  ban: { label: "Ban", icon: "ban", tone: "red" }, tempban: { label: "Temp ban", icon: "clock", tone: "red" },
  unban: { label: "Unban", icon: "unlock", tone: "green" }, note: { label: "Note", icon: "note", tone: "muted" },
};
const DISC = { warn: "Warning", timeout: "Timeout", kick: "Kick", ban: "Ban", unban: "Unban", note: "Note" };
const actionTag = (a, map = GAME) => { const m = map[a] || { label: DISC[a] || a, icon: "shield", tone: "muted" }; return h("span", { class: `tag t-${m.tone || "muted"}` }, icon(m.icon || "shield"), m.label || DISC[a] || a); };
const statusPill = (s) => pill(s, { active: "ok", revoked: "off", expired: "off", failed: "bad", cleared: "off" }[s] || "plain");
const gameState = (r) => ({ sent: h("span", { class: "tag t-green" }, icon("check"), "In-game"), failed: tip(h("span", { class: "tag t-red" }, icon("alert"), "Failed"), r.in_game_detail || "Command failed"),
                            not_requested: h("span", { class: "tag t-muted" }, "Record only") }[r.in_game]);
const BOLO_PR = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };
const boloPill = (p) => h("span", { class: `pill bp-${p}` }, BOLO_PR[p] || p);

// ============================================================================ OVERVIEW
const KPIS = {
  members: "Total members", verified: "Verified members", staff: "Staff online", players: "Players in-game", incidents: "Active incidents",
  applications: "Open applications", sessions: "Active sessions", automations: "Automations on", bolos: "Active BOLOs", unread: "Unread notifications",
};
const PANELS = { trend: "Activity trend", myshift: "My shift", live: "Live server", feed: "Recent activity", moderation: "Recent moderation",
                 health: "System health", notifications: "Notifications", quick: "Quick actions", recent: "Recently visited" };
const PANEL_SPAN = { trend: 8, myshift: 4, live: 4, feed: 8, moderation: 6, health: 6, notifications: 4, quick: 4, recent: 4 };
const DEFAULT_PANELS = ["trend", "myshift", "feed", "live", "moderation", "health", "notifications", "quick", "recent"];

page("dashboard", "Dashboard", async (el) => {
  let d = await api("/overview");
  const allowed = navPaths();
  const prefs = S.prefs || {};
  const order = () => { const o = (S.prefs?.widgets || DEFAULT_PANELS).filter((w) => PANELS[w]); for (const k of DEFAULT_PANELS) if (!o.includes(k) && !(S.prefs?.widgets || []).length) o.push(k); return o; };
  const hidden = () => new Set(S.prefs?.hidden || []);
  const goIf = (p) => (allowed.has(p) ? () => go(p) : null);

  const kpi = {
    members: () => stat({ label: "Total members", value: fmtNum(d.members.total), sub: d.members.online !== null ? `${fmtNum(d.members.online)} online now` : `${fmtNum(d.members.tracked)} tracked`, ico: "users", onClick: goIf("members") }),
    verified: () => stat({ label: "Verified", value: fmtNum(d.members.verified), sub: d.verification.pending ? `${d.verification.pending} pending` : `${d.verification.today} today`, ico: "checkCircle", onClick: goIf("verification") }),
    staff: () => stat({ label: "Staff online", value: fmtNum(d.staff.on_shift), sub: `of ${fmtNum(d.staff.total)} staff${d.staff.on_leave ? ` · ${d.staff.on_leave} on leave` : ""}`, ico: "badge", tone: d.staff.on_shift ? "t-good" : "", onClick: goIf("staff/overview") || goIf("shifts") }),
    players: () => { const e = d.health.erlc; return stat({ label: "Players in-game", value: e.players === null || e.players === undefined ? "—" : fmtNum(e.players), sub: e.connected ? `${e.staff_online ?? 0} in-game staff` : (e.status === "not_configured" ? "ER:LC not set up" : "Not connected"), ico: "gamepad", onClick: goIf("erlc") }); },
    incidents: () => stat({ label: "Active incidents", value: fmtNum(d.dispatch.active_incidents), sub: d.dispatch.critical ? `${d.dispatch.critical} critical` : `${d.dispatch.units.available} units available`, ico: "siren", tone: d.dispatch.critical ? "t-bad" : "", onClick: goIf("dispatch") }),
    applications: () => stat({ label: "Open applications", value: fmtNum(d.applications.open || 0), sub: "Waiting for review", ico: "file", tone: d.applications.open ? "t-warn" : "", onClick: goIf("applications") }),
    sessions: () => stat({ label: "Active sessions", value: fmtNum(d.sessions.active), sub: `${d.sessions.upcoming} scheduled`, ico: "play", onClick: goIf("sessions") }),
    automations: () => stat({ label: "Automations on", value: fmtNum(d.automations.active), sub: "Running in the background", ico: "zap", onClick: goIf("automation") }),
    bolos: () => d.bolos ? stat({ label: "Active BOLOs", value: fmtNum(d.bolos.active), sub: d.bolos.urgent ? `${d.bolos.urgent} high priority` : "Informational alerts", ico: "eye", tone: d.bolos.urgent ? "t-warn" : "", onClick: goIf("bolos") }) : null,
    unread: () => stat({ label: "Unread notifications", value: fmtNum(d.unread), sub: d.unread ? "Open the notification center" : "All caught up", ico: "bell", tone: d.unread ? "t-accent" : "", onClick: openNotifications }),
  };
  const P = {};
  P.trend = () => card({ title: "Activity trend", sub: "Last 14 days", icon: "chart", body: chart(d.trend.labels, [
    { label: "Activity", values: d.trend.series.activity }, { label: "Player joins", values: d.trend.series.joins }, { label: "Moderation", values: d.trend.series.moderation }], { type: "line", height: 210 }) });
  P.myshift = () => {
    if (!d.me) return null;
    const cur = d.me.shift;
    return card({ title: "My shift", icon: "clock", cls: cur ? "glow-good" : "", body: cur
      ? h("div", { class: "myshift" }, h("div", { class: "myshift-time" }, ticker(cur.started_at)), h("div", { class: "muted" }, `${cur.shift_type} shift · started `, when(cur.started_at)),
        h("button", { class: "btn danger block", onclick: async () => { await act(() => api("/shifts/end", { method: "POST", body: {} }), "Shift ended."); S.refresh(); } }, icon("stop"), "End shift"))
      : h("div", { class: "myshift" }, h("div", { class: "myshift-time off" }, "Off duty"), h("p", { class: "muted" }, "Start a shift to show as online staff."),
        h("button", { class: "btn primary block", onclick: () => go("shifts") }, icon("play"), "Start a shift")) });
  };
  P.live = () => {
    const e = d.health.erlc;
    if (!allowed.has("erlc")) return null;
    return card({ title: "Live server", icon: "server", actions: h("a", { class: "btn ghost sm", href: `#/g/${S.gid}/erlc` }, "Open"), body: e.connected
      ? h("div", { class: "live-mini" }, h("div", { class: "live-num" }, h("b", { class: "num" }, fmtNum(e.players ?? 0)), h("span", null, d.system.erlc.max_players ? `/ ${d.system.erlc.max_players} players` : "players")),
        h("div", { class: "row wrap" }, pill(`${e.staff_online ?? 0} staff in-game`, "info"), pill(d.system.erlc.server || "Connected", "ok")),
        h("small", { class: "muted" }, e.age_s !== null && e.age_s !== undefined ? `Updated ${e.age_s}s ago from the ER:LC API` : "Waiting for the first update"))
      : emptyState(e.status === "not_configured" ? "ER:LC isn't connected" : "Server unreachable", e.status === "not_configured" ? "Add your server key under Settings → General." : (e.detail || d.system.erlc.last_error || "The bot will keep retrying."), null, "server") });
  };
  P.feed = () => card({ title: "Recent activity", icon: "activity", actions: h("a", { class: "btn ghost sm", href: `#/g/${S.gid}/activity` }, "View all"), flush: true,
    body: d.recent.feed.length ? feedList(d.recent.feed.slice(0, 9)) : emptyState("Quiet so far", "Joins, moderation, shifts and BOLOs appear here as they happen.", null, "activity") });
  P.moderation = () => {
    if (d.recent.in_game === null && d.recent.moderation === null) return null;
    const rows = [...(d.recent.in_game || []).map((r) => ({ kind: "game", at: r.created_at, action: r.action, who: r.roblox_username, num: r.number, link: `moderation/records/${r.number}` })),
                  ...(d.recent.moderation || []).map((r) => ({ kind: "discord", at: r.created_at, action: r.action, who: r.target_name || r.target_id, num: r.number, link: `moderation/discord/${r.number}` }))]
      .sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, 7);
    return card({ title: "Recent moderation", icon: "shield", actions: allowed.has("moderation") ? h("a", { class: "btn ghost sm", href: `#/g/${S.gid}/moderation` }, "Moderation center") : null, flush: true,
      body: table([{ label: "Action", render: (r) => actionTag(r.action, r.kind === "game" ? GAME : {}) }, { label: "Target", render: (r) => h("span", null, r.who, h("small", { class: "muted" }, r.kind === "game" ? " · in-game" : " · Discord")) },
        { label: "#", render: (r) => h("span", { class: "num muted" }, `#${r.num}`) }, { label: "When", render: (r) => when(r.at) }], rows, { onRow: (r) => go(r.link), dense: true,
        empty: emptyState("No moderation yet", "Warnings, kicks and bans from Discord and in-game show up here.", null, "shield") }) });
  };
  P.health = () => {
    const s = d.system, hh = d.health;
    const rowH = (ico, name, ok, text) => h("div", { class: "health-row" }, h("span", { class: "health-ico" }, icon(ico)), h("span", { class: "grow" }, name), dot(ok === null ? "off" : ok ? "ok" : "bad"), h("span", { class: "muted" }, text));
    return card({ title: "System health", icon: "signal", body: h("div", { class: "health" },
      rowH("bot", "Bot", true, `Up ${dur(s.uptime_s)}`),
      rowH("message", "Discord", hh.discord.ok, s.latency_ms === null ? (hh.discord.ok ? "Connected" : "Connecting…") : `${s.latency_ms} ms`),
      rowH("database", "Database", s.database.ok, s.database.ok ? `${s.database.ms} ms` : "Not responding"),
      rowH("server", "ER:LC API", s.erlc.configured ? s.erlc.connected : null, s.erlc.connected ? `${s.erlc.server || "Connected"}` : s.erlc.configured ? "Connection problem" : "Not set up"),
      rowH("signal", "Dashboard API", hh.api.ok, `${hh.api.live_clients} live connection(s)`),
      rowH("sparkles", "AI", s.ai.active ? true : null, s.ai.active ? s.ai.active.label : s.ai.configured ? `${s.ai.configured} ready` : "Local only")) });
  };
  P.notifications = () => card({ title: "Notifications", icon: "bell", actions: h("button", { class: "btn ghost sm", onclick: openNotifications }, "Open"), flush: true,
    body: d.notifications.length ? h("div", { class: "mini-list" }, d.notifications.map((n) => h("div", { class: "mini" }, h("span", { class: `note-ico sev-${n.severity}` }, icon(n.severity === "critical" || n.severity === "warning" ? "alert" : "bell")),
      h("span", { class: "grow" }, h("b", null, n.title), h("small", { class: "muted" }, when(n.created_at)))))) : emptyState("All caught up", null, null, "bell") });
  P.quick = () => {
    const acts = paletteActions().filter((a) => !["Notifications", "Sign out", "Ask Hive"].includes(a.title)).slice(0, 6);
    return card({ title: "Quick actions", icon: "zap", body: h("div", null, h("div", { class: "quick" }, acts.map((a) => h("button", { class: "quick-btn", onclick: a.run }, icon(a.icon), h("span", null, a.title)))),
      h("button", { class: "btn ghost block", onclick: openPalette }, icon("command"), "Search everything", h("kbd", null, "Ctrl K"))) });
  };
  P.recent = () => {
    const r = store.get(`pd-recent-${S.gid}`, []);
    return r.length ? card({ title: "Recently visited", icon: "clock", body: h("div", { class: "row wrap" }, r.map((x) => h("a", { class: "chip", href: `#/g/${S.gid}/${x.path}` }, icon(NAV_ICONS[x.path] || "dots"), x.title))) }) : null;
  };

  const kpiBox = h("div", { class: "stats" });
  const grid = h("div", { class: "grid" });
  const slots = {};
  const drawKpis = () => swap(kpiBox, Object.keys(KPIS).filter((k) => !hidden().has("kpi:" + k)).map((k) => kpi[k]()).filter(Boolean));
  const drawPanels = () => {
    clear(grid);
    for (const k of order()) {
      if (hidden().has(k)) continue;
      const node = P[k]();
      if (!node) continue;
      slots[k] = h("div", { class: `col-${PANEL_SPAN[k]}` }, node);
      grid.append(slots[k]);
    }
  };
  const hour = new Date().getHours();
  const greet = hour < 5 ? "Good night" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  clear(el, head(`${greet}, ${S.me.user.name.split(" ")[0]}`, `${S.meta.guild.name} at a glance. Everything updates live.`,
    h("button", { class: "btn", onclick: () => customizeDashboard(() => { drawKpis(); drawPanels(); }) }, icon("grid"), "Customize")), kpiBox, grid);
  drawKpis(); drawPanels();
  S.refresh = async () => {
    try { d = await api("/overview"); } catch { return; }
    drawKpis();
    for (const [k, slot] of Object.entries(slots)) {
      if (!slot.isConnected || k === "quick" || k === "recent") continue;
      const node = P[k](); if (node) swap(slot, node);
    }
  };
  void prefs;
});
function feedList(items) {
  const catIcon = { erlc_players: "gamepad", moderation: "shield", erlc_moderation: "gavel", shifts: "clock", staff: "badge", bolos: "eye", sessions: "play",
                    applications: "file", verification: "checkCircle", calls: "siren", incidents: "siren", closures: "file", assignments: "car", members: "users",
                    admin: "sliders", dashboard: "grid", security: "key", automation: "zap", erlc: "server", erlc_commands: "terminal" };
  return h("ul", { class: "feed" }, items.map((a) => h("li", { class: `feed-item c-${a.category}` }, h("span", { class: "feed-ico" }, icon(catIcon[a.category] || "activity")),
    h("div", { class: "feed-main" }, a.link ? h("a", { href: `#/g/${S.gid}/${a.link}` }, a.title) : h("b", null, a.title), a.body ? h("small", null, a.body) : null),
    h("span", { class: "feed-when" }, when(a.at)))));
}
function customizeDashboard(after) {
  let list = [...(S.prefs?.widgets || DEFAULT_PANELS).filter((w) => PANELS[w])];
  for (const k of Object.keys(PANELS)) if (!list.includes(k)) list.push(k);
  const hid = new Set(S.prefs?.hidden || []);
  const body = h("div");
  const draw = () => clear(body, h("p", { class: "muted" }, "Pick what you want to see and drag the order with the arrows. Only you see this layout."),
    h("h3", null, "Numbers"), h("div", { class: "chk-grid" }, Object.entries(KPIS).map(([k, label]) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: !hid.has("kpi:" + k),
      onchange: (e) => (e.target.checked ? hid.delete("kpi:" + k) : hid.add("kpi:" + k)) }), h("span", { class: "switch" }), h("span", null, label)))),
    h("h3", null, "Panels"), h("ul", { class: "sort-list" }, list.map((k, i) => h("li", null, h("label", { class: "check" }, h("input", { type: "checkbox", checked: !hid.has(k), onchange: (e) => (e.target.checked ? hid.delete(k) : hid.add(k)) }),
      h("span", { class: "switch" }), h("span", null, PANELS[k])), h("span", { class: "grow" }),
      h("button", { class: "icon-btn sm", "aria-label": `Move ${PANELS[k]} up`, disabled: i === 0, onclick: () => { [list[i - 1], list[i]] = [list[i], list[i - 1]]; draw(); } }, icon("chevronDown", "up")),
      h("button", { class: "icon-btn sm", "aria-label": `Move ${PANELS[k]} down`, disabled: i === list.length - 1, onclick: () => { [list[i + 1], list[i]] = [list[i], list[i + 1]]; draw(); } }, icon("chevronDown"))))));
  draw();
  modal({ title: "Customize dashboard", body, actions: [
    { label: "Reset", ghost: true, onClick: async () => { await savePrefs({ widgets: DEFAULT_PANELS, hidden: [] }); after(); toast("Layout reset."); } },
    { label: "Save layout", primary: true, onClick: async () => { await savePrefs({ widgets: list, hidden: [...hid] }); after(); toast("Layout saved."); } }] });
}

// ============================================================================ ACTIVITY FEED
page("activity", "Activity feed", async (el) => {
  const q = { group: "", q: "", page: 1 };
  const out = h("div");
  let groups = [];
  const load = async (soft) => {
    if (!soft) swap(out, skeleton("rows"));
    const d = await api(`/activity?${new URLSearchParams(q)}`);
    groups = d.groups;
    const byDay = {};
    for (const r of d.items) (byDay[new Date(r.at).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })] ||= []).push(r);
    swap(out, d.items.length ? [Object.entries(byDay).map(([day, rows]) => h("section", { class: "day" }, h("h3", { class: "day-head" }, day), card({ flush: true, body: feedList(rows) }))), pager(d, (p) => { q.page = p; load(); window.scrollTo(0, 0); })]
      : emptyState(q.q || q.group ? "Nothing matches" : "No activity yet", q.q || q.group ? "Try another filter or search." : "Everything staff and players do will be listed here.", null, "activity"));
    return d;
  };
  const first = await api(`/activity?per_page=1`);
  groups = first.groups;
  clear(el, head("Activity feed", "Player joins and leaves, moderation, shifts, BOLOs, sessions, applications, verification and admin actions — live."),
    h("div", { class: "toolbar" }, searchBox("Search activity", (v) => { q.q = v; q.page = 1; load(); }),
      chips([["", "Everything"], ...groups.map((g) => [g.key, g.label])], "", (k) => { q.group = k; q.page = 1; load(); })), out);
  await load();
  S.refresh = () => (q.page === 1 ? load(true) : null);
});

// ============================================================================ ER:LC LIVE SERVER
page("erlc", "Live server", async (el) => {
  let d = await api("/erlc/live");
  const st = { q: "", team: "", staffOnly: false, tab: "players" };
  const banner = h("div"), statsBox = h("div", { class: "stats" }), playersBox = h("div"), recentBox = h("div"), tabBox = h("div");
  const teamChipsBox = h("div");
  const conn = () => {
    const c = d.connection, live = d.live;
    if (!live || !live.updated_at) {
      return c.status === "not_configured" ? callout("ER:LC isn't connected yet. Add your private server API key under Settings → General (needs the ER:LC API server pack). Nothing on this page is shown until real data arrives.", "warn")
        : callout(`Waiting for live data from ER:LC${c.detail ? ` — ${c.detail}` : ""}. The bot retries automatically.`, c.status === "error" ? "bad" : "info");
    }
    const parts = [];
    if (live.offline) parts.push(callout("ER:LC reports your private server is offline (no players).", "info", "server"));
    if (c.status !== "connected") parts.push(callout(`Connection problem: ${c.detail || c.last_error || c.status}. Showing the last data received ${live.age_s}s ago.`, "warn"));
    if (live.extras_error) parts.push(callout(`ER:LC refused the extra live sections (${live.extras_error}). Players still update; logs and staff lists are retried every 15 minutes.`, "warn"));
    return parts;
  };
  const drawStats = () => {
    const l = d.live || {};
    swap(statsBox,
      stat({ label: "Players", value: l.updated_at ? `${fmtNum(l.current_players ?? l.players.length)}${l.max_players ? " / " + l.max_players : ""}` : "—", sub: l.server || "ER:LC server", ico: "gamepad" }),
      stat({ label: "Staff in-game", value: l.updated_at ? fmtNum(l.staff_online.length) : "—", sub: "From each player's permission", ico: "badge", tone: l.staff_online?.length ? "t-good" : "" }),
      stat({ label: "Joins today", value: fmtNum(d.stats.joins_today), sub: `${fmtNum(d.stats.unique_today)} unique players`, ico: "login" }),
      stat({ label: "Known players", value: fmtNum(d.stats.known_players), sub: "Seen in your server", ico: "users", onClick: () => go("erlc/players") }),
      stat({ label: "Last update", value: l.age_s === null || l.age_s === undefined ? "—" : `${l.age_s}s`, sub: d.connection.connected ? "Connected" : "Not connected", ico: "refresh", tone: d.connection.connected ? "" : "t-bad" }));
  };
  const playerActions = (p) => menuButton(null, [
    { label: "Open profile", icon: "user", onClick: () => go(`moderation/player/${p.roblox_id || p.name}`) },
    can("erlc.moderate") && { label: "Warn", icon: "warn", onClick: () => erlcActionModal({ player: p.name, action: "warning" }) },
    can("erlc.moderate") && { label: "Kick", icon: "boot", onClick: () => erlcActionModal({ player: p.name, action: "kick" }) },
    can("erlc.ban") && { label: "Ban", icon: "ban", danger: true, onClick: () => erlcActionModal({ player: p.name, action: "ban" }) },
    can("bolo.manage") && { label: "Create BOLO", icon: "eye", onClick: () => boloCreate({ player: p.name }) }]);
  const drawPlayers = () => {
    const l = d.live;
    if (!l || !l.updated_at) return swap(playersBox, emptyState("No live player data", "Players appear here as soon as the ER:LC API answers.", null, "gamepad"));
    const teams = Object.entries(l.teams).sort((a, b) => b[1] - a[1]);
    swap(teamChipsBox, chips([["", `All (${l.players.length})`], ...teams.map(([t, n]) => [t, `${t} (${n})`])], st.team, (k) => { st.team = k; drawPlayers(); }));
    const q = st.q.toLowerCase();
    const rows = l.players.filter((p) => (!st.team || (p.team || "Unknown") === st.team) && (!st.staffOnly || p.staff) && (!q || p.name.toLowerCase().includes(q) || (p.callsign || "").toLowerCase().includes(q)));
    swap(playersBox, table([
      { label: "Player", render: (p) => h("span", { class: "user" }, avatar(p.name, null, "xs"), h("span", { class: "user-name" }, p.name), p.staff ? h("span", { class: "tag t-accent" }, icon("badge"), p.permission.replace("Server ", "")) : null,
        p.bolo ? tip(h("a", { class: `tag t-${p.bolo.priority === "critical" || p.bolo.priority === "high" ? "red" : "amber"}`, href: `#/g/${S.gid}/bolos/${p.bolo.number}` }, icon("eye"), `BOLO #${p.bolo.number}`), "Informational — not a ban") : null) },
      { label: "Team", render: (p) => p.team ? h("span", { class: `team tm-${(p.team || "").toLowerCase().replace(/[^a-z]/g, "")}` }, p.team) : null },
      { label: "Callsign", render: (p) => p.callsign ? h("code", null, p.callsign) : null },
      { label: "Location", render: (p) => p.location ? h("span", { class: "muted" }, [p.location.postal && `Postal ${p.location.postal}`, p.location.street].filter(Boolean).join(" · ")) : null },
      { label: "", cls: "actions", render: (p) => playerActions(p) }], rows,
      { onRow: (p) => go(`moderation/player/${p.roblox_id || p.name}`), empty: emptyState(l.players.length ? "No players match" : "Nobody in-game right now", l.players.length ? "Try a different filter." : "The list fills in as players join.", null, "gamepad") }));
  };
  const drawRecent = () => swap(recentBox, d.recent.length ? h("ul", { class: "feed compact" }, d.recent.map((e) => h("li", { class: "feed-item" },
    h("span", { class: `feed-ico ${e.kind === "join" ? "join" : "leave"}` }, icon(e.kind === "join" ? "login" : "logout")),
    h("div", { class: "feed-main" }, h("a", { href: `#/g/${S.gid}/moderation/player/${e.roblox_id}` }, e.username), h("small", null, e.kind === "join" ? `joined${e.team ? " · " + e.team : ""}` : "left")),
    h("span", { class: "feed-when" }, when(e.at))))) : emptyState("No joins or leaves yet", "Detected by comparing the live player list every poll.", null, "login"));
  const LOGS = [["players", "Players"], ["join_logs", "Join log"], ["kill_logs", "Kill log"], ["command_logs", "Command log"], ["mod_calls", "Mod calls"], ["staff", "Server staff"], ["bans", "Ban list"]];
  const drawTab = async () => {
    if (st.tab === "players") return swap(tabBox, h("div", null, h("div", { class: "toolbar" }, searchBox("Search players or callsigns", (v) => { st.q = v; drawPlayers(); }, st.q),
      h("label", { class: "check inline" }, h("input", { type: "checkbox", checked: st.staffOnly, onchange: (e) => { st.staffOnly = e.target.checked; drawPlayers(); } }), h("span", { class: "switch" }), h("span", null, "Staff only"))),
      teamChipsBox, playersBox));
    swap(tabBox, skeleton("rows"));
    try {
      if (st.tab === "bans") {
        const b = await api("/erlc/bans");
        return swap(tabBox, !b.available ? callout(`The ER:LC ban list isn't available: ${b.error}`, "warn")
          : table([{ label: "Player", render: (r) => h("a", { href: `#/g/${S.gid}/moderation/player/${r.roblox_id}` }, r.username) }, { label: "Roblox ID", render: (r) => h("code", null, r.roblox_id) }],
            b.items, { empty: emptyState("No in-game bans", "Players banned in your private server are listed here.", null, "ban") }));
      }
      const r = await api(`/erlc/logs/${st.tab}`);
      if (!r.available) return swap(tabBox, callout("ER:LC didn't return this section, so there's nothing to show. It's requested on every poll and appears as soon as the API includes it.", "info"));
      const ts = (t) => (t ? when(new Date(t * 1000).toISOString()) : null);
      const plink = (name, id) => (id ? h("a", { href: `#/g/${S.gid}/moderation/player/${id}` }, name) : name);
      const cols = {
        join_logs: [{ label: "Player", render: (x) => plink(x.player, x.roblox_id) }, { label: "Event", render: (x) => x.join ? pill("Joined", "ok") : pill("Left", "off") }, { label: "When", render: (x) => ts(x.ts) }],
        kill_logs: [{ label: "Killer", render: (x) => plink(x.killer, x.killer_id) }, { label: "Killed", render: (x) => plink(x.killed, x.killed_id) }, { label: "When", render: (x) => ts(x.ts) }],
        command_logs: [{ label: "Player", render: (x) => plink(x.player, x.roblox_id) }, { label: "Command", render: (x) => h("code", null, x.command) }, { label: "When", render: (x) => ts(x.ts) }],
        mod_calls: [{ label: "Caller", render: (x) => plink(x.caller, x.caller_id) }, { label: "Answered by", render: (x) => x.moderator ? plink(x.moderator, x.moderator_id) : pill("Unanswered", "warn") }, { label: "When", render: (x) => ts(x.ts) }],
        staff: [{ label: "Player", render: (x) => plink(x.name || `Roblox ${x.roblox_id}`, x.roblox_id) }, { label: "Group", render: (x) => pill(x.group, "info") }, { label: "Roblox ID", render: (x) => h("code", null, x.roblox_id) }],
      }[st.tab];
      swap(tabBox, table(cols, r.items, { empty: emptyState("Nothing logged yet", "ER:LC keeps a short recent history.", null, "logs") }));
    } catch (e) { swap(tabBox, errorBox(e)); }
  };
  const console_ = () => {
    const inp = h("input", { type: "text", placeholder: ":h Server restart in 5 minutes", maxlength: 300, "aria-label": "In-game command" });
    const run = async () => {
      const cmd = inp.value.trim(); if (!cmd) return;
      if (/^:(kick|ban|unban|kill|jail)\b/i.test(cmd) && !(await confirmDialog("Run this command in-game?", `${cmd}\n\nTip: use the Moderation center so the action is recorded on the player's profile.`, "Run command"))) return;
      await act(() => api("/erlc/command", { method: "POST", body: { command: cmd } }), "Command sent to your server."); inp.value = "";
    };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
    const preset = (label, text) => h("button", { class: "chip", onclick: () => { inp.value = text; inp.focus(); } }, label);
    return card({ title: "Server console", icon: "terminal", sub: "Runs as Virtual Server Management", body: h("div", null, h("div", { class: "ask mono" }, inp, h("button", { class: "btn primary", onclick: run }, icon("chevronRight"), "Run")),
      h("div", { class: "chips" }, preset("Hint", ":h "), preset("Message", ":m "), preset("PM player", ":pm "), preset("Weather", ":weather clear"), preset("Time", ":time 12")),
      h("small", { class: "muted" }, "Every command is written to the audit log. ER:LC may refuse restricted commands.")) });
  };
  clear(el, head("Live server", d.live?.server ? `${d.live.server} — live from the official ER:LC API.` : "Live players, in-game staff and server logs from the official ER:LC API.",
    can("erlc.moderate") ? h("button", { class: "btn", onclick: () => erlcActionModal() }, icon("gavel"), "Moderate a player") : null,
    can("bolo.manage") ? h("button", { class: "btn", onclick: () => boloCreate() }, icon("eye"), "New BOLO") : null),
    banner, statsBox,
    h("div", { class: "grid" },
      h("div", { class: "col-8" }, card({ body: h("div", null, tabs(LOGS, st.tab, (k) => { st.tab = k; drawTab(); }), tabBox) })),
      h("div", { class: "col-4" }, d.can_command ? console_() : null, card({ title: "Joins & leaves", icon: "activity", sub: "Live", flush: true, body: recentBox }))));
  const all = () => { swap(banner, conn()); drawStats(); drawPlayers(); drawRecent(); };
  all(); drawTab();
  S.refresh = async () => { try { d = await api("/erlc/live"); } catch { return; } all(); };
});

page("erlc/players", "Players", async (el) => {
  const q = { q: "", filter: "", page: 1 };
  const out = h("div");
  const load = async (soft) => {
    if (!soft) swap(out, skeleton("rows"));
    const d = await api(`/erlc/players?${new URLSearchParams(q)}`);
    swap(out, table([
      { label: "Player", render: (p) => h("span", { class: "user" }, avatar(p.username, null, "xs"), h("span", { class: "user-name" }, p.username), p.online ? h("span", { class: "tag t-green" }, dot("ok"), "In-game") : null) },
      { label: "Roblox ID", render: (p) => h("code", null, p.roblox_id) }, { label: "Last team", key: "team" },
      { label: "Sessions", render: (p) => h("span", { class: "num" }, fmtNum(p.sessions)) },
      { label: "Records", render: (p) => p.records ? h("span", { class: "tag t-amber" }, icon("gavel"), p.records) : h("span", { class: "muted" }, "0") },
      { label: "BOLO", render: (p) => p.active_bolos ? h("span", { class: "tag t-red" }, icon("eye"), "Active") : null },
      { label: "Last seen", render: (p) => p.online ? pill("Now", "ok") : when(p.last_seen) }], d.items,
      { onRow: (p) => go(`moderation/player/${p.roblox_id}`), empty: emptyState(q.q ? "No players match" : "No players seen yet", "Everyone who joins your ER:LC server is remembered here automatically.", null, "gamepad") }),
      pager(d, (p) => { q.page = p; load(); }));
  };
  clear(el, head("Players", "Everyone seen in your ER:LC server, with their moderation history and BOLOs.",
    can("erlc.moderate") ? h("button", { class: "btn primary", onclick: () => erlcActionModal() }, icon("gavel"), "Moderate a player") : null),
    h("div", { class: "toolbar" }, searchBox("Search username or Roblox ID", (v) => { q.q = v; q.page = 1; load(); }),
      chips([["", "Everyone"], ["online", "In-game now"], ["offline", "Offline"]], "", (k) => { q.filter = k; q.page = 1; load(); })), out);
  await load();
  S.refresh = () => load(true);
});

// ============================================================================ MODERATION CENTER
function erlcActionModal(pre = {}) {
  const actions = Object.fromEntries(Object.entries(GAME).filter(([k]) => can(k === "ban" || k === "tempban" || k === "unban" ? "erlc.ban" : "erlc.moderate")).map(([k, v]) => [k, v.label]));
  if (!Object.keys(actions).length) return toast("You don't have permission to moderate in-game players.", true);
  let action = pre.action && actions[pre.action] ? pre.action : Object.keys(actions)[0];
  const player = playerPicker(pre.player || "");
  const reason = h("textarea", { rows: 3, maxlength: 500, placeholder: "What happened? This is saved on the player's record." });
  const evidence = h("input", { type: "text", maxlength: 500, placeholder: "Clip or screenshot links (optional)" });
  const duration = h("input", { type: "text", placeholder: "12h, 3d, 2w" });
  const exec = h("input", { type: "checkbox", checked: action !== "note" && action !== "warning" });
  const durWrap = h("label", { class: "field" }, h("span", null, "Duration"), duration, h("small", null, "The ban is lifted automatically when it ends."));
  const execWrap = h("label", { class: "check" }, exec, h("span", { class: "switch" }), h("span", null, "Carry it out in-game now", h("small", null, "")));
  const seg = h("div", { class: "seg" });
  const sync = () => {
    durWrap.classList.toggle("hidden", action !== "tempban");
    execWrap.classList.toggle("hidden", action === "note");
    execWrap.querySelector("small").textContent = { warning: "Sends the reason to the player as an in-game :pm.", kick: "Runs :kick — the player must be in-game.", ban: "Runs :ban in your private server.",
      tempban: "Runs :ban now and :unban when it ends.", unban: "Runs :unban in your private server." }[action] || "";
    for (const b of seg.children) b.classList.toggle("on", b.dataset.k === action);
  };
  for (const [k, label] of Object.entries(actions)) seg.append(h("button", { type: "button", dataset: { k }, onclick: () => { action = k; exec.checked = !["note", "warning"].includes(k); sync(); } }, icon(GAME[k].icon), label));
  sync();
  modal({ title: "In-game moderation", body: h("div", { class: "form" }, h("label", { class: "field" }, h("span", null, "Player"), player), h("div", { class: "field" }, h("span", null, "Action"), seg),
    durWrap, h("label", { class: "field" }, h("span", null, "Reason"), reason), h("label", { class: "field" }, h("span", null, "Evidence", h("em", null, " optional")), evidence), execWrap),
    actions: [{ label: "Cancel", ghost: true }, { label: "Save record", primary: true, icon: "check", onClick: async () => {
      const body = { action, player: player.getValue(), reason: reason.value, evidence: evidence.value, duration: duration.value, execute: exec.checked && action !== "note" };
      if (!body.player) throw new Error("Enter the player's Roblox username or ID.");
      if (["ban", "tempban", "kick"].includes(action)) {
        if (!(await confirmDialog(`${GAME[action].label} ${body.player}?`, `${body.execute ? "This runs in your ER:LC server and is" : "This is"} recorded on their profile.\n\nReason: ${body.reason || "—"}`, GAME[action].label))) return true;
        body.confirm = true;
      }
      const r = await act(() => api("/moderation/records", { method: "POST", body }), (x) => `Record #${x.number} saved${x.in_game === "sent" ? " and carried out in-game" : ""}.`);
      if (S.refresh) S.refresh();
      return r;
    } }] });
}
window.erlcActionModal = erlcActionModal;

async function recordModal(n) {
  const r = await act(() => api(`/moderation/records/${n}`));
  const reason = h("textarea", { rows: 3, maxlength: 500 }, r.reason || ""), evidence = h("input", { type: "text", value: r.evidence || "", maxlength: 500 });
  const actions = [{ label: "Close", ghost: true }];
  if (can("erlc.moderate")) actions.push({ label: "Save changes", primary: true, onClick: async () => { await act(() => api(`/moderation/records/${n}`, { method: "PATCH", body: { reason: reason.value, evidence: evidence.value } }), "Record updated."); S.refresh && S.refresh(); } });
  const banLike = ["ban", "tempban"].includes(r.action);
  if (r.status === "active" && r.action !== "unban" && can(banLike ? "erlc.ban" : "erlc.moderate")) actions.unshift({ label: banLike ? "Lift ban" : "Revoke", danger: true, onClick: async () => {
    let inGame = false;
    if (banLike) inGame = await confirmDialog("Unban in-game too?", `Also run :unban ${r.roblox_username} in your ER:LC server? Choose Cancel to only mark the record as revoked.`, "Unban in-game", false);
    else if (!(await confirmDialog(`Revoke record #${n}?`, "It stays on the profile but no longer counts as active.", "Revoke"))) return true;
    await act(() => api(`/moderation/records/${n}/revoke`, { method: "POST", body: { execute: inGame } }), "Record revoked."); S.refresh && S.refresh();
  } });
  modal({ title: `In-game record #${n}`, wide: true, actions, body: h("div", null,
    h("div", { class: "rec-head" }, actionTag(r.action), statusPill(r.status), gameState(r), h("span", { class: "grow" }), h("a", { class: "btn sm", href: `#/g/${S.gid}/moderation/player/${r.roblox_id || r.roblox_username}` }, icon("user"), "Player profile")),
    h("dl", { class: "facts" }, h("dt", null, "Player"), h("dd", null, h("b", null, r.roblox_username), r.roblox_id ? h("code", { class: "ml" }, r.roblox_id) : h("small", { class: "muted" }, " (ID unknown)")),
      h("dt", null, "Moderator"), h("dd", null, who(r.moderator_id, nameOf(r.moderator))), h("dt", null, "Created"), h("dd", null, when(r.created_at), h("small", { class: "muted" }, ` · via ${r.source}`)),
      r.duration_s ? [h("dt", null, "Duration"), h("dd", null, dur(r.duration_s), r.expires_at ? h("small", { class: "muted" }, " · ends ", when(r.expires_at)) : null)] : null,
      r.in_game_detail ? [h("dt", null, "ER:LC"), h("dd", null, h("code", null, r.in_game_detail))] : null,
      r.revoked_at ? [h("dt", null, "Revoked"), h("dd", null, when(r.revoked_at), r.revoker ? ` by ${nameOf(r.revoker)}` : "", r.revoke_reason ? h("small", { class: "muted" }, ` — ${r.revoke_reason}`) : null)] : null),
    h("label", { class: "field" }, h("span", null, "Reason"), reason), h("label", { class: "field" }, h("span", null, "Evidence"), evidence)) });
}

page("moderation", "Moderation center", async (el, args) => {
  if (args[0] && /^\d+$/.test(args[0])) { location.replace(`#/g/${S.gid}/moderation/discord/${args[0]}`); return; } // old notification links
  let days = 30;
  let d = await api(`/moderation/stats?days=${days}`);
  const statsBox = h("div", { class: "stats" }), chartBox = h("div"), recentBox = h("div"), modsBox = h("div");
  const look = h("div", { class: "lookup" });
  const draw = () => {
    const sum = (o) => Object.entries(o || {}).filter(([k]) => k !== "note").reduce((a, [, v]) => a + v, 0);
    swap(statsBox, d.can["erlc.view"] ? stat({ label: "In-game actions", value: fmtNum(sum(d.in_game)), sub: `${d.in_game.ban || 0} bans · ${d.in_game.kick || 0} kicks · ${d.in_game.warning || 0} warnings`, ico: "gavel", onClick: () => go("moderation/records") }) : null,
      d.can["mod.view"] ? stat({ label: "Discord actions", value: fmtNum(sum(d.discord)), sub: `${d.discord.ban || 0} bans · ${d.discord.timeout || 0} timeouts · ${d.discord.warn || 0} warnings`, ico: "message", onClick: () => go("moderation/discord") }) : null,
      stat({ label: "Active bans", value: fmtNum((d.active.in_game_bans || 0) + (d.active.discord_bans || 0)), sub: `${d.active.discord_timeouts ?? 0} active timeouts`, ico: "ban", tone: "t-bad", onClick: () => go("moderation/active") }),
      d.active.open_reports !== null ? stat({ label: "Open reports", value: fmtNum(d.active.open_reports), sub: "From members", ico: "alert", tone: d.active.open_reports ? "t-warn" : "", onClick: navPaths().has("moderation/reports") ? () => go("moderation/reports") : null }) : null);
    swap(chartBox, chart(d.labels, [d.can["erlc.view"] && { label: "In-game", values: d.series.in_game }, d.can["mod.view"] && { label: "Discord", values: d.series.discord }].filter(Boolean), { type: "line", height: 200 }));
    const rows = [...(d.recent.in_game || []).map((r) => ({ ...r, kind: "game", target: r.roblox_username })), ...(d.recent.discord || []).map((r) => ({ ...r, kind: "discord", target: r.target_name || r.target_id }))]
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1)).slice(0, 10);
    swap(recentBox, table([{ label: "Action", render: (r) => actionTag(r.action, r.kind === "game" ? GAME : {}) }, { label: "Target", render: (r) => h("span", null, h("b", null, r.target), h("small", { class: "muted" }, r.kind === "game" ? " · in-game" : " · Discord")) },
      { label: "Reason", render: (r) => h("span", { class: "clip" }, r.reason || "—") }, { label: "Status", render: (r) => statusPill(r.status) }, { label: "When", render: (r) => when(r.created_at) }], rows,
      { onRow: (r) => (r.kind === "game" ? recordModal(r.number) : caseModal(r.number)), empty: emptyState("No moderation yet", "Actions taken here, in Discord and in-game are tracked automatically.", null, "shield") }));
    swap(modsBox, d.moderators.length ? h("div", { class: "leader" }, d.moderators.map((m, i) => h("div", { class: "leader-row" }, h("span", { class: "rank num" }, i + 1), who(m.user_id, m.name, m.avatar), h("span", { class: "grow" }),
      h("b", { class: "num" }, fmtNum(m.total))))) : emptyState("No actions yet", null, null, "users"));
  };
  const lookIn = playerPicker("", "Look up a Roblox player…");
  lookIn.querySelector("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && lookIn.getValue()) go(`moderation/player/${encodeURIComponent(lookIn.getValue())}`); });
  clear(look, icon("search"), lookIn, h("button", { class: "btn", onclick: () => lookIn.getValue() && go(`moderation/player/${encodeURIComponent(lookIn.getValue())}`) }, "Open profile"));
  const rangeSel = chips([["7", "7 days"], ["30", "30 days"], ["90", "90 days"]], "30", async (k) => { days = Number(k); d = await api(`/moderation/stats?days=${days}`); draw(); });
  clear(el, head("Moderation center", "In-game and Discord moderation in one place. Every action is permission-checked and recorded.",
    d.can["erlc.moderate"] ? h("button", { class: "btn primary", onclick: () => erlcActionModal() }, icon("gavel"), "In-game action") : null,
    d.can["mod.warn"] ? h("button", { class: "btn", onclick: () => caseCreate() }, icon("message"), "Discord case") : null),
    d.can["erlc.view"] ? look : null, h("div", { class: "toolbar end" }, rangeSel), statsBox,
    h("div", { class: "grid" }, h("div", { class: "col-8" }, card({ title: "Actions over time", icon: "chart", body: chartBox })),
      h("div", { class: "col-4" }, card({ title: "Top moderators", icon: "users", sub: `${days} days`, actions: h("a", { class: "btn ghost sm", href: `#/g/${S.gid}/moderation/staff` }, "All staff"), body: modsBox })),
      h("div", { class: "col-12" }, card({ title: "Latest actions", icon: "list", flush: true, body: recentBox }))));
  draw();
  S.refresh = async () => { try { d = await api(`/moderation/stats?days=${days}`); } catch { return; } draw(); };
});

page("moderation/records", "In-game records", async (el, args) => {
  if (args[0] && /^\d+$/.test(args[0])) setTimeout(() => recordModal(args[0]), 0);
  const q = { q: "", action: "", status: "", page: 1 };
  const out = h("div");
  const load = async (soft) => {
    if (!soft) swap(out, skeleton("rows"));
    const d = await api(`/moderation/records?${new URLSearchParams(q)}`);
    swap(out, table([{ label: "#", render: (r) => h("b", { class: "num" }, `#${r.number}`) }, { label: "Action", render: (r) => actionTag(r.action) },
      { label: "Player", render: (r) => h("a", { href: `#/g/${S.gid}/moderation/player/${r.roblox_id || r.roblox_username}` }, r.roblox_username) },
      { label: "Reason", render: (r) => h("span", { class: "clip" }, r.reason) }, { label: "Moderator", render: (r) => who(r.moderator_id, nameOf(r.moderator)) },
      { label: "In-game", render: gameState }, { label: "Status", render: (r) => statusPill(r.status) }, { label: "When", render: (r) => when(r.created_at) }], d.items,
      { onRow: (r) => recordModal(r.number), empty: emptyState("No records match", "Warnings, kicks and bans against Roblox players are recorded here — from the dashboard or /ingame in Discord.", null, "gavel") }),
      pager(d, (p) => { q.page = p; load(); }));
  };
  const f = (key, opts) => { const x = sel(opts, ""); x.addEventListener("change", () => { q[key] = x.value; q.page = 1; load(); }); return x; };
  clear(el, head("In-game records", "Every in-game warning, kick, ban and note, with whether it was carried out in ER:LC.",
    can("erlc.moderate") ? h("button", { class: "btn primary", onclick: () => erlcActionModal() }, icon("plus"), "New action") : null),
    h("div", { class: "toolbar" }, searchBox("Player, reason, Roblox ID or #record", (v) => { q.q = v; q.page = 1; load(); }),
      f("action", { "": "Any action", ...Object.fromEntries(Object.entries(GAME).map(([k, v]) => [k, v.label])) }), f("status", { "": "Any status", active: "Active", revoked: "Revoked", expired: "Expired" })), out);
  await load();
  S.refresh = () => load(true);
});

page("moderation/player", "Player profile", async (el, args) => {
  const key = decodeURIComponent(args[0] || "");
  if (!key) return clear(el, emptyState("No player selected", "Search for a player from the Moderation center.", null, "user"));
  let p = await api(`/moderation/player/${encodeURIComponent(key)}`);
  let tab = "records";
  const headBox = h("div"), tabBox = h("div"), tabBar = h("div");
  const quick = () => h("div", { class: "row wrap" },
    p.can["erlc.moderate"] ? h("button", { class: "btn", onclick: () => erlcActionModal({ player: p.player.username, action: "warning" }) }, icon("warn"), "Warn") : null,
    p.can["erlc.moderate"] ? h("button", { class: "btn", onclick: () => erlcActionModal({ player: p.player.username, action: "kick" }) }, icon("boot"), "Kick") : null,
    p.can["erlc.ban"] ? (p.active_ban ? h("button", { class: "btn", onclick: () => erlcActionModal({ player: p.player.username, action: "unban" }) }, icon("unlock"), "Unban")
      : h("button", { class: "btn danger", onclick: () => erlcActionModal({ player: p.player.username, action: "ban" }) }, icon("ban"), "Ban")) : null,
    p.can["bolo.manage"] ? h("button", { class: "btn", onclick: () => boloCreate({ player: p.player.username }) }, icon("eye"), "BOLO") : null);
  const drawHead = () => {
    const pl = p.player, activeBolo = p.bolos.find((b) => b.status === "active");
    swap(headBox, h("section", { class: "profile" + (p.active_ban ? " banned" : "") },
      h("div", { class: "profile-av" }, avatar(pl.username, null, "xl"), pl.online ? h("span", { class: "online-dot", title: "In-game now" }) : null),
      h("div", { class: "profile-main" }, h("h1", null, pl.username),
        h("div", { class: "row wrap" }, pl.roblox_id ? h("code", null, `Roblox ${pl.roblox_id}`) : pill("Roblox ID unknown", "off"),
          pl.online ? h("span", { class: "tag t-green" }, dot("ok"), `In-game${pl.team ? " · " + pl.team : ""}`) : pl.last_seen ? h("span", { class: "muted small" }, "Last seen ", when(pl.last_seen)) : h("span", { class: "muted small" }, "Never seen in your server"),
          pl.permission && pl.permission !== "Normal" ? h("span", { class: "tag t-accent" }, icon("badge"), pl.permission) : null,
          p.active_ban ? h("span", { class: "tag t-red" }, icon("ban"), "Banned") : null,
          activeBolo ? tip(h("a", { class: "tag t-amber", href: `#/g/${S.gid}/bolos/${activeBolo.number}` }, icon("eye"), `BOLO #${activeBolo.number}`), "Informational — not a ban") : null),
        h("div", { class: "profile-facts" },
          h("span", null, h("b", { class: "num" }, fmtNum(pl.sessions)), " sessions"), h("span", null, h("b", { class: "num" }, fmtNum(p.records.filter((r) => r.action !== "note").length)), " records"),
          pl.first_seen ? h("span", null, "First seen ", when(pl.first_seen)) : null,
          h("span", null, "Discord: ", p.discord_id ? (p.can["members.view"] ? link(`members/${p.discord_id}`, nameOf(p.discord) || p.discord_id) : nameOf(p.discord) || p.discord_id) : h("span", { class: "muted" }, "not verified")))),
      h("div", { class: "profile-act" }, quick())));
    swap(tabBar, tabs([["records", "Records", p.records.length], ["notes", "Notes", (p.notes || []).length], ["sessions", "Sessions", p.events.length], ["bolos", "BOLOs", p.bolos.length],
      ...(p.discord_cases ? [["discord", "Discord cases", p.discord_cases.length]] : [])], tab, (k) => { tab = k; drawTab(); }));
  };
  const drawTab = () => {
    if (tab === "records") return swap(tabBox, table([{ label: "#", render: (r) => h("b", { class: "num" }, `#${r.number}`) }, { label: "Action", render: (r) => actionTag(r.action) },
      { label: "Reason", render: (r) => h("span", { class: "clip" }, r.reason) }, { label: "By", render: (r) => who(r.moderator_id, nameOf(r.moderator)) }, { label: "In-game", render: gameState },
      { label: "Status", render: (r) => statusPill(r.status) }, { label: "When", render: (r) => when(r.created_at) }], p.records, { onRow: (r) => recordModal(r.number), empty: emptyState("Clean record", "No in-game moderation on file for this player.", null, "checkCircle") }));
    if (tab === "notes") {
      const inp = h("textarea", { rows: 2, maxlength: 1000, placeholder: "Add a private staff note about this player" });
      return swap(tabBox, h("div", null, p.can["erlc.moderate"] ? h("div", { class: "note-add" }, inp, h("button", { class: "btn primary", onclick: async () => {
        if (!inp.value.trim()) return; await act(() => api(`/moderation/player/${encodeURIComponent(p.player.roblox_id || p.player.username)}/notes`, { method: "POST", body: { note: inp.value } }), "Note added."); S.refresh(); } }, "Add note")) : null,
        (p.notes || []).length ? h("ul", { class: "notes" }, p.notes.map((n) => h("li", null, h("div", { class: "note-meta" }, who(n.author_id, nameOf(n.author)), when(n.created_at), h("span", { class: "grow" }),
          p.can["erlc.moderate"] ? h("button", { class: "icon-btn sm", "aria-label": "Delete note", onclick: async () => { if (!(await confirmDialog("Delete this note?", "This can't be undone.", "Delete"))) return; await act(() => api(`/moderation/player-notes/${n.id}`, { method: "DELETE" }), "Note deleted."); S.refresh(); } }, icon("trash")) : null),
          h("p", null, n.note)))) : emptyState("No notes yet", "Notes are private to staff.", null, "note")));
    }
    if (tab === "sessions") return swap(tabBox, table([{ label: "Event", render: (e) => e.kind === "join" ? h("span", { class: "tag t-green" }, icon("login"), "Joined") : h("span", { class: "tag t-muted" }, icon("logout"), "Left") },
      { label: "Team", key: "team" }, { label: "When", render: (e) => when(e.at) }], p.events, { empty: emptyState("No sessions recorded", "Joins and leaves are recorded from v5.0 on.", null, "clock") }));
    if (tab === "bolos") return swap(tabBox, table([{ label: "#", render: (b) => h("b", { class: "num" }, `#${b.number}`) }, { label: "Subject", key: "subject" }, { label: "Priority", render: (b) => boloPill(b.priority) },
      { label: "Status", render: (b) => b.status === "active" ? pill("Active", "warn") : pill("Closed", "off") }, { label: "Created", render: (b) => when(b.created_at) }], p.bolos,
      { onRow: (b) => go(`bolos/${b.number}`), empty: emptyState("No BOLOs", null, p.can["bolo.manage"] ? h("button", { class: "btn", onclick: () => boloCreate({ player: p.player.username }) }, icon("eye"), "Create BOLO") : null, "eye") }));
    if (tab === "discord") return swap(tabBox, table([{ label: "Case", render: (c) => h("b", { class: "num" }, `#${c.number}`) }, { label: "Action", render: (c) => actionTag(c.action, {}) },
      { label: "Reason", render: (c) => h("span", { class: "clip" }, c.reason) }, { label: "Status", render: (c) => statusPill(c.status) }, { label: "When", render: (c) => when(c.created_at) }], p.discord_cases,
      { onRow: (c) => caseModal(c.number), empty: emptyState("No Discord cases", "Their linked Discord account has a clean record.", null, "message") }));
  };
  clear(el, h("div", { class: "back-row" }, h("a", { class: "btn ghost sm", href: `#/g/${S.gid}/erlc/players` }, icon("chevronLeft"), "Players")), headBox, card({ body: h("div", null, tabBar, tabBox) }));
  drawHead(); drawTab();
  S.refresh = async () => { try { p = await api(`/moderation/player/${encodeURIComponent(key)}`); } catch { return; } drawHead(); drawTab(); };
});

page("moderation/active", "Active punishments", async (el) => {
  let d = await api("/moderation/active");
  const box = h("div");
  const draw = () => swap(box, h("div", { class: "grid" },
    d.in_game !== null ? h("div", { class: d.discord !== null ? "col-6" : "col-12" }, card({ title: "In-game bans", icon: "gavel", sub: `${d.in_game.length} active`, flush: true, body: table([
      { label: "Player", render: (r) => h("a", { href: `#/g/${S.gid}/moderation/player/${r.roblox_id || r.roblox_username}` }, r.roblox_username) }, { label: "Type", render: (r) => actionTag(r.action) },
      { label: "Ends", render: (r) => r.expires_at ? when(r.expires_at) : pill("Permanent", "plain") }, { label: "By", render: (r) => who(r.moderator_id, nameOf(r.moderator)) }], d.in_game,
      { onRow: (r) => recordModal(r.number), empty: emptyState("No active in-game bans", null, null, "checkCircle") }) })) : null,
    d.discord !== null ? h("div", { class: d.in_game !== null ? "col-6" : "col-12" }, card({ title: "Discord bans & timeouts", icon: "message", sub: `${d.discord.length} active`, flush: true, body: table([
      { label: "Member", render: (c) => who(c.target_id, c.target_name) }, { label: "Type", render: (c) => actionTag(c.action, {}) },
      { label: "Ends", render: (c) => c.expires_at ? when(c.expires_at) : pill("Permanent", "plain") }, { label: "By", render: (c) => who(c.moderator_id, nameOf(c.moderator)) }], d.discord,
      { onRow: (c) => caseModal(c.number), empty: emptyState("No active Discord punishments", null, null, "checkCircle") }) })) : null));
  clear(el, head("Active punishments", "Bans and timeouts that are in effect right now. Temporary bans lift themselves when they end."), box);
  draw();
  S.refresh = async () => { try { d = await api("/moderation/active"); } catch { return; } draw(); };
});

page("moderation/staff", "Staff records", async (el) => {
  let days = 30;
  const box = h("div");
  const load = async () => {
    const d = await api(`/moderation/staff?days=${days}`);
    const cnt = (o, k) => o[k] || 0;
    swap(box, card({ flush: true, body: table([{ label: "Staff member", render: (m) => who(m.user_id, m.name, m.avatar) },
      { label: "Warnings", render: (m) => h("span", { class: "num" }, cnt(m.in_game, "warning") + cnt(m.discord, "warn")) },
      { label: "Kicks", render: (m) => h("span", { class: "num" }, cnt(m.in_game, "kick") + cnt(m.discord, "kick")) },
      { label: "Bans", render: (m) => h("span", { class: "num" }, cnt(m.in_game, "ban") + cnt(m.in_game, "tempban") + cnt(m.discord, "ban")) },
      { label: "Timeouts", render: (m) => h("span", { class: "num" }, cnt(m.discord, "timeout")) },
      { label: "Notes", render: (m) => h("span", { class: "num muted" }, cnt(m.in_game, "note") + cnt(m.discord, "note")) },
      { label: "Total", render: (m) => h("b", { class: "num" }, m.total) }], d.items,
      { onRow: (m) => navPaths().has("staff") ? go(`staff/${m.user_id}`) : null, empty: emptyState("No moderation in this period", null, null, "users") }) }));
  };
  clear(el, head("Staff moderation records", "How many actions each staff member took, in-game and in Discord."),
    h("div", { class: "toolbar end" }, chips([["7", "7 days"], ["30", "30 days"], ["90", "90 days"], ["365", "1 year"]], "30", (k) => { days = Number(k); load(); })), box);
  await load();
  S.refresh = load;
});

// ============================================================================ BOLOs
function boloCreate(pre = {}, existing = null) {
  const v = existing || pre;
  const f = form([
    { key: "kind", label: "Type", type: "select", options: { player: "Roblox player", person: "Person (description)", vehicle: "Vehicle", other: "Other" }, default: pre.player ? "player" : "person", half: true },
    { key: "priority", label: "Priority", type: "select", options: BOLO_PR, default: "medium", half: true },
    { key: "player", label: "Roblox player", type: "player", optional: true, help: "Link a player and staff are told when they join your server." },
    { key: "subject", label: "Subject", max: 100, placeholder: "Who or what to look out for", optional: !!pre.player },
    { key: "reason", label: "Why", type: "textarea", max: 300, placeholder: "Reported for RDM in the city centre" },
    { key: "description", label: "Description", type: "textarea", max: 800, optional: true, placeholder: "Clothing, avatar, behaviour…" },
    { key: "plate", label: "Plate", max: 20, optional: true, half: true }, { key: "last_seen", label: "Last seen", max: 150, optional: true, half: true }],
    { ...v, player: v.player || v.roblox_username || "" });
  modal({ title: existing ? `Edit BOLO #${existing.number}` : "New BOLO", body: h("div", null, callout("A BOLO is information for staff — it never bans or kicks anyone automatically.", "info", "eye"), f.el),
    actions: [{ label: "Cancel", ghost: true }, { label: existing ? "Save changes" : "Create BOLO", primary: true, icon: "check", onClick: async () => {
      const body = f.get();
      if (!body.subject && !body.player) throw new Error("Add a subject or a Roblox player.");
      const b = await act(() => existing ? api(`/bolos/${existing.number}`, { method: "PATCH", body }) : api("/bolos", { method: "POST", body }), (x) => existing ? "BOLO updated." : `BOLO #${x.number} created. Staff have been notified.`);
      if (S.refresh) S.refresh();
      return b;
    } }] });
}
window.boloCreate = boloCreate;
async function boloModal(n) {
  const b = await act(() => api(`/bolos/${n}`));
  const active = b.status === "active";
  const note = h("textarea", { rows: 2, maxlength: 1000, placeholder: "Add an update for staff" });
  const actions = [{ label: "Close", ghost: true }];
  if (b.can_manage) {
    actions.unshift({ label: "Edit", icon: "edit", onClick: () => { boloCreate({}, b); } });
    actions.unshift(active ? { label: "Close BOLO", danger: true, onClick: async () => {
      const f = form([{ key: "reason", label: "Why is it closed?", placeholder: "Located / resolved / no longer needed", max: 300 }]);
      modal({ title: `Close BOLO #${n}`, size: "narrow", body: f.el, actions: [{ label: "Cancel", ghost: true }, { label: "Close BOLO", danger: true, onClick: async () => {
        await act(() => api(`/bolos/${n}/close`, { method: "POST", body: f.get() }), `BOLO #${n} closed.`); S.refresh && S.refresh(); } }] });
    } } : { label: "Reopen", onClick: async () => { await act(() => api(`/bolos/${n}/reopen`, { method: "POST", body: {} }), `BOLO #${n} reopened.`); S.refresh && S.refresh(); } });
  }
  const m = modal({ title: `BOLO #${n}`, wide: true, actions, body: h("div", { class: "bolo-detail" },
    h("div", { class: "rec-head" }, boloPill(b.priority), active ? pill("Active", "warn") : pill("Closed", "off"), pill(b.kind, "plain"),
      b.player_online === true ? h("span", { class: "tag t-red" }, dot("bad"), "Player is in-game now") : b.player_online === false ? h("span", { class: "tag t-muted" }, "Not in-game") : null),
    h("h2", { class: "bolo-subject" }, b.subject),
    h("dl", { class: "facts" }, b.roblox_username ? [h("dt", null, "Roblox player"), h("dd", null, h("a", { href: `#/g/${S.gid}/moderation/player/${b.roblox_id || b.roblox_username}` }, b.roblox_username), b.roblox_id ? h("code", { class: "ml" }, b.roblox_id) : null)] : null,
      b.reason ? [h("dt", null, "Why"), h("dd", null, b.reason)] : null, b.description ? [h("dt", null, "Description"), h("dd", null, b.description)] : null,
      b.plate ? [h("dt", null, "Plate"), h("dd", null, h("code", null, b.plate))] : null, b.last_seen ? [h("dt", null, "Last seen"), h("dd", null, b.last_seen)] : null,
      h("dt", null, "Created"), h("dd", null, when(b.created_at), " by ", who(b.created_by, nameOf(b.creator))),
      !active ? [h("dt", null, "Closed"), h("dd", null, when(b.cleared_at), b.closer ? ` by ${nameOf(b.closer)}` : "", b.close_reason ? h("small", { class: "muted" }, ` — ${b.close_reason}`) : null)] : null),
    h("h3", null, "Updates"), canAny("bolo.manage", "erlc.moderate") ? h("div", { class: "note-add" }, note, h("button", { class: "btn", onclick: async () => {
      if (!note.value.trim()) return; await act(() => api(`/bolos/${n}/notes`, { method: "POST", body: { note: note.value } }), "Update added."); m.close(); boloModal(n); S.refresh && S.refresh(); } }, "Add")) : null,
    b.notes.length ? h("ul", { class: "notes" }, b.notes.map((x) => h("li", null, h("div", { class: "note-meta" }, who(x.author_id, nameOf(x.author)), when(x.created_at)), h("p", null, x.note)))) : h("p", { class: "muted" }, "No updates yet."),
    h("details", null, h("summary", null, `History (${b.history.length})`), h("ul", { class: "timeline" }, b.history.map((x) => h("li", null, h("b", null, x.event), nameOf(x.actor) ? ` · ${nameOf(x.actor)}` : "", h("span", { class: "muted" }, " "), when(x.at)))))) });
}
page("bolos", "BOLO board", async (el, args) => {
  if (args[0] && /^\d+$/.test(args[0])) setTimeout(() => boloModal(args[0]), 0);
  const q = { status: "active", q: "", priority: "", kind: "", page: 1 };
  const statsBox = h("div", { class: "stats" }), out = h("div"), tabBox = h("div");
  let d;
  const load = async (soft) => {
    if (!soft) swap(out, skeleton("rows"));
    d = await api(`/bolos?${new URLSearchParams(q)}`);
    const c = d.counts;
    swap(statsBox, stat({ label: "Active BOLOs", value: fmtNum(c.active || 0), ico: "eye", sub: "Informational only" }), stat({ label: "Critical", value: fmtNum(c.critical || 0), ico: "alert", tone: c.critical ? "t-bad" : "" }),
      stat({ label: "High", value: fmtNum(c.high || 0), ico: "warn", tone: c.high ? "t-warn" : "" }), stat({ label: "Closed", value: fmtNum(c.closed || 0), ico: "checkCircle" }));
    swap(tabBox, tabs([["active", "Active", c.active || 0], ["cleared", "Closed", c.closed || 0], ["", "All"]], q.status, (k) => { q.status = k; q.page = 1; load(); }));
    swap(out, d.items.length ? [h("div", { class: "bolo-grid" }, d.items.map((b) => h("button", { class: `bolo-card bc-${b.priority}${b.status !== "active" ? " closed" : ""}`, onclick: () => boloModal(b.number) },
      h("div", { class: "bolo-top" }, h("span", { class: "num muted" }, `#${b.number}`), boloPill(b.priority), h("span", { class: "grow" }), h("span", { class: "tag t-muted" }, icon(b.kind === "vehicle" ? "car" : b.kind === "player" ? "gamepad" : "user"), b.kind)),
      h("b", { class: "bolo-title" }, b.subject), b.roblox_username && b.roblox_username !== b.subject ? h("span", { class: "muted small" }, `Player: ${b.roblox_username}`) : null,
      b.reason || b.description ? h("p", null, b.reason || b.description) : null,
      h("div", { class: "bolo-foot" }, b.plate ? h("code", null, b.plate) : null, h("span", { class: "grow" }), h("small", { class: "muted" }, nameOf(b.creator) ? `${nameOf(b.creator)} · ` : ""), when(b.created_at))))), pager(d, (p) => { q.page = p; load(); })]
      : emptyState(q.q || q.priority || q.kind ? "No BOLOs match" : q.status === "active" ? "No active BOLOs" : "No BOLOs", "BOLOs help staff keep an eye on players or vehicles. They never ban anyone.",
        d.can_manage ? h("button", { class: "btn primary", onclick: () => boloCreate() }, icon("plus"), "New BOLO") : null, "eye"));
  };
  const f = (key, opts) => { const x = sel(opts, ""); x.addEventListener("change", () => { q[key] = x.value; q.page = 1; load(); }); return x; };
  clear(el, head("BOLO board", "Be-on-the-lookout alerts for staff. Linked players trigger an alert when they join — never an automatic ban.",
    can("bolo.manage") ? h("button", { class: "btn primary", onclick: () => boloCreate() }, icon("plus"), "New BOLO") : null),
    statsBox, h("div", { class: "toolbar" }, tabBox, h("span", { class: "grow" }), searchBox("Subject, player, plate or #number", (v) => { q.q = v; q.page = 1; load(); }),
      f("priority", { "": "Any priority", ...BOLO_PR }), f("kind", { "": "Any type", player: "Players", person: "People", vehicle: "Vehicles", other: "Other" })), out);
  await load();
  S.refresh = () => load(true);
});

// ============================================================================ STAFF
const AVAIL = { on_shift: ["On shift", "ok"], available: ["Available", "info"], on_leave: ["On leave", "warn"], inactive: ["Inactive", "off"] };
page("staff/overview", "Staff overview", async (el) => {
  let d = await api("/staff/overview");
  let filter = "";
  const statsBox = h("div", { class: "stats" }), tableBox = h("div"), chipBox = h("div");
  const draw = () => {
    const c = d.counts;
    swap(statsBox, stat({ label: "On shift now", value: fmtNum(c.on_shift), sub: `of ${fmtNum(d.total)} staff`, ico: "clock", tone: c.on_shift ? "t-good" : "", onClick: () => go("shifts") }),
      stat({ label: "Available", value: fmtNum(c.available), sub: "Active in the last 2 weeks", ico: "user" }),
      stat({ label: "On leave", value: fmtNum(c.on_leave), sub: d.pending_leave ? `${d.pending_leave} request(s) waiting` : "No pending requests", ico: "calendar", tone: d.pending_leave ? "t-warn" : "", onClick: () => go("staff/leave") }),
      stat({ label: "Hours this week", value: fmtNum(d.week.hours), sub: `${fmtNum(d.week.shifts)} shifts`, ico: "chart" }),
      stat({ label: "Inactive", value: fmtNum(c.inactive), sub: `No shift in ${d.inactive_days} days`, ico: "alert", tone: c.inactive ? "t-warn" : "" }));
    swap(chipBox, chips([["", `Everyone (${d.total})`], ...Object.entries(AVAIL).map(([k, [l]]) => [k, `${l} (${c[k]})`])], filter, (k) => { filter = k; drawTable(); }));
    drawTable();
  };
  const drawTable = () => {
    const rows = d.staff.filter((s) => !filter || s.availability === filter);
    swap(tableBox, table([{ label: "Staff member", render: (s) => who(s.user_id, s.display_name || s.username, s.avatar_url) }, { label: "Rank", render: (s) => h("b", null, s.rank) },
      { label: "Availability", render: (s) => h("span", { class: "row nowrap" }, pill(AVAIL[s.availability][0], AVAIL[s.availability][1]), s.availability === "on_shift" ? h("small", { class: "muted" }, s.shift_type, " · ", ticker(s.shift_started, "dur")) : null) },
      { label: "Hours 7d", render: (s) => h("span", { class: "num" }, s.hours_7d) }, { label: "Hours 30d", render: (s) => h("span", { class: "num" }, s.hours_30d) },
      { label: "Shifts 30d", render: (s) => h("span", { class: "num" }, s.shifts30) }, { label: "Mod actions 30d", render: (s) => h("span", { class: "num" }, s.moderation_30d) },
      { label: "Last shift", render: (s) => when(s.last_shift) }], rows,
      { onRow: (s) => go(`staff/${s.user_id}`), empty: emptyState(d.total ? "Nobody matches" : "No staff yet", d.total ? null : "Add your team in the Staff directory or with /staff add.", null, "badge") }));
  };
  clear(el, head("Staff overview", "Who's on shift, who's available and how the team is doing.",
    h("a", { class: "btn", href: `#/g/${S.gid}/shifts` }, icon("clock"), "My shift"), d.can["staff.manage"] ? h("a", { class: "btn", href: `#/g/${S.gid}/staff` }, icon("users"), "Directory") : null),
    statsBox, card({ title: "Team", icon: "badge", body: h("div", null, chipBox, tableBox) }));
  draw();
  S.refresh = async () => { try { d = await api("/staff/overview"); } catch { return; } draw(); };
});

page("shifts", "Shifts", async (el) => {
  let d = await api("/shifts");
  let histPage = 1;
  const mineBox = h("div"), activeBox = h("div"), leadBox = h("div"), histBox = h("div");
  const type = sel(Object.fromEntries(d.types.map((t) => [t, t])), d.types[0]);
  const notes = h("input", { type: "text", placeholder: "Shift notes (optional)", maxlength: 500 });
  const busy = { v: false };
  const drawMine = () => {
    const me = d.me, cur = me.current;
    swap(mineBox, card({ cls: "shift-card" + (cur ? " on" : ""), body: h("div", { class: "shift-hero" },
      h("div", { class: "shift-state" }, h("span", { class: "shift-dot" }), h("span", null, cur ? `On ${cur.shift_type} shift` : "Off duty")),
      h("div", { class: "shift-clock num" }, cur ? ticker(cur.started_at) : "0:00:00"),
      h("div", { class: "shift-meta" }, h("span", null, h("b", { class: "num" }, me.week_text), " this week"), h("span", null, h("b", { class: "num" }, me.total_text), ` across ${me.count} shift(s)`)),
      h("div", { class: "shift-ctl" }, cur ? [notes, h("button", { class: "btn danger lg", onclick: async (e) => {
        if (busy.v) return; busy.v = true; e.currentTarget.disabled = true;
        try { await act(() => api("/shifts/end", { method: "POST", body: { notes: notes.value } }), "Shift ended. Good work!"); notes.value = ""; await reload(); } finally { busy.v = false; } } }, icon("stop"), "End shift")]
        : [type, h("button", { class: "btn primary lg", onclick: async (e) => {
          if (busy.v) return; busy.v = true; e.currentTarget.disabled = true;
          try { await act(() => api("/shifts/start", { method: "POST", body: { type: type.value } }), "Shift started."); await reload(); } finally { busy.v = false; } } }, icon("play"), "Start shift")]),
      h("small", { class: "muted" }, "You can also use /shift start and /shift end in Discord. Shifts left running end automatically after the configured limit.")) }));
  };
  const drawActive = () => {
    if (!d.active) return swap(activeBox);
    swap(activeBox, card({ title: "On shift now", icon: "users", sub: `${d.active.length} active`, flush: true, body: table([{ label: "Member", render: (s) => who(s.user_id, s.display_name || s.username, s.avatar_url) },
      { label: "Rank", key: "rank" }, { label: "Type", render: (s) => pill(s.shift_type, "info") }, { label: "Elapsed", render: (s) => ticker(s.started_at) },
      { label: "", cls: "actions", render: (s) => can("shift.manage") && s.user_id !== S.me.user.id ? h("button", { class: "btn sm", onclick: async () => {
        if (!(await confirmDialog("End their shift?", `End ${s.display_name || s.username || s.user_id}'s shift now?`, "End shift"))) return;
        await act(() => api("/shifts/end", { method: "POST", body: { user_id: s.user_id, notes: "Ended by a supervisor" } }), "Shift ended."); reload(); } }, "End") : null }],
      d.active, { empty: emptyState("Nobody is on shift", "Shifts started here or with /shift appear instantly.", null, "clock") }) }));
    swap(leadBox, card({ title: "Most hours", icon: "chart", sub: "Last 7 days", body: d.leaderboard.length ? h("div", { class: "leader" }, d.leaderboard.map((r, i) => h("div", { class: "leader-row" },
      h("span", { class: "rank num" }, i + 1), who(r.user_id, r.display_name || r.username), h("span", { class: "grow" }), h("b", { class: "num" }, `${r.hours} h`)))) : emptyState("No shifts yet this week", null, null, "chart") }));
  };
  const drawHist = () => swap(histBox, card({ title: d.active ? "Shift history" : "My history", icon: "list", flush: true, body: h("div", null, table([{ label: "Member", render: (s) => who(s.user_id, s.display_name || s.username) },
    { label: "Type", key: "shift_type" }, { label: "Started", render: (s) => when(s.started_at) }, { label: "Length", render: (s) => h("span", { class: "num" }, dur(s.duration_s)) }, { label: "Notes", render: (s) => s.notes ? h("span", { class: "clip" }, s.notes) : null }],
    d.history.items, { empty: emptyState("No finished shifts yet", null, null, "clock") }), pager(d.history, async (p) => { histPage = p; await reload(); })) }));
  const reload = async () => { d = await api(`/shifts?page=${histPage}`); drawMine(); drawActive(); drawHist(); };
  clear(el, head("Shifts", "Start and end your shift, and see who's on duty."),
    h("div", { class: "grid" }, h("div", { class: "col-5" }, mineBox, leadBox), h("div", { class: "col-7" }, activeBox, histBox)));
  drawMine(); drawActive(); drawHist();
  S.refresh = async () => { try { d = await api(`/shifts?page=${histPage}`); } catch { return; } drawActive(); drawHist(); if (!document.activeElement || !mineBox.contains(document.activeElement)) drawMine(); };
});

page("staff/leave", "Leave (LOA)", async (el) => {
  const reviewer = can("staff.leave_review");
  let status = "pending";
  const mineBox = h("div"), reviewBox = h("div");
  const today = new Date().toISOString().slice(0, 10);
  const loadMine = async () => {
    const m = await api("/staff/leave/mine");
    if (!m.is_staff) return swap(mineBox, card({ title: "My leave", icon: "calendar", body: emptyState("Only staff can request leave", "Ask a manager to add you to the staff team.", null, "calendar") }));
    const f = form([{ key: "starts_on", label: "From", type: "date", min: today, default: today, half: true }, { key: "ends_on", label: "To", type: "date", min: today, half: true },
      { key: "reason", label: "Reason", type: "textarea", max: 500, placeholder: "Vacation, exams, family…" }]);
    const pending = m.items.find((x) => x.status === "pending");
    swap(mineBox, card({ title: "Request leave", icon: "calendar", body: h("div", null, pending ? callout(`Your request for ${pending.starts_on} → ${pending.ends_on} is waiting for review.`, "info") : [f.el,
      h("div", { class: "form-actions" }, h("button", { class: "btn primary", onclick: async () => { await act(() => api("/staff/leave/request", { method: "POST", body: f.get() }), "Leave requested. Management has been notified."); loadMine(); if (reviewer) loadReview(); } }, icon("check"), "Submit request"))],
      h("h3", null, "My requests"), table([{ label: "Dates", render: (r) => `${r.starts_on} → ${r.ends_on}` }, { label: "Reason", render: (r) => h("span", { class: "clip" }, r.reason) },
        { label: "Status", render: (r) => pill(r.status, { approved: "ok", denied: "bad", pending: "warn", cancelled: "off" }[r.status]) },
        { label: "", cls: "actions", render: (r) => ["pending", "approved"].includes(r.status) && r.ends_on >= today ? h("button", { class: "btn sm", onclick: async () => {
          if (!(await confirmDialog("Cancel this leave?", `${r.starts_on} → ${r.ends_on}`, "Cancel leave"))) return; await act(() => api(`/staff/leave/${r.id}/cancel`, { method: "POST", body: {} }), "Leave cancelled."); loadMine(); } }, "Cancel") : null }],
        m.items, { dense: true, empty: emptyState("No requests yet", null, null, "calendar") })) }));
  };
  const loadReview = async () => {
    const d = await api(`/staff/leave?status=${status}`);
    swap(reviewBox, card({ title: "Review requests", icon: "checkCircle", body: h("div", null, tabs([["pending", "Waiting"], ["approved", "Approved"], ["denied", "Denied"], ["", "All"]], status, (k) => { status = k; loadReview(); }),
      table([{ label: "Member", render: (r) => who(r.user_id, r.display_name || r.username) }, { label: "Dates", render: (r) => `${r.starts_on} → ${r.ends_on}` }, { label: "Reason", render: (r) => h("span", { class: "clip" }, r.reason) },
        { label: "Status", render: (r) => pill(r.status, { approved: "ok", denied: "bad", pending: "warn", cancelled: "off" }[r.status]) },
        { label: "", cls: "actions", render: (r) => r.status === "pending" ? h("span", { class: "row nowrap" },
          h("button", { class: "btn sm", onclick: async () => { await act(() => api(`/staff/leave/${r.id}`, { method: "POST", body: { approve: false } }), "Denied. They've been told."); loadReview(); } }, "Deny"),
          h("button", { class: "btn sm primary", onclick: async () => { await act(() => api(`/staff/leave/${r.id}`, { method: "POST", body: { approve: true } }), "Approved. They've been told."); loadReview(); } }, "Approve")) : null }],
        d.items, { empty: emptyState(status === "pending" ? "No requests waiting" : "No leave requests", "Approved leave starts and ends automatically on its dates.", null, "calendar") })) }));
  };
  clear(el, head("Leave of absence", "Request time off and track approvals. Approved leave sets your status automatically."),
    h("div", { class: "grid" }, h("div", { class: reviewer ? "col-5" : "col-8" }, mineBox), reviewer ? h("div", { class: "col-7" }, reviewBox) : null));
  await Promise.all([loadMine(), reviewer ? loadReview() : null]);
  S.refresh = async () => { if (reviewer) await loadReview().catch(() => {}); };
});

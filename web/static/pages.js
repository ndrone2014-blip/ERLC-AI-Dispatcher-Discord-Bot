/* Hive Dispatch dashboard — pages. Loaded after app.js. */
"use strict";

// The dashboard and activity feed live in pages-v5.js (v5.0).
page("analytics", "Analytics", async (el) => {
  let range = "7d", custom = { start: "", end: "" };
  const body = h("div");
  const load = async () => {
    clear(body, loading());
    const q = range === "custom" ? `range=custom&start=${custom.start}&end=${custom.end}` : `range=${range}`;
    try {
      const a = await api(`/analytics?${q}`);
      const grid = h("div", { class: "panels" });
      for (const [k, m] of Object.entries(a.metrics)) {
        const note = m.tracking_since ? h("small", { class: "muted" }, `Recorded since ${new Date(m.tracking_since).toLocaleDateString()}`)
          : (k.startsWith("member_") ? h("small", { class: "muted" }, "Recorded from now on (joins before v4.0 weren't tracked)") : null);
        grid.append(h("div", { class: "span-4" }, panel(m.label, h("div", null, h("div", { class: "spark-total" }, fmtNum(m.total)), chart(a.days, [{ label: m.label, values: m.values }], { type: "bar" }), note))));
      }
      const lead = (title, rows, cols) => h("div", { class: "span-6" }, panel(title, table(cols, rows, { empty: emptyState("No data in this range", null) })));
      grid.append(lead("Most shift hours", a.leaders.shifts, [{ label: "Member", render: (r) => who(r.user_id, r.display_name || r.username) }, { label: "Hours", key: "hours" }, { label: "Shifts", key: "shifts" }]));
      grid.append(lead("Most used commands", a.leaders.commands, [{ label: "Command", render: (r) => `/${r.command}` }, { label: "Uses", key: "count" }]));
      grid.append(lead("AI providers", a.breakdowns.ai, [{ label: "Provider", key: "provider" }, { label: "Requests", key: "n" }, { label: "Succeeded", key: "ok" }, { label: "Avg ms", key: "ms" }]));
      grid.append(lead("Moderation by action", Object.entries(a.breakdowns.moderation).map(([k, v]) => ({ k, v })), [{ label: "Action", key: "k" }, { label: "Cases", key: "v" }]));
      clear(body, h("p", { class: "muted small" }, `${a.range.start} to ${a.range.end} (UTC). Every number comes from recorded events — nothing is estimated.`), grid);
    } catch (e) { clear(body, errorBox(e)); }
  };
  const ranges = [["today", "Today"], ["yesterday", "Yesterday"], ["7d", "7 days"], ["30d", "30 days"], ["90d", "90 days"], ["custom", "Custom"]];
  const customRow = h("div", { class: "filters hidden" }, h("input", { type: "date", onchange: (e) => { custom.start = e.target.value; } }),
    h("input", { type: "date", onchange: (e) => { custom.end = e.target.value; } }), h("button", { class: "btn", onclick: load }, "Apply"));
  const tabRow = h("div");
  const drawTabs = () => clear(tabRow, tabs(ranges, range, (k) => { range = k; drawTabs(); customRow.classList.toggle("hidden", k !== "custom"); if (k !== "custom") load(); }));
  drawTabs();
  const exp = can("logs.export") ? h("a", { class: "btn", href: "#", onclick: (e) => { e.preventDefault(); toast("Use Logging → Log viewer → Export for CSV exports of events."); } }, "Export") : null;
  clear(el, head("Analytics", "Trends across dispatch, community and staff activity.", exp), tabRow, customRow, body);
  load();
});

// ============================================================================ DISPATCH
function incidentCreate(after) {
  const f = form([{ key: "nature", label: "What happened?", placeholder: "Structure fire, armed robbery, vehicle crash…", max: 120 },
                  { key: "location", label: "Location", placeholder: "Postal 214, Central Bank…", max: 200 },
                  { key: "description", label: "Details", type: "textarea" }, { key: "category", label: "Category (optional)", max: 60 }]);
  modal({ title: "New incident", body: f.el, actions: [{ label: "Cancel" }, { label: "Create incident", primary: true, onClick: async () => {
    const inc = await act(() => api("/incidents", { method: "POST", body: f.get() }), (i) => `Incident #${i.number} created.`);
    after && after(inc);
  } }] });
}
async function incidentModal(number, board) {
  const d = await act(() => api(`/incidents/${number}`));
  const i = d.incident, open = i.status !== "closed";
  const body = h("div");
  const units = board ? board.units : [];
  const rows = [
    h("dt", null, "Status"), h("dd", null, pill(i.status, open ? "info" : "off"), " ", i.workflow_status ? pill(i.workflow_status, "plain") : null),
    h("dt", null, "Priority"), h("dd", null, prPill(i.effective_priority), i.priority_status === "recommended" && i.ai_priority ? h("span", { class: "muted small" }, " awaiting approval") : null),
    h("dt", null, "Location"), h("dd", null, i.location || "—"), h("dt", null, "Source"), h("dd", null, `${i.source} (${i.verification})`),
    h("dt", null, "Units"), h("dd", null, i.units.length ? i.units.map((u) => h("span", { class: "pill plain", style: "margin-right:4px" }, u.callsign,
      open && can("incident.attach_other") ? h("button", { class: "btn ghost sm", "aria-label": `Clear ${u.callsign}`, onclick: async () => {
        await act(() => api(`/incidents/${number}/unassign`, { method: "POST", body: { callsign: u.callsign } }), `${u.callsign} cleared.`); m.close(); incidentModal(number, board); } }, "✕") : null)) : "None"),
    h("dt", null, "Received"), h("dd", null, when(i.created_at)), i.recommendation_reason ? [h("dt", null, "Recommendation"), h("dd", null, i.recommendation_reason)] : null,
    i.description && i.description !== i.nature ? [h("dt", null, "Details"), h("dd", null, i.description)] : null];
  body.append(h("dl", { class: "facts" }, rows));
  if (open) {
    const ctl = h("div", { class: "panels", style: "margin-top:16px" });
    if (can("incident.update")) {
      const ws = sel(Object.fromEntries([["", "No status"], ...(board?.workflow_statuses || []).map((s) => [s, s])]), i.workflow_status || "");
      ws.addEventListener("change", () => act(() => api(`/incidents/${number}`, { method: "PATCH", body: { workflow_status: ws.value || null } }), "Status updated."));
      ctl.append(h("div", { class: "span-6" }, h("label", { class: "field" }, h("span", null, "Incident status"), ws)));
    }
    if (can("incident.priority_approve")) {
      const ps = sel(PR, i.effective_priority || "medium");
      ctl.append(h("div", { class: "span-6" }, h("label", { class: "field" }, h("span", null, "Priority"), h("div", { class: "ask" }, ps,
        h("button", { class: "btn", onclick: async () => { await act(() => api(`/incidents/${number}/priority`, { method: "POST", body: { priority: ps.value } }), "Priority set."); m.close(); incidentModal(number, board); } }, "Set")))));
    }
    if (can("incident.attach_other")) {
      const free = units.filter((u) => !u.current_incident_id || u.current_incident_id !== i.id);
      const us = sel(Object.fromEntries(free.map((u) => [u.callsign, `${u.callsign} · ${u.status_code} ${u.status_label || ""}`])), "");
      ctl.append(h("div", { class: "span-6" }, h("label", { class: "field" }, h("span", null, "Assign a unit"), free.length ? h("div", { class: "ask" }, us,
        h("button", { class: "btn", onclick: async () => { await act(() => api(`/incidents/${number}/assign`, { method: "POST", body: { callsign: us.value } }), `${us.value} assigned.`); m.close(); incidentModal(number, board); } }, "Assign"))
        : h("div", { class: "muted small" }, "No units on duty to assign."))));
    }
    if (can("incident.note")) {
      const note = h("input", { type: "text", placeholder: "Add a note for responders", maxlength: 500 });
      ctl.append(h("div", { class: "span-6" }, h("label", { class: "field" }, h("span", null, "Note"), h("div", { class: "ask" }, note,
        h("button", { class: "btn", onclick: async () => { if (!note.value.trim()) return; await act(() => api(`/incidents/${number}/note`, { method: "POST", body: { note: note.value } }), "Note added."); m.close(); incidentModal(number, board); } }, "Add")))));
    }
    body.append(ctl);
  }
  body.append(h("h3", { style: "margin-top:12px" }, "History"), h("ul", { class: "list" }, d.history.slice(0, 40).map((x) => h("li", null, h("b", null, x.event_type),
    h("span", { class: "muted small" }, x.detail?.note || x.detail?.reason || (x.detail?.after ? Object.entries(x.detail.after).map(([k, v]) => `${k}: ${v}`).join(", ") : "")), h("span", { class: "when" }, when(x.at))))));
  const actions = [{ label: "Close window" }];
  if (open && can("incident.update")) actions.unshift({ label: "Edit details", onClick: () => { incidentEdit(i, () => incidentModal(number, board)); } });
  if (open && can("incident.close")) actions.unshift({ label: "Close incident", danger: true, onClick: async () => {
    if (!(await confirmDialog(`Close incident #${number}?`, "Assigned units are cleared and repeat announcements stop.", "Close incident"))) return true;
    await act(() => api(`/incidents/${number}/close`, { method: "POST", body: { reason: "Closed from the dashboard" } }), `Incident #${number} closed.`);
    S.refresh && S.refresh();
  } });
  const m = modal({ title: `Incident #${number} — ${i.nature || "Unknown"}`, body, wide: true, actions, onClose: () => S.refresh && S.refresh() });
}
function incidentEdit(i, after) {
  const f = form([{ key: "nature", label: "Nature", max: 120 }, { key: "location", label: "Location", max: 200 }, { key: "description", label: "Details", type: "textarea" },
                  { key: "category", label: "Category", max: 60 }], i);
  modal({ title: `Edit incident #${i.number}`, body: f.el, actions: [{ label: "Cancel" }, { label: "Save", primary: true, onClick: async () => {
    await act(() => api(`/incidents/${i.number}`, { method: "PATCH", body: f.get() }), "Incident updated."); after && after(); } }] });
}

page("dispatch", "Dispatch center", async (el) => {
  const b = await api("/dispatch");
  const unitStatus = (u) => {
    if (!can("unit.manage_other")) return h("span", null, `${u.status_code} ${u.status_label || ""}`);
    const s = sel(Object.fromEntries(b.status_codes.map((c) => [c.code, `${c.code} · ${c.label}`])), u.status_code);
    s.addEventListener("change", () => act(() => api(`/units/${u.id}/status`, { method: "POST", body: { code: s.value } }), `${u.callsign} → ${s.value}`));
    return s;
  };
  const incTable = table([
    { label: "Priority", render: (i) => prPill(i.effective_priority) }, { label: "#", render: (i) => h("b", { class: "num" }, i.number) },
    { label: "Nature", render: (i) => h("span", null, i.nature || "Unknown", i.source === "erlc" ? h("span", { class: "muted small" }, " · in-game") : null) },
    { label: "Location", key: "location" }, { label: "Status", render: (i) => i.workflow_status || i.status },
    { label: "Units", render: (i) => i.units.map((u) => u.callsign).join(", ") || h("span", { class: "muted" }, "Awaiting units") },
    { label: "Age", render: (i) => when(i.created_at) }], b.incidents,
    { onRow: (i) => incidentModal(i.number, b), empty: emptyState("No active incidents", "In-game 911 calls, Discord reports and dashboard incidents appear here live.") });
  for (const tr of incTable.querySelectorAll?.("tbody tr") || []) {
    const i = b.incidents[[...tr.parentNode.children].indexOf(tr)];
    tr.classList.add("inc", `pr-${i.effective_priority || "none"}`);
  }
  clear(el, head("Dispatch center", "Live incidents and units. Updates arrive automatically.",
    can("incident.manual_create") ? h("button", { class: "btn primary", onclick: () => incidentCreate((inc) => { renderPage("dispatch"); incidentModal(inc.number, b); }) }, "New incident") : null),
    h("div", { class: "panels" },
      h("div", { class: "span-8" }, panel(`Active incidents (${b.incidents.length})`, incTable)),
      h("div", { class: "span-4" }, panel(`Units on duty (${b.units.length})`, b.units.length ? h("ul", { class: "list" }, b.units.map((u) => h("li", null,
        h("b", { class: "num" }, u.callsign), h("span", { class: "muted small" }, u.dept_code || ""), h("span", { class: "when" }, unitStatus(u)))))
        : emptyState("No units on duty", "Officers register with /unit-register or on the Units page.")),
        b.backups.length ? panel("Backup requests", h("ul", { class: "list" }, b.backups.map((x) => h("li", null, pill(x.urgency, x.urgency === "routine" ? "info" : "bad"), `${x.callsign || "Unit"} on #${x.incident_number || "—"}`, h("span", { class: "when" }, when(x.created_at)))))) : null,
        b.bolos.length ? panel("Active BOLOs", h("ul", { class: "list" }, b.bolos.map((x) => h("li", null, h("b", null, `#${x.number}`), `${x.kind}: ${x.subject}`)))) : null)));
  S.refresh = () => renderPage("dispatch");
});

page("incidents", "Incident history", async (el) => {
  const q = { q: "", status: "", priority: "", source: "", page: 1 };
  const out = h("div");
  const load = async () => {
    clear(out, loading());
    const d = await api(`/incidents?${new URLSearchParams(q)}`);
    clear(out, table([{ label: "#", render: (i) => h("b", { class: "num" }, i.number) }, { label: "Priority", render: (i) => prPill(i.effective_priority) },
      { label: "Nature", key: "nature" }, { label: "Location", key: "location" }, { label: "Source", key: "source" }, { label: "Status", key: "status" },
      { label: "Created", render: (i) => when(i.created_at) }], d.items, { onRow: (i) => incidentModal(i.number), empty: emptyState("No incidents match", "Try a different search or filter.") }),
      pager(d, (p) => { q.page = p; load(); }));
  };
  const s = h("input", { type: "search", placeholder: "Search nature, location or #number", oninput: debounce((e) => { q.q = e.target.value; q.page = 1; load(); }, 300) });
  const f = (key, opts) => { const x = sel(opts, ""); x.addEventListener("change", () => { q[key] = x.value; q.page = 1; load(); }); return x; };
  clear(el, head("Incident history", "Every incident, open and closed."), h("div", { class: "filters" }, s,
    f("status", { "": "Any status", pending: "Pending", active: "Active", closed: "Closed" }), f("priority", { "": "Any priority", ...PR }),
    f("source", { "": "Any source", erlc: "In-game", discord: "Discord", manual: "Manual", test: "Test" })), out);
  load();
});

page("units", "Units", async (el) => {
  const d = await api("/units");
  const reg = () => {
    const f = form([{ key: "user_id", label: "Member", type: "member" }, { key: "name", label: "Officer name", max: 40 }, { key: "callsign", label: "Callsign", placeholder: "1A-01", max: 15 },
                    { key: "department", label: "Department", type: "select", options: Object.fromEntries(d.departments.map((x) => [x.short_code, `${x.short_code} · ${x.name}`])) },
                    { key: "roblox_username", label: "Roblox username (optional)", max: 40 }]);
    modal({ title: "Add a member to a unit", body: f.el, actions: [{ label: "Cancel" }, { label: "Save", primary: true, onClick: async () => {
      const v = f.get(); if (!v.user_id) throw new Error("Choose a member (search or paste their user ID).");
      await act(() => api("/units/register", { method: "POST", body: v }), "Unit saved."); renderPage("units"); } }] });
  };
  clear(el, head("Units", "Units are created when a member registers with a callsign. Empty units are removed automatically.",
    can("unit.manage_other") ? h("button", { class: "btn primary", onclick: reg }, "Add member to unit") : null),
    h("div", { class: "panels" },
      h("div", { class: "span-7" }, panel("All units", table([{ label: "Callsign", render: (u) => h("b", { class: "num" }, u.callsign) }, { label: "Department", key: "dept_code" },
        { label: "Status", render: (u) => `${u.status_code} ${u.status_label || ""}` }, { label: "Incident", render: (u) => u.incident_number ? `#${u.incident_number}` : null },
        { label: "Members", key: "members" }], d.units, { empty: emptyState("No units yet", "Members register with /unit-register, or add them here.") }))),
      h("div", { class: "span-5" }, panel("Registered officers", table([{ label: "Officer", key: "name" }, { label: "Dept", key: "dept_code" }, { label: "Roblox", key: "roblox_username" },
        { label: "", cls: "actions", render: (o) => can("unit.manage_other") ? h("button", { class: "btn sm danger", onclick: async () => {
          if (!(await confirmDialog("Remove officer?", `${o.name} will be taken off their unit.`, "Remove"))) return;
          await act(() => api(`/units/officers/${o.discord_id}`, { method: "DELETE" }), "Officer removed."); renderPage("units"); } }, "Remove") : null }], d.officers)),
        panel("Recent status changes", h("ul", { class: "list" }, d.history.map((x) => h("li", null, h("b", null, x.callsign), `${x.old_code || "—"} → ${x.new_code}`, h("span", { class: "when" }, when(x.at)))))))));
  S.refresh = () => renderPage("units");
});

page("dispatch/settings", "Dispatch settings", async (el) => {
  const d = await api("/dispatch/settings");
  const s = d.settings, pr = Object.fromEntries(d.priorities.map((p) => [p.value, p.label]));
  const fields = [
    { key: "response_style", label: "Dispatcher response style", type: "select", options: d.styles },
    { key: "agency_name", label: "Agency name", help: "Used in radio phrasing, e.g. 'Liberty County Dispatch'.", max: 60 },
    { key: "priority_approval_threshold", label: "Supervisor approval needed from", type: "select", options: pr },
    { key: "follow_up_questions", label: "Ask follow-up questions", type: "check", help: "Ask for a missing location or nature instead of guessing." },
    { key: "merge_duplicates", label: "Merge duplicate reports into the active incident", type: "check" },
    { key: "duplicate_window_s", label: "Duplicate window (seconds)", type: "number", min: 60, max: 86400 },
    { key: "text_dispatch_enabled", label: "Plain-language #dispatch channel", type: "check" },
    { key: "allow_unverified_reports", label: "Allow Discord reports (unverified)", type: "check" },
    { key: "repeat_enabled", label: "Repeat unanswered calls on the radio", type: "check" },
    { key: "repeat_interval_s", label: "Repeat every (seconds)", type: "number", min: 30, max: 3600 },
    { key: "repeat_max", label: "Maximum repeats", type: "number", min: 0, max: 10 },
    { key: "repeat_min_priority", label: "Only repeat from priority", type: "select", options: pr },
    { key: "ai_announcement_wording", label: "AI-written announcements", type: "check", help: "Slower: adds an AI request before speaking." }];
  const depForm = () => {
    const f = form([{ key: "name", label: "Name", max: 60 }, { key: "code", label: "Short code", placeholder: "PD", max: 8 },
                    { key: "kind", label: "Type", type: "select", options: d.department_kinds }, { key: "role_id", label: "Department role (optional)", type: "role" }]);
    modal({ title: "Add department", body: f.el, actions: [{ label: "Cancel" }, { label: "Add", primary: true, onClick: async () => {
      await act(() => api("/dispatch/departments", { method: "POST", body: f.get() }), "Department added."); renderPage("dispatch/settings"); } }] });
  };
  const codeForm = () => {
    const f = form([{ key: "code", label: "Code", placeholder: "10-97", max: 12 }, { key: "label", label: "Label", placeholder: "Arrived on scene", max: 40 },
                    { key: "availability", label: "Counts as", type: "select", options: { available: "Available", assigned: "Assigned", unavailable: "Unavailable", off: "Off duty" } }]);
    modal({ title: "Add unit status", body: f.el, actions: [{ label: "Cancel" }, { label: "Save", primary: true, onClick: async () => {
      await act(() => api("/dispatch/status-codes", { method: "POST", body: f.get() }), "Status saved."); renderPage("dispatch/settings"); } }] });
  };
  clear(el, head("Dispatch settings", "How the dispatcher behaves, plus departments, unit statuses and incident statuses."),
    h("div", { class: "panels" },
      h("div", { class: "span-6" }, settingsPanel("Dispatcher", fields, s, (v) => api("/dispatch/settings", { method: "PUT", body: { settings: v } }))),
      h("div", { class: "span-6" },
        settingsPanel("Incident statuses", [{ key: "workflow_statuses", label: "One per line", type: "lines", help: "Shown on the Dispatch center. Incidents still open/close as before." }],
          { workflow_statuses: d.workflow_statuses }, (v) => api("/dispatch/settings", { method: "PUT", body: v })),
        panel("Departments", table([{ label: "Code", key: "short_code" }, { label: "Name", key: "name" }, { label: "Type", key: "kind" },
          { label: "", cls: "actions", render: (x) => h("button", { class: "btn sm danger", onclick: async () => {
            if (!(await confirmDialog(`Remove ${x.name}?`, "Officers keep their records; the department is hidden.", "Remove"))) return;
            await act(() => api(`/dispatch/departments/${encodeURIComponent(x.short_code)}`, { method: "DELETE" }), "Removed."); renderPage("dispatch/settings"); } }, "Remove") }], d.departments),
          h("button", { class: "btn sm", onclick: depForm }, "Add department")),
        panel("Unit statuses", table([{ label: "Code", key: "code" }, { label: "Label", key: "label" }, { label: "Counts as", key: "availability" },
          { label: "", cls: "actions", render: (x) => h("button", { class: "btn sm danger", onclick: async () => {
            await act(() => api(`/dispatch/status-codes/${encodeURIComponent(x.code)}`, { method: "DELETE" }), "Removed."); renderPage("dispatch/settings"); } }, "Remove") }], d.status_codes),
          h("button", { class: "btn sm", onclick: codeForm }, "Add status")))));
});

// ============================================================================ COMMUNITY
async function memberModal(uid) {
  const p = await act(() => api(`/members/${uid}`));
  const m = p.member, body = h("div");
  body.append(h("div", { class: "row", style: "margin-bottom:12px" }, m.avatar_url ? h("img", { src: m.avatar_url, alt: "", class: "avatar", style: "width:48px;height:48px" }) : null,
    h("div", null, h("h2", null, m.display_name || m.username || uid), h("div", { class: "muted small" }, `@${m.username || "?"} · ID ${uid}`))));
  body.append(h("dl", { class: "facts" },
    h("dt", null, "Roblox"), h("dd", null, p.roblox ? h("a", { href: p.roblox.profile_url, target: "_blank", rel: "noopener noreferrer" }, p.roblox.roblox_username) : "Not verified"),
    h("dt", null, "Staff"), h("dd", null, p.staff && p.staff.status !== "removed" ? `${p.staff.rank} (${p.staff.status})` : "—"),
    h("dt", null, "Unit"), h("dd", null, p.officer ? `${p.officer.callsign || "—"} · ${p.officer.dept_code || ""}` : "—"),
    h("dt", null, "Joined"), h("dd", null, when(m.joined_at)), m.left_at ? [h("dt", null, "Left"), h("dd", null, when(m.left_at))] : null,
    h("dt", null, "Shifts"), h("dd", null, `${p.shifts.count} · ${p.shifts.total_text}`)));
  if (p.applications.length) body.append(h("h3", { style: "margin-top:14px" }, "Applications"), h("ul", { class: "list" }, p.applications.map((a) => h("li", null, link(`applications/${a.number}`, `#${a.number} ${a.form_name}`), pill(a.status, "plain"), h("span", { class: "when" }, when(a.created_at))))));
  if (p.cases) body.append(h("h3", { style: "margin-top:14px" }, `Moderation (${p.cases.length})`), p.cases.length ? h("ul", { class: "list" }, p.cases.map((c) => h("li", null, link(`moderation/${c.number}`, `#${c.number} ${c.action}`), h("span", { class: "muted" }, c.reason), h("span", { class: "when" }, when(c.created_at))))) : h("p", { class: "muted" }, "Clean record."));
  if (p.notes) {
    const note = h("input", { type: "text", maxlength: 1000, placeholder: "Add a private note for staff" });
    body.append(h("h3", { style: "margin-top:14px" }, "Staff notes"), h("ul", { class: "list" }, p.notes.map((n) => h("li", null, h("span", null, n.note), h("span", { class: "when" }, when(n.created_at))))),
      h("div", { class: "ask" }, note, h("button", { class: "btn", onclick: async () => { if (!note.value.trim()) return; await act(() => api(`/members/${uid}/notes`, { method: "POST", body: { note: note.value } }), "Note added."); mo.close(); memberModal(uid); } }, "Add note")));
  }
  const actions = [{ label: "Close" }];
  if (can("mod.warn")) actions.unshift({ label: "Moderate", onClick: () => caseCreate(uid) });
  if (can("verification.manage")) actions.unshift(p.roblox ? { label: "Remove Roblox link", danger: true, onClick: async () => {
    if (!(await confirmDialog("Remove Roblox link?", "Their verified role is removed and they can verify again.", "Remove link"))) return true;
    await act(() => api(`/verification/${uid}`, { method: "DELETE" }), "Link removed."); } } : { label: "Link Roblox account", onClick: () => manualLink(uid) });
  const mo = modal({ title: "Member profile", body, wide: true, actions });
}
page("members", "Members", async (el) => {
  const q = { q: "", filter: "all", page: 1 };
  const out = h("div");
  const load = async () => {
    clear(out, loading());
    const d = await api(`/members?${new URLSearchParams(q)}`);
    clear(out, table([{ label: "Member", render: (m) => who(m.user_id, m.display_name || m.username, m.avatar_url) }, { label: "Roblox", key: "roblox_username" },
      { label: "Staff", render: (m) => m.staff_status && m.staff_status !== "removed" ? m.staff_rank : null }, { label: "Joined", render: (m) => when(m.joined_at) },
      { label: "Active cases", render: (m) => m.case_count ? pill(String(m.case_count), "warn") : null }], d.items,
      { onRow: (m) => memberModal(m.user_id), empty: emptyState("No members match", "The directory fills in automatically when the bot starts and as people join.") }),
      pager(d, (p) => { q.page = p; load(); }));
  };
  const fsel = sel({ all: "Everyone", verified: "Verified", unverified: "Not verified", staff: "Staff", regular: "Regular members", moderated: "Has moderation history", left: "Left the server" }, "all");
  fsel.addEventListener("change", () => { q.filter = fsel.value; q.page = 1; load(); });
  clear(el, head("Members", "Search your community. Click a member for their full profile."), h("div", { class: "filters" },
    h("input", { type: "search", placeholder: "Name, user ID or Roblox username", oninput: debounce((e) => { q.q = e.target.value; q.page = 1; load(); }, 300) }), fsel), out);
  load();
});

function manualLink(uid) {
  const f = form([{ key: "user_id", label: "Member", type: "member" }, { key: "roblox_username", label: "Roblox username", max: 20 },
                  { key: "reason", label: "Reason (kept in the history)", max: 300 }], { user_id: uid || "" });
  modal({ title: "Link a Roblox account manually", body: h("div", null, h("div", { class: "callout" }, "Use this when a member can't verify themselves. It's recorded as a staff link with your name."), f.el),
    actions: [{ label: "Cancel" }, { label: "Link account", primary: true, onClick: async () => {
      const v = f.get(); if (!v.user_id) throw new Error("Choose a member.");
      const r = await act(() => api("/verification/link", { method: "POST", body: v }), (x) => `Linked to ${x.roblox.name}.`);
      if (r.problems?.length) toast(r.problems.join(" "), true);
      S.refresh && S.refresh(); } }] });
}
page("verification", "Verification", async (el, args) => {
  let tab = args[0] || "links";
  const d = await api("/verification");
  const s = d.settings;
  const body = h("div");
  const draw = async () => {
    clear(body, loading());
    if (tab === "links") {
      const q = { q: "", page: 1 };
      const out = h("div");
      const load = async () => { const v = await api(`/verification?${new URLSearchParams(q)}`); const L = v.links;
        clear(out, table([{ label: "Member", render: (r) => who(r.discord_id, r.display_name || r.username, r.avatar_url) },
          { label: "Roblox", render: (r) => h("a", { href: `https://www.roblox.com/users/${r.roblox_id}/profile`, target: "_blank", rel: "noopener noreferrer" }, r.roblox_username) },
          { label: "Method", render: (r) => ({ profile_code: "Profile code", oauth: "Roblox sign-in", manual: "Staff link", bloxlink: "From Bloxlink", rover: "From RoVer" }[r.method] || r.method) }, { label: "Verified", render: (r) => when(r.verified_at) },
          { label: "", cls: "actions", render: (r) => h("span", null, h("button", { class: "btn sm", onclick: async () => { const x = await act(() => api(`/verification/${r.discord_id}/sync`, { method: "POST" })); toast(x.problems.length ? x.problems.join(" ") : "Roles and nickname updated.", !!x.problems.length); } }, "Sync roles"),
            " ", h("button", { class: "btn sm danger", onclick: async () => { if (!(await confirmDialog("Remove link?", `${r.roblox_username} will be unlinked.`, "Remove"))) return; await act(() => api(`/verification/${r.discord_id}`, { method: "DELETE" }), "Link removed."); load(); } }, "Remove")) }],
          L.items, { empty: emptyState("No verified members yet", "Members verify with /verify, or post a panel with /verification panel.") }), pager(L, (p) => { q.page = p; load(); })); };
      clear(body, h("div", { class: "filters" }, h("input", { type: "search", placeholder: "Search Roblox name or user ID", oninput: debounce((e) => { q.q = e.target.value; q.page = 1; load(); }, 300) }),
        h("button", { class: "btn", onclick: () => manualLink() }, "Link manually")), out);
      load();
    } else if (tab === "pending") {
      const v = await api("/verification");
      clear(body, table([{ label: "Member", render: (r) => who(r.discord_id, r.username) }, { label: "Roblox", key: "roblox_username" }, { label: "Method", key: "method" },
        { label: "Checks", key: "attempts" }, { label: "Started", render: (r) => when(r.created_at) }, { label: "Expires", render: (r) => when(r.expires_at) }], v.pending,
        { empty: emptyState("No verifications in progress", null) }));
    } else if (tab === "history") {
      const hst = await api("/verification/history");
      clear(body, table([{ label: "When", render: (r) => when(r.at) }, { label: "Action", key: "action" }, { label: "Member", render: (r) => who(r.discord_id) },
        { label: "Roblox", key: "roblox_username" }, { label: "Method", key: "method" }, { label: "Detail", key: "detail" }], hst.items));
    } else {
      if (!can("admin.config")) { clear(body, emptyState("Administrators change these settings", null)); return; }
      const v2 = await api("/verification");
      const keyRow = (prov, label, where) => {
        const set = v2.external[prov];
        const inp = h("input", { type: "password", autocomplete: "off", placeholder: set ? "Key saved — paste a new one to replace it" : `Paste your ${label} server API key` });
        return h("div", { style: "margin-bottom:16px" }, h("div", { class: "row", style: "margin-bottom:6px" }, h("b", null, label), set ? pill("Key saved", "ok") : pill("Not set", "off")),
          h("p", { class: "muted small", style: "margin:0 0 6px" }, where),
          h("div", { class: "row" }, h("div", { class: "fill" }, inp),
            h("button", { class: "btn primary", onclick: async () => { if (!inp.value.trim()) return toast("Paste the key first.", true);
              await act(() => api(`/verification/keys/${prov}`, { method: "POST", body: { key: inp.value.trim() } }), `${label} key saved.`); draw(); } }, "Save"),
            set ? h("button", { class: "btn", onclick: async () => { const r = await act(() => api(`/verification/keys/${prov}/test`, { method: "POST" })); toast(r.message); } }, "Test") : null,
            set ? h("button", { class: "btn danger", onclick: async () => { if (!(await confirmDialog(`Remove the ${label} key?`, "Members will no longer be verified through it.", "Remove"))) return;
              await act(() => api(`/verification/keys/${prov}`, { method: "DELETE" }), "Key removed."); draw(); } }, "Remove") : null));
      };
      const oauthNote = !d.oauth.configured ? "Sign in with Roblox needs a free Roblox OAuth app: add ROBLOX_CLIENT_ID and ROBLOX_CLIENT_SECRET to .env (docs/ROBLOX_VERIFICATION.md)."
        : !d.oauth.public_url ? "Sign in with Roblox is configured but needs DASHBOARD_PUBLIC_URL." : null;
      clear(body, h("div", { class: "panels" },
        h("div", { class: "span-6" }, panel("Instant verification (Bloxlink / RoVer)", h("div", null,
          h("p", { class: "muted" }, "Members already verified with Bloxlink or RoVer are linked instantly when they press Verify, or as soon as they join. Keys are stored encrypted and can't be viewed again."),
          keyRow("bloxlink", "Bloxlink", "blox.link → Dashboard → your server → API Keys → create a server key."),
          keyRow("rover", "RoVer", "rover.link → Dashboard → your server → API → create an API key.")))),
        h("div", { class: "span-6" }, settingsPanel("Verification settings", [
          { key: "enabled", label: "Verification enabled", type: "check" },
          { key: "use_bloxlink", label: "Check Bloxlink first", type: "check" }, { key: "use_rover", label: "Check RoVer first", type: "check" },
          { key: "auto_verify_on_join", label: "Verify new members automatically if Bloxlink/RoVer knows them", type: "check" },
          { key: "allow_oauth", label: "Allow 'Sign in with Roblox'", type: "check", help: "Official Roblox login page — the main method." },
          { key: "allow_profile_code", label: "Allow profile-code verification (fallback)", type: "check", help: "Member puts a short code in their Roblox About section. Turn off to hide it." },
          { key: "verified_role_id", label: "Role to give after verifying", type: "role" }, { key: "unverified_role_id", label: "Role to remove after verifying", type: "role" },
          { key: "nickname_format", label: "Nickname after verifying", type: "select", options: d.nickname_formats },
          { key: "min_account_age_days", label: "Minimum Roblox account age (days)", type: "number", min: 0, max: 3650, help: "0 = no minimum." },
          { key: "block_banned_accounts", label: "Refuse banned Roblox accounts", type: "check" }, { key: "dm_on_verify", label: "DM members when they're verified", type: "check" }],
          v2.settings, (v) => api("/verification/settings", { method: "PUT", body: v }), { note: oauthNote }))));
    }
  };
  const tabRow = h("div");
  const drawTabs = () => clear(tabRow, tabs([["links", "Verified members"], ["pending", "In progress"], ["history", "History"], ["settings", "Settings"]], tab, (k) => { tab = k; drawTabs(); draw(); }));
  drawTabs();
  clear(el, head("Roblox verification", "Link Discord accounts to Roblox accounts. An account is only linked after verification really succeeds."),
    h("div", { class: "band" }, [["Linked", d.stats.linked], ["Verified today", d.stats.verified], ["Removed today", d.stats.removed], ["In progress", d.pending.length]].map(([k, v]) =>
      h("div", null, h("div", { class: "v" }, fmtNum(v)), h("div", { class: "k" }, k)))), tabRow, body);
  draw();
  S.refresh = () => draw();
});

// ---------------------------------------------------------------------------- applications
const APP_STATUS = { submitted: "Submitted", under_review: "Under review", awaiting_response: "Awaiting response", approved: "Approved", denied: "Denied", archived: "Archived" };
const appPill = (s) => pill(APP_STATUS[s] || s, { approved: "ok", denied: "bad", awaiting_response: "warn", archived: "off" }[s] || "info");
async function applicationModal(n) {
  const a = await act(() => api(`/applications/${n}`));
  const body = h("div", null, h("dl", { class: "facts" }, h("dt", null, "Applicant"), h("dd", null, who(a.user_id, a.display_name || a.username, a.avatar_url)),
    h("dt", null, "Status"), h("dd", null, appPill(a.status)), h("dt", null, "Submitted"), h("dd", null, when(a.created_at)),
    a.decision_reason ? [h("dt", null, "Reviewer note"), h("dd", null, a.decision_reason)] : null));
  for (const x of a.answers) body.append(h("div", { style: "margin-top:12px" }, h("h3", null, x.question), h("p", { style: "margin:4px 0;white-space:pre-wrap" }, x.answer || "—")));
  const note = h("input", { type: "text", maxlength: 1000, placeholder: "Internal note (applicant can't see it)" });
  body.append(h("h3", { style: "margin-top:16px" }, "Reviewer notes"), h("ul", { class: "list" }, a.notes.map((x) => h("li", null, h("span", null, x.note), h("span", { class: "when" }, when(x.created_at))))),
    h("div", { class: "ask" }, note, h("button", { class: "btn", onclick: async () => { if (!note.value.trim()) return; await act(() => api(`/applications/${n}/note`, { method: "POST", body: { note: note.value } }), "Note added."); mo.close(); applicationModal(n); } }, "Add note")));
  const decide = (status, label, needReason) => ({ label, primary: status === "approved", danger: status === "denied", onClick: async () => {
    const f = form([{ key: "reason", label: needReason ? "What do you need from the applicant?" : "Message to the applicant (optional)", type: "textarea" }]);
    modal({ title: `${label} · application #${n}`, body: f.el, actions: [{ label: "Cancel" }, { label, primary: true, onClick: async () => {
      const v = f.get(); if (needReason && !v.reason.trim()) throw new Error("Write what you need.");
      const r = await act(() => api(`/applications/${n}/status`, { method: "POST", body: { status, reason: v.reason } }), `Application ${APP_STATUS[status].toLowerCase()}.`);
      if (r.problems?.length) toast(r.problems.join(" "), true);
      mo.close(); S.refresh && S.refresh(); } }] });
    return true; } });
  const open = ["submitted", "under_review", "awaiting_response"].includes(a.status);
  const actions = open ? [decide("awaiting_response", "Request info", true), decide("denied", "Deny"), decide("approved", "Approve")]
    : a.status !== "archived" ? [{ label: "Archive", onClick: async () => { await act(() => api(`/applications/${n}/status`, { method: "POST", body: { status: "archived" } }), "Archived."); S.refresh && S.refresh(); } }] : [];
  const mo = modal({ title: `Application #${n} — ${a.form_name}`, body, wide: true, actions: [{ label: "Close" }, ...actions] });
}
function formBuilder(meta, existing) {
  const f0 = existing || { name: "", kind: "custom", description: "", enabled: false, cooldown_hours: 24, require_verified: false, questions: [{ label: "", type: "short", required: true, options: [] }], submit_roles: [], reviewer_roles: [] };
  let qs = f0.questions.map((q) => ({ ...q }));
  const top = form([{ key: "name", label: "Form name", max: 60 }, { key: "kind", label: "Type", type: "select", options: meta.kinds },
    { key: "description", label: "Description shown to applicants", type: "textarea" },
    { key: "enabled", label: "Open for applications", type: "check" }, { key: "require_verified", label: "Require Roblox verification", type: "check" },
    { key: "cooldown_hours", label: "Hours between applications", type: "number", min: 0, max: 720 },
    { key: "approve_role_id", label: "Role given when approved", type: "role" }, { key: "review_channel_id", label: "Post new submissions in", type: "channel" },
    { key: "submit_roles", label: "Only these roles can apply (none selected = everyone)", type: "roles" },
    { key: "reviewer_roles", label: "Extra reviewer roles", type: "roles", help: "People with the 'Review applications' permission can always review." }], f0);
  const qbox = h("div");
  const drawQ = () => clear(qbox, qs.map((q, i) => {
    const label = h("input", { type: "text", value: q.label, maxlength: 300, placeholder: `Question ${i + 1}`, oninput: (e) => { q.label = e.target.value; } });
    const type = sel(meta.question_types, q.type); type.addEventListener("change", () => { q.type = type.value; drawQ(); });
    const req = h("input", { type: "checkbox", checked: q.required, onchange: (e) => { q.required = e.target.checked; } });
    const opts = ["choice", "dropdown"].includes(q.type) ? h("textarea", { placeholder: "One option per line", oninput: (e) => { q.options = e.target.value.split("\n").map((s) => s.trim()).filter(Boolean); } }, (q.options || []).join("\n")) : null;
    return h("div", { class: "panel", style: "margin-bottom:10px;padding:12px" }, h("div", { class: "row" }, h("b", { class: "num" }, `${i + 1}.`), h("div", { class: "fill" }, label), type,
      h("label", { class: "check", style: "margin:0" }, req, "Required"),
      h("button", { class: "btn sm", "aria-label": "Move up", disabled: i === 0, onclick: () => { [qs[i - 1], qs[i]] = [qs[i], qs[i - 1]]; drawQ(); } }, "↑"),
      h("button", { class: "btn sm", "aria-label": "Move down", disabled: i === qs.length - 1, onclick: () => { [qs[i + 1], qs[i]] = [qs[i], qs[i + 1]]; drawQ(); } }, "↓"),
      h("button", { class: "btn sm danger", "aria-label": "Remove question", onclick: () => { qs.splice(i, 1); drawQ(); } }, "✕")), opts ? h("div", { style: "margin-top:8px" }, opts) : null);
  }), h("button", { class: "btn sm", disabled: qs.length >= 25, onclick: () => { qs.push({ label: "", type: "short", required: true, options: [] }); drawQ(); } }, "Add question"));
  drawQ();
  modal({ title: existing ? `Edit ${existing.name}` : "New application form", wide: true, body: h("div", null, top.el, h("h3", null, "Questions"), qbox),
    actions: [{ label: "Cancel" }, { label: "Save form", primary: true, onClick: async () => {
      const body = { ...top.get(), questions: qs };
      await act(() => api(existing ? `/applications/forms/${existing.id}` : "/applications/forms", { method: existing ? "PUT" : "POST", body }), "Form saved.");
      S.refresh && S.refresh(); } }] });
}
page("applications", "Applications", async (el, args) => {
  if (args[0] && /^\d+$/.test(args[0])) { setTimeout(() => applicationModal(args[0]), 0); }
  let tab = args[0] === "forms" ? "forms" : "subs";
  const meta = await api("/applications/meta");
  const body = h("div");
  const draw = async () => {
    clear(body, loading());
    if (tab === "subs") {
      const q = { status: "open", form: "", q: "", page: 1 };
      const forms = (await api("/applications/forms")).forms;
      const out = h("div");
      const load = async () => { const d = await api(`/applications?${new URLSearchParams(q)}`);
        clear(out, table([{ label: "#", render: (a) => h("b", { class: "num" }, a.number) }, { label: "Form", key: "form_name" }, { label: "Applicant", render: (a) => who(a.user_id, a.display_name || a.username) },
          { label: "Status", render: (a) => appPill(a.status) }, { label: "Submitted", render: (a) => when(a.created_at) }], d.items,
          { onRow: (a) => applicationModal(a.number), empty: emptyState("No applications here", forms.length ? "Members apply with /apply in Discord." : "Create a form first.") }), pager(d, (p) => { q.page = p; load(); })); };
      const st = sel({ open: "Needs review", ...APP_STATUS, all: "All" }, "open"); st.addEventListener("change", () => { q.status = st.value; q.page = 1; load(); });
      const fm = sel({ "": "All forms", ...Object.fromEntries(forms.map((f) => [f.id, f.name])) }, ""); fm.addEventListener("change", () => { q.form = fm.value; q.page = 1; load(); });
      clear(body, h("div", { class: "filters" }, h("input", { type: "search", placeholder: "Applicant, form or #number", oninput: debounce((e) => { q.q = e.target.value; q.page = 1; load(); }, 300) }), st, fm), out);
      load();
    } else if (tab === "forms") {
      const forms = (await api("/applications/forms")).forms;
      const preset = sel(Object.fromEntries(meta.presets.map((p) => [p, p])), meta.presets[0]);
      clear(body, can("application.manage") ? h("div", { class: "filters" }, h("button", { class: "btn primary", onclick: () => formBuilder(meta) }, "New form"), preset,
        h("button", { class: "btn", onclick: async () => { await act(() => api("/applications/forms/preset", { method: "POST", body: { preset: preset.value } }), "Form created — it starts closed so you can review it."); draw(); } }, "Create from template")) : null,
        table([{ label: "Form", render: (f) => h("b", null, f.name) }, { label: "Type", render: (f) => meta.kinds[f.kind] }, { label: "Questions", render: (f) => f.questions.length },
          { label: "Open", render: (f) => yes(f.enabled, "Open", "Closed") }, { label: "", cls: "actions", render: (f) => can("application.manage") ? h("span", null,
            h("button", { class: "btn sm", onclick: () => formBuilder(meta, f) }, "Edit"), " ",
            h("button", { class: "btn sm", onclick: async () => { await act(() => api(`/applications/forms/${f.id}`, { method: "PUT", body: { ...f, enabled: !f.enabled } }), f.enabled ? "Form closed." : "Form opened."); draw(); } }, f.enabled ? "Close" : "Open"), " ",
            h("button", { class: "btn sm danger", onclick: async () => { if (!(await confirmDialog(`Delete ${f.name}?`, "Submitted applications are kept.", "Delete form"))) return; await act(() => api(`/applications/forms/${f.id}`, { method: "DELETE" }), "Form deleted."); draw(); } }, "Delete")) : null }],
          forms, { empty: emptyState("No forms yet", "Start from a template (Staff, Police, Fire, EMS, Dispatch, Civilian) or build your own.") }));
    } else {
      clear(body, settingsPanel("Application settings", [{ key: "dm_results", label: "DM applicants when their application is decided", type: "check" },
        { key: "archive_after_days", label: "Archive decided applications after (days)", type: "number", min: 0, max: 365, help: "0 = never archive automatically." }],
        meta.settings, (v) => api("/applications/settings", { method: "PUT", body: v })));
    }
  };
  const tabRow = h("div");
  const items = [["subs", "Submissions"], ["forms", "Forms"]].concat(can("application.manage") ? [["settings", "Settings"]] : []);
  const drawTabs = () => clear(tabRow, tabs(items, tab, (k) => { tab = k; drawTabs(); draw(); }));
  drawTabs();
  clear(el, head("Applications", "Review submissions and build forms. Members apply with /apply in Discord."), tabRow, body);
  draw();
  S.refresh = () => draw();
});

// ---------------------------------------------------------------------------- sessions
page("sessions", "Sessions", async (el) => {
  const d = await api("/sessions?status=all");
  const create = () => {
    const f = form([{ key: "name", label: "Session name", max: 80 }, { key: "description", label: "Description", type: "textarea" },
      { key: "scheduled_for", label: "Start time (your local time)", type: "datetime", help: "Leave empty for an unscheduled session." },
      { key: "host_id", label: "Host", type: "member" }, { key: "department", label: "Department (optional)", max: 40 },
      { key: "reminder_minutes", label: "Reminder (minutes before)", type: "number", min: 0, max: 1440, default: d.settings.default_reminder_minutes },
      { key: "channel_id", label: "Announce in (empty = session channel)", type: "channel" }, { key: "announce", label: "Announce in Discord", type: "check", default: true }]);
    modal({ title: "New session", body: f.el, actions: [{ label: "Cancel" }, { label: "Create session", primary: true, onClick: async () => {
      await act(() => api("/sessions", { method: "POST", body: f.get() }), (s) => `Session #${s.number} created.`); renderPage("sessions"); } }] });
  };
  const STA = { scheduled: "info", active: "ok", ended: "off", cancelled: "off" };
  clear(el, head("Sessions", "Roleplay sessions, trainings and events with announcements and reminders.",
    can("session.manage") ? h("button", { class: "btn primary", onclick: create }, "New session") : null),
    h("div", { class: "panels" }, h("div", { class: can("session.manage") ? "span-8" : "span-12" }, panel("All sessions", table([
      { label: "#", render: (s) => h("b", { class: "num" }, s.number) }, { label: "Name", key: "name" }, { label: "Status", render: (s) => pill(s.status, STA[s.status]) },
      { label: "Start", render: (s) => when(s.started_at || s.scheduled_for) }, { label: "Host", render: (s) => s.host_id ? who(s.host_id) : null }, { label: "Joined", key: "participant_count" },
      { label: "", cls: "actions", render: (s) => !can("session.manage") ? null : s.status === "scheduled"
        ? h("span", null, h("button", { class: "btn sm primary", onclick: async () => { await act(() => api(`/sessions/${s.number}/start`, { method: "POST" }), "Session started."); renderPage("sessions"); } }, "Start"), " ",
          h("button", { class: "btn sm", onclick: async () => { if (!(await confirmDialog("Cancel session?", s.name, "Cancel session"))) return; await act(() => api(`/sessions/${s.number}/end`, { method: "POST", body: {} }), "Session cancelled."); renderPage("sessions"); } }, "Cancel"))
        : s.status === "active" ? h("button", { class: "btn sm", onclick: async () => { await act(() => api(`/sessions/${s.number}/end`, { method: "POST", body: {} }), "Session ended."); renderPage("sessions"); } }, "End") : null }],
      d.sessions, { empty: emptyState("No sessions yet", "Schedule one and Hive announces it, reminds participants and tracks who joined.") }))),
      can("session.manage") ? h("div", { class: "span-4" }, settingsPanel("Session settings", [{ key: "announce_channel_id", label: "Announcement channel", type: "channel" },
        { key: "ping_role_id", label: "Role to ping on start and reminders", type: "role" }, { key: "default_reminder_minutes", label: "Default reminder (minutes)", type: "number", min: 0, max: 1440 }],
        d.settings, (v) => api("/sessions/settings", { method: "PUT", body: v }))) : null));
  S.refresh = () => renderPage("sessions");
});

/* Pulse Dispatch dashboard — staff, moderation, automation, logging, bot, AI and settings pages. */
"use strict";

// ============================================================================ STAFF
async function staffModal(uid) {
  const p = await act(() => api(`/staff/${uid}`));
  const st = p.staff, sh = p.shifts, body = h("div");
  body.append(h("dl", { class: "facts" }, h("dt", null, "Member"), h("dd", null, who(uid, p.member?.display_name || p.member?.username, p.member?.avatar_url)),
    h("dt", null, "Rank"), h("dd", null, st.rank), h("dt", null, "Department"), h("dd", null, st.department || "—"), h("dt", null, "Status"), h("dd", null, pill(st.status, st.status === "active" ? "ok" : st.status === "leave" ? "warn" : "off")),
    h("dt", null, "Appointed"), h("dd", null, when(st.appointed_at)), h("dt", null, "Shifts"), h("dd", null, `This week ${sh.week_text} · all time ${sh.total_text} (${sh.count})`)));
  body.append(h("div", { class: "panels", style: "margin-top:14px" },
    h("div", { class: "span-6" }, h("h3", null, "History"), h("ul", { class: "list" }, p.history.map((x) => h("li", null, h("b", null, x.action), x.to_rank ? `${x.from_rank ? x.from_rank + " → " : ""}${x.to_rank}` : "", x.reason ? h("span", { class: "muted small" }, x.reason) : null, h("span", { class: "when" }, when(x.at)))))),
    h("div", { class: "span-6" }, h("h3", null, "Recent shifts"), p.recent_shifts.length ? h("ul", { class: "list" }, p.recent_shifts.map((x) => h("li", null, x.shift_type, h("span", { class: "muted" }, dur(x.duration_s)), h("span", { class: "when" }, when(x.started_at))))) : h("p", { class: "muted" }, "No shifts yet."),
      p.moderation_actions.length ? [h("h3", { style: "margin-top:12px" }, "Moderation actions"), h("ul", { class: "list" }, p.moderation_actions.map((c) => h("li", null, link(`moderation/discord/${c.number}`, `#${c.number} ${c.action}`), h("span", { class: "when" }, when(c.created_at)))))] : null)));
  if (p.notes) {
    const note = h("input", { type: "text", maxlength: 1000, placeholder: "Confidential note (management only)" });
    body.append(h("h3", { style: "margin-top:14px" }, "Confidential notes"), h("ul", { class: "list" }, p.notes.map((n) => h("li", null, n.note, h("span", { class: "when" }, when(n.created_at))))),
      h("div", { class: "ask" }, note, h("button", { class: "btn", onclick: async () => { if (!note.value.trim()) return; await act(() => api(`/staff/${uid}/notes`, { method: "POST", body: { note: note.value } }), "Note added."); mo.close(); staffModal(uid); } }, "Add note")));
  }
  const actions = [{ label: "Close" }];
  if (can("staff.manage") && st.status !== "removed") {
    const rank = (direction) => async () => {
      const r = await act(() => api(`/staff/${uid}/rank`, { method: "POST", body: { direction } }), (x) => `Now ${x.staff.rank}.`);
      if (r.problems?.length) toast(r.problems.join(" "), true);
      mo.close(); staffModal(uid); S.refresh && S.refresh();
    };
    actions.unshift({ label: "Remove from staff", danger: true, onClick: async () => {
      if (!(await confirmDialog("Remove from staff?", "Their rank role is removed and any running shift ends.", "Remove"))) return true;
      await act(() => api(`/staff/${uid}`, { method: "DELETE" }), "Removed from staff."); S.refresh && S.refresh(); } },
      { label: "Change rank…", onClick: () => { const f = form([{ key: "rank", label: "New rank", max: 40 }, { key: "reason", label: "Reason", max: 300 }]);
        modal({ title: "Change rank", body: f.el, actions: [{ label: "Cancel" }, { label: "Save", primary: true, onClick: async () => {
          await act(() => api(`/staff/${uid}/rank`, { method: "POST", body: f.get() }), "Rank changed."); mo.close(); staffModal(uid); S.refresh && S.refresh(); } }] }); return true; } },
      { label: "Demote", onClick: rank("down") }, { label: "Promote", primary: true, onClick: rank("up") });
  }
  const mo = modal({ title: "Staff profile", body, wide: true, actions });
}
page("staff", "Staff directory", async (el, args) => {
  if (args[0] && /^\d+$/.test(args[0])) setTimeout(() => staffModal(args[0]), 0);
  const q = { status: "current", q: "", department: "", page: 1 };
  const out = h("div");
  let settings = null;
  const load = async () => {
    const d = await api(`/staff?${new URLSearchParams(q)}`);
    settings = d.settings;
    clear(out, table([{ label: "Member", render: (s) => who(s.user_id, s.display_name || s.username, s.avatar_url) }, { label: "Rank", render: (s) => h("b", null, s.rank) },
      { label: "Department", key: "department" }, { label: "Status", render: (s) => s.on_shift ? pill("On shift", "ok") : pill(s.status, s.status === "leave" ? "warn" : s.status === "removed" ? "off" : "plain") },
      { label: "Last shift", render: (s) => when(s.last_shift) }], d.items, { onRow: (s) => staffModal(s.user_id), empty: emptyState("No staff members", "Add your team with the button above or /staff add.") }),
      pager(d, (p) => { q.page = p; load(); }));
  };
  const add = () => {
    const ranks = Object.fromEntries((settings?.ranks || []).map((r) => [r.name, r.name]));
    const f = form([{ key: "user_id", label: "Member", type: "member" }, Object.keys(ranks).length ? { key: "rank", label: "Rank", type: "select", options: ranks } : { key: "rank", label: "Rank", max: 40, placeholder: "Moderator" },
      { key: "department", label: "Department", max: 40 }, { key: "reason", label: "Note (optional)", max: 300 }]);
    modal({ title: "Add staff member", body: f.el, actions: [{ label: "Cancel" }, { label: "Add to staff", primary: true, onClick: async () => {
      const v = f.get(); if (!v.user_id) throw new Error("Choose a member.");
      const r = await act(() => api("/staff", { method: "POST", body: v }), "Added to staff."); if (r.problems?.length) toast(r.problems.join(" "), true); load(); } }] });
  };
  const st = sel({ current: "Current staff", active: "Active", leave: "On leave", removed: "Former staff", all: "Everyone" }, "current");
  st.addEventListener("change", () => { q.status = st.value; q.page = 1; load(); });
  clear(el, head("Staff directory", "Your team, their ranks and activity.", can("staff.manage") ? h("button", { class: "btn primary", onclick: add }, "Add staff member") : null),
    h("div", { class: "filters" }, h("input", { type: "search", placeholder: "Name, rank or user ID", oninput: debounce((e) => { q.q = e.target.value; q.page = 1; load(); }, 300) }), st), out);
  await load();
  S.refresh = load;
});

// Shifts and leave (LOA) pages live in pages-v5.js (v5.0).
page("staff/settings", "Staff settings", async (el) => {
  const d = await api("/staff/settings");
  let ranks = d.staff.ranks.map((r) => ({ ...r }));
  const rbox = h("div");
  const drawR = () => clear(rbox, h("p", { class: "muted small" }, "Lowest rank first. Promote/Demote moves one step and swaps the rank roles."), ranks.map((r, i) => {
    const role = roleSelect(r.role_id); role.addEventListener("change", () => { r.role_id = role.value || null; });
    return h("div", { class: "row", style: "margin-bottom:8px" }, h("b", { class: "num" }, `${i + 1}.`), h("div", { class: "fill" }, h("input", { type: "text", value: r.name, maxlength: 40, oninput: (e) => { r.name = e.target.value; } })), h("div", { class: "fill" }, role),
      h("button", { class: "btn sm", "aria-label": "Move up", disabled: i === 0, onclick: () => { [ranks[i - 1], ranks[i]] = [ranks[i], ranks[i - 1]]; drawR(); } }, "↑"),
      h("button", { class: "btn sm danger", "aria-label": "Remove rank", onclick: () => { ranks.splice(i, 1); drawR(); } }, "✕"));
  }), h("button", { class: "btn sm", onclick: () => { ranks.push({ name: "", role_id: null }); drawR(); } }, "Add rank"));
  drawR();
  const rest = form([{ key: "departments", label: "Staff departments (one per line)", type: "lines" }, { key: "inactivity_days", label: "Flag staff inactive after (days without a shift)", type: "number", min: 0, max: 365, help: "0 = off." },
    { key: "leave_role_id", label: "Role while on leave", type: "role" }], d.staff);
  clear(el, head("Staff settings", "Rank ladder, departments, inactivity and shifts."), h("div", { class: "panels" },
    h("div", { class: "span-7" }, panel("Ranks & staff", h("div", null, rbox, h("hr", { style: "border-color:var(--line);margin:16px 0" }), rest.el,
      h("button", { class: "btn primary", onclick: () => act(() => api("/staff/settings", { method: "PUT", body: { staff: { ...rest.get(), ranks: ranks.filter((r) => r.name.trim()) } } }), "Saved.") }, "Save changes")))),
    h("div", { class: "span-5" }, settingsPanel("Shifts", [{ key: "types", label: "Shift types (one per line)", type: "lines" }, { key: "on_duty_role_id", label: "Role while on shift", type: "role" },
      { key: "max_hours", label: "End shifts automatically after (hours)", type: "number", min: 1, max: 48 }], d.shifts, (v) => api("/staff/settings", { method: "PUT", body: { shifts: v } })))));
});

// ============================================================================ MODERATION
const MOD_ACTIONS = { warn: "Warning", timeout: "Timeout", kick: "Kick", ban: "Ban", unban: "Unban", note: "Note" };
function caseCreate(uid) {
  const allowed = Object.fromEntries(Object.entries(MOD_ACTIONS).filter(([k]) => can({ warn: "mod.warn", note: "mod.warn", timeout: "mod.timeout", kick: "mod.kick", ban: "mod.ban", unban: "mod.ban" }[k])));
  let action = "warn";
  const extra = h("div");
  const durIn = h("input", { type: "text", placeholder: "10m, 2h, 1d (max 28d)" }), delIn = h("input", { type: "number", min: 0, max: 7, value: 0 });
  const drawExtra = () => clear(extra, action === "timeout" ? h("label", { class: "field" }, h("span", null, "Duration"), durIn) : null,
    action === "ban" ? h("label", { class: "field" }, h("span", null, "Delete their messages from the last N days"), delIn) : null);
  const act_ = sel(allowed, action); act_.addEventListener("change", () => { action = act_.value; drawExtra(); });
  const f = form([{ key: "user_id", label: action === "unban" ? "User ID" : "Member", type: "member" }, { key: "reason", label: "Reason", type: "textarea", max: 500 }, { key: "evidence", label: "Evidence (links, message IDs)", max: 500 }], { user_id: uid || "" });
  drawExtra();
  modal({ title: "New moderation case", body: h("div", null, h("label", { class: "field" }, h("span", null, "Action"), act_), f.el, extra), actions: [{ label: "Cancel" }, { label: "Create case", primary: true, onClick: async () => {
    const v = f.get(); if (!v.user_id) throw new Error("Choose a member or paste their user ID.");
    const body = { ...v, action, duration: durIn.value, delete_message_days: Number(delIn.value || 0) };
    if (action === "kick" || action === "ban") {
      if (!(await confirmDialog(`${MOD_ACTIONS[action]} this member?`, `This ${action}s user ${v.user_id} from the server. Reason: ${v.reason}`, MOD_ACTIONS[action]))) return true;
      body.confirm = true;
    }
    const c = await act(() => api("/moderation", { method: "POST", body }), (x) => `Case #${x.number} created.`);
    if (c.escalation && !c.escalation.error) toast(`Automatic ${c.escalation.action} applied (case #${c.escalation.number}).`);
    S.refresh && S.refresh(); } }] });
}
async function caseModal(n) {
  const c = await act(() => api(`/moderation/${n}`));
  const reason = h("textarea", null, c.reason || ""), evidence = h("input", { type: "text", value: c.evidence || "" });
  const actions = [{ label: "Close" }];
  if (can("mod.warn")) actions.unshift({ label: "Save changes", primary: true, onClick: async () => { await act(() => api(`/moderation/${n}`, { method: "PATCH", body: { reason: reason.value, evidence: evidence.value } }), "Case updated."); S.refresh && S.refresh(); } });
  if (can("mod.timeout") && c.status === "active" && c.action !== "ban" && c.action !== "unban") actions.unshift({ label: "Revoke", danger: true, onClick: async () => {
    if (!(await confirmDialog(`Revoke case #${n}?`, c.action === "timeout" ? "The timeout is lifted." : "It no longer counts as active.", "Revoke"))) return true;
    await act(() => api(`/moderation/${n}/revoke`, { method: "POST", body: {} }), "Case revoked."); S.refresh && S.refresh(); } });
  modal({ title: `Case #${n} — ${MOD_ACTIONS[c.action]}`, body: h("div", null, h("dl", { class: "facts" }, h("dt", null, "Member"), h("dd", null, who(c.target_id, c.target_name)),
    h("dt", null, "Moderator"), h("dd", null, who(c.moderator_id)), h("dt", null, "Status"), h("dd", null, pill(c.status, c.status === "active" ? "ok" : c.status === "failed" ? "bad" : "off")),
    c.duration_s ? [h("dt", null, "Duration"), h("dd", null, dur(c.duration_s))] : null, h("dt", null, "Created"), h("dd", null, when(c.created_at)), h("dt", null, "Source"), h("dd", null, c.source),
    h("dt", null, "Outcome"), h("dd", null, c.outcome || "—")), h("label", { class: "field", style: "margin-top:14px" }, h("span", null, "Reason"), reason), h("label", { class: "field" }, h("span", null, "Evidence"), evidence)), actions });
}
page("moderation/discord", "Discord cases", async (el, args) => {
  if (args[0] && /^\d+$/.test(args[0])) setTimeout(() => caseModal(args[0]), 0);
  const q = { q: "", action: "", status: "", page: 1 };
  const out = h("div");
  const load = async () => {
    const d = await api(`/moderation?${new URLSearchParams(q)}`);
    clear(out, table([{ label: "Case", render: (c) => h("b", { class: "num" }, `#${c.number}`) }, { label: "Action", render: (c) => MOD_ACTIONS[c.action] },
      { label: "Member", render: (c) => who(c.target_id, c.target_name) }, { label: "Reason", key: "reason" }, { label: "Moderator", render: (c) => who(c.moderator_id) },
      { label: "Status", render: (c) => pill(c.status, c.status === "active" ? "ok" : c.status === "failed" ? "bad" : "off") }, { label: "When", render: (c) => when(c.created_at) }],
      d.items, { onRow: (c) => caseModal(c.number), empty: emptyState("No cases match", "Cases are created by /warn, /timeout, /kick, /ban and here.") }), pager(d, (p) => { q.page = p; load(); }));
  };
  const f = (key, opts) => { const x = sel(opts, ""); x.addEventListener("change", () => { q[key] = x.value; q.page = 1; load(); }); return x; };
  clear(el, head("Discord cases", "Every Discord warning, timeout, kick and ban with its outcome.", can("mod.warn") ? h("button", { class: "btn primary", onclick: () => caseCreate() }, "New case") : null),
    h("div", { class: "filters" }, h("input", { type: "search", placeholder: "Reason, member, user ID or #case", oninput: debounce((e) => { q.q = e.target.value; q.page = 1; load(); }, 300) }),
      f("action", { "": "Any action", ...MOD_ACTIONS }), f("status", { "": "Any status", active: "Active", revoked: "Revoked", failed: "Failed" })), out);
  await load();
  S.refresh = load;
});
page("moderation/reports", "Member reports", async (el) => {
  let status = "open";
  const out = h("div");
  const load = async () => {
    const d = await api(`/moderation/reports?status=${status}`);
    const resolve = (r, st) => async () => {
      const f = form([{ key: "resolution", label: "What was done?", type: "textarea" }, { key: "case_number", label: "Related case number (optional)", type: "number" }]);
      modal({ title: `${st === "actioned" ? "Action" : "Dismiss"} report #${r.number}`, body: f.el, actions: [{ label: "Cancel" }, { label: "Save", primary: true, onClick: async () => {
        await act(() => api(`/moderation/reports/${r.number}`, { method: "POST", body: { status: st, ...f.get() } }), "Report updated. The reporter was told."); load(); } }] });
    };
    clear(out, table([{ label: "#", render: (r) => h("b", { class: "num" }, r.number) }, { label: "Reported", render: (r) => who(r.target_id) }, { label: "By", render: (r) => who(r.reporter_id) },
      { label: "Reason", key: "reason" }, { label: "Evidence", key: "evidence" }, { label: "When", render: (r) => when(r.created_at) },
      { label: "", cls: "actions", render: (r) => r.status === "open" ? h("span", null, h("button", { class: "btn sm", onclick: resolve(r, "dismissed") }, "Dismiss"), " ",
        h("button", { class: "btn sm primary", onclick: resolve(r, "actioned") }, "Mark actioned")) : pill(r.status, "plain") }], d.items,
      { empty: emptyState(status === "open" ? "No open reports" : "No reports", "Members report others privately with /report-member.") }), pager(d, () => {}));
  };
  const s = sel({ open: "Open", actioned: "Actioned", dismissed: "Dismissed", "": "All" }, status);
  s.addEventListener("change", () => { status = s.value; load(); });
  clear(el, head("Member reports", "Private reports from members."), h("div", { class: "filters" }, s), out);
  await load();
  S.refresh = load;
});
page("moderation/settings", "Moderation settings", async (el) => {
  const s = await api("/moderation/settings");
  clear(el, head("Moderation settings", null), settingsPanel("Moderation", [
    { key: "dm_on_action", label: "DM members when they're warned, timed out, kicked or banned", type: "check" },
    { key: "warn_threshold", label: "Automatic action after this many active warnings", type: "number", min: 0, max: 50, help: "0 = off. Repeats every N warnings." },
    { key: "warn_threshold_action", label: "Automatic action", type: "select", options: { timeout: "Timeout", kick: "Kick", ban: "Ban" } },
    { key: "warn_threshold_duration", label: "Timeout length", placeholder: "1h" }, { key: "appeal_info", label: "Appeal information (added to kick and ban DMs)", type: "textarea", max: 300 }],
    s, (v) => api("/moderation/settings", { method: "PUT", body: v })));
});

// ============================================================================ AUTOMATION
function automationEditor(meta, templates, existing, draft) {
  const a = JSON.parse(JSON.stringify(existing || draft || { name: "", description: "", trigger: Object.keys(meta.triggers)[0], conditions: [], actions: [{ type: "send_message" }], enabled: true }));
  const body = h("div");
  const draw = () => {
    const fields = meta.triggers[a.trigger].fields;
    const trig = sel(Object.fromEntries(Object.entries(meta.triggers).map(([k, v]) => [k, v.label])), a.trigger);
    trig.addEventListener("change", () => { a.trigger = trig.value; a.conditions = []; draw(); });
    const conds = a.conditions.map((c, i) => {
      const fs = sel(Object.fromEntries(fields.map((f) => [f, f])), c.field); fs.addEventListener("change", () => { c.field = fs.value; });
      const os = sel(meta.operators, c.op || "eq"); os.addEventListener("change", () => { c.op = os.value; });
      return h("div", { class: "row", style: "margin-bottom:8px" }, fs, os, h("div", { class: "fill" }, h("input", { type: "text", value: c.value || "", oninput: (e) => { c.value = e.target.value; } })),
        h("button", { class: "btn sm danger", "aria-label": "Remove condition", onclick: () => { a.conditions.splice(i, 1); draw(); } }, "✕"));
    });
    const acts = a.actions.map((x, i) => {
      const ts = sel(Object.fromEntries(Object.entries(meta.actions).map(([k, v]) => [k, v.label])), x.type);
      ts.addEventListener("change", () => { a.actions[i] = { type: ts.value }; draw(); });
      const inner = meta.actions[x.type].fields.map((f) => {
        let input;
        if (f === "channel_id") { input = channelSelect(x[f], { label: "Choose a channel" }); input.addEventListener("change", () => { x[f] = input.value; }); }
        else if (f === "role_id") { input = roleSelect(x[f]); input.addEventListener("change", () => { x[f] = input.value; }); }
        else if (f === "template_id") { input = sel({ "": templates.length ? "Choose a template" : "No templates yet — create one in Embed builder", ...Object.fromEntries(templates.map((t) => [t.id, t.name])) }, x[f] || ""); input.addEventListener("change", () => { x[f] = input.value; }); }
        else if (f === "permission") { input = sel({ "": "Everyone with dashboard access", "staff.manage": "Management", "mod.view": "Moderators", "application.review": "Application reviewers", "incident.view": "Dispatch" }, x[f] || ""); input.addEventListener("change", () => { x[f] = input.value; }); }
        else if (f === "content") { input = h("textarea", { oninput: (e) => { x[f] = e.target.value; } }, x[f] || ""); }
        else { input = h("input", { type: "text", value: x[f] || "", oninput: (e) => { x[f] = e.target.value; } }); }
        return h("label", { class: "field" }, h("span", null, { channel_id: "Channel", role_id: "Role", template_id: "Embed template", content: "Message", title: "Title", permission: "Who sees it" }[f]), input);
      });
      return h("div", { class: "panel", style: "margin-bottom:10px;padding:12px" }, h("div", { class: "row", style: "margin-bottom:8px" }, h("b", null, `${i + 1}.`), h("div", { class: "fill" }, ts),
        h("button", { class: "btn sm danger", "aria-label": "Remove action", onclick: () => { a.actions.splice(i, 1); draw(); } }, "✕")), inner);
    });
    clear(body, h("label", { class: "field" }, h("span", null, "Name"), h("input", { type: "text", value: a.name, maxlength: 60, oninput: (e) => { a.name = e.target.value; } })),
      h("label", { class: "field" }, h("span", null, "Description"), h("input", { type: "text", value: a.description || "", maxlength: 300, oninput: (e) => { a.description = e.target.value; } })),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: a.enabled, onchange: (e) => { a.enabled = e.target.checked; } }), "Enabled"),
      h("h3", null, "When"), h("label", { class: "field" }, trig),
      h("h3", null, "Only if (optional)"), conds, h("button", { class: "btn sm", style: "margin-bottom:14px", disabled: !fields.length, onclick: () => { a.conditions.push({ field: fields[0], op: "eq", value: "" }); draw(); } }, "Add condition"),
      h("h3", null, "Then"), acts, h("button", { class: "btn sm", disabled: a.actions.length >= 10, onclick: () => { a.actions.push({ type: "send_message" }); draw(); } }, "Add action"),
      h("p", { class: "muted small", style: "margin-top:12px" }, "Placeholders you can use in messages: ", [...fields, "server", "user", "user_name", "roblox", "date"].map((f) => `{${f}}`).join(" ")));
  };
  draw();
  modal({ title: existing ? `Edit ${existing.name}` : "New automation", wide: true, body, actions: [{ label: "Cancel" }, { label: "Save automation", primary: true, onClick: async () => {
    await act(() => api(existing ? `/automation/${existing.id}` : "/automation", { method: existing ? "PUT" : "POST", body: a }), "Automation saved."); S.refresh && S.refresh(); } }] });
}
page("automation", "Automations", async (el) => {
  const [meta, sch] = await Promise.all([api("/automation"), api("/automation/schedules")]);
  const tpl = sel(Object.fromEntries(Object.keys(meta.templates).map((k) => [k, k])), Object.keys(meta.templates)[0]);
  clear(el, head("Automations", "Do something automatically when something happens. Each event runs an automation once, and loops are blocked.",
    h("button", { class: "btn primary", onclick: () => automationEditor(meta, sch.templates) }, "New automation")),
    h("div", { class: "filters" }, tpl, h("button", { class: "btn", onclick: () => {
      const t = meta.templates[tpl.value];
      automationEditor(meta, sch.templates, null, { name: tpl.value, description: t.description, trigger: t.trigger, conditions: t.conditions || [], actions: t.actions, enabled: false });
    } }, "Start from template")),
    panel("Your automations", table([{ label: "Name", render: (a) => h("b", null, a.name) }, { label: "When", render: (a) => meta.triggers[a.trigger]?.label || a.trigger },
      { label: "Runs", key: "run_count" }, { label: "Last run", render: (a) => when(a.last_run_at) },
      { label: "Last error", render: (a) => a.last_error ? h("span", { class: "small", style: "color:var(--red)" }, a.last_error.slice(0, 120)) : null },
      { label: "Enabled", render: (a) => h("input", { type: "checkbox", checked: !!a.enabled, "aria-label": "Enabled", onchange: (e) => act(() => api(`/automation/${a.id}/toggle`, { method: "POST", body: { enabled: e.target.checked } }), e.target.checked ? "Enabled." : "Disabled.") }) },
      { label: "", cls: "actions", render: (a) => h("span", null, h("button", { class: "btn sm", onclick: () => automationEditor(meta, sch.templates, a) }, "Edit"), " ",
        h("button", { class: "btn sm danger", onclick: async () => { if (!(await confirmDialog(`Delete ${a.name}?`, "Its run history is deleted too.", "Delete"))) return; await act(() => api(`/automation/${a.id}`, { method: "DELETE" }), "Deleted."); renderPage("automation"); } }, "Delete")) }],
      meta.automations, { empty: emptyState("No automations yet", "Start from a template: welcome DMs, staff alerts for new applications, dispatch pings and more.") })),
    panel("Recent runs", table([{ label: "When", render: (r) => when(r.at) }, { label: "Automation", key: "name" }, { label: "Result", render: (r) => r.ok ? pill("OK", "ok") : pill("Failed", "bad") }, { label: "Details", key: "detail" }], meta.runs)));
  S.refresh = () => renderPage("automation");
});
page("automation/schedules", "Scheduled messages", async (el) => {
  const d = await api("/automation/schedules");
  const edit = (s) => {
    const f = form([{ key: "name", label: "Name", max: 60 }, { key: "channel_id", label: "Channel", type: "channel" }, { key: "content", label: "Message", type: "textarea" },
      { key: "template_id", label: "Embed template (optional)", type: "select", options: { "": "None", ...Object.fromEntries(d.templates.map((t) => [t.id, t.name])) } },
      { key: "next_run_at", label: "Send at (your local time)", type: "datetime" }, { key: "interval_minutes", label: "Repeat every (minutes)", type: "number", min: 10, help: "Leave empty to send once." },
      { key: "enabled", label: "Enabled", type: "check", default: true }], s || {});
    modal({ title: s ? `Edit ${s.name}` : "New scheduled message", body: f.el, actions: [{ label: "Cancel" }, { label: "Save", primary: true, onClick: async () => {
      await act(() => api(s ? `/automation/schedules/${s.id}` : "/automation/schedules", { method: s ? "PUT" : "POST", body: f.get() }), "Saved."); renderPage("automation/schedules"); } }] });
  };
  clear(el, head("Scheduled messages", "Announcements that post on a schedule.", h("button", { class: "btn primary", onclick: () => edit() }, "New scheduled message")),
    panel(null, table([{ label: "Name", render: (s) => h("b", null, s.name) }, { label: "Channel", render: (s) => `#${(S.meta.channels.find((c) => c.id === s.channel_id) || {}).name || "deleted-channel"}` },
      { label: "Next", render: (s) => s.enabled ? when(s.next_run_at) : pill("Off", "off") }, { label: "Repeats", render: (s) => s.interval_minutes ? `every ${dur(s.interval_minutes * 60)}` : "Once" },
      { label: "Last error", key: "last_error" }, { label: "", cls: "actions", render: (s) => h("span", null, h("button", { class: "btn sm", onclick: () => edit(s) }, "Edit"), " ",
        h("button", { class: "btn sm danger", onclick: async () => { if (!(await confirmDialog("Delete scheduled message?", s.name, "Delete"))) return; await act(() => api(`/automation/schedules/${s.id}`, { method: "DELETE" }), "Deleted."); renderPage("automation/schedules"); } }, "Delete")) }],
      d.schedules, { empty: emptyState("Nothing scheduled", "Post rules reminders, session calls or announcements automatically.") })));
});

// ============================================================================ LOGGING
page("logs", "Log viewer", async (el) => {
  const q = { category: "", q: "", since: "", until: "", page: 1 };
  const out = h("div");
  let cats = {};
  const load = async () => {
    const d = await api(`/logs?${new URLSearchParams(q)}`);
    cats = d.categories;
    clear(out, table([{ label: "Time", render: (r) => when(r.at) }, { label: "Category", render: (r) => `${cats[r.category]?.icon || ""} ${cats[r.category]?.label || r.category}` },
      { label: "Event", render: (r) => h("b", null, r.title) }, { label: "Details", render: (r) => h("span", { class: "small" }, [r.detail.description, ...(r.detail.fields || []).map((f) => `${f[0]}: ${f[1]}`)].filter(Boolean).join(" · ").slice(0, 300)) },
      { label: "By", render: (r) => r.actor_id ? who(r.actor_id) : null }], d.items, { empty: emptyState("No log entries match", "Try another category or date range.") }), pager(d, (p) => { q.page = p; load(); }));
    if (!catSel.options.length || catSel.options.length === 1) { for (const [k, v] of Object.entries(cats)) catSel.append(h("option", { value: k }, `${v.icon} ${v.label}`)); }
  };
  const catSel = h("select", null, h("option", { value: "" }, "All categories"));
  catSel.addEventListener("change", () => { q.category = catSel.value; q.page = 1; load(); });
  const since = h("input", { type: "date", "aria-label": "From", onchange: (e) => { q.since = e.target.value; q.page = 1; load(); } });
  const until = h("input", { type: "date", "aria-label": "Until", onchange: (e) => { q.until = e.target.value ? new Date(new Date(e.target.value).getTime() + 86400000).toISOString().slice(0, 10) : ""; q.page = 1; load(); } });
  const exp = can("logs.export") ? h("button", { class: "btn", onclick: () => { location.href = `/api/g/${S.gid}/logs/export.csv?${new URLSearchParams(q)}`; } }, "Export CSV") : null;
  clear(el, head("Log viewer", "Everything Pulse Dispatch records. Secrets and keys are never logged.", exp),
    h("div", { class: "filters" }, h("input", { type: "search", placeholder: "Search events", oninput: debounce((e) => { q.q = e.target.value; q.page = 1; load(); }, 300) }), catSel, since, until), out);
  await load();
  S.refresh = load;
});
page("logs/audit", "Audit log", async (el) => {
  const q = { q: "", page: 1 };
  const out = h("div");
  const load = async () => {
    const d = await api(`/logs/audit?${new URLSearchParams(q)}`);
    clear(out, table([{ label: "Time", render: (r) => when(r.at) }, { label: "Who", render: (r) => r.actor_id ? who(r.actor_id) : "System" }, { label: "Action", render: (r) => h("code", null, r.action) },
      { label: "Target", key: "target" }, { label: "Details", render: (r) => r.detail ? h("span", { class: "small muted" }, JSON.stringify(r.detail).slice(0, 200)) : null }], d.items), pager(d, (p) => { q.page = p; load(); }));
  };
  clear(el, head("Audit log", "Every configuration change and administrative action, with who made it."),
    h("div", { class: "filters" }, h("input", { type: "search", placeholder: "Search actions", oninput: debounce((e) => { q.q = e.target.value; q.page = 1; load(); }, 300) })), out);
  await load();
});
page("logs/settings", "Log routing", async (el) => {
  const d = await api("/logs/settings");
  const main = channelSelect(d.main_channel_id, { label: "No log channel (stored only)" });
  const rows = Object.entries(d.categories).map(([k, c]) => ({ k, ...c, en: h("input", { type: "checkbox", checked: c.enabled, "aria-label": `Log ${c.label}` }), ch: channelSelect(c.channel_id, { label: "Main log channel" }) }));
  clear(el, head("Log routing", "Choose what gets logged and where each kind of event goes."), panel(null, h("div", null,
    h("label", { class: "field" }, h("span", null, "Main log channel"), main),
    table([{ label: "Event", render: (r) => `${r.icon} ${r.label}` }, { label: "Record", render: (r) => r.en }, { label: "Send to", render: (r) => r.ch }], rows),
    h("button", { class: "btn primary", style: "margin-top:12px", onclick: () => act(() => api("/logs/settings", { method: "PUT", body: {
      main_channel_id: main.value || null, categories: Object.fromEntries(rows.map((r) => [r.k, { enabled: r.en.checked, channel_id: r.ch.value || null }])) } }), "Log routing saved.") }, "Save changes"))));
});

// ============================================================================ BOT MANAGEMENT
function embedPreview(d, vals) {
  const sub = (s) => String(s || "").replace(/\{([a-z_]+)\}/g, (m, k) => (k in vals ? vals[k] : m));
  // **bold** like Discord (built with text nodes, never HTML)
  const md = (s) => sub(s).split("**").map((part, i) => (i % 2 ? h("b", null, part) : part));
  const box = h("div", { class: "embed-prev", style: `border-left-color:${d.color || "#3CC8F0"}` });
  if (d.thumbnail) box.append(h("img", { class: "th", src: d.thumbnail, alt: "" }));
  if (d.author) box.append(h("div", { class: "small" }, sub(d.author)));
  if (d.title) box.append(h("div", { class: "t" }, sub(d.title)));
  if (d.description) box.append(h("div", { class: "d" }, md(d.description)));
  if (d.fields?.length) box.append(h("div", { class: "f" }, d.fields.map((f) => h("div", { class: f.inline ? "in" : "" }, h("b", null, sub(f.name)), md(f.value)))));
  if (d.image) box.append(h("img", { class: "im", src: d.image, alt: "" }));
  if (d.footer || d.timestamp) box.append(h("div", { class: "ft" }, [sub(d.footer), d.timestamp ? new Date().toLocaleString() : ""].filter(Boolean).join(" • ")));
  if (d.buttons?.length) box.append(h("div", { class: "row", style: "margin-top:8px" }, d.buttons.map((b) => h("span", { class: "btn sm" }, b.label || "Button"))));
  return box;
}
function toDraft(t) {
  const x = t?.data || {};
  return { name: t?.name || "", title: x.title || "", description: x.description || "", url: x.url || "", color: x.color !== undefined ? "#" + Number(x.color).toString(16).padStart(6, "0") : "#3CC8F0",
           author: x.author?.name || "", footer: x.footer?.text || "", thumbnail: x.thumbnail?.url || "", image: x.image?.url || "", timestamp: !!x.timestamp,
           fields: (x.fields || []).map((f) => ({ ...f })), buttons: (x.buttons || []).map((b) => ({ ...b })) };
}
function fromDraft(d) {
  return { title: d.title, description: d.description, url: d.url, color: d.color, author: d.author ? { name: d.author } : null, footer: d.footer ? { text: d.footer } : null,
           thumbnail: d.thumbnail || null, image: d.image || null, timestamp: d.timestamp, fields: d.fields, buttons: d.buttons };
}
page("embeds", "Embed builder", async (el) => {
  const data = await api("/embeds");
  let current = null, draft = toDraft(null);
  const vals = { server: S.meta.guild.name, member_count: "123", user: `@${S.me.user.name}`, user_name: S.me.user.name, user_id: S.me.user.id, roblox: "Builderman", date: new Date().toISOString().slice(0, 10) };
  const listBox = h("div"), editBox = h("div"), prevBox = h("div");
  const preview = () => clear(prevBox, h("h3", { style: "margin-bottom:8px" }, "Preview"), embedPreview(draft, vals), h("p", { class: "small muted" }, "Placeholders: ", Object.keys(data.placeholders).map((k) => `{${k}}`).join(" ")));
  const inp = (key, label, attrs = {}) => h("label", { class: "field" }, h("span", null, label), attrs.area ? h("textarea", { maxlength: attrs.max, oninput: (e) => { draft[key] = e.target.value; preview(); } }, draft[key])
    : h("input", { type: attrs.type || "text", value: draft[key], maxlength: attrs.max || null, oninput: (e) => { draft[key] = e.target.value; preview(); } }));
  const drawEdit = () => {
    clear(editBox, inp("name", "Template name", { max: 60 }), inp("title", "Title", { max: 256 }), inp("description", "Description", { area: true, max: 4096 }),
      h("div", { class: "grid2" }, inp("color", "Colour", { type: "color" }), inp("url", "Title link")), h("div", { class: "grid2" }, inp("author", "Author", { max: 256 }), inp("footer", "Footer", { max: 2048 })),
      h("div", { class: "grid2" }, inp("thumbnail", "Thumbnail URL"), inp("image", "Image URL")),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: draft.timestamp, onchange: (e) => { draft.timestamp = e.target.checked; preview(); } }), "Show timestamp"),
      h("h3", null, "Fields"), draft.fields.map((f, i) => h("div", { class: "row", style: "margin-bottom:8px" },
        h("div", { class: "fill" }, h("input", { type: "text", value: f.name, placeholder: "Name", maxlength: 256, oninput: (e) => { f.name = e.target.value; preview(); } })),
        h("div", { class: "fill" }, h("input", { type: "text", value: f.value, placeholder: "Value", maxlength: 1024, oninput: (e) => { f.value = e.target.value; preview(); } })),
        h("label", { class: "check", style: "margin:0" }, h("input", { type: "checkbox", checked: !!f.inline, onchange: (e) => { f.inline = e.target.checked; preview(); } }), "Inline"),
        h("button", { class: "btn sm danger", "aria-label": "Remove field", onclick: () => { draft.fields.splice(i, 1); drawEdit(); } }, "✕"))),
      h("button", { class: "btn sm", disabled: draft.fields.length >= 25, onclick: () => { draft.fields.push({ name: "", value: "", inline: false }); drawEdit(); } }, "Add field"),
      h("h3", { style: "margin-top:14px" }, "Link buttons"), draft.buttons.map((b, i) => h("div", { class: "row", style: "margin-bottom:8px" },
        h("div", { class: "fill" }, h("input", { type: "text", value: b.label, placeholder: "Label", maxlength: 80, oninput: (e) => { b.label = e.target.value; preview(); } })),
        h("div", { class: "fill" }, h("input", { type: "text", value: b.url, placeholder: "https://…", oninput: (e) => { b.url = e.target.value; } })),
        h("button", { class: "btn sm danger", "aria-label": "Remove button", onclick: () => { draft.buttons.splice(i, 1); drawEdit(); } }, "✕"))),
      h("button", { class: "btn sm", disabled: draft.buttons.length >= 5, onclick: () => { draft.buttons.push({ label: "", url: "" }); drawEdit(); } }, "Add button"),
      h("div", { class: "row", style: "margin-top:18px" },
        h("button", { class: "btn primary", onclick: async () => { const t = await act(() => api(current ? `/embeds/${current.id}` : "/embeds", { method: current ? "PUT" : "POST", body: { name: draft.name, data: fromDraft(draft) } }), "Template saved."); current = t; reloadList(); } }, "Save template"),
        can("announcement.send") ? h("button", { class: "btn", onclick: () => sendDialog() }, "Send…") : null,
        current ? h("button", { class: "btn danger", onclick: async () => { if (!(await confirmDialog(`Delete ${current.name}?`, "Welcome messages or automations using it stop sending it.", "Delete"))) return; await act(() => api(`/embeds/${current.id}`, { method: "DELETE" }), "Deleted."); current = null; draft = toDraft(null); drawEdit(); reloadList(); } }, "Delete") : null));
    preview();
  };
  const sendDialog = () => {
    const ch = channelSelect(null, { allowNone: false }), msg = h("textarea", { placeholder: "Optional message above the embed" });
    modal({ title: "Send this embed", body: h("div", null, h("label", { class: "field" }, h("span", null, "Channel"), ch), h("label", { class: "field" }, h("span", null, "Message"), msg)),
      actions: [{ label: "Cancel" }, { label: "Send now", primary: true, onClick: () => act(() => api("/embeds/send", { method: "POST", body: { channel_id: ch.value, data: fromDraft(draft), content: msg.value } }), "Sent.") }] });
  };
  const reloadList = async () => {
    const d = await api("/embeds");
    const preset = sel(Object.fromEntries(d.presets.map((p) => [p, p])), d.presets[0]);
    clear(listBox, h("div", { class: "row", style: "margin-bottom:10px" }, h("button", { class: "btn primary sm", onclick: () => { current = null; draft = toDraft(null); drawEdit(); } }, "New blank")),
      h("div", { class: "row", style: "margin-bottom:12px" }, h("div", { class: "fill" }, preset), h("button", { class: "btn sm", onclick: async () => { const t = await act(() => api("/embeds/preset", { method: "POST", body: { preset: preset.value } }), "Template created."); current = t; draft = toDraft(t); drawEdit(); reloadList(); } }, "Add preset")),
      d.templates.length ? h("ul", { class: "list" }, d.templates.map((t) => h("li", null, h("a", { href: "#", onclick: (e) => { e.preventDefault(); current = t; draft = toDraft(t); drawEdit(); } }, t.name), h("span", { class: "when" }, when(t.updated_at)))))
        : emptyState("No templates", "Start from a preset."));
  };
  clear(el, head("Embed builder", "Design Discord embeds with a live preview, save them as templates and send them anywhere."),
    h("div", { class: "panels" }, h("div", { class: "span-4" }, panel("Templates", listBox)), h("div", { class: "span-4" }, panel("Editor", editBox)), h("div", { class: "span-4" }, panel(null, prevBox))));
  void data;
  await reloadList(); drawEdit();
});
page("welcome", "Welcome messages", async (el) => {
  const [w, e] = await Promise.all([api("/welcome"), api("/embeds")]);
  const tpl = { "": "No template (plain message)", ...Object.fromEntries(e.templates.map((t) => [t.id, t.name])) };
  clear(el, head("Welcome messages", "Greet new members and give them starter roles."), h("div", { class: "panels" },
    h("div", { class: "span-7" }, settingsPanel("When someone joins", [{ key: "enabled", label: "Send a welcome message", type: "check" }, { key: "channel_id", label: "Channel", type: "channel" },
      { key: "template_id", label: "Embed template", type: "select", options: tpl }, { key: "message", label: "Plain message (used when no template is chosen)", type: "textarea", help: "Placeholders: {user} {user_name} {server} {member_count}" },
      { key: "dm", label: "Also send it as a DM", type: "check" }, { key: "auto_role_ids", label: "Give these roles automatically (up to 5)", type: "roles" }], w, (v) => api("/welcome", { method: "PUT", body: v }))),
    h("div", { class: "span-5" }, settingsPanel("When someone leaves", [{ key: "leave_enabled", label: "Post a goodbye message", type: "check" }, { key: "leave_channel_id", label: "Channel", type: "channel" },
      { key: "leave_message", label: "Message", type: "textarea", help: "Placeholders: {user_name} {user_id}" }], w, (v) => api("/welcome", { method: "PUT", body: v })))));
});
page("announcements", "Announcements", async (el) => {
  const e = can("embeds.manage") ? await api("/embeds") : { templates: [] };
  const f = form([{ key: "channel_id", label: "Channel", type: "channel" }, { key: "content", label: "Message", type: "textarea", max: 2000 },
    { key: "template_id", label: "Embed template (optional)", type: "select", options: { "": "None", ...Object.fromEntries(e.templates.map((t) => [t.id, t.name])) } }]);
  clear(el, head("Announcements", "Send a message or embed to any channel. To ping a role, use /announcement in Discord."), panel(null, h("div", null, f.el,
    h("button", { class: "btn primary", onclick: async () => { const v = f.get(); if (!v.channel_id) return toast("Choose a channel.", true);
      if (!(await confirmDialog("Send announcement?", `This posts in #${(S.meta.channels.find((c) => c.id === v.channel_id) || {}).name}.`, "Send", false))) return;
      await act(() => api("/embeds/send", { method: "POST", body: v }), "Announcement sent."); } }, "Send announcement"))));
});
page("commands", "Slash commands", async (el) => {
  const d = await api("/commands");
  let q = "";
  const out = h("div");
  const draw = () => clear(out, table([{ label: "Command", render: (c) => h("code", null, `/${c.name}`) }, { label: "What it does", key: "description" }, { label: "Uses (30 days)", key: "uses" }],
    d.commands.filter((c) => !q || c.name.includes(q) || c.description.toLowerCase().includes(q))));
  clear(el, head("Slash commands", `${d.commands.length} commands · ${fmtNum(d.total_uses)} uses in the last 30 days. Who can use each one is set under Settings → Permissions.`),
    h("div", { class: "filters" }, h("input", { type: "search", placeholder: "Find a command", oninput: (e) => { q = e.target.value.toLowerCase(); draw(); } })), out);
  draw();
});
page("bot/appearance", "Appearance", async (el) => {
  const a = await api("/bot/appearance");
  clear(el, head("Appearance", "How Pulse Dispatch's Discord messages look."),
    settingsPanel("Discord embeds", [{ key: "accent", label: "Accent colour", type: "color" }, { key: "footer_note", label: "Footer note", max: 60, help: "Shown after 'Pulse Dispatch | ERLC'." },
      { key: "compact", label: "Compact messages", type: "check" }, { key: "show_thumbnails", label: "Show thumbnails", type: "check" }], { ...a, accent: a.accent_hex },
      (v) => api("/bot/appearance", { method: "PUT", body: v })));
});

// ============================================================================ AI
page("ai", "AI providers", async (el) => {
  const d = await api("/ai");
  const COST = { free: ["Free tier", "ok"], trial: ["Trial credits", "warn"], credits: ["Small credits", "warn"], paid: ["Paid", "bad"], local: ["Runs locally", "info"] };
  const keyDialog = (p) => {
    const f = form([{ key: "key", label: `${p.label} API key`, type: "password", help: "Stored encrypted. It's never shown again, here or in Discord." },
      ...(p.provider === "cloudflare" ? [{ key: "account_id", label: "Cloudflare account ID" }] : [])]);
    modal({ title: `${p.label} key`, body: h("div", null, p.signup_url ? h("p", null, "Get a key: ", h("a", { href: p.signup_url, target: "_blank", rel: "noopener noreferrer" }, p.signup_url)) : null, f.el),
      actions: [{ label: "Cancel" }, { label: "Save & test", primary: true, onClick: async () => { const r = await act(() => api(`/ai/providers/${p.provider}/key`, { method: "POST", body: f.get() }));
        toast(r.test_ok ? `Key saved — ${r.message}` : `Key saved, but the test failed: ${r.message}`, !r.test_ok); renderPage("ai"); } }] });
  };
  clear(el, head("AI providers", "Pulse answers most things locally. AI providers are only used for conversation and incident assessment."),
    h("div", { class: "panels" },
      h("div", { class: "span-4" }, settingsPanel("Selection", [{ key: "mode", label: "Mode", type: "select", options: { auto: "Automatic — healthiest free provider first", manual: "Manual — use my order" } },
        { key: "failover", label: "Automatic failover", type: "check", help: "Try the next provider when one fails." },
        { key: "preferred", label: "Preferred provider", type: "select", options: { "": "No preference", ...Object.fromEntries(d.providers.map((p) => [p.provider, p.label])) } }],
        { ...d.settings, preferred: d.settings.preferred || "" }, (v) => api("/ai/settings", { method: "PUT", body: v }))),
      h("div", { class: "span-8" }, panel("Providers", table([
        { label: "Provider", render: (p) => h("b", null, p.label) }, { label: "Cost", render: (p) => pill(...(COST[p.cost] || [p.cost, "info"])) },
        { label: "Status", render: (p) => !p.configured ? pill("No key", "off") : p.cooling_down ? pill("Cooling down", "warn") : p.enabled ? pill("Ready", "ok") : pill("Disabled", "off") },
        { label: "Model", render: (p) => { const i = h("input", { type: "text", value: p.model || "", placeholder: p.effective_model || "default" });
          i.addEventListener("change", () => act(() => api(`/ai/providers/${p.provider}`, { method: "POST", body: { model: i.value } }), "Model saved.")); return i; } },
        { label: "On", render: (p) => h("input", { type: "checkbox", checked: !!p.enabled, "aria-label": "Enabled", onchange: (e) => act(() => api(`/ai/providers/${p.provider}`, { method: "POST", body: { enabled: e.target.checked } }), "Saved.") }) },
        { label: "", cls: "actions", render: (p) => h("span", null, p.key_source !== "env" ? h("button", { class: "btn sm", onclick: () => keyDialog(p) }, p.server_key ? "Replace key" : "Add key") : h("span", { class: "muted small" }, "Key in .env "),
          " ", p.configured ? h("button", { class: "btn sm", onclick: async () => { const r = await act(() => api(`/ai/providers/${p.provider}/test`, { method: "POST" })); toast(r.message, !r.ok); } }, "Test") : null,
          " ", p.server_key ? h("button", { class: "btn sm danger", onclick: async () => { if (!(await confirmDialog("Remove key?", `${p.label} stops being used on this server.`, "Remove"))) return; await act(() => api(`/ai/providers/${p.provider}/key`, { method: "DELETE" }), "Key removed."); renderPage("ai"); } }, "Remove") : null) }],
        d.providers)))));
});
page("ai/personality", "Dispatcher personality", async (el) => {
  const p = await api("/ai/personality");
  clear(el, head("Dispatcher personality", "How the dispatcher sounds on the radio and in text."), settingsPanel(null, [
    { key: "response_style", label: "Style", type: "select", options: p.styles }, { key: "agency_name", label: "Agency name", max: 60, placeholder: "Liberty County Dispatch" },
    { key: "follow_up_questions", label: "Ask follow-up questions when details are missing", type: "check" },
    { key: "ai_announcement_wording", label: "Let AI word announcements", type: "check", help: "More varied, but adds an AI request before speaking." }],
    p, (v) => api("/ai/personality", { method: "PUT", body: v })));
});
page("ai/usage", "AI usage", async (el) => {
  const d = await api("/ai/usage?range=30d");
  const m = d.series.metrics.ai_requests;
  clear(el, head("AI usage", "Requests sent to AI providers from this server (last 30 days)."), h("div", { class: "panels" },
    h("div", { class: "span-7" }, panel(`Requests · ${fmtNum(m.total)}`, chart(d.series.days, [{ label: "AI requests", values: m.values }], { type: "bar" }))),
    h("div", { class: "span-5" }, panel("By provider", table([{ label: "Provider", key: "provider" }, { label: "Requests", key: "n" }, { label: "Succeeded", key: "ok" }, { label: "Avg ms", key: "avg_ms" }], d.by_provider,
      { empty: emptyState("No AI requests yet", "Local recognition handled everything.") }))),
    h("div", { class: "span-12" }, panel("Recent requests", table([{ label: "When", render: (r) => when(r.at) }, { label: "Provider", key: "provider" }, { label: "Purpose", key: "purpose" },
      { label: "Result", render: (r) => r.ok ? pill("OK", "ok") : pill("Failed", "bad") }, { label: "ms", key: "latency_ms" }, { label: "Error", key: "error" }], d.recent)))));
});

// ============================================================================ SETTINGS
page("settings", "General settings", async (el) => {
  const s = await api("/settings");
  const chFields = Object.entries(s.channels).map(([k, v]) => ({ key: k, label: v.label, type: "channel" }));
  const keyDialog = () => {
    const f = form([{ key: "key", label: "ER:LC server key", type: "password", help: "In game: Settings → search 'API'. It's tested first, then stored encrypted." }]);
    modal({ title: "Connect ER:LC", body: f.el, actions: [{ label: "Cancel" }, { label: "Test & save", primary: true, onClick: async () => {
      const r = await act(() => api("/settings/erlc-key", { method: "POST", body: f.get() })); toast(`Connected — ${r.message}`); renderPage("settings"); } }] });
  };
  clear(el, head("General settings", "Channels and the ER:LC connection."), h("div", { class: "panels" },
    h("div", { class: "span-6" }, settingsPanel("Channels", chFields, Object.fromEntries(Object.entries(s.channels).map(([k, v]) => [k, v.value])), (v) => api("/settings/channels", { method: "PUT", body: v }))),
    h("div", { class: "span-6" }, can("admin.erlc") ? panel("ER:LC connection", h("div", null, h("p", null, s.erlc.key_set ? pill("Server key saved", "ok") : pill("No server key", "off"), " ",
      h("span", { class: "muted small" }, "The key is write-only: it can be replaced but never viewed.")),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!s.erlc.monitoring, onchange: (e) => act(() => api("/settings/erlc-monitoring", { method: "PUT", body: { enabled: e.target.checked } }), "Saved.") }),
        h("span", null, "Watch for in-game 911 calls", h("small", null, "Polls the official ER:LC API."))),
      h("button", { class: "btn primary", onclick: keyDialog }, s.erlc.key_set ? "Replace server key" : "Add server key"), " ", link("settings/integrations", "Integration status"))) : null)));
});
page("settings/permissions", "Permissions", async (el) => {
  const d = await api("/settings/permissions");
  const groups = d.groups.filter((g) => !["owner"].includes(g.key));
  let q = "";
  const groupRows = groups.filter((g) => g.key !== "citizen").map((g) => {
    const rs = roleSelect(g.roles, { multiple: true });
    return h("div", { class: "row", style: "margin-bottom:10px;align-items:flex-start" }, h("b", { style: "width:170px" }, g.label), h("div", { class: "fill" }, rs),
      h("button", { class: "btn sm", onclick: () => act(() => api(`/settings/permissions/groups/${g.key}`, { method: "PUT", body: { roles: [...rs.selectedOptions].map((o) => o.value) } }), `${g.label} roles saved.`) }, "Save"));
  });
  const matrix = h("div");
  const drawMatrix = () => {
    const rows = d.permissions.filter((p) => !q || p.key.includes(q) || p.description.toLowerCase().includes(q));
    const tb = h("tbody", null, rows.map((p) => {
      const boxes = groups.map((g) => h("input", { type: "checkbox", checked: p.groups.includes(g.key), "aria-label": `${g.label}: ${p.description}` }));
      return h("tr", null, h("td", null, h("b", null, p.description), h("br"), h("code", { class: "small muted" }, p.key), p.overridden ? h("span", { class: "pill plain small", style: "margin-left:6px" }, "custom") : null),
        boxes.map((b) => h("td", null, b)), h("td", { class: "actions" },
          h("button", { class: "btn sm", onclick: async () => { const r = await act(() => api(`/settings/permissions/overrides/${p.key}`, { method: "PUT", body: { groups: groups.filter((g, i) => boxes[i].checked).map((g) => g.key) } }), "Saved."); d.permissions = r.permissions; drawMatrix(); } }, "Save"),
          p.overridden ? h("button", { class: "btn sm ghost", onclick: async () => { const r = await act(() => api(`/settings/permissions/overrides/${p.key}`, { method: "PUT", body: { groups: null } }), "Reset to default."); d.permissions = r.permissions; drawMatrix(); } }, "Reset") : null));
    }));
    clear(matrix, h("div", { class: "tbl-wrap" }, h("table", { class: "matrix" }, h("thead", null, h("tr", null, h("th", null, "Permission"), groups.map((g) => h("th", null, g.label)), h("th"))), tb)));
  };
  drawMatrix();
  clear(el, head("Permissions", "Map Discord roles to permission groups, then choose what each group may do. The server owner always has every permission; administrators can always manage permissions."),
    panel("Roles in each group", h("div", null, h("p", { class: "muted small" }, "Members with Discord's Administrator or Manage Server permission are Administrators automatically. Everyone is a Regular Member. Hold Ctrl (or ⌘ on Mac) to select several roles."), groupRows)),
    h("div", { style: "height:16px" }),
    panel("What each group can do", h("div", null, h("div", { class: "filters" }, h("input", { type: "search", placeholder: "Find a permission", oninput: (e) => { q = e.target.value.toLowerCase(); drawMatrix(); } })), matrix)));
});
page("settings/notifications", "Notifications", async (el) => {
  const d = await api("/settings/notifications");
  const rows = Object.entries(d.kinds).map(([k, label]) => ({ k, label, en: h("input", { type: "checkbox", checked: !d.disabled.includes(k), "aria-label": label }), ch: channelSelect(d.channels[k], { label: "Dashboard only" }) }));
  clear(el, head("Notifications", "Dashboard notifications appear under the bell for people allowed to see them. Optionally mirror each kind to a Discord channel."),
    panel(null, h("div", null, table([{ label: "Kind", key: "label" }, { label: "On", render: (r) => r.en }, { label: "Also post in", render: (r) => r.ch }], rows),
      h("button", { class: "btn primary", style: "margin-top:12px", onclick: () => act(() => api("/settings/notifications", { method: "PUT", body: {
        disabled: rows.filter((r) => !r.en.checked).map((r) => r.k), channels: Object.fromEntries(rows.filter((r) => r.ch.value).map((r) => [r.k, r.ch.value])) } }), "Notification settings saved.") }, "Save changes"))));
});
page("settings/integrations", "Integrations", async (el) => {
  const d = await api("/settings/integrations");
  const e = d.erlc;
  clear(el, head("Integrations", "What's connected, and what each integration can and can't do."), h("div", { class: "panels" },
    h("div", { class: "span-7" }, panel("ER:LC (Emergency Response: Liberty County)", h("div", null, h("dl", { class: "facts" },
      h("dt", null, "Status"), h("dd", null, e.connected ? pill("Connected", "ok") : e.configured ? pill("Not connected", "warn") : pill("Not set up", "off")),
      h("dt", null, "Server"), h("dd", null, e.server_name || "—"), h("dt", null, "Players"), h("dd", null, e.players !== null ? `${e.players}/${e.max_players}` : "—"),
      h("dt", null, "Last success"), h("dd", null, when(e.last_success_at)), e.last_error ? [h("dt", null, "Last error"), h("dd", null, e.last_error)] : null,
      h("dt", null, "Rate limit left"), h("dd", null, e.rate_limit_remaining ?? "—")),
      h("h3", { style: "margin-top:14px" }, "Capabilities"), table([{ label: "Capability", key: "name" }, { label: "Available", render: (c) => yes(c.available, "Yes", "No") }, { label: "How", key: "how" }], e.capabilities)))),
    h("div", { class: "span-5" }, panel("Roblox verification", h("dl", { class: "facts" }, h("dt", null, "Profile code"), h("dd", null, pill("Always available", "ok")),
      h("dt", null, "Sign in with Roblox"), h("dd", null, d.roblox.oauth_ready ? pill("Ready", "ok") : d.roblox.oauth_configured ? pill("Needs DASHBOARD_PUBLIC_URL", "warn") : pill("Not set up", "off")))),
      panel("AI providers", h("p", null, `${d.ai.providers_ready} of ${d.ai.providers_total} ready`)),
      panel("Dashboard address", h("dl", { class: "facts" }, h("dt", null, "Public"), h("dd", null, d.dashboard.public_url || "Not set (this PC only)"), h("dt", null, "On the bot's PC"), h("dd", null, d.dashboard.local_url))))));
});
page("settings/security", "Security", async (el) => {
  const d = await api("/api/me/sessions");
  clear(el, head("Security", "Your dashboard sign-ins. Sessions last 7 days."), panel("Your sessions", h("div", null, table([{ label: "Device", key: "user_agent" }, { label: "IP", key: "ip" },
    { label: "Signed in", render: (s) => when(s.created_at) }, { label: "Last active", render: (s) => when(s.last_seen) }, { label: "", render: (s) => s.current ? pill("This browser", "ok") : null }], d.items),
    h("button", { class: "btn danger", style: "margin-top:12px", onclick: async () => {
      if (!(await confirmDialog("Sign out everywhere?", "Every browser signed in to your account will need to sign in again.", "Sign out everywhere"))) return;
      await fetch("/api/me/logout-all", { method: "POST", headers: { "X-CSRF-Token": S.me.csrf }, credentials: "same-origin" }); S.me = null; location.hash = "#/"; boot(); } }, "Sign out everywhere"))),
    panel("How your data is protected", h("ul", null, h("li", null, "Sign-in uses Discord and only reads your username and avatar."),
      h("li", null, "Which servers and sections you can open is checked on the server for every request, from your real roles."),
      h("li", null, "API keys and the ER:LC key are encrypted and can never be read back through the dashboard."),
      h("li", null, "Every change is recorded in the audit log with who made it."))));
});

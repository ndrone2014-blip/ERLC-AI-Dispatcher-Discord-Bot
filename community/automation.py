"""Event-based automation engine and scheduled messages.

An automation = trigger (an event name) + conditions (all must match the event data) +
actions (run in order). Safeguards:

* **No duplicates** - every (automation, event) pair is recorded with a UNIQUE key, so the same event
  can never run the same automation twice (e.g. after a reconnect replays an event).
* **No loops** - events caused by an automation carry origin="automation" and are ignored, and each
  automation is limited to 20 runs per minute (60 per server).
* **Visible failures** - every run is stored with its result; failures are logged and raise a
  dashboard notification.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Optional

from core.events import Event
from database.models import dumps

from .common import ServiceError, ValidationError, clean_text, iso_now, loads, parse_iso, row_dicts, safe_format, utcnow

if TYPE_CHECKING:
    from .platform import CommunityPlatform

log = logging.getLogger(__name__)

# trigger -> (label, fields available to conditions/placeholders)
TRIGGERS: dict[str, tuple[str, list[str]]] = {
    "member.joined": ("A member joins the server", ["user_id", "user_name"]),
    "member.left": ("A member leaves the server", ["user_id", "user_name"]),
    "verification.completed": ("A member verifies their Roblox account", ["user_id", "roblox", "method"]),
    "verification.removed": ("A Roblox link is removed", ["user_id", "roblox"]),
    "application.submitted": ("An application is submitted", ["user_id", "form", "application_number"]),
    "application.decided": ("An application is approved or denied", ["user_id", "form", "status", "application_number"]),
    "incident.created": ("A dispatch incident is created", ["incident_number", "nature", "location", "priority", "source"]),
    "incident.closed": ("A dispatch incident is closed", ["incident_number", "nature", "location"]),
    "mod.case": ("A moderation case is created", ["user_id", "action", "case_number", "moderator_id", "reason"]),
    "mod.report": ("A member is reported", ["user_id", "reporter_id", "report_number"]),
    "shift.started": ("A staff shift starts", ["user_id", "shift_type"]),
    "shift.ended": ("A staff shift ends", ["user_id", "duration_s"]),
    "staff.changed": ("Staff are appointed, promoted, demoted or removed", ["user_id", "action", "rank"]),
    "staff.inactive": ("A staff member becomes inactive", ["user_id", "days"]),
    "session.created": ("A session is scheduled", ["session_number", "session_name"]),
    "session.reminder": ("A session is about to start", ["session_number", "session_name"]),
    "session.started": ("A session starts", ["session_number", "session_name"]),
    "session.ended": ("A session ends", ["session_number", "session_name"]),
}
ACTIONS: dict[str, tuple[str, list[str]]] = {
    "send_message": ("Send a message to a channel", ["channel_id", "content"]),
    "send_embed": ("Send an embed template to a channel", ["channel_id", "template_id"]),
    "add_role": ("Give the member a role", ["role_id"]),
    "remove_role": ("Remove a role from the member", ["role_id"]),
    "dm_member": ("Send the member a DM", ["content"]),
    "notify": ("Create a dashboard notification", ["title", "content", "permission"]),
    "log": ("Write a log entry", ["content"]),
}
OPERATORS = {"eq": "is", "neq": "is not", "contains": "contains", "not_contains": "does not contain", "gt": "is greater than",
             "lt": "is less than", "in": "is one of (comma separated)"}

TEMPLATES: dict[str, dict] = {
    "Verified role reminder": {"description": "Thank members after they verify.", "trigger": "verification.completed",
                               "actions": [{"type": "dm_member", "content": "Thanks for verifying as **{roblox}**! You now have access to {server}."}]},
    "Staff alert: new application": {"description": "Ping the staff channel when an application arrives.", "trigger": "application.submitted",
                                     "actions": [{"type": "send_message", "channel_id": "", "content": "📝 New **{form}** application (#{application_number}) from {user}."}]},
    "Dispatch alert: new incident": {"description": "Tell dispatchers about every new incident.", "trigger": "incident.created",
                                     "actions": [{"type": "send_message", "channel_id": "", "content": "🚨 Incident #{incident_number}: {nature} at {location}"}]},
    "Supervisor alert: ban": {"description": "Notify management of every ban.", "trigger": "mod.case",
                              "conditions": [{"field": "action", "op": "eq", "value": "ban"}],
                              "actions": [{"type": "notify", "title": "Ban issued", "content": "Case #{case_number}: {user} — {reason}",
                                           "permission": "staff.manage"}]},
    "Approved applicant welcome": {"description": "DM approved applicants.", "trigger": "application.decided",
                                   "conditions": [{"field": "status", "op": "eq", "value": "approved"}],
                                   "actions": [{"type": "dm_member", "content": "🎉 Welcome to the team! Your {form} application was approved."}]},
    "Session reminder ping": {"description": "Post in a channel shortly before a session starts.", "trigger": "session.reminder",
                              "actions": [{"type": "send_message", "channel_id": "", "content": "⏰ **{session_name}** starts soon — get ready!"}]},
    "Inactivity notice": {"description": "DM staff who have become inactive.", "trigger": "staff.inactive",
                          "actions": [{"type": "dm_member", "content": "Hey! You haven't started a shift in {days} days. Everything okay?"}]},
}

SCHEDULE_MIN_INTERVAL = 10  # minutes


def _key(e: Event) -> str:
    d = e.data
    for k in ("event_key", "incident_id", "application_number", "case_number", "report_number", "shift_id", "session_number"):
        if d.get(k) is not None:
            return f"{e.name}:{k}:{d[k]}"
    return f"{e.name}:{d.get('user_id')}:{int(time.time() // 5)}"  # same user, same 5 s window = same event


def check_condition(cond: dict, data: dict) -> bool:
    field, op, want = cond.get("field"), cond.get("op", "eq"), str(cond.get("value", ""))
    have = data.get(field)
    have_s = "" if have is None else str(have)
    if op == "eq":
        return have_s.lower() == want.lower()
    if op == "neq":
        return have_s.lower() != want.lower()
    if op == "contains":
        return want.lower() in have_s.lower()
    if op == "not_contains":
        return want.lower() not in have_s.lower()
    if op in {"gt", "lt"}:
        try:
            return float(have_s) > float(want) if op == "gt" else float(have_s) < float(want)
        except ValueError:
            return False
    if op == "in":
        return have_s.lower() in {x.strip().lower() for x in want.split(",")}
    return False


def validate(data: dict) -> dict:
    name = clean_text(data.get("name"), 60, required=True, field="Name")
    trigger = data.get("trigger")
    if trigger not in TRIGGERS:
        raise ValidationError("Choose a trigger.")
    conds = []
    for c in data.get("conditions") or []:
        if not c or not c.get("field"):
            continue
        if c["field"] not in TRIGGERS[trigger][1]:
            raise ValidationError(f"'{c['field']}' is not available for this trigger.")
        if c.get("op", "eq") not in OPERATORS:
            raise ValidationError("Unknown condition operator.")
        conds.append({"field": c["field"], "op": c.get("op", "eq"), "value": clean_text(str(c.get("value", "")), 200)})
    actions = []
    for a in data.get("actions") or []:
        t = (a or {}).get("type")
        if t not in ACTIONS:
            raise ValidationError("Unknown action type.")
        clean: dict[str, Any] = {"type": t}
        for f in ACTIONS[t][1]:
            v = a.get(f)
            if f in {"channel_id", "role_id", "template_id"}:
                if v in (None, "", 0, "0"):
                    raise ValidationError(f"{ACTIONS[t][0]}: choose a {f.replace('_id', '')}.")
                clean[f] = int(v)
            elif f == "permission":
                clean[f] = clean_text(v, 40) or None
            else:
                clean[f] = clean_text(v, 1800)
                if f == "content" and not clean[f]:
                    raise ValidationError(f"{ACTIONS[t][0]}: write the message.")
        actions.append(clean)
    if not actions:
        raise ValidationError("Add at least one action.")
    if len(actions) > 10:
        raise ValidationError("At most 10 actions per automation.")
    return {"name": name, "description": clean_text(data.get("description"), 300) or None, "trigger": trigger,
            "conditions": conds[:10], "actions": actions, "enabled": bool(data.get("enabled", True))}


class AutomationEngine:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db
        self._recent: dict[tuple, deque] = defaultdict(deque)
        for trig in TRIGGERS:
            p.bus.subscribe(trig, self.handle)

    # ------------------------------------------------------------------ CRUD
    def _row(self, r) -> dict:
        d = dict(r)
        d["conditions"], d["actions"] = loads(d["conditions"], []), loads(d["actions"], [])
        for a in d["actions"]:
            for k in ("channel_id", "role_id", "template_id"):
                if a.get(k):
                    a[k] = str(a[k])
        return d

    async def list(self, guild_id: int) -> list[dict]:
        return [self._row(r) for r in await self.db.fetchall("SELECT * FROM automations WHERE guild_id = ? ORDER BY name", (guild_id,))]

    async def get(self, guild_id: int, automation_id: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM automations WHERE guild_id = ? AND id = ?", (guild_id, int(automation_id)))
        return self._row(r) if r else None

    async def save(self, guild_id: int, data: dict, actor_id: int, automation_id: Optional[int] = None) -> dict:
        v = validate(data)
        dup = await self.db.fetchone("SELECT id FROM automations WHERE guild_id = ? AND lower(name) = lower(?)", (guild_id, v["name"]))
        if dup and dup["id"] != automation_id:
            raise ServiceError(f"An automation named '{v['name']}' already exists.")
        if automation_id:
            if not await self.get(guild_id, automation_id):
                raise ServiceError("Automation not found.")
            await self.db.execute("UPDATE automations SET name = ?, description = ?, trigger = ?, conditions = ?, actions = ?, enabled = ?, "
                                  "updated_at = ? WHERE id = ? AND guild_id = ?",
                                  (v["name"], v["description"], v["trigger"], dumps(v["conditions"]), dumps(v["actions"]), int(v["enabled"]),
                                   iso_now(), int(automation_id), guild_id))
            aid = int(automation_id)
        else:
            if await self.db.fetchval("SELECT COUNT(*) FROM automations WHERE guild_id = ?", (guild_id,), 0) >= 50:
                raise ServiceError("A server can have at most 50 automations.")
            aid = await self.db.execute("INSERT INTO automations(guild_id, name, description, trigger, conditions, actions, enabled, created_by) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (guild_id, v["name"], v["description"], v["trigger"],
                                                                           dumps(v["conditions"]), dumps(v["actions"]), int(v["enabled"]), actor_id))
        await self.p.audit.record(guild_id, actor_id, "automation.save", v["name"], {"trigger": v["trigger"], "enabled": v["enabled"]})
        return await self.get(guild_id, aid)

    async def set_enabled(self, guild_id: int, automation_id: int, enabled: bool, actor_id: int) -> dict:
        a = await self.get(guild_id, automation_id)
        if not a:
            raise ServiceError("Automation not found.")
        await self.db.execute("UPDATE automations SET enabled = ?, updated_at = ? WHERE id = ?", (int(enabled), iso_now(), a["id"]))
        await self.p.audit.record(guild_id, actor_id, "automation.toggle", a["name"], {"enabled": enabled})
        return await self.get(guild_id, automation_id)

    async def delete(self, guild_id: int, automation_id: int, actor_id: int) -> bool:
        a = await self.get(guild_id, automation_id)
        if not a:
            return False
        await self.db.execute("DELETE FROM automations WHERE id = ? AND guild_id = ?", (a["id"], guild_id))
        await self.p.audit.record(guild_id, actor_id, "automation.delete", a["name"])
        return True

    async def runs(self, guild_id: int, automation_id: Optional[int] = None, limit: int = 50) -> list[dict]:
        sql, params = ("SELECT r.*, a.name FROM automation_runs r LEFT JOIN automations a ON a.id = r.automation_id WHERE r.guild_id = ?",
                       [guild_id])
        if automation_id:
            sql += " AND r.automation_id = ?"
            params.append(int(automation_id))
        return row_dicts(await self.db.fetchall(sql + " ORDER BY r.id DESC LIMIT ?", params + [limit]))

    async def active_count(self, guild_id: int) -> int:
        return await self.db.fetchval("SELECT COUNT(*) FROM automations WHERE guild_id = ? AND enabled = 1", (guild_id,), 0)

    # ------------------------------------------------------------------ execution
    def _allowed(self, guild_id: int, automation_id: int) -> bool:
        now = time.monotonic()
        for key, cap in (((guild_id, automation_id), 20), ((guild_id,), 60)):
            q = self._recent[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= cap:
                return False
        self._recent[(guild_id, automation_id)].append(now)
        self._recent[(guild_id,)].append(now)
        return True

    async def handle(self, e: Event) -> None:
        if e.data.get("origin") == "automation":
            return
        rows = await self.db.fetchall("SELECT * FROM automations WHERE guild_id = ? AND trigger = ? AND enabled = 1", (e.guild_id, e.name))
        if not rows:
            return
        data = await self._event_data(e)
        key = _key(e)
        for r in rows:
            a = self._row(r)
            if not all(check_condition(c, data) for c in a["conditions"]):
                continue
            try:  # claim the (automation, event) pair; a duplicate event fails here and is skipped
                run_id = await self.db.execute("INSERT INTO automation_runs(guild_id, automation_id, event_key, ok) VALUES (?, ?, ?, 0)",
                                               (e.guild_id, a["id"], key))
            except Exception:
                continue
            if not self._allowed(e.guild_id, a["id"]):
                await self._finish(e.guild_id, a, run_id, False, "Skipped: run limit reached (possible loop)")
                continue
            await self.run(e.guild_id, a, data, run_id)

    async def _event_data(self, e: Event) -> dict:
        data = dict(e.data)
        if data.get("incident_id") and self.p.dispatch is not None:
            inc = await self.p.dispatch.incidents.get(data["incident_id"])
            if inc:
                data.update(incident_number=inc["number"], nature=inc.get("nature") or "Unknown", location=inc.get("location") or "unknown location",
                            priority=inc.get("priority") or inc.get("ai_priority") or "unassessed", source=inc["source"])
        vals = await self.p.embeds.values(e.guild_id, int(data["user_id"]) if data.get("user_id") else None)
        return {**vals, **{k: v for k, v in data.items() if v is not None}}

    async def run(self, guild_id: int, a: dict, data: dict, run_id: Optional[int] = None) -> tuple[bool, str]:
        results, ok = [], True
        uid = int(data["user_id"]) if str(data.get("user_id") or "").isdigit() else None
        for act in a["actions"]:
            t = act["type"]
            err: Optional[str] = None
            text = safe_format(act.get("content"), data, 1800) if act.get("content") else None
            try:
                if t == "send_message":
                    _, err = await self.p.actions.send(guild_id, int(act["channel_id"]), content=text)
                elif t == "send_embed":
                    await self.p.embeds.send(guild_id, int(act["channel_id"]), actor_id=None, template_id=int(act["template_id"]), user_id=uid)
                elif t in {"add_role", "remove_role", "dm_member"} and uid is None:
                    err = "this event has no member"
                elif t == "add_role":
                    err = await self.p.actions.add_role(guild_id, uid, int(act["role_id"]), f"Automation: {a['name']}")
                elif t == "remove_role":
                    err = await self.p.actions.remove_role(guild_id, uid, int(act["role_id"]), f"Automation: {a['name']}")
                elif t == "dm_member":
                    err = await self.p.actions.dm(uid, content=text)
                elif t == "notify":
                    await self.p.notifications.create(guild_id, "automation", safe_format(act.get("title") or a["name"], data, 200),
                                                      body=text or "", permission=act.get("permission"))
                elif t == "log":
                    await self.p.log(guild_id, "automation", f"Automation · {a['name']}", description=text)
            except ServiceError as ex:
                err = str(ex)
            except Exception as ex:  # never let one automation break the event bus
                log.exception("Automation %s action %s failed", a["id"], t)
                err = f"{type(ex).__name__}"
            results.append(f"{ACTIONS[t][0]}: {'failed - ' + err if err else 'ok'}")
            ok = ok and not err
        detail = "; ".join(results)
        await self._finish(guild_id, a, run_id, ok, detail)
        return ok, detail

    async def _finish(self, guild_id: int, a: dict, run_id: Optional[int], ok: bool, detail: str) -> None:
        if run_id:
            await self.db.execute("UPDATE automation_runs SET ok = ?, detail = ? WHERE id = ?", (int(ok), detail[:1000], run_id))
        if ok:
            await self.db.execute("UPDATE automations SET run_count = run_count + 1, last_run_at = ? WHERE id = ?", (iso_now(), a["id"]))
        else:
            await self.db.execute("UPDATE automations SET run_count = run_count + 1, last_run_at = ?, last_error = ?, last_error_at = ? WHERE id = ?",
                                  (iso_now(), detail[:500], iso_now(), a["id"]))
            await self.p.log(guild_id, "automation", f"Automation Failed · {a['name']}", description=detail[:1000], colour=0xD29922)
            await self.p.notifications.create(guild_id, "automation", f"Automation failed: {a['name']}", body=detail[:300], severity="warning",
                                              link="#/automation", permission="automation.manage", dedupe_key=f"auto-fail:{a['id']}")

    # ------------------------------------------------------------------ scheduled messages
    async def schedules(self, guild_id: int) -> list[dict]:
        rows = row_dicts(await self.db.fetchall("SELECT * FROM scheduled_messages WHERE guild_id = ? ORDER BY next_run_at", (guild_id,)))
        for r in rows:
            r["channel_id"] = str(r["channel_id"])
        return rows

    async def save_schedule(self, guild_id: int, data: dict, actor_id: int, schedule_id: Optional[int] = None) -> dict:
        name = clean_text(data.get("name"), 60, required=True, field="Name")
        channel = data.get("channel_id")
        if channel in (None, "", "0"):
            raise ValidationError("Choose a channel.")
        content = clean_text(data.get("content"), 2000)
        template_id = int(data["template_id"]) if data.get("template_id") not in (None, "", "0", 0) else None
        if not content and not template_id:
            raise ValidationError("Write a message or choose an embed template.")
        interval = data.get("interval_minutes")
        interval = int(interval) if interval not in (None, "", 0, "0") else None
        if interval is not None and interval < SCHEDULE_MIN_INTERVAL:
            raise ValidationError(f"The shortest repeat interval is {SCHEDULE_MIN_INTERVAL} minutes.")
        start = parse_iso(str(data.get("next_run_at") or "").replace(" ", "T"))
        if start is None:
            raise ValidationError("Choose when to send it (UTC).")
        if start.tzinfo is None:
            start = start.replace(tzinfo=dt.timezone.utc)
        nxt = start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        vals = (name, int(channel), content or None, template_id, interval, nxt, int(bool(data.get("enabled", True))))
        if schedule_id:
            n = await self.db.execute_rowcount("UPDATE scheduled_messages SET name = ?, channel_id = ?, content = ?, template_id = ?, "
                                               "interval_minutes = ?, next_run_at = ?, enabled = ?, last_error = NULL WHERE id = ? AND guild_id = ?",
                                               (*vals, int(schedule_id), guild_id))
            if not n:
                raise ServiceError("Scheduled message not found.")
            sid = int(schedule_id)
        else:
            sid = await self.db.execute("INSERT INTO scheduled_messages(name, channel_id, content, template_id, interval_minutes, next_run_at, "
                                        "enabled, guild_id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*vals, guild_id, actor_id))
        await self.p.audit.record(guild_id, actor_id, "schedule.save", name)
        r = dict(await self.db.fetchone("SELECT * FROM scheduled_messages WHERE id = ?", (sid,)))
        r["channel_id"] = str(r["channel_id"])
        return r

    async def delete_schedule(self, guild_id: int, schedule_id: int, actor_id: int) -> bool:
        n = await self.db.execute_rowcount("DELETE FROM scheduled_messages WHERE id = ? AND guild_id = ?", (int(schedule_id), guild_id))
        if n:
            await self.p.audit.record(guild_id, actor_id, "schedule.delete", str(schedule_id))
        return bool(n)

    async def run_due_schedules(self) -> int:
        rows = await self.db.fetchall("SELECT * FROM scheduled_messages WHERE enabled = 1 AND next_run_at <= ?", (iso_now(),))
        for r in rows:
            err = None
            try:
                await self.p.embeds.send(r["guild_id"], r["channel_id"], actor_id=None, template_id=r["template_id"], content=r["content"])
            except (ServiceError, ValidationError) as e:
                err = str(e)
            if r["interval_minutes"]:
                nxt = (parse_iso(r["next_run_at"]) or utcnow())
                while nxt <= utcnow():
                    nxt += dt.timedelta(minutes=r["interval_minutes"])
                await self.db.execute("UPDATE scheduled_messages SET next_run_at = ?, last_run_at = ?, last_error = ? WHERE id = ?",
                                      (nxt.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), iso_now(), err, r["id"]))
            else:
                await self.db.execute("UPDATE scheduled_messages SET enabled = 0, last_run_at = ?, last_error = ? WHERE id = ?",
                                      (iso_now(), err, r["id"]))
            if err:
                await self.p.notifications.create(r["guild_id"], "automation", f"Scheduled message failed: {r['name']}", body=err,
                                                  severity="warning", link="#/automation/schedules", permission="automation.manage",
                                                  dedupe_key=f"sched-fail:{r['id']}")
            await asyncio.sleep(0)
        return len(rows)

"""Application system: custom forms, submissions and the review workflow."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any, Optional

from database.models import dumps

from .common import ServiceError, ValidationError, clean_text, iso_now, loads, paginate_sql, row_dicts, utcnow

if TYPE_CHECKING:
    from .platform import CommunityPlatform

QUESTION_TYPES = {"short": "Short answer", "long": "Long answer", "choice": "Multiple choice", "dropdown": "Dropdown",
                  "yesno": "Yes / No", "confirm": "Required confirmation"}
FORM_KINDS = {"staff": "Staff", "police": "Police Department", "sheriff": "Sheriff's Office", "fire": "Fire Department",
              "ems": "EMS", "dispatch": "Dispatch", "civilian": "Civilian", "custom": "Custom"}
STATUSES = {"submitted": "Submitted", "under_review": "Under Review", "awaiting_response": "Awaiting Response",
            "approved": "Approved", "denied": "Denied", "archived": "Archived"}
OPEN_STATUSES = ("submitted", "under_review", "awaiting_response")
DEFAULTS = {"dm_results": True, "archive_after_days": 0}

_Q = lambda label, type="short", required=True, options=None: {"label": label, "type": type, "required": required,  # noqa: E731
                                                               "options": options or []}
PRESETS: dict[str, dict] = {
    "Staff Application": {"kind": "staff", "description": "Apply to join the moderation team.", "questions": [
        _Q("How old are you?"), _Q("What timezone are you in?"),
        _Q("Why do you want to join the staff team?", "long"), _Q("Do you have previous moderation experience?", "long"),
        _Q("How would you handle a member breaking the rules repeatedly?", "long"),
        _Q("How active can you be each week?", "dropdown", options=["Less than 5 hours", "5-10 hours", "10-20 hours", "20+ hours"]),
        _Q("I have read and agree to the staff guidelines.", "confirm")]},
    "Police Department": {"kind": "police", "description": "Apply to patrol as a law enforcement officer.", "questions": [
        _Q("What is your Roblox username?"), _Q("Why do you want to join the police department?", "long"),
        _Q("What does 10-4 mean?"), _Q("Describe how you would conduct a traffic stop.", "long"),
        _Q("Have you been in a police department in another community?", "yesno"),
        _Q("I agree to follow the department's SOPs.", "confirm")]},
    "Sheriff's Office": {"kind": "sheriff", "description": "Apply to the Sheriff's Office.", "questions": [
        _Q("What is your Roblox username?"), _Q("Why the Sheriff's Office?", "long"),
        _Q("Explain when a pursuit should be terminated.", "long"), _Q("I agree to follow the department's SOPs.", "confirm")]},
    "Fire Department": {"kind": "fire", "description": "Apply to the fire department.", "questions": [
        _Q("What is your Roblox username?"), _Q("Why do you want to join the fire department?", "long"),
        _Q("Which role interests you most?", "choice", options=["Engine", "Ladder", "Rescue", "Command"]),
        _Q("I agree to follow the department's SOPs.", "confirm")]},
    "EMS": {"kind": "ems", "description": "Apply to Emergency Medical Services.", "questions": [
        _Q("What is your Roblox username?"), _Q("Why do you want to join EMS?", "long"),
        _Q("How would you prioritise two patients at one scene?", "long"), _Q("I agree to follow the department's SOPs.", "confirm")]},
    "Dispatch": {"kind": "dispatch", "description": "Apply to become a dispatcher.", "questions": [
        _Q("What is your Roblox username?"), _Q("Why do you want to dispatch?", "long"),
        _Q("Rewrite this call professionally: 'yo theres a fire at the gas station send ppl'", "long"),
        _Q("Can you use a microphone during sessions?", "yesno"), _Q("I agree to the dispatch guidelines.", "confirm")]},
    "Civilian": {"kind": "civilian", "description": "Apply for the civilian whitelist.", "questions": [
        _Q("What is your Roblox username?"), _Q("Describe a roleplay character you would like to play.", "long"),
        _Q("I agree to the server rules.", "confirm")]},
}


def validate_questions(questions: Any) -> list[dict]:
    if not isinstance(questions, list) or not questions:
        raise ValidationError("A form needs at least one question.")
    if len(questions) > 25:
        raise ValidationError("A form can have at most 25 questions.")
    out = []
    for i, q in enumerate(questions, 1):
        q = q or {}
        label = clean_text(q.get("label"), 300, required=True, field=f"Question {i}")
        qtype = q.get("type") or "short"
        if qtype not in QUESTION_TYPES:
            raise ValidationError(f"Question {i}: unknown answer type.")
        options = [o for o in (clean_text(x, 100) for x in q.get("options") or []) if o]
        if qtype in {"choice", "dropdown"}:
            if len(options) < 2:
                raise ValidationError(f"Question {i}: add at least two options.")
            if len(options) > 25:
                raise ValidationError(f"Question {i}: at most 25 options.")
            if len({o.lower() for o in options}) != len(options):
                raise ValidationError(f"Question {i}: options must be unique.")
        else:
            options = []
        max_len = 300 if qtype == "short" else 1500
        out.append({"id": f"q{i}", "label": label, "type": qtype, "required": bool(q.get("required", True)) or qtype == "confirm",
                    "options": options, "max_length": max_len})
    return out


def validate_answers(questions: list[dict], answers: dict[str, Any]) -> list[dict]:
    out = []
    for q in questions:
        raw = answers.get(q["id"])
        if q["type"] == "confirm":
            ok = raw in (True, "true", "yes", "on", "1", 1, "I agree", "Yes")
            if not ok:
                raise ValidationError(f"You must confirm: “{q['label']}”")
            value = "Confirmed"
        elif q["type"] == "yesno":
            if raw in (None, "") and not q["required"]:
                value = ""
            elif str(raw).lower() in {"yes", "true", "1"}:
                value = "Yes"
            elif str(raw).lower() in {"no", "false", "0"}:
                value = "No"
            else:
                raise ValidationError(f"Answer yes or no: “{q['label']}”")
        elif q["type"] in {"choice", "dropdown"}:
            value = clean_text(raw, 100)
            if not value and not q["required"]:
                value = ""
            elif value not in q["options"]:
                raise ValidationError(f"Pick one of the options for “{q['label']}”")
        else:
            value = clean_text(raw, q["max_length"])
            if q["required"] and not value:
                raise ValidationError(f"“{q['label']}” is required.")
        out.append({"question": q["label"], "answer": value, "type": q["type"]})
    return out


class ApplicationService:
    def __init__(self, p: "CommunityPlatform"):
        self.p = p
        self.db = p.db

    async def settings(self, guild_id: int) -> dict:
        return await self.p.settings(guild_id, "application_settings", DEFAULTS)

    # ------------------------------------------------------------------ forms
    def _form(self, r) -> dict:
        d = dict(r)
        d["questions"] = loads(d["questions"], [])
        d["submit_roles"] = [str(x) for x in loads(d["submit_roles"], [])]
        d["reviewer_roles"] = [str(x) for x in loads(d["reviewer_roles"], [])]
        for k in ("approve_role_id", "review_channel_id"):
            d[k] = str(d[k]) if d.get(k) else None
        return d

    async def forms(self, guild_id: int, *, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM application_forms WHERE guild_id = ?" + (" AND enabled = 1" if enabled_only else "") + " ORDER BY name"
        return [self._form(r) for r in await self.db.fetchall(sql, (guild_id,))]

    async def form(self, guild_id: int, form_id: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM application_forms WHERE guild_id = ? AND id = ?", (guild_id, int(form_id)))
        return self._form(r) if r else None

    async def form_by_name(self, guild_id: int, name: str) -> Optional[dict]:
        r = await self.db.fetchone("SELECT * FROM application_forms WHERE guild_id = ? AND lower(name) = lower(?)", (guild_id, name.strip()))
        return self._form(r) if r else None

    async def save_form(self, guild_id: int, data: dict, actor_id: int, form_id: Optional[int] = None) -> dict:
        name = clean_text(data.get("name"), 60, required=True, field="Form name")
        kind = data.get("kind") or "custom"
        if kind not in FORM_KINDS:
            raise ValidationError("Unknown form type.")
        questions = validate_questions(data.get("questions"))
        ids = lambda key: dumps([int(x) for x in (data.get(key) or []) if str(x).strip().isdigit()][:20])  # noqa: E731
        opt = lambda key: int(data[key]) if data.get(key) not in (None, "", 0, "0") else None  # noqa: E731
        existing = await self.form_by_name(guild_id, name)
        if existing and existing["id"] != form_id:
            raise ServiceError(f"A form named '{name}' already exists.")
        values = (name, kind, clean_text(data.get("description"), 500) or None, dumps(questions), int(bool(data.get("enabled", True))),
                  ids("submit_roles"), ids("reviewer_roles"), opt("approve_role_id"), opt("review_channel_id"),
                  max(0, min(720, int(data.get("cooldown_hours", 24) or 0))), int(bool(data.get("require_verified"))))
        if form_id:
            if not await self.form(guild_id, form_id):
                raise ServiceError("Form not found.")
            await self.db.execute(
                "UPDATE application_forms SET name = ?, kind = ?, description = ?, questions = ?, enabled = ?, submit_roles = ?, "
                "reviewer_roles = ?, approve_role_id = ?, review_channel_id = ?, cooldown_hours = ?, require_verified = ?, updated_at = ? "
                "WHERE id = ? AND guild_id = ?", (*values, iso_now(), int(form_id), guild_id))
            fid = int(form_id)
        else:
            fid = await self.db.execute(
                "INSERT INTO application_forms(name, kind, description, questions, enabled, submit_roles, reviewer_roles, approve_role_id, "
                "review_channel_id, cooldown_hours, require_verified, guild_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, guild_id))
        await self.p.audit.record(guild_id, actor_id, "application.form_save", name)
        return await self.form(guild_id, fid)

    async def create_from_preset(self, guild_id: int, preset: str, actor_id: int) -> dict:
        if preset not in PRESETS:
            raise ServiceError("Unknown preset.")
        name, n = preset, 2
        while await self.form_by_name(guild_id, name):
            name, n = f"{preset} {n}", n + 1
        return await self.save_form(guild_id, {"name": name, **PRESETS[preset], "enabled": False}, actor_id)

    async def delete_form(self, guild_id: int, form_id: int, actor_id: int) -> bool:
        f = await self.form(guild_id, form_id)
        if not f:
            return False
        await self.db.execute("DELETE FROM application_forms WHERE id = ? AND guild_id = ?", (int(form_id), guild_id))
        await self.p.audit.record(guild_id, actor_id, "application.form_delete", f["name"])
        return True

    # ------------------------------------------------------------------ submissions
    async def can_submit(self, guild_id: int, form: dict, user_id: int, role_ids: set[int]) -> Optional[str]:
        if not form["enabled"]:
            return "This application is closed right now."
        if form["submit_roles"] and not role_ids & {int(r) for r in form["submit_roles"]}:
            return "You don't have a role that can submit this application."
        if form.get("require_verified") and not await self.p.verification.get_link(guild_id, user_id):
            return "Verify your Roblox account first with `/verify`."
        open_app = await self.db.fetchone(
            f"SELECT number FROM applications WHERE guild_id = ? AND form_id = ? AND user_id = ? AND status IN {OPEN_STATUSES}",
            (guild_id, form["id"], int(user_id)))
        if open_app:
            return f"You already have an open application (#{open_app['number']}) for this form."
        if form["cooldown_hours"]:
            since = (utcnow() - dt.timedelta(hours=form["cooldown_hours"])).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if await self.db.fetchone("SELECT 1 FROM applications WHERE guild_id = ? AND form_id = ? AND user_id = ? AND created_at >= ?",
                                      (guild_id, form["id"], int(user_id), since)):
                return f"You can apply to this form once every {form['cooldown_hours']} hour(s)."
        return None

    async def submit(self, guild_id: int, form_id: int, user_id: int, answers: dict[str, Any], role_ids: set[int]) -> dict:
        form = await self.form(guild_id, form_id)
        if not form:
            raise ServiceError("Form not found.")
        problem = await self.can_submit(guild_id, form, user_id, role_ids)
        if problem:
            raise ServiceError(problem)
        clean = validate_answers(form["questions"], answers)
        number = await self.db.next_number(guild_id, "application")
        aid = await self.db.execute("INSERT INTO applications(guild_id, number, form_id, form_name, user_id, answers) VALUES (?, ?, ?, ?, ?, ?)",
                                    (guild_id, number, form["id"], form["name"], int(user_id), dumps(clean)))
        await self.p.log(guild_id, "applications", f"Application #{number} Submitted",
                         fields=[("Form", form["name"], True), ("Applicant", f"<@{user_id}>", True)], actor_id=int(user_id), colour=0x8957E5)
        await self.p.notifications.create(guild_id, "application", f"New application: {form['name']}",
                                          body=f"Application #{number} from <@{user_id}> is waiting for review.",
                                          link=f"#/applications/{number}", permission="application.review")
        if form.get("review_channel_id"):
            embed = {"title": f"📝 Application #{number} · {form['name']}", "color": 0x8957E5,
                     "description": f"Submitted by <@{user_id}>.\nReview it on the dashboard or with `/application review {number}`.",
                     "fields": [{"name": a["question"][:256], "value": (a["answer"] or "—")[:1024], "inline": False} for a in clean[:10]],
                     "footer": {"text": "Pulse Dispatch | ERLC • Applications"}, "timestamp": iso_now()}
            await self.p.actions.send(guild_id, int(form["review_channel_id"]), embed=embed)
        await self.p.publish("application.submitted", guild_id, application_number=number, application_id=aid, form=form["name"],
                             user_id=int(user_id))
        return await self.get(guild_id, number)

    async def get(self, guild_id: int, number: int) -> Optional[dict]:
        r = await self.db.fetchone("SELECT a.*, m.username, m.display_name, m.avatar_url FROM applications a LEFT JOIN members m "
                                   "ON m.guild_id = a.guild_id AND m.user_id = a.user_id WHERE a.guild_id = ? AND a.number = ?",
                                   (guild_id, int(number)))
        if not r:
            return None
        d = dict(r)
        d["answers"] = loads(d["answers"], [])
        d["user_id"] = str(d["user_id"])
        d["notes"] = row_dicts(await self.db.fetchall("SELECT * FROM application_notes WHERE application_id = ? ORDER BY id", (d["id"],)))
        return d

    async def list(self, guild_id: int, *, status: str = "open", form_id: Optional[int] = None, user_id: Optional[int] = None,
                   q: str = "", page: int = 1, per_page: int = 25) -> dict:
        where, params = ["a.guild_id = ?"], [guild_id]
        if status == "open":
            where.append(f"a.status IN {OPEN_STATUSES}")
        elif status in STATUSES:
            where.append("a.status = ?")
            params.append(status)
        if form_id:
            where.append("a.form_id = ?")
            params.append(int(form_id))
        if user_id:
            where.append("a.user_id = ?")
            params.append(int(user_id))
        if q:
            where.append("(m.username LIKE ? OR a.form_name LIKE ? OR CAST(a.number AS TEXT) = ?)")
            params += [f"%{q}%", f"%{q}%", q.lstrip("#")]
        base = " FROM applications a LEFT JOIN members m ON m.guild_id = a.guild_id AND m.user_id = a.user_id WHERE " + " AND ".join(where)
        pg = await paginate_sql(self.db, "SELECT a.id, a.number, a.form_name, a.user_id, a.status, a.reviewer_id, a.created_at, a.updated_at, "
                                         "m.username, m.display_name" + base + " ORDER BY a.id DESC", "SELECT COUNT(*)" + base, params, page, per_page)
        for it in pg.items:
            it["user_id"] = str(it["user_id"])
        return pg.as_dict()

    async def can_review(self, guild_id: int, app: dict, role_ids: set[int], has_permission: bool) -> bool:
        if has_permission:
            return True
        form = await self.form(guild_id, app["form_id"]) if app.get("form_id") else None
        return bool(form and form["reviewer_roles"] and role_ids & {int(r) for r in form["reviewer_roles"]})

    async def set_status(self, guild_id: int, number: int, status: str, actor_id: int, reason: str = "") -> dict:
        if status not in STATUSES:
            raise ValidationError("Unknown status.")
        app = await self.get(guild_id, number)
        if not app:
            raise ServiceError("Application not found.")
        if app["status"] in {"approved", "denied"} and status not in {"archived"}:
            raise ServiceError(f"That application was already {app['status']}.")
        reason = clean_text(reason, 1000)
        if status == "awaiting_response" and not reason:
            raise ValidationError("Write what additional information you need from the applicant.")
        await self.db.execute("UPDATE applications SET status = ?, reviewer_id = ?, decision_reason = COALESCE(?, decision_reason), "
                              "updated_at = ? WHERE id = ?", (status, actor_id, reason or None, iso_now(), app["id"]))
        problems: list[str] = []
        form = await self.form(guild_id, app["form_id"]) if app.get("form_id") else None
        if status == "approved" and form and form.get("approve_role_id"):
            if (err := await self.p.actions.add_role(guild_id, int(app["user_id"]), int(form["approve_role_id"]), f"Application #{number} approved")):
                problems.append(f"Could not give the role: {err}")
        s = await self.settings(guild_id)
        if s["dm_results"] and status in {"approved", "denied", "awaiting_response"}:
            colour = {"approved": 0x2EA043, "denied": 0xDA3633, "awaiting_response": 0xD29922}[status]
            title = {"approved": "✅ Application approved", "denied": "❌ Application denied",
                     "awaiting_response": "✉️ More information needed"}[status]
            desc = f"Your **{app['form_name']}** application (#{number}) was updated." + (f"\n\n**Reviewer note:** {reason}" if reason else "")
            if (err := await self.p.actions.dm(int(app["user_id"]), embed={"title": title, "description": desc, "color": colour,
                                                                            "footer": {"text": "Pulse Dispatch | ERLC"}})):
                problems.append(f"Could not DM the applicant: {err}")
        await self.p.audit.record(guild_id, actor_id, "application.status", f"application:{number}", {"status": status})
        await self.p.log(guild_id, "applications", f"Application #{number} · {STATUSES[status]}",
                         fields=[("Form", app["form_name"], True), ("Applicant", f"<@{app['user_id']}>", True)]
                         + ([("Reason", reason, False)] if reason else []) + ([("Problems", "; ".join(problems), False)] if problems else []),
                         actor_id=actor_id, colour={"approved": 0x2EA043, "denied": 0xDA3633}.get(status))
        if status in {"approved", "denied"}:
            await self.p.publish("application.decided", guild_id, application_number=number, form=app["form_name"],
                                 user_id=int(app["user_id"]), status=status, actor_id=actor_id)
        out = await self.get(guild_id, number)
        out["problems"] = problems
        return out

    async def add_note(self, guild_id: int, number: int, author_id: int, note: str) -> dict:
        app = await self.get(guild_id, number)
        if not app:
            raise ServiceError("Application not found.")
        await self.db.execute("INSERT INTO application_notes(guild_id, application_id, author_id, note) VALUES (?, ?, ?, ?)",
                              (guild_id, app["id"], author_id, clean_text(note, 1000, required=True, field="Note")))
        if app["status"] == "submitted":
            await self.db.execute("UPDATE applications SET status = 'under_review', updated_at = ? WHERE id = ?", (iso_now(), app["id"]))
        return await self.get(guild_id, number)

    async def respond(self, guild_id: int, number: int, user_id: int, text: str) -> dict:
        """The applicant answers a 'more information needed' request."""
        app = await self.get(guild_id, number)
        if not app or int(app["user_id"]) != int(user_id):
            raise ServiceError("Application not found.")
        if app["status"] != "awaiting_response":
            raise ServiceError("This application is not waiting for your response.")
        await self.db.execute("INSERT INTO application_notes(guild_id, application_id, author_id, note) VALUES (?, ?, ?, ?)",
                              (guild_id, app["id"], int(user_id), "Applicant response: " + clean_text(text, 1500, required=True, field="Response")))
        await self.db.execute("UPDATE applications SET status = 'under_review', updated_at = ? WHERE id = ?", (iso_now(), app["id"]))
        await self.p.notifications.create(guild_id, "application", f"Applicant responded (#{number})",
                                          body=f"<@{user_id}> answered the reviewer's question.", link=f"#/applications/{number}",
                                          permission="application.review")
        return await self.get(guild_id, number)

    async def tick(self, guild_id: int) -> int:
        days = int((await self.settings(guild_id))["archive_after_days"] or 0)
        if not days:
            return 0
        cutoff = (utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return await self.db.execute_rowcount("UPDATE applications SET status = 'archived', updated_at = ? WHERE guild_id = ? "
                                              "AND status IN ('approved','denied') AND updated_at < ?", (iso_now(), guild_id, cutoff))

    async def counts(self, guild_id: int) -> dict:
        rows = await self.db.fetchall("SELECT status, COUNT(*) AS n FROM applications WHERE guild_id = ? GROUP BY status", (guild_id,))
        c = {r["status"]: r["n"] for r in rows}
        c["open"] = sum(c.get(s, 0) for s in OPEN_STATUSES)
        return c


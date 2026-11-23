"""Police reports (private to authorised personnel)."""
from __future__ import annotations

from typing import Optional

from core import events as ev
from core.events import EventBus
from database.connection import Database
from database.models import dumps, iso_now, loads
from security.audit import AuditLog
from security.validation import clean_text

REPORT_STATUSES = {
    "submitted": "Submitted",
    "assigned": "Assigned",
    "in_review": "In Review",
    "supervisor_review": "Supervisor Review Requested",
    "closed": "Closed",
}


class ReportService:
    def __init__(self, db: Database, bus: EventBus, audit: AuditLog):
        self.db = db
        self.bus = bus
        self.audit = audit

    async def create(self, guild_id: int, reporter_id: int, *, rp_name: str, location: str, category: str, occurred_at: str,
                     description: str, suspects: str = "", witnesses: str = "", attachments: Optional[list[str]] = None) -> dict:
        number = await self.db.next_number(guild_id, "report")
        rid = await self.db.execute(
            "INSERT INTO reports(guild_id, number, reporter_id, rp_name, location, category, occurred_at, description, suspects, witnesses, attachments) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, number, reporter_id, clean_text(rp_name, 60) or None, clean_text(location, 150), clean_text(category, 60),
             clean_text(occurred_at, 60), clean_text(description, 2000, required=True, field="Description"),
             clean_text(suspects, 600) or None, clean_text(witnesses, 600) or None, dumps(attachments or [])),
        )
        await self._history(guild_id, rid, reporter_id, "submitted")
        await self.audit.record(guild_id, reporter_id, "report.create", f"report:{number}")
        await self.bus.publish(ev.REPORT_CHANGED, guild_id, report_id=rid, action="created")
        return await self.get(rid)

    async def get(self, report_id: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM reports WHERE id = ?", (report_id,))
        return (dict(row) | {"attachments": loads(row["attachments"], [])}) if row else None

    async def get_by_number(self, guild_id: int, number: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT id FROM reports WHERE guild_id = ? AND number = ?", (guild_id, number))
        return await self.get(row["id"]) if row else None

    async def list(self, guild_id: int, status: Optional[str] = None, limit: int = 20, reporter_id: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM reports WHERE guild_id = ?"
        params: list = [guild_id]
        if status == "open":
            sql += " AND status != 'closed'"
        elif status:
            sql += " AND status = ?"
            params.append(status)
        if reporter_id is not None:
            sql += " AND reporter_id = ?"
            params.append(reporter_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await self.db.fetchall(sql, params)]

    async def set_status(self, report_id: int, actor_id: int, status: str, note: str = "") -> dict:
        if status not in REPORT_STATUSES:
            raise ValueError("Invalid report status")
        rep = await self.get(report_id)
        if rep is None:
            raise ValueError("Report not found")
        await self.db.execute("UPDATE reports SET status = ?, updated_at = ? WHERE id = ?", (status, iso_now(), report_id))
        await self._history(rep["guild_id"], report_id, actor_id, "status", old=rep["status"], new=status, note=note)
        await self.audit.record(rep["guild_id"], actor_id, "report.status", f"report:{rep['number']}", {"status": status})
        await self.bus.publish(ev.REPORT_CHANGED, rep["guild_id"], report_id=report_id, action="status")
        return await self.get(report_id)

    async def assign(self, report_id: int, actor_id: int, assignee_id: int) -> dict:
        rep = await self.get(report_id)
        if rep is None:
            raise ValueError("Report not found")
        new_status = "assigned" if rep["status"] == "submitted" else rep["status"]
        await self.db.execute("UPDATE reports SET assigned_to = ?, status = ?, updated_at = ? WHERE id = ?",
                              (assignee_id, new_status, iso_now(), report_id))
        await self._history(rep["guild_id"], report_id, actor_id, "assigned", assignee=assignee_id)
        await self.audit.record(rep["guild_id"], actor_id, "report.assign", f"report:{rep['number']}", {"assignee": assignee_id})
        await self.bus.publish(ev.REPORT_CHANGED, rep["guild_id"], report_id=report_id, action="assigned")
        return await self.get(report_id)

    async def add_note(self, report_id: int, actor_id: int, note: str) -> None:
        rep = await self.get(report_id)
        if rep is None:
            raise ValueError("Report not found")
        await self._history(rep["guild_id"], report_id, actor_id, "note", note=clean_text(note, 1000, required=True, field="Note"))
        await self.db.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (iso_now(), report_id))
        await self.bus.publish(ev.REPORT_CHANGED, rep["guild_id"], report_id=report_id, action="note")

    async def set_summary(self, report_id: int, summary: str) -> None:
        await self.db.execute("UPDATE reports SET summary = ? WHERE id = ?", (summary, report_id))

    async def history(self, report_id: int) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM report_history WHERE report_id = ? ORDER BY id", (report_id,))
        return [dict(r) | {"detail": loads(r["detail"], {})} for r in rows]

    async def _history(self, guild_id: int, report_id: int, actor_id: int, event_type: str, **detail) -> None:
        await self.db.execute("INSERT INTO report_history(guild_id, report_id, actor_id, event_type, detail) VALUES (?, ?, ?, ?, ?)",
                              (guild_id, report_id, actor_id, event_type, dumps(detail)))


async def summarize_report(ai, guild_id: int, rep: dict) -> Optional[str]:
    """AI summary that must not invent facts or present allegations as proven."""
    if ai is None:
        return None
    from ai.manager import AIUnavailable
    prompt = (
        "Summarize this roleplay police report for an officer in 2-4 neutral sentences. Use only the information given. "
        "Describe allegations as allegations ('the reporter states...'), never as proven facts. Do not add details.\n\n"
        f"Category: {rep.get('category')}\nLocation: {rep.get('location')}\nWhen: {rep.get('occurred_at')}\n"
        f"Description: {rep.get('description')}\nSuspects (as reported): {rep.get('suspects') or 'none given'}\n"
        f"Witnesses (as reported): {rep.get('witnesses') or 'none given'}"
    )
    try:
        resp = await ai.generate(guild_id, purpose="report_summary", system="You write neutral, factual police report summaries.",
                                 prompt=prompt, max_tokens=250, temperature=0.2)
        return resp.text.strip()[:1000]
    except AIUnavailable:
        return None

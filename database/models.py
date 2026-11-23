"""Shared constants and lightweight helpers for database records."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

PRIORITIES = ["low", "routine", "medium", "high", "critical"]
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
PRIORITY_LABEL = {
    "low": "Low",
    "routine": "Routine",
    "medium": "Medium",
    "high": "High",
    "critical": "Critical",
}
PRIORITY_EMOJI = {
    "low": "⚪",
    "routine": "🔵",
    "medium": "🟡",
    "high": "🟠",
    "critical": "🔴",
}

SOURCE_LABEL = {
    "erlc": "ER:LC In-Game 911",
    "discord": "Discord 911 Submission",
    "manual": "Officer Manual Report",
    "test": "TEST DATA (Simulated)",
}
VERIFICATION_LABEL = {
    "verified": "✅ API-VERIFIED",
    "unverified": "⚠️ UNVERIFIED",
    "test": "🧪 TEST DATA",
}

INCIDENT_STATUSES = ["pending", "active", "closed"]

DEFAULT_DEPARTMENTS = [
    # name, short_code, kind
    ("Police Department", "PD", "law"),
    ("Sheriff's Office", "SO", "law"),
    ("State Police", "SP", "law"),
    ("Fire Department", "FD", "fire"),
    ("EMS", "EMS", "ems"),
    ("Dispatch Supervisors", "SUP", "supervisor"),
]

DEFAULT_STATUS_CODES = [
    # code, label, availability, sort
    ("10-8", "Available", "available", 1),
    ("EN-ROUTE", "En Route", "assigned", 2),
    ("10-23", "On Scene", "assigned", 3),
    ("10-15", "Transporting", "assigned", 4),
    ("10-7", "Unavailable", "unavailable", 5),
    ("OOS", "Out of Service", "off", 6),
]

ANNOUNCEMENT_TYPES = {
    "new_911": "New 911 calls",
    "new_report": "New police reports",
    "unit_assignment": "Unit assignments",
    "backup_request": "Backup requests",
    "priority_change": "Incident priority changes",
    "incident_closure": "Incident closures",
    "bolo": "BOLO announcements",
    "department_notice": "Routine department notifications",
}

# Spoken announcement priority (higher = plays first).
ANNOUNCE_PRIORITY = {
    "critical": 100,
    "backup_request": 80,
    "new_911": 60,
    "priority_change": 50,
    "bolo": 40,
    "unit_assignment": 30,
    "incident_closure": 20,
    "new_report": 15,
    "department_notice": 10,
    "voice_reply": 70,
    "test": 5,
}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def discord_ts(value: Optional[str], style: str = "R") -> str:
    """Format an ISO timestamp as a Discord dynamic timestamp."""
    d = parse_iso(value)
    if d is None:
        return "—"
    return f"<t:{int(d.timestamp())}:{style}>"


def loads(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def row_to_dict(row) -> Optional[dict]:
    return dict(row) if row is not None else None

"""Shared helpers for the community platform services (v4.0 Command Center).

Services in this package never import discord.py: Discord side effects go through
`community.actions.DiscordActions`, so the same logic serves slash commands, buttons and
the web dashboard and can be unit-tested without Discord.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from database.models import iso_now, loads, parse_iso, utcnow
from security.validation import ValidationError, clean_text

__all__ = ["ServiceError", "ValidationError", "clean_text", "iso_now", "loads", "parse_iso", "utcnow", "Page",
           "paginate_sql", "parse_duration", "fmt_duration", "iso_in", "safe_format", "row_dicts", "as_int", "parse_date"]


class ServiceError(Exception):
    """A user-facing error (shown as-is in Discord and on the dashboard)."""


@dataclass
class Page:
    items: list
    total: int
    page: int
    per_page: int

    def as_dict(self) -> dict:
        pages = max(1, -(-self.total // self.per_page))
        return {"items": self.items, "total": self.total, "page": self.page, "per_page": self.per_page, "pages": pages}


async def paginate_sql(db, select_sql: str, count_sql: str, params: Iterable[Any], page: int = 1, per_page: int = 25) -> Page:
    page = max(1, int(page or 1))
    per_page = max(1, min(100, int(per_page or 25)))
    params = list(params)
    total = int(await db.fetchval(count_sql, params, 0))
    rows = await db.fetchall(select_sql + " LIMIT ? OFFSET ?", params + [per_page, (page - 1) * per_page])
    return Page([dict(r) for r in rows], total, page, per_page)


def row_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def as_int(value: Any, field: str = "value", *, required: bool = True) -> Optional[int]:
    if value in (None, ""):
        if required:
            raise ValidationError(f"{field} is required.")
        return None
    try:
        return int(str(value).strip().strip("<@!&#>"))
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a number.") from None


_DUR_RE = re.compile(r"(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)", re.I)
_DUR_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: Optional[str], *, default_unit: str = "m", max_seconds: Optional[int] = None) -> int:
    """'10m', '2h 30m', '1d', '90' (minutes) -> seconds."""
    t = (text or "").strip().lower()
    if not t:
        raise ValidationError("A duration is required, e.g. 10m, 2h or 1d.")
    if t.isdigit():
        secs = int(t) * _DUR_MULT[default_unit]
    else:
        parts = _DUR_RE.findall(t)
        if not parts or _DUR_RE.sub("", t).strip(" ,and"):
            raise ValidationError("Use a duration like 10m, 2h, 1d or 1w.")
        secs = sum(int(n) * _DUR_MULT[u[0]] for n, u in parts)
    if secs <= 0:
        raise ValidationError("The duration must be longer than zero.")
    if max_seconds and secs > max_seconds:
        raise ValidationError(f"The maximum duration is {fmt_duration(max_seconds)}.")
    return secs


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m and not d else ""]
    out = " ".join(p for p in parts if p)
    return out or f"{s}s"


def iso_in(seconds: float) -> str:
    return (utcnow() + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_date(value: Optional[str], field: str = "Date") -> str:
    v = (value or "").strip()
    try:
        return dt.date.fromisoformat(v).isoformat()
    except ValueError:
        raise ValidationError(f"{field} must be a date like 2026-12-24.") from None


_PLACEHOLDER = re.compile(r"\{([a-z_]{1,32})\}")


def safe_format(template: Optional[str], values: dict[str, Any], max_len: int = 2000) -> str:
    """Replace {placeholders} with known values only (no attribute access, no format-string tricks)."""
    def sub(m: re.Match) -> str:
        key = m.group(1)
        return str(values[key]) if key in values and values[key] is not None else m.group(0)
    out = _PLACEHOLDER.sub(sub, template or "")
    out = re.sub(r"@(everyone|here)", "@​\\1", out)
    return out[:max_len]

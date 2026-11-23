"""Input validation and sanitising for user-supplied text."""
from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict, deque
from typing import Optional

from database.connection import Database

CALLSIGN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\- ]{0,14}$")
_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ValidationError(ValueError):
    pass


def clean_text(value: Optional[str], max_len: int = 1000, required: bool = False, field: str = "value") -> str:
    text = (value or "").strip()
    text = _CONTROL_RE.sub("", text)
    text = _MENTION_RE.sub(lambda m: "@​" + m.group(1), text)
    text = text.replace("<@&", "<@​&")  # neutralise role mentions
    if required and not text:
        raise ValidationError(f"{field} is required.")
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def validate_callsign(value: str) -> str:
    v = clean_text(value, 15, required=True, field="Callsign").upper()
    if not CALLSIGN_RE.match(v):
        raise ValidationError("Callsign may only contain letters, numbers, spaces and dashes (max 15 characters).")
    return v


def parse_incident_number(value: str) -> int:
    v = (value or "").strip().lstrip("#").upper().replace("INC-", "").replace("INC", "")
    if not v.isdigit():
        raise ValidationError("Incident number must be a number, e.g. 104.")
    return int(v)


def digest(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").strip().lower().encode())
        h.update(b"\x00")
    return h.hexdigest()[:32]


class RateLimiter:
    """Per-key sliding window rate limiter (memory-only; protects against spam, not state)."""

    def __init__(self, max_calls: int, per_seconds: float):
        self.max_calls = max_calls
        self.per = per_seconds
        self._hits: dict[tuple, deque] = defaultdict(deque)

    def check(self, *key) -> Optional[float]:
        """Returns None if allowed, otherwise seconds until the next allowed call."""
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.max_calls:
            return self.per - (now - q[0])
        q.append(now)
        return None


async def is_duplicate_submission(db: Database, guild_id: int, user_id: int, kind: str, dig: str, window: float = 120.0) -> bool:
    """Database-backed duplicate submission protection (survives restarts)."""
    now = time.time()
    await db.execute("DELETE FROM submission_guard WHERE at < ?", (now - 3600,))
    row = await db.fetchone(
        "SELECT at FROM submission_guard WHERE guild_id = ? AND user_id = ? AND kind = ? AND digest = ?",
        (guild_id, user_id, kind, dig),
    )
    if row is not None and now - row["at"] < window:
        return True
    await db.execute(
        "INSERT INTO submission_guard(guild_id, user_id, kind, digest, at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, kind, digest) DO UPDATE SET at = excluded.at",
        (guild_id, user_id, kind, dig, now),
    )
    return False

"""Realistic, varied dispatcher wording.

Every phrase is built only from stored incident facts (number, location, nature,
departments, verification) so nothing is ever invented. Variants rotate per guild
so the dispatcher does not repeat the same sentence back-to-back.

Response styles (per guild, `/settings response-style`):
  professional - full radio phrasing (default)
  concise      - short, minimal phrasing
  detailed     - professional + recommendation details
"""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Optional

STYLES = {"professional": "Professional radio phrasing", "concise": "Short and to the point",
          "detailed": "Professional with full recommendation details"}

_last: dict[tuple[int, str], int] = defaultdict(lambda: -1)

DEPT_SPOKEN = {"law": "law enforcement", "fire": "fire department", "ems": "EMS", "supervisor": "a supervisor"}


def pick(guild_id: Optional[int], key: str, options: list[str]) -> str:
    """Choose a variant, never the same one twice in a row for this guild."""
    if len(options) == 1:
        return options[0]
    k = (guild_id or 0, key)
    choices = [i for i in range(len(options)) if i != _last[k]]
    i = random.choice(choices)
    _last[k] = i
    return options[i]


def _article(nature: str) -> str:
    return "an" if nature[:1].lower() in "aeiou" else "a"


def nature_phrase(inc: dict) -> str:
    nature = (inc.get("nature") or inc.get("category") or "").strip()
    if not nature:
        return "a call for service"
    low = nature.lower()
    if low.startswith("[test data]"):
        low = low.replace("[test data]", "").strip()
    return f"{'a reported' if not low.startswith(('a ', 'an ')) else ''} {low}".strip()


def kinds_for(inc: dict, departments: Optional[list[dict]] = None) -> set[str]:
    from database.models import loads
    codes = loads(inc.get("recommended_departments"), []) or []
    if not departments:
        out = set()
        for c in codes:
            cu = str(c).upper()
            if cu in {"EMS", "MED"}:
                out.add("ems")
            elif cu in {"FD", "FIRE"}:
                out.add("fire")
        return out
    return {d["kind"] for d in departments if d["short_code"] in codes}


def new_call_broadcast(inc: dict, *, style: str = "professional", guild_id: Optional[int] = None,
                       agency: str = "") -> str:
    """Spoken/written broadcast for a new incident. Always contains the incident number and exact location."""
    from database.models import PRIORITY_LABEL, loads
    loc = inc.get("location") or "a location not yet provided"
    nature = nature_phrase(inc)
    num = inc["number"]
    deps = loads(inc.get("recommended_departments"), []) or []
    kinds = kinds_for(inc)
    prio = PRIORITY_LABEL.get(inc.get("priority") or inc.get("ai_priority") or "", "")
    lead = ""
    if inc.get("source") == "test":
        lead = "This is a test. Test data only. "
    elif inc.get("verification") == "unverified":
        lead = "Unverified report. "
    who = f"{agency} to all units" if agency else "Dispatch to all units"
    if style == "concise":
        text = f"{lead}All units, incident {num}, {nature}, {loc}."
        if prio:
            text += f" {prio} priority."
        return text
    opener = pick(guild_id, "new_call_open", [
        f"Attention all available units, respond to {nature} at {loc}.",
        f"{who}, incident {num}: {nature} at {loc}.",
        f"All units, be advised, we have {nature} at {loc}.",
        f"Units in the area, respond to {nature}, location {loc}.",
    ])
    if f"incident {num}" not in opener:
        opener += f" Incident {num}."
    parts = [lead + opener]
    if prio:
        parts.append(pick(guild_id, "new_call_prio", [f"{prio} priority.", f"Priority {prio.lower()}.", f"Treat as {prio.lower()} priority."]))
    if "ems" in kinds:
        parts.append(pick(guild_id, "ems", ["EMS assistance may be required.", "EMS requested to the scene.",
                                            "Medical units, be advised, EMS is recommended."]))
    if "fire" in kinds:
        parts.append(pick(guild_id, "fire", ["Fire department requested.", "Fire units respond."]))
    if style == "detailed" and inc.get("recommended_units"):
        n = inc["recommended_units"]
        parts.append(f"Recommending {n} unit{'s' if n != 1 else ''}" + (f", {', '.join(deps)}." if deps else "."))
    if inc.get("supervisor_recommended"):
        parts.append("Supervisor notification recommended.")
    if not inc.get("location"):
        parts.append("Location unavailable, caller information is limited.")
    parts.append(pick(guild_id, "new_call_close", ["Advise your status.", "Units responding, advise.",
                                                   "First available unit, advise when en route.",
                                                   "Show yourselves en route on the call."]))
    return " ".join(p for p in parts if p)


def call_received_reply(inc: dict, *, style: str = "professional", guild_id: Optional[int] = None) -> str:
    """Reply to the person who reported the incident in text."""
    loc = inc.get("location") or "location unknown"
    nature = (inc.get("nature") or "call for service").lower()
    if style == "concise":
        return f"Copy. Incident #{inc['number']} created: {nature}, {loc}."
    return pick(guild_id, "received", [
        f"Copy, 911 call received. Incident #{inc['number']}, {nature} at {loc}. Units are being notified.",
        f"Understood. I've opened incident #{inc['number']} for {_article(nature)} {nature} at {loc} and alerted available units.",
        f"10-4. Incident #{inc['number']} is entered: {nature}, {loc}. Stay on the line if anything changes.",
    ])


def duplicate_reply(inc: dict, *, guild_id: Optional[int] = None) -> str:
    loc = inc.get("location") or "the same location"
    return pick(guild_id, "dup", [
        f"Copy, that matches active incident #{inc['number']} at {loc}. I've added your information to the call.",
        f"Already working that one: incident #{inc['number']}, {loc}. Your details were added to the call.",
        f"10-4, units are already assigned to incident #{inc['number']} at {loc}. Information added.",
    ])


def ack_status(callsign: str, code: str, label: str, guild_id: Optional[int] = None) -> str:
    label = (label or "").strip()
    return pick(guild_id, "ack_status", [
        f"{callsign}, copy, showing you {code}" + (f", {label}." if label else "."),
        f"10-4 {callsign}, {code}" + (f", {label}." if label else "."),
        f"Copy {callsign}, you're {code}" + (f", {label}." if label else "."),
    ])


def ack_attach(callsign: str, inc: dict, guild_id: Optional[int] = None) -> str:
    loc = inc.get("location") or "location unavailable"
    return pick(guild_id, "ack_attach", [
        f"{callsign}, copy, attached to incident {inc['number']}, {loc}.",
        f"10-4 {callsign}, you're on incident {inc['number']}, {loc}.",
        f"Copy {callsign}, showing you assigned to incident {inc['number']} at {loc}.",
    ])


def ask_again(guild_id: Optional[int] = None) -> str:
    return pick(guild_id, "say_again", [
        "Dispatch, say again? I didn't copy that.",
        "Unable to copy, repeat your traffic.",
        "Say again? Give me the nature and the location.",
    ])

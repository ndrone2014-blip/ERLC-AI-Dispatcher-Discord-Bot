"""Resource recommendations.

`rule_based_assessment` is a deterministic keyword assessor used when no AI
provider is available (and as a sanity baseline). `normalize_assessment`
validates any assessment (AI or rules) against the server's real configuration:
it never lets a recommendation reference a department that does not exist.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from database.models import PRIORITIES, PRIORITY_RANK

# (pattern, category, priority, units, kinds needed, supervisor?)
RULES: list[tuple[str, str, str, int, set[str], bool]] = [
    (r"active shooter|shots? fired|shooting|gunshots?|being shot|\bshot\b", "Shots fired / shooting", "critical", 4, {"law", "ems"}, True),
    (r"hostage|kidnap", "Hostage / kidnapping", "critical", 4, {"law", "ems"}, True),
    (r"stab(bed|bing)?|knife attack", "Stabbing", "high", 3, {"law", "ems"}, True),
    (r"\b(gun|armed|weapon|firearm|pistol|rifle)\b", "Armed subject", "high", 3, {"law"}, True),
    (r"bank robbery|robbing the bank|heist|jewel(le)?ry (store )?robbery", "Major robbery", "high", 3, {"law"}, True),
    (r"robbery|robbing|robbed|hold ?up|mugg", "Robbery", "high", 2, {"law"}, False),
    (r"pursuit|chase|fleeing|evading|running from (the )?police", "Pursuit / fleeing subject", "high", 3, {"law"}, True),
    (r"explosion|explosive|bomb", "Explosion / explosive", "critical", 3, {"law", "fire", "ems"}, True),
    (r"(house|building|car|vehicle|structure|brush|forest|wild)\s*fire|on fire|burning|smoke|flames", "Fire", "high", 1, {"fire", "ems", "law"}, False),
    (r"(multi(ple)?|several|many|\d+)[\s-]*(car|vehicle)s?.{0,20}(crash|accident|collision|pile)", "Multi-vehicle collision", "high", 2, {"law", "fire", "ems"}, False),
    (r"crash|accident|collision|wreck|hit and run|t-?boned|rear[- ]ended", "Traffic collision", "medium", 1, {"law"}, False),
    (r"unconscious|not breathing|cardiac|heart attack|overdose|seizure|bleeding|injur|hurt|wounded|medical", "Medical emergency", "high", 1, {"ems", "law"}, False),
    (r"fight|assault|attack(ed|ing)?|beating|brawl|domestic", "Assault / disturbance", "medium", 2, {"law"}, False),
    (r"break[- ]?in|burglar|breaking into", "Burglary", "medium", 2, {"law"}, False),
    (r"stolen|theft|steal|shoplift|carjack", "Theft", "routine", 1, {"law"}, False),
    (r"reckless|speeding|drunk|dui|swerving", "Reckless / impaired driver", "medium", 1, {"law"}, False),
    (r"vandal|graffiti|trespass", "Vandalism / trespass", "routine", 1, {"law"}, False),
    (r"suspicious|loiter", "Suspicious activity", "routine", 1, {"law"}, False),
    (r"noise|parking|blocking|abandoned", "Minor complaint", "low", 1, {"law"}, False),
]

ESCALATORS = [
    (r"multiple (people|victims|suspects|injur)|several (people|victims|suspects)|mass casualty", 1, 1),
    (r"(officer|cop|deputy|trooper) (down|shot|injured|hurt)", 2, 2),
    (r"\b(gun|armed|weapon)\b", 1, 1),
]


def rule_based_assessment(text: str, assigned_count: int = 0, requested_department: Optional[str] = None) -> dict:
    t = (text or "").lower()
    match = None
    for pattern, category, prio, units, kinds, sup in RULES:
        if re.search(pattern, t):
            match = (category, prio, units, set(kinds), sup)
            break
    if match is None:
        category, prio, units, kinds, sup = ("Unclassified call", "routine", 1, {"law"}, False)
        uncertain = True
    else:
        category, prio, units, kinds, sup = match
        uncertain = False
    for pattern, prio_bump, unit_bump in ESCALATORS:
        if re.search(pattern, t) and category not in {"Shots fired / shooting", "Hostage / kidnapping"}:
            prio = PRIORITIES[min(len(PRIORITIES) - 1, PRIORITY_RANK[prio] + prio_bump)]
            units += unit_bump
    if PRIORITY_RANK[prio] >= PRIORITY_RANK["high"] and units >= 3:
        sup = True
    if not t.strip():
        uncertain = True
    reasoning = (
        f"Keyword assessment classified the call as '{category}'."
        if not uncertain else "The reported description is unclear; defaulting to a routine single-unit response until more information is available."
    )
    return {
        "category": category,
        "priority": prio,
        "recommended_units": units,
        "department_kinds": sorted(kinds),
        "departments": [],
        "supervisor_notification": sup,
        "reasoning": reasoning,
        "uncertain": uncertain,
        "source": "rules",
        "requested_department": requested_department,
        "assigned_at_assessment": assigned_count,
    }


def _resolve_departments(raw: Any, kinds: list[str], departments: list[dict], requested: Optional[str]) -> list[str]:
    """Map AI/rule output onto configured department short codes only."""
    active = [d for d in departments if d.get("active", 1)]
    by_code = {d["short_code"].upper(): d for d in active}
    by_name = {d["name"].lower(): d for d in active}
    out: list[str] = []

    def add(code: str):
        if code not in out:
            out.append(code)

    for item in raw or []:
        s = str(item).strip()
        if s.upper() in by_code:
            add(by_code[s.upper()]["short_code"])
        elif s.lower() in by_name:
            add(by_name[s.lower()]["short_code"])
        else:
            for k in ("law", "fire", "ems", "supervisor"):
                if s.lower() in {k, {"law": "police", "fire": "fire department", "ems": "medical", "supervisor": "supervisors"}[k]}:
                    kinds = list(kinds) + [k]
    if requested and requested.upper() in by_code:
        add(by_code[requested.upper()]["short_code"])
    for kind in kinds:
        if any(by_code[c]["kind"] == kind for c in out):
            continue
        for d in active:
            if d["kind"] == kind:
                add(d["short_code"])
                break
    return out


def normalize_assessment(raw: dict, departments: list[dict], *, assigned_count: int, available_by_kind: dict[str, int],
                         requested_department: Optional[str] = None, source: str = "ai") -> dict:
    prio = str(raw.get("priority", "routine")).strip().lower()
    if prio not in PRIORITIES:
        prio = {"p1": "critical", "p2": "high", "p3": "medium", "p4": "routine", "p5": "low", "emergency": "critical",
                "urgent": "high", "normal": "routine", "moderate": "medium"}.get(prio, "routine")
    try:
        units = int(raw.get("recommended_units", 1))
    except (TypeError, ValueError):
        units = 1
    units = max(1, min(units, 10))
    kinds = [k for k in raw.get("department_kinds", []) if k in {"law", "fire", "ems", "supervisor"}]
    if raw.get("supervisor_notification"):
        kinds.append("supervisor")
    deps = _resolve_departments(raw.get("departments"), kinds, departments, requested_department)
    additional = max(0, units - assigned_count)
    reasoning = str(raw.get("reasoning") or "").strip()[:600]
    notes = []
    if assigned_count:
        notes.append(f"{assigned_count} unit(s) already assigned; {additional} additional recommended.")
    kinds_needed = {d["kind"] for d in departments if d["short_code"] in deps}
    for kind in sorted(kinds_needed):
        if kind in available_by_kind and available_by_kind[kind] == 0:
            label = {"law": "law-enforcement", "fire": "fire", "ems": "EMS", "supervisor": "supervisor"}.get(kind, kind)
            notes.append(f"No {label} units currently available.")
    return {
        "category": str(raw.get("category") or "Unclassified call")[:80],
        "priority": prio,
        "recommended_units": units,
        "additional_units": additional,
        "departments": deps,
        "supervisor_notification": bool(raw.get("supervisor_notification")) or any(
            d["kind"] == "supervisor" and d["short_code"] in deps for d in departments),
        "reasoning": reasoning or "No reasoning provided.",
        "availability_notes": notes,
        "uncertain": bool(raw.get("uncertain", False)),
        "source": source,
    }

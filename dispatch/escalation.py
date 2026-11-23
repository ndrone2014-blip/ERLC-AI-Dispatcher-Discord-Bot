"""Decide whether a reassessment is a *significant* change worth announcing."""
from __future__ import annotations

from typing import Optional

from database.models import PRIORITY_RANK, loads


def compare_assessments(old: dict, new: dict) -> Optional[dict]:
    """Return a description of a significant escalation, or None.

    `old`/`new` are incident rows (before/after apply_assessment).
    Significant = recommended priority went up, two or more extra units, a new
    department/resource type, or supervisor notification newly recommended.
    De-escalations and small changes are recorded in history but not announced.
    """
    if old.get("ai_priority") is None:
        return None  # first assessment, not an escalation
    reasons = []
    old_p = PRIORITY_RANK.get(old.get("ai_priority") or "routine", 1)
    new_p = PRIORITY_RANK.get(new.get("ai_priority") or "routine", 1)
    if new_p > old_p:
        reasons.append(f"priority raised from {old.get('ai_priority')} to {new.get('ai_priority')}")
    old_u = old.get("recommended_units") or 0
    new_u = new.get("recommended_units") or 0
    if new_u - old_u >= 2:
        reasons.append(f"recommended units increased from {old_u} to {new_u}")
    old_d = set(loads(old.get("recommended_departments"), []) or [])
    new_d = set(loads(new.get("recommended_departments"), []) or [])
    added = sorted(new_d - old_d)
    if added:
        reasons.append("additional resources recommended: " + ", ".join(added))
    if new.get("supervisor_recommended") and not old.get("supervisor_recommended"):
        reasons.append("supervisor notification now recommended")
    if not reasons:
        return None
    return {"reasons": reasons, "priority_up": new_p > old_p, "added_departments": added,
            "old_priority": old.get("ai_priority"), "new_priority": new.get("ai_priority")}

"""Incident assessment and announcement wording.

The AI only *recommends*. Its output is validated by `normalize_assessment`
against real configuration, and announcement wording is accepted only if it
still contains the real incident facts (otherwise a template is used).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from database.models import PRIORITY_LABEL, loads
from dispatch.recommendations import normalize_assessment, rule_based_assessment

from .manager import AIManager, AIUnavailable
from .providers.base import ProviderError, extract_json

log = logging.getLogger(__name__)

DISPATCHER_PERSONA = (
    "You are the AI dispatcher for a Roblox Emergency Response: Liberty County (ER:LC) ROLEPLAY community on Discord. "
    "This is a game; you are not a real emergency service and must never claim to be. "
    "You speak like a calm, trained radio dispatcher: concise, professional, plain language, configured 10-codes. "
    "You never invent facts, locations, units, departments, suspects or call details. "
    "If information is missing or unclear, you say so."
)

ASSESS_INSTRUCTIONS = """Assess this roleplay 911 incident and recommend a response. Use ONLY the information given.
Return a single JSON object with these keys:
  "category": short incident type (e.g. "Traffic collision"),
  "priority": one of "low","routine","medium","high","critical",
  "recommended_units": integer TOTAL units appropriate for the incident (1-10),
  "department_kinds": list drawn from ["law","fire","ems","supervisor","special"] (special = SWAT/K9/air units),
  "departments": list of department short codes chosen ONLY from the configured list below,
  "supervisor_notification": true/false,
  "uncertain": true if the description is vague or unconfirmed,
  "reasoning": 1-3 sentences explaining the recommendation, mentioning units already assigned if relevant.
Consider: severity, people involved, injuries, immediate threats, whether other departments are needed, signs of escalation,
units already assigned and units currently available. Do not recommend more units than needed; if already assigned units are
sufficient, say so in the reasoning."""


def _incident_facts(inc: dict, notes: list[str]) -> str:
    lines = [
        f"Incident number: #{inc['number']}",
        f"Source: {inc['source']} (verification: {inc['verification']})",
        f"Reported nature: {inc.get('nature') or 'not provided'}",
        f"Description: {inc.get('description') or 'not provided'}",
        f"Location: {inc.get('location') or 'not provided'}",
    ]
    if inc.get("postal"):
        lines.append(f"Postal: {inc['postal']}")
    if inc.get("requested_department"):
        lines.append(f"Caller requested: {inc['requested_department']}")
    if inc.get("erlc_team"):
        lines.append(f"In-game call team: {inc['erlc_team']}")
    if notes:
        lines.append("Updates/notes from units (most recent last):")
        lines += [f"  - {n}" for n in notes[-8:]]
    return "\n".join(lines)


async def assess_incident(ai: Optional[AIManager], guild_id: int, inc: dict, *, departments: list[dict],
                          assigned_units: list[dict], availability: dict, notes: list[str],
                          custom: Optional[list[dict]] = None) -> dict:
    available_by_kind: dict[str, int] = {}
    for d in departments:
        bucket = availability.get("by_department", {}).get(d["short_code"], {})
        available_by_kind[d["kind"]] = available_by_kind.get(d["kind"], 0) + bucket.get("available", 0)
    assigned_count = len(assigned_units)
    dept_list = ", ".join(f"{d['short_code']} = {d['name']} ({d['kind']})" for d in departments) or "none configured"

    if ai is not None:
        prompt = (
            f"{ASSESS_INSTRUCTIONS}\n\nConfigured departments: {dept_list}\n"
            f"Units already assigned to this incident: {assigned_count} "
            f"({', '.join(u['callsign'] for u in assigned_units) or 'none'})\n"
            f"Available units by department type: {available_by_kind}\n"
            + (("Server-specific incident categories: " + "; ".join(
                f"{c.get('category')} ({c.get('priority')}) when the call mentions {', '.join(c.get('keywords') or [])}"
                for c in custom) + "\n") if custom else "")
            + f"\nINCIDENT:\n{_incident_facts(inc, notes)}"
        )
        try:
            resp = await ai.generate(guild_id, purpose="assessment", system=DISPATCHER_PERSONA, prompt=prompt,
                                     json_mode=True, max_tokens=500, temperature=0.2)
            raw = extract_json(resp.text)
            result = normalize_assessment(raw, departments, assigned_count=assigned_count, available_by_kind=available_by_kind,
                                          requested_department=inc.get("requested_department"), source=f"ai:{resp.provider}")
            result["model"] = resp.model
            return result
        except (AIUnavailable, ProviderError) as e:
            log.info("AI assessment unavailable for incident #%s, using rules: %s", inc["number"], e)

    text = " ".join(filter(None, [inc.get("nature"), inc.get("description")] + notes[-5:]))
    raw = rule_based_assessment(text, assigned_count, inc.get("requested_department"), custom=custom)
    return normalize_assessment(raw, departments, assigned_count=assigned_count, available_by_kind=available_by_kind,
                                requested_department=inc.get("requested_department"), source="rules")


# ---------------------------------------------------------------------- announcement wording
def _spoken_location(inc: dict) -> str:
    loc = inc.get("location")
    if not loc:
        return "Location unavailable"
    return loc


def template_new_call(inc: dict, style: str = "professional", agency: str = "") -> str:
    """Deterministic announcement built only from stored incident fields (varied realistic wording)."""
    from dispatch.phrases import new_call_broadcast
    return new_call_broadcast(inc, style=style, guild_id=inc.get("guild_id"), agency=agency)


def _contains_facts(text: str, inc: dict) -> bool:
    t = text.lower()
    if str(inc["number"]) not in t:
        return False
    loc = (inc.get("location") or "").lower()
    if loc:
        # Every meaningful token of the real location must be present.
        tokens = [w for w in re.findall(r"[a-z0-9]+", loc) if len(w) > 2 or w.isdigit()]
        if not all(tok in t for tok in tokens):
            return False
    elif "unavailable" not in t and "unknown" not in t:
        return False
    if inc["source"] == "test" and "test" not in t:
        return False
    if inc["verification"] == "unverified" and "unverified" not in t:
        return False
    return True


async def announcement_for_new_call(ai: Optional[AIManager], guild_id: int, inc: dict, use_ai: bool = True,
                                    style: str = "professional", agency: str = "") -> tuple[str, str]:
    """Return (text, source). AI wording is only used if it preserves every real fact."""
    template = template_new_call(inc, style, agency)
    if ai is None or not use_ai:
        return template, "template"
    prompt = (
        "Rewrite this dispatcher radio announcement so it sounds natural and professional when read aloud. "
        "Keep it under 70 words. Keep the incident number, the exact location and the nature of call exactly as given. "
        "Keep any 'test' or 'unverified' warning. Do not add any facts. Reply with the announcement text only.\n\n"
        f"ANNOUNCEMENT:\n{template}"
    )
    try:
        resp = await ai.generate(guild_id, purpose="announcement", system=DISPATCHER_PERSONA, prompt=prompt,
                                 max_tokens=200, temperature=0.6)
    except AIUnavailable:
        return template, "template"
    text = resp.text.strip().strip('"')
    if len(text) > 600 or not _contains_facts(text, inc):
        log.info("Discarded AI announcement wording for incident #%s (facts not preserved)", inc["number"])
        return template, "template"
    return text, f"ai:{resp.provider}"


def escalation_text(inc: dict, change: dict) -> str:
    prio = PRIORITY_LABEL.get(inc.get("ai_priority") or "", "updated")
    deps = loads(inc.get("recommended_departments"), []) or []
    text = (f"Dispatch update on incident {inc['number']}, {inc.get('location') or 'location unavailable'}. "
            f"Assessment updated to {prio} priority. ")
    if inc.get("recommended_units"):
        text += f"Now recommending {inc['recommended_units']} units"
        text += (", with " + ", ".join(deps) + ". " if deps else ". ")
    if inc.get("supervisor_recommended"):
        text += "Supervisor notification recommended. "
    if inc.get("priority_status") == "recommended" and inc.get("priority") != inc.get("ai_priority"):
        text += "Awaiting supervisor priority approval."
    return text.strip()

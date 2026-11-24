"""Natural-language understanding for officer requests (voice or text).

Clear, common phrases are parsed deterministically (fast, no AI quota used).
Anything else is sent to the AI, which may ONLY choose from a fixed intent list;
the dispatch core then executes the intent against the database.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from .manager import AIManager, AIUnavailable
from .providers.base import ProviderError, extract_json

log = logging.getLogger(__name__)

INTENTS = {
    "status_change": "Officer changes their own unit status. args: code",
    "attach": "Attach the speaker's unit to an incident. args: incident_number",
    "detach": "Clear the speaker's unit from an incident. args: incident_number (optional = current)",
    "backup": "Request backup for the speaker. args: urgency (routine|urgent|emergency), note",
    "add_note": "Give new information/update about an incident. args: incident_number (optional = current), text",
    "query_highest": "Ask which call has the highest priority.",
    "query_active": "Ask what calls are active.",
    "query_incident_units": "Ask how many/which units are on an incident. args: incident_number (optional = current)",
    "query_incident": "Ask for details of an incident. args: incident_number (optional = current)",
    "query_available": "Ask which units are available.",
    "query_bolos": "Ask about active BOLOs.",
    "question": "Any other dispatch-related question. args: text",
    "unclear": "The request is ambiguous or not a dispatch request.",
}

NUMBER_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20",
    "twenty-three": "23", "twenty three": "23",
}

WAKE_WORDS = ("dispatcher", "dispatch", "central", "control")


@dataclass
class Intent:
    name: str
    args: dict = field(default_factory=dict)
    confidence: float = 1.0
    source: str = "rules"
    clarification: Optional[str] = None


# Common speech-to-text mishearings of radio words (Whisper output varies with accents/radio quality).
MISHEARD = [
    (r"\b(?:this|the|his|dis|da)[\s-]?patch(?:es|ed)?\b", "dispatch"),
    (r"\bdispatch(?:'s|es)\b", "dispatch"),
    (r"\b(?:attachee|attachy|attached|attaching|atach|a tach|a-tach|attatch)\b", "attach"),
    (r"\b(?:attack|a touch|touch)(?= (?:me|us|my unit)\b)", "attach"),
    (r"\b(?:avable|avaliable|availible|avalible|availabe|avail|available's|avialable)\b", "available"),
    (r"\bten[\s-]?ate\b", "10-8"),
    (r"\bin route\b", "en route"),
    (r"\bback-up\b", "backup"),
]


def normalize_speech(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"[‐-―]", "-", t)
    t = re.sub(r"[.!?]+$", "", t)
    for pattern, repl in MISHEARD:
        t = re.sub(pattern, repl, t)
    t = re.sub(r"\bten[\s-]+(twenty[\s-]+three|twenty|fifteen|eight|seven|six|four|five|nine|one|two|three)\b",
               lambda m: "10-" + NUMBER_WORDS.get(m.group(1).replace(" ", "-"), NUMBER_WORDS.get(m.group(1), m.group(1))), t)
    t = re.sub(r"\b10[\s.]+(\d{1,2})\b", r"10-\1", t)
    t = re.sub(r"\s+", " ", t)

    def _code(m: re.Match) -> str:  # "108" -> "10-8", but never "incident 104" -> "incident 10-4"
        prev = t[: m.start()].rstrip().split(" ")[-1] if t[: m.start()].strip() else ""
        if prev.strip("#") in {"incident", "call", "number", "inc", "postal", "no", ""} or prev.endswith("#"):
            if prev == "":
                return "10-" + m.group(1)
            return m.group(0)
        return "10-" + m.group(1)

    t = re.sub(r"(?<![\w#-])10(23|15|97|8|7|6)(?![\w-])", _code, t)
    return t


def strip_wake_word(text: str) -> tuple[bool, str]:
    t = normalize_speech(text)
    m = re.match(r"^\W*(?:hey |ok |okay )?(%s)\b[\s,.:!-]*(.*)$" % "|".join(WAKE_WORDS), t)
    if m:
        return True, m.group(2).strip()
    return False, t


def _incident_number(t: str) -> Optional[int]:
    m = re.search(r"(?:incident|call|inc|number|#)\s*(?:number\s*)?#?\s*(\d{1,6})", t)
    if m:
        return int(m.group(1))
    return None


def parse_rules(text: str, status_codes: list[dict]) -> Optional[Intent]:
    t = normalize_speech(text)
    if not t:
        return Intent("unclear", clarification="I didn't catch that.")
    num = _incident_number(t)

    # Questions first, so "show me available units" is not read as "mark me available".
    if re.search(r"\b(available|10-8) (units|officers)\b", t) or re.search(r"\bwho('?s| is) available\b", t):
        return Intent("query_available")
    if re.search(r"\bhow many (units|officers)\b", t) or re.search(r"\bwho('?s| is)? (on|assigned to)\b", t):
        return Intent("query_incident_units", {"incident_number": num} if num else {})

    # Status codes - match configured codes and labels
    for sc in status_codes:
        code = sc["code"].lower()
        label = sc["label"].lower()
        patterns = [rf"(?<![\w-]){re.escape(code)}(?![\w-])", rf"\b(i'?m|i am|show me|put me|mark me|set me)\b.*\b{re.escape(label)}\b"]
        if code == "en-route":
            patterns.append(r"\b(en ?route|on my way|responding)\b")
        if code == "10-23":
            patterns.append(r"\b(on scene|arrived|on location)\b")
        if code == "oos":
            patterns.append(r"\bout of service\b")
        if code == "10-15":
            patterns.append(r"\btransporting\b")
        if any(re.search(p, t) for p in patterns):
            if code in {"en-route", "10-23"} and re.search(r"\b(attach|to (?:the |that |this )?(?:incident|call))\b", t):
                break  # "en route to incident 104" / "en route to the call" -> attach
            return Intent("status_change", {"code": sc["code"]})

    if (re.search(r"\b(attach|assign|put|add)\b.*\b(me|us|my unit)\b", t)
            or re.search(r"\b(en ?route|responding|show me on|put me on|i'?ll take)\b.*\b(incident|call)\b", t)):
        # No number ("attach me to the call") -> resolved against the database by the executor.
        return Intent("attach", {"incident_number": num} if num is not None else {"latest": True})
    if re.search(r"\b(clear|cleared|detach|remove me|done)\b.*\b(incident|call|scene)\b", t) or re.search(r"\bi'?m clear\b", t):
        return Intent("detach", {"incident_number": num} if num else {})
    if re.search(r"\b(backup|back up|assistance|additional units|need units|cover)\b", t) and not re.search(r"\b(how many|which|what)\b", t):
        urgency = "emergency" if re.search(r"\b(emergency|urgent|asap|shots|officer down|10-33|code 3)\b", t) else "routine"
        return Intent("backup", {"urgency": urgency, "note": text.strip()[:200]})
    if re.search(r"\b(highest|top|most urgent|most serious)\b.*\b(priority|call|incident)\b", t):
        return Intent("query_highest")
    if re.search(r"\b(what|which|any|list)\b.*\b(calls|incidents)\b.*\b(active|open|pending|out)\b", t) or re.search(r"\bactive (calls|incidents)\b", t):
        return Intent("query_active")
    if re.search(r"\bhow many (units|officers)\b", t) or re.search(r"\bwho('?s| is)? (on|assigned to)\b", t):
        return Intent("query_incident_units", {"incident_number": num} if num else {})
    if re.search(r"\b(available|10-8) units\b", t) or re.search(r"\bwho('?s| is) available\b", t) or re.search(r"\bshow me available\b", t):
        return Intent("query_available")
    if re.search(r"\bbolo", t):
        return Intent("query_bolos")
    if re.search(r"\b(details|info|information) (on|for|about)\b", t) and num:
        return Intent("query_incident", {"incident_number": num})
    return None


async def parse_with_ai(ai: AIManager, guild_id: int, text: str, status_codes: list[dict], context: list[str],
                        current_incident: Optional[int]) -> Intent:
    codes = ", ".join(f"{s['code']} ({s['label']})" for s in status_codes)
    intent_list = "\n".join(f"- {k}: {v}" for k, v in INTENTS.items())
    prompt = (
        "Classify the officer's radio transmission to dispatch. Choose exactly one intent from the list. "
        "Never invent incident numbers: only use a number the officer said, otherwise leave it out.\n"
        f"Intents:\n{intent_list}\nConfigured status codes: {codes}\n"
        f"Officer's current incident: {('#' + str(current_incident)) if current_incident else 'none'}\n"
        f"Recent conversation:\n" + ("\n".join(context[-6:]) or "(none)") +
        f"\n\nTransmission: \"{text}\"\n\n"
        'Reply as JSON: {"intent": "...", "args": {...}, "confidence": 0-1, "clarification": "question to ask if unclear or null"}'
    )
    try:
        resp = await ai.generate(guild_id, purpose="intent", system="You convert radio transmissions into structured dispatch intents.",
                                 prompt=prompt, json_mode=True, max_tokens=200, temperature=0.0)
        data = extract_json(resp.text)
    except (AIUnavailable, ProviderError) as e:
        log.info("AI intent parsing unavailable: %s", e)
        return Intent("unclear", source="none", clarification="Dispatch copies, but I couldn't process that request right now. Please use the Discord panel.")
    name = str(data.get("intent", "unclear"))
    if name not in INTENTS:
        name = "unclear"
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    # Do not trust an incident number the officer did not actually say.
    said = _incident_number(normalize_speech(text))
    if "incident_number" in args and args.get("incident_number") is not None:
        try:
            n = int(args["incident_number"])
        except (TypeError, ValueError):
            n = None
        args["incident_number"] = n if n is not None and (n == said or str(n) in text) else None
        if args["incident_number"] is None:
            args.pop("incident_number")
    if name == "status_change":
        valid = {s["code"].upper(): s["code"] for s in status_codes}
        code = str(args.get("code", "")).upper()
        if code not in valid:
            return Intent("unclear", source="ai", clarification="Which status code? Say for example 10-8 or 10-7.")
        args["code"] = valid[code]
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    clar = data.get("clarification") if isinstance(data.get("clarification"), str) else None
    if conf < 0.45 and name not in {"question", "unclear"}:
        return Intent("unclear", args, conf, "ai", clar or "Dispatch, say again? Please repeat your request.")
    return Intent(name, args, conf, "ai", clar if name == "unclear" else None)


class ConversationMemory:
    """Short rolling conversational context per officer (NOT authoritative state)."""

    def __init__(self, max_turns: int = 10):
        self._turns: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=max_turns))

    def add(self, guild_id: int, user_id: int, speaker: str, text: str) -> None:
        self._turns[(guild_id, user_id)].append(f"{speaker}: {text}")

    def get(self, guild_id: int, user_id: int) -> list[str]:
        return list(self._turns[(guild_id, user_id)])


async def answer_question(ai: AIManager, guild_id: int, question: str, situation: str, context: list[str]) -> str:
    prompt = (
        "Answer the officer's question using ONLY the dispatch database snapshot below. If the answer is not in the snapshot, "
        "say dispatch does not have that information. Answer in at most 3 short radio-style sentences.\n\n"
        f"DATABASE SNAPSHOT:\n{situation}\n\nRecent conversation:\n" + ("\n".join(context[-6:]) or "(none)") +
        f"\n\nQuestion: {question}"
    )
    from .assessment import DISPATCHER_PERSONA
    resp = await ai.generate(guild_id, purpose="question", system=DISPATCHER_PERSONA, prompt=prompt, max_tokens=220, temperature=0.3)
    return resp.text.strip()

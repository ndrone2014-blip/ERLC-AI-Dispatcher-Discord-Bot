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
    "self_initiated": ("Officer starts their own activity. args: kind (traffic_stop|subject_stop|pursuit|foot_pursuit), "
                       "location (only if said), plate (only if said), detail"),
    "question": "Any other dispatch-related question. args: text",
    "unclear": "The request is ambiguous or not a dispatch request.",
}

NUMBER_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20",
    "twenty-three": "23", "twenty three": "23", "eighty": "80",
}

# Officer-initiated activity: kind -> (spoken label, phrase pattern)
SELF_INITIATED = {
    "traffic_stop": ("traffic stop", r"\b(traffic stop|car stop|vehicle stop|t-stop|10-11)\b"),
    "foot_pursuit": ("foot pursuit", r"\b(foot pursuit|foot chase|on foot after|running after)\b"),
    "pursuit": ("vehicle pursuit", r"\b(pursuit|10-80|in a chase|chasing|vehicle fleeing|failing to stop|fail to stop)\b"),
    "subject_stop": ("subject stop", r"\b(subject stop|pedestrian stop|ped stop|stopping a subject|10-12)\b"),
}

_PLATE_RE = re.compile(r"\b(?:plate|tag|license(?: plate)?)(?: number)?(?: is| of|:)?\s+([a-z0-9][a-z0-9 -]{1,60}?)(?=,|\.|$|\s+(?:at|on|near|heading|going)\b)")

# Phonetic alphabets used on police radio (APCO "Adam, Boy..." and NATO "Alpha, Bravo...").
PHONETIC = {
    "adam": "A", "alpha": "A", "boy": "B", "bravo": "B", "charles": "C", "charlie": "C", "david": "D", "delta": "D",
    "edward": "E", "echo": "E", "frank": "F", "foxtrot": "F", "george": "G", "golf": "G", "henry": "H", "hotel": "H",
    "ida": "I", "india": "I", "john": "J", "juliet": "J", "king": "K", "kilo": "K", "lincoln": "L", "lima": "L",
    "mary": "M", "mike": "M", "nora": "N", "november": "N", "ocean": "O", "oscar": "O", "paul": "P", "papa": "P",
    "queen": "Q", "quebec": "Q", "robert": "R", "romeo": "R", "sam": "S", "sierra": "S", "tom": "T", "tango": "T",
    "union": "U", "uniform": "U", "victor": "V", "william": "W", "whiskey": "W", "x-ray": "X", "xray": "X",
    "young": "Y", "yankee": "Y", "zebra": "Z", "zulu": "Z",
}
DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
               "seven": "7", "eight": "8", "nine": "9", "niner": "9"}


def spoken_plate(raw: str) -> Optional[str]:
    """'adam boy charles 1 2 3' / 'abc 123' / 'a b c one two three' -> 'ABC123'."""
    out = []
    for tok in re.split(r"[\s-]+", raw.lower()):
        if not tok:
            continue
        if tok in PHONETIC:
            out.append(PHONETIC[tok])
        elif tok in DIGIT_WORDS:
            out.append(DIGIT_WORDS[tok])
        elif re.fullmatch(r"[a-z0-9]{1,8}", tok):
            out.append(tok.upper())
        else:
            break
    plate = "".join(out)
    return plate if 2 <= len(plate) <= 8 else None
_LOCATION_RE = re.compile(r"\b(?:at|on|near|by|in front of|outside|around)\s+(?!a\b|an\b|the stop\b|traffic\b|foot\b)(.+?)"
                          r"(?=,|\.|\bplate\b|\btag\b|\bwith\b|\bheading\b|\bin a\b|$)")


def _self_initiated(t: str) -> Optional[Intent]:
    for kind, (label, pattern) in SELF_INITIATED.items():
        m = re.search(pattern, t)
        if not m:
            continue
        rest = t[m.end():]
        loc = _LOCATION_RE.search(rest) or _LOCATION_RE.search(t[: m.start()].replace("show me on", "").replace("i'm on", ""))
        plate = _PLATE_RE.search(t)
        location = loc.group(1).strip(" ,.") if loc else None
        if location and re.fullmatch(r"(a|an|the|my|scene|duty)", location):
            location = None
        args = {"kind": kind, "location": location.title() if location else None,
                "plate": spoken_plate(plate.group(1)) if plate else None, "detail": t[:200]}
        return Intent("self_initiated", args)
    return None

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
    (r"\b(?:play|plait|plates?)(?=\s*\d|\d)", "plate "),
    (r"\b(?:play|plait)(?= (?:number|is)\b)", "plate"),
    (r"\btraffic stopped\b", "traffic stop"),
]


def normalize_speech(text: str) -> str:
    t = (text or "").lower().strip()
    t = re.sub(r"[‐-―]", "-", t)
    t = re.sub(r"[.!?]+$", "", t)
    for pattern, repl in MISHEARD:
        t = re.sub(pattern, repl, t)
    t = re.sub(r"\bten[\s-]+(twenty[\s-]+three|twenty|fifteen|eighty|eleven|twelve|eight|seven|six|four|five|nine|one|two|three)\b",
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


def maybe_wake_word(text: str) -> bool:
    """Lenient check used on the fast (less accurate) transcript: could the speaker have said "Dispatch"
    near the start? False only when the clip is clearly not addressed to dispatch."""
    import difflib
    words = re.findall(r"[a-z']+", (text or "").lower())[:5]
    if not words:
        return False
    candidates = set(words) | {a + b for a, b in zip(words, words[1:])}
    for c in candidates:
        if "patch" in c or c in {"central", "control", "dispatcher"}:
            return True
        if len(c) >= 5 and difflib.SequenceMatcher(None, c, "dispatch").ratio() >= 0.7:
            return True
    return False


def strip_wake_word(text: str) -> tuple[bool, str]:
    t = normalize_speech(text)
    m = re.match(r"^\W*(?:hey |ok |okay )?(%s)\b[\s,.:!-]*(.*)$" % "|".join(WAKE_WORDS), t)
    if m:
        return True, m.group(2).strip()
    # Fuzzy: a close mishearing of "dispatch" as the first word (or first two words joined).
    import difflib
    m = re.match(r"^\W*(?:hey |ok |okay )?([a-z']+)(?:[\s,.:!-]+([a-z']+))?[\s,.:!-]*(.*)$", t)
    if m:
        first, second, rest = m.group(1), m.group(2) or "", m.group(3)
        for cand, remainder in ((first, (second + " " + rest).strip()), (first + second, rest)):
            if len(cand) >= 6 and difflib.SequenceMatcher(None, cand, "dispatch").ratio() >= 0.75:
                return True, remainder.strip()
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

    # Radio check
    if re.fullmatch(r"(are you there|you there|radio check|do you copy|how copy|you copy|test(ing)?|check)\W*", t):
        return Intent("radio_check")

    # Questions first, so "show me available units" is not read as "mark me available".
    if re.search(r"\b(available|10-8) (units|officers)\b", t) or re.search(r"\bwho('?s| is) available\b", t):
        return Intent("query_available")
    if re.search(r"\bhow many (units|officers)\b", t) or re.search(r"\bwho('?s| is)? (on|assigned to)\b", t):
        return Intent("query_incident_units", {"incident_number": num} if num else {})

    # Clearing from a self-initiated stop/pursuit
    if re.search(r"\b(clear|cleared|code 4|done)\b.*\b(stop|pursuit|chase)\b", t):
        return Intent("detach", {"incident_number": num} if num else {})
    # Officer-initiated activity ("show me on a traffic stop at ...") before plain status codes
    si = _self_initiated(t)
    if (si is not None and not re.search(r"\b(how many|which|what|any)\b", t)
            and (not re.search(r"\b(backup|back up)\b", t) or re.search(r"\b(show me|i'?m|we'?re|starting|initiating)\b", t))):
        return si

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
                                 prompt=prompt, json_mode=True, max_tokens=400, temperature=0.0)
        data = extract_json(resp.text)
    except (AIUnavailable, ProviderError) as e:
        log.info("AI intent parsing unavailable: %s", e)
        return Intent("unclear", source="none", clarification=(
            "Dispatch, say again? You can say: show me available, attach me to the call, "
            "show me on a traffic stop, request backup, or what calls are active."))
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

"""Local natural-language understanding for 911 calls and dispatch requests.

Fast (well under a millisecond), deterministic and works with every AI provider
offline. It understands *meaning* rather than exact phrases:

  * word-order independent  - incident types are detected from concept sets, not regex order
  * synonyms                - "wreck", "fender bender", "pile-up" -> collision
  * spelling mistakes       - "acident", "colision", "ambulence" are corrected (fuzzy match)
  * abbreviations / slang   - "mva", "gsw", "pd", "ems", "veh", "@", "10-50", "rn", "pls"
  * location extraction     - "at the bank", "bank location", "outside the gas station", "postal 214"
  * department requests     - "send police", "need EMS", "get fire out here"
  * missing information     - what to ask the caller next

`analyze()` is the main entry point. AI interpretation is layered on top by the
callers; nothing here ever needs the network.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

# ---------------------------------------------------------------------------- normalisation
ABBREVIATIONS = {
    "mva": "motor vehicle accident", "mvc": "motor vehicle collision", "tc": "traffic collision",
    "gsw": "gunshot wound", "veh": "vehicle", "vehs": "vehicles", "acc": "accident", "ped": "pedestrian",
    "peds": "pedestrians", "pd": "police", "leo": "police", "fd": "fire department", "so": "sheriff",
    "sp": "state police", "dot": "dot", "hosp": "hospital", "ambo": "ambulance", "bus": "bus",
    "dui": "drunk driver", "dwi": "drunk driver", "susp": "suspect", "susps": "suspects", "vic": "victim",
    "vics": "victims", "unk": "unknown", "approx": "approximately", "rn": "right now", "pls": "please",
    "plz": "please", "u": "you", "ur": "your", "r": "are", "w": "with", "b4": "before", "bc": "because",
    "cuz": "because", "thx": "thanks", "asap": "asap", "ave": "avenue", "blvd": "boulevard", "rd": "road",
    "hwy": "highway", "fwy": "freeway", "apt": "apartment", "apts": "apartments", "bldg": "building",
    "govt": "government", "dept": "department", "nxt": "next", "abt": "about", "ppl": "people",
    "sum1": "someone", "some1": "someone", "smth": "something", "omw": "on my way", "lp": "license plate",
    "ofc": "officer", "ofcr": "officer", "dep": "deputy", "tpr": "trooper", "k9": "k9", "swat": "swat",
}
# Radio 10-codes commonly used in ER:LC communities to *describe an incident*.
TEN_CODES = {
    "10-50": "vehicle accident", "10-52": "ambulance needed", "10-53": "road blocked", "10-70": "fire",
    "10-71": "shooting", "10-80": "pursuit", "10-31": "crime in progress", "10-32": "armed subject",
    "10-33": "officer emergency", "10-90": "alarm", "10-99": "officer down",
}

# Words the spell-checker must never "correct" (common English + dispatch words).
COMMON_WORDS = set("""
a about above after again against all am an and any are around as at away back be because been before behind being
below between both but by can come could did do does doing down during each few for from further get got had has have
having he her here hers him his how i if in into is it its just like me more most my near need needs next no nor not
now of off on once only or other our out over own please quick quickly right same send she should so some someone
something still such than that the their them then there theres these they this those through to too under until up
us very was we were what when where which while who whom why will with would you your yours yes yeah ok okay sir
help hurry fast now really think looks look seen saw just guy guys man woman lady kid kids people person someone
dispatch dispatcher unit units officer officers copy roger respond responding scene location address street road
avenue boulevard highway freeway lane drive way court place postal block corner intersection
""".split())

# ---------------------------------------------------------------------------- concepts
# concept -> words/phrases. Phrases may contain spaces; matching is done on the normalised text.
CONCEPTS: dict[str, list[str]] = {
    "collision": ["crash", "crashed", "crashes", "accident", "accidents", "collision", "collided", "wreck", "wrecked",
                  "pileup", "pile up", "fender bender", "rear ended", "t boned", "tboned", "hit and run", "rolled over",
                  "rollover", "flipped over", "flipped", "ran into", "smashed into", "slammed into", "drove into",
                  "hit a pole", "hit a tree", "hit a wall", "vehicle accident", "car accident", "traffic collision",
                  "motor vehicle accident", "motor vehicle collision"],
    "vehicle": ["vehicle", "vehicles", "car", "cars", "truck", "trucks", "van", "suv", "motorcycle", "motorbike",
                "semi", "sedan", "pickup", "bus", "taxi", "jeep", "coupe", "tractor"],
    "multiple": ["multiple", "several", "many", "two", "three", "four", "five", "2", "3", "4", "5", "pileup", "pile up"],
    "shooting": ["shots fired", "shooting", "shootout", "shot", "gunshot", "gunshots", "gunfire", "shooter",
                 "opened fire", "open fire", "firing", "shots", "gunshot wound", "being shot", "active shooter"],
    "hostage": ["hostage", "hostages", "kidnap", "kidnapped", "kidnapping", "abducted", "abduction", "barricaded"],
    "stabbing": ["stab", "stabbed", "stabbing", "knifed", "knife attack", "slashed", "cut him", "cut her"],
    "weapon": ["gun", "guns", "armed", "weapon", "weapons", "firearm", "pistol", "rifle", "shotgun", "knife",
               "machete", "ak", "glock", "armed subject"],
    "robbery": ["robbery", "robbing", "robbed", "rob", "hold up", "holdup", "held up", "mugging", "mugged", "heist",
                "stick up", "stickup", "robbers", "robber"],
    "major_target": ["bank", "jewelry", "jewelry store", "jewellery", "vault", "armored truck", "heist"],
    "pursuit": ["pursuit", "chase", "chasing", "fleeing", "evading", "running from police", "fled", "flee",
                "high speed", "won't stop", "wont stop", "failing to stop", "fail to stop", "speeding away"],
    "explosion": ["explosion", "exploded", "explosive", "explosives", "bomb", "blast", "ied", "detonated"],
    "fire": ["on fire", "fire", "fires", "burning", "burns", "smoke", "flames", "blaze", "ablaze", "arson",
             "house fire", "structure fire", "brush fire", "wildfire", "car fire", "vehicle fire"],
    "medical": ["unconscious", "not breathing", "cardiac", "heart attack", "overdose", "overdosed", "od", "seizure",
                "bleeding", "injured", "injury", "injuries", "hurt", "wounded", "medical", "collapsed", "passed out",
                "fainted", "stroke", "choking", "broken leg", "broken arm", "ambulance", "ambulance needed",
                "paramedic", "paramedics", "sick", "pain", "unresponsive", "dying", "dead", "deceased", "fell"],
    "injury": ["injured", "injury", "injuries", "hurt", "bleeding", "wounded", "unconscious", "unresponsive",
               "trapped", "pinned", "broken", "dead", "dying", "gunshot wound", "not breathing"],
    "assault": ["fight", "fighting", "assault", "assaulted", "attacked", "attacking", "beating", "beat up",
                "brawl", "punched", "punching", "domestic", "jumped", "hitting", "fistfight"],
    "burglary": ["break in", "breakin", "broke in", "breaking in", "breaking into", "broke into", "burglar",
                 "burglary", "burglarized", "intruder", "intruders"],
    "theft": ["stolen", "stole", "theft", "steal", "stealing", "shoplift", "shoplifting", "shoplifter",
              "carjack", "carjacked", "carjacking", "took my", "snatched", "grand theft auto", "gta"],
    "reckless": ["reckless", "speeding", "drunk driver", "drunk", "swerving", "racing", "street racing",
                 "wrong way", "impaired", "drifting", "burnouts", "doing donuts"],
    "vandalism": ["vandal", "vandalism", "vandalized", "graffiti", "trespass", "trespassing", "trespasser",
                  "smashed windows", "spray painted"],
    "suspicious": ["suspicious", "loitering", "loiter", "creeping", "lurking", "casing", "weird guy", "strange"],
    "minor": ["noise", "loud music", "parking", "parked", "blocking", "abandoned", "littering", "noise complaint"],
    "missing": ["missing", "lost child", "lost kid", "missing person", "can't find", "cant find", "ran away"],
    "welfare": ["welfare check", "check on", "wellness check", "hasn't answered", "not answering"],
    "officer_emergency": ["officer down", "officer emergency", "officer needs assistance", "cop down", "deputy down",
                          "trooper down", "officer shot", "officer injured", "panic button", "10-33", "10-99"],
    "alarm": ["alarm", "alarm going off", "alarm activation", "silent alarm"],
    "road_hazard": ["road blocked", "debris", "tree down", "downed tree", "pothole", "power lines down",
                    "road hazard", "stalled", "broken down", "disabled vehicle"],
}

DEPARTMENT_WORDS = {
    "law": ["police", "cops", "cop", "officers", "officer", "law enforcement", "sheriff", "deputies", "deputy",
            "trooper", "troopers", "state police", "patrol", "swat", "k9"],
    "ems": ["ems", "ambulance", "ambulances", "medic", "medics", "paramedic", "paramedics", "medical",
            "emt", "emts", "doctor"],
    "fire": ["fire department", "firefighters", "firefighter", "fire truck", "fire engine", "firemen", "fire rescue",
             "fire"],
}

# Places commonly found on ER:LC maps; used for location extraction and duplicate detection.
LANDMARKS = [
    "bank", "gas station", "jewelry store", "jewelry", "hospital", "police station", "police department",
    "fire station", "sheriff's office", "sheriffs office", "dealership", "car dealership", "airport", "airfield",
    "prison", "jail", "courthouse", "court house", "school", "high school", "gun store", "gun shop", "hardware store",
    "mall", "hotel", "motel", "park", "church", "farm", "beach", "pier", "harbor", "harbour", "port", "dock", "docks",
    "highway", "freeway", "interstate", "bridge", "tunnel", "mechanic", "tow yard", "apartments", "apartment",
    "mansion", "cafe", "diner", "restaurant", "bar", "club", "nightclub", "warehouse", "construction site",
    "power plant", "train station", "bus station", "gas", "store", "shop", "supermarket", "grocery store",
    "pharmacy", "clinic", "city hall", "town hall", "library", "museum", "stadium", "arena", "marina", "lake",
    "river", "quarry", "mine", "lumber yard", "lumber mill", "factory", "plant", "office", "offices", "house",
    "home", "residence", "trailer park", "campground", "gas pumps", "intersection", "roundabout", "parking lot",
    "car wash", "garage", "tool shop", "pawn shop", "liquor store", "convenience store", "barber", "salon",
    "fast food", "burger", "pizza", "donut shop", "coffee shop", "ranch", "cabin", "military base", "base",
]
STREET_SUFFIXES = ["street", "st", "road", "avenue", "boulevard", "highway", "freeway", "lane", "drive", "way",
                   "court", "place", "parkway", "route", "interstate", "circle", "trail", "pike", "expressway"]
LOCATION_MODIFIERS = ["downtown", "uptown", "north", "south", "east", "west", "northern", "southern", "eastern",
                      "western", "main", "old", "new", "big", "small", "county", "city", "central", "upper", "lower"]

# Words that end a captured location phrase.
_LOCATION_STOP = set("""
for with there theres there's and because cause involving where who that which please asap now right we i it he she
they is are was were has have had been be just someone somebody some guy guys man woman people person there's
crash accident collision fire shooting robbery fight need needs send sending hurry quick quickly help near by at on
in to from but or so if when after before while about like
""".split())

# ---------------------------------------------------------------------------- incident types
@dataclass(frozen=True)
class IncidentType:
    key: str
    category: str            # matches the category names used by dispatch.recommendations
    priority: str
    units: int
    kinds: frozenset
    supervisor: bool = False
    nature: str = ""         # radio-friendly nature of call


INCIDENT_TYPES: list[IncidentType] = [
    IncidentType("officer_emergency", "Officer needs assistance", "critical", 4, frozenset({"law", "ems"}), True,
                 "Officer needs assistance"),
    IncidentType("shooting", "Shots fired / shooting", "critical", 4, frozenset({"law", "ems"}), True, "Shots fired"),
    IncidentType("hostage", "Hostage / kidnapping", "critical", 4, frozenset({"law", "ems", "special"}), True, "Hostage situation"),
    IncidentType("stabbing", "Stabbing", "high", 3, frozenset({"law", "ems"}), True, "Stabbing"),
    IncidentType("major_robbery", "Major robbery", "high", 3, frozenset({"law"}), True, "Robbery in progress"),
    IncidentType("armed", "Armed subject", "high", 3, frozenset({"law"}), True, "Armed subject"),
    IncidentType("robbery", "Robbery", "high", 2, frozenset({"law"}), False, "Robbery"),
    IncidentType("pursuit", "Pursuit / fleeing subject", "high", 3, frozenset({"law"}), True, "Vehicle pursuit"),
    IncidentType("explosion", "Explosion / explosive", "critical", 3, frozenset({"law", "fire", "ems"}), True,
                 "Explosion"),
    IncidentType("fire", "Fire", "high", 1, frozenset({"fire", "ems", "law"}), False, "Fire"),
    IncidentType("multi_collision", "Multi-vehicle collision", "high", 2, frozenset({"law", "fire", "ems"}), False,
                 "Multi-vehicle collision"),
    IncidentType("collision", "Traffic collision", "medium", 1, frozenset({"law"}), False, "Vehicle collision"),
    IncidentType("medical", "Medical emergency", "high", 1, frozenset({"ems", "law"}), False, "Medical emergency"),
    IncidentType("assault", "Assault / disturbance", "medium", 2, frozenset({"law"}), False, "Assault / disturbance"),
    IncidentType("burglary", "Burglary", "medium", 2, frozenset({"law"}), False, "Burglary"),
    IncidentType("theft", "Theft", "routine", 1, frozenset({"law"}), False, "Theft"),
    IncidentType("reckless", "Reckless / impaired driver", "medium", 1, frozenset({"law"}), False, "Reckless driver"),
    IncidentType("missing", "Missing person", "medium", 1, frozenset({"law"}), False, "Missing person"),
    IncidentType("alarm", "Alarm activation", "medium", 1, frozenset({"law"}), False, "Alarm activation"),
    IncidentType("vandalism", "Vandalism / trespass", "routine", 1, frozenset({"law"}), False, "Vandalism / trespass"),
    IncidentType("road_hazard", "Road hazard", "routine", 1, frozenset({"law"}), False, "Road hazard"),
    IncidentType("suspicious", "Suspicious activity", "routine", 1, frozenset({"law"}), False, "Suspicious activity"),
    IncidentType("welfare", "Welfare check", "routine", 1, frozenset({"law"}), False, "Welfare check"),
    IncidentType("minor", "Minor complaint", "low", 1, frozenset({"law"}), False, "Minor complaint"),
]
TYPE_BY_KEY = {t.key: t for t in INCIDENT_TYPES}
TYPE_BY_CATEGORY = {t.category: t for t in INCIDENT_TYPES}

EMERGENCY_HINTS = ["911", "emergency", "help", "hurry", "send", "need", "urgent", "report", "reporting", "there's",
                   "theres", "we have", "there is", "someone", "just saw", "i see", "happening"]

PRIORITY_ORDER = ["low", "routine", "medium", "high", "critical"]


def _phrase_regex(phrase: str) -> re.Pattern:
    # word boundaries on both ends; spaces in phrases match any run of spaces/hyphens
    body = r"[\s-]+".join(re.escape(p) for p in phrase.split())
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


_CONCEPT_PATTERNS: dict[str, list[re.Pattern]] = {c: [_phrase_regex(w) for w in words] for c, words in CONCEPTS.items()}
_DEPT_PATTERNS = {k: [_phrase_regex(w) for w in words] for k, words in DEPARTMENT_WORDS.items()}
_LANDMARK_PATTERNS = sorted(((lm, _phrase_regex(lm)) for lm in LANDMARKS), key=lambda x: -len(x[0]))

VOCABULARY = sorted({w for words in CONCEPTS.values() for phrase in words for w in phrase.split() if len(w) >= 4}
                    | {w for words in DEPARTMENT_WORDS.values() for phrase in words for w in phrase.split() if len(w) >= 4}
                    | {w for lm in LANDMARKS for w in lm.split() if len(w) >= 4}
                    | {"ambulance", "emergency", "accident", "location", "vehicle", "collision", "robbery", "suspect",
                       "victim", "injured", "unconscious", "breathing", "jewelry", "available", "attach", "backup",
                       "incident", "officer", "dispatch", "responding", "pursuit", "station", "hospital"})
_VOCAB_SET = set(VOCABULARY)
# Frequent misspellings that difflib would not pick confidently.
MISSPELLINGS = {
    "acident": "accident", "accidnet": "accident", "accedent": "accident", "acidnet": "accident", "axident": "accident",
    "colision": "collision", "collison": "collision", "colission": "collision", "crashd": "crashed", "crach": "crash",
    "ambulence": "ambulance", "ambulanse": "ambulance", "ambulnce": "ambulance", "amublance": "ambulance",
    "robery": "robbery", "robbry": "robbery", "roberry": "robbery", "robing": "robbing", "jewlery": "jewelry",
    "jewelery": "jewelry", "jewllery": "jewelry", "polise": "police", "pollice": "police", "poilce": "police",
    "plice": "police", "shoting": "shooting", "shootin": "shooting", "shotting": "shooting", "gunshoot": "gunshot",
    "stabed": "stabbed", "stabing": "stabbing", "fyre": "fire", "hospitle": "hospital", "hosptial": "hospital",
    "vehical": "vehicle", "vehicel": "vehicle", "vechile": "vehicle", "vehicule": "vehicle", "vehcile": "vehicle",
    "injurd": "injured", "injuried": "injured", "unconcious": "unconscious", "unconsious": "unconscious",
    "breathin": "breathing", "explostion": "explosion", "bulding": "building", "buliding": "building",
    "pursute": "pursuit", "persuit": "pursuit", "pursiut": "pursuit", "chaseing": "chasing", "susspect": "suspect",
    "suspicous": "suspicious", "suspisious": "suspicious", "stolin": "stolen", "theif": "thief", "burgler": "burglar",
    "emergancy": "emergency", "emergeny": "emergency", "emrgency": "emergency", "loction": "location",
    "locaton": "location", "adress": "address", "addres": "address", "intersecton": "intersection",
    "stattion": "station", "staion": "station", "gass": "gas", "dealorship": "dealership", "dealrship": "dealership",
    "assualt": "assault", "asault": "assault", "fite": "fight", "fihgt": "fight", "wreckd": "wrecked",
    "speedin": "speeding", "drunck": "drunk", "overdoes": "overdose", "seizur": "seizure", "bleding": "bleeding",
    "theres": "there's", "thers": "there's", "ther's": "there's", "pls": "please", "cuz": "because",
}


@lru_cache(maxsize=4096)
def correct_word(word: str) -> str:
    """Correct one lower-case token if it is a likely misspelling of a dispatch word."""
    if word in MISSPELLINGS:
        return MISSPELLINGS[word]
    if len(word) < 5 or word in _VOCAB_SET or word in COMMON_WORDS or not word.isalpha():
        return word
    match = difflib.get_close_matches(word, VOCABULARY, n=1, cutoff=0.84)
    return match[0] if match else word


@lru_cache(maxsize=2048)
def normalize(text: str) -> str:
    """Lower-case, expand abbreviations and 10-codes, correct spelling, tidy punctuation."""
    t = (text or "").lower()
    t = t.replace("’", "'").replace("‘", "'")
    t = re.sub(r"[‐-―]", "-", t)
    t = re.sub(r"\b10\s*[-.]?\s*(\d{2})\b", r"10-\1", t)
    for code, meaning in TEN_CODES.items():
        t = re.sub(rf"(?<![\d-]){re.escape(code)}(?![\d])", f" {meaning} ", t)
    t = re.sub(r"(?<=\s)@(?=\s)|^@|@(?=\w)", " at ", t)
    t = t.replace("&", " and ").replace("w/", "with ").replace("b/c", "because")
    t = re.sub(r"\bp\s?(\d{2,4})\b", r"postal \1", t)          # "p214" / "p 214" -> postal 214
    t = re.sub(r"\b(?:pos|post|postal code|postal #)\s*#?\s*(\d{2,4})\b", r"postal \1", t)
    tokens = re.findall(r"[a-z0-9']+|[.,!?;:]", t)
    out = []
    for tok in tokens:
        if tok in ABBREVIATIONS:
            out.append(ABBREVIATIONS[tok])
        elif re.fullmatch(r"[a-z']+", tok):
            out.append(correct_word(tok.strip("'")) if tok.strip("'") else tok)
        else:
            out.append(tok)
    t = " ".join(out)
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)
    t = re.sub(r"\bt[\s-]?bone(d)?\b", "t boned", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def concepts_in(normalized: str) -> set[str]:
    found = set()
    for concept, patterns in _CONCEPT_PATTERNS.items():
        if any(p.search(normalized) for p in patterns):
            found.add(concept)
    return found


def _classify(found: set[str], t: str) -> Optional[IncidentType]:
    """Pick an incident type from the concept set (order of INCIDENT_TYPES = precedence)."""
    has = found.__contains__
    # "fire" as in "open fire"/"opened fire"/"firing" is a shooting, not a fire.
    if has("fire") and has("shooting") and not re.search(r"\b(on fire|burning|smoke|flames|blaze)\b", t):
        found = found - {"fire"}
        has = found.__contains__
    # "shot" in "shot clock"/"screenshot" never reaches here (word boundaries); "shot" alone with a weapon = shooting.
    checks = [
        ("officer_emergency", has("officer_emergency")),
        ("shooting", has("shooting") and not re.search(r"\b(shot (him|her|them) a|took a shot at the)\b", t)),
        ("hostage", has("hostage")),
        ("stabbing", has("stabbing")),
        ("major_robbery", has("robbery") and has("major_target")),
        ("armed", has("weapon") and not has("robbery")),
        ("robbery", has("robbery")),
        ("pursuit", has("pursuit")),
        ("explosion", has("explosion")),
        ("fire", has("fire")),
        ("multi_collision", has("collision") and _multi_vehicle(t)),
        ("collision", has("collision") or (has("vehicle") and re.search(r"\b(hit|struck|ran over|run over)\b", t) is not None)),
        ("medical", has("medical")),
        ("assault", has("assault")),
        ("burglary", has("burglary")),
        ("theft", has("theft")),
        ("reckless", has("reckless")),
        ("missing", has("missing")),
        ("alarm", has("alarm")),
        ("vandalism", has("vandalism")),
        ("road_hazard", has("road_hazard")),
        ("suspicious", has("suspicious")),
        ("welfare", has("welfare")),
        ("minor", has("minor")),
    ]
    for key, ok in checks:
        if ok:
            return TYPE_BY_KEY[key]
    return None


def _multi_vehicle(t: str) -> bool:
    if re.search(r"\b(pile ?up|multi(ple)?[\s-]*(car|vehicle)|several (cars|vehicles)|many (cars|vehicles))\b", t):
        return True
    m = re.search(r"\b(two|three|four|five|six|\d+)[\s-]*(car|vehicle|truck)s?\b", t)
    if m:
        n = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}.get(m.group(1))
        n = n if n is not None else int(m.group(1)) if m.group(1).isdigit() else 0
        return n >= 2
    return False


_REQUEST_VERB = re.compile(r"\b(send|sending|need|needs|needed|get|call|request|requesting|roll|want|dispatch|bring|"
                           r"have|require|requires)\b[\w\s']{0,20}$")


def requested_departments(t: str) -> set[str]:
    """Department kinds the caller explicitly asked for ("send police", "need EMS")."""
    kinds = set()
    for kind, patterns in _DEPT_PATTERNS.items():
        for p in patterns:
            for m in p.finditer(t):
                word = m.group(0)
                before = t[max(0, m.start() - 30): m.start()]
                if kind == "ems" and word in {"ems", "ambulance", "ambulances", "paramedic", "paramedics", "medic", "medics", "emt", "emts"}:
                    kinds.add("ems")  # mentioning an ambulance always means EMS is wanted
                elif kind == "fire" and word == "fire":
                    if _REQUEST_VERB.search(before):
                        kinds.add("fire")  # "send fire" - bare "fire" is otherwise the incident itself
                elif kind == "fire" or _REQUEST_VERB.search(before):
                    kinds.add(kind)
    return kinds


# ---------------------------------------------------------------------------- locations
_STREET_RE = re.compile(
    r"\b((?:\d{1,5}\s+)?(?:[a-z0-9']+\s+){0,3}(?:%s))\b" % "|".join(STREET_SUFFIXES))
_POSTAL_RE = re.compile(r"\bpostal\s*(\d{2,4})\b")
_PREP_RE = re.compile(  # zero-width lookahead so overlapping phrases are all considered
    r"(?=\b(at|near|by|outside(?: of)?|behind|in front of|across from|next to|on|inside(?: of)?|in|to|around|off of|off)\s+"
    r"((?:the|a|an)\s+)?([a-z0-9'][a-z0-9' ]{1,60}))")
_EXPLICIT_RE = re.compile(r"\b(?:location|address|loc|located)\b\s*(?:is|:|=|-|of)?\s*([a-z0-9'][a-z0-9' ]{1,60})")
_SUFFIX_LOC_RE = re.compile(r"\b((?:[a-z0-9']+\s+){1,3})(?:location|area|vicinity)\b")


def _trim_phrase(words: list[str], max_words: int = 6) -> list[str]:
    out = []
    for w in words:
        if w in _LOCATION_STOP or w in {"crash", "accident", "fire", "shooting", "robbery", "fight", "collision"}:
            break
        out.append(w)
        if len(out) >= max_words:
            break
    while out and out[-1] in {"the", "a", "an", "of"}:
        out.pop()
    return out


def _is_place(phrase: str) -> bool:
    if not phrase:
        return False
    if any(p.search(phrase) for _, p in _LANDMARK_PATTERNS):
        return True
    if _STREET_RE.search(phrase) or _POSTAL_RE.search(phrase):
        return True
    return False


def _pretty(phrase: str) -> str:
    words = phrase.split()
    small = {"of", "and", "the", "at", "on"}
    out = []
    for i, w in enumerate(words):
        if w in {"st"}:
            out.append("St")
        elif w.isdigit():
            out.append(w)
        elif i and w in small:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    s = " ".join(out)
    s = re.sub(r"\bPostal (\d+)\b", r"Postal \1", s)
    return s.replace("'S", "'s")


def extract_location(t: str) -> Optional[str]:
    """Best-effort location from normalised text. Returns a tidy display string or None."""
    # 1) explicit "location: ..." / "address is ..."
    m = _EXPLICIT_RE.search(t)
    if m:
        words = _trim_phrase(m.group(1).split())
        if words:
            return _pretty(" ".join(words))
    candidates: list[tuple[int, str]] = []
    # 2) prepositional phrases that name a place
    for m in _PREP_RE.finditer(t):
        prep = m.group(1)
        words = _trim_phrase(m.group(3).split())
        phrase = " ".join(words)
        if not phrase or not _is_place(phrase):
            continue
        # "at the bank and the gas station" -> keep first; prefer longer, more specific phrases
        score = 3 if prep in {"at", "outside", "outside of", "in front of", "behind", "near", "across from", "next to"} else 2
        if _STREET_RE.search(phrase) or _POSTAL_RE.search(phrase):
            score += 1
            # "the old warehouse on 5th street" -> keep the landmark in front of the street
            before = [w for w in t[: m.start()].split()[-3:] if w not in {"the", "a", "an"}]
            lead = []
            for w in reversed(before):
                if w in _LOCATION_STOP:
                    break
                lead.insert(0, w)
            lead_phrase = " ".join(lead)
            if lead and any(p.search(lead_phrase) for _, p in _LANDMARK_PATTERNS):
                phrase = f"{lead_phrase} {prep} {phrase}"
                score += 1
        candidates.append((score * 100 + len(words), phrase))
    # 3) "<place> location/area" ("bank location", "downtown area")
    for m in _SUFFIX_LOC_RE.finditer(t):
        words = [w for w in m.group(1).split() if w not in _LOCATION_STOP]
        phrase = " ".join(_trim_phrase(words))
        if phrase and (_is_place(phrase) or any(w in LOCATION_MODIFIERS for w in words)):
            candidates.append((250 + len(words), phrase))
    if candidates:
        candidates.sort(key=lambda c: -c[0])
        best = candidates[0][1]
        postal = _POSTAL_RE.search(t)
        if postal and "postal" not in best:
            best += f" postal {postal.group(1)}"
        return _pretty(best)
    # 4) any landmark mentioned anywhere, with an optional modifier in front ("downtown bank")
    for lm, p in _LANDMARK_PATTERNS:
        m = p.search(t)
        if m:
            before = t[: m.start()].split()[-2:]
            mods = [w for w in before if w in LOCATION_MODIFIERS]
            phrase = " ".join(mods + [lm])
            postal = _POSTAL_RE.search(t)
            if postal:
                phrase += f" postal {postal.group(1)}"
            return _pretty(phrase)
    m = _STREET_RE.search(t)
    if m:
        words = [w for w in m.group(1).split() if w not in _LOCATION_STOP]
        if words:
            return _pretty(" ".join(words))
    m = _POSTAL_RE.search(t)
    if m:
        return f"Postal {m.group(1)}"
    return None


def location_key(location: Optional[str]) -> str:
    """Canonical form used to compare two locations ("Downtown Bank" ~ "the bank")."""
    if not location:
        return ""
    t = normalize(location)
    words = [w for w in re.findall(r"[a-z0-9]+", t) if w not in {"the", "a", "an", "of", "location", "area"}]
    return " ".join(words)


def _primary_landmark(key: str) -> Optional[str]:
    for lm, p in _LANDMARK_PATTERNS:  # longest first: "trailer park" before "park"
        if p.search(key):
            return lm
    return None


def locations_match(a: Optional[str], b: Optional[str]) -> bool:
    ka, kb = location_key(a), location_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    pa, pb = _POSTAL_RE.search(ka), _POSTAL_RE.search(kb)
    if pa and pb:
        return pa.group(1) == pb.group(1)
    la, lb = _primary_landmark(ka), _primary_landmark(kb)
    if la and lb and la != lb:
        return False  # "park" vs "trailer park", "bank" vs "gas station"
    if re.search(rf"(?<![a-z0-9]){re.escape(ka)}(?![a-z0-9])", kb) or re.search(rf"(?<![a-z0-9]){re.escape(kb)}(?![a-z0-9])", ka):
        return True
    if la and la == lb:
        return True
    return difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.85


# ---------------------------------------------------------------------------- analysis
@dataclass
class Analysis:
    text: str
    normalized: str
    incident_type: Optional[IncidentType]
    location: Optional[str]
    postal: Optional[str]
    concepts: set[str] = field(default_factory=set)
    requested_kinds: set[str] = field(default_factory=set)
    injuries: bool = False
    weapons: bool = False
    people: Optional[int] = None
    confidence: float = 0.0
    missing: list[str] = field(default_factory=list)

    @property
    def category(self) -> Optional[str]:
        return self.incident_type.category if self.incident_type else None

    @property
    def nature(self) -> Optional[str]:
        if not self.incident_type:
            return None
        n = self.incident_type.nature
        if self.injuries and self.incident_type.key in {"collision", "multi_collision"}:
            n += " with injuries"
        return n

    @property
    def is_emergency_report(self) -> bool:
        """Does the text describe an incident (rather than an officer request or a question)?"""
        return self.incident_type is not None and self.confidence >= 0.5

    def kinds(self) -> set[str]:
        kinds = set(self.incident_type.kinds) if self.incident_type else {"law"}
        kinds |= self.requested_kinds
        if self.injuries:
            kinds.add("ems")
        return kinds

    def follow_up(self) -> Optional[str]:
        """The single most useful question to ask the caller next."""
        if "nature" in self.missing and "location" in self.missing:
            return "What is your emergency, and what is your location?"
        if "nature" in self.missing:
            return "Copy. What is the nature of your emergency?"
        if "location" in self.missing:
            what = (self.incident_type.nature.lower() if self.incident_type else "incident")
            return f"Copy, {what}. What is the location? A street, landmark or postal works."
        return None


_PEOPLE_RE = re.compile(r"\b(two|three|four|five|six|several|multiple|many|\d+)\s+(people|persons|victims|suspects|"
                        r"guys|men|women|subjects|injured|patients|shooters)\b")


def analyze(text: str) -> Analysis:
    t = normalize(text)
    found = concepts_in(t)
    itype = _classify(set(found), t)
    location = extract_location(t)
    postal_m = _POSTAL_RE.search(t)
    injuries = "injury" in found or bool(re.search(r"\b(ems|ambulance|medic|paramedic)\b", t) and "collision" in found)
    weapons = "weapon" in found or (itype is not None and itype.key in {"shooting", "stabbing", "armed"})
    people = None
    pm = _PEOPLE_RE.search(t)
    if pm:
        w = pm.group(1)
        people = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "several": 3, "multiple": 3, "many": 5}.get(
            w, int(w) if w.isdigit() else None)
    conf = 0.0
    if itype is not None:
        conf = 0.6
        if location:
            conf += 0.25
        if any(h in t for h in EMERGENCY_HINTS):
            conf += 0.1
        if len(found) >= 2:
            conf += 0.05
    elif location and any(h in t for h in ("911", "emergency", "help")):
        conf = 0.3
    missing = []
    if itype is None:
        missing.append("nature")
    if not location:
        missing.append("location")
    return Analysis(text=text, normalized=t, incident_type=itype, location=location,
                    postal=postal_m.group(1) if postal_m else None, concepts=found,
                    requested_kinds=requested_departments(t), injuries=injuries, weapons=weapons, people=people,
                    confidence=min(conf, 1.0), missing=missing)


def classify(text: str) -> Optional[IncidentType]:
    return analyze(text).incident_type


def looks_like_officer_request(t: str) -> bool:
    """True when the text is an officer talking to dispatch about themselves (status, attach, backup...)."""
    t = normalize(t)
    return bool(re.search(r"\b(show me|mark me|put me|attach me|assign me|clear me|i'?m (10-|available|en route|on scene|"
                          r"clear|busy|out of service)|10-8|10-7|10-6|10-19|10-23|10-97|en route|on my way|"
                          r"requesting backup|need backup|send backup|backup)\b", t))

"""Parse transcribed user speech into a radio command.

Examples handled:
  "Dispatch to Unit three, status, over."         -> {addressee: UNIT-3}
  "Unit-1, ..., over."                            -> {addressee: UNIT-1}
  "Status check, over."                           -> {kind: STATUS_CHECK}
  "All units, regroup, over."                     -> {addressee: ALL}
  "Permission granted, over."                     -> {kind: PERMISSION, value: GRANTED}
  "Negative, over."                               -> {kind: PERMISSION, value: DENIED}
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# UNIT-<digit> (digit can be spoken as "one"/"two"/... too)
NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
}

CALLSIGN_RE = re.compile(
    r"\b(?:unit[- ]?(\d+|" + "|".join(NUMBER_WORDS.keys()) + r"))\b",
    re.IGNORECASE,
)

ALL_PATTERNS = [
    r"\ball units\b", r"\ball agents\b", r"\beverybody\b", r"\bevery unit\b",
]

STATUS_PATTERNS = [
    r"\bstatus check\b",
    r"\bstatus report\b",
    r"\bsit ?rep\b",
    r"\ball units (?:status|report)\b",
    r"\bwhat'?s your status\b",
    r"\breport in\b",
    r"\beveryone report\b",
]

PERMISSION_GRANT = [
    r"\bpermission granted\b", r"\bgranted\b", r"\bapproved\b",
    r"\baffirmative\b", r"\bgo ahead\b", r"\bproceed\b", r"\bcleared\b",
    r"\bcopy\b", r"\broger\b", r"\bten[- ]four\b", r"\b10[- ]4\b",
]

PERMISSION_DENY = [
    r"\bpermission denied\b", r"\bdenied\b", r"\bnegative\b",
    r"\bstand down\b", r"\babort\b", r"\bdo not\b", r"\bdon'?t\b",
]

END_SIGN = re.compile(r"\bover(?: and out)?\.?\s*$", re.IGNORECASE)


@dataclass
class Command:
    raw: str
    addressee: str | None   # "UNIT-3" | "ALL" | None
    body: str
    kind: str               # "MESSAGE" | "STATUS_CHECK" | "PERMISSION" | "EMPTY"
    permission: str | None = None


def _normalize_callsign(token: str) -> str:
    token = token.lower()
    if token.isdigit():
        return f"UNIT-{int(token)}"
    n = NUMBER_WORDS.get(token)
    if n is not None:
        return f"UNIT-{n}"
    return ""


def parse(text: str) -> Command:
    raw = text.strip()
    low = raw.lower()
    if not low:
        return Command(raw=raw, addressee=None, body="", kind="EMPTY")

    low_body = END_SIGN.sub("", low).strip(" ,.")

    # status check
    for pat in STATUS_PATTERNS:
        if re.search(pat, low_body):
            return Command(raw=raw, addressee="ALL", body="status check",
                           kind="STATUS_CHECK")

    # permission
    if any(re.search(p, low_body) for p in PERMISSION_GRANT) and len(low_body) < 60:
        return Command(raw=raw, addressee=None, body=low_body,
                       kind="PERMISSION", permission="GRANTED")
    if any(re.search(p, low_body) for p in PERMISSION_DENY) and len(low_body) < 60:
        return Command(raw=raw, addressee=None, body=low_body,
                       kind="PERMISSION", permission="DENIED")

    # all units
    if any(re.search(p, low_body) for p in ALL_PATTERNS):
        body = re.sub(r"\b(?:all units|all agents|everybody|every unit)\b,?\s*",
                       "", low_body).strip(" ,.")
        return Command(raw=raw, addressee="ALL", body=body, kind="MESSAGE")

    m = CALLSIGN_RE.search(low_body)
    if m:
        cs = _normalize_callsign(m.group(1))
        # strip "dispatch to unit X," / "unit X,"
        body = re.sub(r"\bdispatch\s+to\s+", "", low_body)
        body = CALLSIGN_RE.sub("", body)
        body = body.strip(" ,.")
        return Command(raw=raw, addressee=cs, body=body, kind="MESSAGE")

    # no callsign — broadcast to all (caller may route differently)
    return Command(raw=raw, addressee="ALL", body=low_body, kind="MESSAGE")

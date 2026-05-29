"""Dynamic agent roster derived from open Claude Code sessions.

Each open session becomes one agent on the channel:
  - callsign: UNIT-N, stable across roster refreshes (keyed by session_id)
  - voice: round-robin from a US-voice pool, also stable per session_id
  - persona: derived from project label + session title
  - reply path: `claude -p --resume <session_id> <message>` (so the user's
    own terminal sees the exchange too).
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from sessions import OpenSession, list_open_sessions, send_to_session


# Pool of usable macOS US voices — order matters (assignment is stable & in this order).
VOICE_POOL = [
    "Ralph",     # deep US male
    "Reed",      # level US male
    "Fred",      # gruff older US male
    "Samantha",  # clear US female
    "Kathy",     # alt US female
    "Junior",    # younger US male
]

# DISPATCH (the user-facing "system" voice) — also used for the attention intro.
DISPATCH_VOICE = "Kathy"


@dataclass
class Agent:
    callsign: str            # e.g. "UNIT-3"
    voice: str               # macOS voice name
    title: str               # human-friendly thread title
    project_label: str       # project slug (last segment of encoded cwd)
    session_id: str
    cwd: str                 # absolute cwd of the running session (for --resume)
    pid: int                 # pid of the live claude process (for tty injection)
    awaiting_input: bool
    last_assistant_text: str
    permission_mode: str = "default"   # default | acceptEdits | bypassPermissions | plan

    @property
    def voice_key(self) -> str:
        """Used by audio.speak() — we map the voice directly via VOICE_OVERRIDES below."""
        return self.callsign

    def system_prompt(self) -> str:
        """Used only for the attention envelope's DISPATCH intro voice line.
        The actual session reply comes from `claude --resume`, not from here."""
        return ""


# ---------------- callsign + voice assignment ----------------

_ASSIGN_LOCK = Lock()
_CALLSIGN_BY_SID: dict[str, str] = {}
_VOICE_BY_SID: dict[str, str] = {}


def _assign(session_id: str) -> tuple[str, str]:
    """Return (callsign, voice) for this session_id, creating it if new."""
    with _ASSIGN_LOCK:
        cs = _CALLSIGN_BY_SID.get(session_id)
        if cs is None:
            n = len(_CALLSIGN_BY_SID) + 1
            cs = f"UNIT-{n}"
            _CALLSIGN_BY_SID[session_id] = cs
            _VOICE_BY_SID[session_id] = VOICE_POOL[(n - 1) % len(VOICE_POOL)]
        return cs, _VOICE_BY_SID[session_id]


def build_roster() -> dict[str, Agent]:
    """Snapshot live sessions into agents keyed by callsign (e.g. 'UNIT-1')."""
    agents: dict[str, Agent] = {}
    for s in list_open_sessions():
        callsign, voice = _assign(s.session_id)
        agents[callsign] = Agent(
            callsign=callsign,
            voice=voice,
            title=s.short_title(),
            project_label=s.project_label,
            session_id=s.session_id,
            cwd=s.cwd,
            pid=s.pid,
            awaiting_input=s.awaiting_input,
            last_assistant_text=s.last_assistant_text,
            permission_mode=s.permission_mode,
        )
    return agents


def get_agent(key: str, roster: dict[str, Agent] | None = None) -> Agent | None:
    """Resolve a user-spoken key (UNIT-3 / UNIT3 / unit three / U3) to an agent.
    Re-uses the current roster if not provided."""
    if roster is None:
        roster = build_roster()
    norm = (key or "").strip().upper().replace(" ", "")
    # accept UNIT3, UNIT-3, U3, 3
    if norm.startswith("U") and not norm.startswith("UNIT"):
        norm = "UNIT-" + norm[1:]
    if norm.isdigit():
        norm = f"UNIT-{norm}"
    if "-" not in norm and norm.startswith("UNIT"):
        norm = "UNIT-" + norm[4:]
    return roster.get(norm)


# ---------------- talking to a session ----------------

# A wrapper that calls claude --resume and forces a short radio-style reply.
RADIO_STYLE_PREFIX = (
    "You are on a police-style radio channel. Reply in ONE short transmission "
    "(max 2 sentences, ~25 words). Start with '{callsign} to DISPATCH,'. End "
    "with 'over.' on its own. Use radio idioms when natural. If you need user "
    "approval to act, end with 'requesting permission, over.' Do not use "
    "markdown, code fences, or quotes. Plain prose only.\n\n"
    "--- TRANSMISSION FROM DISPATCH ---\n{message}"
)


def transmit_to(agent: Agent, message: str, *, timeout: int = 90) -> str:
    """Send a radio transmission into the agent's session, get a radio reply."""
    framed = RADIO_STYLE_PREFIX.format(callsign=agent.callsign, message=message)
    reply = send_to_session(agent.session_id, framed,
                            cwd=agent.cwd, timeout=timeout)
    return _force_radio_style(reply, agent)


def _force_radio_style(text: str, agent: Agent) -> str:
    text = (text or "").strip().strip("`").strip()
    if not text:
        return f"{agent.callsign} to DISPATCH, no reply, over."
    # If session went long, keep only the last "paragraph" (model sometimes
    # adds a preamble before the radio line).
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines:
        # Prefer the line that mentions the callsign — that's the radio TX.
        radio_lines = [ln for ln in lines if agent.callsign.upper() in ln.upper()]
        text = radio_lines[-1] if radio_lines else lines[-1]
    if not text.lower().rstrip(".").endswith("over"):
        text = text.rstrip(".") + ". Over."
    return text

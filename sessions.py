"""Discover open Claude Code sessions and treat each one as a radio agent.

A session counts as "open" iff there is a live `claude` process registered for
it. Claude Code writes ~/.claude/sessions/<pid>.json for every interactive
process it spawns; we use that file as the source of truth, then read the
matching jsonl under ~/.claude/projects/ for title + last assistant text.

This means closed terminals drop off the roster the moment they exit, and
one-shot `claude -p` calls (which don't register an interactive entry) never
appear in the first place.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
PIDS_DIR = CLAUDE_DIR / "sessions"

# A session needs at least this many user turns before we count it as a real
# interactive session (filters out one-shot subagent calls).
MIN_USER_TURNS = 1


# Heuristics for "session is asking me something"
_QUESTION_TAIL = re.compile(r"\?\s*$")
_NEEDS_INPUT = re.compile(
    r"\b("
    r"want me to|"
    r"should i|"
    r"do you want|"
    r"do you prefer|"
    r"please confirm|"
    r"let me know|"
    r"which (?:option|approach|one)|"
    r"need (?:your )?(?:input|confirmation|approval|answer)|"
    r"awaiting (?:your )?(?:input|answer|confirmation)|"
    r"y/n"
    r")\b",
    re.IGNORECASE,
)
# Heuristics for "session has finished a task" (proactive announcement)
_COMPLETE_PATTERNS = re.compile(
    r"(?:"
    r"\bdone\b\s*[.!]|"
    r"\bshipped\b\s*[.!]|"
    r"\bfinished\b\s*[.!]|"
    r"\bcomplete[d]?\b\s*[.!]|"
    r"\bdeployed\b\s*[.!]|"
    r"\bpushed\b\s*[.!]|"
    r"\bmerged\b\s*[.!]|"
    r"\ball set\b|"
    r"\bready (?:for|to) review\b|"
    r"\btests? pass(?:ing|ed)?\b|"
    r"\ball green\b|"
    r"\bsuccessfully\s+\w+ed\b|"
    r"^✅|^✔|^\*\*Done"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
# States a session can be in for our purposes
STATE_PENDING = "pending"     # needs the user's input / answer
STATE_COMPLETE = "complete"   # finished a task — announcement worthy
STATE_IDLE = "idle"           # nothing to surface


@dataclass
class OpenSession:
    session_id: str
    project_key: str          # encoded cwd dirname e.g. "-Users-vaibhav"
    project_label: str        # last segment for display, e.g. "vaibhav" or "riff"
    title: str                # ai-title or fallback
    jsonl_path: Path
    cwd: str                  # absolute cwd of the running claude process
    last_modified: float
    last_assistant_text: str
    awaiting_input: bool      # legacy alias for state == STATE_PENDING
    state: str                # STATE_PENDING | STATE_COMPLETE | STATE_IDLE
    user_turns: int
    permission_mode: str = "default"  # default | acceptEdits | bypassPermissions | plan
    pid: int = 0
    proc_started: float = 0.0

    def short_title(self, n: int = 38) -> str:
        t = self.title.strip() or "(untitled)"
        return (t[: n - 1] + "…") if len(t) > n else t


def list_open_sessions() -> list[OpenSession]:
    """Return sessions backed by a live `claude` process, oldest-process first
    so callsign numbering is stable across refreshes (UNIT-1 = oldest running
    terminal, UNIT-N = most recently spawned)."""
    if not PIDS_DIR.exists():
        return []
    entries: list[tuple[float, OpenSession]] = []
    for pidfile in PIDS_DIR.glob("*.json"):
        try:
            reg = json.loads(pidfile.read_text())
        except Exception:
            continue
        pid = reg.get("pid")
        session_id = reg.get("sessionId")
        cwd = reg.get("cwd") or ""
        kind = reg.get("kind", "")
        if not pid or not session_id:
            continue
        if kind and kind != "interactive":
            continue
        if not _pid_alive(int(pid)):
            continue
        project_key = _cwd_to_project_key(cwd)
        jsonl_path = PROJECTS_DIR / project_key / f"{session_id}.jsonl"
        if not jsonl_path.exists():
            continue
        info = _parse_session(jsonl_path)
        if info is None or info.user_turns < MIN_USER_TURNS:
            continue
        proc_started = float(reg.get("startedAt", 0)) / 1000.0 or info.last_modified
        info.proc_started = proc_started
        info.pid = int(pid)
        info.cwd = cwd
        entries.append((proc_started, info))
    entries.sort(key=lambda x: x[0])  # oldest process first
    return [e[1] for e in entries]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _cwd_to_project_key(cwd: str) -> str:
    """Mirror Claude Code's filesystem encoding: '/Users/vaibhav' -> '-Users-vaibhav'."""
    return cwd.replace("/", "-")


def _parse_session(p: Path) -> OpenSession | None:
    title = ""
    last_assistant_text = ""
    user_turns = 0
    permission_mode = "default"
    try:
        with p.open("rb") as f:
            for raw in f:
                try:
                    j = json.loads(raw)
                except Exception:
                    continue
                t = j.get("type")
                if t == "ai-title":
                    title = j.get("aiTitle", "") or title
                elif t == "permission-mode":
                    # Claude writes a new entry each time the user toggles
                    # the mode; the last one wins.
                    pm = j.get("permissionMode")
                    if pm:
                        permission_mode = pm
                elif t == "user":
                    user_turns += 1
                elif t == "assistant":
                    text = _extract_assistant_text(j)
                    if text:
                        last_assistant_text = text
    except OSError:
        return None

    state = _classify(last_assistant_text)
    return OpenSession(
        session_id=p.stem,
        project_key=p.parent.name,
        project_label=_pretty_project(p.parent.name),
        title=title or "(untitled)",
        jsonl_path=p,
        cwd="",
        last_modified=p.stat().st_mtime,
        last_assistant_text=last_assistant_text,
        awaiting_input=(state == STATE_PENDING),
        state=state,
        user_turns=user_turns,
        permission_mode=permission_mode,
    )


def _extract_assistant_text(j: dict) -> str:
    m = j.get("message") or {}
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        for it in c:
            if isinstance(it, dict) and it.get("type") == "text":
                return it.get("text", "")
    return ""


def _pretty_project(key: str) -> str:
    parts = key.lstrip("-").split("-")
    # encoded cwd: -Users-vaibhav-claude-apps-riff -> "riff"
    return parts[-1] if parts else key


def _is_awaiting_input(text: str) -> bool:
    if not text:
        return False
    tail = text.strip()[-400:]
    if _QUESTION_TAIL.search(tail):
        return True
    if _NEEDS_INPUT.search(tail):
        return True
    return False


def _classify(text: str) -> str:
    """Three-way classifier — pending wins over complete (a question after a
    completion announcement still needs an answer)."""
    if not text:
        return STATE_IDLE
    if _is_awaiting_input(text):
        return STATE_PENDING
    # Look in the LAST chunk of the message — completion lines usually trail.
    tail = text.strip()[-500:]
    if _COMPLETE_PATTERNS.search(tail):
        return STATE_COMPLETE
    return STATE_IDLE


# --------------- talk INTO a session ---------------

def send_to_session(session_id: str, message: str, *,
                    cwd: str = "", timeout: int = 60) -> str:
    """Inject a message into an existing session via `claude -p --resume`.

    IMPORTANT: claude --resume looks for the session in the *current working
    directory's* project bucket — so we must invoke from the session's own
    cwd, otherwise we get "No conversation found with session ID: …".
    """
    import subprocess

    try:
        result = subprocess.run(
            ["claude", "-p", "--resume", session_id, message],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
        )
    except subprocess.TimeoutExpired:
        return "(session timed out, over.)"
    out = (result.stdout or "").strip()
    if not out:
        err = (result.stderr or "").strip()
        if err:
            return f"(session error: {err[:200]})"
        return "(no reply)"
    return out

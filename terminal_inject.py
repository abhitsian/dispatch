"""Type a transcribed voice message directly into the terminal where a Claude
session is running, so the message appears in the user's chat window AND
executes — instead of going through `claude -p --resume` (which writes to the
jsonl but the running interactive process never sees the new turn).

Mechanism: locate the tty associated with the session's pid, then drive
iTerm2 or Terminal.app via AppleScript to write text into the matching
session. Both apps' AppleScript dialects auto-submit (press Enter) on
`write text` / `do script`.

Supported terminals: iTerm2, Terminal.app. Anything else (Warp, Alacritty,
kitty, etc.) will fail gracefully — caller should fall back to claude --resume.
"""
from __future__ import annotations

import subprocess


def find_tty(pid: int) -> str | None:
    """Return /dev/ttysNN for the pid, or None if not a tty-backed process."""
    if not pid:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return None
    tty = (result.stdout or "").strip()
    if not tty or tty == "?":
        return None
    if not tty.startswith("/dev/"):
        tty = "/dev/" + tty
    return tty


def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _app_running(name: str) -> bool:
    """Check if a Mac app is currently running via osascript."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to (name of processes) contains "{name}"'],
            capture_output=True, text=True, timeout=3,
        )
        return "true" in (result.stdout or "").lower()
    except Exception:
        return False


def inject_iterm(tty: str, message: str) -> bool:
    """Write `message` into the iTerm2 session whose tty matches. Press Enter."""
    if not (_app_running("iTerm2") or _app_running("iTerm")):
        return False
    msg = _escape_applescript(message)
    script = (
        'tell application "iTerm"\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        '      repeat with s in sessions of t\n'
        f'        if tty of s is "{tty}" then\n'
        f'          tell s to write text "{msg}"\n'
        '          return "ok"\n'
        '        end if\n'
        '      end repeat\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return "no_match"\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return False
    return "ok" in (result.stdout or "")


def inject_terminal(tty: str, message: str) -> bool:
    """Write `message` into the Terminal.app tab whose tty matches. Press Enter."""
    if not _app_running("Terminal"):
        return False
    msg = _escape_applescript(message)
    # `do script ... in selected tab of ...` syntax preserves the existing
    # window/tab — vs `do script "x"` which spawns a new Terminal window.
    script = (
        'tell application "Terminal"\n'
        '  repeat with w in windows\n'
        '    repeat with t in tabs of w\n'
        f'      if tty of t is "{tty}" then\n'
        f'        do script "{msg}" in t\n'
        '        return "ok"\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return "no_match:" & tty\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=8,
        )
    except Exception as exc:
        last_inject_error[tty] = f"osascript crashed: {exc}"
        return False
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if "ok" in out:
        return True
    # Capture the real reason so the dispatcher can log it.
    last_inject_error[tty] = (
        f"exit={result.returncode} stdout={out!r} stderr={err!r}"
    )
    return False


# Module-level diagnostic store so dispatch can include the real osascript
# error in the channel log (e.g. "errAEEventNotPermitted" when Automation
# permission for Dispatch.app → Terminal isn't granted).
last_inject_error: dict[str, str] = {}


def inject(pid: int, message: str) -> tuple[bool, str]:
    """Try injecting into iTerm then Terminal. Returns (success, reason)."""
    tty = find_tty(pid)
    if not tty:
        return False, f"no tty for pid {pid}"
    msg = message.strip()
    if len(msg) > 2000:
        msg = msg[:2000]
    if inject_iterm(tty, msg):
        return True, f"injected into iTerm session at {tty}"
    if inject_terminal(tty, msg):
        return True, f"injected into Terminal tab at {tty}"
    why = last_inject_error.get(tty, "")
    if why:
        return False, f"no terminal app has a session at {tty} | {why[:160]}"
    return False, f"no terminal app has a session at {tty}"

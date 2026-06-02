"""Install or uninstall the Dispatch PreToolUse hook in ~/.claude/settings.local.json.

By default the hook matches `Bash|Write|Edit|WebFetch` — the tools where you
likely want a 'should I let this run?' check. Read tools (Read, Glob, Grep) are
deliberately not matched so safe lookups don't gate.

Idempotent: re-running install is fine; uninstall only removes our entry.

Usage:
    python install_hook.py status
    python install_hook.py install [--matcher "Bash|Write|Edit|WebFetch"]
    python install_hook.py uninstall
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.local.json"
HOOK_CMD = str(Path(__file__).resolve().parent / "hooks" / "pretooluse.sh")
PROMPT_HOOK_CMD = str(Path(__file__).resolve().parent / "hooks" / "userpromptsubmit.sh")
DEFAULT_MATCHER = "Bash|Write|Edit|WebFetch|Task|WebSearch"
# Stable ids we use to recognise our own hook entries.
HOOK_TAG = "dispatch:pretooluse"
PROMPT_HOOK_TAG = "dispatch:userpromptsubmit"


def _load() -> dict:
    if not SETTINGS.exists():
        return {}
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def _is_ours(entry: dict) -> bool:
    """An entry is ours if any of its `hooks` references our PreToolUse script path or tag."""
    for h in entry.get("hooks", []) or []:
        cmd = h.get("command", "") or ""
        if HOOK_CMD in cmd or HOOK_TAG in cmd:
            return True
    return False


def _is_ours_prompt(entry: dict) -> bool:
    """An entry is ours if it references our UserPromptSubmit script path or tag."""
    for h in entry.get("hooks", []) or []:
        cmd = h.get("command", "") or ""
        if PROMPT_HOOK_CMD in cmd or PROMPT_HOOK_TAG in cmd:
            return True
    return False


def status() -> int:
    data = _load()
    entries = data.get("hooks", {}).get("PreToolUse", []) or []
    ours = [e for e in entries if _is_ours(e)]
    if ours:
        print(f"installed — {len(ours)} entry/entries in {SETTINGS}")
        for e in ours:
            print(f"  matcher = {e.get('matcher', '*')!r}")
            for h in e.get("hooks", []):
                print(f"    command = {h.get('command')}")
        return 0
    print(f"not installed (no dispatch entry in {SETTINGS})")
    return 1


def install(matcher: str) -> int:
    data = _load()
    hooks_root = data.setdefault("hooks", {})

    # --- PreToolUse (gate + M3a sub-agent rewriter) ---
    pre = hooks_root.setdefault("PreToolUse", [])
    pre[:] = [e for e in pre if not _is_ours(e)]
    pre.append({
        "matcher": matcher,
        "hooks": [{
            "type": "command",
            "command": HOOK_CMD,
            "timeout": 130,
        }],
    })

    # --- UserPromptSubmit (M3b prompt router) ---
    # The hook fails open when m3b_prompt_router is off, so installing it
    # always is harmless — the feature flag controls behavior.
    ups = hooks_root.setdefault("UserPromptSubmit", [])
    ups[:] = [e for e in ups if not _is_ours_prompt(e)]
    ups.append({
        "hooks": [{
            "type": "command",
            "command": PROMPT_HOOK_CMD,
            "timeout": 8,
        }],
    })

    _save(data)
    print(f"installed -> {SETTINGS}")
    print(f"  PreToolUse matcher = {matcher!r}")
    print(f"    command = {HOOK_CMD}")
    print(f"  UserPromptSubmit (no matcher — fires on every prompt)")
    print(f"    command = {PROMPT_HOOK_CMD}")
    print("  start a NEW claude session for this to take effect.")
    return 0


def uninstall() -> int:
    data = _load()
    pre = data.get("hooks", {}).get("PreToolUse", []) or []
    ups = data.get("hooks", {}).get("UserPromptSubmit", []) or []
    before_pre = len(pre)
    before_ups = len(ups)
    pre[:] = [e for e in pre if not _is_ours(e)]
    ups[:] = [e for e in ups if not _is_ours_prompt(e)]
    removed = (before_pre - len(pre)) + (before_ups - len(ups))
    if removed == 0:
        print("nothing to remove")
        return 1
    # tidy up empty containers
    if not pre:
        data.get("hooks", {}).pop("PreToolUse", None)
    if not ups:
        data.get("hooks", {}).pop("UserPromptSubmit", None)
    if not data.get("hooks"):
        data.pop("hooks", None)
    _save(data)
    print(f"uninstalled — {removed} entry/entries removed from {SETTINGS}")
    return 0


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    pi = sub.add_parser("install")
    pi.add_argument("--matcher", default=DEFAULT_MATCHER)
    sub.add_parser("uninstall")
    args = p.parse_args()
    if args.cmd == "status":
        sys.exit(status())
    if args.cmd == "install":
        sys.exit(install(args.matcher))
    if args.cmd == "uninstall":
        sys.exit(uninstall())


if __name__ == "__main__":
    main()
